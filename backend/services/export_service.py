"""
services/export_service.py
----------------------------
Shared helpers for turning tabular data (a "properties assigned" list, the
audit ledger, etc.) into a downloadable CSV or PDF file. Used by
services/user_service.py, services/outsider_service.py, and
services/audit_service.py so every exporter in the app escapes/formats data
exactly the same way instead of re-implementing it three separate times.
"""

import csv
import datetime
import io
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from config import settings

# ---------------------------------------------------------------------------
# Unicode-capable font (currency symbols)
# ---------------------------------------------------------------------------
# reportlab's built-in "Helvetica"/"Helvetica-Bold" are the standard 14 PDF
# core fonts -- Latin-1 only. This app's default currency (settings.
# CURRENCY_CODE = "NGN", see config.py) prints with the Naira sign "₦"
# (U+20A6), which Latin-1 doesn't cover -- every export that runs
# format_money() through Helvetica would silently render it as a missing-
# glyph box. We vendor DejaVu Sans (backend/assets/fonts/, same "ship it in
# the repo instead of relying on what happens to be installed on the host"
# approach as build-tailwind's vendored CLI) and register it under the
# names below so every exporter in this module can reach for a font that
# actually has the glyph. Wrapped in try/except so a missing font file
# degrades to the old Latin-1-only behavior instead of crashing every PDF
# export outright.
_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FONT_REGULAR = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
try:
    pdfmetrics.registerFont(TTFont("DejaVuSans", str(_FONT_DIR / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(_FONT_DIR / "DejaVuSans-Bold.ttf")))
    FONT_REGULAR = "DejaVuSans"
    FONT_BOLD = "DejaVuSans-Bold"
except Exception:
    pass

# ---------------------------------------------------------------------------
# UTC -> display-timezone conversion for exports
# ---------------------------------------------------------------------------
# Every timestamp is stored/queried as UTC (see models.py's utc_now()) --
# that never changes here. What used to differ export-to-export was the
# DISPLAY of that UTC instant: the live UI converts to the viewer's own
# browser-local time (js/ui.js's formatTimestamp()), while these exports
# used to print the raw UTC numbers labeled "UTC" -- correct, but an hour
# (or more) off from what a person had just seen on the Audit Trail page,
# which is what actually caused the "exports are behind the Audit Trail"
# mismatch. A static export file has no browser to localize into at
# generation time, so it needs ONE fixed zone instead -- settings.DISPLAY_TIMEZONE
# (see config.py) -- and every exporter in the app now goes through this
# single helper so they all render the same hour, consistently.
# Public on purpose (unlike a leading-underscore "private" module var) --
# audit_service.py's date-range export filter also needs to build
# DISPLAY_TIMEZONE-aware boundaries directly, the same way this module
# does below.
DISPLAY_TZ = ZoneInfo(settings.DISPLAY_TIMEZONE)


def display_now() -> datetime.datetime:
    """The current instant, converted to DISPLAY_TIMEZONE -- use this (not
    utc_now()) anywhere an export prints "today"/"exported at" so those
    stamps land on the same wall-clock day/hour a person sees in the UI."""
    return datetime.datetime.now(DISPLAY_TZ)


def format_export_datetime(value: Optional[Union[str, datetime.datetime]]) -> str:
    """
    Turns a UTC timestamp -- either an aware `datetime` (e.g. an AuditLog's
    `.timestamp`) or one of the `.isoformat()` strings used throughout
    user_service.py/outsider_service.py's dicts -- into a
    "YYYY-MM-DD HH:MM:SS <ZONE>" string in DISPLAY_TIMEZONE, with the
    real zone abbreviation (e.g. "WAT") rather than a hardcoded "UTC" that
    would misrepresent the converted time. Every CSV/PDF exporter in the
    app should call this instead of formatting timestamps itself, so they
    all stay in sync with each other and with the Audit Trail on screen.
    """
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value)
        except ValueError:
            return value  # unparseable -- surface it as-is rather than silently dropping it
    if value.tzinfo is None:
        # Defensive: every DateTime(timezone=True) column round-trips as
        # aware UTC already (see models.py), but treat a naive value as
        # UTC rather than let a TypeError bubble up from astimezone().
        value = value.replace(tzinfo=datetime.timezone.utc)
    local = value.astimezone(DISPLAY_TZ)
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} {local.tzname()}"


# ---------------------------------------------------------------------------
# CSV / "Formula Injection" protection
# ---------------------------------------------------------------------------
# Several columns across our exports (asset names, user/outsider names,
# audit `details`, etc.) contain free text that ultimately originated from
# something someone typed elsewhere in the app. If any of that text happens
# to start with '=', '+', '-', or '@', Microsoft Excel / Google Sheets /
# LibreOffice will interpret the ENTIRE cell as a FORMULA the instant
# someone opens the exported CSV -- e.g. a name of "=HYPERLINK(...)" could
# silently execute a spreadsheet formula on whoever opens the file. We
# defend against this the standard, industry-recommended way: prefix any
# such value with a single quote (') before writing it to the CSV, which
# makes every spreadsheet program render it as plain literal text instead.
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")


# Currency symbols for the small set of codes this deployment is likely to
# use -- settings.CURRENCY_CODE (see config.py) defaults to "NGN". Falls
# back to printing the raw ISO code (e.g. "KES 1,000.00") for anything not
# in this table rather than guessing at a symbol.
_CURRENCY_SYMBOLS = {
    "NGN": "₦",
    "USD": "$",
    "GBP": "£",
    "EUR": "€",
    "GHS": "GH₵",
    "KES": "KSh",
    "ZAR": "R",
}


def format_money(value, currency_code: Optional[str] = None) -> str:
    """
    Formats a numeric amount as a "₦1,899.00"-style string for CSV/PDF
    exports, using settings.CURRENCY_CODE (see config.py) by default.
    Shared by every exporter that prints a price/total (Asset Inventory
    export, Quotation PDF export) so the symbol/format never drifts
    between them -- mirrors js/ui.js's formatPrice() on the frontend.
    """
    if value is None:
        return "—"
    code = (currency_code or settings.CURRENCY_CODE or "NGN").upper()
    symbol = _CURRENCY_SYMBOLS.get(code, f"{code} ")
    return f"{symbol}{float(value):,.2f}"


def csv_safe_cell(value) -> str:
    """Neutralizes formula-injection payloads before they reach a CSV cell."""
    text = "" if value is None else str(value)
    if text and text[0] in _FORMULA_TRIGGER_CHARS:
        return "'" + text
    return text


def build_csv_bytes(headers: Sequence[str], rows: Iterable[Sequence]) -> bytes:
    """
    Builds a complete CSV file in memory and returns it as UTF-8 bytes.

    This is appropriate for every export in this module EXCEPT the audit
    ledger's CSV export, which stays row-by-row STREAMED instead (see
    services/audit_service.export_audit_logs_csv) because that ledger is an
    append-only log that can grow without bound over the system's lifetime.
    Every other export here (one user's/outsider's assigned items, or a
    full directory's worth of them) is a bounded, "give me everything
    right now" dataset small enough to build in memory in one shot.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([csv_safe_cell(cell) for cell in row])
    return output.getvalue().encode("utf-8")


def _column_widths(available_width: float, headers: Sequence[str], rows: Sequence[Sequence], min_width: float = 0.55 * inch) -> list:
    """
    Splits `available_width` across the table's columns proportionally to
    how much text each one actually holds, instead of leaving reportlab to
    size columns off their *unwrapped* natural width (which is what let a
    long `Details`/`Operator` value push the audit-ledger PDF's table wider
    than the page). A column's "weight" is its longest cell (header or
    data), capped at CAP characters so a single very long free-text note
    doesn't swallow the whole table -- long values still fit, they just
    wrap onto multiple lines within their own column instead.

    After the proportional pass, any column that came out narrower than
    `min_width` (e.g. an "ID" column that's only ever 1-2 digits) is
    widened back up to that floor, and the extra space is taken back out
    of the other columns proportionally so the total still fits exactly
    inside `available_width` -- i.e. the table never exceeds the page's
    printable area, which is the actual bug this fixes.
    """
    n = len(headers)
    if n == 0:
        return []

    CAP = 40
    weights = []
    for i, header in enumerate(headers):
        longest = len(str(header))
        for row in rows:
            if i < len(row) and row[i] is not None:
                longest = max(longest, len(str(row[i])))
        weights.append(min(max(longest, 1), CAP))

    total_weight = sum(weights)
    raw_widths = [available_width * w / total_weight for w in weights]

    deficit = sum(max(0.0, min_width - w) for w in raw_widths)
    if deficit <= 0:
        return raw_widths

    flexible_total = sum(w for w in raw_widths if w > min_width) or 1
    widths = []
    for w in raw_widths:
        if w <= min_width:
            widths.append(min_width)
        else:
            widths.append(max(min_width, w - deficit * (w / flexible_total)))
    return widths


def build_pdf_bytes(title: str, subtitle: Optional[str], headers: Sequence[str], rows: Iterable[Sequence]) -> bytes:
    """
    Renders the same tabular data as a simple, print-friendly PDF using
    reportlab's Platypus layout engine: one title, an optional subtitle
    line, then a single table with a repeating header row on every page.

    `title`/`subtitle` and every table CELL are passed through `Paragraph`
    (not drawn as plain strings), which does two things:
      1. Lets long values (a lengthy audit `details` note, a full ISO
         timestamp, etc.) WRAP onto multiple lines within their own
         column instead of forcing the column -- and the whole table --
         wider than the page, which is what made the audit ledger PDF
         export unreadable/cut off before.
      2. Requires XML-escaping every value first, since `Paragraph`
         interprets a small subset of HTML-like markup and would
         otherwise raise a parse error (or misrender) on user-typed text
         containing a stray '<' or '&' (a name, an email, etc.).
    Column widths are computed by `_column_widths()` above so the table
    always sums to exactly the page's printable width, however many
    columns it has.
    """
    rows = list(rows)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch, topMargin=0.6 * inch, bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ExportTitle", parent=styles["Title"], fontName=FONT_BOLD)
    subtitle_style = ParagraphStyle("ExportSubtitle", parent=styles["Normal"], fontName=FONT_REGULAR)
    elements = [Paragraph(xml_escape(title), title_style)]
    if subtitle:
        elements.append(Paragraph(xml_escape(subtitle), subtitle_style))
    elements.append(Spacer(1, 0.25 * inch))

    header_style = ParagraphStyle("AuditTableHeader", parent=styles["Normal"], fontName=FONT_BOLD, fontSize=8, leading=10, textColor=colors.white)
    cell_style = ParagraphStyle("AuditTableCell", parent=styles["Normal"], fontName=FONT_REGULAR, fontSize=7.5, leading=9.5)

    def cell(value, style):
        text = "" if value is None else str(value)
        return Paragraph(xml_escape(text), style)

    table_data = [[cell(h, header_style) for h in headers]]
    table_data += [[cell(c, cell_style) for c in row] for row in rows]

    col_widths = _column_widths(doc.width, headers, rows)
    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elements.append(table)
    doc.build(elements)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Quotation / Checkout Receipt PDF -- a letterhead-style document, distinct
# from build_pdf_bytes() above (which is a bare "title + one big table"
# layout used by the Properties Assigned / Audit Ledger exports). This one
# mirrors the printed layout of the business's own paper rental-quotation
# template: a company letterhead + quote-number box, a bordered "CLIENT
# DETAILS" panel, the line-item table, a right-aligned totals box, a notes
# panel, and a Terms & Conditions / Authorisation footer -- so a Quotation
# exported at ANY status (Draft/Pending Review/Approved/Fulfilled -- see
# quotation_service.py's STATUS_LABELS) prints as a document a client can
# recognize, rather than a generic data table.
#
# Only services/quotation_service.py's _build_quotation_pdf() calls this;
# every other exporter in the app (users/outsiders/audit) keeps using the
# simpler build_pdf_bytes() above, since those really are just "one table"
# exports with no letterhead/client-details concept to render.
# ---------------------------------------------------------------------------

_DARK = colors.HexColor("#1f2937")
_BORDER = colors.HexColor("#94a3b8")
_STRIPE = colors.HexColor("#f1f5f9")

_ITEM_HEADERS = ["Item / Description", "Category", "Qty", "Daily Rate", "Days", "Total"]


def _qp_styles():
    """Paragraph styles shared across the Quotation PDF's sections."""
    base = getSampleStyleSheet()
    return {
        "company": ParagraphStyle("QPCompany", parent=base["Title"], fontName=FONT_BOLD, fontSize=15, leading=18, alignment=0),
        "doctype": ParagraphStyle("QPDocType", parent=base["Normal"], fontName=FONT_BOLD, fontSize=10, textColor=_DARK),
        "meta": ParagraphStyle("QPMeta", parent=base["Normal"], fontName=FONT_REGULAR, fontSize=9, leading=13, alignment=TA_RIGHT),
        "section": ParagraphStyle("QPSection", parent=base["Normal"], fontName=FONT_BOLD, fontSize=9, textColor=colors.white),
        "label": ParagraphStyle("QPLabel", parent=base["Normal"], fontName=FONT_BOLD, fontSize=8, leading=11),
        "value": ParagraphStyle("QPValue", parent=base["Normal"], fontName=FONT_REGULAR, fontSize=8, leading=11),
        "itemHeader": ParagraphStyle("QPItemHeader", parent=base["Normal"], fontName=FONT_BOLD, fontSize=8, leading=10, textColor=colors.white),
        "itemCell": ParagraphStyle("QPItemCell", parent=base["Normal"], fontName=FONT_REGULAR, fontSize=7.5, leading=9.5),
        "itemCellRight": ParagraphStyle("QPItemCellRight", parent=base["Normal"], fontName=FONT_REGULAR, fontSize=7.5, leading=9.5, alignment=TA_RIGHT),
        "summaryLabel": ParagraphStyle("QPSummaryLabel", parent=base["Normal"], fontName=FONT_REGULAR, fontSize=8.5, alignment=TA_RIGHT),
        "summaryValue": ParagraphStyle("QPSummaryValue", parent=base["Normal"], fontName=FONT_REGULAR, fontSize=8.5, alignment=TA_RIGHT),
        "summaryTotalLabel": ParagraphStyle("QPSummaryTotalLabel", parent=base["Normal"], fontName=FONT_BOLD, fontSize=9.5, alignment=TA_RIGHT),
        "summaryTotalValue": ParagraphStyle("QPSummaryTotalValue", parent=base["Normal"], fontName=FONT_BOLD, fontSize=9.5, alignment=TA_RIGHT),
        "notes": ParagraphStyle("QPNotes", parent=base["Normal"], fontName=FONT_REGULAR, fontSize=8.5, leading=12),
        "terms": ParagraphStyle("QPTerms", parent=base["Normal"], fontName=FONT_REGULAR, fontSize=7.5, leading=11),
        "footer": ParagraphStyle("QPFooter", parent=base["Normal"], fontName=FONT_REGULAR, fontSize=7.5, alignment=1, textColor=colors.HexColor("#64748b")),
    }


def _qp_p(text, style):
    text = "" if text is None else str(text)
    return Paragraph(xml_escape(text), style)


def _qp_section_bar(text, width, style):
    """A single full-width shaded bar, e.g. 'CLIENT DETAILS' / 'QUOTED ITEMS' --
    mirrors the merged section-header row in the paper template."""
    t = Table([[_qp_p(text, style)]], colWidths=[width])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _DARK),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("BOX", (0, 0), (-1, -1), 0.5, _DARK),
    ]))
    return t


def build_quotation_document_pdf(
    site_name: str,
    reference_number: Optional[str],
    date_str: str,
    status_label: str,
    client_fields: Sequence[tuple],
    items: Sequence[Sequence],
    summary_rows: Sequence[tuple],
    notes: Optional[str],
    terms: Sequence[str],
) -> bytes:
    """
    Builds the full letterhead-style Quotation / Checkout Receipt PDF.

    client_fields: list of (label, value) pairs rendered as a 2-column x
        N-row grid, e.g. ("Customer Name", "Fatherland").
    items: rows already formatted as display strings, matching
        _ITEM_HEADERS column-for-column.
    summary_rows: list of (label, value, is_grand_total) tuples rendered
        right-aligned, e.g. ("Subtotal", "₦2,826,000.00", False).
    terms: plain-text lines rendered as a numbered Terms & Conditions list.
    """
    styles = _qp_styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch, topMargin=0.5 * inch, bottomMargin=0.5 * inch,
    )
    width = doc.width
    elements = []

    # --- Letterhead: company name / doc type (left) + quote no / date / status (right) ---
    meta_lines = [
        f"Quotation No: {reference_number or 'DRAFT'}",
        f"Date: {date_str}",
        f"Status: {status_label}",
    ]
    meta_text = "<br/>".join(xml_escape(line) for line in meta_lines)
    letterhead = Table(
        [[
            [_qp_p(site_name, styles["company"]), _qp_p("EQUIPMENT RENTAL QUOTATION", styles["doctype"])],
            Paragraph(meta_text, styles["meta"]),
        ]],
        colWidths=[width * 0.62, width * 0.38],
    )
    letterhead.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, _DARK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(letterhead)
    elements.append(Spacer(1, 0.15 * inch))

    # --- Client details panel ---
    elements.append(_qp_section_bar("CLIENT DETAILS", width, styles["section"]))
    grid_rows = []
    for i in range(0, len(client_fields), 2):
        pair = client_fields[i:i + 2]
        row = []
        for label, value in pair:
            row.append(_qp_p(label, styles["label"]))
            row.append(_qp_p(value, styles["value"]))
        if len(pair) == 1:
            row += [Paragraph("", styles["label"]), Paragraph("", styles["value"])]
        grid_rows.append(row)
    client_table = Table(grid_rows, colWidths=[width * 0.16, width * 0.34] * 2)
    client_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, _BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(client_table)
    elements.append(Spacer(1, 0.15 * inch))

    # --- Quoted items ---
    elements.append(_qp_section_bar("QUOTED ITEMS", width, styles["section"]))
    header_row = [_qp_p(h, styles["itemHeader"]) for h in _ITEM_HEADERS]
    body_rows = []
    for row in items:
        formatted = [
            _qp_p(row[0], styles["itemCell"]),
            _qp_p(row[1], styles["itemCell"]),
            _qp_p(row[2], styles["itemCellRight"]),
            _qp_p(row[3], styles["itemCellRight"]),
            _qp_p(row[4], styles["itemCellRight"]),
            _qp_p(row[5], styles["itemCellRight"]),
        ]
        body_rows.append(formatted)
    item_col_widths = [width * 0.34, width * 0.16, width * 0.08, width * 0.16, width * 0.08, width * 0.18]
    item_table = Table([header_row] + body_rows, colWidths=item_col_widths, repeatRows=1)
    item_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _DARK),
        ("GRID", (0, 0), (-1, -1), 0.5, _BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _STRIPE]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elements.append(item_table)
    elements.append(Spacer(1, 0.15 * inch))

    # --- Summary (right-aligned totals box, same width as the letterhead's right column) ---
    elements.append(_qp_section_bar("SUMMARY", width, styles["section"]))
    summary_data = []
    for label, value, is_total in summary_rows:
        label_style = styles["summaryTotalLabel"] if is_total else styles["summaryLabel"]
        value_style = styles["summaryTotalValue"] if is_total else styles["summaryValue"]
        summary_data.append([_qp_p(label, label_style), _qp_p(value, value_style)])
    summary_table = Table(summary_data, colWidths=[width * 0.75, width * 0.25])
    summary_row_styles = [
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("BOX", (0, 0), (-1, -1), 0.5, _BORDER),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, _BORDER),
    ]
    for idx, (_, _, is_total) in enumerate(summary_rows):
        if is_total:
            summary_row_styles.append(("BACKGROUND", (0, idx), (-1, idx), _STRIPE))
    summary_table.setStyle(TableStyle(summary_row_styles))
    elements.append(summary_table)
    elements.append(Spacer(1, 0.15 * inch))

    # --- Special notes ---
    elements.append(_qp_section_bar("SPECIAL NOTES", width, styles["section"]))
    notes_table = Table([[_qp_p(notes or "N/A", styles["notes"])]], colWidths=[width])
    notes_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, _BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.append(notes_table)
    elements.append(Spacer(1, 0.15 * inch))

    # --- Terms & Conditions (left) / Authorisation signature lines (right) ---
    terms_text = "<br/>".join(f"{i}. {xml_escape(t)}" for i, t in enumerate(terms, start=1))
    terms_para = Paragraph(terms_text, styles["terms"])
    sig_lines = (
        f"{'_' * 34}<br/>Authorised Signature<br/><br/>"
        f"{'_' * 34}<br/>Customer Signature<br/><br/>"
        f"{'_' * 34}<br/>Date Accepted"
    )
    sig_para = Paragraph(sig_lines, styles["value"])
    footer_table = Table([[terms_para, sig_para]], colWidths=[width * 0.58, width * 0.42])
    footer_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, _BORDER),
        ("LINEAFTER", (0, 0), (0, 0), 0.5, _BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(footer_table)
    elements.append(Spacer(1, 0.12 * inch))
    elements.append(_qp_p(f"Thank you for choosing {site_name}.", styles["footer"]))

    doc.build(elements)
    return buffer.getvalue()
