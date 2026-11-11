from __future__ import annotations

import html
from concurrent.futures import ThreadPoolExecutor

_quality_search_pool = ThreadPoolExecutor(max_workers=2)
import base64
import json
import re
import uuid
from functools import wraps
from datetime import datetime, timedelta, timezone
from time import monotonic
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import qrcode
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from PySide6.QtCore import QDate, QMarginsF, QPoint, QRectF, QRegularExpression, QSize, QSizeF, QThread, QTimer, QTime, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPageLayout, QPageSize, QPainter, QPalette, QPixmap, QRegularExpressionValidator, QTextDocument
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QToolButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QTimeEdit, QTextEdit, QVBoxLayout, QWidget, QCalendarWidget,
)

from .constants import MOVEMENTS, SYNC_LABELS
from .discrepancy import weight_discrepancy
from .discrepancy_dialog import WeightDiscrepancyDialog
from .discrepancy_report import minutes_html, paint_minutes_page
from .theme import set_widget_style, ThemeToggle, current_theme, set_theme, theme_color, set_navigation_icon
from .design import app_style, POPUP_STYLE, SCROLLBAR_STYLE, set_tone
from .document_view import DocumentViewDialog
from .print_dialog import print_documents
from .account_view import AccountDialog
from .truck_queue import QueuePage
from .dates import gregorian_to_jalali
from .time_picker import TimePicker
from .database import Database
from .session_store import forget_session
from .reports import report_document, right_align_report_html, verification_section
from .avatars import circular_avatar
from .login import LoginDialog, SessionCheckWorker, LogoutWorker
from .services import fetch_scraps, fetch_companies, test_endpoint, fetch_available_waybills, fetch_quality_document, search_quality_documents, normalize_quality_code, reserve_waybill, release_waybill, send_webhook
from .sync import SyncWorker, ServerCacheSyncWorker, ReferenceDataWorker
from .domain import CompositionInput, DomainError, calculate_load, calculate_weighed_load
from .serial_scale import SerialScale, Teta10AParser
from . import serial_scale as serial_backend
from .locations import load_iran_locations


BLUE = "#1769e0"
PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
LATIN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def requires_desktop_access(capability):
    def decorate(method):
        @wraps(method)
        def guarded(self,*args,**kwargs):
            if not self.db.allows(capability):return
            return method(self,*args,**kwargs)
        return guarded
    return decorate


def fa_digits(value) -> str:
    return str(value).translate(PERSIAN_DIGITS)



def jalali_date(iso_value: str) -> str:
    try:
        value=datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
        jy,jm,jd=gregorian_to_jalali(value.year,value.month,value.day)
        return fa_digits(f"{jy:04d}/{jm:02d}/{jd:02d}")
    except (TypeError, ValueError):
        return "—"



def iran_jalali_datetime(value: str) -> str:
    try:
        stamp=datetime.fromisoformat(value.replace("Z", "+00:00"))
        iran=timezone(timedelta(hours=3, minutes=30))
        local=stamp.replace(tzinfo=iran) if stamp.tzinfo is None else stamp.astimezone(iran)
        jy,jm,jd=gregorian_to_jalali(local.year,local.month,local.day)
        return fa_digits(f"{jy:04d}/{jm:02d}/{jd:02d} · {local:%H:%M:%S}")
    except (AttributeError,TypeError,ValueError):
        return "—"



def entry_in_today_range(registered_at, entry_at, today, start_minute, end_minute):
    iran=timezone(timedelta(hours=3,minutes=30))
    try:
        dates=[]
        for value in (registered_at,entry_at):
            stamp=datetime.fromisoformat(value.replace("Z", "+00:00"))
            dates.append(stamp.replace(tzinfo=iran) if stamp.tzinfo is None else stamp.astimezone(iran))
        registered,entry=dates
        minute=entry.hour*60+entry.minute
        return registered.date()==today and entry.date()==today and start_minute<=minute<=end_minute
    except (AttributeError,TypeError,ValueError):
        return False


def parse_jalali_date(value: str) -> tuple[int, int, int] | None:
    match=re.fullmatch(r"\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s*",(value or "").translate(LATIN_DIGITS))
    if not match:return None
    result=tuple(map(int,match.groups()))
    return result if 1 <= result[1] <= 12 and 1 <= result[2] <= 31 else None

def claim_sidebar_scale(dialog):
    parent=dialog.parentWidget()
    while parent is not None:
        if hasattr(parent,"claim_sidebar_scale"):
            parent.claim_sidebar_scale(dialog)
            break
        parent=parent.parentWidget()


class StyledComboBox(QComboBox):
    """Combo box with a fully Qt-drawn popup, independent from the OS theme."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLayoutDirection(Qt.RightToLeft)
        view = QListView(self)
        view.setLayoutDirection(Qt.RightToLeft)
        view.setObjectName("comboPopupList")
        view.setFrameShape(QFrame.NoFrame)
        view.setSpacing(2)
        set_widget_style(view, POPUP_STYLE)
        view.setAutoFillBackground(True)
        view.viewport().setAutoFillBackground(True)
        set_widget_style(view.verticalScrollBar(), SCROLLBAR_STYLE)
        self.setView(view)

    def showPopup(self):
        super().showPopup()
        popup = self.view().window()
        popup.setAttribute(Qt.WA_StyledBackground, True)
        popup.setAutoFillBackground(True)
        set_widget_style(popup, POPUP_STYLE)
        popup.setPalette(self.view().palette())


class SearchableComboBox(StyledComboBox):
    """RTL searchable picker that only accepts an item from its data model."""

    def __init__(self, placeholder: str, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setCurrentIndex(-1)
        # Use the same physically-RTL editor as the waybill search. Native Qt
        # placeholders are mirrored inconsistently by some Linux themes.
        self.setLineEdit(RtlPlaceholderLineEdit(placeholder, self))
        self.lineEdit().setAlignment(Qt.AlignRight)
        completer = QCompleter(self.model(), self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.popup().setLayoutDirection(Qt.RightToLeft)
        set_widget_style(completer.popup(), POPUP_STYLE)
        self.setCompleter(completer)
        self.lineEdit().textEdited.connect(lambda _text: completer.complete())
        self.lineEdit().editingFinished.connect(self._accept_known_value)

    def _accept_known_value(self):
        text = self.currentText().strip()
        index = self.findText(text, Qt.MatchFixedString)
        if index >= 0:
            self.setCurrentIndex(index)
        else:
            self.setCurrentIndex(-1)
            self.lineEdit().clear()

    def select_text(self, text: str) -> bool:
        index = self.findText((text or "").strip(), Qt.MatchFixedString)
        self.setCurrentIndex(index)
        return index >= 0


class RtlPlaceholderLineEdit(QLineEdit):
    """Line edit whose empty-state hint is physically painted from the right."""
    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent); self._rtl_placeholder=placeholder
        self.setLayoutDirection(Qt.RightToLeft); self.setAlignment(Qt.AlignRight|Qt.AlignVCenter)

    def setRtlPlaceholderText(self, text: str) -> None:
        self._rtl_placeholder=text; self.update()

    def setPlaceholderText(self, text: str) -> None:
        # Keep dynamic hints (for example province -> city) on the custom
        # painter path as well; do not hand them back to the native renderer.
        self.setRtlPlaceholderText(text)

    def placeholderText(self) -> str:
        return self._rtl_placeholder

    def paintEvent(self,event):
        super().paintEvent(event)
        if self.text() or not self._rtl_placeholder or self.hasSelectedText():return
        painter=QPainter(self); painter.setPen(QColor(theme_color("#94a3b8")))
        # Qt may mirror AlignRight again for an RTL line edit.  Position the
        # hint with physical coordinates so it always hugs the visible right
        # edge, regardless of the desktop locale or platform theme.
        metrics=painter.fontMetrics(); hint_width=metrics.horizontalAdvance(self._rtl_placeholder)
        x=max(8,self.contentsRect().right()-8-hint_width)
        y=self.contentsRect().center().y()+(metrics.ascent()-metrics.descent())//2
        painter.drawText(x,y,self._rtl_placeholder)


class AppSearchLineEdit(RtlPlaceholderLineEdit):
    """Shared minimal search field used by every free-text search in the app."""

    def __init__(self, placeholder: str, parent=None):
        super().__init__(placeholder, parent)
        self.setObjectName("appSearchInput")
        self.setClearButtonEnabled(True)
        self.setMinimumHeight(46)
        set_widget_style(self, '\n            QLineEdit#appSearchInput {\n                background:#081426;\n                color:#edf7ff;\n                border:1px solid #263B56;\n                border-radius:8px;\n                padding:7px 14px 7px 38px;\n                selection-background-color:#dbeafe;\n                selection-color:#12396b;\n            }\n            QLineEdit#appSearchInput:hover { border-color:#466791; background:#0D1B30; }\n            QLineEdit#appSearchInput:focus { border:2px solid #3987FF; padding:6px 13px 6px 37px; }\n        ')


class SidebarNavButton(QPushButton):
    """Navigation button whose Persian label and icon stay pinned to the right."""

    def __init__(self, title: str, icon: Path, parent=None):
        super().__init__(parent)
        self.setObjectName("nav")
        self.setCheckable(True)
        self.setText("")
        self.setFixedHeight(48)
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 0, 18, 0)
        row.setSpacing(12)
        row.setDirection(QHBoxLayout.LeftToRight)
        image = QLabel(objectName="navIcon")
        image.setAttribute(Qt.WA_TransparentForMouseEvents)
        image.setFixedSize(25, 25)
        set_navigation_icon(image, icon)
        text = QLabel(title, objectName="navLabel")
        text.setAttribute(Qt.WA_TransparentForMouseEvents)
        text.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        text.setMinimumWidth(110); set_widget_style(text, 'color:#F3F7FF;background:transparent;font-size:13px;font-weight:600;')
        row.addStretch(1)
        row.addWidget(image)
        row.addWidget(text)


class IranTrailerPlate(QFrame):
    """Visual Iranian trailer plate editor: 2 digits, letter, 3 digits, region."""

    LETTERS = ("الف", "ب", "پ", "ت", "ث", "ج", "د", "س", "ص", "ط", "ع", "ق", "ل", "م", "ن", "و", "هـ", "ی")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("iranPlate")
        self.setLayoutDirection(Qt.LeftToRight)
        # Every child width adds up to this exact value.  Keeping the frame
        # exact prevents a theme/layout engine from leaving a strip between
        # the last plate cell and the rounded outer frame.
        self.setFixedWidth(560)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout = QHBoxLayout(self)
        layout.setDirection(QHBoxLayout.LeftToRight)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        country = QFrame(objectName="plateCountry")
        country.setFixedWidth(60)
        country_layout = QVBoxLayout(country)
        country_layout.setContentsMargins(8, 7, 8, 7)
        country_layout.setSpacing(3)
        flag = QLabel(objectName="plateFlag")
        flag.setPixmap(QPixmap(str(Path(__file__).resolve().parent / "assets" / "icons" / "iran-flag.svg")).scaled(34, 18, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        flag.setAlignment(Qt.AlignCenter)
        country_text = QLabel("I.R.\nIRAN", objectName="plateCountryText")
        country_text.setAlignment(Qt.AlignCenter)
        set_widget_style(country_text, "color:rgb(255,255,255);font-size:9px;font-weight:700;")
        country_layout.addWidget(flag)
        country_layout.addWidget(country_text)
        layout.addWidget(country)

        self.first = self._digits(2, "۱۲")
        self.first.setFixedWidth(112)
        self.first.setLayoutDirection(Qt.LeftToRight)
        self.letter = StyledComboBox()
        self.letter.setLayoutDirection(Qt.RightToLeft)
        self.letter.setObjectName("plateLetter")
        self.letter.setFixedWidth(100)
        self.letter.setPlaceholderText("حرف")
        for letter in self.LETTERS:
            self.letter.addItem(letter, letter)
        self.letter.setCurrentIndex(-1)
        self.middle = self._digits(3, "۳۴۵")
        self.middle.setFixedWidth(176)
        self.middle.setLayoutDirection(Qt.LeftToRight)
        self.region = self._digits(2, "۱۱")
        self.region.setLayoutDirection(Qt.LeftToRight)
        self.region.setObjectName("plateRegion")

        region_box = QFrame(objectName="plateRegionBox")
        region_box.setFixedWidth(112)
        region_layout = QGridLayout(region_box)
        region_layout.setContentsMargins(0, 0, 0, 0)
        region_layout.setSpacing(0)
        iran_label = QLabel("ایران", objectName="plateIran")
        iran_label.setAlignment(Qt.AlignHCenter | Qt.AlignTop | Qt.AlignAbsolute)
        # Both widgets share the same cell: the small country caption stays at
        # the top while the region number keeps the exact baseline of the
        # other plate digits.
        region_layout.addWidget(self.region, 0, 0)
        region_layout.addWidget(iran_label, 0, 0, Qt.AlignTop | Qt.AlignHCenter)

        layout.addWidget(self.first)
        layout.addWidget(self.letter)
        layout.addWidget(self.middle)
        layout.addWidget(region_box)

    @staticmethod
    def _digits(length: int, placeholder: str) -> QLineEdit:
        field = QLineEdit()
        field.setObjectName("plateDigits")
        field.setMaxLength(length)
        field.setValidator(QRegularExpressionValidator(QRegularExpression(f"[0-9۰-۹]{{0,{length}}}"), field))
        field.setPlaceholderText(placeholder)
        field.setAlignment(Qt.AlignCenter)
        field.setMinimumWidth(92 if length == 2 else 138)
        field.setMaximumWidth(16777215)
        field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        def normalize(text, target=field):
            converted=fa_digits(text)
            if converted != text:
                cursor=target.cursorPosition()
                target.blockSignals(True); target.setText(converted); target.setCursorPosition(cursor); target.blockSignals(False)
        field.textChanged.connect(normalize)
        return field

    def value(self) -> str:
        if not self.is_complete():
            return ""
        return f"{self.first.text()} {self.letter.currentText()} {self.middle.text()} ایران {self.region.text()}"

    def is_complete(self) -> bool:
        return len(self.first.text()) == 2 and self.letter.currentData() is not None and len(self.middle.text()) == 3 and len(self.region.text()) == 2

    def set_value(self, value: str) -> None:
        match = re.search(r"(\d{2})\s*([^\d\s]+)\s*(\d{3})(?:\s*ایران\s*)?(\d{2})", value or "")
        if not match:
            return
        first, letter, middle, region = match.groups()
        self.first.setText(first)
        index = self.letter.findText(letter)
        if index >= 0:
            self.letter.setCurrentIndex(index)
        self.middle.setText(middle)
        self.region.setText(region)


def style_table_scrollbars(table: QTableWidget) -> None:
    set_widget_style(table.verticalScrollBar(), SCROLLBAR_STYLE)
    set_widget_style(table.horizontalScrollBar(), SCROLLBAR_STYLE)
    set_widget_style(table.viewport(), ""); table.setShowGrid(False); table.setAlternatingRowColors(True); table.verticalHeader().setDefaultSectionSize(96)


class FullWidthTable(QTableWidget):
    """Share spare viewport width among data columns without shrinking controls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fill_bases = {}
        self._fill_pending = False
        self.horizontalHeader().sectionResized.connect(self._schedule_fill)

    def configure_fill(self, columns):
        self._fill_bases = {column: self.columnWidth(column) for column in columns}
        self.fill_available_width()

    def fill_available_width(self):
        if not self._fill_bases:
            return
        fixed = sum(self.columnWidth(column) for column in range(self.columnCount())
                    if column not in self._fill_bases and not self.isColumnHidden(column))
        bases = {column: width for column, width in self._fill_bases.items()
                 if not self.isColumnHidden(column)}
        total = sum(bases.values())
        if not total:
            return
        spare = max(0, self.viewport().width() - fixed - total)
        remaining = spare
        for index, (column, width) in enumerate(bases.items()):
            extra = remaining if index == len(bases) - 1 else spare * width // total
            remaining -= extra
            self.setColumnWidth(column, width + extra)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_fill()

    def _schedule_fill(self, *_args):
        if not self._fill_pending:
            self._fill_pending = True
            QTimer.singleShot(0, self._finish_fill)

    def _finish_fill(self):
        from shiboken6 import isValid
        if not isValid(self):return
        self._fill_pending = False
        self.fill_available_width()


def fit_table_controls(table, columns):
    """Reserve the actual styled control widths before Qt sizes each column."""
    for column in columns:
        for row in range(table.rowCount()):
            host=table.cellWidget(row,column)
            if host is None: continue
            host.ensurePolished()
            controls=host.findChildren(QPushButton)+host.findChildren(QLabel)
            if isinstance(host,(QPushButton,QLabel)): controls.append(host)
            for control in controls:
                control.ensurePolished()
                text_width=control.fontMetrics().horizontalAdvance(control.text())
                control.setMinimumWidth(text_width+(52 if isinstance(control,QPushButton) else 24))
                if isinstance(control,QLabel): control.setMaximumHeight(34)
            if host.layout():
                host.layout().activate()
                host.setMinimumWidth(host.layout().sizeHint().width())
        table.resizeColumnToContents(column)
    if isinstance(table, FullWidthTable):
        table.fill_available_width()


def reverse_document_table_columns(output: str) -> str:
    """Mirror cells in each flat report row, preserving row order and cell widths."""
    def reverse_row(match):
        cells = re.findall(r"<(?:td|th)\b[^>]*>.*?</(?:td|th)>", match.group(2), re.DOTALL)
        return match.group(1) + "".join(reversed(cells)) + match.group(3)
    return re.sub(r"(<tr\b[^>]*>)(.*?)(</tr>)", reverse_row, output, flags=re.DOTALL)


def message(parent, title: str, text: str, error=False):
    (QMessageBox.critical if error else QMessageBox.information)(parent, title, text)


class DocumentDialog(QDialog):
    def __init__(self, db: Database, parent=None, existing=None, document_id=None, created_for_dialog=False):
        super().__init__(parent)
        self.db = db
        self.document_id = document_id or (existing["id"] if existing else None)
        self.first_stage_published=bool(self.document_id and (self.db.first_weighing_queued(self.document_id) or (existing and existing["status"]=="first_weighed" and existing["cache_source"]=="server")))
        self.created_for_dialog = created_for_dialog
        self.current_step = 0
        self.finalizing = False
        self.calculation = None
        self.latest = None
        self.locked_readings = {}
        self.scale_connected = False
        self.scale_message = ""
        self.captures = {}
        self.setWindowTitle("ثبت سند جدید")
        self.setMinimumSize(900, 700)
        self.resize(980, 760)
        self.setLayoutDirection(Qt.RightToLeft)
        self.setObjectName("documentDialog")
        set_widget_style(self, app_style())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)
        hero=QFrame(objectName="documentHero"); hero_row=QHBoxLayout(hero); hero_row.setContentsMargins(22,18,22,18); hero_row.setSpacing(16)
        hero_mark=QLabel(objectName="documentHeroMark"); hero_mark.setAlignment(Qt.AlignCenter); hero_mark.setFixedSize(62,62)
        set_navigation_icon(hero_mark, Path(__file__).resolve().parent/"assets"/"icons"/"documents.svg", 30)
        hero_text=QVBoxLayout(); title=QLabel("ویزارد ثبت سند باسکول",objectName="documentHeroTitle"); title.setAlignment(Qt.AlignRight|Qt.AlignAbsolute)
        self.progress_label=QLabel("مرحله ۱ از ۳ · اطلاعات پایه",objectName="documentHeroText"); self.progress_label.setAlignment(Qt.AlignRight|Qt.AlignAbsolute)
        hero_text.addWidget(title); hero_text.addWidget(self.progress_label); hero_row.addWidget(hero_mark); hero_row.addLayout(hero_text,1); layout.addWidget(hero)

        header = QFrame(objectName="wizardHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 10, 12, 10)
        header_layout.setSpacing(10)
        self.step_labels = []
        for text in ("۱  اطلاعات پایه", "۲  وزن پر تریلی", "۳  وزن خالی و ثبت سند"):
            label = QLabel(text, objectName="wizardStep")
            label.setAlignment(Qt.AlignCenter)
            self.step_labels.append(label)
            header_layout.addWidget(label, 1)
        layout.addWidget(header)

        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)

        basic, basic_body, basic_layout = self._page("اطلاعات تریلی، مسیر و طرف معامله", "مرحله اول از سه مرحله؛ اطلاعات پایه متناسب با نوع گردش را تکمیل کنید.")
        self.basic_form = QFormLayout()
        self.basic_form.setLabelAlignment(Qt.AlignRight | Qt.AlignAbsolute | Qt.AlignVCenter)
        self.basic_form.setFormAlignment(Qt.AlignTop)
        self.basic_form.setHorizontalSpacing(22)
        self.basic_form.setVerticalSpacing(14)
        self.material_type=StyledComboBox()
        for material in db.material_types():self.material_type.addItem(material['name'],material)
        self.movement = StyledComboBox()
        for key, label in MOVEMENTS.items(): self.movement.addItem(label, key)
        self.basic_form.addRow("نوع کالا",self.material_type)
        self.material_display=QLabel(); self.basic_form.addRow("کالای بارنامه",self.material_display)
        self.company=StyledComboBox(); self.company.addItem("انتخاب شرکت تأمین‌کننده…",None)
        for x in db.companies(): self.company.addItem(f"{x['code']} — {x['name']}" if x['code'] else x['name'],dict(x))
        self.driver_name=QLineEdit(); self.driver_phone=QLineEdit(); self.trailer_plate=IranTrailerPlate(); self.selected_waybill=None; self.waybill_reservation_token=""
        provinces, self.cities_by_province = load_iran_locations()
        self.province=SearchableComboBox("جستجو و انتخاب استان…")
        for province in provinces: self.province.addItem(province["name"], int(province["id"]))
        self.province.setCurrentIndex(-1)
        self.city=SearchableComboBox("ابتدا استان را انتخاب کنید…"); self.city.setEnabled(False)
        self.province.currentIndexChanged.connect(self.province_changed)
        self.destination=QLineEdit(); self.waybill=RtlPlaceholderLineEdit("بخشی از شماره بارنامه را وارد کنید…"); self.waybill.setObjectName("waybillSearchInput"); self.waybill.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9۰-۹]*"),self.waybill))
        self.waybill_search_box=QFrame(objectName="waybillSearchControl"); self.waybill_search_box.setLayoutDirection(Qt.LeftToRight); waybill_search_layout=QHBoxLayout(self.waybill_search_box); waybill_search_layout.setDirection(QHBoxLayout.LeftToRight); waybill_search_layout.setContentsMargins(0,0,12,0); waybill_search_layout.setSpacing(0)
        self.waybill_search_state=QLabel("",objectName="waybillSearchState"); self.waybill_search_state.setFixedWidth(110); self.waybill_search_state.setAlignment(Qt.AlignCenter)
        self.waybill_dropdown_button=QToolButton(objectName="waybillDropdown"); self.waybill_dropdown_button.setFixedSize(46,46); self.waybill_dropdown_button.setIcon(QIcon(str(Path(__file__).resolve().parent/"assets"/"icons"/"chevron-down.svg"))); self.waybill_dropdown_button.setIconSize(QSize(17,17)); self.waybill_dropdown_button.setToolTip("نمایش نتایج"); self.waybill_dropdown_button.clicked.connect(self.toggle_waybill_results)
        # Left cluster: arrow + state. The editable area then owns the entire
        # right side, so both typed text and its caret begin at the right edge.
        waybill_search_layout.addWidget(self.waybill_dropdown_button); waybill_search_layout.addWidget(self.waybill_search_state); waybill_search_layout.addWidget(self.waybill,1)
        set_widget_style(self.waybill_search_box, "QFrame#waybillSearchControl { background:#081426; border:1px solid #263B56; border-radius:8px; } QLineEdit#waybillSearchInput { color:#F3F7FF; background:transparent; border:0; padding:8px; } QToolButton#waybillDropdown { background:#132842; border:0; } QLabel#waybillSearchState { color:#9CAFC8; }")
        self.waybill_popup=QFrame(self,objectName="waybillPopupPanel"); self.waybill_popup.setAttribute(Qt.WA_StyledBackground,True); self.waybill_popup.setLayoutDirection(Qt.RightToLeft); set_widget_style(self.waybill_popup, 'QFrame#waybillPopupPanel{background:#0D1B30;border:1px solid #263B56;border-radius:13px;}')
        popup_layout=QVBoxLayout(self.waybill_popup); popup_layout.setContentsMargins(5,5,5,5); popup_layout.setSpacing(0)
        self.waybill_results=QListWidget(self.waybill_popup); self.waybill_results.setObjectName("waybillPopup"); self.waybill_results.setAttribute(Qt.WA_StyledBackground,True); self.waybill_results.setLayoutDirection(Qt.RightToLeft); self.waybill_results.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.waybill_results.setAlternatingRowColors(False); set_widget_style(self.waybill_results, POPUP_STYLE + SCROLLBAR_STYLE + "QListWidget#waybillPopup { background:#0D1B30; color:#F3F7FF; border:0; padding:0; } QListWidget#waybillPopup::item { padding:0; } QListWidget#waybillPopup::item:selected { background:#183763; } QListWidget#waybillPopup::item:hover { background:#132842; }"); popup_layout.addWidget(self.waybill_results); self.waybill_popup.hide(); self.waybill_results.itemClicked.connect(self.select_waybill_result)
        self.waybill_debounce=QTimer(self); self.waybill_debounce.setSingleShot(True); self.waybill_debounce.setInterval(450); self.waybill_debounce.timeout.connect(self.search_waybill)
        self.waybill.textEdited.connect(self.clear_waybill_result)
        self.waybill_info=QLabel("شماره بارنامه را وارد کنید تا جستجو به‌صورت خودکار انجام شود."); self.waybill_info.setLayoutDirection(Qt.RightToLeft); self.waybill_info.setAlignment(Qt.AlignRight|Qt.AlignAbsolute|Qt.AlignVCenter); self.waybill_info.setWordWrap(True); set_widget_style(self.waybill_info, 'color:#9CAFC8;background:#132842;padding:12px;border-radius:10px')
        self.notes=QTextEdit(); self.notes.setMaximumHeight(90)
        self.base_inputs = (self.driver_name,self.driver_phone,self.province,self.city,self.destination,self.waybill)
        for widget in (self.driver_name,self.driver_phone,self.destination):
            widget.setLayoutDirection(Qt.RightToLeft); widget.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        self.notes.setLayoutDirection(Qt.RightToLeft)
        for label, widget in (("نوع گردش",self.movement),("شماره بارنامه",self.waybill_search_box),("اطلاعات بارنامه",self.waybill_info),("شرکت تأمین‌کننده",self.company),("نام راننده",self.driver_name),("شماره تماس",self.driver_phone),("پلاک تریلی ایران",self.trailer_plate),("استان مبدأ",self.province),("شهر مبدأ",self.city),("مقصد / خط تولید",self.destination),("توضیحات",self.notes)):
            self.basic_form.addRow(label, widget)
        self.source_warehouse=SearchableComboBox("جستجوی انبار مبدأ…")
        self.destination_factory=SearchableComboBox("جستجوی کارخانه مقصد…")
        self.explicit_destination=SearchableComboBox("انتخاب انبار دیگر همان کارخانه…")
        self.basic_form.addRow("مقصد صریح در همان کارخانه",self.explicit_destination)
        self.explicit_destination.currentIndexChanged.connect(self.explicit_destination_changed)
        self.source_warehouse.currentIndexChanged.connect(self.resolve_destination_warehouse)
        self.destination_warehouse_label=QLabel("کارخانه مقصد را انتخاب کنید.")
        self.destination_warehouse_label.setWordWrap(True)
        self._resolved_destination=None
        self.basic_form.addRow("انبار مبدأ",self.source_warehouse)
        self.basic_form.addRow("کارخانه مقصد",self.destination_factory)
        self.basic_form.addRow("انبار مقصد نهایی",self.destination_warehouse_label)
        self.destination_factory.currentIndexChanged.connect(self.resolve_destination_warehouse)
        self._warehouse_type_id=None
        basic_layout.addLayout(self.basic_form)
        basic_layout.addStretch()
        self.pages.addWidget(basic)

        weighing, weighing_body, wl = self._page("ثبت وزن پر تریلی", "در این مرحله فقط وزن پر از باسکول دریافت یا در حالت اضطراری دستی ثبت می‌شود.")
        self.scale_status=QLabel("● در حال اتصال به باسکول…"); set_widget_style(self.scale_status, 'font-size:15px;color:#FFB454;background:#132842;padding:12px;border-radius:10px')
        self.live_weight=QLabel("۰ kg"); self.live_weight.setAlignment(Qt.AlignCenter); set_widget_style(self.live_weight, 'font-size:42px;font-weight:800;color:#62B8FF;background:#132842;padding:20px;border-radius:14px')
        self.raw=QLabel("داده خام: —"); self.raw.setWordWrap(True); self.raw.setAlignment(Qt.AlignRight)
        wl.addWidget(self.scale_status); wl.addWidget(self.live_weight); wl.addWidget(self.raw)
        wf=QFormLayout(); self.first_weighing_form=wf; wf.setLabelAlignment(Qt.AlignRight | Qt.AlignAbsolute | Qt.AlignVCenter); wf.setHorizontalSpacing(22); wf.setVerticalSpacing(14)
        self.first_weight=QDoubleSpinBox(); self.second_weight=QDoubleSpinBox()
        for w in (self.first_weight,self.second_weight): w.setRange(0,100000000); w.setDecimals(0); w.setSingleStep(1); w.setSuffix(" kg"); w.setReadOnly(True); w.setAlignment(Qt.AlignRight)
        self.first_manual=QLineEdit(); self.first_manual.setPlaceholderText("علت ثبت دستی وزن اول")
        self.second_manual=QLineEdit(); self.second_manual.setPlaceholderText("علت ثبت دستی وزن دوم")
        self.first_manual.setAlignment(Qt.AlignRight); self.second_manual.setAlignment(Qt.AlignRight)
        self.capture_first=QPushButton("ثبت وزن پایدار به‌عنوان توزین اول",objectName="primary"); self.capture_second=QPushButton("ثبت وزن پایدار به‌عنوان توزین دوم",objectName="primary")
        self.lock_first=QPushButton("قفل وزن پایدار"); self.lock_second=QPushButton("قفل وزن پایدار")
        self.lock_first.setEnabled(False); self.lock_second.setEnabled(False)
        self.manual_first=QPushButton("ثبت وزن دستی پُر",objectName="primary"); self.manual_second=QPushButton("ثبت وزن دستی خالی",objectName="primary")
        self.capture_first.clicked.connect(lambda:self.capture(1)); self.capture_second.clicked.connect(lambda:self.capture(2)); self.manual_first.clicked.connect(lambda:self.manual(1)); self.manual_second.clicked.connect(lambda:self.manual(2))
        self.lock_first.clicked.connect(lambda:self.toggle_weight_lock(1)); self.lock_second.clicked.connect(lambda:self.toggle_weight_lock(2))
        wf.addRow("وزن پر",self.first_weight); wf.addRow("",self.lock_first); wf.addRow("",self.capture_first); wf.addRow("علت حالت اضطراری",self.first_manual); wf.addRow("",self.manual_first)
        wl.addLayout(wf); wl.addStretch(); self.pages.addWidget(weighing)

        quality, quality_body, ql = self._page("وزن خالی و ثبت سند", "وزن خالی را ثبت کنید؛ کارشناس پس از ثبت سند، کنترل کیفیت را روی همین سند انجام می‌دهد.")
        self.second_scale_status=QLabel("● در حال اتصال به باسکول…"); set_widget_style(self.second_scale_status, 'font-size:15px;color:#FFB454;background:#132842;padding:12px;border-radius:10px')
        self.second_live_weight=QLabel("۰ kg"); self.second_live_weight.setAlignment(Qt.AlignCenter); set_widget_style(self.second_live_weight, 'font-size:42px;font-weight:800;color:#62B8FF;background:#132842;padding:20px;border-radius:14px')
        ql.addWidget(self.second_scale_status); ql.addWidget(self.second_live_weight)
        second_box=QFrame(objectName="card"); second_layout=QFormLayout(second_box); self.second_weighing_form=second_layout; second_layout.setContentsMargins(18,16,18,16); second_layout.setHorizontalSpacing(22); second_layout.setVerticalSpacing(12); second_layout.setLabelAlignment(Qt.AlignRight | Qt.AlignAbsolute | Qt.AlignVCenter)
        second_layout.addRow("وزن خالی",self.second_weight); second_layout.addRow("",self.lock_second); second_layout.addRow("",self.capture_second); second_layout.addRow("علت حالت اضطراری",self.second_manual); second_layout.addRow("",self.manual_second); ql.addWidget(second_box)
        self.quality_title=quality_title=QLabel("اقلام بار",objectName="wizardPageTitle"); quality_title.setAlignment(Qt.AlignRight|Qt.AlignAbsolute); ql.addWidget(quality_title)
        quality_code_row=QHBoxLayout(); self.quality_code=QLineEdit(); self.quality_code.setPlaceholderText("جست‌وجوی زنده با کد سند یا نام کارشناس…"); self.quality_code.setLayoutDirection(Qt.LeftToRight); self.quality_code.setAlignment(Qt.AlignLeft)
        self.quality_lookup=QPushButton("حذف / تغییر سند"); self.quality_lookup.clicked.connect(self.clear_quality_selection); quality_code_row.addWidget(self.quality_code,1); quality_code_row.addWidget(self.quality_lookup); ql.addLayout(quality_code_row)
        self._selected_quality_code = ""
        self._quality_future = None
        self.quality_results = QListWidget(); self.quality_results.setMaximumHeight(180); self.quality_results.hide(); ql.addWidget(self.quality_results)
        self.quality_results.itemClicked.connect(self.select_quality_result)
        self.quality_search_timer = QTimer(self); self.quality_search_timer.setSingleShot(True); self.quality_search_timer.setInterval(350)
        self.quality_search_timer.timeout.connect(self.search_quality_live)
        self.quality_poll_timer = QTimer(self); self.quality_poll_timer.setInterval(80); self.quality_poll_timer.timeout.connect(self.poll_quality_search)
        self.quality_code.textEdited.connect(self.quality_query_edited)
        self.quality_status=QLabel("کد صادرشده در پنل کنترل کیفیت را وارد کنید."); self.quality_status.setWordWrap(True); set_widget_style(self.quality_status, 'color:#9CAFC8;background:#132842;padding:12px;border-radius:10px'); ql.addWidget(self.quality_status)
        self.composition=QTableWidget(0,2); self.composition.setHorizontalHeaderLabels(["قلم کالا","درصد"]); self.composition.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch); self.composition.horizontalHeader().setSectionResizeMode(1,QHeaderView.Interactive); self.composition.setColumnWidth(1,170); self.composition.setMinimumHeight(190); self.composition.setLayoutDirection(Qt.RightToLeft); style_table_scrollbars(self.composition); ql.addWidget(self.composition)
        self.add_material_button=QPushButton("افزودن قلم کالا"); self.add_material_button.clicked.connect(self.add_composition); ql.addWidget(self.add_material_button)
        self.remove_material_button=QPushButton("حذف قلم انتخاب‌شده"); self.remove_material_button.clicked.connect(lambda:self.composition.removeRow(self.composition.currentRow())); ql.addWidget(self.remove_material_button)
        self.quality_form=QFormLayout(); self.quality_form.setLabelAlignment(Qt.AlignRight | Qt.AlignAbsolute | Qt.AlignVCenter); self.quality_form.setHorizontalSpacing(22); self.quality_form.setVerticalSpacing(12)
        self.loss=QDoubleSpinBox(); self.rejected=QDoubleSpinBox()
        for field in (self.loss,self.rejected): field.setRange(0,99.999); field.setDecimals(3); field.setSuffix(" ٪"); field.setAlignment(Qt.AlignRight|Qt.AlignAbsolute)
        self.expert=QLineEdit(); self.expert.setAlignment(Qt.AlignRight|Qt.AlignAbsolute)
        self.loss.setReadOnly(True); self.rejected.setReadOnly(True); self.expert.setReadOnly(True)
        self.quality_form.addRow("درصد افت",self.loss); self.quality_form.addRow("درصد برگشتی",self.rejected); self.quality_form.addRow("کارشناس کنترل کیفیت",self.expert); ql.addLayout(self.quality_form)
        self.composition_hint=hint=QLabel("مجموع درصد اقلام باید ۱۰۰٪ باشد. برای کالای بدون کنترل کیفیت، وزن خالص میان اقلام تقسیم می‌شود. ترکیب قراضه‌ها باید دقیقاً ۱۰۰٪ باشد. افت و برگشتی جداگانه از وزن اولیه کسر می‌شوند و وزن باقی‌مانده مطابق ترکیب بین قراضه‌ها توزیع می‌شود."); hint.setWordWrap(True); set_widget_style(hint, 'color:#9CAFC8;background:#132842;padding:10px;border-radius:8px'); ql.addWidget(hint)
        self.pages.addWidget(quality)

        footer = QHBoxLayout(); footer.setSpacing(10)
        self.close_button=QPushButton("بستن و ادامه در آینده"); self.close_button.clicked.connect(self.reject)
        self.back_button=QPushButton("بازگشت به مرحله قبل"); self.back_button.clicked.connect(self.previous_step)
        self.next_button=QPushButton("ادامه به مرحله بعد",objectName="primary"); self.next_button.clicked.connect(self.next_step)
        self.finish_button=QPushButton("محاسبه و صدور سند",objectName="primary"); self.finish_button.clicked.connect(self.finish)
        footer.addWidget(self.close_button); footer.addStretch(); footer.addWidget(self.back_button); footer.addWidget(self.next_button); footer.addWidget(self.finish_button)
        layout.addLayout(footer)
        self.movement.currentIndexChanged.connect(self.movement_changed); self.material_type.currentIndexChanged.connect(self.material_changed)
        parser=Teta10AParser(int(self.db.get_setting("scale_maximum_weight_kg","60000")))
        self.scale=SerialScale(self.db.get_setting("scale_port","COM1"),int(self.db.get_setting("scale_baudrate","9600")),parser=parser)
        claim_sidebar_scale(self); self.scale.start(self.on_reading,self.on_scale_state); self.poll=QTimer(self); self.poll.timeout.connect(self.refresh_scale); self.poll.start(150)
        self.movement_changed()
        if existing: self.load_existing(existing)
        self.goto_step(self.current_step)

    def _page(self, title, subtitle):
        scroll=QScrollArea(objectName="wizardScroll"); scroll.setWidgetResizable(True); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        card=QFrame(objectName="wizardCard"); body=QVBoxLayout(card); body.setContentsMargins(24,22,24,22); body.setSpacing(16)
        heading=QLabel(title,objectName="wizardPageTitle"); heading.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Preferred); heading.setAlignment(Qt.AlignRight|Qt.AlignAbsolute)
        description=QLabel(subtitle,objectName="wizardPageHint"); description.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Preferred); description.setAlignment(Qt.AlignRight|Qt.AlignAbsolute); description.setWordWrap(True)
        body.addWidget(heading); body.addWidget(description); scroll.setWidget(card)
        return scroll, card, body

    def load_existing(self,d):
        self._restoring_material = next((m for m in self.db.material_types() if str(m['id']) == str(d['material_type_id'])), None)
        if 'material_type_id' in d.keys() and d['material_type_id']:
            for idx in range(self.material_type.count()):
                if int(self.material_type.itemData(idx)['id'])==int(d['material_type_id']):self.material_type.setCurrentIndex(idx);break
        idx=self.movement.findData(d["movement_type"]); self.movement.setCurrentIndex(max(0,idx)); v=json.loads(d["company_snapshot"] or "{}")
        if v:
            idx=next((i for i in range(self.company.count()) if (self.company.itemData(i) or {}).get("id")==v.get("id")),-1)
            if idx>=0:self.company.setCurrentIndex(idx)
        for widget,key in ((self.driver_name,"driver_name"),(self.driver_phone,"driver_phone"),(self.destination,"destination")):widget.setText(d[key] or "")
        if d["waybill_number"]:
            self.waybill_reservation_token=d["waybill_reservation_token"] or ""
            self.waybill.setText(fa_digits(d["waybill_number"])); snapshot=json.loads(d["waybill_snapshot"] or "{}")
            local=self.db.waybill_by_number(int(str(d["waybill_number"]).translate(LATIN_DIGITS)))
            self.selected_waybill=dict(local) if local else snapshot
            if self.selected_waybill:self.show_waybill_result()
        self.province.select_text(d["origin_province"] or "")
        self.province_changed()
        self.city.select_text(d["origin_city"] or "")
        self.trailer_plate.set_value(d["trailer_plate"] or d["vehicle_plate"] or "")
        self.notes.setPlainText(d["notes"] or ""); ws,parts=self.db.document_parts(d["id"])
        self.quality_code.setText(d["quality_document_code"] or "")
        for w in ws:
            spin=self.first_weight if w["sequence"]==1 else self.second_weight; spin.setValue(w["weight_kg"]); spin.setReadOnly(w["source"]!="manual")
            if w["source"]=="manual":(self.first_manual if w["sequence"]==1 else self.second_manual).setText(w["manual_reason"] or "")
            self.captures[w["sequence"]] = dict(w)
        external=self.movement.currentData() not in ("warehouse_to_production","warehouse_transfer")
        self.loss.setValue(d["loss_percentage"] if external else 0); self.rejected.setValue(d["rejected_percentage"] if external else 0); self.expert.setText(d["expert_name"] or "")
        for part in parts:
            self.add_composition(); row=self.composition.rowCount()-1; combo=self.composition.cellWidget(row,0); idx=combo.findData(part["scrap_type_id"]); idx=combo.findData(int(part["scrap_type_id"])) if idx<0 and str(part["scrap_type_id"]).isdigit() else idx; combo.setCurrentIndex(max(0,idx)); self.composition.cellWidget(row,1).setValue(part["percentage"])
        if d["quality_document_code"]:
            self._selected_quality_code = str(d["quality_document_code"])
            self.quality_code.setReadOnly(False)
            for row in range(self.composition.rowCount()):
                self.composition.cellWidget(row,0).setEnabled(False); self.composition.cellWidget(row,1).setReadOnly(True)
            self.quality_status.setText(f"سند کنترل کیفیت {d['quality_document_code']} در پیش‌نویس ذخیره شده است.")
            set_widget_style(self.quality_status, 'color:#32D6A0;background:#16372F;padding:12px;border-radius:10px')
        self._restoring_material = None
        self.movement_changed()
        if "source_warehouse_id" in d.keys() and d["source_warehouse_id"]:
            for idx in range(self.source_warehouse.count()):
                if (self.source_warehouse.itemData(idx) or {}).get("id")==d["source_warehouse_id"]:
                    self.source_warehouse.setCurrentIndex(idx);break
        if "warehouse_snapshot_json" in d.keys() and d["destination_warehouse_id"]:
            saved=json.loads(d["warehouse_snapshot_json"] or "[]")
            destination=next((w for w in saved if w["id"]==d["destination_warehouse_id"]),None)
            if destination:
                idx=self.destination_factory.findData(destination["factory_id"])
                self.destination_factory.blockSignals(True);self.destination_factory.setCurrentIndex(idx);self.destination_factory.blockSignals(False)
                self._resolved_destination=destination
                self.destination_warehouse_label.setText(f"{destination['name']} · {destination['code']} (مقصد ذخیره‌شده سند)")
        self.current_step = 2 if ws else (1 if self._base_complete(False) else 0)

    def on_reading(self,reading): self.latest=reading
    def on_scale_state(self,connected,text): self.scale_connected=connected; self.scale_message=text
    def refresh_scale(self):
        connected=self.scale_connected
        status_text=self.scale_message or ("ارتباط کامل با باسکول" if connected else "وضعیت اتصال باسکول مشخص نیست")
        self.scale_status.setText("● "+status_text); self.second_scale_status.setText("● "+status_text)
        waiting=(not connected and ("در حال" in status_text or "باز شد" in status_text))
        colors=("#32D6A0","#16372F") if connected else (("#FFB454","#3A3025") if waiting else ("#FF647C","#382333"))
        status_style="font-size:15px;color:%s;background:%s;padding:12px;border-radius:10px" % colors
        set_widget_style(self.scale_status, status_style); set_widget_style(self.second_scale_status, status_style)
        # در حالت اضطراری، خود علت تشخیصی باسکول عیناً در سند ذخیره می‌شود.
        if not connected:
            self.first_manual.setText(status_text); self.second_manual.setText(status_text)
        for sequence,form,spin,lock,capture,reason,manual in (
            (1,self.first_weighing_form,self.first_weight,self.lock_first,self.capture_first,self.first_manual,self.manual_first),
            (2,self.second_weighing_form,self.second_weight,self.lock_second,self.capture_second,self.second_manual,self.manual_second),
        ):
            form.setRowVisible(spin,not connected); form.setRowVisible(lock,connected); form.setRowVisible(capture,connected)
            form.setRowVisible(reason,False); form.setRowVisible(manual,not connected)
            frozen = sequence==1 and self.first_stage_published
            spin.setReadOnly(connected or frozen)
            capture.setEnabled(not frozen);manual.setEnabled(not frozen)
            locked = sequence in self.locked_readings
            lock.setEnabled(not frozen and (locked or bool(self.latest and self.latest.stable)))
            lock.setText("باز کردن قفل وزن" if locked else "قفل وزن پایدار")
            lock.setProperty("locked", locked)
            set_widget_style(lock, "background:#16372F;color:#32D6A0;border:1px solid #2D6353;"
                if locked else "")
        self.live_weight.setVisible(connected); self.raw.setVisible(connected); self.second_live_weight.setVisible(connected)
        if self.latest:
            for sequence,label in ((1,self.live_weight),(2,self.second_live_weight)):
                reading=self.locked_readings.get(sequence) or self.latest
                suffix="  قفل‌شده" if sequence in self.locked_readings else ("  ✓ پایدار" if reading.stable else "  در حال تغییر")
                label.setText(fa_digits(f"{round(float(reading.weight_kg)):,} kg")+suffix)
            self.raw.setText("داده خام: "+fa_digits(self.latest.raw_payload))

    def selected_material(self):
        if self.movement.currentData() not in ('warehouse_to_production','warehouse_transfer') and not (self.selected_waybill or {}).get('material_type_id'):
            return getattr(self, '_restoring_material', None)
        material = self.material_type.currentData()
        wb = (self.selected_waybill or {}) if self.movement.currentData() not in ('warehouse_to_production','warehouse_transfer') else {}
        if wb.get('material_type_id'):
            candidates = self.db.material_types()
            material = next((m for m in candidates if int(m['id'])==int(wb['material_type_id'])),None)
        return material

    def selected_policy(self):
        material=self.selected_material()
        if not material:return {}
        wb=(self.selected_waybill or {}) if self.movement.currentData() not in ('warehouse_to_production','warehouse_transfer') else {}
        return wb.get('material_policy') or json.loads(wb.get('material_policy_json') or '{}') or json.loads(material['policy_json'])

    def material_quality_enabled(self):
        if not self.selected_material():return self.movement.currentData() not in ("warehouse_to_production","warehouse_transfer")
        policy=self.selected_policy()
        return bool(policy.get('quality_enabled')) and self.movement.currentData() not in ('warehouse_to_production','warehouse_transfer')

    def material_quality_required(self):
        if not self.selected_material():return True
        policy=self.selected_policy()
        return bool(policy.get('quality_required')) and self.movement.currentData() not in ('warehouse_to_production','warehouse_transfer')

    def material_changed(self):
        self.clear_quality_selection()
        material=self.selected_material(); wb=self.selected_waybill or {}
        supplier_company_id=wb.get('supplier_company_id') or (wb.get('company') or {}).get('id') or json.loads(wb.get('company_snapshot') or '{}').get('id')
        if material and wb and supplier_company_id and str(supplier_company_id) not in {str(v['id']) for v in self.db.material_companies(material['id'])}:
            if self.waybill_reservation_token:
                try:release_waybill(self.db.get_setting('waybill_release_url'),int(wb['number']),self.waybill_reservation_token,self.db.get_setting('transport_api_token'))
                except Exception:pass
            self.selected_waybill=None; self.waybill_reservation_token=''; self.waybill.clear()
            self.waybill_info.setText('تأمین‌کننده بارنامه در حوزه انتخاب‌شده مجاز نیست؛ بارنامه دیگری انتخاب یا اطلاعات مرجع را تازه‌سازی کنید.')
        self.movement_changed()

    def movement_changed(self):
        external=self.movement.currentData() not in ("warehouse_to_production","warehouse_transfer")
        production=self.movement.currentData() in ("warehouse_to_production","external_to_production")
        self.basic_form.setRowVisible(self.material_type,not external)
        self.basic_form.setRowVisible(self.material_display,external)
        self.material_display.setText((self.selected_material() or {}).get('name') or 'پس از انتخاب بارنامه نمایش داده می‌شود')
        for w in (self.waybill_search_box,self.waybill_info):self.basic_form.setRowVisible(w,external)
        if not external:self.waybill_popup.hide()
        for w in (self.company,self.driver_name,self.driver_phone,self.province,self.city): self.basic_form.setRowVisible(w,False)
        self.basic_form.setRowVisible(self.trailer_plate,not external)
        self.basic_form.setRowVisible(self.destination,production)
        self.quality_form.setRowVisible(self.loss,external); self.quality_form.setRowVisible(self.rejected,external)
        if not external:self.loss.setValue(0); self.rejected.setValue(0)
        if hasattr(self,'quality_code'):
            enabled=self.material_quality_enabled()
            self.add_material_button.setVisible(not enabled); self.remove_material_button.setVisible(not enabled)
            self.composition.setVisible(not enabled); self.quality_title.setVisible(not enabled); self.composition_hint.setVisible(not enabled)
            for widget in (self.quality_code,self.quality_lookup,self.expert):widget.hide()
            self.quality_results.hide()
            self.quality_status.setVisible(enabled)
            self.quality_status.setText("کنترل کیفیت پس از ثبت این سند، توسط کارشناس در مگاپالس انجام می‌شود. ثبت وزن خالی و صدور سند به کنترل کیفیت وابسته نیست.")
            self.quality_form.setRowVisible(self.loss,False); self.quality_form.setRowVisible(self.rejected,False); self.quality_form.setRowVisible(self.expert,False)
            self.material_type.setEnabled(not external)
            if not external:self.loss.setValue(0); self.rejected.setValue(0)

        self.refresh_warehouse_choices()

    def refresh_warehouse_choices(self):
        material=self.selected_material()
        type_id=material.get("id") if material else None
        movement=self.movement.currentData()
        use_warehouses=bool(self.db.warehouses())
        if self._warehouse_type_id != type_id:
            self._warehouse_type_id=type_id
            self.source_warehouse.clear(); self.destination_factory.blockSignals(True); self.destination_factory.clear()
            self.source_warehouse.addItem("انتخاب انبار مبدأ…",None)
            self.destination_factory.addItem("انتخاب کارخانه مقصد…",None)
            factories={}
            if type_id:
                for warehouse in self.db.warehouses(type_id):
                    factory=warehouse.get("factory") or {}
                    self.source_warehouse.addItem(f"{factory.get('name','')} / {warehouse['name']} ({warehouse['code']})",warehouse)
                    factories[warehouse['factory_id']]=factory
            for factory_id,factory in factories.items():
                self.destination_factory.addItem(factory.get("name",str(factory_id)),factory_id)
            self.destination_factory.blockSignals(False)
            self._resolved_destination=None
            self.destination_warehouse_label.setText("کارخانه مقصد را انتخاب کنید.")
        self.basic_form.setRowVisible(self.explicit_destination,False)
        self.basic_form.setRowVisible(self.source_warehouse,use_warehouses and movement in ("warehouse_to_production","warehouse_transfer"))
        self.basic_form.setRowVisible(self.destination_factory,use_warehouses and movement in ("external_to_warehouse","warehouse_transfer"))
        self.basic_form.setRowVisible(self.destination_warehouse_label,use_warehouses and movement in ("external_to_warehouse","warehouse_transfer"))

    def explicit_destination_changed(self):
        warehouse=self.explicit_destination.currentData()
        if warehouse:
            self._resolved_destination=warehouse
            self.destination_warehouse_label.setText(f"{warehouse['name']} · {warehouse['code']} (انتخاب صریح مقصد)")

    def resolve_destination_warehouse(self):
        self._resolved_destination=None
        factory_id=self.destination_factory.currentData()
        source=self.source_warehouse.currentData()
        same_factory=bool(source and factory_id and int(source["factory_id"])==int(factory_id) and self.movement.currentData()=="warehouse_transfer")
        self.explicit_destination.blockSignals(True);self.explicit_destination.clear();self.explicit_destination.addItem("انتخاب انبار دیگر…",None)
        if same_factory:
            for warehouse in self.db.warehouses(source["material_type_id"]):
                if int(warehouse["factory_id"])==int(factory_id) and warehouse["id"]!=source["id"]:
                    self.explicit_destination.addItem(warehouse["name"],warehouse)
        self.explicit_destination.blockSignals(False)
        self.basic_form.setRowVisible(self.explicit_destination,same_factory)
        material=self.selected_material()
        if not factory_id or not material:
            self.destination_warehouse_label.setText("کارخانه مقصد را انتخاب کنید.");return
        try:
            warehouse=self.db.resolve_warehouse(factory_id,material["id"])
            self._resolved_destination=warehouse
            self.destination_warehouse_label.setText(f"{warehouse['name']} · {warehouse['code']} · {warehouse.get('location') or ''}")
        except ValueError as exc:
            self.destination_warehouse_label.setText(str(exc))

    def clear_waybill_result(self):
        self.waybill_results.clear(); self.waybill_popup.hide()
        raw=self.waybill.text().strip().translate(LATIN_DIGITS)
        if raw:
            self.waybill_search_state.setText("در انتظار تایپ…")
            if not self.selected_waybill:self.waybill_info.setText("پس از مکث کوتاه، بارنامه‌ها بر اساس شماره جستجو می‌شوند.")
            self.waybill_debounce.start()
        else:
            self.waybill_debounce.stop()
            if self.selected_waybill:
                self.waybill_search_state.setText("انتخاب محفوظ است"); self.show_waybill_result()
            else:
                self.waybill_search_state.clear(); self.waybill_info.setText("بخشی از شماره بارنامه را وارد کنید."); set_widget_style(self.waybill_info, 'color:#9CAFC8;background:#132842;padding:12px;border-radius:10px')

    def search_waybill(self):
        raw=self.waybill.text().strip().translate(LATIN_DIGITS)
        if not raw or not raw.isdigit():return
        self.waybill_search_state.setText("در حال جستجو…"); self.waybill_results.clear()
        try:
            matches=[dict(row) for row in self.db.available_waybills_by_number_prefix(raw)]
            url=self.db.get_setting("waybill_available_url")
            if url:
                for item in fetch_available_waybills(url,self.db.get_setting("transport_api_token"),raw,material_type_id=None):
                    row=dict(item); row["_remote"]=True; row["company_snapshot"]=json.dumps(row.get("company") or {},ensure_ascii=False)
                    if str(row.get("number","")).startswith(raw) and not any(str(x.get("number"))==str(row.get("number")) for x in matches):matches.append(row)
            matches=[r for r in matches if r.get('material_type_id')]
            matches.sort(key=lambda row:int(row.get("number",0)),reverse=True)
            for row in matches[:12]:
                row_width=max(520,self.waybill_search_box.width()-20); company=json.loads(row.get("company_snapshot") or "{}"); item=QListWidgetItem(); item.setData(Qt.UserRole,row); item.setSizeHint(QSize(row_width,88)); self.waybill_results.addItem(item)
                result_card=QFrame(objectName="waybillResultCard"); result_card.setMinimumWidth(row_width); result_card.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed); result_card.setAttribute(Qt.WA_TransparentForMouseEvents); result_card.setLayoutDirection(Qt.RightToLeft); set_widget_style(result_card, 'QFrame#waybillResultCard{background:transparent;border:0;border-bottom:1px solid #263B56;} QLabel{background:transparent;border:0;}')
                card_layout=QVBoxLayout(result_card); card_layout.setContentsMargins(15,8,15,8); card_layout.setSpacing(3)
                title=QLabel("\u200f"+fa_digits(f"بارنامه شماره {row.get('number','—')}  —  {company.get('name','شرکت تأمین‌کننده نامشخص')}")); title.setAttribute(Qt.WA_TransparentForMouseEvents); title.setLayoutDirection(Qt.RightToLeft); title.setAlignment(Qt.AlignRight|Qt.AlignAbsolute|Qt.AlignVCenter); set_widget_style(title, 'color:#F3F7FF;font-size:12px;font-weight:800')
                transport=QLabel("\u200f"+fa_digits(f"پلاک: {row.get('trailer_plate','نامشخص')}  |  راننده: {row.get('driver_name','—')}، {row.get('driver_phone','—')}")); transport.setAttribute(Qt.WA_TransparentForMouseEvents); transport.setLayoutDirection(Qt.RightToLeft); transport.setAlignment(Qt.AlignRight|Qt.AlignAbsolute|Qt.AlignVCenter); set_widget_style(transport, 'color:#9CAFC8;font-size:12px')
                detail=QLabel("\u200f"+fa_digits(f"کالا: {row.get('material_name') or (row.get('material_type') or {}).get('name','—')} · مبدأ: {row.get('origin_province','—')}، {row.get('origin_city','—')}  |  وزن مبدأ: {int(row.get('origin_net_weight_kg') or 0):,} کیلوگرم")); detail.setAttribute(Qt.WA_TransparentForMouseEvents); detail.setLayoutDirection(Qt.RightToLeft); detail.setAlignment(Qt.AlignRight|Qt.AlignAbsolute|Qt.AlignVCenter); set_widget_style(detail, 'color:#9CAFC8;font-size:12px')
                for label in (title,transport,detail):
                    label.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Preferred)
                    line=QWidget(); line.setAttribute(Qt.WA_TransparentForMouseEvents); line.setLayoutDirection(Qt.LeftToRight); line_layout=QHBoxLayout(line); line_layout.setDirection(QHBoxLayout.LeftToRight); line_layout.setContentsMargins(0,0,0,0); line_layout.setSpacing(0); line_layout.addStretch(1); line_layout.addWidget(label); card_layout.addWidget(line)
                self.waybill_results.setItemWidget(item,result_card)
            has_results=self.waybill_results.count()>0
            if has_results:self.show_waybill_popup()
            else:self.waybill_popup.hide()
            self.waybill_search_state.setText(fa_digits(f"{self.waybill_results.count()} نتیجه") if has_results else "بدون نتیجه")
            if not self.selected_waybill:self.waybill_info.setText("یکی از نتایج را انتخاب کنید." if has_results else "بارنامه قابل استفاده‌ای با این شماره پیدا نشد.")
        except Exception as exc:
            self.waybill_search_state.setText("خطای اتصال")
            if not self.selected_waybill:
                self.waybill_info.setText(f"جستجو انجام نشد: {exc}"); set_widget_style(self.waybill_info, 'color:#FF647C;background:#382333;padding:12px;border-radius:10px')

    def show_waybill_popup(self):
        anchor=self.waybill_search_box; position=anchor.mapTo(self,QPoint(0,anchor.height()+4)); height=min(360,max(92,self.waybill_results.count()*88+10))
        content_width=max(420,anchor.width()-20)
        for index in range(self.waybill_results.count()):
            item=self.waybill_results.item(index); item.setSizeHint(QSize(content_width,88)); card=self.waybill_results.itemWidget(item)
            if card:
                card.setFixedWidth(content_width)
        self.waybill_popup.setGeometry(position.x(),position.y(),anchor.width(),height); self.waybill_popup.raise_(); self.waybill_popup.show(); self.waybill_results.setFocus()

    def toggle_waybill_results(self):
        if self.waybill_popup.isVisible():return
        if self.waybill_results.count()>0:self.show_waybill_popup(); return
        raw=self.waybill.text().strip().translate(LATIN_DIGITS)
        if raw:self.waybill_debounce.stop(); self.search_waybill()
        else:self.waybill.setFocus()

    def select_waybill_result(self,item):
        row=item.data(Qt.UserRole); previous_number=(self.selected_waybill or {}).get("number"); self.selected_waybill=dict(row) if isinstance(row,dict) else None
        if not self.selected_waybill:return
        material_id=self.selected_waybill.get('material_type_id')
        if not material_id:
            self.selected_waybill=None
            self.waybill_info.setText("ابتدا نوع کالا را در بارنامه ثبت کنید."); return
        if material_id:
            for idx in range(self.material_type.count()):
                if int(self.material_type.itemData(idx)['id'])==int(material_id):self.material_type.setCurrentIndex(idx);break
        self.material_changed()
        if not self.selected_waybill:return
        if str(previous_number or "") != str(self.selected_waybill.get("number") or ""):self.waybill_reservation_token=""
        self.waybill.blockSignals(True); self.waybill.setText(fa_digits(self.selected_waybill.get("number",""))); self.waybill.blockSignals(False)
        self.waybill_popup.hide(); self.waybill_search_state.setText("انتخاب شد"); self.show_waybill_result()

    def show_waybill_result(self):
        if not self.selected_waybill:return
        company=json.loads(self.selected_waybill.get("company_snapshot") or "{}")
        self.waybill_info.setText("\u200f"+fa_digits(f"شرکت تأمین‌کننده: {company.get('name','—')} · راننده: {self.selected_waybill['driver_name']} · موبایل: {self.selected_waybill['driver_phone']}\nپلاک: {self.selected_waybill['trailer_plate']} · مبدأ: {self.selected_waybill['origin_province']}، {self.selected_waybill['origin_city']} · وزن مبدأ: {self.selected_waybill['origin_net_weight_kg']:,} کیلوگرم"))
        self.waybill_info.setLayoutDirection(Qt.RightToLeft); self.waybill_info.setAlignment(Qt.AlignRight|Qt.AlignAbsolute|Qt.AlignVCenter)
        set_widget_style(self.waybill_info, 'color:#32D6A0;background:#16372F;border:1px solid #32D6A0;padding:12px;border-radius:10px')

    def province_changed(self):
        province_id = self.province.currentData()
        self.city.blockSignals(True)
        self.city.clear()
        if province_id is not None:
            for city in self.cities_by_province.get(int(province_id), []):
                self.city.addItem(city["name"], int(city["id"]))
        self.city.setCurrentIndex(-1)
        self.city.lineEdit().setPlaceholderText("جستجو و انتخاب شهر…" if province_id is not None else "ابتدا استان را انتخاب کنید…")
        self.city.setEnabled(province_id is not None)
        self.city.blockSignals(False)

    def goto_step(self, step):
        self.current_step=max(0,min(2,step)); self.pages.setCurrentIndex(self.current_step)
        self.progress_label.setText(("مرحله ۱ از ۳ · اطلاعات پایه", "مرحله ۲ از ۳ · ثبت وزن پُر", "مرحله ۳ از ۳ · وزن خالی و ثبت سند")[self.current_step])
        for i,label in enumerate(self.step_labels):
            label.setProperty("current",i==self.current_step); label.setProperty("done",i<self.current_step); label.style().unpolish(label); label.style().polish(label)
        self.back_button.setVisible(self.current_step>0); self.next_button.setVisible(self.current_step<2); self.finish_button.setVisible(self.current_step==2)

    def _base_complete(self, show_messages=True):
        external=self.movement.currentData() not in ("warehouse_to_production","warehouse_transfer")
        checks=[]
        if self.db.warehouses() or self.movement.currentData()=="warehouse_transfer":
            if self.movement.currentData() in ("warehouse_transfer","warehouse_to_production"):
                checks.append((bool(self.source_warehouse.currentData()),"انبار مبدأ","انبار مبدأ را انتخاب کنید."))
            if self.movement.currentData() in ("warehouse_transfer","external_to_warehouse"):
                checks.append((bool(self._resolved_destination),"انبار مقصد","کارخانه و انبار مقصد مشخص و بدون ابهام لازم است."))
            if self.movement.currentData()=="warehouse_transfer" and self.source_warehouse.currentData() and self._resolved_destination:
                checks.append((self.source_warehouse.currentData()["id"]!=self._resolved_destination["id"],"انتقال","مبدأ و مقصد نمی‌توانند یکسان باشند."))
        checks.append((self.selected_material() is not None,'نوع کالا','بارنامه دارای نوع کالای معتبر را انتخاب کنید؛ اطلاعات مرجع را نیز به‌روز کنید.'))
        if external:checks.append((self.selected_waybill is not None,"بارنامه","یک بارنامه معتبر و استفاده‌نشده انتخاب کنید."))
        else:checks.append((self.trailer_plate.is_complete(),"پلاک تریلی","پلاک تریلی را به‌صورت کامل وارد کنید: دو رقم، حرف، سه رقم و کد دو رقمی ایران."))
        if self.movement.currentData() in ("warehouse_to_production","external_to_production"): checks.append((bool(self.destination.text().strip()),"مقصد","مقصد یا خط تولید را وارد کنید."))
        for ok,title,text in checks:
            if not ok:
                if show_messages: message(self,title,text,True)
                return False
        return True

    def next_step(self):
        if self.current_step==0 and not self._base_complete():return
        if self.current_step==0 and self.selected_waybill and self.selected_waybill.get("_remote") and not self.waybill_reservation_token:
            template=self.db.get_setting("waybill_reserve_url")
            if not template:return message(self,"تنظیمات بارنامه","آدرس رزرو بارنامه در تنظیمات وارد نشده است.",True)
            try:
                result=reserve_waybill(template,int(self.selected_waybill["number"]),self.db.get_setting("transport_api_token")); self.waybill_reservation_token=str(result.get("reservation_token") or result.get("token") or "")
                if not self.waybill_reservation_token:raise ValueError("سرور کد رزرو بارنامه را برنگرداند.")
            except Exception as exc:return message(self,"رزرو بارنامه",str(exc),True)
        if self.current_step==1 and self.first_weight.value()<=0:return message(self,"وزن پر","وزن پر تریلی را ثبت کنید.",True)
        try:self.persist_draft()
        except Exception as exc:return message(self,"ذخیره پیش‌نویس",f"ذخیره اطلاعات انجام نشد: {exc}",True)
        if self.current_step==1:
            try:self.publish_first_weighing()
            except Exception as exc:return message(self,"ثبت توزین اول",str(exc),True)
        self.goto_step(self.current_step+1)

    def previous_step(self):
        if self.document_id and self.db.first_weighing_queued(self.document_id):return message(self,"توزین اول ثبت شده","اطلاعات پایه و وزن پر پس از ارسال توزین اول قابل تغییر نیستند.",True)
        try:self.persist_draft()
        except Exception:pass
        self.goto_step(self.current_step-1)

    def toggle_weight_lock(self, sequence):
        if sequence in self.locked_readings:
            del self.locked_readings[sequence]
            return
        if not self.scale_connected:
            return message(self,"قفل وزن","ارتباط معتبر با باسکول برقرار نیست.",True)
        if not self.latest or not self.latest.stable:
            return message(self,"قفل وزن","فقط وزن پایدار قابل قفل‌کردن است.",True)
        self.locked_readings[sequence] = self.latest
        weight=round(float(self.latest.weight_kg))
        title="وزن پُر" if sequence==1 else "وزن خالی"
        message(self,"وزن قفل شد",f"{title} روی {fa_digits(f'{weight:,}')} کیلوگرم قفل شد و تغییرات بعدی باسکول روی آن اثر نمی‌گذارد.")

    def publish_first_weighing(self):
        if not self.document_id or not self._base_complete(False):return
        self.persist_draft()
        self.db.queue_first_weighing(self.document_id)
        self.first_stage_published=True
        self.back_button.setEnabled(False)
        parent=self.parent()
        if parent and hasattr(parent,'start_sync'):parent.start_sync()

    def capture(self,sequence):
        if sequence==1 and self.first_stage_published:return message(self,"توزین اول ثبت شده","وزن پر پس از ارسال به سایت قابل تغییر نیست.",True)
        if not self.scale_connected:return message(self,"وزن از باسکول قابل دریافت نیست",self.scale_message or "اتصال معتبر با باسکول برقرار نیست.",True)
        reading=self.locked_readings.get(sequence) or self.latest
        if not reading or not reading.stable:return message(self,"وزن ناپایدار","برای جلوگیری از خطا، فقط وزن پایدار قابل ثبت است.",True)
        captured_weight=round(float(reading.weight_kg))
        (self.first_weight if sequence==1 else self.second_weight).setValue(captured_weight)
        self.captures[sequence] = {
            "weight_kg": captured_weight, "source": "device",
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "stable": True, "manual_reason": "", "raw_payload": reading.raw_payload,
        }
        if sequence==1:
            try:self.publish_first_weighing()
            except Exception as exc:return message(self,"ثبت توزین اول",str(exc),True)
        title="توزین اول (وزن پُر)" if sequence==1 else "توزین دوم (وزن خالی)"
        message(self,"توزین ثبت شد",f"وزن {fa_digits(f'{captured_weight:,}')} کیلوگرم به‌عنوان {title} در سند ثبت شد.")

    def manual(self,sequence):
        if sequence==1 and self.first_stage_published:return message(self,"توزین اول ثبت شده","وزن پر پس از ارسال به سایت قابل تغییر نیست.",True)
        if self.scale_connected:return message(self,"ثبت دستی غیرفعال است","تا زمانی که باسکول متصل است وزن باید مستقیم از دستگاه ثبت شود.",True)
        spin=self.first_weight if sequence==1 else self.second_weight
        if spin.value()<=0:return message(self,"وزن دستی","ابتدا عدد وزن را وارد کنید.",True)
        reason=self.scale_message or "اتصال معتبر با باسکول برقرار نیست."
        (self.first_manual if sequence==1 else self.second_manual).setText(reason)
        self.captures[sequence]={"weight_kg":round(spin.value()),"source":"manual","captured_at":datetime.now().astimezone().isoformat(timespec="seconds"),"stable":False,"manual_reason":reason,"raw_payload":""}
        if sequence==1:
            try:self.publish_first_weighing()
            except Exception as exc:return message(self,"ثبت توزین اول",str(exc),True)
        message(self,"وزن دستی ثبت شد",f"وزن {fa_digits(f'{round(spin.value()):,}')} کیلوگرم با علت تشخیصی باسکول ثبت شد.")

    def add_composition(self):
        material=self.selected_material()
        policy=json.loads(material['policy_json']) if material else {}
        scraps=self.db.scraps() if not material or policy.get('calculator')=='scrap_v1' else self.db.material_items(material['id'])
        if self.movement.currentData() in ("warehouse_to_production","warehouse_transfer") and self.source_warehouse.currentData() and material:
            allowed=self.db.warehouse_item_ids(self.source_warehouse.currentData()["id"])
            available=self.db.material_items(material["id"])
            allowed_refs={str(i["scrap_type_id"] if policy.get("calculator")=="scrap_v1" else i["id"]) for i in available if int(i["id"]) in allowed}
            scraps=[i for i in scraps if str(i["id"]) in allowed_refs]
        if not scraps:return message(self,"قراضه‌ها","ابتدا فهرست قراضه‌ها را از تنظیمات دریافت کنید.",True)
        row=self.composition.rowCount(); self.composition.insertRow(row); combo=StyledComboBox()
        for x in scraps:combo.addItem(x["name"],x["id"])
        pct=QDoubleSpinBox(); pct.setRange(.001,100); pct.setDecimals(3); pct.setSuffix(" ٪"); pct.setAlignment(Qt.AlignRight); self.composition.setCellWidget(row,0,combo); self.composition.setCellWidget(row,1,pct); self.composition.setRowHeight(row,64)

    def clear_quality_selection(self, checked=False, keep_query=False):
        had_selection = bool(self._selected_quality_code)
        self._selected_quality_code = ""
        self.quality_search_timer.stop(); self.quality_results.clear(); self.quality_results.hide()
        self.composition.setRowCount(0); self.loss.setValue(0); self.rejected.setValue(0); self.expert.clear()
        self.quality_code.setReadOnly(False)
        if not keep_query:self.quality_code.clear()
        self.quality_status.setText("سند کنترل کیفیت را جست‌وجو و از نتایج انتخاب کنید.")
        set_widget_style(self.quality_status, 'color:#9CAFC8;background:#132842;padding:12px;border-radius:10px')
        if had_selection and getattr(self, 'document_id', None):
            self.db.clear_quality_document(self.document_id)
        self.quality_code.setFocus()

    def quality_query_edited(self, text):
        self.clear_quality_selection(keep_query=True)
        self.quality_search_timer.start()

    def search_quality_live(self):
        url = self.db.quality_document_url()
        if not url:
            self.quality_status.setText("ابتدا اتصال به پنل را تنظیم کنید."); return
        material = self.selected_material()
        if not material:
            self.quality_status.setText("ابتدا بارنامه دارای نوع کالای معتبر را انتخاب کنید."); return
        self._quality_query = (self.quality_code.text(), str(material['code']) if material else 'scrap')
        if self._quality_future:self._quality_future.cancel()
        self._quality_future = _quality_search_pool.submit(search_quality_documents, url, *self._quality_query, self.db.get_setting('transport_api_token'))
        self.quality_status.setText("در حال جست‌وجوی اسناد قابل استفاده…")
        self.quality_poll_timer.start()

    def poll_quality_search(self):
        if not self._quality_future or not self._quality_future.done():return
        self.quality_poll_timer.stop()
        material = self.selected_material()
        if self._quality_query != (self.quality_code.text(), str(material['code']) if material else 'scrap') or self._selected_quality_code:return
        self.quality_results.clear()
        try:
            results = self._quality_future.result()
            for document in results:
                names = '، '.join(str(x.get('material_name', '')) for x in document.get('items', []))
                item = QListWidgetItem(f"{document['document_code']} · {document.get('expert_name', '')} · {names}")
                item.setData(Qt.UserRole, document); self.quality_results.addItem(item)
            self.quality_results.setVisible(bool(results))
            self.quality_status.setText("سند مورد نظر را از فهرست انتخاب کنید." if results else "سند قابل استفاده‌ای برای این کالا یافت نشد.")
        except Exception as exc:
            self.quality_results.hide(); self.quality_status.setText(str(exc))

    def select_quality_result(self, item):
        self.verify_quality_code(item.data(Qt.UserRole))
        self.quality_results.hide()

    def verify_quality_code(self, selected_data=None):
        if not self.selected_material():
            self.quality_status.setText("ابتدا بارنامه دارای نوع کالای معتبر را انتخاب کنید."); return
        code=normalize_quality_code(self.quality_code.text())
        if not code:return message(self,"کد کنترل کیفیت","کد یکتای سند کنترل کیفیت را وارد کنید.",True)
        url=self.db.quality_document_url()
        if not url:return message(self,"تنظیمات اتصال","آدرس پنل "+self.db.erp_label+" در تنظیمات اتصال ثبت نشده است.",True)
        self._selected_quality_code = ""
        self.quality_lookup.setEnabled(False); self.quality_status.setText("در حال دریافت سند کنترل کیفیت…")
        try:
            data=selected_data if isinstance(selected_data, dict) else fetch_quality_document(url,code,self.db.get_setting("transport_api_token"))
            selected=self.selected_material()
            kind=str(selected['code']) if selected else 'scrap'
            if data.get("material_kind")!=kind:raise RuntimeError("نوع کالای سند کنترل کیفیت با کالای انتخاب‌شده برابر نیست.")
            self.composition.setRowCount(0)
            scraps={str(x["id"]):x for x in (self.db.scraps() if kind=="scrap" else self.db.material_items(selected["id"]))}
            for item in data.get("items",[]):
                material_id=str(item.get("material_reference_id") or ""); material=scraps.get(material_id)
                if not material:raise RuntimeError(f"قلم کالا «{item.get('material_name','')}» در اطلاعات مرجع اپ موجود نیست؛ ابتدا همگام‌سازی کنید.")
                self.add_composition(); row=self.composition.rowCount()-1; combo=self.composition.cellWidget(row,0); idx=combo.findData(material_id); combo.setCurrentIndex(max(0,idx)); combo.setEnabled(False); pct=self.composition.cellWidget(row,1); pct.setValue(float(item.get("percentage") or 0)); pct.setReadOnly(True)
            self.loss.setValue(float(data.get("loss_percentage") or 0)); self.rejected.setValue(float(data.get("rejected_percentage") or 0)); self.expert.setText(str(data.get("expert_name") or "")); self.quality_code.setText(str(data.get("document_code") or code)); self.quality_code.setReadOnly(False); self._selected_quality_code = str(data.get("document_code") or code)
            notes=str(data.get("notes") or ""); summary=f"سند معتبر است · کارشناس: {self.expert.text()} · مجموع ترکیب: {sum(float(x.get('percentage') or 0) for x in data.get('items',[])):g}٪"
            if notes:summary+=f"\nتوضیحات: {notes}"
            self.quality_status.setText(summary); set_widget_style(self.quality_status, 'color:#32D6A0;background:#16372F;padding:12px;border-radius:10px')
        except Exception as exc:
            self.quality_code.setReadOnly(False); self.composition.setRowCount(0); self.quality_status.setText(str(exc)); set_widget_style(self.quality_status, 'color:#FF647C;background:#382333;padding:12px;border-radius:10px'); message(self,"سند کنترل کیفیت",str(exc),True)
        finally:self.quality_lookup.setEnabled(True)

    def values(self):
        items=[]
        for row in range(self.composition.rowCount()):
            c=self.composition.cellWidget(row,0); p=self.composition.cellWidget(row,1); items.append(CompositionInput(str(c.currentData()),c.currentText(),p.value()))
        return items

    def finish(self):
        if not self._base_complete():self.goto_step(0);return
        if self.first_weight.value() <= 0 or self.second_weight.value() <= 0:return message(self,"توزین","هر دو وزن پر و خالی باید ثبت شوند.",True)
        try:
            if self.material_quality_enabled():
                self._selected_quality_code=""; self.composition.setRowCount(0); self.loss.setValue(0); self.rejected.setValue(0)
                self.calculation=calculate_weighed_load(self.first_weight.value(),self.second_weight.value())
            else:
                self.calculation=calculate_load(self.movement.currentData(),self.first_weight.value(),self.second_weight.value(),self.values(),0,0)
        except DomainError as exc:return message(self,"اطلاعات قابل صدور نیست",str(exc),True)
        try:self.persist_draft()
        except Exception as exc:return message(self,"ذخیره سند",f"ذخیره اطلاعات انجام نشد: {exc}",True)
        self.finalizing=True
        self.accept()

    validate = finish

    def weighing_data(self, sequence):
        spin = self.first_weight if sequence == 1 else self.second_weight
        reason = (self.first_manual if sequence == 1 else self.second_manual).text().strip()
        saved = self.captures.get(sequence)
        if saved and abs(float(saved.get("weight_kg", 0)) - spin.value()) < 0.0005:
            return saved
        source = "manual" if not spin.isReadOnly() else "device"
        return {
            "weight_kg": spin.value(), "source": source,
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "stable": source == "device", "manual_reason": reason if source == "manual" else "",
            "raw_payload": self.latest.raw_payload if (self.latest and source == "device") else "",
        }

    def persist_draft(self):
        if not self.document_id:return
        external=self.movement.currentData() not in ("warehouse_to_production","warehouse_transfer"); wb=self.selected_waybill if external else None; company=json.loads(wb.get("company_snapshot") or "{}") if wb else None
        snapshot=self.db.waybill_payload(wb) if wb and not wb.get("_remote") else (dict(wb) if wb else {})
        material=self.selected_material()
        if material:
            policy=self.selected_policy()
            self.db.set_document_material(self.document_id,material['id'],policy,self.movement.currentData())
        self.db.update_draft(self.document_id,{"movement_type":self.movement.currentData(),"company":company,"driver_name":wb.get("driver_name","") if wb else "","driver_phone":wb.get("driver_phone","") if wb else "","vehicle_plate":"","trailer_plate":wb.get("trailer_plate","") if wb else self.trailer_plate.value(),"origin_province":wb.get("origin_province","") if wb else "","origin_city":wb.get("origin_city","") if wb else "","destination":self.destination.text().strip() if self.destination.isVisible() else "","waybill_number":str(wb.get("number","")) if wb else "","waybill_id":wb.get("id") if wb and not wb.get("_remote") else None,"waybill_reservation_token":self.waybill_reservation_token,"waybill":snapshot,"quality_document_code":self._selected_quality_code,"notes":self.notes.toPlainText().strip()})
        if material:
            movement=self.movement.currentData()
            source=self.source_warehouse.currentData() if movement in ("warehouse_to_production","warehouse_transfer") else None
            destination=self._resolved_destination if movement in ("external_to_warehouse","warehouse_transfer") else None
            self.db.set_document_warehouses(self.document_id,source["id"] if source else None,destination["id"] if destination else None)
        for seq,spin in ((1,self.first_weight),(2,self.second_weight)):
            if spin.value()>0:
                data=self.weighing_data(seq); self.db.add_weighing(self.document_id,seq,"gross" if seq==1 else "tare",data["weight_kg"],data["source"],data["captured_at"],data["stable"],data["manual_reason"],data["raw_payload"])
        items=[{"scrap_type_id":x.scrap_type_id,"scrap_name":x.scrap_name,"percentage":float(x.percentage)} for x in self.values()]
        if items and not self.material_quality_enabled():self.db.save_composition(self.document_id,items,self.expert.text().strip(),self.rejected.value(),loss_percentage=self.loss.value())

    def save_draft(self):
        try:self.persist_draft()
        except Exception as exc:return message(self,"ذخیره پیش‌نویس",str(exc),True)
        self.reject()

    def closeEvent(self,event):
        if not self.finalizing:
            try:self._persist_or_discard_empty()
            except Exception:pass
        self.scale.stop(); super().closeEvent(event)

    def done(self,result):
        if not self.finalizing:
            try:self._persist_or_discard_empty()
            except Exception:pass
        self.scale.stop(); super().done(result)

    def _has_user_data(self):
        return any((
            self.movement.currentData() != next(iter(MOVEMENTS)),
            self.company.currentData() is not None,
            self.driver_name.text().strip(), self.driver_phone.text().strip(),
            self.trailer_plate.value(), self.province.currentData() is not None, self.city.currentData() is not None,
            self.destination.text().strip(), self.waybill.text().strip(), self.selected_waybill is not None, self.notes.toPlainText().strip(),
            self.first_weight.value() > 0, self.second_weight.value() > 0,
            self.composition.rowCount() > 0,
        ))

    def _persist_or_discard_empty(self):
        if self.created_for_dialog and not self._has_user_data():
            self.db.delete_advanced_document(self.document_id)
            self.document_id = None
            return
        self.persist_draft()


class WebhookSiteDialog(QDialog):
    ENDPOINTS=(
        ("weighbridge","اسناد باسکول","/api/webhooks/weighbridge"),
        ("waybill","بارنامه‌ها","/api/transportation/webhooks/waybills"),
        ("processing","اسناد فرآوری","/api/webhooks/weighbridge/scrap-processing"),
    )
    def __init__(self, db:Database, row=None, parent=None):
        super().__init__(parent); self.db=db; self.row=row; self.endpoint_inputs={}
        set_widget_style(self, app_style()); self.setLayoutDirection(Qt.RightToLeft)
        self.setWindowTitle("اتصال سایت و ERP"); self.setMinimumSize(780,700); self.resize(900,760); self.setLayoutDirection(Qt.RightToLeft)
        root=QVBoxLayout(self); root.setContentsMargins(24,22,24,22); root.setSpacing(14)
        title=QLabel("تعریف سایت و Endpointهای وب‌هوک")
        set_widget_style(title, 'font-size:22px;font-weight:700;color:#F3F7FF;background:transparent')
        hint=QLabel("اطلاعات اتصال را یک‌بار وارد کنید. در بخش پایین فقط مسیر Endpoint ثبت می‌شود و مگاسنج آدرس نهایی را به‌صورت خودکار می‌سازد.")
        hint.setWordWrap(True); set_widget_style(hint, 'color:#9CAFC8;background:#132842;border:1px solid #263B56;border-radius:12px;padding:12px')
        root.addWidget(title); root.addWidget(hint)
        info=QFrame(objectName="wizardCard"); form=QFormLayout(info); form.setContentsMargins(20,18,20,18); form.setVerticalSpacing(12); form.setHorizontalSpacing(22)
        self.name=QLineEdit(row["name"] if row else ""); self.name.setPlaceholderText("مثلاً مگاپالس، SAP یا ERP کارخانه")
        self.base_url=QLineEdit(row["base_url"] if row else ""); self.base_url.setPlaceholderText("https://erp.example.com")
        self.token=QLineEdit(row["token"] if row else ""); self.token.setPlaceholderText("Bearer Token مشترک سایت"); self.token.setEchoMode(QLineEdit.Password)
        self.active=QCheckBox("این سایت و ارسال‌های آن فعال باشد"); self.active.setChecked(bool(row["active"]) if row else True)
        form.addRow("نام سیستم مدیریتی / ERP",self.name); form.addRow("آدرس پایه سایت",self.base_url); form.addRow("توکن دسترسی",self.token); form.addRow("",self.active)
        root.addWidget(info)
        endpoint_card=QFrame(objectName="wizardCard"); grid=QGridLayout(endpoint_card); grid.setContentsMargins(20,18,20,18); grid.setHorizontalSpacing(14); grid.setVerticalSpacing(12)
        header=QLabel("Endpointهای این سایت"); set_widget_style(header, 'font-size:18px;font-weight:700;color:#F3F7FF;background:transparent')
        sub=QLabel("مسیرها را با / وارد کنید؛ دامنه و توکن از اطلاعات بالای فرم استفاده می‌شوند."); set_widget_style(sub, 'color:#9CAFC8;background:transparent')
        grid.addWidget(header,0,0,1,3); grid.addWidget(sub,1,0,1,3)
        existing={r["kind"]:r for r in (db.site_endpoints(int(row["id"])) if row else [])}
        for index,(kind,label,default_path) in enumerate(self.ENDPOINTS,2):
            saved=existing.get(kind); enabled=QCheckBox(label); enabled.setChecked(bool(saved["active"]) if saved else False)
            path=QLineEdit(saved["path"] if saved and saved["path"] else ""); path.setPlaceholderText(default_path); path.setLayoutDirection(Qt.LeftToRight); path.setAlignment(Qt.AlignLeft); path.setEnabled(enabled.isChecked())
            preview=QLabel(); preview.setMinimumHeight(42); preview.setWordWrap(True); preview.setLayoutDirection(Qt.LeftToRight); preview.setTextInteractionFlags(Qt.TextSelectableByMouse); set_widget_style(preview, 'color:#62B8FF;background:#132842;border-radius:8px;padding:8px;font-size:12px')
            self.endpoint_inputs[kind]=(enabled,path,preview)
            enabled.toggled.connect(path.setEnabled); enabled.toggled.connect(self.update_previews); path.textChanged.connect(self.update_previews)
            grid.addWidget(enabled,index,0); grid.addWidget(path,index,1); grid.addWidget(preview,index,2)
        grid.setColumnStretch(1,2); grid.setColumnStretch(2,3); root.addWidget(endpoint_card,1)
        self.base_url.textChanged.connect(self.update_previews); self.update_previews()
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("ذخیره اتصال سایت"); buttons.button(QDialogButtonBox.Save).setObjectName("primary"); buttons.button(QDialogButtonBox.Cancel).setText("انصراف")
        buttons.accepted.connect(self.validate); buttons.rejected.connect(self.reject); root.addWidget(buttons)

    def update_previews(self):
        base=self.base_url.text().strip().rstrip("/")
        for enabled,path,preview in self.endpoint_inputs.values():
            preview.setText((base+"/"+path.text().strip().lstrip("/")) if base else "آدرس نهایی پس از ورود دامنه نمایش داده می‌شود")
            preview.setEnabled(enabled.isChecked())

    def values(self):
        endpoints=[{"kind":kind,"path":path.text().strip(),"active":enabled.isChecked()} for kind,(enabled,path,_) in self.endpoint_inputs.items() if enabled.isChecked()]
        return self.name.text().strip(),self.base_url.text().strip(),self.token.text().strip(),self.active.isChecked(),endpoints

    def validate(self):
        name,base,_,_,endpoints=self.values(); parts=urlsplit(base)
        if not name or parts.scheme not in ("http","https") or not parts.netloc:return message(self,"اطلاعات ناقص","نام ERP و یک آدرس پایه معتبر با http یا https وارد کنید.",True)
        if not endpoints:return message(self,"Endpoint لازم است","حداقل یک Endpoint را فعال و مسیر آن را وارد کنید.",True)
        if any(not item["path"] for item in endpoints):return message(self,"مسیر ناقص","برای تمام Endpointهای فعال مسیر وارد کنید.",True)
        self.accept()


class ReferenceApiDialog(QDialog):
    """Advanced editor kept outside the main settings page to reduce visual noise."""

    FIELDS = (
        ("admin_panel_url", "آدرس سامانه مرکزی", "https://erp.example.com"),
        ("transport_api_token", "توکن اتصال", "توکن دسترسی سامانه"),
    )

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        set_widget_style(self, app_style()); self.setLayoutDirection(Qt.RightToLeft)
        self.db = db
        self.inputs: dict[str, QLineEdit] = {}
        self.setWindowTitle("اتصال به سامانه مرکزی")
        self.setMinimumSize(660, 400)
        self.setLayoutDirection(Qt.RightToLeft)
        root = QVBoxLayout(self); root.setContentsMargins(24, 22, 24, 22); root.setSpacing(14)
        title = QLabel("یک اتصال برای تمام اطلاعات عملیاتی")
        set_widget_style(title, 'font-size:20px;font-weight:700;color:#F3F7FF;background:transparent')
        hint = QLabel("آدرس سامانه و توکن را یک‌بار ثبت کنید. کالاها، حوزه‌های تأمین، بارنامه‌ها و اسناد از همین اتصال دریافت می‌شوند.")
        if db.session:hint.setText('اتصال مرکزی از حساب ورود فعال گرفته می‌شود. برای تغییر سرور یا حساب، از بخش حساب کاربری خارج و دوباره وارد شوید. مقصدهای ارسال مستقل در تنظیمات قابل مدیریت‌اند.')
        hint.setWordWrap(True); set_widget_style(hint, 'color:#9CAFC8;background:#132842;padding:12px;border-radius:10px')
        root.addWidget(title); root.addWidget(hint)
        scroll = QScrollArea(objectName="wizardScroll"); scroll.setWidgetResizable(True); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        card = QFrame(objectName="wizardCard"); form = QFormLayout(card); form.setContentsMargins(20,18,20,18); form.setVerticalSpacing(12); form.setHorizontalSpacing(20)
        for key, label, placeholder in self.FIELDS:
            edit = QLineEdit(self.db.get_setting(key)); edit.setPlaceholderText(placeholder); edit.setClearButtonEnabled(True)
            if key.endswith("token"): edit.setEchoMode(QLineEdit.Password)
            if db.session:edit.setReadOnly(True)
            self.inputs[key] = edit; form.addRow(label, edit)
        scroll.setWidget(card); root.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("ذخیره اتصال‌ها"); buttons.button(QDialogButtonBox.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.Cancel).setText("انصراف")
        buttons.accepted.connect(self.validate); buttons.rejected.connect(self.reject); root.addWidget(buttons)

    def validate(self):
        values = self.values()
        parts=urlsplit(values['admin_panel_url'])
        if parts.scheme not in ('http','https') or not parts.netloc or parts.query or parts.fragment or parts.username:
            return message(self,'آدرس نامعتبر','آدرس پایه سامانه مانند https://erp.example.com را وارد کنید.',True)
        self.accept()

    def values(self) -> dict[str, str]:
        base=self.inputs['admin_panel_url'].text().strip().rstrip('/')
        token=self.inputs['transport_api_token'].text().strip()
        # Compatibility keys are derived, never separate operator configuration.
        return {'admin_panel_url':base,'transport_api_token':token,
                'scrap_api_url':base+'/api/transportation/reference/scrap-types','scrap_api_token':token,
                'company_api_url':base+'/api/transportation/reference/companies','company_api_token':token,
                'waybill_available_url':base+'/api/transportation/waybills/available',
                'waybill_reserve_url':base+'/api/transportation/waybills/{number}/reserve',
                'waybill_release_url':base+'/api/transportation/waybills/{number}/release'}


class ProcessingDialog(QDialog):
    """Two-step processing document wizard with the same safe scale capture contract."""
    def __init__(self,db:Database,parent=None):
        super().__init__(parent); self.db=db; self.latest=None; self.scale_connected=False; self.scale_message=""; self.locked=None; self.capture_data=None; self.submitted=False
        set_widget_style(self, app_style()); self.setLayoutDirection(Qt.RightToLeft)
        self.setWindowTitle("ثبت سند فرآوری"); self.setMinimumSize(880,650); self.resize(980,720); self.setLayoutDirection(Qt.RightToLeft)
        root=QVBoxLayout(self); root.setContentsMargins(24,22,24,22); root.setSpacing(14)
        hero=QFrame(objectName="documentsHero"); hr=QHBoxLayout(hero); title=QLabel("ویزارد ثبت سند فرآوری",objectName="heroTitle"); self.step_text=QLabel("مرحله ۱ از ۲ · ترکیب ورودی",objectName="heroSubtitle"); ht=QVBoxLayout(); ht.addWidget(title); ht.addWidget(self.step_text); hr.addLayout(ht); root.addWidget(hero)
        self.steps=QStackedWidget(); root.addWidget(self.steps,1)
        page1=QWidget(); p1=QVBoxLayout(page1); p1.addWidget(QLabel("ترکیب درصدی قراضه‌های ارسالی به مرکز فرآوری")); self.inputs=QTableWidget(0,3); self.inputs.setHorizontalHeaderLabels(["نوع قراضه","درصد از کل","حذف"]); self.inputs.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch); self.inputs.setColumnWidth(1,170); self.inputs.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeToContents); self.inputs.verticalHeader().setVisible(False); p1.addWidget(self.inputs)
        add=QPushButton("＋ افزودن قراضه ورودی",objectName="primary"); add.clicked.connect(self.add_input); p1.addWidget(add,0,Qt.AlignRight); self.total=QLabel("مجموع: ۰٪"); p1.addWidget(self.total); self.steps.addWidget(page1)
        page2=QWidget(); p2=QFormLayout(page2); p2.setVerticalSpacing(14); self.output=StyledComboBox(); self.output.addItem("انتخاب قراضه فرآوری‌شده…",None)
        for x in db.scraps():self.output.addItem(x["name"],dict(x))
        self.source_warehouse=SearchableComboBox("جستجوی انبار برداشت…");self.destination_warehouse=SearchableComboBox("جستجوی انبار دریافت…")
        for combo in (self.source_warehouse,self.destination_warehouse):
            combo.addItem("انتخاب انبار…",None)
            for warehouse in db.warehouses():
                if (warehouse.get("material_type") or {}).get("code")=="scrap":
                    combo.addItem(f"{(warehouse.get('factory') or {}).get('name','')} / {warehouse['name']}",warehouse["id"])
        if db.warehouses():
            p2.addRow("انبار برداشت مواد",self.source_warehouse);p2.addRow("انبار دریافت محصول",self.destination_warehouse)
        self.weight=QSpinBox(); self.weight.setRange(0,99999999); self.weight.setSuffix(" کیلوگرم"); self.weight.setAlignment(Qt.AlignRight)
        self.status=QLabel("در حال بررسی اتصال باسکول…"); self.live=QLabel("—"); set_widget_style(self.live, 'font-size:30px;font-weight:700;color:#62B8FF;padding:18px;background:#132842;border-radius:14px')
        self.lock=QPushButton("قفل وزن پایدار"); self.lock.clicked.connect(self.toggle_lock); self.capture=QPushButton("ثبت وزن پایدار",objectName="primary"); self.capture.clicked.connect(self.capture_weight)
        self.manual=QPushButton("ثبت وزن دستی",objectName="primary"); self.manual.clicked.connect(self.capture_manual); self.expert=QLineEdit(); self.notes=QTextEdit(); self.notes.setMaximumHeight(90)
        p2.addRow("قراضه خروجی",self.output); p2.addRow("وضعیت باسکول",self.status); p2.addRow("وزن زنده",self.live); p2.addRow("",self.lock); p2.addRow("",self.capture); p2.addRow("وزن دستی",self.weight); p2.addRow("",self.manual); p2.addRow("کارشناس کنترل کیفیت",self.expert); p2.addRow("توضیحات",self.notes); self.steps.addWidget(page2)
        footer=QHBoxLayout(); self.back=QPushButton("مرحله قبل"); self.back.clicked.connect(lambda:self.goto(0)); self.next=QPushButton("مرحله بعد",objectName="primary"); self.next.clicked.connect(self.next_step); self.save=QPushButton("ثبت سند و ارسال",objectName="primary"); self.save.clicked.connect(self.submit); cancel=QPushButton("انصراف"); cancel.clicked.connect(self.reject); footer.addWidget(cancel); footer.addStretch(); footer.addWidget(self.back); footer.addWidget(self.next); footer.addWidget(self.save); root.addLayout(footer)
        if not self.restore_draft():self.add_input(); self.goto(0)
        parser=Teta10AParser(int(db.get_setting("scale_maximum_weight_kg","60000"))); self.scale=SerialScale(db.get_setting("scale_port","COM1"),int(db.get_setting("scale_baudrate","9600")),parser=parser); claim_sidebar_scale(self); self.scale.start(self.on_reading,self.on_state)
        self.timer=QTimer(self); self.timer.timeout.connect(self.refresh_scale); self.timer.start(150)
        self.finished.connect(lambda _result: self.scale.stop())

    def _combo_index_for_id(self,combo,scrap_id):
        for index in range(combo.count()):
            data=combo.itemData(index)
            if isinstance(data,dict) and str(data.get("id"))==str(scrap_id):return index
        return -1
    def add_input(self,scrap_id=None,percentage=0):
        row=self.inputs.rowCount(); self.inputs.insertRow(row); combo=StyledComboBox()
        for x in self.db.scraps():combo.addItem(x["name"],dict(x))
        if scrap_id is not None:
            index=self._combo_index_for_id(combo,scrap_id)
            if index>=0:combo.setCurrentIndex(index)
        pct=QDoubleSpinBox(); pct.setRange(.001,100); pct.setDecimals(3); pct.setSuffix(" ٪"); pct.setValue(float(percentage or .001)); pct.valueChanged.connect(self.update_total)
        delete=QPushButton("حذف",objectName="danger"); delete.setFixedHeight(40)
        delete.clicked.connect(lambda _=False,w=delete:self.remove_input(w))
        self.inputs.setCellWidget(row,0,combo); self.inputs.setCellWidget(row,1,pct)
        holder=QWidget(); button_layout=QHBoxLayout(holder); button_layout.setContentsMargins(6,6,6,6); button_layout.setAlignment(Qt.AlignCenter); button_layout.addWidget(delete)
        self.inputs.setCellWidget(row,2,holder)
        fit_table_controls(self.inputs,(2,))
        # Include the table item's own padding as well as the host layout.
        self.inputs.horizontalHeader().setSectionResizeMode(2,QHeaderView.Fixed)
        self.inputs.setColumnWidth(2,max(112,holder.minimumWidth()+24))
        self.inputs.setRowHeight(row,max(88,holder.sizeHint().height()+24))
        self.update_total()
    def remove_input(self,button):
        for row in range(self.inputs.rowCount()):
            if self.inputs.cellWidget(row,2) is button or button in self.inputs.cellWidget(row,2).findChildren(QPushButton):self.inputs.removeRow(row);break
        self.update_total()
    def update_total(self):
        total=sum(self.inputs.cellWidget(r,1).value() for r in range(self.inputs.rowCount())); self.total.setText(f"مجموع: {fa_digits(f'{total:g}')}٪")
    def draft_payload(self):
        inputs=[]
        for row in range(self.inputs.rowCount()):
            scrap=self.inputs.cellWidget(row,0).currentData()
            if scrap:inputs.append({"scrap_type_id":scrap.get("id"),"percentage":self.inputs.cellWidget(row,1).value()})
        output=self.output.currentData()
        capture=dict(self.capture_data or {})
        return {"source_warehouse_id":self.source_warehouse.currentData(),"destination_warehouse_id":self.destination_warehouse.currentData(),"inputs":inputs,"output_scrap_type_id":output.get("id") if isinstance(output,dict) else None,
            "output_weight_kg":self.weight.value(),"capture_data":capture,"quality_expert_name":self.expert.text().strip(),
            "notes":self.notes.toPlainText(),"weight_locked":bool(self.locked)}
    @staticmethod
    def _draft_has_data(payload):
        meaningful_inputs=any(float(row.get("percentage") or 0)>.001 for row in payload.get("inputs",[]))
        return bool(meaningful_inputs or payload.get("output_scrap_type_id") or payload.get("output_weight_kg") or payload.get("quality_expert_name") or payload.get("notes") or payload.get("capture_data"))
    def persist_draft(self):
        if self.submitted:return
        payload=self.draft_payload()
        if self._draft_has_data(payload):self.db.save_processing_draft(self.steps.currentIndex(),payload)
        else:self.db.delete_processing_draft()
    def restore_draft(self):
        draft=self.db.processing_draft()
        if not draft:return False
        payload=draft.get("payload") or {}
        for combo,key in ((self.source_warehouse,"source_warehouse_id"),(self.destination_warehouse,"destination_warehouse_id")):
            idx=combo.findData(payload.get(key))
            if idx>=0:combo.setCurrentIndex(idx)
        for row in payload.get("inputs") or []:self.add_input(row.get("scrap_type_id"),row.get("percentage"))
        if self.inputs.rowCount()==0:self.add_input()
        output_id=payload.get("output_scrap_type_id")
        if output_id is not None:
            index=self._combo_index_for_id(self.output,output_id)
            if index>=0:self.output.setCurrentIndex(index)
        self.weight.setValue(int(round(float(payload.get("output_weight_kg") or 0))))
        self.capture_data=payload.get("capture_data") or None
        self.expert.setText(str(payload.get("quality_expert_name") or "")); self.notes.setPlainText(str(payload.get("notes") or ""))
        self.goto(max(0,min(1,int(draft.get("current_step") or 0))))
        self.step_text.setText(self.step_text.text()+" · پیش‌نویس بازیابی شد")
        return True
    def goto(self,step):self.steps.setCurrentIndex(step); self.step_text.setText(("مرحله ۱ از ۲ · ترکیب ورودی","مرحله ۲ از ۲ · خروجی و توزین")[step]); self.back.setVisible(step>0); self.next.setVisible(step==0); self.save.setVisible(step==1)
    def next_step(self):
        if self.inputs.rowCount()<1 or abs(sum(self.inputs.cellWidget(r,1).value() for r in range(self.inputs.rowCount()))-100)>.001:return message(self,"ترکیب ورودی","مجموع درصد ورودی‌ها باید دقیقاً ۱۰۰ درصد باشد.",True)
        ids=[self.inputs.cellWidget(r,0).currentData()["id"] for r in range(self.inputs.rowCount())]
        if len(ids)!=len(set(ids)):return message(self,"ترکیب ورودی","هر قراضه فقط یک‌بار قابل انتخاب است.",True)
        self.goto(1)
    def on_reading(self,r):self.latest=r
    def on_state(self,c,t):self.scale_connected=c; self.scale_message=t
    def refresh_scale(self):
        self.status.setText("● "+(self.scale_message or "وضعیت نامشخص")); self.weight.setVisible(not self.scale_connected); self.manual.setVisible(not self.scale_connected); self.capture.setVisible(self.scale_connected); self.lock.setVisible(self.scale_connected); self.live.setVisible(self.scale_connected)
        if self.latest:
            reading=self.locked or self.latest; self.live.setText(fa_digits(f"{round(reading.weight_kg):,} کیلوگرم")+(" · قفل‌شده" if self.locked else (" · پایدار" if reading.stable else " · در حال تغییر")))
    def toggle_lock(self):
        if self.locked:self.locked=None; self.lock.setText("قفل وزن پایدار");return
        if not self.latest or not self.latest.stable:return message(self,"قفل وزن","فقط وزن پایدار قابل قفل‌کردن است.",True)
        self.locked=self.latest; self.lock.setText("باز کردن قفل وزن")
    def capture_weight(self):
        reading=self.locked or self.latest
        if not self.scale_connected or not reading or not reading.stable:return message(self,"ثبت وزن","وزن پایدار و اتصال معتبر باسکول لازم است.",True)
        value=round(reading.weight_kg); self.weight.setValue(value); self.capture_data={"source":"device","stable":True,"manual_reason":"","raw_payload":reading.raw_payload}; message(self,"وزن ثبت شد",f"وزن {fa_digits(f'{value:,}')} کیلوگرم برای خروجی فرآوری ثبت شد.")
    def capture_manual(self):
        if self.weight.value()<=0:return message(self,"وزن دستی","وزن خروجی را وارد کنید.",True)
        self.capture_data={"source":"manual","stable":False,"manual_reason":self.scale_message or "ارتباط معتبر با باسکول برقرار نیست.","raw_payload":""}; message(self,"وزن ثبت شد","وزن دستی با علت تشخیصی باسکول ثبت شد.")
    def submit(self):
        if self.db.warehouses() and (not self.source_warehouse.currentData() or not self.destination_warehouse.currentData()):return message(self,"انبارهای فرآوری","انبار برداشت مواد و دریافت محصول را مشخص کنید.",True)
        if self.output.currentData() is None:return message(self,"قراضه خروجی","نوع قراضه فرآوری‌شده را انتخاب کنید.",True)
        if not self.capture_data or self.weight.value()<=0:return message(self,"وزن خروجی","ابتدا وزن خروجی را ثبت کنید.",True)
        if not self.expert.text().strip():return message(self,"کارشناس","نام کارشناس کنترل کیفیت را وارد کنید.",True)
        inputs=[]
        for r in range(self.inputs.rowCount()):
            scrap=self.inputs.cellWidget(r,0).currentData(); inputs.append({"scrap_type_id":scrap["id"],"scrap_name":scrap["name"],"percentage":self.inputs.cellWidget(r,1).value()})
        output=self.output.currentData(); now=datetime.now().astimezone(); event=str(uuid.uuid4()); number=f"PRC-LOCAL-{uuid.uuid4().hex[:8].upper()}"
        self.db.create_processing_document({"source_warehouse_id":self.source_warehouse.currentData(),"destination_warehouse_id":self.destination_warehouse.currentData(),"event_id":event,"document_number":number,"processed_at":now.isoformat(timespec="seconds"),"inputs":inputs,"output_scrap_type_id":output["id"],"output_scrap_name":output["name"],"output_weight_kg":self.weight.value(),"quality_expert_name":self.expert.text().strip(),"notes":self.notes.toPlainText().strip(),"weight_source":self.capture_data["source"],"weight_stable":self.capture_data["stable"],"manual_reason":self.capture_data["manual_reason"],"raw_payload":self.capture_data["raw_payload"]}); self.submitted=True; self.db.delete_processing_draft(); self.accept()
    def reject(self):
        self.persist_draft(); super().reject()
    def closeEvent(self,event):
        self.persist_draft()
        try:self.scale.stop()
        except Exception:pass
        super().closeEvent(event)


class WaybillDialog(QDialog):
    """Offline-first waybill issuance form with the same Iranian plate control."""
    def __init__(self, db: Database, parent=None):
        super().__init__(parent); self.db=db; self.created_id=None; self.event_id=str(uuid.uuid4())
        self.setObjectName("waybillDialog")
        self.setWindowTitle("صدور بارنامه جدید"); self.setMinimumSize(900,650); self.resize(1000,780); self.setLayoutDirection(Qt.RightToLeft)
        set_widget_style(self, app_style())
        root=QVBoxLayout(self); root.setContentsMargins(24,22,24,22); root.setSpacing(16)

        hero=QFrame(objectName="waybillHero"); hero_row=QHBoxLayout(hero); hero_row.setContentsMargins(22,18,22,18); hero_row.setSpacing(16)
        hero_mark=QLabel(objectName="waybillHeroMark"); hero_mark.setAlignment(Qt.AlignCenter); hero_mark.setFixedSize(62,62)
        set_navigation_icon(hero_mark, Path(__file__).resolve().parent / "assets" / "icons" / "waybills.svg", 30)
        hero_text=QVBoxLayout(); title=QLabel("صدور بارنامه حمل کالا", objectName="waybillHeroTitle"); title.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        hint=QLabel("ثبت اطلاعات مبدأ، راننده و ناوگان • شماره نهایی به‌صورت یکتا از سامانه مرکزی دریافت می‌شود.", objectName="waybillHeroText"); hint.setWordWrap(True); hint.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        hero_text.addWidget(title); hero_text.addWidget(hint); hero_row.addWidget(hero_mark); hero_row.addLayout(hero_text,1); root.addWidget(hero)

        scroll=QScrollArea(objectName="waybillScroll"); scroll.setWidgetResizable(True); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content=QWidget(); content.setLayoutDirection(Qt.RightToLeft)
        content_layout=QVBoxLayout(content); content_layout.setContentsMargins(2,2,2,2); content_layout.setSpacing(14)
        self.material_type = StyledComboBox(); self.material_type.addItem('نوع کالا را انتخاب کنید…', None)
        for material in db.material_types():self.material_type.addItem(material['name'], material)
        self.material_type.currentIndexChanged.connect(self.refresh_material_companies)
        self.company=SearchableComboBox("جستجو و انتخاب شرکت تأمین‌کننده…")
        self.company.lineEdit().setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        for row in db.companies():self.company.addItem(f"{row['code']} — {row['name']}" if row['code'] else row['name'],dict(row))
        self.weight=QSpinBox(); self.weight.setRange(0,100_000_000); self.weight.setSuffix(" کیلوگرم"); self.weight.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        self.driver=QLineEdit(); self.phone=QLineEdit(); self.plate=IranTrailerPlate()
        for editor in (self.driver,self.phone):
            editor.setLayoutDirection(Qt.RightToLeft); editor.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        provinces,self.cities=load_iran_locations(); self.province=SearchableComboBox("جستجو و انتخاب استان…")
        self.province.lineEdit().setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        for row in provinces:self.province.addItem(row["name"],int(row["id"]))
        self.province.setCurrentIndex(-1); self.city=SearchableComboBox("ابتدا استان را انتخاب کنید…"); self.city.lineEdit().setAlignment(Qt.AlignRight | Qt.AlignAbsolute); self.city.setEnabled(False); self.province.currentIndexChanged.connect(self._province_changed)
        self.freight_by_contractor=QCheckBox("کرایه با پیمانکار است")
        self.no_origin_weighbridge=QCheckBox("بارنامه باسکول مبدأ ندارد")
        self.freight_by_contractor.setToolTip("مبلغ کرایه صفر ثبت می‌شود و رسید پرداخت صادر نمی‌شود.")
        self.no_origin_weighbridge.setToolTip("وزن مبدأ ثبت نمی‌شود و گواهی عدم تطابق فقط به‌صورت دستی قابل صدور است.")
        self.freight=QSpinBox(); self.freight.setRange(0,2_000_000_000); self.freight.setSuffix(" ریال"); self.freight.setGroupSeparatorShown(True); self.freight.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        self.freight_by_contractor.toggled.connect(lambda checked:self.freight.setEnabled(not checked))
        self.no_origin_weighbridge.toggled.connect(lambda checked:self.weight.setEnabled(not checked))
        def field(label_text, widget, hint_text=""):
            box=QFrame(objectName="waybillField"); box_layout=QVBoxLayout(box); box_layout.setContentsMargins(14,9,14,9); box_layout.setSpacing(2)
            label=QLabel(label_text,objectName="waybillFieldLabel"); label.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
            box_layout.addWidget(label); box_layout.addWidget(widget)
            if hint_text:
                small=QLabel(hint_text,objectName="waybillSectionHint"); small.setAlignment(Qt.AlignRight | Qt.AlignAbsolute); box_layout.addWidget(small)
            return box

        basic=QFrame(objectName="waybillSection"); basic_layout=QVBoxLayout(basic); basic_layout.setContentsMargins(20,17,20,19); basic_layout.setSpacing(12)
        basic_title=QLabel("اطلاعات بار و طرف معامله",objectName="waybillSectionTitle"); basic_title.setAlignment(Qt.AlignRight | Qt.AlignAbsolute); basic_layout.addWidget(basic_title)
        basic_grid=QGridLayout(); basic_grid.setHorizontalSpacing(12); basic_grid.setVerticalSpacing(12)
        self.refresh_material_companies()
        basic_grid.addWidget(field("نوع کالا",self.material_type),0,0,1,2)
        basic_grid.addWidget(field("شرکت تأمین‌کننده",self.company),1,0)
        self.weight_field=field("وزن خالص بار در مبدأ",self.weight,"وزن ثبت‌شده در نقطه بارگیری")
        basic_grid.addWidget(self.weight_field,1,1)
        basic_layout.addLayout(basic_grid); basic_layout.addWidget(self.no_origin_weighbridge); content_layout.addWidget(basic)

        transport=QFrame(objectName="waybillSection"); transport_layout=QVBoxLayout(transport); transport_layout.setContentsMargins(20,17,20,19); transport_layout.setSpacing(12)
        transport_title=QLabel("راننده و ناوگان",objectName="waybillSectionTitle"); transport_title.setAlignment(Qt.AlignRight | Qt.AlignAbsolute); transport_layout.addWidget(transport_title)
        transport_grid=QGridLayout(); transport_grid.setHorizontalSpacing(12); transport_grid.setVerticalSpacing(12)
        transport_grid.addWidget(field("نام و نام خانوادگی راننده",self.driver),0,0)
        transport_grid.addWidget(field("شماره موبایل راننده",self.phone),0,1)
        transport_layout.addLayout(transport_grid)
        plate_panel=QFrame(objectName="platePanel"); plate_layout=QVBoxLayout(plate_panel); plate_layout.setContentsMargins(18,13,18,16); plate_layout.setSpacing(8)
        plate_title=QLabel("پلاک تریلی ایران",objectName="platePanelTitle"); plate_title.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        plate_hint=QLabel("پلاک را از چپ به راست و دقیقاً مطابق کارت ناوگان وارد کنید.",objectName="platePanelHint"); plate_hint.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        plate_row=QHBoxLayout(); plate_row.addStretch(1); plate_row.addWidget(self.plate); plate_row.addStretch(1)
        plate_layout.addWidget(plate_title); plate_layout.addWidget(plate_hint); plate_layout.addLayout(plate_row); transport_layout.addWidget(plate_panel)
        content_layout.addWidget(transport)

        origin=QFrame(objectName="waybillSection"); origin_layout=QVBoxLayout(origin); origin_layout.setContentsMargins(20,17,20,19); origin_layout.setSpacing(12)
        origin_title=QLabel("مبدأ حمل و هزینه",objectName="waybillSectionTitle"); origin_title.setAlignment(Qt.AlignRight | Qt.AlignAbsolute); origin_layout.addWidget(origin_title)
        origin_grid=QGridLayout(); origin_grid.setHorizontalSpacing(12); origin_grid.setVerticalSpacing(12)
        origin_grid.addWidget(field("استان مبدأ",self.province),0,0); origin_grid.addWidget(field("شهر مبدأ",self.city),0,1)
        self.freight_field=field("مبلغ کرایه",self.freight)
        origin_grid.addWidget(self.freight_field,1,0,1,2); origin_layout.addLayout(origin_grid); origin_layout.addWidget(self.freight_by_contractor); content_layout.addWidget(origin)
        content_layout.addStretch(1); content.setObjectName("waybillContent"); scroll.setWidget(content); root.addWidget(scroll,1)
        self.submission_status=QLabel(""); self.submission_status.setWordWrap(True); root.addWidget(self.submission_status)
        actions=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel); actions.button(QDialogButtonBox.Save).setText("صدور بارنامه"); actions.button(QDialogButtonBox.Save).setObjectName("primary"); actions.button(QDialogButtonBox.Cancel).setText("انصراف"); actions.accepted.connect(self.submit); actions.rejected.connect(self.reject); root.addWidget(actions)
        self.form_scroll=scroll; self.form_actions=actions; self.restore_draft()

    def refresh_material_companies(self):
        previous = (self.company.currentData() or {}).get('id')
        self.company.clear(); self.company.addItem('جست‌وجو با کد یا نام شرکت…',None)
        material = self.material_type.currentData()
        self.company.setEnabled(bool(material))
        for row in (self.db.material_companies(material['id']) if material else []):
            self.company.addItem(f"{row.get('code') or '—'} — {row['name']}",row)
            if row['id'] == previous:self.company.setCurrentIndex(self.company.count()-1)

    def draft_payload(self):
        company=self.company.currentData() or {}
        return {"material_type_id":(self.material_type.currentData() or {}).get("id"),"event_id":self.event_id,"issued_at":getattr(self,"issued_at",None),"supplier_company_id":company.get("id"),"weight":self.weight.value(),"driver":self.driver.text(),"phone":self.phone.text(),"plate":self.plate.value(),"province":self.province.currentText(),"city":self.city.currentText(),"freight":self.freight.value(),"freight_by_contractor":bool(getattr(self,"freight_by_contractor",None) and self.freight_by_contractor.isChecked()),"no_origin_weighbridge":bool(getattr(self,"no_origin_weighbridge",None) and self.no_origin_weighbridge.isChecked())}

    def persist_draft(self):
        meaningful=any((self.material_type.currentData(),self.company.currentData(),self.driver.text().strip(),self.phone.text().strip(),self.weight.value()))
        if meaningful:self.db.set_setting("waybill_draft",json.dumps(self.draft_payload(),ensure_ascii=False))

    def restore_draft(self):
        try:data=json.loads(self.db.get_setting("waybill_draft","{}") or "{}")
        except json.JSONDecodeError:return
        if not data:return
        self.event_id=data.get("event_id") or self.event_id; self.issued_at=data.get("issued_at"); self.weight.setValue(int(data.get("weight") or 1)); self.driver.setText(data.get("driver", "")); self.phone.setText(data.get("phone","")); self.plate.set_value(data.get("plate","")); self.freight.setValue(int(data.get("freight") or 0))
        self.freight_by_contractor.setChecked(bool(data.get("freight_by_contractor"))); self.no_origin_weighbridge.setChecked(bool(data.get("no_origin_weighbridge")))
        for index in range(self.material_type.count()):
            material = self.material_type.itemData(index)
            if material and str(material['id']) == str(data.get('material_type_id')):self.material_type.setCurrentIndex(index);break
        supplier_company_id=str(data.get("supplier_company_id") or "")
        for i in range(self.company.count()):
            row=self.company.itemData(i)
            if row and str(row.get("id"))==supplier_company_id:self.company.setCurrentIndex(i); break
        province_index=self.province.findText(data.get("province","")); self.province.setCurrentIndex(province_index)
        city_index=self.city.findText(data.get("city","")); self.city.setCurrentIndex(city_index)

    def reject(self):
        self.persist_draft(); super().reject()

    def closeEvent(self,event):
        self.persist_draft(); super().closeEvent(event)

    def _province_changed(self):
        self.city.clear(); province_id=self.province.currentData()
        if province_id is not None:
            for row in self.cities.get(int(province_id),[]):self.city.addItem(row["name"],row["id"])
        self.city.setCurrentIndex(-1); self.city.setEnabled(province_id is not None)

    def submit(self):
        material=self.material_type.currentData()
        if not material:return message(self,"نوع کالا","نوع کالای بارنامه را انتخاب کنید.",True)
        company=self.company.currentData()
        if not company:return message(self,"اطلاعات ناقص","شرکت تأمین‌کننده را انتخاب کنید.",True)
        if not self.driver.text().strip() or not self.phone.text().strip():return message(self,"اطلاعات ناقص","نام و شماره موبایل راننده را وارد کنید.",True)
        if not self.plate.is_complete():return message(self,"پلاک ناقص","پلاک تریلی را کامل وارد کنید.",True)
        if self.province.currentData() is None or self.city.currentData() is None:return message(self,"مبدأ ناقص","استان و شهر مبدأ را انتخاب کنید.",True)
        endpoint=self.db.primary_waybill_endpoint()
        if not endpoint:
            return message(self,"صدور بارنامه","هیچ مقصد فعال برای صدور بارنامه تعریف نشده است. ابتدا آدرس API بارنامه را در تنظیمات ثبت کنید.",True)
        self.issued_at=getattr(self,"issued_at",None) or datetime.now().astimezone().isoformat(timespec="seconds")
        self.persist_draft()
        contractor=bool(getattr(self,"freight_by_contractor",None) and self.freight_by_contractor.isChecked())
        no_origin=bool(getattr(self,"no_origin_weighbridge",None) and self.no_origin_weighbridge.isChecked())
        data={"material_type_id":material["id"],"material_name":material["name"],"event_id":self.event_id,"company":company,"origin_net_weight_kg":0 if no_origin else self.weight.value(),"driver_name":self.driver.text().strip(),"driver_phone":self.phone.text().strip(),"trailer_plate":self.plate.value(),"origin_province":self.province.currentText(),"origin_city":self.city.currentText(),"freight_amount":0 if contractor else self.freight.value(),"freight_by_contractor":contractor,"no_origin_weighbridge":no_origin,"issued_at":self.issued_at}
        QApplication.setOverrideCursor(Qt.WaitCursor)
        accepted_number=0
        try:
            ok,code,error,response=send_webhook(endpoint["url"],self.db.delivery_token(endpoint["url"],endpoint["token"]),self.db.waybill_payload_from_data(data))
            if not ok:
                self.db.record_submission_error(self.event_id,error,code)
                self.submission_status.setText("ثبت بارنامه انجام نشد: "+str(error)+"؛ اطلاعات برای تلاش دوباره حفظ شد.")
                return
            response_data=response.get("data",response) if isinstance(response,dict) else {}
            official_number=int(response_data.get("number") or 0)
            if official_number <= 0:
                self.db.record_submission_error(self.event_id,"سامانه مرکزی شماره قطعی بارنامه را برنگرداند.",code)
                self.submission_status.setText("بارنامه هنوز ثبت نشده است؛ اطلاعات فرم برای تلاش دوباره حفظ شده است.")
                return
            if int(response_data.get('material_type_id') or 0) != int(material['id']):
                self.db.record_submission_error(self.event_id,"نوع کالای پاسخ سامانه با بارنامه تطابق ندارد.",code)
                self.submission_status.setText("نوع کالای بارنامه از سامانه تأیید نشد؛ فرم برای پیگیری حفظ شده است."); return
            accepted_number=official_number
            data.update(freight_by_contractor=response_data.get("freight_by_contractor",data["freight_by_contractor"]),freight_amount=response_data.get("freight_amount",data["freight_amount"]),queue_number=response_data.get("queue_number"),queue_day=response_data.get("queue_day"),material_type_id=response_data.get("material_type_id"),material_policy=response_data.get("material_policy"),material_movement=response_data.get("movement_type"))
            self.created_id=self.db.create_waybill(data,official_number=official_number)
            self.db.mark_waybill_endpoint_synced(self.created_id,int(endpoint["id"]),int(code or 201))
        except Exception as exc:
            self.db.record_submission_error(self.event_id,str(exc))
            if accepted_number:
                self.submission_status.setText(f"بارنامه شماره {accepted_number} در سرور ثبت شده؛ ذخیره محلی کامل نشد: {exc}. با همین فرم دوباره تلاش کنید تا همان بارنامه بازیابی شود.")
            else:
                self.submission_status.setText("ثبت بارنامه انجام نشد: "+str(exc)+"؛ اطلاعات فرم حفظ شد.")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.db.set_setting("waybill_draft",""); self.accept()


class MainWindow(QMainWindow):
    document_dialog_class = DocumentDialog
    def __init__(self, db: Database):
        if not db.session:raise PermissionError("ابتدا وارد حساب کاربری شوید.")
        super().__init__(); self.db = db; self.sync_thread = None; self.server_sync_thread = None; self.document_page = 0; self.document_page_size = 10
        self._logging_out=False; self._session_check=None; self._logout_worker=None; self.reference_thread=None; self._reference_sync_sites=set()
        self.selected_document_ids: set[int] = set()
        self.selected_waybill_ids: set[int] = set()
        self.selected_processing_ids: set[int] = set()
        self.setWindowTitle("مگاسنج | مدیریت هوشمند باسکول"); self.resize(1240, 760); self.setMinimumSize(980, 650)
        self.assets = Path(__file__).resolve().parent / "assets" / "icons"
        assets = self.assets
        themed_style = app_style(assets)
        self._dark_mode = current_theme() == "dark"
        set_widget_style(self, themed_style); self.setLayoutDirection(Qt.RightToLeft)
        root = QWidget(objectName="root"); self.setCentralWidget(root); outer = QHBoxLayout(root); outer.setContentsMargins(18,18,18,18); outer.setSpacing(16)
        sidebar = QFrame(objectName="sidebar"); sidebar.setFixedWidth(224); sidebar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding); side = QVBoxLayout(sidebar); side.setContentsMargins(13,13,13,13); side.setSpacing(8)
        brand_card = QFrame(objectName="brandCard"); brand_layout = QVBoxLayout(brand_card); brand_layout.setContentsMargins(12,14,12,14); brand_layout.setSpacing(5)
        brand_icon = QLabel(objectName="brandIcon"); brand_icon.setFixedSize(54,54)
        icon_path = Path(__file__).resolve().parent / "assets" / "brand" / "megasanj-icon.png"
        brand_icon.setPixmap(QPixmap(str(icon_path)).scaled(52,52,Qt.KeepAspectRatio,Qt.SmoothTransformation))
        brand_text = QVBoxLayout(); brand_text.setSpacing(0)
        brand_name = QLabel("مگاسنج", objectName="brandName"); brand_name.setWordWrap(False)
        brand_tagline = QLabel("مدیریت هوشمند باسکول", objectName="brandTagline"); brand_tagline.setWordWrap(False)
        brand_text.addWidget(brand_name); brand_text.addWidget(brand_tagline)
        brand_controls=QHBoxLayout(); brand_controls.addStretch(); self.theme_toggle=ThemeToggle(); brand_controls.addWidget(self.theme_toggle); brand_controls.addStretch(); brand_layout.addLayout(brand_controls)
        brand_layout.addWidget(brand_icon, 0, Qt.AlignHCenter); brand_layout.addLayout(brand_text); brand_name.setAlignment(Qt.AlignCenter); brand_tagline.setAlignment(Qt.AlignCenter); side.addWidget(brand_card)
        self.docs_nav = SidebarNavButton("اسناد باسکول", assets / "nav-weighbridge.svg"); self.docs_nav.setChecked(True)
        self.waybills_nav = SidebarNavButton("بارنامه‌ها",assets / "nav-waybills.svg")
        self.processing_nav = SidebarNavButton("اسناد فرآوری",assets / "nav-processing.svg")
        self.queue_nav = SidebarNavButton("نوبت‌دهی تریلی‌ها",assets / "nav-queue.svg"); side.addWidget(self.queue_nav)
        self.settings_nav = SidebarNavButton("تنظیمات", assets / "nav-settings.svg")
        side.addWidget(self.docs_nav); side.addWidget(self.waybills_nav); side.addWidget(self.processing_nav); side.addWidget(self.settings_nav)
        self.account_avatar=QLabel(); self.account_avatar.setFixedSize(48,48); self.account_avatar.setAlignment(Qt.AlignCenter); set_widget_style(self.account_avatar, 'background:#1d4969;color:white;border-radius:24px;font-size:22px;font-weight:800')
        self.account_name=QLabel(""); self.account_name.setWordWrap(True); set_widget_style(self.account_name, 'color:white;font-weight:700;padding-top:8px')
        self.account_role=QLabel(""); self.account_role.setWordWrap(True); set_widget_style(self.account_role, 'color:#9CAFC8')
        profile=QPushButton("اطلاعات حساب"); profile.clicked.connect(self.show_account)
        logout=QPushButton("خروج از حساب"); logout.clicked.connect(self.logout)
        self.account_name.setAlignment(Qt.AlignCenter); self.account_role.setAlignment(Qt.AlignCenter)
        side.addWidget(self.account_avatar,0,Qt.AlignHCenter); side.addWidget(self.account_name); side.addWidget(self.account_role); side.addWidget(profile); side.addWidget(logout)
        side.addSpacing(18)
        side_stats=QFrame(objectName="sidebarStats"); stats_layout=QVBoxLayout(side_stats); stats_layout.setContentsMargins(12,12,12,12); stats_layout.setSpacing(7)
        stats_layout.addWidget(QLabel("نمای کلی سامانه", objectName="sidebarStatsTitle"))
        self.sidebar_status=QLabel("● آماده دریافت سند", objectName="sidebarStatsText"); stats_layout.addWidget(self.sidebar_status)
        mini=QHBoxLayout(); mini.setSpacing(6)
        self.sidebar_count=QLabel("۰\nاسناد", objectName="sidebarMiniBlue"); self.sidebar_pending=QLabel("۰\nدر صف", objectName="sidebarMiniAmber"); self.sidebar_synced=QLabel("۰\nارسال‌شده", objectName="sidebarMiniGreen")
        for item in (self.sidebar_count,self.sidebar_pending,self.sidebar_synced): item.setAlignment(Qt.AlignCenter); mini.addWidget(item,1)
        stats_layout.addLayout(mini); side.addWidget(side_stats)
        side.addSpacing(12)
        scale_indicator=QFrame(objectName="sidebarScaleStatus"); indicator_layout=QHBoxLayout(scale_indicator); indicator_layout.setContentsMargins(10,10,10,10); indicator_layout.setSpacing(8)
        self.sidebar_scale_dot=QFrame(objectName="sideInfoDot"); self.sidebar_scale_dot.setFixedSize(8,8); set_widget_style(self.sidebar_scale_dot,"background:#9CAFC8;border-radius:4px;"); self.sidebar_scale_status=QLabel("در حال بررسی باسکول",objectName="sideInfoValue"); self.sidebar_scale_status.setWordWrap(True)
        indicator_layout.addWidget(self.sidebar_scale_dot); indicator_layout.addWidget(self.sidebar_scale_status,1); side.addWidget(scale_indicator)
        side.addStretch(1)
        self.side_last_sync=QLabel(self); self.side_last_sync.hide()
        self.sync_label=QLabel(self); self.sync_label.hide()
        self._sidebar_scale=None; self._sidebar_scale_owner=None; self._sidebar_scale_state=(False,"در حال بررسی باسکول"); self._scale_retry_at=0
        # A process-local gate: outbound queues stay paused until this app
        # session has first reconciled its cache with the authoritative server.
        self.server_sync_session_ready = False
        self.stack = QStackedWidget(); self.stack.addWidget(self.build_documents()); self.stack.addWidget(self.build_waybills()); self.stack.addWidget(self.build_processing()); self.stack.addWidget(self.build_settings())
        self.queue_page=QueuePage(db); self.stack.addWidget(self.queue_page); self.queue_nav.clicked.connect(lambda:self.navigate(4))
        side.setSizeConstraint(QVBoxLayout.SetMinimumSize)
        sidebar_scroll=QScrollArea(); sidebar_scroll.setWidgetResizable(True); sidebar_scroll.setFixedWidth(234)
        set_widget_style(sidebar_scroll, 'QScrollArea { background:#081426; border:0; }'); sidebar_scroll.setFrameShape(QFrame.NoFrame); sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sidebar_scroll.setWidget(sidebar)
        sidebar_host=QWidget(); sidebar_host_layout=QVBoxLayout(sidebar_host); sidebar_host_layout.setContentsMargins(0,0,0,0); sidebar_host_layout.addWidget(sidebar_scroll,1)
        outer.addWidget(sidebar_host, 0); outer.addWidget(self.stack, 1)
        self.docs_nav.clicked.connect(lambda: self.navigate(0)); self.waybills_nav.clicked.connect(lambda:self.navigate(1)); self.processing_nav.clicked.connect(lambda:self.navigate(2)); self.settings_nav.clicked.connect(lambda: self.navigate(3))
        self.apply_access()
        self.session_timer=QTimer(self); self.session_timer.timeout.connect(self.check_session); self.session_timer.start(60000)
        self.auto_timer = QTimer(self); self.auto_timer.timeout.connect(self.start_sync); self.auto_timer.start(120000)
        self.server_sync_timer = QTimer(self); self.server_sync_timer.timeout.connect(self.start_server_sync); self.server_sync_timer.start(60000)
        self.refresh_documents(); self.refresh_waybills(); self.refresh_processing(); self.refresh_endpoints()
        QTimer.singleShot(250,self.start_server_sync)
        QTimer.singleShot(0,self.start_reference_sync)
        # Send backfilled/offline records immediately after startup instead of
        # leaving them waiting for the first two-minute timer tick.
        QTimer.singleShot(0,self.start_sync)
        self.scale_status_timer=QTimer(self); self.scale_status_timer.timeout.connect(self.refresh_scale_status); self.scale_status_timer.start(250); QTimer.singleShot(0,self.refresh_scale_status)

    def stop_sidebar_scale(self):
        if self._sidebar_scale:
            self._sidebar_scale.stop(); self._sidebar_scale=None

    def claim_sidebar_scale(self, dialog):
        # The sidebar and weighing form must never open the same serial port.
        self.stop_sidebar_scale(); self._sidebar_scale_owner=dialog
        dialog.finished.connect(lambda _result:self.release_sidebar_scale(dialog))

    def release_sidebar_scale(self, dialog):
        if self._sidebar_scale_owner is dialog:
            self._sidebar_scale_owner=None; self._scale_retry_at=0

    def refresh_scale_status(self):
        if self._logging_out or getattr(self,"_closing",False):return
        owner=self._sidebar_scale_owner
        if owner is not None:
            connected=owner.scale_connected; detail=owner.scale_message or "در حال بررسی باسکول"
        else:
            port=self.db.get_setting("scale_port", "COM1").strip()
            if not port:
                self.stop_sidebar_scale(); self._sidebar_scale_state=(False,"پورت باسکول تنظیم نشده")
            else:
                if self._sidebar_scale and self._sidebar_scale.config["port"]!=port:
                    self.stop_sidebar_scale(); self._scale_retry_at=0
                running=self._sidebar_scale and self._sidebar_scale.is_running()
                if not running and monotonic()>=self._scale_retry_at:
                    self.stop_sidebar_scale(); self._scale_retry_at=monotonic()+3
                    self._sidebar_scale_state=(False,"در حال بررسی اتصال باسکول")
                    self._sidebar_scale=SerialScale(port,int(self.db.get_setting("scale_baudrate","9600")),parser=Teta10AParser(int(self.db.get_setting("scale_maximum_weight_kg","60000"))))
                    self._sidebar_scale.start(lambda reading:None,lambda connected,detail:setattr(self,"_sidebar_scale_state",(connected,detail)))
            connected,detail=self._sidebar_scale_state
        self.sidebar_scale_status.setText("باسکول متصل است" if connected else "باسکول قطع است")
        self.sidebar_scale_status.setToolTip(detail)
        set_widget_style(self.sidebar_scale_dot,"background:#32D6A0;border-radius:4px;" if connected else "background:#FF647C;border-radius:4px;")

    def navigate(self, index):
        capability={0:'weighbridge',1:'waybills',2:'processing',3:'settings',4:getattr(self.db,'queue_capability','weighbridge')}.get(index)
        if not capability or not self.db.allows(capability):return
        self.stack.setCurrentIndex(index); self.docs_nav.setChecked(index == 0); self.waybills_nav.setChecked(index==1); self.processing_nav.setChecked(index==2); self.settings_nav.setChecked(index == 3); self.queue_nav.setChecked(index == 4)

    def apply_access(self):
        user=self.db.session.user
        self.account_name.setText(str(user.get('name') or ''))
        self.account_role.setText(' · '.join(user.get('role_labels') or user.get('roles') or []))
        avatar=QPixmap()
        if self.db.session.avatar_data and avatar.loadFromData(self.db.session.avatar_data):
            self.account_avatar.clear(); self.account_avatar.setPixmap(circular_avatar(avatar,48))
        else:self.account_avatar.clear(); self.account_avatar.setText(str(user.get('name') or '؟')[:1])
        pages=[(self.docs_nav,'weighbridge'),(self.waybills_nav,'waybills'),(self.processing_nav,'processing'),(self.settings_nav,'settings'),(self.queue_nav,getattr(self.db,'queue_capability','weighbridge'))]
        for button,capability in pages:button.setVisible(self.db.allows(capability))
        current=self.stack.currentIndex()
        if current < 0 or not self.db.allows(pages[current][1]):
            for index,(_,capability) in enumerate(pages):
                if self.db.allows(capability):self.navigate(index); break

    def show_account(self):
        AccountDialog(self.db.session,self,format_datetime=iran_jalali_datetime).exec()

    def start_reference_sync(self):
        if self._logging_out or (self.reference_thread and self.reference_thread.isRunning()):return
        key=self.db.session.base_url if self.db.session else self.db.path
        if key in self._reference_sync_sites:return
        self._reference_sync_sites.add(key)
        self.reference_thread=QThread(self); worker=ReferenceDataWorker(self.db); self._reference_worker=worker
        worker.updated.connect(self.reference_data_updated)
        worker.moveToThread(self.reference_thread); self.reference_thread.started.connect(worker.run)
        worker.finished.connect(self.reference_thread.quit); worker.finished.connect(worker.deleteLater)
        self.reference_thread.finished.connect(self.reference_thread.deleteLater)
        self.reference_thread.finished.connect(self.reference_sync_closed); self.reference_thread.start()

    def reference_sync_closed(self):
        self.reference_thread=None; self._reference_worker=None

    def reference_data_updated(self,kind,ok,text):
        self._set_api_status(kind,'ok' if ok else 'error',text)
        self.refresh_reference_summary()

    def check_session(self):
        if self._logging_out or (self._session_check and self._session_check.isRunning()):return
        if not any(self.db.session.allows(capability) for capability in self.db.session.user.get('weighbridge_capabilities',[])):
            self.logout(); return
        self._session_check=SessionCheckWorker(self.db.session,self)
        self._session_check.succeeded.connect(self.session_updated)
        self._session_check.failed.connect(self.session_check_failed)
        self._session_check.finished.connect(self._session_check.deleteLater)
        self._session_check.finished.connect(self.session_check_closed)
        self._session_check.start()

    def session_check_closed(self):
        self._session_check=None

    def session_updated(self,user):
        if self._logging_out:return
        if (user.get('roles') != self.db.session.user.get('roles')
                or user.get('weighbridge_capabilities') != self.db.session.user.get('weighbridge_capabilities')):
            self.logout(); return
        self.db.session.user=user; self.apply_access()

    def session_check_failed(self,message,invalid):
        if invalid:self.logout()

    def logout(self):
        if self._logging_out:return
        self._logging_out=True
        self.queue_page.shutdown()
        forget_session()
        self.stop_sidebar_scale()
        for dialog in self.findChildren(QDialog):dialog.reject()
        for timer in (self.auto_timer,self.server_sync_timer,self.session_timer,self.scale_status_timer):timer.stop()
        self.setEnabled(False); self.sync_label.setText('در حال خروج از حساب…'); self.hide()
        self.finish_logout_when_idle()

    def finish_logout_when_idle(self):
        threads=(self.sync_thread,self.server_sync_thread,self._session_check,self.reference_thread)
        if any(thread and thread.isRunning() for thread in threads):
            QTimer.singleShot(150,self.finish_logout_when_idle); return
        self._logout_worker=LogoutWorker(self.db.session,self)
        self._logout_worker.finished.connect(self._logout_worker.deleteLater)
        self._logout_worker.finished.connect(self.logout_worker_closed)
        self._logout_worker.finished.connect(self.prompt_login); self._logout_worker.start()

    def logout_worker_closed(self):
        self._logout_worker=None

    def prompt_login(self):
        if getattr(self,'_closing',False):return
        self.db.session=None
        app=QApplication.instance(); app.setQuitOnLastWindowClosed(False)
        login=LoginDialog(self.db)
        if not login.exec():self.close(); app.quit(); return
        self.db=self.db.for_session(login.session); self._logging_out=False; self.server_sync_session_ready=False
        self.queue_page.db=self.db; self.queue_page.stopping=False; self.queue_page.snapshot={"entries":[]}; self.queue_page.public=None; self.queue_page.timer.start()
        self.selected_document_ids.clear(); self.selected_waybill_ids.clear(); self.selected_processing_ids.clear()
        self.setEnabled(True); self.show(); self.apply_access()
        app.setQuitOnLastWindowClosed(True)
        self.refresh_documents(); self.refresh_waybills(); self.refresh_processing(); self.refresh_endpoints()
        for timer in (self.auto_timer,self.server_sync_timer,self.session_timer,self.scale_status_timer):timer.start()
        self.start_server_sync()
        self.start_reference_sync()

    def closeEvent(self,event):
        self.queue_page.shutdown()
        if getattr(self,'_close_ready',False):event.accept(); return
        self._closing=True; self._logging_out=True
        self.stop_sidebar_scale()
        for timer in (self.auto_timer,self.server_sync_timer,self.session_timer,self.scale_status_timer):timer.stop()
        event.ignore(); self.setEnabled(False)
        self.finish_close_when_idle()

    def finish_close_when_idle(self):
        threads=(self.sync_thread,self.server_sync_thread,self._session_check,self.reference_thread,self._logout_worker)
        if any(thread and thread.isRunning() for thread in threads):
            QTimer.singleShot(150,self.finish_close_when_idle); return
        self._close_ready=True; self.close()

    def toggle_theme(self, enabled: bool):
        self._dark_mode = enabled
        set_theme("dark" if enabled else "light")

    def build_documents(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(14)
        hero = QFrame(objectName="documentsHero"); hero_layout = QHBoxLayout(hero); hero_layout.setContentsMargins(24, 20, 24, 20); hero_layout.setSpacing(18)
        hero_text = QVBoxLayout(); hero_text.setSpacing(4)
        title = QLabel("اسناد باسکول", objectName="heroTitle"); subtitle = QLabel("ردگیری، بررسی و مدیریت هوشمند تمام توزین‌ها", objectName="heroSubtitle")
        hero_text.addWidget(title); hero_text.addWidget(subtitle); hero_layout.addLayout(hero_text, 1)
        hero_badge = QLabel("● سامانه آماده دریافت سند", objectName="heroBadge"); hero_badge.setAlignment(Qt.AlignCenter); hero_badge.hide()
        new = QPushButton("ثبت سند جدید", objectName="primary"); new.setIcon(QIcon(str(self.assets / "add-document.svg"))); new.setIconSize(QSize(21,21)); new.clicked.connect(self.new_document)
        sync = QPushButton("همگام‌سازی"); sync.setIcon(QIcon(str(self.assets / "sync.svg"))); sync.setIconSize(QSize(21,21)); sync.clicked.connect(self.start_sync)
        hero_layout.addWidget(sync); hero_layout.addWidget(new); layout.addWidget(hero)
        metrics = QHBoxLayout(); metrics.setSpacing(12)
        for attr, label, tone in (("total", "اسناد در این فهرست", "brand"), ("pending", "در انتظار تکمیل یا ارسال", "warning"), ("synced", "همگام‌شده با سرور", "success")):
            panel = QFrame(objectName="metricCard"); metric = QVBoxLayout(panel); metric.setContentsMargins(20,14,20,14); metric.setSpacing(4)
            metric.addWidget(QLabel(label, objectName="metricTitle"))
            value = QLabel("۰", objectName="metricValue"); value.setProperty("tone",tone); value.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
            setattr(self, "documents_stat_" + attr, value); metric.addWidget(value); metrics.addWidget(panel,1)
        layout.addLayout(metrics)
        filters = QFrame(objectName="card"); fl = QGridLayout(filters); fl.setContentsMargins(16, 14, 16, 14); fl.setHorizontalSpacing(10); fl.setVerticalSpacing(10)
        self.search = AppSearchLineEdit("جست‌وجوی شماره سند یا قراضه…"); self.search.textChanged.connect(self.refresh_documents)
        self.movement_filter = StyledComboBox(); self.movement_filter.addItem("همه گردش‌ها", "")
        for k,v in MOVEMENTS.items(): self.movement_filter.addItem(v,k)
        self.status_filter = StyledComboBox(); self.status_filter.addItem("همه وضعیت‌ها", "")
        for k,v in (("weight_discrepancy","عدم تطابق وزن"),("draft","پیش‌نویس"),("first_weighed","توزین اول ثبت شده"),("quality_assessed","کنترل کیفیت"),("second_weighed","توزین دوم ثبت شده"),("finalized","نهایی‌شده"), *SYNC_LABELS.items()):
            if self.status_filter.findData(k) < 0: self.status_filter.addItem(v,k)
        self.entry_time_enabled=QCheckBox("بازه ساعت ورود · فقط اسناد امروز")
        self.entry_time_from=TimePicker(QTime(0,0)); self.entry_time_to=TimePicker(QTime(23,59))
        for editor in (self.entry_time_from,self.entry_time_to):
            editor.setLayoutDirection(Qt.LeftToRight); editor.setMaximumWidth(120); editor.setEnabled(False)
        self.entry_time_from.timeChanged.connect(self.refresh_documents); self.entry_time_to.timeChanged.connect(self.refresh_documents)
        self.entry_time_enabled.toggled.connect(self.toggle_entry_time_filter)
        self.movement_filter.currentIndexChanged.connect(self.refresh_documents); self.status_filter.currentIndexChanged.connect(self.refresh_documents)
        fl.addWidget(self.search, 0, 0, 1, 2); fl.addWidget(self.movement_filter, 0, 2); fl.addWidget(self.status_filter, 0, 3)
        time_row=QHBoxLayout(); time_row.addWidget(self.entry_time_enabled); time_row.addWidget(QLabel("از")); time_row.addWidget(self.entry_time_from); time_row.addWidget(QLabel("تا")); time_row.addWidget(self.entry_time_to); time_row.addStretch()
        fl.addLayout(time_row,1,0,1,3)
        reset = QPushButton("پاک‌کردن فیلترها"); reset.clicked.connect(self.reset_document_filters); fl.addWidget(reset,1,3)
        self.entry_time_hint=QLabel("ساعت‌ها به وقت ایران هستند.",objectName="mutedText"); fl.addWidget(self.entry_time_hint,2,0,1,4)
        fl.setColumnStretch(0,2); fl.setColumnStretch(1,2); fl.setColumnStretch(2,1); fl.setColumnStretch(3,1)
        for widget, hint in ((self.search,"جست‌وجو در شماره سند، شرکت تأمین‌کننده، راننده و پلاک"),(self.entry_time_from,"شروع بازه ساعت ورود به وقت ایران"),(self.entry_time_to,"پایان بازه ساعت ورود به وقت ایران"),(self.status_filter,"فیلتر وضعیت سند"),(self.movement_filter,"فیلتر نوع گردش")):
            widget.setAccessibleName(hint); widget.setToolTip(hint)
        layout.addWidget(filters)
        card = QFrame(objectName="card"); cl = QVBoxLayout(card); cl.setContentsMargins(0,0,0,0); cl.setSpacing(0)
        toolbar = QHBoxLayout(); toolbar.setContentsMargins(16,12,16,12)
        self.select_page = QCheckBox("انتخاب این صفحه"); self.select_page.setTristate(True); self.select_page.clicked.connect(self.select_document_page)
        toolbar.addWidget(self.select_page); toolbar.addStretch()
        self.selection_summary = QLabel("هیچ سندی انتخاب نشده",objectName="selectionSummary"); toolbar.addWidget(self.selection_summary); cl.addLayout(toolbar)
        self.table = FullWidthTable(0,9)
        self.table.setHorizontalHeaderLabels(["سند / تاریخ ثبت","وضعیت","گردش / مبدأ و مقصد","شرکت تأمین‌کننده / پلاک","وزن خالص","انتخاب","عملیات","ساعت ورود","ساعت خروج"])
        self.table.setAlternatingRowColors(True); self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False); self.table.verticalHeader().setDefaultSectionSize(96)
        header=self.table.horizontalHeader(); header.setSectionResizeMode(QHeaderView.Interactive)
        for column,width in enumerate((205,140,180,160,115,56,110,110,110)): self.table.setColumnWidth(column,width)
        header.setMinimumSectionSize(48); header.moveSection(header.visualIndex(5),0)
        for column in (1,5,6): header.setSectionResizeMode(column,QHeaderView.ResizeToContents)
        header.moveSection(header.visualIndex(7),2); header.moveSection(header.visualIndex(8),3)
        self.table.configure_fill((0,2,3,4))
        self.table.setWordWrap(False); self.table.doubleClicked.connect(lambda _index:self.details())
        self.table.setAccessibleName("فهرست اسناد باسکول")
        cl.addWidget(self.table)
        self.document_empty = QWidget(); empty = QVBoxLayout(self.document_empty); empty.setContentsMargins(30,50,30,50)
        empty.addStretch(); title = QLabel("سندی در این فهرست نیست",objectName="emptyTitle"); title.setAlignment(Qt.AlignCenter); empty.addWidget(title)
        hint = QLabel("فیلترها را تغییر دهید یا یک سند جدید ثبت کنید.",objectName="emptyHint"); hint.setAlignment(Qt.AlignCenter); hint.setWordWrap(True); empty.addWidget(hint); empty.addStretch()
        cl.addWidget(self.document_empty); self.document_empty.hide(); layout.addWidget(card,1)
        pager = QFrame(objectName="paginationCard"); pager_layout = QHBoxLayout(pager); pager_layout.setContentsMargins(12, 8, 12, 8)
        self.document_page_label = QLabel("صفحه ۱ از ۱", objectName="paginationLabel"); self.document_page_label.setAlignment(Qt.AlignCenter)
        self.document_prev = QPushButton("‹ قبلی", objectName="paginationButton"); self.document_next = QPushButton("بعدی ›", objectName="paginationButton")
        self.document_prev.clicked.connect(lambda: self.change_document_page(-1)); self.document_next.clicked.connect(lambda: self.change_document_page(1))
        pager_layout.addWidget(self.document_prev); pager_layout.addStretch(); pager_layout.addWidget(self.document_page_label); pager_layout.addStretch(); pager_layout.addWidget(self.document_next); layout.addWidget(pager)
        self.document_export_bar = QWidget(); export = QHBoxLayout(self.document_export_bar); export.setContentsMargins(0,0,0,0)
        for label, slot in [("خروجی Excel",self.export_excel),("خروجی PDF",self.export_pdf),("چاپ سند",self.print_selected)]:
            b=QPushButton(label); b.clicked.connect(slot); export.addWidget(b)
        export.addStretch(); self.document_export_bar.hide(); layout.addWidget(self.document_export_bar); return page

    def change_document_page(self, direction: int):
        total = len(getattr(self, "filtered_document_rows", []))
        pages = max(1, (total + self.document_page_size - 1) // self.document_page_size)
        self.document_page = max(0, min(pages - 1, self.document_page + direction))
        self.refresh_documents()

    def _add_date_picker(self, field):
        action=QAction("تقویم", field); action.triggered.connect(lambda checked=False, target=field:self._pick_jalali_date(target)); field.addAction(action, QLineEdit.TrailingPosition)

    def _pick_jalali_date(self, field):
        dialog=QDialog(self); dialog.setWindowTitle("انتخاب تاریخ شمسی"); dialog.setLayoutDirection(Qt.RightToLeft); layout=QVBoxLayout(dialog)
        calendar=QCalendarWidget(); calendar.setGridVisible(True); layout.addWidget(calendar); buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        if dialog.exec():
            date=calendar.selectedDate(); jy,jm,jd=gregorian_to_jalali(date.year(),date.month(),date.day()); field.setText(fa_digits(f"{jy:04d}/{jm:02d}/{jd:02d}"))

    def build_waybills(self):
        page=QWidget(); page.setObjectName("waybillsPage"); layout=QVBoxLayout(page); layout.setSpacing(14)
        hero=QFrame(objectName="pageHero"); top=QHBoxLayout(hero); top.setContentsMargins(24,18,24,18); title=QLabel("بارنامه‌های حمل",objectName="pageHeroTitle")
        create=QPushButton("صدور بارنامه جدید",objectName="primary"); create.setIcon(QIcon(str(self.assets/"add-document.svg"))); create.clicked.connect(self.new_waybill)
        top.addWidget(title); top.addStretch(); top.addWidget(create); layout.addWidget(hero)
        filters=QFrame(objectName="card"); fl=QHBoxLayout(filters); self.waybill_search=AppSearchLineEdit("جستجوی شماره، شرکت تأمین‌کننده، راننده، موبایل یا پلاک…"); self.waybill_search.textChanged.connect(self.refresh_waybills)
        self.waybill_status=StyledComboBox(); self.waybill_status.addItem("همه وضعیت‌ها",""); self.waybill_status.addItem("قابل استفاده","issued"); self.waybill_status.addItem("استفاده‌شده","used"); self.waybill_status.addItem("رزرو شده","reserved"); self.waybill_status.currentIndexChanged.connect(self.refresh_waybills)
        fl.addWidget(self.waybill_search,1); fl.addWidget(self.waybill_status); layout.addWidget(filters)
        card=QFrame(objectName="card"); body=QVBoxLayout(card)
        self.waybill_table=FullWidthTable(0,11)
        self.waybill_table.setHorizontalHeaderLabels(["شماره بارنامه","تاریخ صدور","شرکت تأمین‌کننده","راننده","پلاک تریلی","وزن مبدأ","مبدأ","کرایه","وضعیت ارسال","عملیات","انتخاب"])
        self.waybill_table.setAlternatingRowColors(True); self.waybill_table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.waybill_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.waybill_table.verticalHeader().setVisible(False); self.waybill_table.verticalHeader().setDefaultSectionSize(96)
        waybill_header=self.waybill_table.horizontalHeader(); waybill_header.setSectionResizeMode(QHeaderView.Interactive); waybill_header.setStretchLastSection(False)
        for column,width in enumerate((130,100,110,130,140,100,105,135,145,110,56)):self.waybill_table.setColumnWidth(column,width)
        waybill_header.setMinimumSectionSize(48); waybill_header.moveSection(10,0)
        for column in (8,9,10): waybill_header.setSectionResizeMode(column,QHeaderView.ResizeToContents)
        self.waybill_table.configure_fill(tuple(range(8)))
        self.waybill_table.setWordWrap(False); self.waybill_table.doubleClicked.connect(lambda _index:self._open_selected_waybill())
        style_table_scrollbars(self.waybill_table)
        toolbar=QHBoxLayout(); toolbar.setContentsMargins(8,4,8,12)
        self.waybill_select_page=QCheckBox("انتخاب فهرست"); self.waybill_select_page.setTristate(True); self.waybill_select_page.clicked.connect(lambda checked:self.select_list("waybill",checked))
        self.waybill_selection_summary=QLabel(objectName="selectionSummary"); toolbar.addWidget(self.waybill_select_page); toolbar.addStretch(); toolbar.addWidget(self.waybill_selection_summary); body.addLayout(toolbar)
        body.addWidget(self.waybill_table)
        self.waybill_empty=self.empty_list("بارنامه‌ای در این فهرست نیست", "جست‌وجو و وضعیت را تغییر دهید یا بارنامه جدید صادر کنید."); body.addWidget(self.waybill_empty); layout.addWidget(card,1)
        self.waybill_export_bar=QWidget(); export=QHBoxLayout(self.waybill_export_bar); export.setContentsMargins(0,0,0,0)
        for label,slot in (("خروجی Excel",self.export_waybills_excel),("خروجی PDF",self.export_waybills_pdf),("چاپ بارنامه‌ها",self.print_selected_waybills)):
            button=QPushButton(label); button.clicked.connect(slot); export.addWidget(button)
        export.addStretch(); self.waybill_export_bar.hide(); layout.addWidget(self.waybill_export_bar); return page

    def new_waybill(self):
        if not self.db.allows('waybills'):return
        dlg=WaybillDialog(self.db,self)
        if dlg.exec():
            row=self.db.waybill(dlg.created_id); self.refresh_waybills(); self.start_sync(); message(self,"بارنامه صادر شد",f"شماره قطعی بارنامه از سامانه مرکزی دریافت شد:\nBL-{fa_digits(f'{int(row['number']):08d}')}\nشماره انتظار: {fa_digits(row['queue_number']) if row['queue_number'] else 'دریافت نشده؛ نسخه سرور را بررسی کنید'}\n\nارسال به مقصدهای ثانویه در پس‌زمینه انجام می‌شود.")

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item=layout.takeAt(0); widget=item.widget()
            if widget: widget.deleteLater()

    def toggle_entry_time_filter(self, enabled):
        self.entry_time_from.setEnabled(enabled); self.entry_time_to.setEnabled(enabled)
        self.document_page=0; self.refresh_documents()

    def reset_document_filters(self):
        for widget in (self.search,self.entry_time_enabled,self.entry_time_from,self.entry_time_to,self.movement_filter,self.status_filter): widget.blockSignals(True)
        self.search.clear(); self.entry_time_enabled.setChecked(False); self.entry_time_from.setTime(QTime(0,0)); self.entry_time_to.setTime(QTime(23,59)); self.entry_time_from.setEnabled(False); self.entry_time_to.setEnabled(False)
        self.movement_filter.setCurrentIndex(0); self.status_filter.setCurrentIndex(0); self.document_page=0
        for widget in (self.search,self.entry_time_enabled,self.entry_time_from,self.entry_time_to,self.movement_filter,self.status_filter): widget.blockSignals(False)
        self.refresh_documents()

    def select_document_page(self, checked):
        for row, advanced in getattr(self,"visible_document_rows",[]):
            key=-int(row["id"]) if advanced else int(row["id"])
            (self.selected_document_ids.add if checked else self.selected_document_ids.discard)(key)
        self.update_document_selection()
        self.render_document_cards(getattr(self,"visible_document_rows",[]))

    def update_document_selection(self):
        if not hasattr(self,"selection_summary"): return
        self.document_export_bar.setVisible(bool(self.selected_document_ids))
        self.selection_summary.setText(f"{fa_digits(len(self.selected_document_ids))} سند انتخاب شده" if self.selected_document_ids else "هیچ سندی انتخاب نشده")
        keys={-int(row["id"]) if advanced else int(row["id"]) for row,advanced in getattr(self,"visible_document_rows",[])}
        count=len(keys & self.selected_document_ids)
        self.select_page.blockSignals(True)
        self.select_page.setCheckState(Qt.Checked if keys and count==len(keys) else Qt.PartiallyChecked if count else Qt.Unchecked)
        self.select_page.blockSignals(False)

    def render_document_cards(self, visible):
        """Render compact rows in exactly the paginated chronological order."""
        self.visible_document_rows=visible
        self.table.setRowCount(len(visible)); self.table.setVisible(bool(visible)); self.document_empty.setVisible(not visible)
        status_names={"draft":"پیش‌نویس","first_weighed":"در انتظار توزین دوم","quality_assessed":"کنترل کیفیت","second_weighed":"توزین دوم ثبت شده"}
        for index,(row,advanced) in enumerate(visible):
            key=-int(row["id"]) if advanced else int(row["id"])
            def cell(column, primary, secondary=""):
                host=QWidget(); set_widget_style(host, 'background:transparent;')
                box=QVBoxLayout(host); box.setContentsMargins(12,8,12,8); box.setSpacing(4)
                label=QLabel(str(primary),objectName="documentCardNumber"); label.setToolTip(str(primary)); box.addWidget(label)
                if column==0: label.setLayoutDirection(Qt.LeftToRight); label.setAlignment(Qt.AlignRight)
                if secondary:
                    sub=QLabel(str(secondary),objectName="documentCardInfo"); sub.setToolTip(str(secondary)); box.addWidget(sub)
                self.table.setCellWidget(index,column,host)
                item=QTableWidgetItem(""); item.setData(Qt.UserRole,key); self.table.setItem(index,column,item)
            cell(0,fa_digits(row["document_number"]),jalali_date(row["registered_at"]))
            state=row["sync_status"] if not advanced or row["status"]=="finalized" else row["status"]
            status=SYNC_LABELS.get(state,status_names.get(state,state))
            certificate=weight_discrepancy(row) if advanced else {}
            if advanced and row["status"]=="finalized" and row["quality_pending"]:status="در انتظار کنترل کیفیت"
            if certificate:status="عدم تطابق وزن"
            badge=QLabel(status,objectName="statusBadge"); badge.setAlignment(Qt.AlignCenter)
            if certificate:badge.setToolTip("کسری وزن: "+self._format_weight(certificate.get("shortage_weight_kg"))+" · "+str(certificate["certificate_number"]))
            badge.setProperty("tone","error" if certificate else "success" if state=="synced" else "error" if state in ("failed","error","partial","needs_attention") else "warning")
            host=QWidget(); box=QHBoxLayout(host); box.setContentsMargins(8,12,8,12); box.addWidget(badge); self.table.setCellWidget(index,1,host)
            company=(json.loads(row["company_snapshot"] or "{}") or {}).get("name") or "داخلی" if advanced else "داخلی"
            origin=" · ".join(str(row[k] or "") for k in ("origin_province","origin_city") if row[k]) if advanced else row["scrap_name"]
            cell(2,MOVEMENTS.get(row["movement_type"],row["movement_type"]),origin or "—")
            cell(3,company,fa_digits(row["trailer_plate"] or "—") if advanced else "—")
            weight=max(0,float(row["gross_weight_kg"] or 0)-float(row["tare_weight_kg"] or 0)) if advanced else row["weight_kg"]
            cell(4,self._format_weight(weight) if not advanced or (row["gross_weight_kg"] and row["tare_weight_kg"]) else "در انتظار توزین دوم")
            for column,time_key in ((7,"entry_at"),(8,"exit_at")):
                value=row[time_key] if time_key in row.keys() else None
                cell(column,iran_jalali_datetime(value).rsplit(" · ",1)[-1])
            host=QWidget(); box=QHBoxLayout(host); box.setAlignment(Qt.AlignCenter)
            check=QCheckBox(); check.setAccessibleName("انتخاب سند " + str(row["document_number"])); check.setChecked(key in self.selected_document_ids)
            check.toggled.connect(lambda checked, key=key:self._toggle_document_selection(key,checked)); box.addWidget(check); self.table.setCellWidget(index,5,host)
            host=QWidget(); box=QHBoxLayout(host); box.setContentsMargins(8,12,8,12)
            button=QPushButton("عملیات"); button.setAccessibleName("عملیات سند " + str(row["document_number"]))
            menu=QMenu(button)
            for title,slot in (("مشاهده اطلاعات",self.details),("چاپ سند",self.print_document),("ذخیره PDF",self.export_document_pdf)):
                action=menu.addAction(title); action.triggered.connect(lambda checked=False,key=key,slot=slot:slot(key))
            button.setMenu(menu); box.addWidget(button); self.table.setCellWidget(index,6,host)
        fit_table_controls(self.table,(1,5,6))
        self.update_document_selection()

    @staticmethod
    def empty_list(title, hint):
        host=QWidget(); box=QVBoxLayout(host); box.setContentsMargins(24,40,24,40); box.addStretch()
        for text,name in ((title,"emptyTitle"),(hint,"emptyHint")):
            label=QLabel(text,objectName=name); label.setWordWrap(True); label.setAlignment(Qt.AlignCenter); box.addWidget(label)
        box.addStretch(); return host

    def select_list(self, kind, checked):
        selected=getattr(self,"selected_"+kind+"_ids")
        ids=getattr(self,"visible_"+kind+"_ids",set())
        selected.update(ids) if checked else selected.difference_update(ids)
        self.refresh_waybills() if kind=="waybill" else self.refresh_processing()

    def update_list_selection(self, kind, key=None, checked=False):
        selected=getattr(self,"selected_"+kind+"_ids")
        if key is not None: (selected.add if checked else selected.discard)(key)
        getattr(self,kind+"_export_bar").setVisible(bool(selected))
        ids=getattr(self,"visible_"+kind+"_ids",set()); count=len(ids & selected)
        checkbox=getattr(self,kind+"_select_page"); checkbox.blockSignals(True)
        checkbox.setCheckState(Qt.Checked if ids and count==len(ids) else Qt.PartiallyChecked if count else Qt.Unchecked); checkbox.blockSignals(False)
        getattr(self,kind+"_selection_summary").setText(f"{fa_digits(len(ids))} سند · {fa_digits(len(selected))} انتخاب‌شده")

    def render_waybill_cards(self, rows):
        self.visible_waybill_ids={int(row["id"]) for row in rows}
        self.waybill_empty.setVisible(not rows); self.waybill_table.setVisible(bool(rows)); self.update_list_selection("waybill")

    def _toggle_document_selection(self, key: int, checked: bool) -> None:
        (self.selected_document_ids.add if checked else self.selected_document_ids.discard)(key)
        self.update_document_selection()

    def _toggle_waybill_selection(self, key: int, checked: bool) -> None:
        self.update_list_selection("waybill", key, checked)

    def _selected_documents(self):
        # Use the list queries rather than document(id): the list queries add
        # calculated fields such as sync_status and the two weighing totals.
        advanced_by_id={int(row["id"]):row for row in self.db.advanced_documents()}
        legacy_by_id={int(row["id"]):row for row in self.db.documents()}
        selected=[]
        for key in sorted(self.selected_document_ids, key=abs):
            row=advanced_by_id.get(-key) if key<0 else legacy_by_id.get(key)
            if row:selected.append((row,key<0))
        return selected

    def _selected_waybills(self):
        return [row for key in sorted(self.selected_waybill_ids) if (row:=self.db.waybill(key))]

    def refresh_waybills(self):
        if not self.db.allows('waybills'):
            self.waybill_table.setRowCount(0); self.render_waybill_cards([]); return
        if not hasattr(self,"waybill_table"):return
        rows=self.db.waybills(self.waybill_search.text().strip() if hasattr(self,"waybill_search") else "")
        if not self.db.allows('weighbridge'):
            synced=sum(1 for row in rows if row['cache_source']=='server' or (row['total_count'] and row['ok_count']==row['total_count']))
            self.sidebar_count.setText(f'{fa_digits(len(rows))}\nبارنامه‌ها')
            self.sidebar_pending.setText(f'{fa_digits(len(rows)-synced)}\nدر صف')
            self.sidebar_synced.setText(f'{fa_digits(synced)}\nارسال‌شده')
        status=self.waybill_status.currentData() if hasattr(self,"waybill_status") else ""
        if status:rows=[row for row in rows if row["status"]==status]
        self.render_waybill_cards(rows)
        self.waybill_table.setRowCount(len(rows)); self.waybill_table.verticalHeader().setDefaultSectionSize(96)
        for i,row in enumerate(rows):
            company=json.loads(row["company_snapshot"] or "{}")
            if row["cache_source"]=="server":sync_status="ثبت‌شده در "+self.db.erp_label
            elif not row["server_confirmed"]:sync_status="در انتظار شماره قطعی"
            elif not row["total_count"]:sync_status="بدون مقصد"
            elif row["ok_count"]==row["total_count"]:sync_status="ارسال‌شده"
            elif row["ok_count"]:sync_status=f"ناقص ({row['ok_count']}/{row['total_count']})"
            else:sync_status="در صف / ناموفق"
            number=fa_digits(f"BL-{int(row['number']):08d}") if row["server_confirmed"] else "—"
            ticket=f"\nنوبت {fa_digits(row["queue_number"])}" if row["queue_number"] else ""
            values=(number+ticket+("\n"+str(row["material_name"])) if row["material_name"] else number+ticket,jalali_date(row["issued_at"]),company.get("name","—"),f"{row['driver_name']}\n{fa_digits(row['driver_phone'])}","",self.waybill_origin_label(row),f"{row['origin_province']}، {row['origin_city']}",self.waybill_freight_label(row),sync_status,"")
            for j,value in enumerate(values):item=QTableWidgetItem(str(value)); item.setTextAlignment(Qt.AlignCenter); item.setData(Qt.UserRole,row["id"]); self.waybill_table.setItem(i,j,item)
            self.waybill_table.setCellWidget(i,4,self._plate_badge(row["trailer_plate"]))
            self._set_waybill_actions(i,row["id"])
            holder=QWidget(); select_layout=QHBoxLayout(holder); select_layout.setAlignment(Qt.AlignCenter)
            check=QCheckBox(); check.setChecked(int(row["id"]) in self.selected_waybill_ids); check.setAccessibleName("انتخاب بارنامه " + number)
            check.toggled.connect(lambda checked,key=int(row["id"]):self._toggle_waybill_selection(key,checked)); select_layout.addWidget(check)
            self.waybill_table.setCellWidget(i,10,holder)
            badge=QLabel(sync_status,objectName="statusBadge"); badge.setAlignment(Qt.AlignCenter); set_tone(badge,"success" if sync_status in ("ثبت‌شده در "+self.db.erp_label,"ارسال‌شده") else "warning")
            holder=QWidget(); status_layout=QHBoxLayout(holder); status_layout.setContentsMargins(6,12,6,12); status_layout.addWidget(badge); self.waybill_table.setCellWidget(i,8,holder)
            self.waybill_table.item(i,8).setText("")

        fit_table_controls(self.waybill_table,(8,9,10))

    def _set_waybill_actions(self,row,waybill_id):
        holder=QWidget(); actions=QHBoxLayout(holder); actions.setContentsMargins(5,12,5,12); actions.setSpacing(6)
        button=QPushButton("عملیات",objectName="tableActions")
        menu=QMenu(button); menu.setLayoutDirection(Qt.RightToLeft)
        view_action=QAction("مشاهده اطلاعات",menu); view_action.triggered.connect(lambda checked=False,wid=waybill_id:self.waybill_details(wid)); menu.addAction(view_action)
        print_action=QAction("چاپ بارنامه",menu); print_action.triggered.connect(lambda checked=False,wid=waybill_id:self.print_waybill(wid)); menu.addAction(print_action)
        pdf_action=QAction("ذخیره فایل PDF",menu); pdf_action.triggered.connect(lambda checked=False,wid=waybill_id:self.export_waybill_pdf(wid)); menu.addAction(pdf_action)
        menu.addSeparator(); delete_action=QAction("حذف بارنامه",menu); delete_action.triggered.connect(lambda checked=False,wid=waybill_id:self.delete_waybill(wid)); menu.addAction(delete_action)
        button.setMenu(menu); actions.addWidget(button); self.waybill_table.setCellWidget(row,9,holder)

    def _open_selected_waybill(self):
        row=self.waybill_table.currentRow()
        if row>=0 and self.waybill_table.item(row,0):self.waybill_details(int(self.waybill_table.item(row,0).data(Qt.UserRole)))

    @requires_desktop_access('waybills')
    def waybill_details(self,waybill_id):
        row=self.db.waybill(waybill_id)
        if not row:return
        company=json.loads(row["company_snapshot"] or "{}") or {}
        number=fa_digits(f"BL-{int(row['number']):08d}") if row["server_confirmed"] else "در انتظار شماره قطعی سرور"
        dialog=DocumentViewDialog(self,title="بارنامه حمل قراضه",number=number,
            status=self._waybill_status_label(row["status"]),
            metrics=(("شماره انتظار",fa_digits(row["queue_number"]) if row["queue_number"] else "—"),("وزن خالص مبدأ",self.waybill_origin_label(row)),*(((("کرایه حمل",self.waybill_freight_label(row)),) if self.waybill_freight_visible(row) else ()))),
            sections=(("مشخصات صدور و طرف معامله",(("تاریخ صدور",jalali_date(row["issued_at"])),("نوع کالا",row["material_name"]),("شرکت تأمین‌کننده",company.get("name")),("کد شرکت",company.get("code")),("نام حقوقی",company.get("legal_name")),("شناسه ملی شرکت",company.get("national_id")),("شناسه رویداد",row["event_id"]))),
                      ("راننده و مسیر حمل",(("نام راننده",row["driver_name"]),("شماره تماس",fa_digits(row["driver_phone"])),("پلاک تریلی",fa_digits(row["trailer_plate"])),("مبدأ",f"{row['origin_province']}، {row['origin_city']}")))),
            print_slot=lambda:self.print_waybill(waybill_id),pdf_slot=lambda:self.export_waybill_pdf(waybill_id))
        dialog.exec()

    def _admin_base_url(self) -> str:
        configured=self.db.get_setting("admin_panel_url","").strip().rstrip("/")
        if configured:return configured
        for key in ("waybill_available_url","scrap_api_url","company_api_url"):
            parts=urlsplit(self.db.get_setting(key,"").strip())
            if parts.scheme in ("http","https") and parts.netloc:return f"{parts.scheme}://{parts.netloc}"
        return ""

    def _waybill_verification_url(self,row) -> str:
        base=self._admin_base_url()
        return f"{base}/transportation/waybills/verify/{int(row['number'])}" if base else ""

    def _weighbridge_verification_url(self,row) -> str:
        base=self._admin_base_url()
        return f"{base}/transportation/weighbridge/verify/{row['event_id']}" if base else ""

    @staticmethod
    def _file_data_uri(path: Path) -> str:
        mime="image/png" if path.suffix.lower()==".png" else "image/svg+xml"
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"

    @staticmethod
    def _qr_data_uri(value: str) -> str:
        image=qrcode.make(value or "بارنامه فاقد نشانی استعلام است")
        buffer=BytesIO(); image.save(buffer,format="PNG")
        return "data:image/png;base64,"+base64.b64encode(buffer.getvalue()).decode("ascii")

    @staticmethod
    def _waybill_status_label(status: str) -> str:
        return {"issued":"قابل استفاده","reserved":"رزرو شده","used":"استفاده شده","cancelled":"باطل شده"}.get(status,status or "—")

    @staticmethod
    def waybill_freight_visible(row):
        return True

    @staticmethod
    def waybill_freight_label(row):
        contractor=bool(row["freight_by_contractor"]) if "freight_by_contractor" in row.keys() else False
        return "۰ ریال · کرایه با پیمانکار است" if contractor else fa_digits(f"{int(row['freight_amount'] or 0):,} ریال")

    @staticmethod
    def waybill_origin_label(row):
        no_origin=bool(row["no_origin_weighbridge"]) if "no_origin_weighbridge" in row.keys() else False
        return "بارنامه باسکول مبدأ ندارد" if no_origin else MainWindow._format_weight(row["origin_net_weight_kg"])

    def waybill_html(self,row) -> str:
        company=json.loads(row["company_snapshot"] or "{}")
        esc=lambda value:html.escape(str(value or "—"))
        number=f"BL-{int(row['number']):08d}"
        verify_url=self._waybill_verification_url(row)
        logo_uri=self._file_data_uri(Path(__file__).resolve().parent/"assets"/"brand"/"megasanj-icon.png")
        stamp_uri=self._file_data_uri(Path(__file__).resolve().parent/"assets"/"brand"/"megapulse-official-stamp.png")
        qr_uri=self._qr_data_uri(verify_url or str(row["event_id"]))
        queue_number=fa_digits(row['queue_number']) if row['queue_number'] else '—'
        queue_date=jalali_date(row['queue_day']) if row['queue_day'] else '—'
        issued_date=jalali_date(row["issued_at"])
        try:issued_time=fa_digits(datetime.fromisoformat(row["issued_at"].replace("Z","+00:00")).strftime("%H:%M"))
        except (TypeError,ValueError):issued_time="—"
        output = f"""<!doctype html><html dir='rtl' lang='fa'><head><meta charset='utf-8'><style>
        @page {{ size:A4 portrait; margin:11mm; }}
        body {{ direction:rtl; font-family:'PeydaWeb(FaNum)'; color:#172033; margin:0; font-size:10.5pt; }}
        .sheet {{ border:1.4px solid #173b67; padding:18px; }}
        .header {{ width:100%; border-bottom:3px solid #1769d2; padding-bottom:12px; }}
        .logo {{ width:58px; height:58px; }} .title {{ color:#12396b; font-size:21pt; font-weight:800; margin:0; }}
        .number {{ color:#1769d2; font-size:15pt; font-weight:800; direction:ltr; }}
        .section-title {{ margin:12px 0 6px; padding:6px 10px; background:#eaf2fb; color:#173b67; font-size:12pt; font-weight:800; border-right:4px solid #1769d2; }}
        table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
        td {{ border:1px solid #cbd8e8; padding:9px; vertical-align:middle; }}
        .label {{ color:#64748b; font-size:8.5pt; margin-bottom:3px; }} .value {{ color:#172033; font-size:10.5pt; font-weight:700; }}
        .qr {{ width:96px; height:96px; }} .verify {{ font-size:7.5pt; color:#52677f; direction:ltr; word-break:break-all; }}
        .notice {{ background:#fff8e7; border:1px solid #f4d48e; color:#6f4b0b; padding:6px 9px; margin-top:8px; line-height:1.45; }}
        .sign td {{ height:66px; text-align:center; vertical-align:top; color:#52677f; }}
        .stamp {{ width:54px; height:54px; margin-top:3px; }}
        .footer {{ margin-top:12px; border-top:1px solid #cbd8e8; padding-top:8px; color:#64748b; font-size:8pt; text-align:center; }}
        </style></head><body><div class='sheet'>
        <table class='header' width='100%'><tr><td style='border:0;text-align:left'><div class='label'>شماره بارنامه</div><div class='number'>{esc(number)}</div><div class='label'>تاریخ ثبت: {issued_date} · ساعت {issued_time}</div></td><td style='border:0'><div class='title'>بارنامه حمل کالا</div><div>{esc(row['material_name'] if 'material_name' in row.keys() and row['material_name'] else 'نیازمند ثبت نوع کالا در بارنامه')}</div><div>سامانه یکپارچه {self.db.erp_label} · اپلیکیشن مگاسنج</div></td><td style='border:0;width:72px;text-align:right'><img class='logo' width='58' height='58' src='{logo_uri}'></td></tr></table>
        <table width='100%'><tr><td align='center' style='background:#eaf2fb;padding:10px'><div class='label'>شماره انتظار · {queue_date}</div><div style='font-size:26pt;color:#1769d2;font-weight:500'>{queue_number}</div></td></tr></table>
        <div class='section-title'>اطلاعات ثبتی و طرف معامله</div>
        <table width='100%'><tr><td><div class='label'>شرکت تأمین‌کننده</div><div class='value'>{esc(company.get('name'))}</div></td><td><div class='label'>کد شرکت تأمین‌کننده</div><div class='value'>{esc(company.get('code'))}</div></td><td><div class='label'>نام حقوقی</div><div class='value'>{esc(company.get('legal_name'))}</div></td></tr>
        <tr><td><div class='label'>شناسه ملی شرکت</div><div class='value'>{fa_digits(esc(company.get('national_id')))}</div></td><td colspan='2'><div class='label'>شماره ثبت شرکت</div><div class='value'>{fa_digits(esc(company.get('registration_number')))}</div></td></tr>
        <tr><td><div class='label'>شماره تماس شرکت تأمین‌کننده</div><div class='value'>{fa_digits(esc(company.get('phone')))}</div></td><td colspan='2'><div class='label'>نشانی ثبت‌شده</div><div class='value'>{esc(company.get('address') or '—')}</div></td></tr></table>
        <div class='section-title'>اطلاعات حمل، راننده و بار</div>
        <table width='100%'><tr><td><div class='label'>نام راننده</div><div class='value'>{esc(row['driver_name'])}</div></td><td><div class='label'>شماره همراه</div><div class='value'>{fa_digits(esc(row['driver_phone']))}</div></td><td><div class='label'>پلاک تریلی</div><div class='value'>{fa_digits(esc(row['trailer_plate']))}</div></td></tr>
        <tr><td><div class='label'>استان مبدأ</div><div class='value'>{esc(row['origin_province'])}</div></td><td><div class='label'>شهر مبدأ</div><div class='value'>{esc(row['origin_city'])}</div></td><td><div class='label'>مقصد</div><div class='value'>کارخانه مگافولاد</div></td></tr>
        <tr><td><div class='label'>وزن خالص بار در مبدأ</div><div class='value'>{esc(self.waybill_origin_label(row))}</div></td>{("<td><div class='label'>مبلغ کرایه ثبت‌شده</div><div class='value'>"+esc(self.waybill_freight_label(row))+"</div></td>") if self.waybill_freight_visible(row) else "<td></td>"}<td><div class='label'>وضعیت بارنامه</div><div class='value'>{esc(self._waybill_status_label(row['status']))}</div></td></tr></table>
        __REPORT_VERIFICATION__
        <div class='notice'>مسئولیت کنترل وزن بار در مبدأ و حفاظت از بار در طول مسیر تا تحویل در مقصد بر عهده راننده است.</div>
        <div class='section-title'>تأیید و امضا</div><table class='sign' width='100%'><tr><td>امضا و اثر انگشت راننده</td><td>مهر صادرکننده بارنامه<br><img class='stamp' width='54' height='54' src='{stamp_uri}'></td><td>مهر و امضای تحویل‌گیرنده مقصد</td></tr></table>
        <div class='footer'>این سند به‌صورت الکترونیکی توسط سامانه {self.db.erp_label} تولید شده است · شماره مرجع {esc(number)}</div>
        </div></body></html>"""
        return right_align_report_html(output.replace("__REPORT_VERIFICATION__",verification_section(qr_uri)))

    @staticmethod
    def _configure_a4(printer: QPrinter) -> None:
        printer.setPageSize(QPageSize(QPageSize.A4)); printer.setPageOrientation(QPageLayout.Portrait)
        printer.setPageMargins(QMarginsF(8,8,8,8),QPageLayout.Millimeter)

    @staticmethod
    def _configure_a5(printer: QPrinter) -> None:
        printer.setPageSize(QPageSize(QPageSize.A5)); printer.setPageOrientation(QPageLayout.Portrait)
        printer.setPageMargins(QMarginsF(6,6,6,6),QPageLayout.Millimeter)

    @requires_desktop_access('waybills')
    def print_waybill(self,waybill_id):
        row=self.db.waybill(waybill_id)
        if not row:return message(self,"چاپ بارنامه","بارنامه پیدا نشد.",True)
        if not row["server_confirmed"]:return message(self,"چاپ بارنامه","تا دریافت شماره قطعی از سامانه مرکزی امکان چاپ بارنامه وجود ندارد.",True)
        self._print_html_documents([self.waybill_html(row)],f"چاپ بارنامه BL-{int(row['number']):08d}")

    @requires_desktop_access('waybills')
    def export_waybill_pdf(self,waybill_id):
        row=self.db.waybill(waybill_id)
        if not row:return message(self,"PDF بارنامه","بارنامه پیدا نشد.",True)
        if not row["server_confirmed"]:return message(self,"PDF بارنامه","تا دریافت شماره قطعی از سامانه مرکزی امکان ذخیره PDF وجود ندارد.",True)
        filename=f"BL-{int(row['number']):08d}.pdf"
        path,_=QFileDialog.getSaveFileName(self,"ذخیره بارنامه رسمی",filename,"PDF (*.pdf)")
        if not path:return
        if not path.lower().endswith(".pdf"):path += ".pdf"
        printer=QPrinter(QPrinter.HighResolution); printer.setOutputFormat(QPrinter.PdfFormat); printer.setOutputFileName(path); self._configure_a4(printer)
        document=report_document(self.waybill_html(row)); document.print_(printer)
        message(self,"بارنامه رسمی",f"فایل PDF بارنامه با موفقیت ذخیره شد:\n{path}")

    @staticmethod
    def _merge_print_documents(documents: list[str]) -> str:
        """Merge complete HTML documents into a single print job, one document per page."""
        styles=[]; bodies=[]
        for source in documents:
            styles.extend(re.findall(r"<style[^>]*>(.*?)</style>",source,re.I|re.S))
            match=re.search(r"<body[^>]*>(.*?)</body>",source,re.I|re.S)
            bodies.append(match.group(1) if match else source)
        pages="".join(f"<div class='print-page'>{body}</div>" for body in bodies)
        return "<!doctype html><html dir='rtl'><head><meta charset='utf-8'><style>"+"\n".join(styles)+"\n.print-page{page-break-after:always}.print-page:last-child{page-break-after:auto}</style></head><body>"+pages+"</body></html>"

    def _print_html(self, output: str, title: str) -> None:
        self._print_html_documents([output],title)

    def _render_separate_html_documents(self, printer: QPrinter, documents: list[str]) -> None:
        """Render every source as a separate print document/page sequence.

        Qt's HTML engine does not reliably honour CSS page-break rules around
        nested full HTML documents. Painting each source independently and
        calling newPage() guarantees that the next selected record can never
        continue directly below the previous one.
        """
        page_rect=printer.pageLayout().paintRectPoints()
        page_size=QSizeF(page_rect.width(),page_rect.height())
        painter=QPainter(printer)
        if not painter.isActive():raise RuntimeError("راه‌اندازی موتور چاپ انجام نشد.")
        scale_x=printer.logicalDpiX()/72.0; scale_y=printer.logicalDpiY()/72.0
        painter.scale(scale_x,scale_y)
        try:
            first_page=True
            for source in documents:
                if not first_page and not printer.newPage():raise RuntimeError("ساخت صفحه جدید چاپ انجام نشد.")
                first_page=False
                if 'data-minutes-page' in source:
                    paint_minutes_page(painter,source,page_size)
                    continue
                document=report_document(source)
                # Lay the document out at the selected paper's printable width,
                # then fit the full content inside that same printable area.
                # This makes the one-selected-record/one-physical-page contract
                # deterministic even for unusually long notes or compositions.
                document.setTextWidth(page_size.width())
                if 'data-minutes-page' not in source:
                    document.adjustSize()
                content=document.size()
                fit=min(1.0,page_size.width()/max(1.0,content.width()),page_size.height()/max(1.0,content.height()))
                x=max(0.0,(page_size.width()-content.width()*fit)/2.0)
                painter.save(); painter.translate(x,0); painter.scale(fit,fit)
                document.drawContents(painter,QRectF(0,0,content.width(),content.height()))
                painter.restore()
        finally:
            painter.end()

    def _print_html_documents(self, documents: list[str], title: str, page_size: str="a4") -> None:
        print_documents(self,documents,title,self._render_separate_html_documents)

    def _save_pdf_documents(self, documents: list[str], filename: str, title: str, page_size: str="a4") -> None:
        path,_=QFileDialog.getSaveFileName(self,title,filename,"PDF (*.pdf)")
        if not path:return
        if not path.lower().endswith(".pdf"):path += ".pdf"
        printer=QPrinter(QPrinter.HighResolution); printer.setOutputFormat(QPrinter.PdfFormat); printer.setOutputFileName(path); (self._configure_a5 if page_size=="a5" else self._configure_a4)(printer)
        self._render_separate_html_documents(printer,documents)
        message(self,"خروجی",f"فایل با موفقیت ذخیره شد:\n{path}")

    def _save_pdf_html(self, output: str, filename: str, title: str) -> None:
        path,_=QFileDialog.getSaveFileName(self,title,filename,"PDF (*.pdf)")
        if not path:return
        if not path.lower().endswith(".pdf"):path += ".pdf"
        printer=QPrinter(QPrinter.HighResolution); printer.setOutputFormat(QPrinter.PdfFormat); printer.setOutputFileName(path); self._configure_a4(printer)
        document=report_document(output); document.print_(printer)
        message(self,"خروجی",f"فایل با موفقیت ذخیره شد:\n{path}")

    @requires_desktop_access('waybills')
    def print_selected_waybills(self):
        rows=self._selected_waybills()
        if not rows:return message(self,"چاپ بارنامه‌ها","حداقل یک بارنامه را انتخاب کنید.",True)
        invalid=[row for row in rows if not row["server_confirmed"]]
        if invalid:return message(self,"چاپ بارنامه‌ها","بارنامه‌های انتخابی باید شماره قطعی سامانه مرکزی داشته باشند.",True)
        self._print_html_documents([self.waybill_html(row) for row in rows],"چاپ بارنامه‌های انتخاب‌شده")

    @requires_desktop_access('waybills')
    def export_waybills_pdf(self):
        rows=self._selected_waybills()
        if not rows:return message(self,"PDF بارنامه‌ها","حداقل یک بارنامه را انتخاب کنید.",True)
        if any(not row["server_confirmed"] for row in rows):return message(self,"PDF بارنامه‌ها","بارنامه‌های انتخابی باید شماره قطعی سامانه مرکزی داشته باشند.",True)
        self._save_pdf_documents([self.waybill_html(row) for row in rows],"selected-waybills.pdf","ذخیره بارنامه‌های انتخاب‌شده")

    @requires_desktop_access('waybills')
    def export_waybills_excel(self):
        rows=self._selected_waybills()
        if not rows:return message(self,"Excel بارنامه‌ها","حداقل یک بارنامه را انتخاب کنید.",True)
        path,_=QFileDialog.getSaveFileName(self,"ذخیره Excel بارنامه‌ها","selected-waybills.xlsx","Excel (*.xlsx)")
        if not path:return
        if not path.lower().endswith(".xlsx"):path += ".xlsx"
        wb=Workbook(); ws=wb.active; ws.title="بارنامه‌ها"
        headers=["شماره بارنامه","تاریخ صدور","شرکت تأمین‌کننده","کد شرکت تأمین‌کننده","راننده","موبایل","پلاک تریلی","استان مبدأ","شهر مبدأ","وزن مبدأ (کیلوگرم)","کرایه (ریال)","وضعیت","نوع کالا"]
        ws.append(headers)
        for cell in ws[1]:cell.font=Font(bold=True,color="FFFFFF"); cell.fill=PatternFill("solid",fgColor="1769E0"); cell.alignment=Alignment(horizontal="center")
        for row in rows:
            company=json.loads(row["company_snapshot"] or "{}") or {}
            number=f"BL-{int(row['number']):08d}" if row["server_confirmed"] else "در انتظار شماره قطعی"
            ws.append([number,row["issued_at"],company.get("name","—"),company.get("code","—"),row["driver_name"],row["driver_phone"],row["trailer_plate"],row["origin_province"],row["origin_city"],self.waybill_origin_label(row) if row["no_origin_weighbridge"] else round(float(row["origin_net_weight_kg"] or 0)),self.waybill_freight_label(row) if row["freight_by_contractor"] else int(row["freight_amount"] or 0),self._waybill_status_label(row["status"]),row["material_name"] or "—"])
        ws.sheet_view.rightToLeft=True
        for column in ws.columns:ws.column_dimensions[column[0].column_letter].width=max(15,min(32,max(len(str(cell.value or "")) for cell in column)+3))
        wb.save(path); message(self,"خروجی","فایل Excel بارنامه‌های انتخاب‌شده ذخیره شد.")

    @requires_desktop_access('waybills')
    def delete_waybill(self,waybill_id):
        if QMessageBox.question(self,"حذف بارنامه محلی","بارنامه فقط از این رایانه حذف می‌شود و شماره آن هرگز دوباره صادر نخواهد شد. ادامه می‌دهید؟",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)==QMessageBox.Yes:
            self.db.delete_waybill(waybill_id); self.refresh_waybills()

    def build_processing(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setSpacing(14)
        hero=QFrame(objectName="pageHero"); top=QHBoxLayout(hero); top.setContentsMargins(24,18,24,18); title=QLabel("اسناد فرآوری قراضه",objectName="pageHeroTitle"); create=QPushButton("ثبت فرآوری جدید",objectName="primary"); create.clicked.connect(self.new_processing); top.addWidget(title); top.addStretch(); top.addWidget(create); layout.addWidget(hero)
        filters=QFrame(objectName="card"); filter_layout=QHBoxLayout(filters)
        self.processing_search=AppSearchLineEdit("جست‌وجوی شماره سند، قراضه خروجی یا کارشناس…"); self.processing_search.textChanged.connect(self.refresh_processing); filter_layout.addWidget(self.processing_search); layout.addWidget(filters)
        card=QFrame(objectName="card"); body=QVBoxLayout(card); self.processing_table=FullWidthTable(0,9); self.processing_table.setHorizontalHeaderLabels(["انتخاب","شماره سند","تاریخ","ترکیب ورودی","قراضه خروجی","وزن","کارشناس","وضعیت ارسال","عملیات"]); self.processing_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive); self.processing_table.horizontalHeader().setMinimumSectionSize(48);
        for col,width in enumerate((56,180,105,185,130,135,120,140,110)):self.processing_table.setColumnWidth(col,width)
        for column in (0,7,8): self.processing_table.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeToContents)
        self.processing_table.configure_fill(tuple(range(1,7)))
        self.processing_table.verticalHeader().setVisible(False); self.processing_table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.processing_table.setSelectionBehavior(QAbstractItemView.SelectRows); style_table_scrollbars(self.processing_table); toolbar=QHBoxLayout(); self.processing_select_page=QCheckBox("انتخاب فهرست"); self.processing_select_page.setTristate(True); self.processing_select_page.clicked.connect(lambda checked:self.select_list("processing",checked)); toolbar.addWidget(self.processing_select_page); toolbar.addStretch(); self.processing_selection_summary=QLabel(objectName="selectionSummary"); toolbar.addWidget(self.processing_selection_summary); body.addLayout(toolbar); body.addWidget(self.processing_table); self.processing_empty=self.empty_list("هنوز سند فرآوری ثبت نشده", "برای شروع، یک سند فرآوری جدید ثبت کنید یا جست‌وجو را تغییر دهید."); body.addWidget(self.processing_empty); layout.addWidget(card,1)
        self.processing_export_bar=QWidget(); actions=QHBoxLayout(self.processing_export_bar); actions.setContentsMargins(0,0,0,0)
        for text,slot in (("خروجی Excel",self.export_processing_excel),("خروجی PDF",self.export_processing_pdf),("چاپ اسناد",self.print_processing)):
            button=QPushButton(text); button.clicked.connect(slot); actions.addWidget(button)
        actions.addStretch(); self.processing_export_bar.hide(); layout.addWidget(self.processing_export_bar); return page

    def new_processing(self):
        if not self.db.allows('processing'):return
        dialog=ProcessingDialog(self.db,self)
        if dialog.exec():self.refresh_processing(); self.start_sync(); message(self,"سند فرآوری","سند در حافظه محلی ثبت و وارد صف همگام‌سازی شد.")

    def refresh_processing(self):
        if not hasattr(self,"processing_table"):return
        if not self.db.allows('processing'):self.processing_table.setRowCount(0); self._processing_rows={}; return
        rows=self.db.processing_documents(); self._processing_rows={int(row["id"]):row for row in rows}
        query=self.processing_search.text().strip().translate(LATIN_DIGITS).casefold()
        rows=[row for row in rows if not query or query in " ".join(str(row[key] or "") for key in ("document_number","output_scrap_name","quality_expert_name")).translate(LATIN_DIGITS).casefold()]
        self.visible_processing_ids={int(row["id"]) for row in rows}; self.processing_table.setRowCount(len(rows)); self.processing_empty.setVisible(not rows); self.processing_table.setVisible(bool(rows)); self.update_list_selection("processing")
        for i,row in enumerate(rows):
            check=QCheckBox(); check.setChecked(int(row["id"]) in self.selected_processing_ids); check.setAccessibleName("انتخاب سند فرآوری " + row["document_number"]); check.toggled.connect(lambda checked,doc_id=int(row["id"]):self.update_list_selection("processing",doc_id,checked)); holder=QWidget(); select_layout=QHBoxLayout(holder); select_layout.setAlignment(Qt.AlignCenter); select_layout.addWidget(check); self.processing_table.setCellWidget(i,0,holder)
            inputs="، ".join(f"{x['scrap_name']} {fa_digits(f'{x['percentage']:g}')}٪" for x in self.db.processing_inputs(int(row["id"])))
            status="همگام‌شده" if row["total_count"] and row["ok_count"]==row["total_count"] else ("در صف ارسال" if row["total_count"] else "بدون مقصد")
            values=(row["document_number"],jalali_date(row["processed_at"]),inputs,row["output_scrap_name"],fa_digits(f"{int(row['output_weight_kg']):,} کیلوگرم"),row["quality_expert_name"],status)
            for col,value in enumerate(values,1):item=QTableWidgetItem(str(value)); item.setTextAlignment(Qt.AlignCenter); self.processing_table.setItem(i,col,item)
            view=QPushButton("مشاهده"); view.clicked.connect(lambda _=False,doc_id=int(row["id"]):self.view_processing(doc_id)); holder=QWidget(); action_layout=QHBoxLayout(holder); action_layout.setContentsMargins(6,12,6,12); action_layout.addWidget(view); self.processing_table.setCellWidget(i,8,holder)
            self.processing_table.setRowHeight(i,96)
            badge=QLabel(status,objectName="statusBadge"); badge.setAlignment(Qt.AlignCenter); set_tone(badge,"success" if status=="همگام‌شده" else "warning"); holder=QWidget(); status_layout=QHBoxLayout(holder); status_layout.setContentsMargins(6,12,6,12); status_layout.addWidget(badge); self.processing_table.setCellWidget(i,7,holder); self.processing_table.item(i,7).setText("")

        fit_table_controls(self.processing_table,(0,7,8))

    @requires_desktop_access('processing')
    def view_processing(self,document_id):
        row=getattr(self,"_processing_rows",{}).get(document_id)
        if not row:return
        parts=self.db.processing_inputs(document_id)
        status="همگام‌شده" if row["total_count"] and row["ok_count"]==row["total_count"] else "در انتظار ارسال"
        dialog=DocumentViewDialog(self,title="سند فرآوری قراضه",number=fa_digits(row["document_number"]),status=status,
            metrics=(("وزن خروجی",self._format_weight(row["output_weight_kg"])),("قراضه تولیدشده",row["output_scrap_name"])),
            sections=(("مشخصات فرآوری",(("تاریخ ثبت",jalali_date(row["processed_at"])),("کارشناس کنترل کیفیت",row["quality_expert_name"]),("روش ثبت وزن","ثبت دستی" if row["weight_source"]=="manual" else "باسکول"),("توضیحات",row["notes"]))),
                      ("ترکیب ورودی",tuple((x["scrap_name"],fa_digits(f"{float(x['percentage']):g}٪")) for x in parts)),
                      ("کارخانه و انبار",tuple(("انبار برداشت" if w["id"]==row["source_warehouse_id"] else "انبار دریافت",f"{(w.get('factory') or {}).get('name','')} / {w['name']} · {w['code']}") for w in (json.loads(row["warehouse_snapshot_json"] or "[]") if "warehouse_snapshot_json" in row.keys() else [])))),
            print_slot=lambda:self._print_html_documents([self.processing_html(row)],"چاپ سند فرآوری","a5"),
            pdf_slot=lambda:self._save_pdf_documents([self.processing_html(row)],f"{row['document_number']}.pdf","ذخیره سند فرآوری","a5"))
        dialog.exec()

    def _selected_processing(self):
        return [self._processing_rows[x] for x in self.selected_processing_ids if x in getattr(self,"_processing_rows",{})]

    def processing_html(self,row):
        esc=lambda value:html.escape(str(value if value not in (None,"") else "—"))
        inputs=self.db.processing_inputs(int(row["id"])); weight=float(row["output_weight_kg"])
        base=self.db.get_setting("admin_panel_url","").rstrip("/")
        verify=f"{base}/warehouse/scrap-processing?event={row['event_id']}" if base else row["event_id"]
        logo=self._file_data_uri(Path(__file__).parent/"assets/brand/megasanj-icon.png")
        qr=self._qr_data_uri(verify)
        source="ثبت دستی" if row["weight_source"]=="manual" else "دریافت از باسکول"
        rows="".join(f"<tr><td width='10%' align='center'>{fa_digits(i)}</td><td width='44%' align='right'>{esc(item['scrap_name'])}</td><td width='18%' align='center'>{fa_digits(f"{float(item['percentage']):g}")}٪</td><td width='28%' align='right'>{fa_digits(f"{round(weight*float(item['percentage'])/100):,}")} کیلوگرم</td></tr>" for i,item in enumerate(inputs,1))
        notes=("<p class='note' align='right' dir='rtl'><span class='caption'>توضیحات: </span>"+esc(row["notes"])+"</p>") if row["notes"] else ""
        warehouse_html=""
        for warehouse in (json.loads(row["warehouse_snapshot_json"] or "[]") if "warehouse_snapshot_json" in row.keys() else []):
            role="برداشت و دریافت" if row["source_warehouse_id"]==row["destination_warehouse_id"] else ("برداشت" if warehouse["id"]==row["source_warehouse_id"] else "دریافت")
            warehouse_html+=f"<tr><td>انبار {role}</td><td>{esc((warehouse.get('factory') or {}).get('name'))} / {esc(warehouse.get('name'))} · {esc(warehouse.get('code'))}</td></tr>"
        if warehouse_html:warehouse_html="<p class='section'>کارخانه و انبار</p><table width='100%'>"+warehouse_html+"</table>"
        output=f"""<!doctype html><html dir='rtl' lang='fa'><head><meta charset='utf-8'><style>
        body{{font-family:'PeydaWeb(FaNum)';direction:rtl;color:#243650;font-size:9pt;font-weight:400;text-align:right;margin:0}}
        p{{margin:0 0 5px;text-align:right}}table{{border-collapse:collapse}}td,th{{padding:7px;vertical-align:middle;border:1px solid #D8E2EE}}
        th{{background:#EDF3FA;color:#284C76;font-weight:500}}.header td{{border:0;padding:0 4px 10px}}.title{{font-size:16pt;font-weight:600;color:#173B67}}
        .caption{{font-size:7.5pt;color:#6B7F97;font-weight:400}}.value{{font-size:10pt;font-weight:500;color:#243650}}
        .section{{font-size:10pt;color:#173B67;font-weight:500;margin:12px 0 5px}}.note{{padding:8px;background:#F5F8FC;margin:10px 0}}
        .verify td{{border:0;padding:8px;background:#F1F7FC}}.footer{{font-size:7pt;color:#71839A;margin-top:10px;text-align:center}}
        </style></head><body>
        <table width='100%' class='header' dir='rtl'><tr><td width='13%' align='right'><img width='42' height='42' src='{logo}'></td><td width='57%' align='right'><span class='title'>سند فرآوری قراضه</span><br><span class='caption'>مگاسنج · سامانه یکپارچه {self.db.erp_label}</span></td><td width='30%' align='left'><span class='caption'>شماره سند</span><br><span style='font-size:8pt;color:#284C76' dir='ltr'>{esc(row['document_number'])}</span><br><span class='caption'>{jalali_date(row['processed_at'])}</span></td></tr></table>
        <p class='section' align='right' dir='rtl'>مشخصات فرآوری</p>
        <table width='100%' dir='rtl' cellspacing='0'><tr><td width='55%' align='right'><span class='caption'>قراضه تولیدشده</span><br><span class='value'>{esc(row['output_scrap_name'])}</span></td><td width='45%' align='right'><span class='caption'>وزن خالص خروجی</span><br><span class='value'>{fa_digits(f'{round(weight):,}')} کیلوگرم</span></td></tr><tr><td width='55%' align='right'><span class='caption'>کارشناس کنترل کیفیت</span><br>{esc(row['quality_expert_name'])}</td><td width='45%' align='right'><span class='caption'>روش ثبت وزن</span><br>{source}</td></tr></table>
        <p class='section' align='right' dir='rtl'>ترکیب ورودی‌ها</p>
        <table width='100%' dir='rtl' cellspacing='0'><tr><th width='10%' align='center'>ردیف</th><th width='44%' align='right'>نوع قراضه</th><th width='18%' align='center'>سهم</th><th width='28%' align='right'>وزن متناظر</th></tr>{rows}<tr><td colspan='2' align='right'>جمع کل</td><td align='center'>{fa_digits(f"{sum(float(x['percentage']) for x in inputs):g}")}٪</td><td align='right'>{fa_digits(f'{round(weight):,}')} کیلوگرم</td></tr></table>
        {notes}
        {warehouse_html}
        __REPORT_VERIFICATION__
        <p class='footer' align='center'>ثبت الکترونیکی توسط مگاسنج · تاریخ صدور {jalali_date(row['processed_at'])}</p>
        </body></html>"""
        return right_align_report_html(reverse_document_table_columns(output).replace("__REPORT_VERIFICATION__",verification_section(qr)))

    @requires_desktop_access('processing')
    def export_processing_pdf(self):
        rows=self._selected_processing()
        if not rows:return message(self,"PDF فرآوری","حداقل یک سند را انتخاب کنید.",True)
        self._save_pdf_documents([self.processing_html(row) for row in rows],"selected-processing-documents.pdf","ذخیره اسناد فرآوری","a5")
    @requires_desktop_access('processing')
    def print_processing(self):
        rows=self._selected_processing()
        if not rows:return message(self,"چاپ فرآوری","حداقل یک سند را انتخاب کنید.",True)
        self._print_html_documents([self.processing_html(row) for row in rows],"چاپ اسناد فرآوری","a5")
    @requires_desktop_access('processing')
    def export_processing_excel(self):
        rows=self._selected_processing()
        if not rows:return message(self,"Excel فرآوری","حداقل یک سند را انتخاب کنید.",True)
        path,_=QFileDialog.getSaveFileName(self,"ذخیره Excel فرآوری","processing-documents.xlsx","Excel (*.xlsx)")
        if not path:return
        if not path.lower().endswith(".xlsx"):path+=".xlsx"
        wb=Workbook(); ws=wb.active; ws.title="اسناد فرآوری"; ws.append(["شماره سند","تاریخ","ورودی‌ها","قراضه خروجی","وزن خروجی","کارشناس","منبع وزن","توضیحات"])
        for cell in ws[1]:cell.font=Font(bold=True,color="FFFFFF");cell.fill=PatternFill("solid",fgColor="1769E0");cell.alignment=Alignment(horizontal="center")
        for row in rows:ws.append([row["document_number"],jalali_date(row["processed_at"]),"، ".join(f"{x['scrap_name']} {x['percentage']}٪" for x in self.db.processing_inputs(int(row["id"]))),row["output_scrap_name"],int(row["output_weight_kg"]),row["quality_expert_name"],"دستی" if row["weight_source"]=="manual" else "باسکول",row["notes"]])
        ws.sheet_view.rightToLeft=True; wb.save(path); message(self,"خروجی","فایل Excel ذخیره شد.")

    def build_settings(self):
        page=QWidget(); page.setObjectName("settingsPage"); layout=QVBoxLayout(page); layout.setSpacing(14)
        hero=QFrame(objectName="pageHero"); hero_layout=QHBoxLayout(hero); hero_layout.setContentsMargins(24,18,24,18); title=QLabel("تنظیمات اتصال",objectName="pageHeroTitle"); hero_layout.addWidget(title); hero_layout.addStretch(); layout.addWidget(hero)
        tabs=QTabWidget(); self.settings_tabs=tabs; layout.addWidget(tabs,1)
        def tab(content, title):
            content.setObjectName("settingsContent"); scroll=QScrollArea(objectName="settingsScroll"); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame); scroll.setWidget(content); tabs.addTab(scroll,title)

        api=QWidget(); api_layout=QVBoxLayout(api); api_layout.setContentsMargins(8,16,8,8); api_layout.setSpacing(16)
        connection=QFrame(objectName='card'); connection_layout=QVBoxLayout(connection); connection_layout.setContentsMargins(24,22,24,22); connection_layout.setSpacing(12)
        connection_head=QHBoxLayout(); connection_title=QLabel('سامانه مرکزی '+self.db.erp_label); set_widget_style(connection_title,'font-size:19px;font-weight:800;color:#F3F7FF;background:transparent')
        edit_button=QPushButton('ویرایش اتصال'); edit_button.clicked.connect(self.edit_reference_apis); connection_head.addWidget(connection_title); connection_head.addStretch(); connection_head.addWidget(edit_button); connection_layout.addLayout(connection_head)
        connection_hint=QLabel('یک اتصال مشترک برای کالاها، تأمین‌کنندگان، بارنامه‌ها و اسناد باسکول.'); connection_hint.setWordWrap(True); set_widget_style(connection_hint,'color:#9CAFC8;background:transparent'); connection_layout.addWidget(connection_hint)
        self.central_server_label=QLabel(self.db.get_setting('admin_panel_url') or 'هنوز تنظیم نشده'); self.central_server_label.setLayoutDirection(Qt.LeftToRight); self.central_server_label.setTextInteractionFlags(Qt.TextSelectableByMouse); set_widget_style(self.central_server_label,'color:#62B8FF;background:#132842;border:1px solid #263B56;border-radius:12px;padding:14px;font-weight:700'); connection_layout.addWidget(self.central_server_label); api_layout.addWidget(connection)
        health=QFrame(objectName='card'); health_layout=QVBoxLayout(health); health_layout.setContentsMargins(24,22,24,22); health_layout.setSpacing(14)
        health_header=QHBoxLayout(); health_title=QLabel('اطلاعات مرجع و همگام‌سازی'); set_widget_style(health_title,'font-size:18px;font-weight:800;color:#F3F7FF;background:transparent')
        test_all=QPushButton('بررسی اتصال و به‌روزرسانی',objectName='primary'); test_all.clicked.connect(self.test_all_reference_apis); health_header.addWidget(health_title); health_header.addStretch(); health_header.addWidget(test_all); health_layout.addLayout(health_header)
        hint=QLabel('کالاها، اقلام و حوزه‌های معتبر تأمین در یک دریافت هماهنگ به‌روز می‌شوند. در قطع ارتباط، آخرین اطلاعات معتبر روی این رایانه حفظ می‌شود.'); hint.setWordWrap(True); set_widget_style(hint,'color:#9CAFC8;background:transparent'); health_layout.addWidget(hint)
        self.api_status_labels={}
        status_grid=QGridLayout(); status_grid.setHorizontalSpacing(18); status_grid.setVerticalSpacing(12)
        for row,(key,label) in enumerate((('materials','فهرست کالاها و حوزه‌های تأمین'),('waybill_search','ارتباط بارنامه‌ها'))):
            name=QLabel(label); set_widget_style(name,'color:#F3F7FF;font-weight:700;background:transparent'); status=QLabel('بررسی نشده'); status.setAlignment(Qt.AlignCenter); status.setMinimumWidth(200); self.api_status_labels[key]=status; self._set_api_status(key,'idle','بررسی نشده'); status_grid.addWidget(name,row,0); status_grid.addWidget(status,row,1)
        status_grid.setColumnStretch(0,1); health_layout.addLayout(status_grid)
        self.reference_summary=QLabel(); self.reference_summary.setWordWrap(True); set_widget_style(self.reference_summary,'color:#9CAFC8;background:#132842;padding:14px;border-radius:12px'); health_layout.addWidget(self.reference_summary); api_layout.addWidget(health); api_layout.addStretch(); tab(api,'اتصال و اطلاعات مرجع')

        hooks=QWidget(); hl=QVBoxLayout(hooks); hl.setContentsMargins(8,16,8,8); hl.setSpacing(14)
        hook_intro=QFrame(objectName="card"); hook_intro_layout=QVBoxLayout(hook_intro); hook_intro_layout.setContentsMargins(20,16,20,16)
        hook_title=QLabel("مقصدهای ارسال اسناد"); set_widget_style(hook_title, 'font-size:18px;font-weight:700;color:#F3F7FF;background:transparent')
        hook_hint=QLabel("مشخص کنید اسناد باسکول، بارنامه و فرآوری به کدام سامانه‌ها ارسال شوند. اتصال دریافت اطلاعات مرجع در زبانه اول مدیریت می‌شود."); hook_hint.setWordWrap(True); set_widget_style(hook_hint, 'color:#9CAFC8;background:transparent')
        hook_intro_layout.addWidget(hook_title); hook_intro_layout.addWidget(hook_hint); hl.addWidget(hook_intro)
        actions=QHBoxLayout(); add=QPushButton("＋ افزودن سایت / ERP",objectName="primary"); add.clicked.connect(self.add_endpoint); test=QPushButton("بررسی اتصال مقصد"); test.clicked.connect(self.test_selected_endpoint); disable=QPushButton("غیرفعال‌سازی سایت",objectName="danger"); disable.clicked.connect(self.disable_endpoint); actions.addWidget(add); actions.addWidget(test); actions.addWidget(disable); actions.addStretch(); hl.addLayout(actions)
        delete=QPushButton('حذف ERP',objectName='danger'); delete.clicked.connect(lambda checked=False:self.delete_endpoint()); actions.insertWidget(3,delete)
        self.endpoint_table=FullWidthTable(0,5); self.endpoint_table.setHorizontalHeaderLabels(["سیستم مدیریتی / ERP","آدرس پایه سایت","سرویس‌های فعال","وضعیت","شناسه"]); self.endpoint_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive);
        for column,width in enumerate((190,280,210,100,70)):self.endpoint_table.setColumnWidth(column,width)
        self.endpoint_table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeToContents); self.endpoint_table.horizontalHeader().setSectionResizeMode(4,QHeaderView.ResizeToContents); self.endpoint_table.configure_fill((0,1,2));
        self.endpoint_table.setSelectionBehavior(QAbstractItemView.SelectRows); self.endpoint_table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.endpoint_table.verticalHeader().setVisible(False); self.endpoint_table.doubleClicked.connect(self.edit_endpoint); style_table_scrollbars(self.endpoint_table); hl.addWidget(self.endpoint_table); tab(hooks,"مقصدهای ارسال")
        scale=QWidget(); scale_layout=QVBoxLayout(scale); scale_layout.setContentsMargins(8,16,8,8); scale_layout.setSpacing(14)
        scale_card=QFrame(objectName="card"); sf=QFormLayout(scale_card); sf.setContentsMargins(22,20,22,20); sf.setVerticalSpacing(14); sf.setHorizontalSpacing(24)
        self.scale_port=QLineEdit(self.db.get_setting("scale_port","COM1")); self.scale_port.setPlaceholderText("مثلاً COM1")
        self.scale_maximum=QSpinBox(); self.scale_maximum.setRange(1000,200000); self.scale_maximum.setSingleStep(1000); self.scale_maximum.setSuffix(" kg"); self.scale_maximum.setValue(int(self.db.get_setting("scale_maximum_weight_kg","60000")))
        profile=QLabel("TETA10-A  ·  9600 baud  ·  8 data bits  ·  No parity  ·  1 stop bit  ·  Handshake off")
        profile.setWordWrap(True); set_widget_style(profile, 'color:#62B8FF;background:#132842;border:1px solid #263B56;border-radius:10px;padding:12px;font-weight:700')
        protocol=QLabel("خواندن مستقیم فریم دودویی پنج‌بایتی توزین الکتریک؛ تنظیمات غیرمرتبط و الگوی متنی از برنامه حذف شده‌اند.")
        protocol.setWordWrap(True); set_widget_style(protocol, 'color:#9CAFC8;background:transparent')
        scale_save=QPushButton("ذخیره تنظیمات باسکول",objectName="primary"); scale_save.clicked.connect(self.save_scale_settings)
        sf.addRow("پورت ویندوز",self.scale_port); sf.addRow("حداکثر ظرفیت دستگاه",self.scale_maximum); sf.addRow("پروفایل ارتباطی ثابت",profile); sf.addRow("",protocol); sf.addRow("",scale_save)
        scale_layout.addWidget(scale_card); scale_layout.addStretch(); tab(scale,"باسکول و پورت سریال")
        transfer=QWidget(); transfer_layout=QVBoxLayout(transfer); transfer_layout.setContentsMargins(8,16,8,8); transfer_layout.setSpacing(16)
        transfer_card=QFrame(objectName='card'); transfer_body=QVBoxLayout(transfer_card); transfer_body.setContentsMargins(24,22,24,22); transfer_body.setSpacing(14)
        transfer_title=QLabel('انتقال تنظیمات به رایانه دیگر'); set_widget_style(transfer_title,'font-size:19px;font-weight:800;color:#F3F7FF;background:transparent'); transfer_body.addWidget(transfer_title)
        transfer_hint=QLabel('اتصال مرکزی، مقصدهای ارسال و تنظیمات دستگاه را در یک فایل نگهداری کنید. ورود فایل، تنظیمات را یکپارچه بازیابی می‌کند.'); transfer_hint.setWordWrap(True); set_widget_style(transfer_hint,'color:#9CAFC8;background:transparent'); transfer_body.addWidget(transfer_hint)
        buttons=QHBoxLayout(); export_button=QPushButton('خروجی تنظیمات',objectName='primary'); export_button.clicked.connect(self.export_settings); import_button=QPushButton('بازیابی از فایل'); import_button.clicked.connect(self.import_settings); buttons.addWidget(export_button); buttons.addWidget(import_button); buttons.addStretch(); transfer_body.addLayout(buttons); transfer_layout.addWidget(transfer_card); transfer_layout.addStretch(); tab(transfer,'انتقال و بازیابی')
        self.refresh_reference_summary()
        return page

    @requires_desktop_access('settings')
    def save_scale_settings(self):
        port=self.scale_port.text().strip().upper()
        if not re.fullmatch(r"COM\d+",port):return message(self,"تنظیمات باسکول","نام پورت باید مانند COM1 یا COM3 باشد.",True)
        for key,value in (("scale_port",port),("scale_baudrate","9600"),("scale_maximum_weight_kg",str(self.scale_maximum.value()))):self.db.set_setting(key,value)
        self.db.delete_settings(("scale_stability_samples","scale_tolerance_kg","scale_pattern"))
        self.scale_port.setText(port)
        message(self,"تنظیمات باسکول","تنظیمات ذخیره شد و در فرایند توزین بعدی اعمال می‌شود.")

    @requires_desktop_access('settings')
    def edit_reference_apis(self):
        dialog=ReferenceApiDialog(self.db,self)
        if not dialog.exec():return
        for key,value in dialog.values().items():self.db.set_setting(key,value)
        self.central_server_label.setText(self.db.get_setting('admin_panel_url')); self._reference_sync_sites.discard(self.db.session.base_url if self.db.session else self.db.path)
        self.start_reference_sync()
        message(self,'اتصال مرکزی','تنظیمات ذخیره شد و دریافت اطلاعات مرجع آغاز شد.')

    @requires_desktop_access('settings')
    def export_settings(self):
        if QMessageBox.question(self,"خروجی تنظیمات","این فایل شامل توکن‌های محرمانه اتصال است. آن را فقط روی رایانه مطمئن نگهداری کنید. فایل ساخته شود؟",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
        default=f"megasanj-settings-{datetime.now():%Y%m%d}.json"
        path,_=QFileDialog.getSaveFileName(self,"ذخیره فایل تنظیمات",default,"MegaSanj Settings (*.json)")
        if not path:return
        if not path.lower().endswith(".json"):path+=".json"
        try:
            Path(path).write_text(json.dumps(self.db.configuration_bundle(),ensure_ascii=False,indent=2),encoding="utf-8")
            message(self,"خروجی تنظیمات",f"فایل تنظیمات با موفقیت ساخته شد:\n{path}")
        except Exception as exc:message(self,"خروجی تنظیمات",f"ساخت فایل انجام نشد: {exc}",True)

    @requires_desktop_access('settings')
    def import_settings(self):
        path,_=QFileDialog.getOpenFileName(self,"انتخاب فایل تنظیمات","","MegaSanj Settings (*.json);;JSON (*.json)")
        if not path:return
        if QMessageBox.question(self,"ورود تنظیمات","تنظیمات اتصال و مقصدهای فعلی با اطلاعات این فایل جایگزین شوند؟",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
        try:
            bundle=json.loads(Path(path).read_text(encoding="utf-8-sig")); self.db.apply_configuration_bundle(bundle)
            self.scale_port.setText(self.db.get_setting("scale_port","COM1")); self.scale_maximum.setValue(int(self.db.get_setting("scale_maximum_weight_kg","60000")))
            self.refresh_endpoints()
            for key in self.api_status_labels:self._set_api_status(key,"idle","نیازمند بررسی")
            message(self,"ورود تنظیمات","همه تنظیمات با موفقیت اعمال شدند. برای اطمینان، بررسی اتصال‌ها را اجرا کنید.")
        except Exception as exc:message(self,"ورود تنظیمات",f"فایل اعمال نشد: {exc}",True)

    def _set_api_status(self,key,state,text):
        label=self.api_status_labels.get(key)
        if label is None:return
        palette={"idle":("#9CAFC8","#132842","#263B56"),"testing":("#FFB454","#3A3025","#665137"),"ok":("#32D6A0","#16372F","#2D6353"),"error":("#FF647C","#382333","#643547")}
        color,background,border=palette[state]
        label.setText(text); set_widget_style(label, f"color:{color};background:{background};border:1px solid {border};border-radius:9px;padding:8px 12px;font-weight:700")

    def refresh_reference_summary(self):
        if not hasattr(self,'reference_summary'):return
        types=self.db.material_types()
        with self.db.connection() as con:
            items=con.execute('SELECT COUNT(*) FROM material_items WHERE is_active=1').fetchone()[0]
            companies=con.execute('SELECT COUNT(*) FROM material_companies WHERE is_active=1').fetchone()[0]
        stamp=self.db.get_setting('material_reference_received_at')
        self.reference_summary.setText(f"{fa_digits(len(types))} نوع کالا  ·  {fa_digits(items)} قلم  ·  {fa_digits(companies)} تأمین‌کننده\nآخرین دریافت معتبر: {iran_jalali_datetime(stamp) if stamp else 'هنوز دریافت نشده'}")

    def test_all_reference_apis(self):
        for key in self.api_status_labels:self._set_api_status(key,'testing','در حال بررسی…')
        QApplication.processEvents(); QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            from .materials import fetch_materials
            result=fetch_materials(self.db.material_reference_url(),self.db.get_setting('transport_api_token'),self.db.get_setting('material_reference_version'))
            self.db.save_material_reference(result); self.db.refresh_reference_aliases(); self._set_api_status('materials','ok','اطلاعات مرجع به‌روز است')
        except Exception as exc:self._set_api_status('materials','error',str(exc)[:120])
        try:
            fetch_available_waybills(self.db.get_setting('waybill_available_url'),self.db.get_setting('transport_api_token'),'__connection_test__')
            self._set_api_status('waybill_search','ok','متصل و پاسخ‌گو')
        except Exception as exc:self._set_api_status('waybill_search','error',str(exc)[:120])
        finally:QApplication.restoreOverrideCursor(); self.refresh_reference_summary()

    def refresh_endpoints(self):
        rows=self.db.webhook_sites(); self.endpoint_table.setRowCount(len(rows))
        for i,r in enumerate(rows):
            endpoints=self.db.site_endpoints(int(r["id"]),True); labels={"weighbridge":"باسکول","waybill":"بارنامه","processing":"فرآوری"}
            endpoint_text="، ".join(labels.get(x["kind"],x["kind"]) for x in endpoints) or "بدون Endpoint فعال"
            for j,val in enumerate((r["name"],r["base_url"],endpoint_text,"فعال" if r["active"] else "غیرفعال",str(r["id"]))): self.endpoint_table.setItem(i,j,QTableWidgetItem(str(val)))

    def selected_endpoint(self):
        row=self.endpoint_table.currentRow()
        if row<0: return None
        site_id=int(self.endpoint_table.item(row,4).text())
        return self.db.webhook_site(site_id)

    @requires_desktop_access('settings')
    def add_endpoint(self):
        dlg=WebhookSiteDialog(self.db,parent=self)
        if dlg.exec():
            try:self.db.upsert_webhook_site(None,*dlg.values()); self.refresh_endpoints()
            except Exception as exc:message(self,"ذخیره اتصال",str(exc),True)

    @requires_desktop_access('settings')
    def edit_endpoint(self, _index=None):
        row=self.selected_endpoint()
        if not row:return
        dlg=WebhookSiteDialog(self.db,row,self)
        if dlg.exec():
            try:self.db.upsert_webhook_site(int(row["id"]),*dlg.values()); self.refresh_endpoints()
            except Exception as exc:message(self,"ذخیره اتصال",str(exc),True)

    @requires_desktop_access('settings')
    def disable_endpoint(self):
        row=self.selected_endpoint()
        if row and QMessageBox.question(self,"غیرفعال‌سازی سایت",f"ارسال همه Endpointهای «{row['name']}» متوقف شود؟",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)==QMessageBox.Yes:self.db.disable_webhook_site(int(row["id"])); self.refresh_endpoints()

    def test_selected_endpoint(self):
        row=self.selected_endpoint()
        if not row:return message(self,"انتخاب سایت","یک سایت یا ERP را انتخاب کنید.",True)
        endpoints=self.db.site_endpoints(int(row["id"]),True)
        if not endpoints:return message(self,"آزمایش اتصال","این سایت Endpoint فعالی ندارد.",True)
        labels={"weighbridge":"اسناد باسکول","waybill":"بارنامه‌ها","processing":"اسناد فرآوری"}; results=[]; all_ok=True
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for endpoint in endpoints:
                ok,text=test_endpoint(endpoint["url"],endpoint["token"]); all_ok=all_ok and ok; results.append(f"{labels.get(endpoint['kind'],endpoint['kind'])}: {'متصل' if ok else text}")
        finally:QApplication.restoreOverrideCursor()
        message(self,"نتیجه آزمایش سایت","\n".join(results),not all_ok)

    @requires_desktop_access('settings')
    def delete_endpoint(self):
        row=self.selected_endpoint()
        if not row:return message(self,'انتخاب ERP','ابتدا یک سایت یا ERP را انتخاب کنید.',True)
        if self.sync_thread and self.sync_thread.isRunning():return message(self,'حذف ERP','پس از پایان ارسال فعلی، حذف ERP را انجام دهید.',True)
        prompt=f"اتصال «{row['name']}» همراه Endpointها و سوابق ارسال محلی آن حذف شود؟\nخود اسناد و اطلاعات روی سرور حذف نمی‌شوند."
        if QMessageBox.question(self,'حذف ERP',prompt,QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
        if self.sync_thread and self.sync_thread.isRunning():return message(self,'حذف ERP','ارسال خودکار در حال انجام است؛ پس از پایان آن دوباره حذف را انجام دهید.',True)
        try:
            self.db.delete_webhook_site(int(row['id'])); self.refresh_endpoints(); self.refresh_documents(); self.refresh_waybills(); self.refresh_processing()
        except Exception as exc:message(self,'حذف ERP',str(exc),True)

    def new_document(self):
        if not self.db.allows('weighbridge'):return
        now=datetime.now().astimezone(); stamp=now.isoformat(timespec="seconds"); uid=str(uuid.uuid4()); number=f"WB-{now:%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
        movement=next(iter(MOVEMENTS))
        doc_id=self.db.create_draft(uid,number,movement,{"registered_at":stamp})
        try:
            dlg=self.document_dialog_class(self.db,self,self.db.advanced_document(doc_id),doc_id,created_for_dialog=True)
        except Exception as exc:
            self.db.delete_advanced_document(doc_id)
            return message(self,"باز کردن فرم ثبت سند",f"فرم ثبت سند باز نشد: {exc}",True)
        if dlg.exec():
            self.complete_draft(doc_id,dlg)
        elif self.db.advanced_document(doc_id):
            self.refresh_documents()
            message(self,"پیش‌نویس ذخیره شد",f"اطلاعات سند {number} ذخیره شد و بعداً می‌توانید آن را ادامه دهید.")

    def refresh_documents(self):
        if not self.db.allows('weighbridge'):
            self.table.setRowCount(0); self._document_rows=[]; self.render_document_cards([]); return
        rows=self.db.documents()
        if self.db.get_setting("server_snapshot_reconciled")=="1":
            # Legacy rows are never rendered after the first authoritative
            # replacement. Only advanced local drafts may coexist with cache.
            rows=[]
        advanced=self.db.advanced_documents()
        hourly=self.entry_time_enabled.isChecked()
        start=self.entry_time_from.time(); end=self.entry_time_to.time()
        today=datetime.now(timezone(timedelta(hours=3,minutes=30))).date()
        invalid=hourly and start>end
        self.entry_time_hint.setText("ساعت شروع باید قبل از ساعت پایان باشد." if invalid else "فقط اسناد ثبت‌شده امروز با ورود در این بازه · ساعت ایران" if hourly else "ساعت‌ها به وقت ایران هستند.")
        search=(self.search.text().strip().translate(LATIN_DIGITS).casefold() if hasattr(self,"search") else "")
        movement=self.movement_filter.currentData() if hasattr(self,"movement_filter") else ""
        status_filter=self.status_filter.currentData() if hasattr(self,"status_filter") else ""

        def in_entry_time_range(row):
            if not hourly:return True
            if invalid or "entry_at" not in row.keys():return False
            return entry_in_today_range(row["registered_at"],row["entry_at"],today,start.hour()*60+start.minute(),end.hour()*60+end.minute())

        def matches(row, is_advanced=False):
            company = (json.loads(row["company_snapshot"] or "{}") if is_advanced else {}) or {}
            searchable = " ".join(str(row[key] or "") for key in (("document_number", "driver_name", "driver_phone", "trailer_plate", "origin_city", "origin_province") if is_advanced else ("document_number", "scrap_name"))) + " " + str(company.get("name", "")) + " " + str(company.get("code", ""))
            searchable = searchable.translate(LATIN_DIGITS).casefold()
            row_movement = row["movement_type"]
            if is_advanced:
                row_status = "draft" if row["status"] != "finalized" else row["sync_status"]
            else:
                row_status = row["sync_status"]
            if is_advanced and weight_discrepancy(row):row_status="weight_discrepancy"
            return (not search or search in searchable.casefold()) and (not movement or row_movement == movement) and (not status_filter or row_status == status_filter) and in_entry_time_range(row)

        rows=[row for row in rows if matches(row)]
        advanced=[row for row in advanced if matches(row, True)]
        combined=[(row, True) for row in advanced] + [(row, False) for row in rows]
        combined.sort(key=lambda item: str(item[0]["registered_at"] or ""), reverse=True)
        self.filtered_document_rows=combined
        if hasattr(self,"sidebar_count"):
            self.sidebar_count.setText(f"{fa_digits(len(combined))}\nاسناد")
            self.sidebar_pending.setText(f"{fa_digits(sum(1 for row,adv in combined if ((row['status']!='finalized' or row['sync_status']!='synced') if adv else row['sync_status']!='synced')))}\nدر صف")
            self.sidebar_synced.setText(f"{fa_digits(sum(1 for row,adv in combined if row['sync_status']=='synced'))}\nارسال‌شده")
        if hasattr(self, "documents_stat_total"):
            self.documents_stat_total.setText(fa_digits(len(combined)))
            pending=sum(1 for row, is_advanced in combined if ((row["status"] != "finalized" or row["sync_status"] != "synced") if is_advanced else row["sync_status"] != "synced"))
            synced=sum(1 for row, is_advanced in combined if row["sync_status"] == "synced")
            self.documents_stat_pending.setText(fa_digits(pending)); self.documents_stat_synced.setText(fa_digits(synced))
        pages=max(1, (len(combined)+self.document_page_size-1)//self.document_page_size)
        self.document_page=max(0,min(pages-1,self.document_page))
        start=self.document_page*self.document_page_size
        visible=combined[start:start+self.document_page_size]
        self.render_document_cards(visible)
        advanced=[row for row,is_advanced in visible if is_advanced]
        rows=[row for row,is_advanced in visible if not is_advanced]
        self.current_rows=[row for row,is_advanced in visible if not is_advanced]
        if hasattr(self,"document_page_label"):
            self.document_page_label.setText(f"صفحه {fa_digits(self.document_page+1)} از {fa_digits(pages)} · {fa_digits(len(combined))} سند")
            self.document_prev.setEnabled(self.document_page>0); self.document_next.setEnabled(self.document_page<pages-1)
    def _plate_badge(self, value):
        holder=QWidget(); box=QHBoxLayout(holder); box.setContentsMargins(5,10,5,10)
        label=QLabel(fa_digits(value or "—")); label.setAlignment(Qt.AlignCenter); label.setToolTip(fa_digits(value or "پلاک ثبت نشده")); set_widget_style(label, "background:#132842;color:#F3F7FF;border:1px solid #263B56;border-radius:6px;padding:6px;font-size:12px;")
        box.addWidget(label); return holder

    @staticmethod
    def _format_weight(value):
        if value is None:return "—"
        number=f"{round(float(value)):,}"
        return fa_digits(f"{number} kg")

    def _set_document_actions(self, row, doc_id):
        holder=QWidget(); actions=QHBoxLayout(holder); actions.setContentsMargins(5,12,5,12); actions.setSpacing(6)
        button=QPushButton("انتخاب عملیات",objectName="tableActions"); button.setFixedWidth(145)
        menu=QMenu(button); menu.setLayoutDirection(Qt.RightToLeft)
        view=QAction("مشاهده اطلاعات",menu); view.triggered.connect(lambda checked=False, value=doc_id:self.details(value)); menu.addAction(view)
        print_action=QAction("چاپ اطلاعات",menu); print_action.triggered.connect(lambda checked=False, value=doc_id:self.print_document(value)); menu.addAction(print_action)
        pdf_action=QAction("ذخیره فایل PDF",menu); pdf_action.triggered.connect(lambda checked=False, value=doc_id:self.export_document_pdf(value)); menu.addAction(pdf_action)
        button.setMenu(menu); actions.addWidget(button); self.table.setCellWidget(row,7,holder)

    def selected_doc_id(self):
        row=self.table.currentRow(); return self.table.item(row,0).data(Qt.UserRole) if row>=0 else None

    @requires_desktop_access('weighbridge')
    def details(self, doc_id=None):
        doc_id=doc_id or self.selected_doc_id()
        if not doc_id:return
        advanced=doc_id<0
        d=self.db.advanced_document(-doc_id) if advanced else self.db.document(doc_id)
        if not d:return
        deliveries=self.db.advanced_delivery_details(-doc_id) if advanced else self.db.delivery_details(doc_id)
        delivery_fields=tuple((x["name"],f"{SYNC_LABELS.get(x['status'],x['status'])} · {fa_digits(x['attempts'])} تلاش" + (f" · {x['last_error']}" if "last_error" in x.keys() and x["last_error"] else "")) for x in deliveries)
        actions=[]
        printable=not advanced or d["status"]=="finalized"
        if advanced:
            names={"draft":"پیش‌نویس","first_weighed":"در انتظار توزین دوم","quality_assessed":"کنترل کیفیت","second_weighed":"توزین دوم ثبت شده","finalized":"نهایی‌شده"}
            status=names.get(d["status"],d["status"])
            weights,parts=self.db.document_parts(-doc_id)
            company=json.loads(d["company_snapshot"] or "{}") or {}
            metrics=(("وزن خالص توزین" if d["quality_pending"] else "وزن پذیرفته‌شده",self._format_weight(d["accepted_weight_kg"])),("افت",self._format_weight(d["loss_weight_kg"])),("برگشتی",self._format_weight(d["rejected_weight_kg"])))
            if len(weights)<2:metrics=(("وزن پر",self._format_weight(weights[0]['weight_kg']) if weights else '—'),("وضعیت","در انتظار توزین دوم"))
            sections=[("اطلاعات سند و مسیر",(("تاریخ ثبت",jalali_date(d["registered_at"])),("نوع گردش",MOVEMENTS.get(d["movement_type"],d["movement_type"])),("شرکت تأمین‌کننده",company.get("name")),("کد شرکت",company.get("code")),("شناسه ملی شرکت",company.get("national_id")),("مبدأ",f"{d['origin_province'] or '—'}، {d['origin_city'] or '—'}"),("نام راننده",d["driver_name"]),("شماره تماس",fa_digits(d["driver_phone"])),("پلاک تریلی",fa_digits(d["trailer_plate"])),("زمان ورود",iran_jalali_datetime(d["entry_at"])),("زمان خروج",iran_jalali_datetime(d["exit_at"])))),
                      ("سوابق توزین",tuple((f"توزین {fa_digits(x['sequence'])}",self._format_weight(x["weight_kg"])+(" · ثبت دستی" if x["source"]=="manual" else " · باسکول")) for x in weights)),
                      ("ترکیب بار و کنترل کیفیت",tuple((x["scrap_name"],fa_digits(f"{x['percentage']}٪")+" · "+self._format_weight(x["weight_kg"])) for x in parts)+(("درصد افت",fa_digits(f"{d['loss_percentage']}٪")),("درصد برگشتی",fa_digits(f"{d['rejected_percentage']}٪"))))]
            if not printable:actions.append(("ادامه تکمیل سند","resume"))
        else:
            status="همگام‌شده" if deliveries and all(x["status"]=="synced" for x in deliveries) else "در انتظار ارسال"
            metrics=(("وزن خالص",self._format_weight(d["weight_kg"])),("نوع قراضه",d["scrap_name"]))
            sections=[("مشخصات سند",(("تاریخ ثبت",jalali_date(d["registered_at"])),("نوع گردش",MOVEMENTS.get(d["movement_type"],d["movement_type"])),("شناسه رویداد",d["event_id"])))]
        if advanced:
            all_fields=dict(sections[0][1])
            sections[0:1]=[
                ("مشخصات سند",tuple((key,all_fields[key]) for key in ("تاریخ ثبت","نوع گردش","شرکت تأمین‌کننده"))),
                ("راننده و ناوگان",tuple((key,all_fields[key]) for key in ("نام راننده","شماره تماس","پلاک تریلی"))),
                ("مسیر و زمان‌بندی",(("مبدأ",all_fields["مبدأ"]),("مقصد",d["destination"] or "—"),("زمان ورود",all_fields["زمان ورود"]),("زمان خروج",all_fields["زمان خروج"])))
            ]
        if advanced and "warehouse_snapshot_json" in d.keys():
            warehouse_fields=[]
            for warehouse in json.loads(d["warehouse_snapshot_json"] or "[]"):
                role="مبدأ" if warehouse["id"]==d["source_warehouse_id"] else "مقصد"
                factory=warehouse.get("factory") or {}
                warehouse_fields.extend(((f"کارخانه {role}",factory.get("name")),(f"انبار {role}",f"{warehouse['name']} · {warehouse['code']}")))
            if warehouse_fields:sections.append(("کارخانه‌ها و انبارها",tuple(warehouse_fields)))
        if advanced and d["quality_pending"]:sections.insert(0,("کنترل کیفیت",(("وضعیت","سند باسکول ثبت شده؛ در انتظار بررسی کارشناس در مگاپالس"),)))
        certificate=weight_discrepancy(d) if advanced else {}
        if certificate:
            status="عدم تطابق وزن"
            def certificate_weight(key):
                value=certificate.get(key)
                if value is None:value=(certificate.get("comparison") or {}).get(key)
                if value is None:return "ثبت نشده"
                return fa_digits(f"{float(value):,.3f}".rstrip("0").rstrip("."))+" کیلوگرم"
            metrics=metrics+(("کسری وزن",certificate_weight("shortage_weight_kg")),)
            certificate_fields=[("شماره گواهی",certificate["certificate_number"]),
                ("کسری وزن",certificate_weight("shortage_weight_kg")),
                ("وزن خالص مبدأ",certificate_weight("origin_weight_kg")),
                ("وزن خالص مقصد",certificate_weight("destination_weight_kg")),
                ("وضعیت رسیدگی",{"open":"در انتظار رسیدگی","pending":"در انتظار رسیدگی","closed":"رسیدگی‌شده"}.get(certificate.get("status"),"گواهی صادر شده"))]
            if certificate.get("surplus_weight_kg") is not None:certificate_fields.append(("اضافه وزن",certificate_weight("surplus_weight_kg")))
            if certificate.get("severity_label"):certificate_fields.append(("درجه عدم تطابق",str(certificate["severity_label"])))
            if certificate.get("status")=="closed":
                certificate_fields.extend((("جریمه آهن",certificate_weight("penalty_weight_kg")),("شرح تصمیم",certificate.get("deduction_reason") or "—"),("شماره ملی راننده",certificate.get("driver_national_id") or "—")))
            sections.insert(0,("عدم تطابق وزن",tuple(certificate_fields)))
        sections.append(("وضعیت ارسال به سامانه‌ها",delivery_fields or (("مقصد ارسال","مقصدی ثبت نشده است"),)))
        if advanced and deliveries and all(x['status']=='needs_attention' and x['response_code'] in (409,422) for x in deliveries):actions.append(('اصلاح سند ردشده','revise'))
        if printable and any(x["status"]!="synced" for x in deliveries):actions.append(("ارسال مجدد","retry"))
        if certificate and certificate.get("status") != "closed":actions.append(("تکمیل صورت‌جلسه عدم تطابق","complete_discrepancy"))
        actions.append(("حذف سند محلی","delete"))
        sections.extend(self.extra_document_sections(d,doc_id))
        dialog=DocumentViewDialog(self,title="سند باسکول",number=fa_digits(d["document_number"]),status=status,
            metrics=metrics,sections=sections,actions=actions,printable=printable,
            print_slot=lambda:self.print_document(doc_id),pdf_slot=lambda:self.export_document_pdf(doc_id))
        dialog.exec()
        if dialog.action=="complete_discrepancy":
            WeightDiscrepancyDialog(self.db,d,self).exec()
            self.refresh_documents()
        elif dialog.action=="resume":
            editor=self.document_dialog_class(self.db,self,d,-doc_id)
            if editor.exec():self.complete_draft(-doc_id,editor)
            self.refresh_documents()
        elif dialog.action=='revise':
            try:
                self.db.reopen_rejected_document(-doc_id)
                revised=self.db.advanced_document(-doc_id)
                editor=self.document_dialog_class(self.db,self,revised,-doc_id)
                if editor.exec():self.complete_draft(-doc_id,editor)
                self.refresh_documents()
            except ValueError as exc:message(self,'اصلاح سند',str(exc),True)
        elif dialog.action=="retry":
            self.db.retry_advanced_document(-doc_id) if advanced else self.db.retry_document(doc_id)
            self.start_sync(); self.refresh_documents()
        elif dialog.action=="delete":self.delete_local_document(doc_id)

    @requires_desktop_access('weighbridge')
    def extra_document_sections(self, document, document_id):
        return []

    def delete_local_document(self, doc_id):
        confirm=QMessageBox.question(self,"حذف سند از این رایانه","این عملیات سند را فقط از دیتابیس داخلی اپ حذف می‌کند و قابل بازگردانی نیست. آیا مطمئن هستید؟",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if confirm!=QMessageBox.Yes:return
        try:
            if doc_id<0:self._delete_advanced_document(-doc_id)
            else:self.db.delete_document(doc_id)
            self.refresh_documents(); message(self,"سند حذف شد","سند با موفقیت از دیتابیس داخلی حذف شد.")
        except (ValueError,RuntimeError) as exc:message(self,"حذف انجام نشد",str(exc),True)

    def _delete_advanced_document(self, doc_id):
        """Release a reserved waybill first; never delete while its remote reservation is stranded."""
        document=self.db.advanced_document(doc_id)
        if not document:raise ValueError("سند در دیتابیس محلی پیدا نشد.")
        number=str(document["waybill_number"] or "").strip(); reservation=str(document["waybill_reservation_token"] or "").strip()
        if number and reservation:
            template=self.db.get_setting("waybill_release_url") or self.db.get_setting("waybill_reserve_url").replace("/reserve","/release")
            if not template or "{number}" not in template:raise RuntimeError("آدرس آزادسازی بارنامه در تنظیمات وارد نشده است؛ سند حذف نشد.")
            try:release_waybill(template,int(number),reservation,self.db.get_setting("transport_api_token"))
            except Exception as exc:raise RuntimeError(f"آزادسازی رزرو بارنامه انجام نشد و برای جلوگیری از مغایرت، سند حذف نشد:\n{exc}") from exc
        if number and reservation:self.db.release_local_waybill(int(number))
        self.db.delete_advanced_document(doc_id)

    @requires_desktop_access('weighbridge')
    def complete_draft(self,doc_id,dlg):
        self.db.finalize_document(doc_id,dlg.calculation)
        if dlg.selected_waybill:self.db.mark_waybill_used(int(dlg.selected_waybill["number"]))
        self.refresh_documents(); self.refresh_waybills(); self.start_sync(); message(self,"سند صادر شد","پیش‌نویس تکمیل و برای همگام‌سازی آماده شد.")

    def start_sync(self):
        if self._logging_out or not self.db.session:return
        if self.sync_thread and self.sync_thread.isRunning(): return
        if not self.server_sync_session_ready or self.db.get_setting("server_snapshot_reconciled")!="1":
            # Never upload historical local rows before the server has replaced
            # the cache. New offline drafts remain safely stored until then.
            self.sync_label.setText("● در انتظار دریافت داده معتبر")
            self.start_server_sync()
            return
        if not self.db.has_session_pending():
            self.sync_label.setText("● همگام"); self.start_server_sync(); return
        self.sync_thread=QThread(self); worker=SyncWorker(self.db); worker.moveToThread(self.sync_thread); self._worker=worker
        self.sync_thread.started.connect(worker.run); worker.progress.connect(self.sync_label.setText)
        worker.attention.connect(self.sync_attention)
        worker.weight_discrepancy.connect(self.show_weight_discrepancy)
        worker.finished.connect(self.sync_finished); worker.finished.connect(self.sync_thread.quit); worker.finished.connect(worker.deleteLater)
        self.sync_thread.finished.connect(self.sync_thread.deleteLater); self.sync_thread.finished.connect(self.sync_thread_closed)
        self.sync_label.setText("● در حال ارسال…"); self.sync_thread.start()

    def sync_finished(self,ok,fail):
        self.sync_label.setText(f"● {ok} موفق، {fail} ناموفق"); self.refresh_documents(); self.refresh_waybills(); self.refresh_processing()
        QTimer.singleShot(100,self.start_server_sync)

    def start_server_sync(self):
        if self._logging_out or not self.db.session:return
        if self.server_sync_thread and self.server_sync_thread.isRunning():return
        if not self.db.transportation_sync_url():return
        self.server_sync_thread=QThread(self); worker=ServerCacheSyncWorker(self.db); worker.moveToThread(self.server_sync_thread); self._server_sync_worker=worker
        self.server_sync_thread.started.connect(worker.run); worker.finished.connect(self.server_sync_finished)
        worker.finished.connect(self.server_sync_thread.quit); worker.finished.connect(worker.deleteLater)
        self.server_sync_thread.finished.connect(self.server_sync_thread.deleteLater); self.server_sync_thread.finished.connect(self.server_sync_thread_closed)
        self.side_last_sync.setText("دریافت آخرین اسناد از مگاپالس…"); self.server_sync_thread.start()

    def server_sync_finished(self,waybills,documents,error):
        if error:
            self.side_last_sync.setText("سامانه در دسترس نیست؛ نمایش آخرین کش معتبر")
            self.sidebar_status.setText("● حالت آفلاین")
            return
        self.server_sync_session_ready = True
        self.side_last_sync.setText(f"آخرین همگام‌سازی: اکنون · {fa_digits(waybills+documents)} تغییر")
        self.sidebar_status.setText("● همگام با مگاپالس")
        self.refresh_documents(); self.refresh_waybills()
        # If a new draft was completed while the snapshot was in flight, send
        # it only after the authoritative replacement has finished.
        if self.db.has_session_pending():
            QTimer.singleShot(0, self.start_sync)

    def server_sync_thread_closed(self):
        self.server_sync_thread=None; self._server_sync_worker=None

    def sync_attention(self, document_number, error):
        # Delivery status/error is already persisted by the worker. Background
        # failures must not interrupt the scale operator with a modal dialog.
        pass

    def show_weight_discrepancy(self, document_number, data):
        comparison=data.get("comparison", {})
        shortage=fa_digits(f"{int(data.get('shortage_weight_kg', 0)):,}")
        origin=fa_digits(f"{int(comparison.get('origin_weight_kg', 0)):,}")
        destination=fa_digits(f"{int(comparison.get('destination_weight_kg', 0)):,}")
        grade=fa_digits(data.get("severity_grade", "—"))
        certificate=data.get("certificate_number", "—")
        message(self, "عدم تطابق وزن بارنامه",
            f"وزن سند {document_number} با بارنامه تطابق ندارد.\n\n"
            f"وزن مبدأ: {origin} کیلوگرم\nوزن باسکول مقصد: {destination} کیلوگرم\n"
            f"کسری وزن: {shortage} کیلوگرم\nدرجه عدم تطابق: {grade} ({data.get('severity_label','')})\n"
            f"شماره گواهی: {certificate}\n\nگواهی صادر شد و مدیران کارخانه اطلاع‌رسانی شدند.", True)

    def sync_thread_closed(self):
        self.sync_thread = None
        self._worker = None

    def document_html(self,d):
        logo=self._file_data_uri(Path(__file__).parent/"assets/brand/megasanj-icon.png")
        output=f"""<html dir='rtl'><body style='font-family:&quot;PeydaWeb(FaNum)&quot;;margin:35px'>
        <table class='header' width='100%'><tr><td><div class='label'>شماره سند</div><div class='number'>{html.escape(d['document_number'])}</div></td><td><div class='title' style='color:#1769e0;font-size:18pt'>رسید باسکول مگافولاد</div></td><td><img width='48' height='48' src='{logo}'></td></tr></table><hr>
        <p>تاریخ و ساعت ثبت: {html.escape(d['registered_at'])}</p><p>نوع قراضه: {html.escape(d['scrap_name'])}</p><p>وزن خالص: <b>{round(d['weight_kg']):,} کیلوگرم</b></p><p>نوع گردش: {MOVEMENTS[d['movement_type']]}</p><br><br><p>امضای اپراتور: ........................</p></body></html>"""
        return right_align_report_html(output)

    def advanced_document_html(self,doc_id):
        d=self.db.advanced_document(doc_id)
        weights,_=self.db.document_parts(doc_id)
        company=json.loads(d["company_snapshot"] or "{}") or {}
        waybill=json.loads(d["waybill_snapshot"] or "{}") or {}
        esc=lambda value:html.escape(str(value if value not in (None,"") else "—"))
        first=float(weights[0]["weight_kg"] or 0) if len(weights)>0 else 0
        second=float(weights[1]["weight_kg"] or 0) if len(weights)>1 else 0
        gross=next((float(w['weight_kg']) for w in weights if w['role']=='gross'),0); tare=next((float(w['weight_kg']) for w in weights if w['role']=='tare'),0); net=max(0,gross-tare) if len(weights)>=2 else 0
        verify_url=self._weighbridge_verification_url(d)
        logo_uri=self._file_data_uri(Path(__file__).resolve().parent/"assets"/"brand"/"megasanj-icon.png")
        stamp_uri=self._file_data_uri(Path(__file__).resolve().parent/"assets"/"brand"/"megapulse-official-stamp.png")
        qr_uri=self._qr_data_uri(verify_url or str(d["event_id"]))
        waybill_number=waybill.get("display_number") or (f"BL-{int(d['waybill_number']):08d}" if str(d["waybill_number"] or "").isdigit() else d["waybill_number"] or "—")
        movement=MOVEMENTS.get(d["movement_type"],d["movement_type"])
        warehouse_html=""
        if "warehouse_snapshot_json" in d.keys():
            for warehouse in json.loads(d["warehouse_snapshot_json"] or "[]"):
                role="مبدأ" if warehouse["id"]==d["source_warehouse_id"] else "مقصد"
                warehouse_html+=f"<tr><td>کارخانه {role}: {esc((warehouse.get('factory') or {}).get('name'))}</td><td>انبار {role}: {esc(warehouse.get('name'))} · {esc(warehouse.get('code'))}</td></tr>"
            if warehouse_html:warehouse_html="<div class='section'>کارخانه و انبار</div><table>"+warehouse_html+"</table><p class='gap'>&nbsp;</p>"
        weighing_rows="".join(f"<tr><td>{'وزن پُر' if w['role']=='gross' else 'وزن خالی'}</td><td>{fa_digits(f'{round(float(w['weight_kg'])):,}')} کیلوگرم</td><td>{'ثبت دستی' if w['source']=='manual' else 'دریافت از باسکول'}</td><td>{iran_jalali_datetime(w['captured_at'])}</td><td>{esc(w['manual_reason'])}</td></tr>" for i,w in enumerate(weights))
        output = f"""<!doctype html><html dir='rtl' lang='fa'><head><meta charset='utf-8'><style>
        @page {{ size:A4 portrait; margin:6mm; }} body {{ direction:rtl;font-family:'PeydaWeb(FaNum)';color:#172033;margin:0;font-size:8.5pt }}
        .sheet {{ border:1.2px solid #173b67;padding:10px }} .header {{ width:100%;border-bottom:2px solid #1769d2;padding-bottom:5px }}
        .logo {{ width:48px;height:48px }} .title {{ color:#12396b;font-size:17pt;font-weight:800;margin:0 }} .number {{ color:#1769d2;font-size:11.5pt;font-weight:800;direction:ltr }}
        .section {{ margin:0 0 3px;padding:3px 7px;background:#eaf2fb;color:#173b67;font-size:9.5pt;font-weight:800;border-right:3px solid #1769d2 }}
        .gap {{ margin:0;height:6px;font-size:2px;line-height:6px }}
        table {{ width:100%;border-collapse:collapse;table-layout:fixed }} td,th {{ border:1px solid #cbd8e8;padding:4px;vertical-align:middle }} th {{ background:#f5f8fc;color:#173b67 }}
        .label {{ color:#64748b;font-size:7pt;margin-bottom:1px }} .value {{ color:#172033;font-size:8.5pt;font-weight:700 }} .metric {{ font-size:10.5pt;font-weight:900;color:#12396b }}
        .qr {{ width:68px;height:68px }} .notice {{ background:#ecfdf5;border:1px solid #a7f3d0;color:#086b4c;padding:4px;line-height:1.3 }}
        .sign td {{ height:48px;text-align:center;vertical-align:top;color:#52677f }} .stamp {{ width:40px;height:40px;margin-top:1px }}
        .footer {{ margin-top:5px;border-top:1px solid #cbd8e8;padding-top:4px;color:#64748b;font-size:6.8pt;text-align:center }}
        </style></head><body><div class='sheet'>
        <table class='header' width='100%'><tr><td width='31%' align='right' style='border:0'><div class='label'>تاریخ ثبت سند</div><div class='value'>{jalali_date(d['registered_at'])}</div></td><td width='58%' align='right' style='border:0'><div class='title'>سند رسمی توزین باسکول</div><div>سامانه یکپارچه {self.db.erp_label} · اپلیکیشن مگاسنج</div></td><td width='11%' align='center' style='border:0'><img class='logo' width='48' height='48' src='{logo_uri}'></td></tr></table>
        <p class='gap'>&nbsp;</p><div class='section'>مشخصات سند و حمل</div><table width='100%'><tr><td width='33%'><div class='label'>شماره بارنامه</div><div class='value'>{fa_digits(esc(waybill_number))}</div></td><td width='34%'><div class='label'>نوع گردش</div><div class='value'>{esc(movement)} · {esc(d['material_name'] if 'material_name' in d.keys() and d['material_name'] else 'قراضه')}</div></td><td width='33%'><div class='label'>شرکت تأمین‌کننده / مبدأ</div><div class='value'>{esc(company.get('name') or 'داخلی')}</div></td></tr>
        <tr><td><div class='label'>کد شرکت</div><div class='value'>{esc(company.get('code'))}</div></td><td><div class='label'>نام حقوقی شرکت</div><div class='value'>{esc(company.get('legal_name'))}</div></td><td><div class='label'>شناسه ملی شرکت</div><div class='value'>{fa_digits(esc(company.get('national_id')))}</div></td></tr>
        <tr><td><div class='label'>نام راننده</div><div class='value'>{esc(d['driver_name'])}</div></td><td><div class='label'>شماره همراه</div><div class='value'>{fa_digits(esc(d['driver_phone']))}</div></td><td><div class='label'>پلاک تریلی</div><div class='value'>{fa_digits(esc(d['trailer_plate']))}</div></td></tr>
        <tr><td><div class='label'>مبدأ</div><div class='value'>{esc(d['origin_province'])}، {esc(d['origin_city'])}</div></td><td><div class='label'>مقصد</div><div class='value'>{esc(d['destination'])}</div></td><td><div class='label'>شماره سند باسکول</div><div class='value'>{fa_digits(esc(d['document_number']))}</div></td></tr></table>
        <p class='gap'>&nbsp;</p><div class='section'>خلاصه رسمی توزین</div><table width='100%'><tr><td width='33%'><div class='label'>وزن پُر</div><div class='metric'>{fa_digits(f'{round(gross):,}')} کیلوگرم</div></td><td width='34%'><div class='label'>وزن خالی</div><div class='metric'>{fa_digits(f'{round(tare):,}')} کیلوگرم</div></td><td width='33%'><div class='label'>وزن خالص</div><div class='metric'>{fa_digits(f'{round(net):,}')} کیلوگرم</div></td></tr>
        </table>
        <p class='gap'>&nbsp;</p><div class='section'>ریز توزین‌ها</div><table width='100%'><tr><th width='12%'>مرحله</th><th width='15%'>وزن</th><th width='14%'>منبع</th><th width='17%'>زمان ثبت</th><th width='42%'>علت ثبت دستی</th></tr>{weighing_rows}</table>
        <p class='gap'>&nbsp;</p><div class='section'>زمان ورود و خروج تریلی</div><table width='100%'><tr><td width='50%'><div class='label'>ورود تریلی · ساعت ایران</div><div class='value'>{iran_jalali_datetime(d['entry_at'])}</div></td><td width='50%'><div class='label'>خروج تریلی · ساعت ایران</div><div class='value'>{iran_jalali_datetime(d['exit_at'])}</div></td></tr></table>
        {warehouse_html}
        __REPORT_VERIFICATION__
        <p class='gap'>&nbsp;</p><div class='section'>تأیید و امضا</div><table class='sign' width='100%'><tr><td width='33%'>امضا و اثر انگشت راننده</td><td width='34%'>مهر صادرکننده سند<br><img class='stamp' width='40' height='40' src='{stamp_uri}'></td><td width='33%'>مهر و امضای مسئول باسکول</td></tr></table>
        <div class='footer'>این سند به‌صورت الکترونیکی توسط سامانه مگاپالس تولید شده و اصالت آن تنها از طریق کد QR قابل بررسی است.</div></div></body></html>"""
        return right_align_report_html(reverse_document_table_columns(output).replace("__REPORT_VERIFICATION__",verification_section(qr_uri)))

    def _single_document_output(self, doc_id):
        """Return the local row, its type and printable HTML for one document."""
        if int(doc_id) < 0:
            advanced_id=-int(doc_id); row=self.db.advanced_document(advanced_id)
            if not row:return None
            if row["status"]!="finalized":
                message(self,"چاپ سند","این سند هنوز نهایی نشده است.",True); return None
            return row,True,self.advanced_document_html(advanced_id)
        with self.db.connection() as con:
            row=con.execute("SELECT d.*, COALESCE(s.sync_status,'no_endpoint') sync_status FROM documents d LEFT JOIN (SELECT document_id, CASE WHEN SUM(CASE WHEN status='synced' THEN 1 ELSE 0 END)=COUNT(*) THEN 'synced' ELSE 'pending' END sync_status FROM deliveries GROUP BY document_id) s ON s.document_id=d.id WHERE d.id=?",(int(doc_id),)).fetchone()
        if not row:return None
        return row,False,self.document_html(row)

    def _document_print_pages(self, row, advanced, output):
        pages=[output]
        certificate=weight_discrepancy(row) if advanced else {}
        if not certificate:return pages
        try:
            if not certificate.get("statement_html") or not certificate.get("verification_url"):
                from .services import request_weight_discrepancy
                certificate=request_weight_discrepancy(self.db.session,row["event_id"])
                self.db.save_document_weight_discrepancy(row["id"],certificate)
                if certificate.get("status")=="voided":return pages
            logo=self._file_data_uri(Path(__file__).resolve().parent/"assets"/"brand"/"megasanj-icon.png")
            qr=self._qr_data_uri(certificate.get("verification_url"))
            pages.append(minutes_html(row,certificate,logo,qr,iran_jalali_datetime(certificate.get("created_at") or row["registered_at"])))
        except ValueError as exc:
            message(self,"چاپ صورت‌جلسه",str(exc),True)
            return None
        return pages

    @requires_desktop_access('weighbridge')
    def print_document(self, doc_id):
        result=self._single_document_output(doc_id)
        if result:
            pages=self._document_print_pages(*result)
            if pages:self._print_html_documents(pages,"چاپ سند باسکول")

    @requires_desktop_access('weighbridge')
    def export_document_pdf(self, doc_id):
        result=self._single_document_output(doc_id)
        if not result:return
        row,_,output=result; default=f"{row['document_number']}.pdf"
        pages=self._document_print_pages(*result)
        if pages:self._save_pdf_documents(pages,default,"ذخیره PDF سند باسکول")

    @requires_desktop_access('weighbridge')
    def print_selected(self):
        rows=self._selected_documents()
        if not rows:return message(self,"چاپ","حداقل یک سند را انتخاب کنید.",True)
        outputs=[]
        for row,advanced in rows:
            if advanced:
                if row["status"]!="finalized":return message(self,"چاپ سند","تمام اسناد انتخابی باید نهایی شده باشند.",True)
                pages=self._document_print_pages(row,True,self.advanced_document_html(row["id"]))
                if pages is None:return
                outputs.extend(pages)
            else:outputs.append(self.document_html(row))
        self._print_html_documents(outputs,"چاپ اسناد باسکول انتخاب‌شده")

    @requires_desktop_access('weighbridge')
    def export_pdf(self):
        rows=self._selected_documents()
        if not rows:return message(self,"PDF","حداقل یک سند را انتخاب کنید.",True)
        outputs=[]
        for row,advanced in rows:
            if advanced:
                if row["status"]!="finalized":return message(self,"PDF سند","تمام اسناد انتخابی باید نهایی شده باشند.",True)
                pages=self._document_print_pages(row,True,self.advanced_document_html(row["id"]))
                if pages is None:return
                outputs.extend(pages)
            else:outputs.append(self.document_html(row))
        filename=(f"{rows[0][0]['document_number']}.pdf" if len(rows)==1 else "selected-weighbridge-documents.pdf")
        self._save_pdf_documents(outputs,filename,"ذخیره PDF اسناد انتخاب‌شده")

    @requires_desktop_access('weighbridge')
    def export_excel(self):
        rows=self._selected_documents()
        if not rows:return message(self,"Excel","حداقل یک سند را انتخاب کنید.",True)
        path,_=QFileDialog.getSaveFileName(self,"ذخیره Excel","weighbridge-documents.xlsx","Excel (*.xlsx)")
        if not path:return
        if not path.lower().endswith(".xlsx"):path += ".xlsx"
        wb=Workbook(); ws=wb.active; ws.title="اسناد باسکول"; headers=["شماره سند","زمان ثبت","نوع سند","نوع قراضه / ترکیب","وزن پُر","وزن خالی","وزن خالص","نوع گردش","پلاک تریلی","شرکت تأمین‌کننده","راننده","وضعیت"]
        ws.append(headers)
        for c in ws[1]: c.font=Font(bold=True,color="FFFFFF"); c.fill=PatternFill("solid",fgColor="1769E0"); c.alignment=Alignment(horizontal="center")
        for row,advanced in rows:
            if advanced:
                _weights,parts=self.db.document_parts(row["id"]); company=json.loads(row["company_snapshot"] or "{}") or {}
                measured=[round(float(item["weight_kg"] or 0)) for item in _weights]
                gross=max(measured,default=0); tare=min(measured) if len(measured)>=2 else 0; net=max(0,gross-tare)
                composition="، ".join(f"{part['scrap_name']} ({float(part['percentage']):g}٪)" for part in parts) or "—"
                status="پیش‌نویس" if row["status"]!="finalized" else SYNC_LABELS.get(row["sync_status"],row["sync_status"])
                ws.append([row["document_number"],row["registered_at"],"سند پیشرفته",composition,gross,tare,net,MOVEMENTS.get(row["movement_type"],row["movement_type"]),row["trailer_plate"] or "—",company.get("name") or "داخلی",row["driver_name"] or "—",status])
            else:
                ws.append([row["document_number"],row["registered_at"],"سند ساده",row["scrap_name"],"—","—",round(float(row["weight_kg"] or 0)),MOVEMENTS.get(row["movement_type"],row["movement_type"]),"—","—","—",SYNC_LABELS.get(row["sync_status"],row["sync_status"])])
        ws.sheet_view.rightToLeft=True
        for column in ws.columns:ws.column_dimensions[column[0].column_letter].width=max(14,min(34,max(len(str(cell.value or "")) for cell in column)+3))
        wb.save(path); message(self,"خروجی","فایل Excel ذخیره شد.")


