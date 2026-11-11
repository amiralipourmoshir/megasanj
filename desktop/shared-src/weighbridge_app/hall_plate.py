"""Live Iranian plates drawn as native graphics, projected onto the banner bumper."""
import re

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QPixmap, QPolygonF, QTransform, QLinearGradient
from .typography import FONT_FAMILY


LETTERS={"الف","ب","پ","ت","ث","ج","د","س","ص","ط","ع","ق","ک","ل","م","ن","و","ه","هـ","ی"}
PERSIAN=str.maketrans("0123456789","۰۱۲۳۴۵۶۷۸۹")


def plate_parts(value):
    value=str(value or '').translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩يك','01234567890123456789یک'))
    value=re.sub('[\u200c\u200e\u200f\u202a-\u202e\u2066-\u2069]','',value).strip()
    match=re.fullmatch(r'(\d{2})\s*([^\d\s-]+)\s*(\d{3})\s*(?:ایران\s*)?-?\s*(\d{2})',value)
    if not match or match[2] not in LETTERS:return None
    return match.groups()


def paint_plate(p, value, dark=False):
    parts=plate_parts(value)
    if parts is None:return
    first,letter,middle,region=parts
    p.setRenderHint(QPainter.Antialiasing); p.setRenderHint(QPainter.TextAntialiasing)
    public=letter in ('ع','ت','ک')
    top='#E4BE51' if public else '#E6E7E1'
    bottom='#B9983A' if public else '#BFC4C5'
    if dark:top='#CBB15F' if public else '#B8BDC3'; bottom='#9B8549' if public else '#8E9AA8'
    surface=QLinearGradient(0,0,1040,220); surface.setColorAt(0,QColor(top)); surface.setColorAt(1,QColor(bottom))
    p.setPen(QPen(QColor('#353B40'),9)); p.setBrush(surface); p.drawRoundedRect(QRectF(5,5,1030,210),12,12)
    p.fillRect(QRectF(10,10,100,200),QColor('#133F80' if dark else '#1756A2'))
    p.fillRect(QRectF(25,28,68,14),QColor('#238B60')); p.fillRect(QRectF(25,42,68,14),QColor('#ECE8D6')); p.fillRect(QRectF(25,56,68,14),QColor('#BE3B3B'))
    def text(rect,value,size,color='#171D22'):
        font=QFont(FONT_FAMILY); font.setPixelSize(size); font.setWeight(QFont.Bold)
        bounds=QFontMetricsF(font).tightBoundingRect(value)
        ratio=min(1,rect.width()/max(1,bounds.width()),rect.height()/max(1,bounds.height()))
        if ratio<1:font.setPixelSize(max(8,int(size*ratio))); bounds=QFontMetricsF(font).tightBoundingRect(value)
        p.setFont(font); p.setPen(QColor(color)); p.drawText(QPointF(rect.center().x()-bounds.center().x(),rect.center().y()-bounds.center().y()),value)
    text(QRectF(17,96,83,34),'I.R.',30,'#EEEFE9')
    text(QRectF(15,139,87,37),'IRAN',28,'#EEEFE9')
    for rect,value in ((QRectF(135,40,205,142),first),(QRectF(355,35,160,152),letter),(QRectF(535,40,315,142),middle)):
        text(rect,value.translate(PERSIAN),140)
    p.setPen(QPen(QColor('#414447'),4)); p.drawLine(QPointF(868,12),QPointF(868,208))
    text(QRectF(883,20,135,46),'ایران',45)
    text(QRectF(882,83,140,114),region.translate(PERSIAN),109)
    # Restrained mounting hardware and a reflected highlight avoid a flat sticker.
    p.setPen(Qt.NoPen); p.setBrush(QColor('#677078'))
    for x in (128,850):p.drawEllipse(QPointF(x,24),5,5)
    sheen=QLinearGradient(0,0,0,220); sheen.setColorAt(0,QColor(255,255,255,25)); sheen.setColorAt(.45,QColor(255,255,255,0)); sheen.setColorAt(1,QColor(0,0,0,18)); p.fillRect(QRectF(10,10,1020,200),sheen)


def draw_plate_in_rect(painter,value,rect,dark=False):
    """Paint text at the final device resolution, without an intermediate bitmap."""
    if plate_parts(value) is None:return
    painter.save(); painter.translate(rect.left(),rect.top()); painter.scale(rect.width()/1040,rect.height()/220)
    paint_plate(painter,value,dark); painter.restore()


# Corners measured on the original 724 x 2172 source photographs (TL, TR, BR, BL).
BUMPER_QUADS={
    'light':((513.5,1357.5),(562,1357),(562,1369.5),(513.5,1371)),
    'dark':((515.5,1357.7),(564,1356.7),(564,1369),(515.5,1371)),
}


def draw_bumper_plate(painter,value,source,target,mode):
    """Use the same image crop as the photograph before applying perspective."""
    if plate_parts(value) is None:return
    corners=QPolygonF([QPointF(target.left()+(x-source.left())*target.width()/source.width(),
                             target.top()+(y-source.top())*target.height()/source.height()) for x,y in BUMPER_QUADS[mode]])
    original=QPolygonF([QPointF(0,0),QPointF(1040,0),QPointF(1040,220),QPointF(0,220)])
    transform=QTransform()
    if QTransform.quadToQuad(original,corners,transform):
        painter.save(); painter.setTransform(transform,True); paint_plate(painter,value,mode=='dark'); painter.restore()
        return corners.boundingRect().center()
