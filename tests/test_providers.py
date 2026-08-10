"""PROVIDERS — DeepSeek client wire-format verified against a LOCAL mock
server that mimics the real SSE stream (no egress needed). Validates:
request shape, incremental token parsing, usage accounting, error handling,
json_mode, and the offline SimProvider contract."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from core.providers import DeepSeekProvider, SimProvider, SimSearch


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class MockDeepSeek:
    """Tiny HTTP server speaking DeepSeek's /chat/completions SSE dialect."""

    def __init__(self, chunks=None, status=200, delay_each=0.0, capture=None):
        self.chunks = chunks or ["Hel", "lo ", "world"]
        self.status = status
        self.delay = delay_each
        self.capture = capture or {}
        self.port = None
        self._srv = None
        self._thread = None

    def start(self):
        import socket
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                req_body = self.rfile.read(length)
                capture = self.server.mock.capture
                status = self.server.mock.status
                capture["body"] = json.loads(req_body)
                capture["auth"] = self.headers.get("Authorization", "")
                self.send_response(status)
                body = capture.get("body", {})
                streaming = body.get("stream", True)
                if status != 200:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"bad key"}')
                    return
                if not streaming:
                    # plain JSON response for stream=False (complete())
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "id": "chatcmpl-mock", "object": "chat.completion",
                        "model": "deepseek-chat",
                        "choices": [{"index": 0, "message": {"role": "assistant",
                                                             "content": "mock complete reply"},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 3,
                                  "total_tokens": 8},
                    }).encode())
                    return
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                prompt_tokens = 10
                chunks = self.server.mock.chunks
                for i, c in enumerate(chunks):
                    obj = {
                        "id": "chatcmpl-mock", "object": "chat.completion.chunk",
                        "model": "deepseek-chat",
                        "choices": [{"index": 0, "delta": {"content": c}, "finish_reason": None}],
                    }
                    if i == len(chunks) - 1:
                        obj["choices"][0]["finish_reason"] = "stop"
                    self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                    self.wfile.flush()
                    if self.server.mock.delay:
                        import time
                        time.sleep(self.server.mock.delay)
                usage = {"prompt_tokens": prompt_tokens, "completion_tokens": 7,
                         "prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 7,
                         "total_tokens": 17}
                self.wfile.write(f"data: {json.dumps({'usage': usage})}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def log_message(self, *a):
                pass

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self._srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self._srv.mock = self
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()


@pytest.fixture()
def mock_ds():
    m = MockDeepSeek(chunks=['{"ctrl":{"depth":0.2,"config_deltas":{},"memory_writes":[],"code_intent":false,"ask":[]}}', "Sure", ", here", " you go"]).start()
    yield m
    m.stop()


def test_deepseek_stream_parses_sse(mock_ds):
    p = DeepSeekProvider(api_key="sk-test", base_url=f"http://127.0.0.1:{mock_ds.port}/v1")
    out = []
    async def go():
        async for c in p.stream([{"role": "user", "content": "hi"}]):
            out.append(c)
    _run(go())
    assert "".join(out) == '{"ctrl":{"depth":0.2,"config_deltas":{},"memory_writes":[],"code_intent":false,"ask":[]}}Sure, here you go'
    body = mock_ds.capture["body"]
    assert body["stream"] is True
    assert body["model"] == "deepseek-chat"
    assert mock_ds.capture["auth"] == "Bearer sk-test"


def test_deepseek_json_mode(mock_ds):
    p = DeepSeekProvider(api_key="sk-test", base_url=f"http://127.0.0.1:{mock_ds.port}/v1")
    _run(p.complete([{"role": "user", "content": "x"}], json_mode=True))
    assert mock_ds.capture["body"].get("response_format") == {"type": "json_object"}


def test_deepseek_error_surface(mock_ds):
    mock_ds.status = 401
    p = DeepSeekProvider(api_key="sk-wrong", base_url=f"http://127.0.0.1:{mock_ds.port}/v1")
    with pytest.raises(RuntimeError) as e:
        _run(p.complete([{"role": "user", "content": "hi"}]))
    assert "401" in str(e.value)


def test_usage_cost_math():
    from core.providers import LLMProvider
    usage = {"prompt_cache_hit_tokens": 1000000, "prompt_cache_miss_tokens": 1000000,
             "completion_tokens": 1000000}
    cost = LLMProvider.usage_cost(usage)
    # 1M cached @0.014 + 1M new @0.27 + 1M out @1.10
    assert abs(cost - (0.014 + 0.27 + 1.10)) < 1e-6


def test_sim_provider_contract():
    """The offline provider must emit a valid CTRL block then prose — the
    same contract the real model is held to."""
    sp = SimProvider(search=SimSearch())
    out = _run(sp.complete([
        {"role": "system", "content": "<SLOTS>[{\"text\":\"User has a dentist appointment on 2026-08-25 at 11:00 AM\"}]</SLOTS><NOW>{}</NOW>"},
        {"role": "user", "content": "does my dentist appointment clash?"}]))
    ctrl_json, _, prose = out.partition("\n")
    ctrl = json.loads(ctrl_json)
    assert "ctrl" in ctrl
    assert isinstance(ctrl["ctrl"]["config_deltas"], dict)
    assert isinstance(ctrl["ctrl"]["memory_writes"], list)
    assert "dentist" in prose.lower()


def test_sim_search_fixtures():
    s = SimSearch()
    res = _run(s.search("amarnath yatra dates 2026"))
    assert any("Yatra" in r["title"] for r in res)
    res2 = _run(s.search("completely unrelated topic xyz"))
    assert len(res2) >= 1  # fails open


def test_duckduckgo_html_parser():
    from core.providers import DuckDuckGoSearch
    html = '''
    <div class="result results_links deep_link 1">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fevents&amp;rut=x">Events in Ahmedabad <b>today</b></a>
      <a class="result__snippet" href="//duckduckgo.com/l/?uddg=...">Heritage walk at the riverfront, free entry.</a>
    </div>
    '''
    out = DuckDuckGoSearch._parse_html(html, 5)
    assert len(out) == 1
    assert out[0]["url"] == "https://example.com/events"
    assert "Ahmedabad" in out[0]["title"]
    assert "riverfront" in out[0]["snippet"].lower()


def test_duckduckgo_lite_parser():
    from core.providers import DuckDuckGoSearch
    html = '''
    <a rel="nofollow" href="https://lite.example.org/show">Science City Planetarium show</a>
    <td class="result-snippet">Astronomy night at 8pm &amp;ndash; free.</td>
    '''
    out = DuckDuckGoSearch._parse_lite(html, 5)
    assert len(out) == 1
    assert out[0]["url"] == "https://lite.example.org/show"
    assert "Planetarium" in out[0]["title"]


def test_make_search_defaults():
    import os as _os
    from core.providers import make_search
    _os.environ.pop("TAVILY_API_KEY", None)
    _os.environ["SEARCH_PROVIDER"] = "duckduckgo"
    assert make_search().__class__.__name__ == "DuckDuckGoSearch"
    _os.environ["SEARCH_PROVIDER"] = "sim"
    assert make_search().__class__.__name__ == "SimSearch"
    _os.environ.pop("SEARCH_PROVIDER", None)
    assert make_search().__class__.__name__ == "DuckDuckGoSearch"


def test_bing_parser():
    from core.providers import BingSearch
    html = '''
    <li class="b_algo"><h2><a href="https://example.org/garba">Navratri Garba Night in Ahmedabad</a></h2>
    <p>Garba at Sabarmati riverfront tonight, entry Rs 200.</p></li>
    '''
    out = BingSearch._parse(html, 5)
    assert len(out) == 1
    assert out[0]["url"] == "https://example.org/garba"
    assert "Garba" in out[0]["title"]
    assert "riverfront" in out[0]["snippet"].lower()


# =========================================================================== #
# Gemini provider — message/tool conversion + routing (no network)
# =========================================================================== #
def test_gemini_contents_conversion():
    from core.providers import _gemini_contents
    messages = [
        {"role": "system", "content": "You are Friday."},
        {"role": "user", "content": "who are you"},
        {"role": "assistant", "content": "I'm Friday."},
        {"role": "user", "content": "search the web"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "name": "web_search",
                         "arguments": '{"query": "ahmedabad events"}'}]},
        {"role": "tool", "name": "web_search", "content": "results here"},
        {"role": "user", "content": "thanks"},
    ]
    contents, sys_parts = _gemini_contents(messages)
    assert sys_parts == [{"text": "You are Friday."}]
    assert contents[0]["role"] == "user"
    assert contents[0]["parts"][0]["text"] == "who are you"
    assert contents[1]["role"] == "model" and contents[1]["parts"][0]["text"] == "I'm Friday."
    # assistant tool_call → functionCall part (index 3 after sys/user/model/user)
    fc = contents[3]
    assert fc["role"] == "model"
    assert fc["parts"][0]["functionCall"]["name"] == "web_search"
    assert fc["parts"][0]["functionCall"]["args"] == {"query": "ahmedabad events"}
    # tool response → functionResponse part; consecutive users merged
    tr = contents[4]
    assert tr["role"] == "user"
    assert tr["parts"][0]["functionResponse"]["name"] == "web_search"
    assert tr["parts"][0]["functionResponse"]["response"]["result"] == "results here"
    # trailing user merged into the same user turn
    texts = " ".join(p.get("text", "") for p in tr["parts"])
    assert "thanks" in texts


def test_gemini_tools_conversion():
    from core.providers import _gemini_tools, TOOL_SCHEMAS
    g = _gemini_tools(TOOL_SCHEMAS)
    assert g and len(g) == 1
    decls = g[0]["functionDeclarations"]
    names = [d["name"] for d in decls]
    assert names == ["web_search", "web_read", "memory_recall"]
    assert decls[0]["parameters"]["required"] == ["query"]


def test_gemini_provider_parses_tool_response():
    """complete_tools must convert Gemini functionCall parts back into the
    OpenAI-style shape cortex expects ({content, tool_calls:[{name,arguments}]})."""
    from core.providers import GeminiProvider
    p = GeminiProvider(api_key="AIza-test-key")
    # monkeypatch the network call
    async def fake_post(url, **kwargs):
        class _R:
            status_code = 200
            text = ""
            async def aread(self):
                return b""
            def json(self):
                return {"candidates": [{
                    "content": {"parts": [
                        {"functionCall": {"name": "web_search",
                                          "args": {"query": "garba ahmedabad"}}}]},
                    "finishReason": "STOP"}]}
        return _R()
    _fake = type("C", (), {})()
    _fake.post = fake_post          # instance attr → not bound, url stays positional
    p.client = lambda: _fake
    import asyncio
    out = asyncio.get_event_loop().run_until_complete(
        p.complete_tools([{"role": "user", "content": "find garba"}],
                         [{"type": "function", "function": {"name": "web_search"}}]))
    assert out["tool_calls"][0]["name"] == "web_search"
    import json as _json
    assert _json.loads(out["tool_calls"][0]["arguments"]) == {"query": "garba ahmedabad"}


def test_make_llm_gemini_routing(db):
    """llm.provider=gemini + a saved gemini key → GeminiProvider wins."""
    import os as _os
    from core.providers import make_llm
    _os.environ.pop("DEEPSEEK_API_KEY", None)
    _os.environ.pop("GEMINI_API_KEY", None)
    db.set_setting("llm.provider", "gemini")
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('gemini','default','AIza-fake-1234567890',1,'admin',?,?)",
            (1, 1))
    p = make_llm()
    assert p.name == "gemini", p.name
    # model override honored
    db.set_setting("llm.model", "gemini-2.0-flash")
    p2 = make_llm()
    assert p2.model == "gemini-2.0-flash"
    # switching back to deepseek without a deepseek key → env fallback → sim
    db.set_setting("llm.provider", "deepseek")
    db.exec("DELETE FROM provider_keys WHERE provider='gemini'")
    p3 = make_llm()
    assert p3.name in ("sim", "deepseek")


def test_gemini_model_chain_falls_forward_on_404():
    """Google retired gemini-2.5-flash for new users (July 2026). When the
    configured model 404s with 'no longer available', the provider must try
    the next candidate in the GA chain instead of failing."""
    from core.providers import GeminiProvider
    p = GeminiProvider(api_key="AIza-test-key", model="gemini-2.5-flash")
    assert p._models[0] == "gemini-2.5-flash"
    assert "gemini-3.6-flash" in p._models

    calls = []

    async def fake_post(url, **kwargs):
        calls.append(url)
        body_err = b'{"error": {"message": "This model models/gemini-2.5-flash is no longer available to new users. Please update your code to use a newer model"}}'
        class _R:
            status_code = 404 if "gemini-2.5-flash" in url else 200
            text = body_err.decode() if status_code == 404 else ""
            async def aread(self):
                return body_err if self.status_code == 404 else b""
            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": "hi from fallback"}]}}]}
        return _R()

    _fake = type("C", (), {})()
    _fake.post = fake_post
    p.client = lambda: _fake
    import asyncio
    out = asyncio.get_event_loop().run_until_complete(
        p.complete([{"role": "user", "content": "ping"}], max_tokens=5))
    assert out == "hi from fallback"
    assert len(calls) == 2, calls          # first model 404'd, second succeeded
    assert "gemini-2.5-flash" in calls[0] and "gemini-3.6-flash" in calls[1]


def test_make_llm_scope_routing(db):
    """Per-scope models: research can run on gemini while chat stays on
    deepseek; scopes inherit chat model only when provider matches."""
    import os as _os
    from core.providers import make_llm
    _os.environ.pop("DEEPSEEK_API_KEY", None)
    _os.environ.pop("GEMINI_API_KEY", None)
    db.set_setting("llm.provider", "deepseek")
    db.set_setting("llm.model", "")
    db.set_setting("llm.research.provider", "gemini")
    db.set_setting("llm.research.model", "gemini-3.6-flash")
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('gemini','default','AIza-fake-1234567890',1,'admin',?,?)", (1, 1))
    # chat → deepseek (no deepseek key → sim/env fallback)
    chat = make_llm("chat")
    assert chat.name in ("sim", "deepseek")
    # research → gemini with its own model
    res = make_llm("research")
    assert res.name == "gemini" and res.model == "gemini-3.6-flash"
    # books: no routing set → inherits chat provider (deepseek)
    books = make_llm("books")
    assert books.name in ("sim", "deepseek")
    # gemini model string must NOT leak into a deepseek-scope call
    db.set_setting("llm.model", "gemini-3.6-flash")   # chat model = gemini string
    db.set_setting("llm.research.model", "")           # research clears its model
    res2 = make_llm("research")                        # research still gemini
    assert res2.name == "gemini"
    books2 = make_llm("books")                         # books = deepseek + NO gemini model
    assert books2.name in ("sim", "deepseek")
    if books2.name == "deepseek":
        assert "gemini" not in books2.model
    # cleanup
    db.exec("DELETE FROM provider_keys WHERE provider='gemini'")
    db.set_setting("llm.research.provider", None)
    db.set_setting("llm.research.model", None)
    db.set_setting("llm.model", None)
