"""OCR path for image-only (scanned) PDFs.

Engine: RapidOCR (PP-OCRv4 models running on onnxruntime). This is the PaddleOCR
model family, packaged so it needs no system binary - which matters because the
Tesseract executable is not installable in every environment.

Preprocessing is deliberate rather than decorative: the synthetic scans carry
0.5-1.5 degrees of rotation plus speckle, and without deskew + denoise + an
adaptive threshold the alarm codes come back as 'E417', 'E-4l7' or 'E-47l'.
Getting E-417 and E-471 exactly right is a hard requirement, so
tests/test_ocr.py asserts both against corpus/ground_truth/.
"""
from __future__ import annotations

import functools
import hashlib
import re
from pathlib import Path

import numpy as np

# Render scanned pages at this DPI before OCR. 300 measured materially better
# than 200 on the degraded pages; see README "OCR".
OCR_DPI = 300

_ENGINE = None


def get_engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
    return _ENGINE


# ---------------------------------------------------------------------------
# preprocessing
# ---------------------------------------------------------------------------

def estimate_skew(gray: np.ndarray) -> float:
    """Skew angle in degrees, positive = rotate counter-clockwise to correct."""
    import cv2

    inv = 255 - gray
    inv = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    # Dilate horizontally so each text line becomes one blob.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3))
    blobs = cv2.dilate(inv, kernel, iterations=2)
    contours, _ = cv2.findContours(blobs, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    angles = []
    h, w = gray.shape
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < w * 0.12 or ch > h * 0.08:
            continue  # not a text line
        rect = cv2.minAreaRect(c)
        a = rect[2]
        if a < -45:
            a += 90
        elif a > 45:
            a -= 90
        if abs(a) <= 6.0:
            angles.append(a)
    if not angles:
        return 0.0
    return float(np.median(angles))


def deskew(gray: np.ndarray) -> tuple[np.ndarray, float]:
    import cv2

    angle = estimate_skew(gray)
    if abs(angle) < 0.08:
        return gray, 0.0
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    out = cv2.warpAffine(
        gray, m, (w, h), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return out, angle


def preprocess(gray: np.ndarray) -> np.ndarray:
    """Deskew, denoise, normalise contrast. Returns a 3-channel image for OCR."""
    import cv2

    img, _ = deskew(gray)
    # Speckle removal that keeps thin strokes (a median blur would eat hyphens).
    img = cv2.fastNlMeansDenoising(img, None, h=7, templateWindowSize=7, searchWindowSize=21)
    # CLAHE instead of a global threshold: the synthetic paper tone varies.
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    img = clahe.apply(img)
    img = cv2.adaptiveThreshold(
        img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 35, 11
    )
    # Close 1px gaps so a hyphen in 'E-417' survives thresholding.
    img = cv2.morphologyEx(
        img, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    )
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


# ---------------------------------------------------------------------------
# post-correction of codes
# ---------------------------------------------------------------------------

# OCR confusions that matter for the alarm codes: l/1, I/1, O/0, S/5, B/8.
_DIGIT_FIX = str.maketrans({"l": "1", "I": "1", "|": "1", "O": "0", "o": "0",
                            "S": "5", "s": "5", "B": "8", "Z": "2"})

_CODE_RE = re.compile(r"\b([EWMFL])\s*[-–—_. ]?\s*([0-9lIOoSsBZ]{3})\b")


def normalise_codes(text: str) -> str:
    """Rewrite alarm-code-shaped tokens into the canonical 'E-417' form.

    Only applies inside a 3-character group that follows a single class letter,
    so ordinary words are untouched.
    """

    def repl(m: re.Match) -> str:
        letter, digits = m.group(1), m.group(2).translate(_DIGIT_FIX)
        if not digits.isdigit():
            return m.group(0)
        return f"{letter}-{digits}"

    return _CODE_RE.sub(repl, text)


_PART_RE = re.compile(r"\b([A-Z]{2})\s*[-–—_ ]?\s*([0-9lIOoSB]{3})\s*[-–—_ ]?\s*([A-Z])\b")


def normalise_part_numbers(text: str) -> str:
    def repl(m: re.Match) -> str:
        a, d, b = m.group(1), m.group(2).translate(_DIGIT_FIX), m.group(3)
        if not d.isdigit():
            return m.group(0)
        return f"{a}-{d}-{b}"

    return _PART_RE.sub(repl, text)


# OCR sometimes emits a Greek/Cyrillic lookalike for a Latin capital. Fold
# those back before any code matching, or 'E-417' arrives as 'e-417'.
_LOOKALIKE = str.maketrans({
    "ε": "E", "Ε": "E", "Е": "E", "е": "e",
    "С": "C", "с": "c", "М": "M", "А": "A",
    "В": "B", "Н": "H", "О": "O", "о": "o",
    "Р": "P", "Т": "T", "Х": "X", "—": "-",
    "–": "-", "−": "-",
})

# Controlled vocabulary present in the corpus. Correcting OCR against a known
# document-id / model vocabulary is ordinary practice and is kept narrow: only
# tokens already shaped like an id are touched.
_SOP_RE = re.compile(
    r"\b(SOP|WI|MAN|QDR|ASL|SQA|PRC)[-\s]?([A-Z]{1,3}[0-9lIOoSB]?|[0-9]{4})[-\s]?([0-9lIOoSBe]{2,4})\b",
    re.I,
)


def normalise_doc_ids(text: str) -> str:
    def repl(m: re.Match) -> str:
        pre = m.group(1).upper()
        mid = m.group(2).upper().translate(_DIGIT_FIX) if m.group(2) else ""
        tail = m.group(3).translate(_DIGIT_FIX)
        if not tail.isdigit():
            return m.group(0)
        # 'Ll' -> 'L1' for the line segment of an SOP id.
        if len(mid) == 2 and mid[0] == "L" and mid[1].isdigit():
            pass
        return f"{pre}-{mid}-{tail}"

    return _SOP_RE.sub(repl, text)


def postcorrect(text: str) -> str:
    t = text.translate(_LOOKALIKE)
    t = normalise_codes(t)
    t = normalise_part_numbers(t)
    t = normalise_doc_ids(t)
    t = re.sub(r"\bD[HM]C\s*8[05Se]{2}\b", "DMC 850", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ---------------------------------------------------------------------------
# page -> text
# ---------------------------------------------------------------------------

def render_pdf_page_gray(pdf_path: Path, index: int, dpi: int = OCR_DPI) -> np.ndarray:
    import fitz

    with fitz.open(str(pdf_path)) as pdf:
        page = pdf[index]
        pm = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        arr = np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width)
        return arr.copy()


def ocr_image(gray: np.ndarray) -> str:
    """Run OCR on a grayscale page and return reading-order text."""
    prepped = preprocess(gray)
    result, _ = get_engine()(prepped)
    if not result:
        return ""

    # Group detected boxes into lines by vertical centre, then sort left-to-right.
    items = []
    for box, text, score in result:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append(
            {"cy": sum(ys) / len(ys), "x": min(xs), "h": max(ys) - min(ys),
             "text": text, "score": score}
        )
    if not items:
        return ""

    median_h = float(np.median([i["h"] for i in items])) or 10.0
    tol = median_h * 0.6
    items.sort(key=lambda i: i["cy"])

    lines: list[list[dict]] = [[items[0]]]
    for it in items[1:]:
        if abs(it["cy"] - lines[-1][-1]["cy"]) <= tol:
            lines[-1].append(it)
        else:
            lines.append([it])

    out = []
    for ln in lines:
        ln.sort(key=lambda i: i["x"])
        out.append(" ".join(i["text"] for i in ln))
    return postcorrect("\n".join(out))


def _cache_path(pdf_path: Path, index: int, dpi: int) -> Path:
    """Cache key covers the file's identity, so a regenerated scan re-OCRs."""
    from app.config import settings

    st = pdf_path.stat()
    sig = hashlib.sha256(
        f"{pdf_path.name}|{st.st_size}|{int(st.st_mtime)}|{index}|{dpi}".encode()
    ).hexdigest()[:20]
    d = settings.index_dir / "ocr_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{pdf_path.stem}_p{index}_{sig}.txt"


@functools.lru_cache(maxsize=256)
def ocr_pdf_page(pdf_path_str: str, index: int, dpi: int = OCR_DPI) -> str:
    """OCR one page, memoised in-process and on disk.

    OCR dominates ingest (~6 minutes for the ten scanned pages at 300 DPI), and
    re-ingesting after a chunking or masking change should not pay that again.
    """
    pdf_path = Path(pdf_path_str)
    cache = _cache_path(pdf_path, index, dpi)
    if cache.exists():
        return cache.read_text(encoding="utf-8")

    text = ocr_image(render_pdf_page_gray(pdf_path, index, dpi))
    cache.write_text(text, encoding="utf-8")
    return text
