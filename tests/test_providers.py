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
