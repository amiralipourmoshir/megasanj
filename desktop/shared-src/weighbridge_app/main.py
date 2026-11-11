import os
import sys
from time import monotonic
from pathlib import Path

from PySide6.QtCore import QEventLoop, QLocale, QTimer, Qt
from PySide6.QtGui import QFont, QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

from .database import Database
from .ui import MainWindow
from .theme import set_widget_style, restore_theme
from .design import app_style
from .login import LoginDialog, restore_startup_session


def data_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    return root / "MegaFoolad" / ("WeighbridgeOdoo" if os.environ.get('MEGASANJ_EDITION') == 'odoo' else "Weighbridge")


def main():
    QApplication.setAttribute(Qt.AA_DontUseNativeDialogs)
    app = QApplication(sys.argv)
    QLocale.setDefault(QLocale(QLocale.Persian, QLocale.Iran))
    app.setStyle("Fusion")
    set_widget_style(app, app_style())
    app.setLayoutDirection(Qt.RightToLeft)
    app.setApplicationName("MegaSanj")
    app.setApplicationDisplayName("MegaSanj")
    app.setApplicationVersion("3.0.0")
    app.setOrganizationName("MegaFoolad")
    from .typography import apply_app_font
    apply_app_font(app)
    restore_theme()
    icon_path = Path(__file__).resolve().parent / "assets" / "brand" / "megasanj-icon.png"
    if os.environ.get('MEGASANJ_EDITION') == 'odoo':
        from megasanj_odoo.integration import OdooDatabase
        icon_path = OdooDatabase.app_icon_path
    app.setWindowIcon(QIcon(str(icon_path)))

    splash_started = monotonic()
    splash_path = Path(__file__).resolve().parent / "assets" / "splash" / "megasanj-splash.png"
    splash_pixmap = QPixmap(str(splash_path))
    if not splash_pixmap.isNull():
        splash_pixmap = splash_pixmap.scaled(
            760, 420, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        splash = QSplashScreen(
            splash_pixmap,
            Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint,
        )
        splash.setAttribute(Qt.WA_TranslucentBackground)
        splash.show()
        app.processEvents()
    else:
        splash = None

    database_class, window_class = Database, MainWindow
    if os.environ.get('MEGASANJ_EDITION') == 'odoo':
        from megasanj_odoo.integration import OdooDatabase, OdooMainWindow
        database_class, window_class = OdooDatabase, OdooMainWindow
        app.setApplicationName('MegaSanjOdoo')
        app.setApplicationDisplayName('مگاسنج · اودو')
    db = database_class(data_dir() / "weighbridge.sqlite3")
    session,login_error=(None, "") if os.environ.get("MEGASANJ_SMOKE_TEST") == "1" else restore_startup_session()
    if splash is not None:
        remaining=max(0,1200-int((monotonic()-splash_started)*1000))
        if remaining:
            wait=QEventLoop(); QTimer.singleShot(remaining,wait.quit); wait.exec()
    if session is None:
        if splash is not None:splash.hide()
        login = LoginDialog(db)
        if login_error:
            login.status.show(); login._failure(login_error)
        if os.environ.get("MEGASANJ_SMOKE_TEST") == "1":
            login.show(); QTimer.singleShot(3000, app.quit)
            raise SystemExit(app.exec())
        if not login.exec():return
        session=login.session
    db = db.for_session(session)
    window = window_class(db)

    def reveal_main_window():
        window.show()
        window.raise_()
        window.activateWindow()
        if splash is not None:
            splash.finish(window)

    reveal_main_window()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
