"""Consistent print typography, independent of the current screen theme."""
import re
import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextDocument
from PySide6.QtWidgets import QApplication


def verification_section(qr_uri):
    """Shared final-order QR block: physical left QR, right-aligned text."""
    cell = "border:0;padding:8px;background:#F1F7FC;vertical-align:middle;"
    return (
        "<p align='right' dir='rtl' style='font-size:10pt;color:#173B67;font-weight:500;margin:12px 0 5px'>استعلام و اصالت سند</p>"
        "<table class='reportVerification' width='100%' align='right' cellspacing='0'><tr>"
        f"<td width='18%' align='center' style='{cell}'><img width='52' height='52' src='{html.escape(qr_uri, quote=True)}'></td>"
        f"<td width='82%' align='right' style='{cell}font-size:9pt;color:#243650;'>"
        "این سند در سامانه مگاپالس قابل استعلام است.<br>"
        "<span style='font-size:7.5pt;color:#6B7F97;font-weight:400'>برای مشاهده اطلاعات مرجع، کد مقابل را اسکن کنید.</span>"
        "</td></tr></table>"
    )


def right_align_report_html(source):
    """Use attributes understood by Qt without reordering any table cells."""
    source = standardize_report_header(source)
    source = center_report_signatures(source)
    def align(match):
        tag, attrs = match[1], match[2]
        explicit = re.search(r"\balign\s*=|text-align\s*:", attrs, re.I)
        if not explicit:
            alignment = "center" if re.search(r"class=['\"](?:footer|sign)['\"]", attrs) else "right"
            attrs += f" align='{alignment}'"
        if tag.lower() in ("div", "p") and not re.search(r"\bdir\s*=", attrs):
            attrs += " dir='ltr'" if "class='number'" in attrs else " dir='rtl'"
        return f"<{tag}{attrs}>"
    return re.sub(r"<(div|p|td|th|table)\b([^>]*)>", align, source, flags=re.I)


def standardize_report_header(source):
    """Keep the logo in the physical rightmost cell of every Qt report."""
    def header(match):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", match[0], re.I | re.S)
        if len(cells) != 3:
            return match[0]
        logo = next((cell for cell in cells if "<img" in cell), None)
        title = next((cell for cell in cells if re.search(r"class=['\"]title['\"]", cell)), None)
        if logo is None or title is None:
            return match[0]
        metadata = next(cell for cell in cells if cell != logo and cell != title)
        # Align the two side contents toward the heading. Its center is the
        # midpoint of the free space between them, rather than the page center.
        def centered_block(item):
            attrs = re.sub(r"\s+align=['\"][^'\"]*['\"]", "", item[2], flags=re.I)
            attrs = re.sub(r"text-align\s*:[^;'\"]+", "text-align:center", attrs, flags=re.I)
            return f"<{item[1]}{attrs} align='center'>"
        title = re.sub(r"<(div|p)\b([^>]*)>", centered_block, title)
        return ("<table class='header' width='100%' align='right'><tr>"
                f"<td width='30%' align='right' style='border:0'>{metadata}</td>"
                f"<td width='57%' align='center' style='border:0;padding-left:10px;padding-right:10px'>{title}</td>"
                f"<td width='13%' align='left' style='border:0'>{logo}</td>"
                "</tr></table>")
    return re.sub(r"<table\b[^>]*class=['\"]header['\"][^>]*>.*?</table>", header, source, flags=re.I | re.S)


def center_report_signatures(source):
    def signatures(match):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", match[0], re.I | re.S)
        if len(cells) != 3:
            return match[0]
        return ("<table class='sign' width='100%' align='center' border='1' cellspacing='0' cellpadding='8' style='border:1px solid #CBD8E8;border-collapse:collapse'><tr>" +
                "".join(f"<td width='{width}%' align='center' style='border:1px solid #CBD8E8;text-align:center;vertical-align:top'>{cell}</td>"
                        for width, cell in zip((33, 34, 33), cells)) + "</tr></table>")
    return re.sub(r"<table\b[^>]*class=['\"]sign['\"][^>]*>.*?</table>", signatures, source, flags=re.I | re.S)


def report_document(source):
    source = re.sub(r"font-weight\s*:\s*(\d+)",
                    lambda match: "font-weight:" + str(min(600, int(match[1]))), source)
    source = re.sub(r"font-weight\s*:\s*bold\b", "font-weight:500", source)
    rules = "<style>b,strong,th{font-weight:500;}h1,h2,h3,h4{font-weight:600;}body{font-weight:400;text-align:right;}</style>"
    if "</head>" in source:
        source = source.replace("</head>", rules + "</head>", 1)
    else:
        source = source.replace("<body", "<head>" + rules + "</head><body", 1)
    document = QTextDocument()
    font = QFont(QApplication.font())
    font.setWeight(QFont.Normal)
    document.setDefaultFont(font)
    option = document.defaultTextOption()
    option.setTextDirection(Qt.RightToLeft)
    option.setAlignment(Qt.AlignRight)
    document.setDefaultTextOption(option)
    document.setHtml(source)
    return document
