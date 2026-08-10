"""HERMES — pre-fire, governors, blast-radius classifier, ask budget."""
from __future__ import annotations

import asyncio

from core.hermes import Hermes


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_prefire_live_triggers(db):
    from core.providers import SimSearch
    h = Hermes(db, search=SimSearch())
    pf = _run(h.prefire("research phones under 20k"))
    assert len(pf.search) >= 1
    assert pf.burned_usd > 0


def test_prefire_smalltalk_none(db):
    from core.providers import SimSearch
    h = Hermes(db, search=SimSearch())
    pf = _run(h.prefire("haan ok thik hai"))
    assert len(pf.search) == 0
    assert pf.burned_usd == 0


def test_budget_governor(db):
    h = Hermes(db)
    assert h.governor_allows(0.1) is True
    h.spend(5.99)
    assert h.governor_allows(0.1) is False
    assert h.spend_today() == 5.99


def test_blast_radius_classifier(db):
    h = Hermes(db)
    assert h.classify({"action": "payment", "amount": 100})["class"] == "blocking"
    assert h.classify({"action": "email_send", "recipients_ext": 6})["class"] == "blocking"
    assert h.classify({"action": "email_send", "recipients_ext": 2})["class"] == "blocking"
    assert h.classify({"action": "credential_read"})["class"] == "blocking"
    assert h.classify({"action": "browser_write", "dom_has": ["card"]})["class"] == "blocking"
    assert h.classify({"action": "fs_delete", "files": 1})["class"] == "reversible"
    assert h.classify({"action": "fs_delete", "files": 21})["class"] == "blocking"
    assert h.classify({"action": "web_search"})["class"] == "safe"
    assert h.classify({"action": "git_push", "force": True})["class"] == "blocking"


def test_ask_budget_daily_reset(db):
    h = Hermes(db)
    h.ask_used()
    h.ask_used()
    assert h.ask_budget_left() == 0
    # next day resets
    from core.config import cfg
    db.set_setting("ask.used_date", "1999-01-01")
    assert h.ask_budget_left() == cfg.get("hermes.ask_budget_per_day", 2)


def test_runaway_check(db):
    h = Hermes(db)
    assert h.runaway_check(1, 2, 0) is False
    assert h.runaway_check(1, 2, 3) is True
    assert h.runaway_check(1, 999, 0) is True
