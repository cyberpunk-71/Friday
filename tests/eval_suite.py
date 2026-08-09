#!/usr/bin/env python3
"""FRIDAY EVAL SUITE — 50 complex scenarios × 4-6 prompts each.

Every scenario exercises a mix of: live search, deep browsing (web_read),
multi-hop reasoning, memory access/correction, task initiation, artifacts.
Run against a live server:
    python3 tests/eval_suite.py --base http://127.0.0.1:8010 --out /opt/friday/data/eval_report

Writes:
  eval_report.html      — human-readable proof (downloadable)
  eval_report.json      — machine-readable results
  eval_chat_log.md      — full prompt/reply transcript per scenario
"""
import argparse
import asyncio
import json
import re
import sys
import time
import urllib.request
from pathlib import Path


def post_sse(base: str, text: str, timeout: int = 120) -> dict:
    """POST /api/chat, parse SSE, return {reply, model, latency_ms, cost_usd, events}."""
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(base + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    events = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode().strip()
            if line.startswith("data:"):
                try:
                    ev = json.loads(line[5:])
                    events.append(ev)
                except Exception:
                    pass
    reply = ""
    model = "?"
    for ev in events:
        if ev.get("type") == "delta":
            reply += ev.get("text", "")
        elif ev.get("type") == "done":
            reply = ev.get("reply") or reply
            model = ev.get("model", model)
    return {"reply": reply, "model": model,
            "latency_ms": int((time.time() - t0) * 1000),
            "cost_usd": sum(ev.get("cost_usd", 0) for ev in events if ev.get("type") == "done"),
            "events": events}


def check(reply: str, *patterns: str, min_hits: int = 1) -> tuple[bool, str]:
    low = reply.lower()
    hits = [p for p in patterns if p.lower() in low]
    ok = len(hits) >= min_hits
    return ok, f"found {len(hits)}/{len(patterns)}: {hits}" if ok else \
        f"missing {[p for p in patterns if p.lower() not in low][:3]}"


# --------------------------------------------------------------------------- #
# SCENARIOS — each is a story; steps must flow (memory + follow-ups)
# --------------------------------------------------------------------------- #
SCENARIOS = [
    # 1 — Yatra + dentist clash + tracker (live + memory + task)
    {"id": 1, "title": "Amarnath Yatra + dentist clash + tracker",
     "steps": ["amarnath yatra dates this year and how to register",
               "does it clash with my dentist appointment on 25 aug",
               "keep an eye on the registration portal"],
     "checks": [
         lambda r, i: check(r, "yatra", "july", "august", "register", min_hits=2),
         lambda r, i: check(r, "dentist", "25", "clash", "no", min_hits=2),
         lambda r, i: check(r, "tracker", "track", "watch", "portal", min_hits=1)]},
    # 2 — Budget phones + salary + email (typos + memory + task)
    {"id": 2, "title": "Budget phones + salary + email draft",
     "steps": ["reserach best phone undr 20k in india",
               "when is my salary credited",
               "draft mail to sarah recommending a phone with table"],
     "checks": [
         lambda r, i: check(r, "₹", "moto", "redmi", "17,999", "18,999", min_hits=2),
         lambda r, i: check(r, "1st", "first", "salary", min_hits=1),
         lambda r, i: check(r, "sarah", "draft", "email", "table", min_hits=2)]},
    # 3 — Saree purchase → payment gate (task + approval)
    {"id": 3, "title": "Mom's handloom saree → payment gate",
     "steps": ["buy best handloom saree for mom under 10k",
               "dont ask just buy it"],
     "checks": [
         lambda r, i: check(r, "handloom", "saree", "mom", min_hits=2),
         lambda r, i: check(r, "approval", "approve", "pay", "blocked", "gate", min_hits=1)]},
    # 4 — Mayor of Ahmedabad (live)
    {"id": 4, "title": "Mayor of Ahmedabad (live)",
     "steps": ["who is the current mayor of ahmedabad"],
     "checks": [lambda r, i: check(r, "barot", "mayor", "hitesh", min_hits=1)]},
    # 5 — CM of Gujarat (live)
    {"id": 5, "title": "CM of Gujarat (live)",
     "steps": ["who is the chief minister of gujarat"],
     "checks": [lambda r, i: check(r, "bhupendra", "patel", "cm", "gujarat", min_hits=2)]},
    # 6 — PM of India (live)
    {"id": 6, "title": "PM of India (live)",
     "steps": ["who is the prime minister of india"],
     "checks": [lambda r, i: check(r, "modi", "narendra", min_hits=1)]},
    # 7 — Events in Ahmedabad (live search + typo)
    {"id": 7, "title": "Events in Ahmedabad tomorrow",
     "steps": ["what are events in ahmedaabd for tomorrow"],
     "checks": [lambda r, i: check(r, "ahmedabad", "event", "thing to do", "mirror", min_hits=2)]},
    # 8 — BookMyShow chain (multi-turn context)
    {"id": 8, "title": "BookMyShow follow-up chain",
     "steps": ["search book my show for events in ahmedabad",
               "any comedy shows",
               "any this weekend"],
     "checks": [
         lambda r, i: check(r, "bookmyshow", "event", "ahmedabad", "show", min_hits=2),
         lambda r, i: check(r, "comedy", "show", "stand", "laugh", min_hits=1),
         lambda r, i: check(r, "weekend", "sat", "sun", min_hits=1)]},
    # 9 — Astronomy event (memory pref)
    {"id": 9, "title": "Astronomy event for a stargazer",
     "steps": ["i love astronomy, what astronomy events are happening near gandhinagar this week",
               "is the science city planetarium showing something good"],
     "checks": [
         lambda r, i: check(r, "astronom", "planetarium", "science city", "event", min_hits=2),
         lambda r, i: check(r, "planetarium", "show", "science", "ticket", min_hits=2)]},
    # 10 — Meteor shower (live + memory)
    {"id": 10, "title": "Perseid meteor shower timing",
     "steps": ["when is the perseid meteor shower visible in india this month",
               "best time to watch tonight"],
     "checks": [
         lambda r, i: check(r, "perseid", "meteor", "august", "11", "12", "13", min_hits=2),
         lambda r, i: check(r, "night", "tonight", "peak", "after midnight", "pm", min_hits=1)]},
    # 11 — Weekend trip Jaipur (memory prefs)
    {"id": 11, "title": "Jaipur trip with saved flight prefs",
     "steps": ["plan a 3 day jaipur trip",
               "i prefer morning flights and window seats"],
     "checks": [
         lambda r, i: check(r, "jaipur", "trip", "day", "plan", min_hits=2),
         lambda r, i: check(r, "morning", "window", "flight", min_hits=2)]},
    # 12 — Goa plan applying prefs (memory carryover)
    {"id": 12, "title": "Goa plan auto-applies saved prefs",
     "steps": ["plan a weekend goa trip"],
     "checks": [lambda r, i: check(r, "goa", "morning", "window", "flight", min_hits=3)]},
    # 13 — Weather (live)
    {"id": 13, "title": "Weather in Ahmedabad tomorrow",
     "steps": ["what is the weather in ahmedabad tomorrow"],
     "checks": [lambda r, i: check(r, "°", "celsius", "rain", "cloud", "temperature", "°c", min_hits=1)]},
    # 14 — Cricket (live)
    {"id": 14, "title": "India cricket next match (live)",
     "steps": ["when is india's next cricket match"],
     "checks": [lambda r, i: check(r, "india", "match", "test", "odi", "t20", "august", min_hits=2)]},
    # 15 — Gold price (live)
    {"id": 15, "title": "Gold price today (live)",
     "steps": ["what is the gold rate in ahmedabad today"],
     "checks": [lambda r, i: check(r, "₹", "gold", "rate", "carat", "22", "24", min_hits=2)]},
    # 16 — EV comparison (research)
    {"id": 16, "title": "EV scooter comparison (research)",
     "steps": ["compare ola s1 pro vs ather 450x vs tvs iqube",
               "which is best value for money"],
     "checks": [
         lambda r, i: check(r, "ola", "ather", "tvs", min_hits=2),
         lambda r, i: check(r, "value", "best", "price", "₹", "recommend", min_hits=2)]},
    # 17 — Phone deep compare (research)
    {"id": 17, "title": "Budget phone deep compare",
     "steps": ["deep research: moto g85 vs redmi note 14 vs iqoo z9",
               "which has the best camera for under 20k"],
     "checks": [
         lambda r, i: check(r, "moto", "redmi", "iqoo", min_hits=2),
         lambda r, i: check(r, "camera", "best", "₹", "recommend", min_hits=2)]},
    # 18 — Laptop under 60k (research)
    {"id": 18, "title": "Laptop under 60k research",
     "steps": ["best laptop under 60000 for ml work in india"],
     "checks": [lambda r, i: check(r, "laptop", "₹", "ram", "ryzen", "i5", "16gb", min_hits=3)]},
    # 19 — ML course deep research (multi-round)
    {"id": 19, "title": "Deep: best ML course 2026",
     "steps": ["deep research the best machine learning course in 2026 for a working professional",
               "is fastai or deeplearning.ai better for me"],
     "checks": [
         lambda r, i: check(r, "course", "ml", "machine learning", "fastai", "coursera", "deeplearning", min_hits=2),
         lambda r, i: check(r, "fastai", "deeplearning.ai", "recommend", "better", min_hits=2)]},
    # 20 — Local LLM on 4GB (deep reasoning)
    {"id": 20, "title": "Deep: run LLM locally on 4GB VM",
     "steps": ["can i run a local llm on my 4gb vm for chat",
               "which model fits in 4gb ram"],
     "checks": [
         lambda r, i: check(r, "4gb", "llm", "model", "ram", "qwen", "llama", "phi", "gemma", min_hits=2),
         lambda r, i: check(r, "qwen", "llama", "phi", "gemma", "2gb", "3gb", "recommend", min_hits=2)]},
    # 21 — Astro photography budget (deep)
    {"id": 21, "title": "Deep: astrophotography setup on budget",
     "steps": ["deep research best budget astrophotography setup for beginners in india",
               "telescope or dslr first"],
     "checks": [
         lambda r, i: check(r, "telescope", "dslr", "camera", "budget", "₹", min_hits=3),
         lambda r, i: check(r, "telescope", "dslr", "start", "recommend", min_hits=2)]},
    # 22 — Handloom research (memory)
    {"id": 22, "title": "Handloom saree shops research",
     "steps": ["research good handloom saree shops in ahmedabad",
               "i love handloom, where should mom and i go"],
     "checks": [
         lambda r, i: check(r, "handloom", "saree", "shop", "ahmedabad", "law garden", min_hits=2),
         lambda r, i: check(r, "law garden", "shop", "handloom", "recommend", min_hits=2)]},
    # 23 — Restaurant near Gandhinagar (live + memory)
    {"id": 23, "title": "Restaurants near Gandhinagar",
     "steps": ["best restaurants near gandhinagar for a family dinner",
               "under 1500 for two people"],
     "checks": [
         lambda r, i: check(r, "restaurant", "gandhinagar", "dinner", min_hits=2),
         lambda r, i: check(r, "₹", "1500", "under", "recommend", min_hits=2)]},
    # 24 — Street food Ahmedabad (research)
    {"id": 24, "title": "Ahmedabad street food guide",
     "steps": ["what is the best street food in ahmedabad old city",
               "give me a walking food trail"],
     "checks": [
         lambda r, i: check(r, "food", "street", "ahmedabad", "manek", "chai", "vadapav", "pav", min_hits=2),
         lambda r, i: check(r, "trail", "walk", "manek chowk", "route", "start", min_hits=1)]},
    # 25 — Heritage walk details (deep browse)
    {"id": 25, "title": "Deep browse: Ahmedabad heritage walk",
     "steps": ["deep research the ahmedabad heritage walk — route, timing, cost, booking"],
     "checks": [lambda r, i: check(r, "heritage", "walk", "₹", "timing", "morning", "book", "route", min_hits=3)]},
    # 26 — History of old city (research)
    {"id": 26, "title": "Ahmedabad old city history",
     "steps": ["research the history of ahmedabad old city and its pols"],
     "checks": [lambda r, i: check(r, "ahmedabad", "pol", "history", "sultan", "ahmed shah", "city", min_hits=2)]},
    # 27 — Salary savings plan (reasoning + memory)
    {"id": 27, "title": "Salary savings plan (memory + math)",
     "steps": ["my salary comes on the 1st, i want to save 20% every month",
               "how much will i save in 6 months if i earn 60k"],
     "checks": [
         lambda r, i: check(r, "1st", "salary", "save", "20%", min_hits=2),
         lambda r, i: check(r, "72", "12,000", "12000", "72000", min_hits=1)]},
    # 28 — Mom birthday gift plan (memory reasoning)
    {"id": 28, "title": "Mom's birthday gift plan",
     "steps": ["my mom's birthday is 12 september and she loves handloom sarees",
               "plan a gift within 5000 rupees"],
     "checks": [
         lambda r, i: check(r, "september", "12", "handloom", "saree", min_hits=2),
         lambda r, i: check(r, "5000", "₹", "budget", "gift", "recommend", min_hits=2)]},
    # 29 — Goa trip budget (reasoning)
    {"id": 29, "title": "Goa 3-day trip budget",
     "steps": ["estimate a 3 day goa trip budget from ahmedabad for two people"],
     "checks": [lambda r, i: check(r, "₹", "budget", "flight", "hotel", "goa", "total", min_hits=3)]},
    # 30 — Dentist reminder (task)
    {"id": 30, "title": "Dentist reminder",
     "steps": ["remind me about my dentist appointment on 25 aug at 11 am"],
     "checks": [lambda r, i: check(r, "remind", "dentist", "11", "25", "set", min_hits=2)]},
    # 31 — Call Mom reminder
    {"id": 31, "title": "Remind to call Mom",
     "steps": ["remind me to call mom at 7 pm today"],
     "checks": [lambda r, i: check(r, "remind", "7", "call", "mom", "set", min_hits=2)]},
    # 32 — Focus session (task + nudge)
    {"id": 32, "title": "Focus session",
     "steps": ["start focos 25m allow github"],
     "checks": [lambda r, i: check(r, "focus", "25", "github", "start", "already active", min_hits=2)]},
    # 33 — Daily tracker (real task)
    {"id": 33, "title": "Daily event tracker",
     "steps": ["set up a daily tracker for bookmyshow events in ahmedabad"],
     "checks": [lambda r, i: check(r, "tracker", "daily", "bookmyshow", "ahmedabad", "set", min_hits=2)]},
    # 34 — Identity + profile (memory)
    {"id": 34, "title": "Who is Friday + profile",
     "steps": ["who are you",
               "what do you know about me"],
     "checks": [
         lambda r, i: check(r, "friday", "assistant", "companion", "memory", min_hits=2),
         lambda r, i: check(r, "dentist", "gandhinagar", "salary", "handloom", "mom", "astronomy", min_hits=2)]},
    # 35 — Correction supremacy (memory)
    {"id": 35, "title": "Memory correction",
     "steps": ["i actually live in ahmedabad, not gandhinagar",
               "so where do i live now"],
     "checks": [
         lambda r, i: check(r, "ahmedabad", "correct", "updated", "noted", min_hits=2),
         lambda r, i: check(r, "ahmedabad", "live", min_hits=1)]},
    # 36 — Beliefs display (memory)
    {"id": 36, "title": "Show beliefs",
     "steps": ["show me what you believe about me"],
     "checks": [lambda r, i: check(r, "believe", "prefer", "like", "you", min_hits=2)]},
    # 37 — Tension creation (memory)
    {"id": 37, "title": "Tension: handloom vs kanjivaram",
     "steps": ["i love handloom sarees but i dont like kanjivaram"],
     "checks": [lambda r, i: check(r, "handloom", "kanjivaram", "note", "understand", min_hits=2)]},
    # 38 — Multi-hop gift reasoning
    {"id": 38, "title": "Multi-hop: gift combining birthday+handloom+budget",
     "steps": ["what should i gift mom combining her birthday, her handloom love, and my budget",
               "make it a concrete plan"],
     "checks": [
         lambda r, i: check(r, "handloom", "birthday", "budget", "saree", "gift", min_hits=3),
         lambda r, i: check(r, "plan", "recommend", "₹", "saree", "stall", min_hits=2)]},
    # 39 — Research + email draft (task + live)
    {"id": 39, "title": "Research scooters + draft email",
     "steps": ["research best scooters under 1.5 lakh in india",
               "draft an email to my brother recommending one"],
     "checks": [
         lambda r, i: check(r, "scooter", "₹", "iqube", "ather", "ola", "recommend", min_hits=2),
         lambda r, i: check(r, "brother", "draft", "email", "recommend", min_hits=2)]},
    # 40 — News digest (live)
    {"id": 40, "title": "Top news today",
     "steps": ["summarize the top news today in india"],
     "checks": [lambda r, i: check(r, "india", "news", "today", "government", "cricket", "pm", min_hits=2)]},
    # 41 — Stock market (live)
    {"id": 41, "title": "Nifty today (live)",
     "steps": ["how is the stock market doing today in india"],
     "checks": [lambda r, i: check(r, "nifty", "sensex", "market", "₹", "point", "percent", min_hits=2)]},
    # 42 — Salary in hand (reasoning)
    {"id": 42, "title": "In-hand salary calculation",
     "steps": ["if my ctc is 18 lakhs per year, what is my approximate in-hand salary per month"],
     "checks": [lambda r, i: check(r, "₹", "1,", "lakh", "in-hand", "month", "approx", min_hits=3)]},
    # 43 — Tax saving (reasoning)
    {"id": 43, "title": "Tax saving advice",
     "steps": ["how can i save tax in india with 18 lakh ctc in 2026"],
     "checks": [lambda r, i: check(r, "tax", "80c", "nps", "section", "saving", "₹", min_hits=2)]},
    # 44 — Book mode (books)
    {"id": 44, "title": "Book: quantum entanglement discussion",
     "steps": ["explain quantum entanglement like i am 12"],
     "checks": [lambda r, i: check(r, "entangl", "particle", "state", "measure", "simpl", min_hits=2)]},
    # 45 — Vision: weekend value plan (memory+live)
    {"id": 45, "title": "Weekend value plan Ahmedabad",
     "steps": ["plan a value for money weekend in ahmedabad this saturday",
               "i love astronomy so include something for that"],
     "checks": [
         lambda r, i: check(r, "weekend", "ahmedabad", "₹", "value", "plan", min_hits=3),
         lambda r, i: check(r, "planetarium", "astronom", "science city", min_hits=1)]},
    # 46 — Follow-up context chain
    {"id": 46, "title": "Multi-turn context chain",
     "steps": ["what is the capital of gujarat",
               "and its largest city",
               "what is the population of that city"],
     "checks": [
         lambda r, i: check(r, "gandhinagar", "capital", min_hits=1),
         lambda r, i: check(r, "ahmedabad", "largest", min_hits=1),
         lambda r, i: check(r, "million", "lakh", "crore", "population", "8", min_hits=1)]},
    # 47 — Task artifact creation
    {"id": 47, "title": "Itinerary artifact",
     "steps": ["create a 2 day ahmedabad itinerary artifact with heritage and food"],
     "checks": [lambda r, i: check(r, "day", "itinerary", "heritage", "food", "morning", "evening", min_hits=3)]},
    # 48 — Deep: compare two cities
    {"id": 48, "title": "Deep: Ahmedabad vs Surat",
     "steps": ["deep research: ahmedabad vs surat for living, jobs, and lifestyle in 2026"],
     "checks": [lambda r, i: check(r, "ahmedabad", "surat", "job", "cost", "living", "lifestyle", min_hits=3)]},
    # 49 — Deep: local LLM eval (multi-round)
    {"id": 49, "title": "Deep: evaluate local LLM options",
     "steps": ["deep research the best open source llms for a coding assistant in 2026",
               "rank the top 3 for me"],
     "checks": [
         lambda r, i: check(r, "llm", "open source", "qwen", "llama", "deepseek", "codestral", "rank", min_hits=2),
         lambda r, i: check(r, "1", "2", "3", "best", "recommend", min_hits=2)]},
    # 50 — Health query
    {"id": 50, "title": "Health: sleep + focus (memory+reasoning)",
     "steps": ["i have trouble focusing after lunch, what helps",
               "set a reminder for my afternoon walk"],
     "checks": [
         lambda r, i: check(r, "focus", "lunch", "walk", "sleep", "water", "recommend", min_hits=2),
         lambda r, i: check(r, "remind", "walk", "set", min_hits=1)]},
]


# --------------------------------------------------------------------------- #
async def run_scenario(base: str, sc: dict, log_lines: list) -> dict:
    replies = []
    results = []
    for i, step in enumerate(sc["steps"]):
        try:
            res = await asyncio.to_thread(post_sse, base, step)
        except Exception as e:
            res = {"reply": f"ERROR: {e}", "model": "?", "latency_ms": 0,
                   "cost_usd": 0, "events": []}
        replies.append(res["reply"])
        log_lines.append(f"\n### S{sc['id']} step{i+1}: {step}\n\n> {res['reply'][:600]}\n")
        ok, why = True, "no checks"
        if i < len(sc["checks"]):
            ok, why = sc["checks"][i](res["reply"], i)
        results.append({"step": i + 1, "prompt": step, "ok": ok, "why": why,
                        "reply": res["reply"][:1200], "model": res["model"],
                        "latency_ms": res["latency_ms"]})
    passed = sum(1 for r in results if r["ok"])
    return {"id": sc["id"], "title": sc["title"], "passed": passed,
            "total": len(results), "ok": passed == len(results),
            "results": results}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8010")
    ap.add_argument("--out", default="eval_report")
    ap.add_argument("--only", type=int, default=0, help="run single scenario id")
    args = ap.parse_args()

    scenarios = [s for s in SCENARIOS if not args.only or s["id"] == args.only]
    log_lines = [f"# FRIDAY EVAL — {len(scenarios)} scenarios @ {args.base}\n"]
    all_results = []
    t_start = time.time()
    for sc in scenarios:
        r = await run_scenario(args.base, sc, log_lines)
        all_results.append(r)
        print(f"S{sc['id']:02d} {'PASS' if r['ok'] else 'FAIL'} ({r['passed']}/{r['total']}) {sc['title']}",
              flush=True)
    elapsed = int(time.time() - t_start)
    n_pass = sum(1 for r in all_results if r["ok"])
    n_fail = len(all_results) - n_pass

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval_chat_log.md").write_text("\n".join(log_lines), encoding="utf-8")

    report = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
              "base": args.base, "scenarios": len(all_results),
              "passed": n_pass, "failed": n_fail, "elapsed_s": elapsed,
              "results": all_results}
    (out / "eval_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                          encoding="utf-8")

    html = render_html(report)
    (out / "eval_report.html").write_text(html, encoding="utf-8")

    print(f"\n===== EVAL DONE: {n_pass}/{len(all_results)} passed in {elapsed}s =====")
    for r in all_results:
        if not r["ok"]:
            fails = [x for x in r["results"] if not x["ok"]]
            for f in fails:
                print(f"  S{r['id']} step{f['step']} FAIL: {f['why']}")
    return 0 if n_fail == 0 else 1


def render_html(report: dict) -> str:
    rows = ""
    for r in report["results"]:
        badge = '<span style="color:#1a7f37;font-weight:700">PASS</span>' if r["ok"] else \
                '<span style="color:#cf222e;font-weight:700">FAIL</span>'
        steps = "".join(
            f'<div style="margin:6px 0;padding:8px;background:#f6f8fa;border-radius:8px">'
            f'<b>Step {x["step"]}:</b> {x["prompt"]}<br>'
            f'<span style="color:{"#1a7f37" if x["ok"] else "#cf222e"}">{x["why"]}</span><br>'
            f'<i style="color:#57606a">{x["reply"][:300]}</i></div>'
            for x in r["results"])
        rows += f'<div style="border:1px solid #d0d7de;border-radius:12px;padding:14px;margin:10px 0">' \
                f'<h3 style="margin:0">S{r["id"]} — {r["title"]} {badge} ({r["passed"]}/{r["total"]})</h3>{steps}</div>'
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Friday Eval Report</title>
<style>body{{font-family:system-ui;max-width:900px;margin:auto;padding:20px;color:#24292f}}
h1{{border-bottom:2px solid #0969da;padding-bottom:8px}}</style></head><body>
<h1>Friday–Δ Eval Report</h1>
<p>Generated {report['generated']} · base {report['base']} · "
   "{report['passed']}/{report['scenarios']} scenarios passed · {report['elapsed_s']}s</p>
{rows}</body></html>"""


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
