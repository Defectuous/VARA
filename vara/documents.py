"""Word (.docx) and PDF documents made from Markdown, such as a chat reply.

Files are written only into V.A.R.A.'s own data folder (`data/documents`), never anywhere else, so
making a document can't overwrite or touch the user's files. Raw HTML in the Markdown is not rendered.
"""

import io
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pymupdf
from docx import Document as new_docx
from docx.enum.text import WD_BREAK
from docx.shared import Pt, RGBColor
from markdown_it import MarkdownIt
from markdown_it.token import Token

DocFormat = Literal["docx", "pdf"]
FORMATS: tuple[DocFormat, ...] = ("docx", "pdf")
MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}
CREATOR = "V.A.R.A."

# A stored file's name: a slug, a UTC timestamp, an optional counter, and the format.
NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,60}-\d{8}-\d{6}(-\d{1,3})?\.(docx|pdf)$")


def _markdown() -> MarkdownIt:
    # "html": False shows any raw HTML as text instead of rendering it.
    return MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


_TASK = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)\[([ xX])\]\s", re.MULTILINE)


def _checkboxes(markdown: str) -> str:
    """Task-list items ("- [ ] Buy milk") get real boxes; CommonMark would show the brackets."""
    return _TASK.sub(lambda m: f"{m.group(1)}{'☐' if m.group(2) == ' ' else '☑'} ", markdown)


def split_title(markdown: str, fallback: str = "Document") -> tuple[str, str]:
    """The document's title and body: a leading `# Heading` becomes the title, else `fallback`."""
    match = re.match(r"\s*#\s+(.+?)\s*#*\s*(?:\n|$)", markdown)
    if match:
        return match.group(1).strip(), markdown[match.end() :]
    return fallback.strip() or "Document", markdown


# ---------------------------------------------------------------------------------------- PDF

PDF_CSS = """
body { font-family: sans-serif; font-size: 11pt; line-height: 1.4; color: #1b1f24; }
h1 { font-size: 20pt; margin: 0 0 10pt 0; }
h2 { font-size: 15pt; margin: 14pt 0 6pt 0; }
h3 { font-size: 13pt; margin: 12pt 0 4pt 0; }
h4, h5, h6 { font-size: 11pt; margin: 10pt 0 4pt 0; }
p { margin: 0 0 8pt 0; }
ul, ol { margin: 0 0 8pt 0; }
li { margin: 0 0 3pt 0; }
code { font-family: monospace; font-size: 10pt; background-color: #f0f2f5; }
pre { font-family: monospace; font-size: 9.5pt; border-left: 3px solid #b0b7bf; padding: 2pt 8pt;
      margin: 0 0 8pt 0; }
pre code { background-color: transparent; }
blockquote { margin: 0 0 8pt 12pt; color: #555c66; }
table { border-collapse: collapse; margin: 0 0 8pt 0; }
th, td { border: 1px solid #b0b7bf; padding: 3pt 6pt; text-align: left; }
th { font-weight: bold; border-bottom: 2px solid #8a939d; }
hr { color: #b0b7bf; }
/* No background fills on blocks or table cells: PyMuPDF's Story repeats them on later pages. */
a { color: #2f6fde; }
"""


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_pdf(title: str, body: str) -> bytes:
    """A US Letter PDF with `title` as its heading and the Markdown `body` below it."""
    html = f"<h1>{_html_escape(title)}</h1>\n{_markdown().render(_checkboxes(body))}"
    story = pymupdf.Story(html=html, user_css=PDF_CSS)
    buffer = io.BytesIO()
    writer = pymupdf.DocumentWriter(buffer)
    page = pymupdf.paper_rect("letter")
    content = page + (54, 54, -54, -54)  # 0.75 inch margins
    more = True
    while more:
        device = writer.begin_page(page)
        more, _ = story.place(content)
        story.draw(device)
        writer.end_page()
    writer.close()
    with pymupdf.open("pdf", buffer.getvalue()) as pdf:
        pdf.set_metadata({"title": title, "creator": CREATOR, "producer": CREATOR})
        pdf.subset_fonts()  # embed only the characters used, not whole fonts (emoji are large)
        return pdf.tobytes(garbage=3, deflate=True)


# --------------------------------------------------------------------------------------- Word


def _add_runs(paragraph, children: list[Token]) -> None:
    """Add a Markdown paragraph's inline content (bold, italic, code, links, breaks) as Word runs."""
    bold = italic = strike = False
    link: str | None = None
    for child in children:
        kind = child.type
        if kind in ("strong_open", "strong_close"):
            bold = kind == "strong_open"
        elif kind in ("em_open", "em_close"):
            italic = kind == "em_open"
        elif kind in ("s_open", "s_close"):
            strike = kind == "s_open"
        elif kind == "link_open":
            link = str(child.attrs.get("href") or "")
        elif kind == "link_close":
            # Word hyperlinks need low-level XML; the address in brackets keeps it usable in print.
            if link and not link.startswith("#"):
                paragraph.add_run(f" ({link})").font.color.rgb = RGBColor(0x55, 0x5C, 0x66)
            link = None
        elif kind == "softbreak":
            paragraph.add_run(" ")
        elif kind == "hardbreak":
            paragraph.add_run().add_break(WD_BREAK.LINE)
        elif kind in ("text", "code_inline", "image"):
            run = paragraph.add_run(child.content)
            run.bold, run.italic, run.font.strike = bold or None, italic or None, strike or None
            if kind == "code_inline":
                run.font.name = "Consolas"
            if link:
                run.font.color.rgb = RGBColor(0x2F, 0x6F, 0xDE)
                run.underline = True


def render_docx(title: str, body: str) -> bytes:
    """A Word document with `title` as its heading and the Markdown `body` below it."""
    doc = new_docx()
    doc.core_properties.title = title
    doc.core_properties.author = CREATOR
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    doc.add_heading(title, level=0)

    lists: list[dict] = []  # open lists, innermost last: {"ordered": bool, "number": int}
    item_started = False  # whether the current list item's first paragraph has been written
    quote_depth = 0
    heading_level: int | None = None
    table: list[list[str]] | None = None
    header_rows = 0
    in_head = False

    tokens = _markdown().parse(_checkboxes(body))
    for token in tokens:
        kind = token.type
        if kind == "heading_open":
            heading_level = int(token.tag[1])
        elif kind == "heading_close":
            heading_level = None
        elif kind in ("bullet_list_open", "ordered_list_open"):
            start = int(token.attrs.get("start") or 1)
            lists.append({"ordered": kind == "ordered_list_open", "number": start - 1})
        elif kind in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif kind == "list_item_open":
            lists[-1]["number"] += 1
            item_started = False
        elif kind == "blockquote_open":
            quote_depth += 1
        elif kind == "blockquote_close":
            quote_depth -= 1
        elif kind == "table_open":
            table, header_rows = [], 0
        elif kind == "thead_open":
            in_head = True
        elif kind == "thead_close":
            in_head = False
        elif kind == "tr_open" and table is not None:
            table.append([])
            header_rows += in_head
        elif kind == "table_close" and table:
            columns = max(len(row) for row in table)
            grid = doc.add_table(rows=len(table), cols=columns)
            grid.style = "Table Grid"
            for r, row in enumerate(table):
                for c, text in enumerate(row):
                    cell = grid.cell(r, c)
                    cell.text = text
                    if r < header_rows:
                        for run in cell.paragraphs[0].runs:
                            run.bold = True
            doc.add_paragraph()
            table = None
        elif kind == "inline" and table is not None:
            table[-1].append(token.content)
        elif kind == "inline":
            if heading_level is not None:
                paragraph = doc.add_heading(level=min(heading_level, 9))
            elif lists:
                depth = len(lists)
                if not item_started:
                    if lists[-1]["ordered"]:
                        # Written out, because Word's numbered style keeps counting across lists.
                        paragraph = doc.add_paragraph(style="List Paragraph")
                        paragraph.add_run(f"{lists[-1]['number']}. ")
                    else:
                        style = "List Bullet" if depth == 1 else f"List Bullet {min(depth, 3)}"
                        paragraph = doc.add_paragraph(style=style)
                    item_started = True
                else:
                    paragraph = doc.add_paragraph(style="List Paragraph")
                paragraph.paragraph_format.left_indent = Pt(18 * depth)
            elif quote_depth:
                paragraph = doc.add_paragraph(style="Quote")
            else:
                paragraph = doc.add_paragraph()
            _add_runs(paragraph, token.children or [])
        elif kind in ("fence", "code_block"):
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.left_indent = Pt(12)
            lines = token.content.rstrip("\n").split("\n")
            for i, line in enumerate(lines):
                run = paragraph.add_run(line)
                run.font.name = "Consolas"
                run.font.size = Pt(9.5)
                if i < len(lines) - 1:
                    run.add_break(WD_BREAK.LINE)
        elif kind == "hr":
            doc.add_paragraph("―" * 30).alignment = 1  # centered

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------------------------ Storage


@dataclass(frozen=True)
class SavedDocument:
    name: str
    title: str
    format: DocFormat
    size: int
    created: datetime

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "format": self.format,
            "size": self.size,
            "created": self.created.isoformat(),
            "url": f"/v1/documents/{self.name}",
        }


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].rstrip("-")
    return slug or "document"


def render(fmt: DocFormat, title: str, body: str) -> bytes:
    return render_pdf(title, body) if fmt == "pdf" else render_docx(title, body)


class DocumentStore:
    """The documents V.A.R.A. has made, in one folder of its own."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def create(
        self, markdown: str, fmt: DocFormat, title: str | None = None, now: datetime | None = None
    ) -> SavedDocument:
        """Render `markdown` and save it. Without `title`, a leading `# Heading` is the title."""
        if title:
            body = markdown
        else:
            title, body = split_title(markdown)
        data = render(fmt, title, body)
        now = now or datetime.now(UTC)
        self.folder.mkdir(parents=True, exist_ok=True)
        stem = f"{_slug(title)}-{now:%Y%m%d-%H%M%S}"
        for n in range(1000):
            name = f"{stem}{f'-{n}' if n else ''}.{fmt}"
            try:
                with open(self.folder / name, "xb") as file:  # never overwrite
                    file.write(data)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("Too many documents with the same name this second")
        return SavedDocument(name, title, fmt, len(data), now)

    def path(self, name: str) -> Path | None:
        """The file for a stored document's name, or None. Only names this store makes are accepted,
        so a name can't reach outside the folder."""
        if not NAME_PATTERN.match(name):
            return None
        path = self.folder / name
        return path if path.is_file() else None

    def all(self) -> list[dict]:
        if not self.folder.is_dir():
            return []
        found = []
        for path in self.folder.iterdir():
            if not NAME_PATTERN.match(path.name):
                continue
            stat = path.stat()
            found.append(
                {
                    "name": path.name,
                    "format": path.suffix[1:],
                    "size": stat.st_size,
                    "created": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                    "url": f"/v1/documents/{path.name}",
                }
            )
        return sorted(found, key=lambda d: d["created"], reverse=True)

    def delete(self, name: str) -> bool:
        path = self.path(name)
        if path is None:
            return False
        path.unlink()
        return True


# ---------------------------------------------------------------------------- Chat requests

_FORMAT_WORDS = r"(?P<fmt>pdf|\.pdf|word(?:\s+(?:doc|document|file))?|docx|\.docx|ms\s+word)"
_REQUEST = re.compile(
    r"\b(?:make|create|write|draft|generate|produce|build|give|put|save|turn|export|convert|"
    r"format|prepare|type)\b[^.?!\n]{0,120}?\b(?:as|into|in|to)?\s*(?:a|an|the)?\s*"
    + _FORMAT_WORDS
    + r"(?![\w.])",
    re.IGNORECASE,
)


def requested_format(text: str) -> DocFormat | None:
    """The file format a chat message asks for ("make me a PDF about ...", "write that up as a Word
    doc"), or None when it doesn't ask for a file."""
    match = _REQUEST.search(text)
    if match is None:
        return None
    word = match.group("fmt").lower()
    return "pdf" if "pdf" in word else "docx"


def document_instructions(fmt: DocFormat) -> str:
    label = "a PDF" if fmt == "pdf" else "a Word document"
    return (
        f"The user wants {label}. Write the complete document itself as your reply, in Markdown: "
        "start with a `# Title` line, then use headings, paragraphs, lists, and tables as the content "
        "needs. Don't keep it short, don't add remarks before or after it, and don't say you can't "
        f"create files: V.A.R.A. saves your reply as {label} and gives the user a download link."
    )
