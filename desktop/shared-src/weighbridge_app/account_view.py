"""Branded, read-only account profile."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from .theme import set_widget_style
from .avatars import circular_avatar
from .design import app_style


class AccountDialog(QDialog):
    def __init__(self, session, parent=None, *, format_datetime=str):
        super().__init__(parent)
        self.setWindowTitle("اطلاعات حساب کاربری")
        self.setLayoutDirection(Qt.RightToLeft)
        self.resize(640, 730)
        self.setMinimumSize(480, 480)
        set_widget_style(self, app_style() + """
            QFrame#accountHero { background:#132842; border:1px solid #315B94; border-radius:16px; }
            QLabel#accountAvatar { background:#1D4969; color:white; border-radius:36px; font-size:30px; font-weight:800; }
            QLabel#accountTitle { font-size:23px; font-weight:800; }
            QFrame#accountSection { background:#0D1B30; border:1px solid #263B56; border-radius:12px; }
            QFrame#accountField { background:#081426; border:1px solid #203249; border-radius:8px; }
            QLabel#accountValue { font-size:14px; font-weight:600; }
            QLabel#accountCaption { color:#9CAFC8; font-size:12px; }
            QLabel#accountCapability { background:#17345D; color:#A9D4FF; padding:10px; border-radius:8px; }
        """)
        user = session.user
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 18)
        root.setSpacing(16)
        header = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QPixmap(str(Path(__file__).parent / "assets/brand/megasanj-icon.png")).scaled(42, 42, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        header.addWidget(logo)
        title = QVBoxLayout()
        title.addWidget(self.label("حساب کاربری", "sectionTitle"))
        title.addWidget(self.label("مگاسنج · مشخصات و دسترسی‌های شما", "accountCaption"))
        header.addLayout(title, 1)
        root.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 6, 0)
        body.setSpacing(14)
        hero = QFrame(objectName="accountHero")
        identity = QVBoxLayout(hero)
        identity.setContentsMargins(22, 22, 22, 22)
        identity.setSpacing(8)
        avatar = self.label(str(user.get("name") or "؟")[:1], "accountAvatar")
        avatar.setFixedSize(72, 72)
        avatar.setAlignment(Qt.AlignCenter)
        pixmap = QPixmap()
        if session.avatar_data and pixmap.loadFromData(session.avatar_data):
            avatar.setText("")
            avatar.setPixmap(circular_avatar(pixmap,72))
        identity.addWidget(avatar, 0, Qt.AlignHCenter)
        for value, name in ((user.get("name"), "accountTitle"),
                            (" · ".join(user.get("role_labels") or user.get("roles") or []), "accountCaption")):
            label = self.label(value, name)
            label.setAlignment(Qt.AlignCenter)
            identity.addWidget(label)
        body.addWidget(hero)

        self.add_section(body, "اطلاعات فردی و تماس", (
            ("شناسه کاربر", user.get("id"), True),
            ("شماره موبایل", user.get("mobile"), True),
            ("ایمیل", user.get("email"), True),
        ))
        self.add_section(body, "اطلاعات اتصال و ورود", (
            ("سامانه متصل", session.base_url, True),
            ("آخرین ورود · ساعت ایران", format_datetime(user["last_login_at"]) if user.get("last_login_at") else None, False),
        ), columns=1)
        card = QFrame(objectName="accountSection")
        access = QVBoxLayout(card)
        access.setContentsMargins(16, 16, 16, 16)
        access.addWidget(self.label("بخش‌های در دسترس", "sectionTitle"))
        labels = {"weighbridge": "اسناد باسکول", "waybills": "بارنامه‌ها", "processing": "اسناد فرآوری", "settings": "تنظیمات"}
        chips = QGridLayout()
        capabilities = user.get("weighbridge_capabilities") or []
        for index, key in enumerate(capabilities):
            chip = self.label(labels.get(key, key), "accountCapability")
            chip.setAlignment(Qt.AlignCenter)
            chips.addWidget(chip, index // 2, index % 2)
        access.addLayout(chips)
        if not capabilities:
            access.addWidget(self.label("بخشی برای این حساب فعال نیست", "accountCaption"))
        body.addWidget(card)
        body.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        footer = QHBoxLayout()
        footer.addWidget(self.label("اطلاعات از حساب سامانه مرکزی دریافت شده است", "accountCaption"), 1)
        close = QPushButton("بستن", objectName="primary")
        close.setMinimumWidth(100)
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        root.addLayout(footer)

    @staticmethod
    def label(value, name):
        label = QLabel(str(value) if value is not None and value != "" else "—", objectName=name)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return label

    def add_section(self, body, title, fields, columns=2):
        card = QFrame(objectName="accountSection")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(self.label(title, "sectionTitle"))
        grid = QGridLayout()
        grid.setSpacing(10)
        for index, (caption, value, ltr) in enumerate(fields):
            field = QFrame(objectName="accountField")
            box = QVBoxLayout(field)
            box.setContentsMargins(12, 10, 12, 10)
            box.addWidget(self.label(caption, "accountCaption"))
            text = self.label(value, "accountValue")
            if ltr:
                text.setLayoutDirection(Qt.LeftToRight)
                text.setAlignment(Qt.AlignRight | Qt.AlignAbsolute)
            box.addWidget(text)
            grid.addWidget(field, index // columns, index % columns)
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)
        body.addWidget(card)
