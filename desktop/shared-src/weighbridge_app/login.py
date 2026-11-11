from pathlib import Path
from PySide6.QtGui import QPixmap
from .theme import set_widget_style, ThemeToggle
from .design import app_style, set_tone
from PySide6.QtCore import QEventLoop, QThread, Signal, Qt, QTimer
from PySide6.QtWidgets import QDialog, QFrame, QCheckBox, QMessageBox, QLabel, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout

from .session_store import load_session, save_session, forget_session
from .auth import AuthenticationError, login_to_site, refresh_session, logout_from_site, fetch_profile_avatar


class LoginWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, site, username, password, parent=None, remember=False):
        super().__init__(parent)
        self.site, self.username, self.password = site, username, password
        self.remember = remember

    def run(self):
        try:
            session=login_to_site(self.site, self.username, self.password, remember=self.remember)
            session.avatar_data=fetch_profile_avatar(session)
            self.succeeded.emit(session)
        except AuthenticationError as exc:self.failed.emit(str(exc))
        except Exception:self.failed.emit("ورود انجام نشد؛ اتصال به سایت را بررسی کنید.")
        finally:self.password = ""


class RememberedLoginWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session

    def run(self):
        try:
            self.session.user = refresh_session(self.session)
            self.session.avatar_data = fetch_profile_avatar(self.session)
            self.succeeded.emit(self.session)
        except AuthenticationError as exc:
            if exc.invalid_session:
                forget_session()
            self.failed.emit(str(exc))
        except Exception:
            self.failed.emit("بررسی ورود ذخیره‌شده ممکن نشد؛ دوباره تلاش کنید یا با رمز وارد شوید.")


def restore_startup_session():
    """Validate remembered credentials while the splash is the only visible UI."""
    session = load_session()
    if session is None:
        return None, ""
    result = {"session": None, "error": ""}
    loop = QEventLoop()
    worker = RememberedLoginWorker(session)
    worker.succeeded.connect(lambda saved: result.update(session=saved))
    worker.failed.connect(lambda error: result.update(error=error))
    worker.finished.connect(loop.quit)
    worker.start()
    loop.exec()
    worker.wait()
    return result["session"], result["error"]


class SessionCheckWorker(QThread):
    succeeded = Signal(dict)
    failed = Signal(str, bool)

    def __init__(self, session, parent=None):
        super().__init__(parent); self.session = session

    def run(self):
        try:self.succeeded.emit(refresh_session(self.session))
        except AuthenticationError as exc:self.failed.emit(str(exc), exc.invalid_session)
        except Exception:self.failed.emit("دریافت اطلاعات حساب فعلاً ممکن نیست.", False)


class LogoutWorker(QThread):
    def __init__(self, session, parent=None):
        super().__init__(parent); self.session = session

    def run(self):logout_from_site(self.session)


class LoginDialog(QDialog):
    def __init__(self, db, parent=None, restore=False):
        super().__init__(parent)
        self.db, self.session, self.worker = db, None, None
        self.setWindowTitle("ورود به مگاسنج")
        self.setLayoutDirection(Qt.RightToLeft); self.setMinimumWidth(460); self.resize(480,640)
        set_widget_style(self, app_style())
        layout=QVBoxLayout(self); layout.setContentsMargins(32,30,32,28); layout.setSpacing(14)
        appearance=QHBoxLayout(); appearance.addStretch(); self.theme_toggle=ThemeToggle(); appearance.addWidget(self.theme_toggle); layout.addLayout(appearance)
        icon_path=getattr(db,'app_icon_path',Path(__file__).resolve().parent / 'assets/brand/megasanj-icon.png')
        logo=QLabel(); logo.setAlignment(Qt.AlignCenter); logo.setPixmap(QPixmap(str(icon_path)).scaled(64,64,Qt.KeepAspectRatio,Qt.SmoothTransformation)); layout.addWidget(logo)
        title=QLabel("ورود به مگاسنج",objectName="heroTitle"); title.setAlignment(Qt.AlignCenter)
        subtitle=QLabel("با حساب کاربری "+db.erp_label+" وارد شوید"); subtitle.setWordWrap(True); subtitle.setObjectName("mutedText"); subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(title); layout.addWidget(subtitle); layout.addSpacing(8)
        self.site=QLineEdit(db.get_setting("login_site_url") or db.get_setting("admin_panel_url"))
        self.site.setPlaceholderText("https://erp.example.com"); self.site.setLayoutDirection(Qt.LeftToRight)
        self.username=QLineEdit(db.get_setting("login_username")); self.username.setPlaceholderText("ایمیل یا شماره موبایل")
        self.username.setLayoutDirection(Qt.LeftToRight)
        self.password=QLineEdit(); self.password.setEchoMode(QLineEdit.Password); self.password.setPlaceholderText("رمز عبور")
        for label,field in [("آدرس سایت "+db.erp_label,self.site),("حساب کاربری",self.username),("رمز عبور",self.password)]:
            caption=QLabel(label); caption.setBuddy(field); field.setAccessibleName(label); layout.addWidget(caption); layout.addWidget(field)
        self.remember=QCheckBox("مرا به خاطر بسپار · ۳۰ روز"); self.remember.setToolTip("ورود خودکار در این حساب ویندوز؛ بدون ذخیره رمز عبور")
        layout.addWidget(self.remember)
        self.status=QLabel(""); self.status.setWordWrap(True); self.status.setObjectName("notice"); set_tone(self.status,"error"); self.status.hide()
        layout.addWidget(self.status)
        self.button=QPushButton("ورود به برنامه",objectName="primary"); self.button.clicked.connect(self.submit); layout.addWidget(self.button)
        hint=QLabel("دسترسی بخش‌ها بر اساس نقش حساب شما تعیین می‌شود."); hint.setWordWrap(True); hint.setObjectName("mutedText"); layout.addWidget(hint)
        self.password.returnPressed.connect(self.submit)
        if restore:QTimer.singleShot(0,self.try_saved_session)

    def submit(self):
        if self.worker and self.worker.isRunning():return
        if not self.site.text().strip() or not self.username.text().strip() or not self.password.text():
            set_tone(self.status,"error"); self.status.show(); self.status.setText("آدرس سایت، حساب کاربری و رمز عبور را وارد کنید."); return
        self.set_busy(True); self.button.setText("در حال ورود…"); set_tone(self.status,"warning"); self.status.show(); self.status.setText("در حال بررسی حساب…")
        self.worker=LoginWorker(self.site.text(),self.username.text(),self.password.text(),self,remember=self.remember.isChecked())
        self.worker.succeeded.connect(self._success); self.worker.failed.connect(self._failure)
        self.worker.finished.connect(self._finished); self.worker.start()

    def set_busy(self, busy):
        for widget in (self.site,self.username,self.password,self.remember,self.button):widget.setEnabled(not busy)

    def try_saved_session(self):
        session=load_session()
        if not session:return
        self.site.setText(session.base_url); self.username.setText(str(session.user.get("email") or session.user.get("mobile") or "")); self.remember.setChecked(True)
        self.set_busy(True); self.status.show(); set_tone(self.status,"warning"); self.status.setText("در حال بررسی ورود ذخیره‌شده…")
        self.worker=RememberedLoginWorker(session,self)
        self.worker.succeeded.connect(self._success); self.worker.failed.connect(self._failure)
        self.worker.finished.connect(self._finished); self.worker.start()

    def _failure(self, text):
        set_tone(self.status,"error"); self.status.setText(text)

    def _success(self, session):
        self.session=session
        self.db.set_setting("login_site_url",session.base_url)
        self.db.set_setting("login_username",self.username.text().strip())
        self.password.clear()
        try:
            save_session(session)
        except OSError:
            session.remembered=False
            forget_session()
            QMessageBox.warning(self,"ذخیره ورود","ورود انجام شد، اما ذخیره امن نشست روی این رایانه ممکن نبود.")

    def _finished(self):
        self.set_busy(False); self.button.setText("ورود به برنامه")
        if self.session:self.accept()

    def reject(self):
        if self.worker and self.worker.isRunning():return
        super().reject()

    def closeEvent(self,event):
        if self.worker and self.worker.isRunning():event.ignore(); return
        super().closeEvent(event)
