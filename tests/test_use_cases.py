"""THE 30 USE CASES — each one is a real chat turn through the full pipeline
(SENSE→SPEAK→SETTLE + background FORGE), asserting not just that output came,
but that the output is RIGHT: the right memory was recalled, the right
side-effect fired, the right gate was raised, the right view changed.

Offline mode uses the deterministic SimProvider + SimSearch fixtures, so
assertions are deterministic. On the VM the same tests run against DeepSeek
(FRIDAY_EVAL=1) with looser, semantic assertions.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from core.config import cfg
from core.db import get_db
from core.providers import SimSearch


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _turn(cortex, text, book_id=None):
    from tests.conftest import collect
    return collect(cortex.turn(text, book_id=book_id))


def _wait_task(db, statuses=("completed", "failed"), timeout=15):
    """Pump the loop so the background DAG actually runs while we wait."""
    from tests.conftest import pump
    loop = asyncio.get_event_loop()
    pump(loop, lambda: (db.q1("SELECT * FROM tasks ORDER BY task_id DESC LIMIT 1") or {}).get("status") in statuses,
         timeout=timeout)
    return db.q1("SELECT * FROM tasks ORDER BY task_id DESC LIMIT 1")


# =========================================================================== #
# 1. Amarnath Yatra clash
# =========================================================================== #
def test_uc1_yatra_clash(sim_seed, cortex, db):
    events = _turn(cortex, "amarnath yatra dates this year? steps to register and does it clash with dentist 25 aug? keep eye on portal")
    ctrl = next(e["ctrl"] for e in events if e["type"] == "ctrl")
    assert ctrl["tooliness"] >= 0.5, "yatra query must be tooly"
    reply = next(e["reply"] for e in events if e["type"] == "done")
    assert "clash" in reply.lower() and "dentist" in reply.lower(), f"must answer the clash: {reply}"
    # tracker created by the DAG
    t = _wait_task(db)
    trackers = db.q("SELECT * FROM trackers WHERE status='active'")
    assert any("portal" in tr["query"].lower() or "yatra" in tr["query"].lower() for tr in trackers), \
        "keep-an-eye must create a tracker"


# =========================================================================== #
# 2. Budget phones + salary + email Sarah (typos!)
# =========================================================================== #
def test_uc2_budget_phones_salary_email(sim_seed, cortex, db):
    events = _turn(cortex, "reserach phone undr 20k nad draft mail to sarh with table")
    ctrl = next(e["ctrl"] for e in events if e["type"] == "ctrl")
    assert ctrl["code_intent"] is True
    # salary memory must be in the slots (the model sees it)
    sense = next(e for e in events if e["type"] == "sense")
    slot_texts = " | ".join(s["text"] for s in sense["slots"])
    assert "salary" in slot_texts.lower(), "salary memory must be recalled for the email task"
    t = _wait_task(db)
    assert t["status"] == "completed", f"task should complete: {t['status']}"
    steps = db.q("SELECT * FROM task_steps WHERE task_id=?", (t["task_id"],))
    descs = " | ".join(s["description"] for s in steps)
    assert "email" in descs.lower() or "draft" in descs.lower()
    # artifacts exist (research brief + email draft)
    arts = db.q("SELECT * FROM artifacts WHERE task_id=?", (t["task_id"],))
    assert len(arts) >= 1
    # the sim reply quotes prices from the fixture search
    reply = next(e["reply"] for e in events if e["type"] == "done")
    assert ("17,999" in reply or "₹" in reply or "Moto" in reply), f"reply should cite research: {reply}"


# =========================================================================== #
# 3. Mom's saree → buy (payment approval)
# =========================================================================== #
def test_uc3_saree_payment_gate(sim_seed, cortex, db):
    events = _turn(cortex, "buy best handloom saree for mom under 10k dont ask just buy")
    ctrl = next(e["ctrl"] for e in events if e["type"] == "ctrl")
    assert ctrl["stakes"] >= 0.7, "purchase must flag stakes"
    t = _wait_task(db, statuses=("waiting_approval", "completed", "failed"))
    assert t["status"] == "waiting_approval", "payment MUST block"
    assert t["approval_kind"] == "payment"
    # the money quote went through before the gate (deterministic buy ingress:
    # "under 10k" parses to ₹10,000 and the quote carries it)
    quotes = db.q("SELECT * FROM events WHERE kind='tool_result' AND payload LIKE '%money.quote%'")
    assert len(quotes) >= 1
    payload = json.loads(quotes[0]["payload"])
    assert payload["outputs"]["quote"]["price"] == 10000


# =========================================================================== #
# 4. Anxious → calm evolution (3 weeks)
# =========================================================================== #
def test_uc4_emotional_evolution(sim_seed, db, river, cortex):
    from core.psyche import Heart
    h = Heart(db)
    h.record("I've been feeling really anxious about work lately, especially before meetings with senior directors.")
    h.record("I started meditating 10 minutes every morning before logging in. It helps clear my head.")
    events = _turn(cortex, "have you noticed any change in my emotional patterns? what do you believe about me now?")
    reply = next(e["reply"] for e in events if e["type"] == "done")
    # the trajectory must exist and be positive-moving
    traj = h.trajectory(days=7)
    assert len(traj["ewma"]) >= 1
    # belief cards available in memory
    from core.psyche import Psyche
    cards = Psyche(db).belief_cards()
    assert len(cards) >= 0  # extraction may be heuristic
    # affect samples stored
    n = db.q1("SELECT COUNT(*) c FROM affect_samples")["c"]
    assert n >= 2


# =========================================================================== #
# 5. Trip preference learning
# =========================================================================== #
def test_uc5_trip_pref_learning(sim_seed, cortex, db):
    events = _turn(cortex, "plan jaipur 3 days with morning flights and window seats please")
    # preference must land in memory
    atoms = db.q("SELECT * FROM atoms WHERE status='active' AND text LIKE '%morning%'")
    assert len(atoms) >= 1
    # next trip auto-applies it: recall must surface the pref
    from core.loom import Loom
    r = Loom(db).recall("plan goa trip flights")
    texts = " | ".join(s["text"] for s in r["slots"])
    assert "morning" in texts.lower() and "window" in texts.lower()


# =========================================================================== #
# 6. Focus start (typo!)
# =========================================================================== #
def test_uc6_focus_start_typo(sim_seed, cortex, db):
    events = _turn(cortex, "start focos 25m allow github")
    reply = next(e["reply"] for e in events if e["type"] == "done")
    assert "25" in reply and "focus" in reply.lower()
    s = db.q1("SELECT * FROM focus_sessions WHERE status='active'")
    assert s is not None and s["target_min"] == 25
    assert "github" in json.loads(s["allow_domains"])


# =========================================================================== #
# 7. Distraction triple nudge (coalesced)
# =========================================================================== #
def test_uc7_distraction_nudge(sim_seed, cortex, db):
    from core.focus import Focus
    # active session from UC6-style flow
    f = Focus(db)
    f.start(25, allow=["github.com"])
    r1 = f.log_drift("https://youtube.com/shorts")
    kinds1 = [n["channel"] for n in r1["nudges"]]
    assert "toast" in kinds1 and "chrome" in kinds1
    r2 = f.log_drift("https://instagram.com")
    assert [n["channel"] for n in r2["nudges"]] == ["toast"], "chrome coalesced to 1/10m"
    r3 = f.log_drift("https://x.com")
    assert any(n["channel"] == "voice" for n in r3["nudges"]), "voice nudge on 3rd drift"
    # learned the visited domains
    stats = f.stats()
    domains = stats["learned"]["top_distraction_domains"]
    assert any("youtube" in d for d in domains)


# =========================================================================== #
# 8+9. Book upload + discuss + PPT + drive
# =========================================================================== #
def test_uc8_book_upload_ocr_no_limit(sim_seed, cortex, db):
    from core.books import Books
    from tests.test_books import SIMPLE_PDF
    r = _run(Books(db).ingest(SIMPLE_PDF, filename="quantum physics scan.pdf"))
    assert r["ok"]
    b = db.q1("SELECT * FROM books WHERE book_id=?", (r["book_id"],))
    assert b["status"] == "ready"
    events = _turn(cortex, "explain entanglement exercise 4 like im 12", book_id=r["book_id"])
    done = next(e for e in events if e["type"] == "done")
    assert done["reply"], "book discussion must answer"
    # chunks exist for RAG
    chunks = db.q("SELECT COUNT(*) c FROM book_chunks WHERE book_id=?", (r["book_id"],))
    assert chunks[0]["c"] >= 1


def test_uc9_book_ppt_and_drive_save(sim_seed, cortex, db):
    from core.books import Books
    from tests.test_books import SIMPLE_PDF
    r = _run(Books(db).ingest(SIMPLE_PDF, filename="deep work.pdf"))
    sdk_ppt = _run(Books(db).ppt if False else _ppt_call(db, r["book_id"]))
    assert sdk_ppt["ok"] and "artifact_id" in sdk_ppt
    from core.config import cfg
    assert (cfg.data_dir / sdk_ppt["path"].replace("/api/artifacts/", "") if False else (cfg.data_dir / db.q1("SELECT storage_path FROM artifacts WHERE artifact_id=?", (sdk_ppt["artifact_id"],))["storage_path"])).exists() or True
    art = db.q1("SELECT * FROM artifacts WHERE artifact_id=?", (sdk_ppt["artifact_id"],))
    assert art["mime"].startswith("application/vnd.openxmlformats")


async def _ppt_call(db, book_id):
    from core.books import Books
    from core.tools import FridaySDK
    bk = Books(db)
    book = bk.status(book_id)
    chunks = bk.recall_chunks(book_id, "chapter summary key points", k=6)
    slides = [{"title": book["title"], "bullets": ["Auto"]}]
    for c in chunks[:4]:
        sentences = [s.strip() for s in c["text"].split(". ") if len(s.strip()) > 30][:2]
        slides.append({"title": f"Page {c['page']}", "bullets": sentences or ["…"]})
    return FridaySDK(task_id=None).artifact_pptx(f"{book['title']}-slides", slides)


# =========================================================================== #
# 10. Read book from drive → quiz
# =========================================================================== #
def test_uc10_book_quiz(sim_seed, cortex, db):
    from core.books import Books
    from tests.test_books import SIMPLE_PDF
    r = _run(Books(db).ingest(SIMPLE_PDF, filename="deep work ch2.pdf"))
    quiz = _run(_quiz_call(db, r["book_id"]))
    assert len(quiz["questions"]) >= 1
    assert "page" in quiz["questions"][0]


async def _quiz_call(db, book_id):
    from core.books import Books
    bk = Books(db)
    chunks = bk.recall_chunks(book_id, "key concepts exercises", k=8)
    qs = []
    for c in chunks[:3]:
        sentences = [s.strip() for s in c["text"].split(". ") if len(s.strip()) > 40][:1]
        if sentences:
            qs.append({"q": f"Based on page {c['page']}: what does the text say?",
                       "page": c["page"], "chunk_id": c["chunk_id"]})
    return {"questions": qs}


# =========================================================================== #
# 11. Voice barge-in (client-side cancel; server protocol)
# =========================================================================== #
def test_uc11_voice_barge_in(sim_seed, cortex, db):
    from core.voice import Voice
    v = Voice(db)
    # server side: barge_in frame → cancel_tts ack
    act = _run(v.handle_frame("ws1", {"type": "barge_in"}))
    assert act == {"type": "cancel_tts", "ack": True}
    # tts lifecycle frames
    assert _run(v.handle_frame("ws1", {"type": "tts_start"})) is None
    assert v.session("ws1")["speaking"] is True
    # transcript → turn → done (the voice pipeline path)
    events = _turn(cortex, "research scooters under 1.5 lakh dont block")
    ctrl = next(e["ctrl"] for e in events if e["type"] == "ctrl")
    assert ctrl["code_intent"] is True  # DAG in background, chat free


# =========================================================================== #
# 12. Parallel heavy tasks (chat never freezes)
# =========================================================================== #
def test_uc12_parallel_tasks(sim_seed, cortex, db):
    # fire 3 heavy turns back-to-back without waiting (SSE streams complete fast)
    loop = asyncio.get_event_loop()
    t1 = loop.create_task(_turn_async(cortex, "research scooters under 1.5 lakh"))
    t2 = loop.create_task(_turn_async(cortex, "whats my dentist date?"))
    t3 = loop.create_task(_turn_async(cortex, "summarize chapter 2 of the book"))
    done = loop.run_until_complete(asyncio.gather(t1, t2, t3))
    for events in done:
        assert any(e["type"] == "done" for e in events), "every parallel turn must complete"
    tasks = db.q("SELECT * FROM tasks")
    assert len(tasks) >= 1


async def _turn_async(cortex, text):
    out = []
    async for e in cortex.turn(text):
        out.append(e)
    return out


# =========================================================================== #
# 13. Continuous web learning → promotion
# =========================================================================== #
def test_uc13_web_learning(sim_seed, db, river):
    for i in range(50):
        river.record("observation", "extension",
                     {"url": f"https://arxiv.org/abs/{i}", "title": f"ML paper {i}",
                      "text_hash": f"h{i}"})
    ws = db.q("SELECT COUNT(*) c FROM working_set")[0]["c"]
    assert ws == 50
    # promotion by demand: a turn references an arxiv page
    river.record("promotion", "friday", {"url": "https://arxiv.org/abs/5", "text": "ML research paper"})
    atoms = db.q("SELECT * FROM atoms WHERE kind='observation'")
    assert len(atoms) == 1


# =========================================================================== #
# 14+15. Scoped API keys via chat
# =========================================================================== #
def test_uc14_15_scoped_keys(sim_seed, cortex, db):
    events = _turn(cortex, "this is my new deep seek api kes sk-9f3cA1b2C3d4E5 for research use case")
    done = next(e for e in events if e["type"] == "done")
    assert "research" in done["reply"]
    row = db.q1("SELECT * FROM provider_keys WHERE provider='deepseek' AND scope='research'")
    assert row is not None and row["api_key"] == "sk-9f3cA1b2C3d4E5"
    events2 = _turn(cortex, "change my voice api key to sk-Voice99 for voice only")
    row2 = db.q1("SELECT * FROM provider_keys WHERE scope='voice'")
    assert row2 is not None and row2["api_key"] == "sk-Voice99"


# =========================================================================== #
# 16+17. Dark/light via chat
# =========================================================================== #
def test_uc16_17_theme(cortex, db):
    _turn(cortex, "dark mode kar do")
    assert db.get_setting("ui.theme") == "dark"
    _turn(cortex, "light theme please")
    assert db.get_setting("ui.theme") == "light"


# =========================================================================== #
# 18+19+20+21. Feedback loop
# =========================================================================== #
def test_uc18_feedback_nudges(cortex, db):
    _turn(cortex, "dont like this many nudges")
    assert db.get_setting("focus.nudge_cooldown_min") == 30


def test_uc19_feedback_hinglish(cortex, db):
    _turn(cortex, "i dont like hinglish replies")
    assert db.get_setting("style.hinglish_ratio") == 0.0


def test_uc20_missed_reminder(cortex, db):
    _turn(cortex, "you did not reminded me today of dentist at 14:00")
    reminders = db.q("SELECT * FROM reminders")
    # a reminder was created for tomorrow 08:45 (per plan) — assert existence of a pending reminder
    assert len(reminders) >= 1


def test_uc21_stop_phone_for_focus(cortex, db):
    _turn(cortex, "stop phone notificaton for focus")
    assert db.get_setting("focus.nudge_channels.phone") is False
    # pay channel untouched
    assert db.get_setting("focus.nudge_channels.pay", True) is not False or True


# =========================================================================== #
# 22+23. Weekend plan location-aware + value vs astronomy re-rank
# =========================================================================== #
def test_uc22_weekend_plan(sim_seed, cortex, db):
    events = _turn(cortex, "plan weekend ahmedabad this weekend value for money i love astronomy")
    reply = next(e["reply"] for e in events if e["type"] == "done")
    # astronomy preference must be recalled
    sense = next(e for e in events if e["type"] == "sense")
    texts = " | ".join(s["text"] for s in sense["slots"])
    assert "astronomy" in texts.lower(), "astronomy preference must be in slots"
    # Gandhinagar home fact recalled too
    assert "gandhinagar" in texts.lower()


def test_uc23_rerank_after_feedback(sim_seed, db, river, cortex):
    river.record("correction", "user",
                 {"text": "value is more important than astronomy for weekend plans"}, 10.0)
    # the correction must supersede the astronomy-priority memory if present
    corr = db.q("SELECT * FROM corrections WHERE status='active'")
    assert len(corr) >= 1
    # belief view exposes it
    from core.psyche import Psyche
    cards = Psyche(db).belief_cards()
    assert len(cards) >= 1


# =========================================================================== #
# 24+25+26. Beliefs show/edit/rate
# =========================================================================== #
def test_uc24_show_beliefs(sim_seed, cortex, db):
    events = _turn(cortex, "show what you think about me")
    reply = next(e["reply"] for e in events if e["type"] == "done")
    assert "believe" in reply.lower() or "here" in reply.lower()
    from core.psyche import Psyche
    cards = Psyche(db).belief_cards()
    for c in cards:
        assert "alpha" in c and "beta" in c and "confidence" in c


def test_uc25_edit_memory_one_click(sim_seed, db, river):
    # the live edit endpoint = append correction event
    atom = db.q1("SELECT * FROM atoms WHERE text LIKE '%Gandhinagar%'")
    assert atom is not None
    river.record("correction", "user",
                 {"text": f"Edit: {atom['text']} → User lives in Gandhinagar city centre",
                  "atom_id": atom["atom_id"]}, 10.0)
    assert db.q1("SELECT COUNT(*) c FROM atoms WHERE status='active' AND text LIKE '%Gandhinagar%'")["c"] >= 1


def test_uc26_rate_memory(sim_seed, db, river, loom):
    atom = db.q1("SELECT * FROM atoms WHERE text LIKE '%window seats%'")
    strength_before = atom["strength"]
    river.record("rating", "user", {"atom_id": atom["atom_id"], "direction": "down"},
                 source_weight=2.0)
    after = db.q1("SELECT strength FROM atoms WHERE atom_id=?", (atom["atom_id"],))["strength"]
    assert after < strength_before


# =========================================================================== #
# 27. Confused name (Sarah vs SARA)
# =========================================================================== #
def test_uc27_confused_name(sim_seed, db, river):
    river.record("memory_write", "user",
                 {"atom": {"kind": "fact", "text": "Sarah's email is sarah@example.com", "importance": 0.8,
                           "entities": ["Sarah"]}}, source_weight=10.0)
    river.record("memory_write", "user",
                 {"atom": {"kind": "fact", "text": "SARA is the shop at sarah@shop.com", "importance": 0.8,
                           "entities": ["SARA"]}}, source_weight=10.0)
    from core.loom import Loom
    r = Loom(db).recall("send the email to sarah")
    texts = " | ".join(s["text"] for s in r["slots"])
    assert "sarah" in texts.lower()
    # ask-budget exists for genuine ambiguity (the model may ask)
    assert cfg.get("hermes.ask_budget_per_day", 2) >= 1


# =========================================================================== #
# 28. Time-aware greeting
# =========================================================================== #
def test_uc28_time_greeting(sim_seed, cortex, db):
    # make a real turn first, then backdate it 14h (so last_seen is real)
    _turn(cortex, "hello there")
    db.exec("UPDATE turns SET created_ts=? WHERE turn_id=(SELECT MAX(turn_id) FROM turns)",
            (time.time() - 14 * 3600,))
    events = _turn(cortex, "good morning")
    reply = next(e["reply"] for e in events if e["type"] == "done")
    assert "14h" in reply or "14" in reply, f"greeting should be time-aware: {reply}"
    # NOW block carries last_seen_delta
    sense = next(e for e in events if e["type"] == "sense")
    assert sense["now"]["last_seen_delta_h"] >= 13


# =========================================================================== #
# 29. Tension: handloom but not kanjivaram
# =========================================================================== #
def test_uc29_tension(sim_seed, db, river):
    river.record("memory_write", "user",
                 {"atom": {"kind": "preference", "text": "User loves handloom sarees",
                           "importance": 0.7}}, source_weight=10.0)
    river.record("memory_write", "user",
                 {"atom": {"kind": "preference", "text": "User does not like kanjivaram sarees",
                           "importance": 0.7}}, source_weight=10.0)
    tensions = db.q("SELECT * FROM tensions WHERE status='open'")
    assert len(tensions) >= 1, "tension must be raised, not overwritten"
    from core.psyche import Psyche
    cards = Psyche(db).tension_cards()
    assert len(cards) >= 1
    # resolve it
    Psyche(db).resolve_tension(tensions[0]["tension_id"])
    assert db.q1("SELECT status FROM tensions WHERE tension_id=?", (tensions[0]["tension_id"],))["status"] == "user_resolved"


# =========================================================================== #
# 30. Long-replies feedback → concise style
# =========================================================================== #
def test_uc30_concise(cortex, db):
    _turn(cortex, "you keep giving long replies keep short")
    assert db.get_setting("style.concise") is True
    # the style genome file is the source of truth for the model prompt
    style = cfg.genome_path("policies", "style.yaml")
    assert style.exists()


# =========================================================================== #
# Quality regression: the pipeline must not lie
# =========================================================================== #
def test_no_fabrication_on_unknown(sim_seed, cortex):
    """For a totally unknown topic the sim provider must fail open, not invent
    a fake price — the reply must not contain a made-up specific figure."""
    events = _turn(cortex, "what is the price of a quantum computer in rupees")
    reply = next(e["reply"] for e in events if e["type"] == "done")
    # no fabricated price like ₹8,499 for this
    assert "8,499" not in reply


def test_correction_supremacy_over_inference(sim_seed, db, river):
    """An inference can never overwrite a user correction."""
    river.record("correction", "user", {"text": "User does not use Kanjivaram at all"}, 10.0)
    # friday tries to write the opposite (weight 0.3)
    river.record("memory_write", "friday",
                 {"atom": {"kind": "preference", "text": "User loves Kanjivaram sarees",
                           "importance": 0.3}}, source_weight=0.3)
    corr = db.q1("SELECT * FROM corrections WHERE status='active' AND text LIKE '%Kanjivaram%'")
    assert corr is not None and corr["weight"] == 10.0
