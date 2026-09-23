"""
Chart of Accounts (COA) PDF Generator
====================================
Generates a comprehensive, publication-quality PDF directory of all 78 ledger
and group accounts defined in Sagar Accounts Software, complete with:
- Code Numbers
- Account Titles
- Major Type & Sub-Type Classifications
- Normal Balance Rules (Debit / Credit)
- Posting Status (Summary Group Header vs Postable Ledger)
- Accounting Content & Transaction Usage Descriptions
- System Tags and Tax Classifications
"""

import os
import sys
import datetime
import argparse
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
)
from reportlab.pdfgen import canvas

# Configure root path for backend database access
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from backend.db import get_db_connection


class NumberedCanvas(canvas.Canvas):
    """Two-pass canvas to dynamically compute and stamp total page count and header/footer."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748b"))

        # Running Header (pages > 1)
        if self._pageNumber > 1:
            self.drawString(32, 565, "SAGAR ACCOUNTS SOFTWARE  |  MASTER CHART OF ACCOUNTS (COA) DIRECTORY")
            self.drawRightString(842 - 32, 565, datetime.date.today().strftime("%d %B %Y"))
            self.setStrokeColor(colors.HexColor("#cbd5e1"))
            self.setLineWidth(0.5)
            self.line(32, 558, 842 - 32, 558)

        # Running Footer (all pages)
        self.setStrokeColor(colors.HexColor("#e2e8f0"))
        self.setLineWidth(0.5)
        self.line(32, 28, 842 - 32, 28)
        self.drawString(32, 17, "Official Accounting Directory & Reference | Sagar Accounts Software")
        page_str = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(842 - 32, 17, page_str)
        self.restoreState()


def fetch_all_accounts():
    """Fetches all accounts from accounts_chart ordered numerically by code."""
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id, code, name, major_type, sub_type, description, normal_balance,
               is_group, is_postable, parent_id, system_tag, tax_classification
        FROM accounts_chart
        ORDER BY CAST(code AS INTEGER) ASC, code ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def generate_coa_pdf(output_pdf_path="Chart_of_Accounts_Directory.pdf"):
    """Builds and compiles the complete multi-page PDF document."""
    accounts = fetch_all_accounts()
    
    doc = SimpleDocTemplate(
        output_pdf_path,
        pagesize=landscape(A4),
        leftMargin=32,
        rightMargin=32,
        topMargin=32,
        bottomMargin=32
    )

    styles = getSampleStyleSheet()

    # Typography and styling definitions
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        textColor=colors.HexColor('#0f172a'),
        spaceAfter=3
    )

    subtitle_style = ParagraphStyle(
        'DocSubTitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#475569'),
        spaceAfter=10
    )

    section_hdr_style = ParagraphStyle(
        'SectionHdr',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10.5,
        leading=13,
        textColor=colors.white,
    )

    th_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#1e293b')
    )

    cell_code = ParagraphStyle(
        'CellCode',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#0f172a')
    )

    cell_name_group = ParagraphStyle(
        'CellNameGroup',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#1e3a8a')
    )

    cell_name_ledger = ParagraphStyle(
        'CellNameLedger',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#1e293b')
    )

    cell_sub_type = ParagraphStyle(
        'CellSubType',
        parent=styles['Normal'],
        fontName='Helvetica-Oblique',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#475569')
    )

    cell_badge_dr = ParagraphStyle(
        'CellBadgeDr',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#166534')
    )

    cell_badge_cr = ParagraphStyle(
        'CellBadgeCr',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#991b1b')
    )

    cell_status_group = ParagraphStyle(
        'CellStatusGroup',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor('#4338ca')
    )

    cell_status_post = ParagraphStyle(
        'CellStatusPost',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor('#15803d')
    )

    cell_desc = ParagraphStyle(
        'CellDesc',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10.5,
        textColor=colors.HexColor('#334155')
    )

    stat_label = ParagraphStyle(
        'StatLabel',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor('#64748b')
    )

    stat_value = ParagraphStyle(
        'StatVal',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=14,
        textColor=colors.HexColor('#0f172a')
    )

    story = []

    # Title Banner Block
    story.append(Paragraph("SAGAR ACCOUNTS SOFTWARE - MASTER CHART OF ACCOUNTS (COA)", title_style))
    story.append(Paragraph(
        f"Official General Ledger Master Directory | All System Accounts, Codes, Types & Content Descriptions | Generated on {datetime.datetime.now().strftime('%d-%b-%Y %I:%M %p')}",
        subtitle_style
    ))

    # Metric KPI Overview Cards
    total_count = len(accounts)
    group_count = sum(1 for a in accounts if a['is_group'])
    postable_count = sum(1 for a in accounts if a['is_postable'])
    debit_count = sum(1 for a in accounts if a['normal_balance'] == 'Debit')
    credit_count = sum(1 for a in accounts if a['normal_balance'] == 'Credit')

    stat_data = [
        [
            Paragraph("Total Accounts", stat_label),
            Paragraph("Header Groups", stat_label),
            Paragraph("Postable Ledgers", stat_label),
            Paragraph("Debit Balance (Dr)", stat_label),
            Paragraph("Credit Balance (Cr)", stat_label),
            Paragraph("Currency & Rules", stat_label)
        ],
        [
            Paragraph(f"{total_count} Accounts", stat_value),
            Paragraph(f"{group_count} Groups", stat_value),
            Paragraph(f"{postable_count} Ledgers", stat_value),
            Paragraph(f"{debit_count} Dr", stat_value),
            Paragraph(f"{credit_count} Cr", stat_value),
            Paragraph("INR Standard", stat_value)
        ]
    ]
    stat_col_widths = [130, 130, 130, 130, 130, 128]  # Sum = 778 pt
    stat_table = Table(stat_data, colWidths=stat_col_widths)
    stat_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f1f5f9')),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#cbd5e1')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(stat_table)
    story.append(Spacer(1, 10))

    # Major Categories configuration with custom color branding
    category_order = [
        ('Asset', '1000 - 1999: ASSETS', '#1e3a8a'),
        ('Liability', '2000 - 2999: LIABILITIES & STATUTORY DUTIES', '#9a3412'),
        ('Equity', '3000 - 3999: EQUITY & OWNER CAPITAL', '#065f46'),
        ('Revenue', '4000 - 4999: REVENUE & OPERATING INCOME', '#0e7490'),
        ('Direct Expense', '5000 - 5999: DIRECT EXPENSES / COGS', '#701a75'),
        ('Operating Expense', '6000 - 6999: OPERATING OVERHEADS & EXPENSES', '#b45309'),
        ('Financial Cost', '7000 - 7999: FINANCIAL CHARGES & BANKING COSTS', '#475569'),
    ]

    col_widths = [55, 160, 110, 58, 85, 310]  # Total = 778 pt

    for major_type, category_title, banner_color in category_order:
        cat_accounts = [a for a in accounts if a['major_type'] == major_type]
        if not cat_accounts:
            continue

        # Section Header Banner
        header_table = Table(
            [[Paragraph(f"<b>{category_title.upper()}</b> &nbsp;&nbsp;({len(cat_accounts)} Accounts)", section_hdr_style)]],
            colWidths=[778]
        )
        header_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(banner_color)),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))

        # Table rows
        table_rows = [
            [
                Paragraph("Code", th_style),
                Paragraph("Account Name", th_style),
                Paragraph("Sub-Type", th_style),
                Paragraph("Balance", th_style),
                Paragraph("Posting Status", th_style),
                Paragraph("Description & Accounting Content", th_style),
            ]
        ]

        row_styles = [
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e2e8f0')),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 3),
            ('TOPPADDING', (0, 0), (-1, 0), 3),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]

        for idx, a in enumerate(cat_accounts, start=1):
            is_grp = bool(a['is_group'])
            norm_bal = a['normal_balance'] or 'Debit'

            name_p = Paragraph(f"<b>{a['name']}</b>" if is_grp else a['name'], cell_name_group if is_grp else cell_name_ledger)
            code_p = Paragraph(f"<b>[{a['code']}]</b>", cell_code)
            sub_p = Paragraph(a['sub_type'] or '-', cell_sub_type)
            bal_p = Paragraph(norm_bal, cell_badge_dr if norm_bal == 'Debit' else cell_badge_cr)
            
            status_text = "Summary Group" if is_grp else "Postable Ledger"
            status_p = Paragraph(status_text, cell_status_group if is_grp else cell_status_post)

            desc_text = a['description'] or ''
            if a.get('system_tag'):
                desc_text += f" <font color='#4f46e5'>[System: {a['system_tag']}]</font>"
            if a.get('tax_classification'):
                desc_text += f" <font color='#0284c7'>[Tax: {a['tax_classification']}]</font>"
            desc_p = Paragraph(desc_text, cell_desc)

            table_rows.append([code_p, name_p, sub_p, bal_p, status_p, desc_p])

            # Alternating row colors
            if is_grp:
                row_styles.append(('BACKGROUND', (0, idx), (-1, idx), colors.HexColor('#f8fafc')))
            elif idx % 2 == 0:
                row_styles.append(('BACKGROUND', (0, idx), (-1, idx), colors.HexColor('#ffffff')))
            else:
                row_styles.append(('BACKGROUND', (0, idx), (-1, idx), colors.HexColor('#fbfcfd')))

            row_styles.append(('TOPPADDING', (0, idx), (-1, idx), 2.5))
            row_styles.append(('BOTTOMPADDING', (0, idx), (-1, idx), 2.5))

        body_table = Table(table_rows, colWidths=col_widths, repeatRows=1)
        body_table.setStyle(TableStyle(row_styles))

        story.append(header_table)
        story.append(body_table)
        story.append(Spacer(1, 10))

    # Concluding audit note
    concluding_style = ParagraphStyle(
        'ConcludingNote',
        parent=styles['Normal'],
        fontName='Helvetica-Oblique',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#64748b'),
        alignment=1
    )
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "*** End of Chart of Accounts Directory -- Total 78 Accounts (12 Summary Groups, 66 Active Postable Ledgers) ***",
        concluding_style
    ))

    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"[+] Successfully generated: {output_pdf_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Chart of Accounts PDF Directory")
    parser.add_argument(
        "--output",
        "-o",
        default="Chart_of_Accounts_Directory.pdf",
        help="Target output PDF file path (default: Chart_of_Accounts_Directory.pdf)"
    )
    args = parser.parse_args()

    # Generate primary target
    generate_coa_pdf(args.output)
    
    # Also update accounts.pdf if running default
    if args.output == "Chart_of_Accounts_Directory.pdf":
        try:
            import shutil
            shutil.copyfile("Chart_of_Accounts_Directory.pdf", "accounts.pdf")
            print("[+] Also updated accounts.pdf with the latest complete Chart of Accounts.")
        except Exception as e:
            print(f"[-] Note on accounts.pdf update: {e}")
