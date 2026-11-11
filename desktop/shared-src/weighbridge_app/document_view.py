"""Shared branded document viewer for weighbridge, waybill and processing records."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from .theme import set_widget_style
from .design import app_style, set_tone


class DocumentViewDialog(QDialog):
    def __init__(self, parent, *, title, number, status, sections, metrics=(),
                 print_slot=None, pdf_slot=None, actions=(), printable=True):
        super().__init__(parent)
        self.action = None
        self.setWindowTitle(title)
        self.setLayoutDirection(Qt.RightToLeft)
        set_widget_style(self, app_style() + """
            QLabel#detailValue { color:#F3F7FF; font-size:14px; font-weight:600; }
            QLabel#detailCaption { color:#9CAFC8; font-size:12px; }
            QFrame#detailSection { background:#0D1B30; border:1px solid #304763; border-radius:14px; }
            QFrame#detailField { background:#112139; border:1px solid #213951; border-radius:9px; }
            QLabel#sectionTitle { padding:2px 0 8px; color:#62B8FF; font-size:17px; }
            QFrame#metricCard { border-top:2px solid #3987FF; }
        """)
        self.setMinimumSize(740, 600)
        self.resize(940, 780)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(16)
        header = QHBoxLayout()
        logo = QLabel()
        logo.setFixedSize(64, 64)
        path = Path(__file__).resolve().parent / "assets/brand/megasanj-icon.png"
        logo.setPixmap(QPixmap(str(path)).scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        header.addWidget(logo)
        heading = QVBoxLayout()
        heading.addWidget(self.label(title, "heroTitle"))
        heading.addWidget(self.label("مگاسنج · سامانه مدیریت اسناد صنعتی", "mutedText"))
        header.addLayout(heading, 1)
        badge = self.label(status, "statusBadge")
        badge.setMaximumHeight(34)
        set_tone(badge, "error" if status == "عدم تطابق وزن" else "success" if status in ("همگام‌شده", "ارسال‌شده", "نهایی‌شده", "قابل استفاده") else "warning")
        header.addWidget(badge)
        root.addLayout(header)
        identifier = self.label(number, "documentCardNumber")
        identifier.setLayoutDirection(Qt.LeftToRight)
        identifier.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        identifier.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(identifier)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        if metrics:
            row = QHBoxLayout()
            for caption, value in metrics:
                card = QFrame(objectName="metricCard")
                layout = QVBoxLayout(card)
                layout.setContentsMargins(18, 14, 18, 14)
                layout.addWidget(self.label(caption, "metricTitle"))
                label = self.label(value, "metricValue")
                label.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
                layout.addWidget(label)
                row.addWidget(card, 1)
            body.addLayout(row)
        for section_title, fields in sections:
            card = QFrame(objectName="detailSection")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(20, 16, 20, 18)
            layout.setSpacing(16)
            layout.addWidget(self.label(section_title, "sectionTitle"))
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(12)
            for index, (caption, value) in enumerate(fields):
                field_card = QFrame(objectName="detailField")
                field = QVBoxLayout(field_card)
                field.setContentsMargins(14, 12, 14, 14)
                field.setSpacing(8)
                field.addWidget(self.label(caption, "detailCaption"))
                label = self.label(str(value if value not in (None, "") else "—"), "detailValue")
                label.setTextInteractionFlags(Qt.TextSelectableByMouse)
                field.addWidget(label)
                grid.addWidget(field_card, index // 2, index % 2)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            layout.addLayout(grid)
            body.addWidget(card)
        body.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        footer = QHBoxLayout()
        for text, key in actions:
            button = QPushButton(text, objectName="danger" if key == "delete" else "secondary")
            button.clicked.connect(lambda _checked=False, key=key: self.choose_action(key))
            footer.addWidget(button)
        footer.addStretch()
        self.print_button = QPushButton("چاپ سند")
        self.pdf_button = QPushButton("ذخیره PDF", objectName="primary")
        for button, slot in ((self.print_button, print_slot), (self.pdf_button, pdf_slot)):
            button.setEnabled(printable and slot is not None)
            if slot is not None:
                button.clicked.connect(lambda _checked=False, slot=slot: slot())
            if not printable:
                button.setToolTip("چاپ و PDF پس از نهایی‌شدن سند فعال می‌شود.")
            footer.addWidget(button)
        close = QPushButton("بستن")
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        root.addLayout(footer)

    @staticmethod
    def label(text, name):
        label = QLabel(str(text), objectName=name)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
        return label

    def choose_action(self, key):
        self.action = key
        self.accept()
