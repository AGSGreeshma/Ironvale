"""Document parsers.

Each parser returns a list of `Page` records so that downstream chunking can
keep a printed page number for scanned material. Digital formats have no
meaningful page number for citation purposes, so they report page=None and are
cited by section instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Page:
    text: str
    page: int | None = None          # printed page number, scanned docs only
    source: str = "digital"          # digital | scanned
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------- markdown

def parse_md(path: Path) -> list[Page]:
    return [Page(text=path.read_text(encoding="utf-8"), source="digital")]


# -------------------------------------------------------------------- html

def parse_html(path: Path) -> list[Page]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    out: list[str] = []
    for el in soup.body.descendants if soup.body else []:
        name = getattr(el, "name", None)
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            out.append("\n" + "#" * int(name[1]) + " " + el.get_text(" ", strip=True))
        elif name == "p":
            out.append(el.get_text(" ", strip=True))
        elif name == "blockquote":
            out.append("> " + el.get_text(" ", strip=True))
        elif name == "li":
            out.append("- " + el.get_text(" ", strip=True))
        elif name == "table":
            for tr in el.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
                if cells:
                    out.append("| " + " | ".join(cells) + " |")
            out.append("")
    return [Page(text="\n".join(out), source="digital")]


# -------------------------------------------------------------------- docx

def parse_docx(path: Path) -> list[Page]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    docu = Document(str(path))
    out: list[str] = []

    # Walk body children in document order so tables stay where they belong.
    body = docu.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, docu)
            txt = p.text.strip()
            if not txt:
                continue
            style = (p.style.name or "").lower()
            if style.startswith("heading"):
                try:
                    lvl = int(style.split()[-1])
                except (ValueError, IndexError):
                    lvl = 2
                out.append("\n" + "#" * max(1, min(lvl, 6)) + " " + txt)
            elif style.startswith("list"):
                out.append("- " + txt)
            else:
                out.append(txt)
        elif tag == "tbl":
            t = Table(child, docu)
            for row in t.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    out.append("| " + " | ".join(cells) + " |")
            out.append("")
    return [Page(text="\n".join(out), source="digital")]


# --------------------------------------------------------------------- pdf

_FOOTER_PAGE = re.compile(r"^\s*page\s+(\d+)\s*$", re.I | re.M)


def pdf_has_text_layer(path: Path, min_chars: int = 200) -> bool:
    import fitz

    with fitz.open(str(path)) as pdf:
        total = sum(len(pdf[i].get_text("text").strip()) for i in range(min(len(pdf), 4)))
    return total >= min_chars


def parse_pdf_digital(path: Path) -> list[Page]:
    """Digital PDF: take the embedded text layer, no OCR."""
    import fitz

    pages: list[Page] = []
    with fitz.open(str(path)) as pdf:
        for i in range(len(pdf)):
            txt = pdf[i].get_text("text")
            pages.append(Page(text=txt, page=None, source="digital",
                              meta={"pdf_index": i}))
    return pages


def parse_pdf_scanned(path: Path, page_map: list[dict] | None = None) -> list[Page]:
    """Image-only PDF: OCR every page and attach the printed page number.

    `page_map` comes from corpus/manifest.json and maps pdf index -> printed
    page. If it is missing we fall back to reading the printed number out of
    the OCR'd footer, and only then to the pdf index.
    """
    import fitz

    from app.ingest.ocr import ocr_pdf_page

    by_index = {m["pdf_index"]: m["printed_page"] for m in (page_map or [])}

    with fitz.open(str(path)) as pdf:
        n = len(pdf)

    pages: list[Page] = []
    for i in range(n):
        text = ocr_pdf_page(str(path), i)
        printed = by_index.get(i)
        if printed is None:
            m = _FOOTER_PAGE.search(text)
            printed = int(m.group(1)) if m else i + 1
        pages.append(
            Page(text=text, page=int(printed), source="scanned",
                 meta={"pdf_index": i, "ocr": True})
        )
    return pages


# ------------------------------------------------------------------ dispatch

def parse(path: Path, fmt: str | None = None, page_map=None) -> list[Page]:
    suffix = (fmt or path.suffix.lstrip(".")).lower()
    if suffix in ("md", "markdown"):
        return parse_md(path)
    if suffix in ("html", "htm"):
        return parse_html(path)
    if suffix == "docx":
        return parse_docx(path)
    if suffix == "pdf":
        if pdf_has_text_layer(path):
            return parse_pdf_digital(path)
        return parse_pdf_scanned(path, page_map)
    raise ValueError(f"unsupported format: {path.name}")
