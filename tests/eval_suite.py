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
    {"id": 1, "title": "Amarnath Yatra full chain",
     "steps": [
               "research the amarnath yatra 2026 dates and registration process",
               "read the official shrine board page for the registration steps",
               "my dentist appointment is on 25 aug at 11am — does the yatra clash with it",
               "keep an eye on when registration opens and remind me a week before"],
     "checks": [
         lambda r, i: check(r, "yatra", "july", "august", "register", "2026", min_hits=2),
         lambda r, i: check(r, "register", "registration", "certificate", "permit", "process", min_hits=1),
         lambda r, i: check(r, "dentist", "25", "clash", "no", min_hits=2),
         lambda r, i: check(r, "tracker", "remind", "portal", "eye", min_hits=1)]},
    {"id": 2, "title": "Deep phone research + salary + email",
     "steps": [
               "deep research the best 5g phones under 20000 in india 2026",
               "compare the top 3 by camera, battery and value",
               "when is my salary credited each month",
               "draft an email to sarah recommending the best one with a comparison table"],
     "checks": [
         lambda r, i: check(r, "moto", "redmi", "iqoo", "17", "999", "18", "999", min_hits=2),
         lambda r, i: check(r, "camera", "battery", "value", "recommend", min_hits=2),
         lambda r, i: check(r, "1st", "salary", "first", min_hits=1),
         lambda r, i: check(r, "sarah", "draft", "email", "table", min_hits=2)]},
    {"id": 3, "title": "Mom saree purchase with payment gate",
     "steps": [
               "my mom loves handloom sarees and her birthday is 12 september",
               "search for good handloom saree options under 10000 in ahmedabad",
               "recommend the best one with price and shop",
               "buy it for me dont ask just buy"],
     "checks": [
         lambda r, i: check(r, "handloom", "saree", "september", "birthday", min_hits=2),
         lambda r, i: check(r, "saree", "ahmedabad", "law garden", "nalli", min_hits=2),
         lambda r, i: check(r, "recommend", "price", min_hits=1),
         lambda r, i: check(r, "approval", "approve", "pay", "gate", min_hits=1)]},
    {"id": 4, "title": "Mayor deep chain",
     "steps": [
               "who is the current mayor of ahmedabad",
               "who was the mayor before them",
               "which party do they belong to and since when",
               "summarize the mayoral history in one line"],
     "checks": [
         lambda r, i: check(r, "barot", "hitesh", "mayor", min_hits=1),
         lambda r, i: check(r, "before", "previous", "prior", "mayor", min_hits=1),
         lambda r, i: check(r, "bjp", "party", "2021", "2026", "since", min_hits=1),
         lambda r, i: check(r, "mayor", "history", "barot", min_hits=1)]},
    {"id": 5, "title": "CM Gujarat deep chain",
     "steps": [
               "who is the chief minister of gujarat",
               "who was the cm before them",
               "how long has the current cm been in office",
               "summarize gujarat's recent cm history"],
     "checks": [
         lambda r, i: check(r, "bhupendra", "patel", "cm", min_hits=1),
         lambda r, i: check(r, "rupani", "vijay", "before", min_hits=1),
         lambda r, i: check(r, "2021", "year", "september", "since", min_hits=1),
         lambda r, i: check(r, "cm", "gujarat", "history", min_hits=1)]},
    {"id": 6, "title": "PM + finance minister chain",
     "steps": [
               "who is the prime minister of india",
               "who is the finance minister",
               "when was the last union budget presented",
               "who was the first finance minister of india"],
     "checks": [
         lambda r, i: check(r, "modi", "narendra", min_hits=1),
         lambda r, i: check(r, "sitharaman", "nirmala", "finance", min_hits=1),
         lambda r, i: check(r, "budget", "2026", "february", "july", min_hits=1),
         lambda r, i: check(r, "finance", "minister", "first", "india", min_hits=1)]},
    {"id": 7, "title": "Ahmedabad events deep chain",
     "steps": [
               "what are events in ahmedaabd for tomorrow",
               "any of them are comedy shows",
               "recommend one with ticket price if available",
               "plan to go with my budget of 1000 rupees"],
     "checks": [
         lambda r, i: check(r, "ahmedabad", "event", "thing to do", "mirror", min_hits=2),
         lambda r, i: check(r, "comedy", "show", "stand", "laugh", min_hits=1),
         lambda r, i: check(r, "ticket", "price", "recommend", min_hits=1),
         lambda r, i: check(r, "1000", "budget", "plan", min_hits=1)]},
    {"id": 8, "title": "BookMyShow multi-turn chain",
     "steps": [
               "search book my show for events in ahmedabad",
               "any comedy shows this weekend",
               "any live music or concerts",
               "which is the highest rated"],
     "checks": [
         lambda r, i: check(r, "bookmyshow", "event", "ahmedabad", "show", min_hits=2),
         lambda r, i: check(r, "comedy", "show", "stand", "laugh", min_hits=1),
         lambda r, i: check(r, "music", "concert", "live", min_hits=1),
         lambda r, i: check(r, "rated", "best", "recommend", min_hits=1)]},
    {"id": 9, "title": "Astronomy chain",
     "steps": [
               "i love astronomy and live in gandhinagar",
               "what astronomy events are near me this week",
               "is the science city planetarium worth visiting this weekend",
               "plan my saturday evening there within 500 rupees"],
     "checks": [
         lambda r, i: check(r, "astronom", "gandhinagar", min_hits=2),
         lambda r, i: check(r, "event", "planetarium", "show", "science", min_hits=2),
         lambda r, i: check(r, "planetarium", "show", "worth", "ticket", min_hits=2),
         lambda r, i: check(r, "500", "plan", "saturday", min_hits=1)]},
    {"id": 10, "title": "Perseid meteor shower chain",
     "steps": [
               "when is the perseid meteor shower in india this month",
               "what is the best time to watch it",
               "where near ahmedabad can i watch it with dark skies",
               "set a reminder for the peak night"],
     "checks": [
         lambda r, i: check(r, "peak", "aug", "12", "13", "eclipse", "morning", "night", min_hits=1),
         lambda r, i: check(r, "peak", "aug", "12", "13", "eclipse", "morning", "night", min_hits=1),
         lambda r, i: check(r, "dark", "thol", "nalsarovar", "outside", "city", min_hits=1),
         lambda r, i: check(r, "remind", "set", "peak", min_hits=1)]},
    {"id": 11, "title": "Jaipur trip full planning chain",
     "steps": [
               "plan a 3 day jaipur trip from ahmedabad",
               "i prefer morning flights and window seats",
               "find a good budget hotel near the old city",
               "make a day by day itinerary"],
     "checks": [
         lambda r, i: check(r, "jaipur", "trip", "day", "flight", min_hits=2),
         lambda r, i: check(r, "morning", "window", "flight", min_hits=2),
         lambda r, i: check(r, "hotel", "budget", "old city", min_hits=2),
         lambda r, i: check(r, "day", "itinerary", "morning", "evening", min_hits=2)]},
    {"id": 12, "title": "Goa weekend full plan",
     "steps": [
               "plan a weekend goa trip",
               "i prefer morning flights and window seats",
               "estimate the total budget for two people",
               "what are the must visit beaches"],
     "checks": [
         lambda r, i: check(r, "goa", "morning", "window", "flight", min_hits=3),
         lambda r, i: check(r, "morning", "window", "flight", min_hits=2),
         lambda r, i: check(r, "budget", "total", "two", min_hits=1),
         lambda r, i: check(r, "beach", "baga", "anjuna", "calangute", "palolem", min_hits=2)]},
    {"id": 13, "title": "Weather deep chain",
     "steps": [
               "what is the weather in ahmedabad today",
               "what about tomorrow",
               "should i plan an outdoor evening tomorrow",
               "what is the best time to visit ahmedabad weather wise"],
     "checks": [
         lambda r, i: check(r, "celsius", "rain", "cloud", "temperature", min_hits=1),
         lambda r, i: check(r, "tomorrow", "rain", "cloud", min_hits=1),
         lambda r, i: check(r, "outdoor", "evening", "plan", "yes", "no", min_hits=1),
         lambda r, i: check(r, "weather", "best", "visit", "winter", "season", min_hits=1)]},
    {"id": 14, "title": "Cricket deep chain",
     "steps": [
               "when is india's next cricket match",
               "which series is it part of",
               "what is the venue",
               "who is india's best batsman in this series"],
     "checks": [
         lambda r, i: check(r, "india", "match", "test", "odi", "t20", min_hits=2),
         lambda r, i: check(r, "series", "tour", "bilateral", min_hits=1),
         lambda r, i: check(r, "venue", "stadium", "ground", min_hits=1),
         lambda r, i: check(r, "india", "batsman", "series", "best", min_hits=1)]},
    {"id": 15, "title": "Gold price deep chain",
     "steps": [
               "what is the gold rate in ahmedabad today",
               "is the price rising or falling this week",
               "is it a good time to buy gold jewellery",
               "what affects gold prices in india"],
     "checks": [
         lambda r, i: check(r, "gold", "buy", "price", "time", "trend", min_hits=1),
         lambda r, i: check(r, "rising", "falling", "up", "down", "week", min_hits=1),
         lambda r, i: check(r, "gold", "buy", "price", "time", "trend", min_hits=1),
         lambda r, i: check(r, "gold", "price", "affect", "dollar", "inflation", min_hits=1)]},
    {"id": 16, "title": "EV scooter deep compare",
     "steps": [
               "deep research ola s1 pro vs ather 450x vs tvs iqube",
               "compare range, speed and price",
               "which is best value for money for daily commute",
               "what is the charging cost per month"],
     "checks": [
         lambda r, i: check(r, "ola", "ather", "tvs", min_hits=2),
         lambda r, i: check(r, "range", "km", "price", "speed", min_hits=2),
         lambda r, i: check(r, "value", "best", "commute", "recommend", min_hits=2),
         lambda r, i: check(r, "charging", "month", "electricity", min_hits=1)]},
    {"id": 17, "title": "Budget phone camera deep chain",
     "steps": [
               "deep research moto g85 vs redmi note 14 vs iqoo z9",
               "which has the best camera system",
               "how does the battery life compare",
               "final recommendation for under 20k"],
     "checks": [
         lambda r, i: check(r, "moto", "redmi", "iqoo", min_hits=2),
         lambda r, i: check(r, "camera", "sensor", "ois", "megapixel", min_hits=2),
         lambda r, i: check(r, "battery", "mah", "charging", "hours", min_hits=1),
         lambda r, i: check(r, "recommend", "best", "20k", min_hits=2)]},
    {"id": 18, "title": "ML laptop deep chain",
     "steps": [
               "best laptop under 60000 for ml work in india",
               "does it need a gpu for deep learning",
               "compare ram and storage options",
               "recommend one with reasons"],
     "checks": [
         lambda r, i: check(r, "laptop", "ram", "ryzen", "i5", "16gb", min_hits=3),
         lambda r, i: check(r, "gpu", "deep learning", "nvidia", "required", min_hits=1),
         lambda r, i: check(r, "ram", "storage", "ssd", "512", "16", min_hits=2),
         lambda r, i: check(r, "recommend", "best", "reason", min_hits=1)]},
    {"id": 19, "title": "ML course deep chain",
     "steps": [
               "deep research the best machine learning courses in 2026",
               "compare fastai vs deeplearning.ai vs andrew ng",
               "which is better for a working professional",
               "how much does it cost"],
     "checks": [
         lambda r, i: check(r, "course", "machine learning", "fastai", "coursera", "deeplearning", min_hits=2),
         lambda r, i: check(r, "fastai", "deeplearning.ai", "andrew", min_hits=2),
         lambda r, i: check(r, "professional", "better", "working", min_hits=1),
         lambda r, i: check(r, "cost", "free", "price", min_hits=1)]},
    {"id": 20, "title": "Local LLM on 4GB deep chain",
     "steps": [
               "can i run a local llm on my 4gb vm",
               "which models fit in 4gb ram",
               "what quantized versions should i use",
               "is it fast enough for chat"],
     "checks": [
         lambda r, i: check(r, "4gb", "llm", "model", "ram", "qwen", "llama", "phi", "gemma", min_hits=2),
         lambda r, i: check(r, "qwen", "llama", "phi", "gemma", "2gb", "3gb", min_hits=2),
         lambda r, i: check(r, "quant", "q4", "q5", "gguf", min_hits=1),
         lambda r, i: check(r, "fast", "speed", "token", "chat", min_hits=1)]},
    {"id": 21, "title": "Astrophotography beginner chain",
     "steps": [
               "deep research best budget astrophotography setup for beginners in india",
               "telescope or dslr first",
               "what accessories are essential",
               "total budget estimate"],
     "checks": [
         lambda r, i: check(r, "telescope", "dslr", "camera", "budget", min_hits=3),
         lambda r, i: check(r, "telescope", "dslr", "start", "first", min_hits=2),
         lambda r, i: check(r, "tripod", "mount", "adapter", "accessor", min_hits=1),
         lambda r, i: check(r, "budget", "total", min_hits=1)]},
    {"id": 22, "title": "Handloom shops chain",
     "steps": [
               "research good handloom saree shops in ahmedabad",
               "which ones have good reviews",
               "i love handloom, where should mom and i go this weekend",
               "what is the price range there"],
     "checks": [
         lambda r, i: check(r, "handloom", "saree", "shop", "ahmedabad", "law garden", min_hits=2),
         lambda r, i: check(r, "review", "rating", "good", min_hits=1),
         lambda r, i: check(r, "law garden", "shop", "weekend", "recommend", min_hits=2),
         lambda r, i: check(r, "price", "range", min_hits=1)]},
    {"id": 23, "title": "Restaurant deep chain",
     "steps": [
               "best restaurants near gandhinagar for family dinner",
               "which one has good reviews for veg food",
               "under 1500 for two people",
               "do i need to book a table"],
     "checks": [
         lambda r, i: check(r, "restaurant", "gandhinagar", "dinner", min_hits=2),
         lambda r, i: check(r, "veg", "review", "jain", min_hits=1),
         lambda r, i: check(r, "1500", "under", "recommend", min_hits=2),
         lambda r, i: check(r, "book", "table", "reservation", min_hits=1)]},
    {"id": 24, "title": "Street food trail chain",
     "steps": [
               "what is the best street food in ahmedabad old city",
               "give me a walking food trail with stops",
               "which stops are famous on instagram",
               "what should i not miss"],
     "checks": [
         lambda r, i: check(r, "food", "street", "ahmedabad", "manek", "vadapav", min_hits=2),
         lambda r, i: check(r, "trail", "walk", "manek chowk", "route", "start", min_hits=1),
         lambda r, i: check(r, "instagram", "famous", "viral", min_hits=1),
         lambda r, i: check(r, "miss", "must", "try", min_hits=1)]},
    {"id": 25, "title": "Heritage walk deep browse",
     "steps": [
               "deep research the ahmedabad heritage walk",
               "what is the route and how long",
               "how much does it cost and how to book",
               "is it worth it for tourists"],
     "checks": [
         lambda r, i: check(r, "heritage", "walk", "book", "route", min_hits=3),
         lambda r, i: check(r, "route", "hour", "km", "duration", min_hits=1),
         lambda r, i: check(r, "cost", "book", "ticket", min_hits=1),
         lambda r, i: check(r, "worth", "tourist", "recommend", min_hits=1)]},
    {"id": 26, "title": "Old city history chain",
     "steps": [
               "research the history of ahmedabad old city",
               "what are pols and how old are they",
               "who built the city and when",
               "what is the unesco status"],
     "checks": [
         lambda r, i: check(r, "ahmedabad", "pol", "history", "sultan", "ahmed shah", min_hits=2),
         lambda r, i: check(r, "pol", "old", "century", "year", min_hits=1),
         lambda r, i: check(r, "ahmed shah", "built", "1411", "founded", min_hits=1),
         lambda r, i: check(r, "unesco", "world heritage", min_hits=1)]},
    {"id": 27, "title": "Salary savings plan chain",
     "steps": [
               "my salary comes on the 1st of every month",
               "i earn 60000 per month and want to save 20 percent",
               "how much will i have after 6 months",
               "suggest a better saving split between emergency and investment"],
     "checks": [
         lambda r, i: check(r, "1st", "salary", "save", "20%", min_hits=2),
         lambda r, i: check(r, "60000", "20%", "save", min_hits=1),
         lambda r, i: check(r, "72", "12", "000", "12000", "72000", min_hits=1),
         lambda r, i: check(r, "emergency", "investment", "split", "suggest", min_hits=2)]},
    {"id": 28, "title": "Mom birthday gift chain",
     "steps": [
               "my mom's birthday is 12 september and she loves handloom sarees",
               "search for handloom saree gifts under 5000",
               "compare two options with prices",
               "which one should i buy and when"],
     "checks": [
         lambda r, i: check(r, "september", "12", "handloom", "saree", min_hits=2),
         lambda r, i: check(r, "5000", "gift", "option", min_hits=2),
         lambda r, i: check(r, "compare", "price", min_hits=1),
         lambda r, i: check(r, "buy", "recommend", "when", min_hits=1)]},
    {"id": 29, "title": "Goa trip budget chain",
     "steps": [
               "estimate a 3 day goa trip budget from ahmedabad for two people",
               "break it down by flights, hotel and food",
               "what is the cheapest month to go",
               "any budget airline deals"],
     "checks": [
         lambda r, i: check(r, "budget", "flight", "hotel", "goa", "total", min_hits=3),
         lambda r, i: check(r, "flight", "hotel", "food", "breakdown", min_hits=2),
         lambda r, i: check(r, "month", "cheap", "season", min_hits=1),
         lambda r, i: check(r, "airline", "deal", "indigo", "spicejet", min_hits=1)]},
    {"id": 30, "title": "Dentist reminder deep chain",
     "steps": [
               "remind me about my dentist appointment on 25 aug at 11 am",
               "also remind me 1 day before",
               "what else do i have on my calendar that week",
               "remind me again but make it a voice reminder"],
     "checks": [
         lambda r, i: check(r, "remind", "dentist", "11", "25", "set", min_hits=2),
         lambda r, i: check(r, "day before", "24", "earlier", "remind", min_hits=1),
         lambda r, i: check(r, "calendar", "august", "nothing", "mom", min_hits=1),
         lambda r, i: check(r, "remind", "voice", "set", min_hits=1)]},
    {"id": 31, "title": "Call Mom deep chain",
     "steps": [
               "remind me to call mom at 7 pm today",
               "what should i ask her about",
               "set a follow up for next sunday",
               "what is the best time to call mom on sunday"],
     "checks": [
         lambda r, i: check(r, "remind", "7", "call", "mom", "set", min_hits=2),
         lambda r, i: check(r, "ask", "health", "family", "suggest", min_hits=1),
         lambda r, i: check(r, "sunday", "follow", "set", min_hits=1),
         lambda r, i: check(r, "sunday", "call", "time", "best", min_hits=1)]},
    {"id": 32, "title": "Focus session deep chain",
     "steps": [
               "start focos 25m allow github",
               "i drifted to youtube",
               "how many times did i drift",
               "stop the focus session"],
     "checks": [
         lambda r, i: check(r, "focus", "25", "github", "start", "already active", min_hits=2),
         lambda r, i: check(r, "drift", "nudge", "back", "focus", min_hits=1),
         lambda r, i: check(r, "drift", "times", "count", "1", min_hits=1),
         lambda r, i: check(r, "focus", "stop", "ended", "min", min_hits=1)]},
    {"id": 33, "title": "Tracker deep chain",
     "steps": [
               "set up a daily tracker for bookmyshow events in ahmedabad",
               "what did you create exactly",
               "can you make it weekly instead",
               "show me all my active trackers"],
     "checks": [
         lambda r, i: check(r, "tracker", "daily", "created", "set up", "check", min_hits=1),
         lambda r, i: check(r, "tracker", "daily", "created", "set up", "check", min_hits=1),
         lambda r, i: check(r, "weekly", "changed", "updated", min_hits=1),
         lambda r, i: check(r, "tracker", "active", "list", min_hits=1)]},
    {"id": 34, "title": "Identity + profile deep chain",
     "steps": [
               "who are you",
               "what do you know about me",
               "which of those is most important to remember",
               "rate my profile completeness"],
     "checks": [
         lambda r, i: check(r, "friday", "assistant", "companion", "memory", min_hits=2),
         lambda r, i: check(r, "dentist", "gandhinagar", "salary", "handloom", "mom", "astronomy", min_hits=2),
         lambda r, i: check(r, "important", "remember", "priority", min_hits=1),
         lambda r, i: check(r, "rate", "complete", "profile", min_hits=1)]},
    {"id": 35, "title": "Memory correction deep chain",
     "steps": [
               "i actually live in ahmedabad not gandhinagar",
               "so where do i live now",
               "what else do you have wrong about me",
               "fix everything"],
     "checks": [
         lambda r, i: check(r, "ahmedabad", "correct", "updated", "noted", min_hits=2),
         lambda r, i: check(r, "ahmedabad", "live", min_hits=1),
         lambda r, i: check(r, "wrong", "review", "check", min_hits=1),
         lambda r, i: check(r, "fixed", "updated", "corrected", min_hits=1)]},
    {"id": 36, "title": "Beliefs deep chain",
     "steps": [
               "show me what you believe about me",
               "which belief is strongest",
               "rate them by confidence",
               "which one should i correct"],
     "checks": [
         lambda r, i: check(r, "believe", "prefer", "like", "you", min_hits=2),
         lambda r, i: check(r, "strong", "confidence", "highest", min_hits=1),
         lambda r, i: check(r, "rate", "confidence", min_hits=1),
         lambda r, i: check(r, "correct", "review", "suggest", min_hits=1)]},
    {"id": 37, "title": "Saree tension deep chain",
     "steps": [
               "i love handloom sarees but i dont like kanjivaram",
               "is that contradictory",
               "how should i explain it to mom",
               "how do i explain my saree preference to family"],
     "checks": [
         lambda r, i: check(r, "handloom", "kanjivaram", "note", "understand", min_hits=2),
         lambda r, i: check(r, "contradict", "tension", "different", min_hits=1),
         lambda r, i: check(r, "explain", "mom", "suggest", "talk", min_hits=1),
         lambda r, i: check(r, "explain", "family", "preference", "saree", min_hits=1)]},
    {"id": 38, "title": "Multi-hop gift chain",
     "steps": [
               "what should i gift mom combining her birthday, her handloom love, and my budget",
               "make it a concrete plan with shop and price",
               "when should i buy it",
               "how should i wrap and present it"],
     "checks": [
         lambda r, i: check(r, "buy", "september", "before", "august", "week", min_hits=1),
         lambda r, i: check(r, "plan", "recommend", "saree", "stall", min_hits=2),
         lambda r, i: check(r, "buy", "september", "before", "august", "week", min_hits=1),
         lambda r, i: check(r, "wrap", "present", "box", "gift", min_hits=1)]},
    {"id": 39, "title": "Scooter research + email chain",
     "steps": [
               "research best scooters under 1.5 lakh in india",
               "compare top 3 by range and price",
               "which is best for city commute",
               "draft an email to my brother recommending one"],
     "checks": [
         lambda r, i: check(r, "scooter", "iqube", "ather", "ola", "recommend", min_hits=2),
         lambda r, i: check(r, "range", "km", "price", min_hits=2),
         lambda r, i: check(r, "commute", "city", "best", min_hits=1),
         lambda r, i: check(r, "brother", "draft", "email", "recommend", min_hits=2)]},
    {"id": 40, "title": "Top news deep chain",
     "steps": [
               "summarize the top news today in india",
               "what is the biggest story",
               "any tech news",
               "any sports news"],
     "checks": [
         lambda r, i: check(r, "india", "news", "today", "government", "cricket", "pm", min_hits=2),
         lambda r, i: check(r, "biggest", "top", "main", min_hits=1),
         lambda r, i: check(r, "tech", "ai", "startup", "phone", min_hits=1),
         lambda r, i: check(r, "sports", "cricket", "match", min_hits=1)]},
    {"id": 41, "title": "Stock market deep chain",
     "steps": [
               "how is the stock market doing today in india",
               "which sectors are up",
               "is nifty near its all time high",
               "should i invest now"],
     "checks": [
         lambda r, i: check(r, "nifty", "sensex", "market", "point", "percent", min_hits=2),
         lambda r, i: check(r, "sector", "bank", "it", "pharma", "up", min_hits=1),
         lambda r, i: check(r, "high", "record", "all time", min_hits=1),
         lambda r, i: check(r, "invest", "advice", "now", min_hits=1)]},
    {"id": 42, "title": "In-hand salary deep chain",
     "steps": [
               "if my ctc is 18 lakhs per year, what is my approximate in-hand salary per month",
               "how much tax will i pay",
               "what if i use 80c deductions",
               "which regime is better for me"],
     "checks": [
         lambda r, i: check(r, "lakh", "in-hand", "month", "approx", min_hits=2),
         lambda r, i: check(r, "tax", "slab", min_hits=1),
         lambda r, i: check(r, "80c", "deduction", "save", min_hits=1),
         lambda r, i: check(r, "regime", "new", "old", "better", min_hits=1)]},
    {"id": 43, "title": "Tax saving deep chain",
     "steps": [
               "how can i save tax in india with 18 lakh ctc in 2026",
               "what are the best 80c instruments",
               "compare nps vs ppf vs elss",
               "recommend a split"],
     "checks": [
         lambda r, i: check(r, "tax", "80c", "nps", "saving", min_hits=2),
         lambda r, i: check(r, "80c", "elss", "ppf", "nps", "life", min_hits=1),
         lambda r, i: check(r, "nps", "ppf", "elss", "compare", min_hits=2),
         lambda r, i: check(r, "recommend", "split", "allocate", min_hits=1)]},
    {"id": 44, "title": "Book discussion deep chain",
     "steps": [
               "explain quantum entanglement like i am 12",
               "give me an example",
               "what is superposition",
               "is it useful for computers"],
     "checks": [
         lambda r, i: check(r, "entangl", "particle", "state", "measure", "simpl", min_hits=2),
         lambda r, i: check(r, "example", "coin", "spin", "analogy", min_hits=1),
         lambda r, i: check(r, "superposition", "both", "state", min_hits=1),
         lambda r, i: check(r, "computer", "quantum", "useful", min_hits=1)]},
    {"id": 45, "title": "Weekend value plan deep chain",
     "steps": [
               "plan a value for money weekend in ahmedabad this saturday",
               "i love astronomy so include something for that",
               "what is the total cost",
               "make a schedule"],
     "checks": [
         lambda r, i: check(r, "weekend", "ahmedabad", "value", "plan", min_hits=3),
         lambda r, i: check(r, "planetarium", "astronom", "science city", min_hits=1),
         lambda r, i: check(r, "total", "cost", min_hits=1),
         lambda r, i: check(r, "schedule", "morning", "evening", "time", min_hits=1)]},
    {"id": 46, "title": "Multi-turn context deep chain",
     "steps": [
               "what is the capital of gujarat",
               "and its largest city",
               "what is the population of that city",
               "how far is it from the capital",
               "what is it famous for"],
     "checks": [
         lambda r, i: check(r, "gandhinagar", "capital", min_hits=1),
         lambda r, i: check(r, "ahmedabad", "largest", min_hits=1),
         lambda r, i: check(r, "million", "lakh", "crore", "population", min_hits=1),
         lambda r, i: check(r, "km", "distance", "30", "far", min_hits=1),
         lambda r, i: check(r, "famous", "textile", "food", "heritage", min_hits=1)]},
    {"id": 47, "title": "Itinerary artifact deep chain",
     "steps": [
               "create a 2 day ahmedabad itinerary artifact with heritage and food",
               "add morning and evening plans for each day",
               "estimate the cost per day",
               "what should i book in advance"],
     "checks": [
         lambda r, i: check(r, "day", "itinerary", "heritage", "food", min_hits=3),
         lambda r, i: check(r, "morning", "evening", "day", min_hits=2),
         lambda r, i: check(r, "cost", "day", min_hits=1),
         lambda r, i: check(r, "book", "advance", "ticket", min_hits=1)]},
    {"id": 48, "title": "Ahmedabad vs Surat deep chain",
     "steps": [
               "deep research ahmedabad vs surat for living in 2026",
               "compare job opportunities",
               "compare cost of living",
               "which is better for a young professional",
               "give your final verdict"],
     "checks": [
         lambda r, i: check(r, "ahmedabad", "surat", "job", "cost", "living", min_hits=3),
         lambda r, i: check(r, "job", "industry", "opportunity", min_hits=1),
         lambda r, i: check(r, "rent", "cost", "living", min_hits=1),
         lambda r, i: check(r, "young", "professional", "better", min_hits=1),
         lambda r, i: check(r, "verdict", "recommend", "choose", min_hits=1)]},
    {"id": 49, "title": "Open source LLM deep chain",
     "steps": [
               "deep research the best open source llms for a coding assistant in 2026",
               "rank the top 3 for me",
               "how do they compare on benchmarks",
               "which one runs best on a budget gpu",
               "what is my final pick"],
     "checks": [
         lambda r, i: check(r, "llm", "open source", "qwen", "llama", "deepseek", "rank", min_hits=2),
         lambda r, i: check(r, "1", "2", "3", "best", "recommend", min_hits=2),
         lambda r, i: check(r, "benchmark", "score", "compare", min_hits=1),
         lambda r, i: check(r, "gpu", "vram", "budget", "rtx", min_hits=1),
         lambda r, i: check(r, "pick", "final", "recommend", min_hits=1)]},
    {"id": 50, "title": "Health + focus deep chain",
     "steps": [
               "i have trouble focusing after lunch, what helps",
               "is it better to walk or nap",
               "what should i eat for better afternoon focus",
               "set a reminder for my afternoon walk"],
     "checks": [
         lambda r, i: check(r, "focus", "lunch", "walk", "sleep", "water", "recommend", min_hits=2),
         lambda r, i: check(r, "walk", "nap", "better", "compare", min_hits=1),
         lambda r, i: check(r, "eat", "protein", "food", "sugar", "lunch", min_hits=1),
         lambda r, i: check(r, "remind", "walk", "set", min_hits=1)]}
]

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
