"""
Quote PDF generation and R2 upload.
Uses reportlab for PDF, existing r2_storage service for upload.
"""

import io
from datetime import datetime, timezone
from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import HexColor
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from app.services.r2_storage import r2_available, upload_to_r2


NAVY = HexColor("#0a1628")
CYAN = HexColor("#0ef0d8")
DARK = HexColor("#050d1a")
GRAY = HexColor("#667788")
WHITE = HexColor("#ffffff")


def generate_quote_pdf(quote) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.5 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("NXTitle", parent=styles["Title"], fontSize=20, textColor=NAVY, spaceAfter=6)
    heading_style = ParagraphStyle("NXHeading", parent=styles["Heading2"], fontSize=13, textColor=NAVY, spaceAfter=4, spaceBefore=12)
    normal_style = ParagraphStyle("NXNormal", parent=styles["Normal"], fontSize=10, textColor=DARK, leading=14)
    small_style = ParagraphStyle("NXSmall", parent=styles["Normal"], fontSize=8, textColor=GRAY, leading=10)

    elements = []

    elements.append(Paragraph("NEXUSPAY - PRICING QUOTE", title_style))
    elements.append(Paragraph("Veteran-Owned Merchant Services", small_style))
    elements.append(Spacer(1, 12))

    elements.append(Paragraph("Merchant Profile", heading_style))
    merchant_data = [
        ["Merchant", quote.merchant_name or "-"],
        ["Vertical", quote.vertical or "-"],
        ["Risk Level", quote.risk_level or "-"],
        ["Monthly Volume", f"${quote.volume:,.2f}"],
        ["Monthly Transactions", f"{quote.transactions:,}"],
        ["Avg Ticket", f"${quote.volume / max(quote.transactions, 1):,.2f}"],
    ]
    t = Table(merchant_data, colWidths=[2.2 * inch, 4.5 * inch])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), GRAY),
        ("TEXTCOLOR", (1, 0), (1, -1), DARK),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, GRAY),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 8))

    elements.append(Paragraph("Your Sell Price", heading_style))
    sell_data = [
        ["Markup Above IC", f"{quote.markup_pct:.2f}%"],
        ["Auth / Per-Item Fee", f"${quote.auth_sell:.2f}"],
        ["AVS Fee", f"${quote.avs_sell:.2f}"],
        ["Batch Fee", f"${quote.batch_sell:.2f}"],
        ["Monthly Platform Fee", f"${quote.monthly_sell:.2f}"],
        ["TransArmor Fee", f"${quote.transarmor_sell:.2f}"],
        ["PCI / Service Fee", f"${quote.pci_sell:.2f}"],
    ]
    if quote.has_amex:
        sell_data.append(["Amex OptBlue Volume", f"${quote.amex_volume:,.2f}"])
    if quote.use_gateway:
        sell_data.append(["Maverick Gateway", "Yes"])

    t2 = Table(sell_data, colWidths=[2.2 * inch, 4.5 * inch])
    t2.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), GRAY),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, GRAY),
    ]))
    elements.append(t2)
    elements.append(Spacer(1, 12))

    elements.append(Paragraph("Residual Comparison", heading_style))

    # Build comparison rows — always show Beacon + Maverick, conditionally show North + Kurv
    comp_data = [
        ["Program", "Gross Margin", "Split", "Your Residual"],
        ["Beacon Traditional", f"${quote.beacon_trad_margin:,.2f}", "75%", f"${quote.beacon_trad_residual:,.2f}"],
        ["Beacon Flex Sell", f"${quote.beacon_flex_margin:,.2f}", "50%", f"${quote.beacon_flex_residual:,.2f}"],
    ]

    # North — only show if residual exists (backward-compatible with old quotes)
    north_res = getattr(quote, "north_residual", 0) or 0
    north_mg = getattr(quote, "north_margin", 0) or 0
    if north_res != 0 or north_mg != 0:
        comp_data.append(["North", f"${north_mg:,.2f}", "70%", f"${north_res:,.2f}"])

    # Kurv / EMS — only show if residual exists
    kurv_res = getattr(quote, "kurv_residual", 0) or 0
    kurv_mg = getattr(quote, "kurv_margin", 0) or 0
    if kurv_res != 0 or kurv_mg != 0:
        kv_risk = getattr(quote, "risk_level", "low") or "low"
        kv_split_label = "50%" if kv_risk == "high" else "80%"
        comp_data.append([f"Kurv / EMS ({kv_risk})", f"${kurv_mg:,.2f}", kv_split_label, f"${kurv_res:,.2f}"])

    # Maverick — always shown
    comp_data.append([f"Maverick ({quote.maverick_risk or quote.risk_level})", f"${quote.maverick_tnr:,.2f}", "90/80/60%", f"${quote.maverick_residual:,.2f}"])

    t3 = Table(comp_data, colWidths=[1.8 * inch, 1.6 * inch, 1.2 * inch, 1.6 * inch])
    t3.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("GRID", (0, 0), (-1, -1), 0.5, GRAY),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ]))
    elements.append(t3)
    elements.append(Spacer(1, 12))

    elements.append(Paragraph(
        f"<b>Recommended Program:</b> {quote.best_program} - ${quote.best_residual:,.2f}/mo residual",
        normal_style,
    ))
    elements.append(Spacer(1, 8))

    if quote.notes:
        elements.append(Paragraph("Notes", heading_style))
        elements.append(Paragraph(quote.notes, normal_style))
        elements.append(Spacer(1, 8))

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    elements.append(Spacer(1, 20))
    elements.append(Paragraph(
        f"Generated {ts} by NexusPay Intelligence - Quote #{quote.id}<br/>"
        f"NexusPay, LLC - Veteran-Owned - nexuspayservices.com - (720) 689-7272",
        small_style,
    ))

    doc.build(elements)
    return buf.getvalue()


async def upload_quote_pdf(quote_id: int, pdf_bytes: bytes):
    if not r2_available():
        return None
    try:
        key = f"quotes/quote-{quote_id}.pdf"
        success = await upload_to_r2(key, pdf_bytes, "application/pdf")
        return key if success else None
    except Exception:
        return None
