"""VOICE — server-side TTS + barge-in protocol.

FRIDAY-Δ: STT is CLIENT-side (Web Speech API / whisper-tiny WASM on the
Chromebook) — barge-in is a local cancel() with zero RTT; the VM never does
STFTs. Server provides optional edge-tts TTS (VM) with client speechSynthesis
fallback. The WS protocol carries {type: barge_in} control frames.
"""
from __future__ import annotations

import asyncio
import io
import json
import os

from .config import cfg
from .db import get_db


class Voice:
    def __init__(self, db=None) -> None:
        self.db = db or get_db()
        self._sessions: dict[str, dict] = {}

    async def tts(self, text: str) -> bytes | None:
        """edge-tts on the VM; None → client falls back to speechSynthesis."""
        prov = os.environ.get("TTS_PROVIDER", cfg.get("speak.tts_provider", "edge"))
        if prov == "none":
            return None
        try:
            import edge_tts
            voice = os.environ.get("EDGE_TTS_VOICE", "en-IN-NeerjaNeural")
            communicate = edge_tts.Communicate(text, voice)
            buf = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            return buf.getvalue() if buf.tell() > 0 else None
        except Exception:
            return None

    # ---- barge-in session management (WS) ----
    def session(self, ws_id: str) -> dict:
        return self._sessions.setdefault(ws_id, {"state": "idle", "speaking": False,
                                                 "last_barge": 0.0})

    async def handle_frame(self, ws_id: str, frame: dict) -> dict | None:
        """Returns server action for the frame, or None."""
        s = self.session(ws_id)
        ftype = frame.get("type")
        if ftype == "barge_in":
            s["speaking"] = False
            s["last_barge"] = asyncio.get_event_loop().time()
            return {"type": "cancel_tts", "ack": True}
        if ftype == "tts_start":
            s["speaking"] = True
            return None
        if ftype == "tts_end":
            s["speaking"] = False
            return None
        if ftype == "listen_start":
            s["state"] = "listening"
            return None
        if ftype == "listen_end":
            s["state"] = "idle"
            return None
        return None
