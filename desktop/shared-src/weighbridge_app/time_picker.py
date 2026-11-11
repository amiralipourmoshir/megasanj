"""Bounded 24-hour time picker with a draggable clock and sliders."""
import math

from PySide6.QtCore import QPointF, QTime, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

from .theme import set_widget_style, theme_color
from .design import app_style


class ClockDial(QWidget):
    valueChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = "hour"
        self.value = 0
        self.setFixedSize(220, 220)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("ساعت قابل تنظیم با کشیدن عقربه")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        center = QPointF(110, 110)
        painter.setPen(QPen(QColor(theme_color("#263B56")), 1))
        painter.setBrush(QColor(theme_color("#081426")))
        painter.drawEllipse(center, 104, 104)
        count = 24 if self.mode == "hour" else 60
        step = 3 if self.mode == "hour" else 5
        for value in range(0, count, step):
            angle = value * math.tau / count - math.pi / 2
            point = center + QPointF(math.cos(angle)*84, math.sin(angle)*84)
            painter.setPen(QColor(theme_color("#9CAFC8")))
            text = str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
            painter.drawText(int(point.x())-16, int(point.y())-12, 32, 24, Qt.AlignCenter, text)
        angle = self.value * math.tau / count - math.pi / 2
        tip = center + QPointF(math.cos(angle)*64, math.sin(angle)*64)
        painter.setPen(QPen(QColor(theme_color("#3987FF")), 3))
        painter.drawLine(center, tip)
        painter.setBrush(QColor(theme_color("#3987FF")))
        painter.drawEllipse(tip, 7, 7)
        painter.drawEllipse(center, 4, 4)

    def _drag(self, event):
        point = event.position() - QPointF(110, 110)
        angle = (math.atan2(point.y(), point.x()) + math.pi/2) % math.tau
        count = 24 if self.mode == "hour" else 60
        self.value = round(angle * count / math.tau) % count
        self.valueChanged.emit(self.value)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._drag(event)


class TimePicker(QPushButton):
    timeChanged = Signal(QTime)

    def __init__(self, time, parent=None):
        super().__init__(parent)
        self._time = QTime(time)
        self.setMinimumWidth(110)
        self.popup = QFrame(self, Qt.Popup)
        self.popup.setObjectName("timePopup")
        set_widget_style(self.popup, app_style() + """
            QFrame#timePopup { background:#0D1B30; border:1px solid #466791; border-radius:12px; }
            QSlider::groove:horizontal { height:6px; background:#263B56; border-radius:3px; }
            QSlider::sub-page:horizontal { background:#3987FF; border-radius:3px; }
            QSlider::handle:horizontal { width:20px; margin:-7px 0; background:#62B8FF; border-radius:10px; }
        """)
        self.popup.setLayoutDirection(Qt.RightToLeft)
        layout = QVBoxLayout(self.popup)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        self.readout = QLabel()
        self.readout.setAlignment(Qt.AlignCenter)
        set_widget_style(self.readout, "color:#F3F7FF;font-size:28px;font-weight:700;")
        layout.addWidget(self.readout)
        tabs = QHBoxLayout()
        for title, mode in (("ساعت", "hour"), ("دقیقه", "minute")):
            button = QPushButton(title)
            button.clicked.connect(lambda _checked=False, mode=mode: self.set_mode(mode))
            tabs.addWidget(button)
        layout.addLayout(tabs)
        self.dial = ClockDial()
        self.dial.valueChanged.connect(self.dial_changed)
        layout.addWidget(self.dial, 0, Qt.AlignCenter)
        self.hour_slider = QSlider(Qt.Horizontal)
        self.minute_slider = QSlider(Qt.Horizontal)
        for caption, slider, maximum in (("ساعت · ۰ تا ۲۳", self.hour_slider, 23),
                                        ("دقیقه · ۰ تا ۵۹", self.minute_slider, 59)):
            slider.setRange(0, maximum)
            slider.setLayoutDirection(Qt.LeftToRight)
            slider.setAccessibleName(caption)
            slider.valueChanged.connect(self.sliders_changed)
            layout.addWidget(QLabel(caption))
            layout.addWidget(slider)
        done = QPushButton("تأیید ساعت", objectName="primary")
        done.clicked.connect(self.popup.hide)
        layout.addWidget(done)
        self.clicked.connect(self.open_popup)
        self._update_controls()

    def time(self):
        return QTime(self._time)

    def setTime(self, value):
        if not value.isValid() or value == self._time:
            return
        self._time = QTime(value.hour(), value.minute())
        self._update_controls()
        self.timeChanged.emit(self.time())

    def _update_controls(self):
        text = self._time.toString("HH:mm")
        self.setText(text + "  ▾")
        self.readout.setText(text)
        for slider, value in ((self.hour_slider, self._time.hour()), (self.minute_slider, self._time.minute())):
            slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(False)
        self.set_mode(self.dial.mode)

    def set_mode(self, mode):
        self.dial.mode = mode
        self.dial.value = self._time.hour() if mode == "hour" else self._time.minute()
        self.dial.update()

    def dial_changed(self, value):
        self.setTime(QTime(value, self._time.minute()) if self.dial.mode == "hour"
                     else QTime(self._time.hour(), value))

    def sliders_changed(self, _value):
        self.setTime(QTime(self.hour_slider.value(), self.minute_slider.value()))

    def open_popup(self):
        self.popup.adjustSize()
        point = self.mapToGlobal(self.rect().bottomLeft())
        screen = self.screen().availableGeometry()
        point.setX(max(screen.left(), min(point.x(), screen.right()-self.popup.width())))
        point.setY(min(point.y()+6, screen.bottom()-self.popup.height()))
        self.popup.move(point)
        self.popup.show()
