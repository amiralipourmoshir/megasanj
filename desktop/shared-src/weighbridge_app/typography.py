"""Bundled Peyda fonts shared by the desktop interface and printed documents."""

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

FONT_FAMILY = "PeydaWeb(FaNum)"


def apply_app_font(app):
    if not app.property("peydaFontsLoaded"):
        for path in sorted((Path(__file__).parent / "assets" / "fonts").glob("PeydaWebFaNum-*.ttf")):
            if QFontDatabase.addApplicationFont(str(path)) < 0:
                raise RuntimeError(f"Unable to load bundled font: {path.name}")
        app.setProperty("peydaFontsLoaded", True)
    app.setFont(QFont(FONT_FAMILY, 10))
