"""CV Book generation for Oxford Alpha Fund."""
import io
import logging
from collections import defaultdict
from pathlib import Path
from typing import List, Dict, Any, Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, white, black
from reportlab.pdfgen import canvas as rl_canvas
from pypdf import PdfWriter, PdfReader

log = logging.getLogger("cv_book")

W, H = A4  # 595.27 x 841.89 pts

LIGHT_BLUE = HexColor("#7BBFDE")
MID_BLUE = HexColor("#1B75BC")
DARK_NAVY = HexColor("#1A2D5A")

STATIC_DIR = Path(__file__).parent / "static"
LOGO_PATH = STATIC_DIR / "oaf_logo.png"


def _draw_bars(c: rl_canvas.Canvas):
    """Three staggered rectangles at top-right, matching the original CV book design."""
    # Light blue — rightmost, shortest
    bar_right = W
    c.setFillColor(LIGHT_BLUE)
    c.rect(bar_right - 42, H - 130, 42, 130, fill=1, stroke=0)

    # Mid blue — overlaps to the left, taller
    c.setFillColor(MID_BLUE)
    c.rect(bar_right - 80, H - 200, 50, 200, fill=1, stroke=0)

    # Dark navy — leftmost, tallest
    c.setFillColor(DARK_NAVY)
    c.rect(bar_right - 115, H - 340, 48, 340, fill=1, stroke=0)


def _draw_logo(c: rl_canvas.Canvas):
    """OAF logo top-left. Uses image if available, else text placeholder."""
    if LOGO_PATH.exists():
        c.drawImage(
            str(LOGO_PATH), 48, H - 130,
            width=140, height=90,
            preserveAspectRatio=True, mask="auto",
        )
    else:
        # Programmatic fallback: "OAF" with OXFORD ALPHA FUND beneath
        c.setFillColor(DARK_NAVY)
        c.setFont("Helvetica-Bold", 36)
        c.drawString(48, H - 90, "OAF")
        c.setFont("Helvetica", 9)
        c.setFillColor(MID_BLUE)
        c.drawString(48, H - 108, "OXFORD  ALPHA  FUND")


def _make_title_page(main_text: str, sub_text: Optional[str] = None) -> bytes:
    """Render one A4 cover/section page and return its raw bytes."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)

    # White background
    c.setFillColor(white)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    _draw_bars(c)
    _draw_logo(c)

    # Main bold title — left-aligned, mid-page
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 46)
    c.drawString(48, H / 2 + 10, main_text)

    if sub_text:
        c.setFont("Helvetica-Oblique", 26)
        c.setFillColor(HexColor("#444444"))
        c.drawString(50, H / 2 - 36, sub_text)

    c.save()
    buf.seek(0)
    return buf.read()


def _add_pdf_bytes(writer: PdfWriter, pdf_bytes: bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    for page in reader.pages:
        writer.add_page(page)


def build_cv_book(
    members: List[Dict[str, Any]],
    year_label: str = "2025-2026",
) -> bytes:
    """
    Build the CV book PDF.

    members — list of dicts:
        username, full_name, graduation_year (int|None),
        track ("Fundamental"|"Quant"|None), cv_bytes (bytes|None)

    Returns the merged PDF as bytes.
    """
    writer = PdfWriter()

    # ── Cover page ──────────────────────────────────────────────────────────
    cover = _make_title_page("Oxford Alpha Fund", f"{year_label} CV Book")
    _add_pdf_bytes(writer, cover)

    # ── Group by graduation year then track ─────────────────────────────────
    by_year: Dict[str, Dict[str, List]] = defaultdict(
        lambda: {"Fundamental": [], "Quant": []}
    )
    for m in members:
        track = m.get("track") or ""
        if track not in ("Fundamental", "Quant"):
            continue  # skip general, bootcamp, unset
        yr = str(m.get("graduation_year") or "Unknown")
        by_year[yr][track].append(m)

    for grad_year in sorted(by_year.keys()):
        tracks_in_year = by_year[grad_year]
        # Only analysts appear in the CV book — bootcamp and general are excluded
        for track in ["Fundamental", "Quant"]:
            group = tracks_in_year[track]
            if not group:
                continue

            # Section divider page
            section_title = f"{grad_year} Graduates"
            divider = _make_title_page(section_title, track)
            _add_pdf_bytes(writer, divider)

            # Individual CVs
            for member in group:
                cv_bytes = member.get("cv_bytes")
                if not cv_bytes:
                    continue
                try:
                    _add_pdf_bytes(writer, cv_bytes)
                except Exception as exc:
                    log.warning("Skipping CV for %s: %s", member.get("username"), exc)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()
