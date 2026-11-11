"""Application-wide themes, including local component styles and native popups."""
import math
import re
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton, QApplication

from .design import app_style


# Pair each existing dark surface with a deliberately chosen light counterpart.
# Keep source styles intact so repeated switches never accumulate replacements.
LIGHT_COLORS = {
    "#081426": "#F3F6FC", "#0d1b30": "#FFFFFF", "#112139": "#F4F7FC",
    "#132842": "#EDF3FC", "#13253d": "#EAF0F9", "#102039": "#F5F8FD",
    "#101f33": "#EDF1F7", "#173052": "#E8F1FF", "#1c3555": "#DFEAFB",
    "#203f67": "#D5E4FA", "#183763": "#DFEAFC", "#17345d": "#E8F1FF",
    "#1b365b": "#DCEAFF", "#1d4969": "#DDEBFC", "#263b56": "#D2DDED",
    "#203249": "#DCE4F0", "#213951": "#D5E0EF", "#304763": "#C9D7EB",
    "#315b94": "#B3CBEF", "#466791": "#90ACD0", "#526987": "#91A7C5",
    "#354c69": "#AFBFD5", "#f3f7ff": "#172B4D", "#edf7ff": "#172B4D",
    "#9cafc8": "#566C89", "#94a3b8": "#64748B", "#71839c": "#8291A8",
    "#62b8ff": "#185FB5", "#a9d4ff": "#245D9F", "#3987ff": "#2563EB",
    "#16372f": "#E4F6EE", "#32d6a0": "#087E59", "#2d6353": "#9AD8BD",
    "#3a3025": "#FFF2D8", "#ffb454": "#9A5B00", "#665137": "#E0C185", "#382333": "#FFF0F3",
    "#ff647c": "#C42B4A", "#643547": "#E8B3BF",
}


def current_theme():
    app = QApplication.instance()
    return (app.property("colorTheme") if app else None) or "dark"


def theme_color(value, mode=None):
    return LIGHT_COLORS.get(value.lower(), value) if (mode or current_theme()) == "light" else value


def themed_style(source, mode=None):
    if (mode or current_theme()) == "dark":
        return source
    result = re.sub(r"#[0-9a-fA-F]{6}\b", lambda match: theme_color(match[0], "light"), source)
    # Text using the named white color belongs to dark-era labels. Strong blue
    # action buttons are explicitly kept white by the shared stylesheet below.
    result = re.sub(r"(?<=:)\s*white\b", "#172B4D", result)

    def contrast(declarations):
        background = re.search(r"(?<![\w-])background(?:-color)?\s*:\s*(#[0-9a-fA-F]{6})\b", declarations)
        if background:
            color = QColor(background[1])
            # Solid brand blue surfaces need white text, including custom
            # component rules that override the shared primary-button style.
            if color.blueF() > color.redF() * 1.2 and color.hsvSaturationF() > .5 and color.lightnessF() < .65:
                declarations = re.sub(r"(?<![\w-])color\s*:[^;{}]+;?", "", declarations)
                declarations = declarations.rstrip() + ";color:rgb(255,255,255);"
        return declarations

    if "{" not in result:
        return contrast(result)
    return re.sub(r"\{([^{}]*)\}", lambda match: "{" + contrast(match[1]) + "}", result)


def set_widget_style(widget, source):
    widget.setProperty("themeSourceStyle", source)
    widget.setStyleSheet(themed_style(source))


class ThemeEvents(QObject):
    changed = Signal(str)


def theme_events():
    app = QApplication.instance()
    if not hasattr(app, "_theme_events"):
        app._theme_events = ThemeEvents(app)
    return app._theme_events


def theme_settings():
    return QSettings("MegaFoolad", "MegaSanj")


def restore_theme():
    set_theme(theme_settings().value("appearance/theme", "dark"), persist=False)


def set_theme(mode, *, persist=True):
    mode = mode if mode in ("light", "dark") else "dark"
    app = QApplication.instance()
    app.setProperty("colorTheme", mode)
    palette = QPalette()
    roles = {
        QPalette.Window: "#081426", QPalette.WindowText: "#F3F7FF",
        QPalette.Base: "#0D1B30", QPalette.AlternateBase: "#102039",
        QPalette.Text: "#F3F7FF", QPalette.Button: "#132842",
        QPalette.ButtonText: "#F3F7FF", QPalette.ToolTipBase: "#132842",
        QPalette.ToolTipText: "#F3F7FF", QPalette.Highlight: "#245BDB",
        QPalette.PlaceholderText: "#9CAFC8", QPalette.Link: "#62B8FF",
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(theme_color(color, mode)))
    palette.setColor(QPalette.HighlightedText, QColor("white"))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(theme_color("#71839C", mode)))
    app.setPalette(palette)
    app.setStyleSheet(themed_style(app_style(), mode))
    for widget in app.allWidgets():
        source = widget.property("themeSourceStyle")
        if source is not None:
            style = themed_style(source, mode)
            if widget.styleSheet() != style:
                widget.setStyleSheet(style)
        if widget.property("themeIconPath"):
            _refresh_icon(widget)
        widget.update()
    if persist:
        settings = theme_settings()
        settings.setValue("appearance/theme", mode)
        settings.sync()
    theme_events().changed.emit(mode)


def set_navigation_icon(label, path, size=23):
    label.setProperty("themeIconPath", str(path))
    label.setProperty("themeIconSize", size)
    _refresh_icon(label)


def _refresh_icon(label):
    svg = Path(label.property("themeIconPath")).read_text(encoding="utf-8")
    if current_theme() == "light":
        svg = re.sub(r"#fff(?:fff)?\b", "#245D9F", svg, flags=re.I)
        svg = re.sub(r"#[0-9a-fA-F]{6}\b", lambda match: theme_color(match[0], "light"), svg)
    renderer = QSvgRenderer(svg.encode())
    size = label.property("themeIconSize") or 23
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    label.setPixmap(pixmap)


class ThemeToggle(QAbstractButton):
    """Keyboard-accessible sun/moon switch, drawn without font glyphs."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(68, 30)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCheckable(True)
        self.clicked.connect(lambda: set_theme("dark" if self.isChecked() else "light"))
        theme_events().changed.connect(self.sync_theme)
        self.sync_theme(current_theme())

    def sync_theme(self, mode):
        self.setChecked(mode == "dark")
        self.setToolTip("فعال‌کردن حالت روشن" if mode == "dark" else "فعال‌کردن حالت تیره")
        self.setAccessibleName(self.toolTip())
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(theme_color("#466791" if self.hasFocus() else "#263B56")), 1))
        painter.setBrush(QColor(theme_color("#0D1B30")))
        painter.drawRoundedRect(QRectF(1, 1, 66, 28), 14, 14)
        dark = self.isChecked()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme_color("#17345D")))
        painter.drawRoundedRect(QRectF(36 if dark else 4, 4, 28, 22), 10, 10)
        sun_color=QColor(theme_color("#9CAFC8" if dark else "#62B8FF"))
        painter.setPen(QPen(sun_color, 1.3, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(18, 15), 3.5, 3.5)
        for index in range(8):
            angle=index*math.pi/4
            painter.drawLine(QPointF(18+6*math.cos(angle),15+6*math.sin(angle)),QPointF(18+8*math.cos(angle),15+8*math.sin(angle)))
        moon=QPainterPath(); moon.addEllipse(QRectF(43,8,14,14))
        cutout=QPainterPath(); cutout.addEllipse(QRectF(48,5,13,13))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme_color("#62B8FF" if dark else "#9CAFC8")))
        painter.drawPath(moon.subtracted(cutout))

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)
