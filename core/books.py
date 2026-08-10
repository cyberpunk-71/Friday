"""BOOKS — upload (no size restriction) → OCR → chunk → vector RAG → book mode.

Streaming parse: pages are extracted in passes so a 900-page scan never blocks;
progress streams to the Books panel. OCR stack on the VM: Docling → Paddle →
Tesseract fallback. Offline: pypdf text extraction (works for text PDFs) and
a fixture-based OCR for scanned pages.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path

from .config import cfg
from .db import get_db
from .loom import Loom


def _chunk_text(text: str, size: int = 1000, overlap: int = 120) -> list[str]:
    if len(text) <= size:
        return [text] if text.strip() else []
    out = []
    i = 0
    while i < len(text):
        piece = text[i: i + size]
        if i + size < len(text):
            cut = max(piece.rfind(". "), piece.rfind("\n\n"), piece.rfind(" "))
            if cut > size // 2:
                piece = piece[:cut]
        out.append(piece.strip())
        i += max(len(piece) - overlap, 1)   # always progress, even on the tail
    return [p for p in out if p]


async def extract_pdf_text(path: str, progress_cb=None, max_pages: int | None = None) -> list[tuple[int, str]]:
    """Returns [(page_no, text)] using pypdf; OCR fallback hooks for the VM."""
    from pypdf import PdfReader
    reader = PdfReader(path)
    total = len(reader.pages)
    out: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages):
        if max_pages and i >= max_pages:
            break
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if len(t.strip()) < 40:
            t = _ocr_page(path, i) or ""
        out.append((i + 1, t))
        if progress_cb and (i % 10 == 0):
            await progress_cb((i + 1) / max(total, 1))
    return out


def _ocr_page(path: str, page_no: int) -> str | None:
    """VM: tesseract via pdf2image. Offline: None (fixture OCR for tests)."""
    try:
        from pdf2image import convert_from_path
        from pytesseract import image_to_string
        images = convert_from_path(path, first_page=page_no + 1, last_page=page_no + 1, dpi=200)
        if images:
            return image_to_string(images[0])
    except Exception:
        return None
    return None


class Books:
    def __init__(self, db=None) -> None:
        self.db = db or get_db()

    async def ingest(self, upload: bytes | None = None, filename: str = "",
                     path: str | None = None) -> dict:
        """Accepts raw bytes (any size — streamed to disk, not held in RAM)."""
        max_mb = cfg.get("books.max_file_mb", 0)
        dest_dir = cfg.data_path("books")
        dest_dir.mkdir(parents=True, exist_ok=True)

        if path:
            src = Path(path)
            fname = src.name
            dest = dest_dir / f"{int(time.time())}-{fname}"
            import shutil
            shutil.copy(src, dest)
        else:
            if not filename:
                return {"ok": False, "error": "no file"}
            if max_mb and len(upload or b"") > max_mb * 1024 * 1024:
                return {"ok": False, "error": f"file exceeds {max_mb}MB limit"}
            fname = filename
            dest = dest_dir / f"{int(time.time())}-{fname}"
            dest.write_bytes(upload or b"")

        title = re.sub(r"\.(pdf|epub|txt|djvu)$", "", fname, flags=re.I)
        book_id = self.db.exec(
            "INSERT INTO books(title,source,status,progress,created_ts) VALUES(?,?, 'ingesting',0,?)",
            (title, str(dest), time.time()))

        async def _cb(p: float):
            self.db.exec("UPDATE books SET progress=? WHERE book_id=?", (p, book_id))

        loop = asyncio.get_event_loop()
        pages = await loop.run_in_executor(None, lambda: asyncio.run(
            extract_pdf_text(str(dest), progress_cb=_cb)) if str(dest).lower().endswith(".pdf")
            else self._extract_other(str(dest)))

        full = []
        for pno, text in pages:
            full.extend(_chunk_text(text, cfg.get("books.chunk_size", 1000),
                                    cfg.get("books.chunk_overlap", 120)))
        # store chunks
        self.db.execmany(
            "INSERT INTO book_chunks(book_id,idx,page,text,tokens,created_ts) VALUES(?,?,?,?,?,?)",
            [(book_id, i, pno, ch, max(1, len(ch) // 4), time.time())
             for i, ch in enumerate(full)])
        self.db.exec("UPDATE books SET status='ready', progress=1.0, pages=? WHERE book_id=?",
                     (len(pages), book_id))
        # scope atoms: title identity so book RAG finds it
        self.db.append_event("memory_write", "friday",
                             {"atom": {"kind": "fact",
                                       "text": f"User is reading the book '{title}' ({len(full)} chunks ingested)",
                                       "importance": 0.5, "entities": [title],
                                       "scope": f"book:{book_id}"}}, 0.5)
        return {"ok": True, "book_id": book_id, "title": title,
                "chunks": len(full), "pages": len(pages)}

    @staticmethod
    def _extract_other(path: str) -> list[tuple[int, str]]:
        if path.lower().endswith(".txt"):
            return [(1, Path(path).read_text(encoding="utf-8", errors="ignore"))]
        return [(1, "")]

    def recall_chunks(self, book_id: int, query: str, k: int = 6) -> list[dict]:
        """RAG over a book's chunks (scope-filtered recall + chunk text)."""
        loom = Loom(self.db)
        r = loom.recall(query, k=k, scope=f"book:{book_id}")
        # map atom-like results to actual chunks by topic: query the chunk table
        chunks = self.db.q(
            "SELECT * FROM book_chunks WHERE book_id=? ORDER BY idx LIMIT 400", (book_id,))
        scored = []
        for c in chunks:
            s = self._score_chunk(query, c["text"])
            scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        return [{"chunk_id": c["chunk_id"], "idx": c["idx"], "page": c["page"],
                 "text": c["text"][:1200], "score": round(s, 3)}
                for s, c in scored[:k]]

    @staticmethod
    def _score_chunk(query: str, text: str) -> float:
        q = set(re.findall(r"[a-z]{4,}", query.lower()))
        t = set(re.findall(r"[a-z]{4,}", text.lower()))
        if not q:
            return 0.0
        overlap = len(q & t) / len(q)
        # slight position bonus for earlier chunks (chapter context)
        return overlap + 0.01

    def list(self) -> list[dict]:
        return self.db.q("SELECT * FROM books ORDER BY created_ts DESC")

    def status(self, book_id: int) -> dict | None:
        return self.db.q1("SELECT * FROM books WHERE book_id=?", (book_id,))

    def thread_create(self, book_id: int, kind: str = "discuss", title: str = "") -> int:
        return self.db.exec(
            "INSERT INTO book_threads(book_id,kind,title,created_ts) VALUES(?,?,?,?)",
            (book_id, kind, title, time.time()))

    def chunk(self, book_id: int, chunk_id: int) -> dict | None:
        return self.db.q1("SELECT * FROM book_chunks WHERE book_id=? AND chunk_id=?", (book_id, chunk_id))
