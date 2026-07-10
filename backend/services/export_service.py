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
import io
from typing import Iterable, Optional, Sequence
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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
    elements = [Paragraph(xml_escape(title), styles["Title"])]
    if subtitle:
        elements.append(Paragraph(xml_escape(subtitle), styles["Normal"]))
    elements.append(Spacer(1, 0.25 * inch))

    header_style = ParagraphStyle("AuditTableHeader", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white)
    cell_style = ParagraphStyle("AuditTableCell", parent=styles["Normal"], fontName="Helvetica", fontSize=7.5, leading=9.5)

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
