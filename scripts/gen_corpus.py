"""Render the Ironvale corpus to corpus/.

  md           -> corpus/<doc_id>.md
  html         -> corpus/<doc_id>.html
  docx         -> corpus/<doc_id>.docx
  pdf          -> corpus/<doc_id>.pdf            (digital text layer)
  scanned_pdf  -> corpus/scanned/<doc_id>.pdf    (image-only, noisy, rotated)

corpus/manifest.json is the authoritative metadata record consumed by ingest.
corpus/ground_truth/ holds the pre-render text of each scanned page; it is for
tests only and is never indexed.

Deterministic: same seed in, same bytes out (bar PDF timestamps).

    python scripts/gen_corpus.py
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import corpus_docs  # noqa: E402
import corpus_docs_b  # noqa: E402,F401  (registers part B)
import corpus_filler  # noqa: E402,F401  (registers filler)

CORPUS = ROOT / "corpus"
SCANNED = CORPUS / "scanned"
GT = CORPUS / "ground_truth"

RENDER_DPI = 250
SEED = 1337

# Access rule, stated once. Prefix -> roles allowed to open the document.
ACCESS_BY_PREFIX = {
    "SOP": corpus_docs.ALL,
    "WI": corpus_docs.ALL,
    "MAN": corpus_docs.ALL,
    "QDR": corpus_docs.QE_HEAD,
    "ASL": corpus_docs.QE_HEAD,
    "SQA": corpus_docs.QE_HEAD,
    "PRC": corpus_docs.HEAD,
}


# ---------------------------------------------------------------------------
# markdown-lite -> block list
# ---------------------------------------------------------------------------

def parse_blocks(md: str) -> list[dict]:
    """Turn the authored markdown into (heading | para | list | table) blocks."""
    blocks: list[dict] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("#"):
            level = len(s) - len(s.lstrip("#"))
            blocks.append({"t": "h", "level": level, "text": s.lstrip("#").strip()})
            i += 1
            continue
        if s.startswith("|") and s.endswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= set("-: ") and c for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append({"t": "table", "rows": rows})
            continue
        if s.startswith("> "):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append({"t": "quote", "text": " ".join(quote)})
            continue
        m = re.match(r"^(\d+)\.\s+(.*)$", s)
        b = re.match(r"^[-*]\s+(.*)$", s)
        if m or b:
            items, ordered = [], bool(m)
            while i < len(lines):
                ss = lines[i].strip()
                mm = re.match(r"^(\d+)\.\s+(.*)$", ss)
                bb = re.match(r"^[-*]\s+(.*)$", ss)
                if mm:
                    items.append(mm.group(2))
                elif bb:
                    items.append(bb.group(1))
                elif ss and items and not ss.startswith(("|", "#", ">")):
                    items[-1] += " " + ss  # continuation line
                else:
                    break
                i += 1
            blocks.append({"t": "list", "ordered": ordered, "items": items})
            continue
        para = []
        while i < len(lines):
            ss = lines[i].strip()
            if not ss or ss.startswith(("#", "|", ">", "- ", "* ")) or re.match(r"^\d+\.\s", ss):
                break
            para.append(ss)
            i += 1
        if para:
            blocks.append({"t": "p", "text": " ".join(para)})
    return blocks


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------

def write_md(path: Path, d: dict) -> None:
    path.write_text(d["body"], encoding="utf-8")


def write_html(path: Path, d: dict) -> None:
    blocks = parse_blocks(d["body"])
    out = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{d['doc_id']} - {d['title']}</title>",
        "<style>body{font-family:Georgia,serif;max-width:52em;margin:2em auto;line-height:1.5}"
        "table{border-collapse:collapse;margin:1em 0}td,th{border:1px solid #999;padding:4px 10px}"
        "</style></head><body>",
    ]
    for b in blocks:
        if b["t"] == "h":
            lvl = min(b["level"], 6)
            out.append(f"<h{lvl}>{b['text']}</h{lvl}>")
        elif b["t"] == "p":
            out.append(f"<p>{b['text']}</p>")
        elif b["t"] == "quote":
            out.append(f"<blockquote>{b['text']}</blockquote>")
        elif b["t"] == "list":
            tag = "ol" if b["ordered"] else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{x}</li>" for x in b["items"]) + f"</{tag}>")
        elif b["t"] == "table":
            head, *rest = b["rows"]
            out.append("<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in head) + "</tr></thead><tbody>")
            for r in rest:
                out.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
    out.append("</body></html>")
    path.write_text("\n".join(out), encoding="utf-8")


def write_docx(path: Path, d: dict) -> None:
    from docx import Document
    from docx.shared import Pt

    docu = Document()
    style = docu.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    for b in parse_blocks(d["body"]):
        if b["t"] == "h":
            docu.add_heading(b["text"], level=min(b["level"], 4))
        elif b["t"] == "p":
            docu.add_paragraph(b["text"])
        elif b["t"] == "quote":
            p = docu.add_paragraph(b["text"])
            p.style = docu.styles["Intense Quote"]
        elif b["t"] == "list":
            st = "List Number" if b["ordered"] else "List Bullet"
            for item in b["items"]:
                docu.add_paragraph(item, style=st)
        elif b["t"] == "table":
            rows = b["rows"]
            t = docu.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            t.style = "Table Grid"
            for ri, r in enumerate(rows):
                for ci, c in enumerate(r):
                    t.cell(ri, ci).text = c
    docu.save(str(path))


def write_pdf(path: Path, d: dict) -> None:
    """Digital PDF with a real text layer (exercises the non-OCR PDF path)."""
    import fitz

    pdf = fitz.open()
    page = pdf.new_page(width=595, height=842)  # A4 points
    y, margin, bottom = 60.0, 56.0, 790.0
    width = 595 - 2 * margin

    def newpage():
        nonlocal page, y
        page = pdf.new_page(width=595, height=842)
        y = 60.0

    for b in parse_blocks(d["body"]):
        if y > bottom - 40:
            newpage()
        if b["t"] == "h":
            size = {1: 16, 2: 13, 3: 11.5}.get(b["level"], 11)
            y += 8
            page.insert_textbox(
                fitz.Rect(margin, y, margin + width, y + size * 2.2),
                b["text"], fontname="Helvetica-Bold", fontsize=size,
            )
            y += size * 1.9
        elif b["t"] in ("p", "quote"):
            text = b["text"]
            h = 12 * (1 + len(text) // 88) + 14
            if y + h > bottom:
                newpage()
            page.insert_textbox(
                fitz.Rect(margin, y, margin + width, y + h + 20),
                text, fontname="Times-Roman", fontsize=10.5, lineheight=1.35,
            )
            y += h
        elif b["t"] == "list":
            for n, item in enumerate(b["items"], 1):
                bullet = f"{n}. " if b["ordered"] else "- "
                h = 12 * (1 + len(item) // 84) + 4
                if y + h > bottom:
                    newpage()
                page.insert_textbox(
                    fitz.Rect(margin + 10, y, margin + width, y + h + 16),
                    bullet + item, fontname="Times-Roman", fontsize=10.5, lineheight=1.3,
                )
                y += h
            y += 6
        elif b["t"] == "table":
            rows = b["rows"]
            ncol = max(len(r) for r in rows)
            colw = width / ncol
            for ri, r in enumerate(rows):
                if y + 20 > bottom:
                    newpage()
                for ci in range(ncol):
                    cell = r[ci] if ci < len(r) else ""
                    rect = fitz.Rect(margin + ci * colw, y, margin + (ci + 1) * colw, y + 19)
                    page.draw_rect(rect, color=(0.6, 0.6, 0.6), width=0.4)
                    page.insert_textbox(
                        fitz.Rect(rect.x0 + 3, rect.y0 + 4, rect.x1 - 2, rect.y1),
                        cell,
                        fontname="Helvetica-Bold" if ri == 0 else "Helvetica",
                        fontsize=8.2,
                    )
                y += 19
            y += 10

    pdf.save(str(path), deflate=True)
    pdf.close()


# ---------------------------------------------------------------------------
# scanned rendering: text -> noisy, blurred, rotated page image -> image-only PDF
# ---------------------------------------------------------------------------

def _mono_font(size: int):
    from PIL import ImageFont

    for name in ("consola.ttf", "cour.ttf", "lucon.ttf", "DejaVuSansMono.ttf"):
        for base in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")):
            p = base / name
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def render_scan_page(text: str, printed_page: int, rng: random.Random):
    """One page of plain text -> a realistically degraded grayscale page image."""
    from PIL import Image, ImageDraw, ImageFilter

    # A4 at RENDER_DPI
    W = int(8.27 * RENDER_DPI)
    H = int(11.69 * RENDER_DPI)
    # Slightly off-white paper so thresholding has something to do.
    img = Image.new("L", (W, H), color=rng.randint(243, 250))
    draw = ImageDraw.Draw(img)

    # Size the glyphs like a real 10.5pt monospace listing: at RENDER_DPI the
    # usable width must fit ~80 columns, which puts the em at ~0.135in.
    # (An earlier pass used 0.052in - far too small, and OCR mangled it.)
    fsize = int(RENDER_DPI * 0.135)
    font = _mono_font(fsize)
    line_h = int(fsize * 1.46)
    x0 = int(RENDER_DPI * 0.75)
    y = int(RENDER_DPI * 0.7)

    # Drop any footer the author wrote inline; we stamp it uniformly below.
    body_lines = [
        ln for ln in text.splitlines()
        if not re.match(r"^\s*page\s+\d+\s*$", ln, re.I)
    ]

    for ln in body_lines:
        if y > H - int(RENDER_DPI * 0.95):
            break
        # Per-line jitter: a real scan is never perfectly aligned.
        jx = x0 + rng.randint(-2, 2)
        jy = y + rng.randint(-1, 1)
        ink = rng.randint(18, 48)
        draw.text((jx, jy), ln, fill=ink, font=font)
        y += line_h

    # Printed page number in the footer - this is what a citation must report.
    foot = _mono_font(int(fsize * 0.95))
    draw.text(
        (W - int(RENDER_DPI * 1.9), H - int(RENDER_DPI * 0.62)),
        f"page {printed_page}",
        fill=rng.randint(25, 55),
        font=foot,
    )

    # ---- degradation: blur, speckle noise, slight rotation
    img = img.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.6, 1.0)))

    import numpy as np

    arr = np.asarray(img).astype(np.float32)
    arr += np.random.default_rng(rng.randint(0, 2**31)).normal(0, rng.uniform(5.0, 9.0), arr.shape)
    # A few dust specks.
    rr = np.random.default_rng(rng.randint(0, 2**31))
    for _ in range(rng.randint(40, 110)):
        sy, sx = rr.integers(0, H - 3), rr.integers(0, W - 3)
        arr[sy:sy + rr.integers(1, 3), sx:sx + rr.integers(1, 3)] = rr.integers(0, 90)
    arr = np.clip(arr, 0, 255).astype("uint8")
    img = Image.fromarray(arr, mode="L")

    angle = rng.choice([-1, 1]) * rng.uniform(0.5, 1.5)
    img = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=246)
    return img, angle


def write_scanned_pdf(path: Path, d: dict, rng: random.Random) -> list[dict]:
    """Image-only PDF. No text layer at all - ingest must OCR it."""
    import fitz
    from io import BytesIO

    pdf = fitz.open()
    page_meta = []
    for idx, pg in enumerate(d["pages"]):
        printed = pg["printed_page"]
        img, angle = render_scan_page(pg["text"], printed, rng)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=84, optimize=True)
        data = buf.getvalue()

        page = pdf.new_page(width=595, height=842)
        page.insert_image(fitz.Rect(0, 0, 595, 842), stream=data)
        page_meta.append(
            {"pdf_index": idx, "printed_page": printed, "rotation_deg": round(angle, 2)}
        )
        # Ground truth for tests only - never indexed.
        (GT / f"{d['doc_id']}_p{printed}.txt").write_text(pg["text"], encoding="utf-8")

    pdf.save(str(path), deflate=True)
    pdf.close()
    return page_meta


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    for p in (CORPUS, SCANNED, GT):
        p.mkdir(parents=True, exist_ok=True)

    rng = random.Random(SEED)
    manifest = []
    counts: dict[str, int] = {}

    for d in corpus_docs.DOCS:
        doc_id = d["doc_id"]
        fmt = d["fmt"]
        prefix = doc_id.split("-")[0]
        expected = ACCESS_BY_PREFIX.get(prefix)
        access = d.get("access") or expected
        if expected is not None and sorted(access) != sorted(expected):
            raise SystemExit(f"{doc_id}: access {access} disagrees with prefix rule {expected}")

        entry = {
            "doc_id": doc_id,
            "title": d["title"],
            "version": d.get("version"),
            "effective_date": d.get("effective_date"),
            "superseded": bool(d.get("superseded")),
            "superseded_on": d.get("superseded_on"),
            "allowed_roles": list(access),
            "scope": d.get("scope", "plant"),
            "filler": bool(d.get("filler")),
        }

        if fmt == "md":
            rel = f"{doc_id}.md"
            write_md(CORPUS / rel, d)
            entry.update(path=rel, source="digital", fmt="md")
        elif fmt == "html":
            rel = f"{doc_id}.html"
            write_html(CORPUS / rel, d)
            entry.update(path=rel, source="digital", fmt="html")
        elif fmt == "docx":
            rel = f"{doc_id}.docx"
            write_docx(CORPUS / rel, d)
            entry.update(path=rel, source="digital", fmt="docx")
        elif fmt == "pdf":
            rel = f"{doc_id}.pdf"
            write_pdf(CORPUS / rel, d)
            entry.update(path=rel, source="digital", fmt="pdf")
        elif fmt == "scanned_pdf":
            rel = f"scanned/{doc_id}.pdf"
            meta = write_scanned_pdf(CORPUS / rel, d, rng)
            entry.update(
                path=rel, source="scanned", fmt="pdf",
                printed_page_start=d.get("printed_page_start"),
                pages=meta,
            )
        else:
            raise SystemExit(f"{doc_id}: unknown fmt {fmt}")

        counts[fmt] = counts.get(fmt, 0) + 1
        manifest.append(entry)

    (CORPUS / "manifest.json").write_text(
        json.dumps({"documents": manifest}, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    restricted = [m["doc_id"] for m in manifest if "line_supervisor" not in m["allowed_roles"]]
    print(f"wrote {len(manifest)} documents to {CORPUS}")
    print("  by format:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"  restricted from supervisors ({len(restricted)}):", ", ".join(sorted(restricted)))
    print(f"  head-only:", ", ".join(
        sorted(m["doc_id"] for m in manifest if m["allowed_roles"] == ["plant_head"])
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
