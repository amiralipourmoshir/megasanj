"""Compact MegaSanj minutes: full shipment details and the shared official text."""
import html
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from .reports import right_align_report_html, verification_section

_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')


def minutes_html(document, certificate, logo_uri, qr_uri, issued_at):
    def get(key, default=''):
        return document[key] if key in document.keys() and document[key] not in (None, '') else default
    def esc(value):
        return html.escape(str(value if value not in (None, '') else '—'))
    def fa(value):
        return esc(value).translate(_DIGITS)
    def weight(key, missing='نامعلوم'):
        value = certificate.get(key)
        if value is None:value = (certificate.get('comparison') or {}).get(key)
        return missing if value is None else f"{Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP):,}".translate(_DIGITS) + ' کیلوگرم'
    def cells(values):
        return '<tr>' + ''.join(f"<td width='25%'><span class='caption'>{esc(label)}</span><br><b>{value}</b></td>" for label, value in reversed(values)) + '</tr>'

    statement = certificate.get('statement_html')
    if not statement or not certificate.get('verification_url'):
        raise ValueError('متن رسمی یا نشانی استعلام صورت‌جلسه دریافت نشده است؛ ابتدا اطلاعات سند را با سرور همگام کنید.')
    # Older cached server text may still contain three decimal places.
    def integer_text(match):
        raw = match[0].translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')).replace('٬', ',').replace('٫', '.')
        rounded = Decimal(raw.replace(',', '')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        return f"{rounded:,}".translate(_DIGITS)
    statement = re.sub(r'\d[\d,٬]*[.٫]\d+', integer_text, statement)
    company = certificate.get('company') or json.loads(get('company_snapshot', '{}')) or {}
    waybill = json.loads(get('waybill_snapshot', '{}')) or {}
    driver = certificate.get('driver_name') or get('driver_name')
    material = certificate.get('material_name') or get('material_name') or (waybill.get('material_type') or {}).get('name') or '—'
    number = certificate.get('waybill_number') or waybill.get('display_number') or get('waybill_number')
    origin = '، '.join(str(certificate.get(key) or get(key)) for key in ('origin_province', 'origin_city') if certificate.get(key) or get(key)) or '—'
    supplier = ' — '.join(str(company[key]) for key in ('code', 'name') if company.get(key)) or '—'
    if company.get('national_id'):supplier += ' · شناسه ملی: ' + str(company['national_id'])
    details = cells([
        ('نام و نام خانوادگی راننده', esc(driver)), ('شماره ملی راننده', fa(certificate.get('driver_national_id') or '......................')),
        ('شماره همراه', fa(certificate.get('driver_phone') or get('driver_phone'))), ('پلاک تریلی', fa(certificate.get('trailer_plate') or get('trailer_plate'))),
    ]) + cells([
        ('نوع کالا / شماره بارنامه', esc(material) + '<br>' + fa(number)), ('شماره سند باسکول', fa(get('document_number'))),
        ('شرکت تأمین‌کننده / فرستنده', fa(supplier)), ('مبدأ بار', esc(origin)),
    ])
    surplus = float(certificate.get('surplus_weight_kg') or 0) > 0
    penalty = weight('penalty_weight_kg') if (certificate.get('status') == 'closed' or certificate.get('penalty_weight_kg') or certificate.get('deduction_reason')) else '........................ کیلوگرم'
    weights = cells([
        ('وزن مورد انتظار در مبدأ', weight('origin_weight_kg')), ('وزن تحویلی در مقصد', weight('destination_weight_kg')),
        ('اضافه وزن ثبت‌شده' if surplus else 'کسری ثبت‌شده', weight('surplus_weight_kg' if surplus else 'shortage_weight_kg')),
        ('میزان جبران مقرر', penalty),
    ])
    title = 'صورت‌جلسه رسمی عدم تطابق وزن'.replace(' ', '&nbsp;')
    source = f"""<!doctype html><html dir='rtl'><head><meta charset='utf-8'><title>صورت‌جلسه رسمی عدم تطابق وزن</title><style>
    body{{font-family:'PeydaWeb(FaNum)';font-size:7.5pt;color:#172b46;margin:0}}
    table{{width:100%;border-collapse:collapse}}td{{padding:5px 7px;border:.65px solid #b9c9dc;vertical-align:middle}}
    .minutes-header td{{border:0;padding:2px 4px 8px}}
    .minutes-title{{font-size:12.5pt;color:#173f74;font-weight:600;white-space:nowrap;margin:0}}
    .sub{{font-size:7pt;color:#697d98;margin:3px 0}}.caption{{font-size:7pt;color:#7489a4}}
    .section{{font-size:9pt;color:#214d86;font-weight:600;background:#f2f6fc;padding:6px 8px;margin:10px 0 0}}
    .details b{{font-size:7.8pt}}.statement td{{font-size:8.3pt;line-height:135%;padding:10px 12px}}
    .sign td{{font-size:7.5pt;padding:9px;vertical-align:top}}.wdm-blank{{font-weight:600}}
    </style></head><body data-minutes-page>
    <!-- minutes:header --><table class='minutes-header' cellspacing='0'><tr><td width='24%'><span class='caption'>شماره سند</span><br><span style='font-size:7.5pt;white-space:nowrap'>{fa(certificate.get('certificate_number'))}</span><br><span class='caption'>تاریخ ثبت</span><br>{fa(issued_at)}</td><td width='62%' align='center'><p class='minutes-title' align='center'>{title}</p><p class='sub' align='center'>سامانه یکپارچه مگاسنج · شرکت مگافولاد</p></td><td width='14%' align='left'><img src='{logo_uri}' width='49' height='49'></td></tr></table><!-- /minutes:header -->
    <!-- minutes:details --><p class='section'>مشخصات راننده، بارنامه و وسیله حمل</p>
    <table class='details' cellspacing='0'>{details}</table><!-- /minutes:details -->
    <!-- minutes:weights --><p class='section'>خلاصه کنترل وزن</p>
    <table class='details' cellspacing='0'>{weights}</table><!-- /minutes:weights -->
    <!-- minutes:statement --><table class='statement' cellspacing='0'><tr><td align='right'>{statement}</td></tr></table><!-- /minutes:statement -->
    <!-- minutes:sign --><table class='sign' cellspacing='0'><tr><td width='50%' align='center'><b>نام و امضای مسئول ثبت سند</b><br>نام: ........................................<br>تاریخ: ........................<br>مهر و امضا:<br></td><td width='50%' align='center'><b>نام، امضا و اثر انگشت راننده</b><br>نام: {esc(driver)}<br>تاریخ: ........................<br>امضا و اثر انگشت:<br></td></tr></table><!-- /minutes:sign -->
    <!-- minutes:verification -->__REPORT_VERIFICATION__<!-- /minutes:verification -->
    </body></html>"""
    footer = verification_section(qr_uri).replace('font-size:10pt', 'font-size:7.5pt').replace('font-size:9pt', 'font-size:7pt').replace("width='52' height='52'", "width='44' height='44'").replace('padding:8px', 'padding:5px')
    return right_align_report_html(source.replace('__REPORT_VERIFICATION__', footer))


def layout_minutes_page(source, page_size):
    """Use explicit card geometry; QTextDocument does not implement border-radius."""
    from PySide6.QtGui import QTextOption
    from .reports import report_document
    head = re.search(r'<head>.*?</head>', source, re.S).group(0)
    overrides = "<style>td{border:0}.details td{border-left:.5px solid #d5dfeb;border-bottom:.5px solid #d5dfeb}.section{margin:0 0 6px;padding:5px}.statement td{padding:0}.sign td{border-left:.5px solid #d5dfeb}.minutes-header td{border:0}</style>"
    blocks = []
    # Reflowing at the narrow A5 width makes the paragraph much taller, then
    # fitting that height shrinks both axes and leaves wide side margins.
    # Lay out once at a stable print width and scale vector text to the paper.
    logical_width = 561.0
    paper_scale = page_size.width() / logical_width
    logical_height = page_size.height() / paper_scale
    inset, gap = 10.0, 10.0
    for name, fragment in re.findall(r'<!-- minutes:(\w+) -->(.*?)<!-- /minutes:\1 -->', source, re.S):
        if name in ('statement', 'sign'):
            fragment = re.sub(r'<td\b([^>]*)>', r"<td\1 style='border:0'>", fragment)
        card = name in ('statement', 'sign')
        padding = inset if card else 0.0
        document = report_document('<html>' + head.replace('</head>', overrides + '</head>') + '<body>' + fragment + '</body></html>')
        document.setUseDesignMetrics(True)
        option = document.defaultTextOption()
        option.setWrapMode(QTextOption.WordWrap)
        document.setDefaultTextOption(option)
        document.setDocumentMargin(0)
        document.setTextWidth(logical_width - 2 * padding)
        blocks.append({'name': name, 'document': document, 'padding': padding, 'height': document.size().height() + 2 * padding, 'card': card, 'page_width': logical_width})
    if len(blocks) != 6:
        raise ValueError('ساختار صفحه صورت‌جلسه کامل نیست.')
    natural = sum(b['height'] for b in blocks) + gap * (len(blocks) - 1)
    scale = paper_scale * min(1.0, logical_height / natural)
    extra = max(0.0, logical_height - natural)
    growth = {'details': .12, 'weights': .08, 'statement': .50, 'sign': .30}
    y = 0.0
    for block in blocks:
        block['height'] += extra * growth.get(block['name'], 0)
        block['y'] = y
        y += block['height'] + gap
    return blocks, scale


def paint_minutes_page(painter, source, page_size):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QPen, QPainter
    blocks, scale = layout_minutes_page(source, page_size)
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate((page_size.width() - blocks[0]['page_width'] * scale) / 2, 0)
    painter.scale(scale, scale)
    for block in blocks:
        if block['card']:
            painter.setPen(QPen(QColor('#bdcde0'), .7))
            painter.setBrush(QColor('#ffffff'))
            painter.drawRoundedRect(QRectF(.5, block['y'] + .5, block['page_width'] - 1, block['height'] - 1), 8, 8)
            if block['name'] == 'sign':
                from PySide6.QtCore import QLineF
                painter.drawLine(QLineF(block['page_width'] / 2, block['y'] + 10, block['page_width'] / 2, block['y'] + block['height'] - 10))
        painter.save()
        offset = block['padding']
        # Additional height is real writing/signature space, not stretched glyphs.
        painter.translate(offset, block['y'] + offset)
        document = block['document']
        document.drawContents(painter, QRectF(0, 0, document.size().width(), document.size().height()))
        painter.restore()
    painter.restore()
