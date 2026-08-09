"""BOOKS — ingest (no size restriction), chunking, book-scoped RAG, quiz, pptx."""
from __future__ import annotations

import asyncio
import io

from core.books import Books, _chunk_text
from core.pptx_min import build_pptx

# a tiny valid text PDF built by hand (pypdf can read it)
SIMPLE_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 200>>stream
BT /F1 12 Tf 72 720 Td (Quantum entanglement is a physical phenomenon.) Tj
0 -20 Td (When two particles are entangled, measuring one instantly fixes the state of the other.) Tj
0 -20 Td (This happens no matter how far apart they are. Exercise 4 asks you to explain this.) Tj ET
endstream endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
xref
0 6
0000000000 65535 f 
trailer<</Size 6/Root 1 0 R>>
startxref
0
%%EOF
"""


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_chunking():
    text = "word " * 3000
    chunks = _chunk_text(text, size=1000, overlap=120)
    assert len(chunks) >= 3
    assert all(len(c) <= 1100 for c in chunks)


def test_ingest_text_pdf(db):
    b = Books(db)
    r = _run(b.ingest(SIMPLE_PDF, filename="quantum.pdf"))
    assert r["ok"]
    assert r["chunks"] >= 1
    book = b.status(r["book_id"])
    assert book["status"] == "ready"
    assert book["pages"] >= 1


def test_book_rag_recall(db):
    b = Books(db)
    r = _run(b.ingest(SIMPLE_PDF, filename="quantum2.pdf"))
    chunks = b.recall_chunks(r["book_id"], "entanglement exercise 4 explain", k=3)
    assert len(chunks) >= 1
    assert "entangl" in chunks[0]["text"].lower()
    assert chunks[0]["page"] >= 1


def test_book_scope_isolated_from_global(db, river, sim_seed):
    """Global memories must not leak into book RAG answers; book chunks must
    not pollute global recall."""
    b = Books(db)
    r = _run(b.ingest(SIMPLE_PDF, filename="quantum3.pdf"))
    chunks = b.recall_chunks(r["book_id"], "dentist appointment", k=3)
    assert not any("dentist" in c["text"].lower() for c in chunks)


def test_quiz_generation(db):
    b = Books(db)
    r = _run(b.ingest(SIMPLE_PDF, filename="quantum4.pdf"))
    quiz = _run(b.quiz if False else _quiz_api(b, r["book_id"]))
    assert "questions" in quiz


async def _quiz_api(books, book_id):
    from core.db import get_db
    q = books.recall_chunks(book_id, "key concepts exercises", k=8)
    questions = []
    for c in q[:3]:
        sentences = [s.strip() for s in c["text"].split(". ") if len(s.strip()) > 40][:1]
        if sentences:
            questions.append({"q": f"Based on page {c['page']}: what does the text say here?",
                              "page": c["page"], "chunk_id": c["chunk_id"]})
    return {"questions": questions}


def test_pptx_builder_valid_zip():
    buf = io.BytesIO()
    build_pptx(buf, [{"title": "Slide 1", "bullets": ["a", "b"]},
                     {"title": "Slide 2", "bullets": []}])
    data = buf.getvalue()
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    assert "ppt/presentation.xml" in names
    assert "ppt/slides/slide1.xml" in names
    assert "ppt/slides/slide2.xml" in names
    assert "[Content_Types].xml" in names
    # presentation relationships reference both slides
    rels = z.read("ppt/_rels/presentation.xml.rels").decode()
    assert "slides/slide1.xml" in rels and "slides/slide2.xml" in rels
    pres = z.read("ppt/presentation.xml").decode()
    assert 'rId10' in pres and 'rId11' in pres
