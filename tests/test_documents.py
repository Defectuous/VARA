import io
from datetime import UTC, datetime

import pymupdf
import pytest
from docx import Document
from fastapi.testclient import TestClient

from vara.config import Settings
from vara.documents import DocumentStore, render_docx, render_pdf, requested_format, split_title
from vara.main import create_app
from vara.providers.fake import FakeProvider
from vara.router import ModelRouter, PrivacyMode

SAMPLE = """# Trip plan

Leave **early** on *Friday* — bring `snacks`.

## Packing

- Tent
- Stove
  - Fuel
1. First
2. Second

| Item | Qty |
| --- | --- |
| Water | 4 |

```
code line 1
code line 2
```

> Quoted

<script>alert(1)</script>
"""


def test_split_title_uses_leading_heading() -> None:
    assert split_title("# Hello\n\nBody") == ("Hello", "Body")
    assert split_title("Body only", "Chat") == ("Chat", "Body only")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("make me a PDF about the solar system", "pdf"),
        ("Can you write that up as a Word document?", "docx"),
        ("export this to docx", "docx"),
        ("create a word doc with my packing list", "docx"),
        ("save that as a pdf please", "pdf"),
        ("what is a pdf file?", None),
        ("I wrote a word on the board", None),
        ("how do I open a .docx on Linux", None),
    ],
)
def test_requested_format(text: str, expected: str | None) -> None:
    assert requested_format(text) == expected


def test_pdf_has_title_text_and_no_raw_html() -> None:
    title, body = split_title(SAMPLE)
    with pymupdf.open("pdf", render_pdf(title, body)) as pdf:
        text = "".join(page.get_text() for page in pdf)
        assert pdf.metadata["title"] == "Trip plan"
    for expected in ("Trip plan", "Packing", "Tent", "Fuel", "Water", "code line 2", "Quoted"):
        assert expected in text
    assert "<script>" in text  # shown as text, never run as HTML


def test_long_pdf_spans_pages() -> None:
    body = "\n\n".join(f"Paragraph {i} " + "words " * 60 for i in range(80))
    with pymupdf.open("pdf", render_pdf("Long", body)) as pdf:
        assert pdf.page_count > 1


def test_docx_has_headings_lists_table_and_code() -> None:
    title, body = split_title(SAMPLE)
    doc = Document(io.BytesIO(render_docx(title, body)))
    paragraphs = [(p.style.name, p.text) for p in doc.paragraphs]
    assert ("Title", "Trip plan") in paragraphs
    assert ("Heading 2", "Packing") in paragraphs
    assert ("List Bullet", "Tent") in paragraphs
    assert ("List Bullet 2", "Fuel") in paragraphs
    assert ("List Paragraph", "2. Second") in paragraphs
    assert any(t == "code line 1\ncode line 2" for _, t in paragraphs)
    bold = [r.text for p in doc.paragraphs for r in p.runs if r.bold]
    assert "early" in bold
    assert [c.text for c in doc.tables[0].rows[1].cells] == ["Water", "4"]
    assert doc.core_properties.title == "Trip plan"


def test_store_never_overwrites_and_rejects_outside_names(tmp_path) -> None:
    store = DocumentStore(tmp_path / "docs")
    now = datetime(2026, 11, 11, 12, tzinfo=UTC)
    first = store.create("# Notes\n\nHi", "pdf", now=now)
    second = store.create("# Notes\n\nHi again", "pdf", now=now)
    assert first.name == "notes-20261111-120000.pdf"
    assert second.name == "notes-20261111-120000-1.pdf"
    assert store.path("../vara.db") is None
    assert store.path("notes-20261111-120000.exe") is None
    assert {d["name"] for d in store.all()} == {first.name, second.name}
    assert store.delete(first.name) and not store.delete(first.name)


def client() -> TestClient:
    router = ModelRouter([FakeProvider("local")], PrivacyMode.LOCAL_ONLY)
    return TestClient(create_app(Settings(), router=router))


def test_documents_api_round_trip() -> None:
    api = client()
    made = api.post("/v1/documents", json={"markdown": SAMPLE, "format": "docx"})
    assert made.status_code == 201
    doc = made.json()
    assert doc["title"] == "Trip plan" and doc["url"] == f"/v1/documents/{doc['name']}"

    download = api.get(doc["url"])
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "attachment" in download.headers["content-disposition"]
    assert [d["name"] for d in api.get("/v1/documents").json()] == [doc["name"]]

    assert api.get("/v1/documents/..%2Fvara.db").status_code == 404
    assert api.delete(doc["url"]).status_code == 204
    assert api.get(doc["url"]).status_code == 404


def test_documents_api_titles_and_validation() -> None:
    api = client()
    titled = api.post(
        "/v1/documents", json={"markdown": "no heading", "format": "pdf", "title": "My Chat"}
    ).json()
    assert titled["name"].startswith("my-chat-") and titled["name"].endswith(".pdf")
    assert api.post("/v1/documents", json={"markdown": "x", "format": "exe"}).status_code == 422


def test_chat_request_for_a_pdf_saves_the_reply() -> None:
    api = client()
    reply = api.post(
        "/v1/chat", json={"messages": [{"role": "user", "content": "make me a PDF of hello"}]}
    ).json()
    assert reply["document"]["format"] == "pdf"
    assert api.get(reply["document"]["url"]).status_code == 200


def test_ordinary_chat_makes_no_document() -> None:
    reply = client().post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert reply.json()["document"] is None


def test_task_lists_get_checkboxes() -> None:
    body = "- [ ] Open\n- [x] Done\n"
    doc = Document(io.BytesIO(render_docx("Tasks", body)))
    assert [p.text for p in doc.paragraphs][1:] == ["☐ Open", "☑ Done"]
    with pymupdf.open("pdf", render_pdf("Tasks", body)) as pdf:
        text = pdf[0].get_text()
    assert "[ ]" not in text and "Open" in text


def test_block_styles_dont_repeat_on_later_pages() -> None:
    # PyMuPDF's Story redraws block background fills at the top of every later page.
    body = "```\ncode\n```\n\n| A | B |\n| - | - |\n| 1 | 2 |\n\n" + "\n\n".join(
        "Para " + "w " * 80 for _ in range(40)
    )
    with pymupdf.open("pdf", render_pdf("T", body)) as pdf:
        assert pdf.page_count > 1
        assert all(not page.get_drawings() for page in list(pdf)[1:])


def test_documents_stay_linked_to_their_message() -> None:
    api = client()
    reply = api.post(
        "/v1/chat", json={"messages": [{"role": "user", "content": "make me a PDF of hello"}]}
    ).json()
    # A Word copy made later with the reply's button is linked too.
    word = api.post(
        "/v1/documents",
        json={"markdown": reply["text"], "format": "docx", "message_id": reply["message_id"]},
    ).json()

    messages = api.get(f"/v1/conversations/{reply['conversation_id']}").json()["messages"]
    assert messages[0]["documents"] == []
    assert [d["name"] for d in messages[1]["documents"]] == [
        reply["document"]["name"],
        word["name"],
    ]

    api.delete(word["url"])  # deleted files drop out of the list
    messages = api.get(f"/v1/conversations/{reply['conversation_id']}").json()["messages"]
    assert [d["format"] for d in messages[1]["documents"]] == ["pdf"]
