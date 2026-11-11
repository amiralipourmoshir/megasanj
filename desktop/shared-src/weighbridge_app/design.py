"""Magical Blue design tokens and shared desktop component styling."""

TOKENS = {
    "canvas": "#081426", "panel": "#0D1B30", "raised": "#132842",
    "line": "#263B56", "brand": "#3987FF", "text": "#F3F7FF",
    "muted": "#9CAFC8", "success": "#32D6A0", "warning": "#FFB454",
    "error": "#FF647C",
}

MAGICAL_BLUE_STYLE = """
* { font-family:"PeydaWeb(FaNum)"; font-size:13px; color:@text; }
QMainWindow, QWidget#root { background:@canvas; }
QLabel { background:transparent; }
QFrame#sidebar { background:@panel; border:1px solid @line; border-radius:16px; }
QFrame#brandCard { background:transparent; border:0; border-bottom:1px solid @line; border-radius:0; }
QLabel#brandName { color:@text; font-size:24px; font-weight:800; }
QLabel#brandTagline { color:@muted; font-size:11px; }
QPushButton#nav { background:transparent; border:1px solid transparent; border-radius:10px; padding:0; min-height:46px; max-height:46px; }
QPushButton#nav:hover { background:@raised; }
QPushButton#nav:checked { background:#183763; border:1px solid #315B94; }
QLabel#navLabel { color:@text; font-size:13px; font-weight:600; }
QFrame#sidebarStats, QFrame#sideInfo { background:@panel; border:0; border-top:1px solid @line; border-radius:0; }
QLabel#sidebarStatsTitle, QLabel#sideInfoTitle { color:@muted; font-size:11px; font-weight:600; }
QLabel#sideInfoValue, QLabel#sidebarStatsText { color:@muted; font-size:11px; }
QLabel#sideInfoDot { color:@success; }
QLabel#sidebarMiniBlue, QLabel#sidebarMiniAmber, QLabel#sidebarMiniGreen { border-radius:8px; padding:8px 2px; font-size:11px; }
QLabel#sidebarMiniBlue { background:#17345D; color:#62B8FF; }
QLabel#sidebarMiniAmber { background:#3A3025; color:@warning; }
QLabel#sidebarMiniGreen { background:#16372F; color:@success; }
QFrame#documentsHero, QFrame#pageHero { background:transparent; border:0; border-radius:0; }
QLabel#heroTitle, QLabel#pageHeroTitle { font-size:27px; font-weight:800; color:@text; }
QLabel#heroSubtitle { color:@muted; font-size:12px; }
QLabel#heroBadge { color:@muted; background:transparent; border:0; padding:0; }
QFrame#card, QFrame#paginationCard, QFrame#wizardHeader, QFrame#wizardCard { background:@panel; border:1px solid @line; border-radius:12px; }
QFrame#metricCard { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #132842,stop:1 #0D1B30); border:1px solid @line; border-radius:12px; }
QLabel#metricTitle { color:@muted; font-size:12px; }
QLabel#metricValue { color:@text; font-size:27px; font-weight:800; }
QLabel#metricValue[tone="success"] { color:@success; }
QLabel#metricValue[tone="warning"] { color:@warning; }
QPushButton, QToolButton { background:@raised; color:@text; border:1px solid @line; border-radius:8px; padding:9px 14px; min-height:20px; }
QPushButton:hover, QToolButton:hover { background:#1C3555; border-color:#466791; }
QPushButton:pressed { background:#203F67; }
QPushButton:focus, QToolButton:focus { border-color:#62B8FF; }
QPushButton#primary, QPushButton#cardActionPrimary { background:#245BDB; border:1px solid #3987FF; color:white; border-radius:8px; }
QPushButton#primary:hover, QPushButton#cardActionPrimary:hover { background:#3275ED; }
QPushButton:disabled { color:#71839C; background:#101F33; border-color:#203249; }
QPushButton#danger, QPushButton#cardActionDanger { color:@error; background:#382333; border:1px solid #643547; }
QLineEdit, QComboBox, QTextEdit, QSpinBox, QDoubleSpinBox, QTimeEdit { background:@canvas; color:@text; border:1px solid @line; border-radius:8px; padding:8px 10px; min-height:22px; selection-background-color:#245BDB; }
QLineEdit:focus, QComboBox:focus, QTextEdit:focus { border-color:@brand; }
QLineEdit:hover, QComboBox:hover { border-color:#466791; }
QComboBox::drop-down { width:26px; background:transparent; border:0; }
QTableWidget { background:@panel; color:@text; border:0; alternate-background-color:#102039; gridline-color:@line; selection-background-color:#1B365B; selection-color:@text; }
QTableWidget::item { padding:10px; border-bottom:1px solid #203249; }
QTableWidget::item:hover { background:#173052; }
QHeaderView { background:#13253D; }
QHeaderView::section { background:#13253D; color:@muted; border:0; border-bottom:1px solid @line; padding:12px 8px; font-size:12px; font-weight:600; }
QTableCornerButton::section { background:#13253D; border:0; }
QCheckBox { background:transparent; color:@muted; spacing:8px; }
QCheckBox::indicator { width:18px; height:18px; border:1px solid #526987; border-radius:5px; background:@canvas; }
QCheckBox::indicator:checked { background:#245BDB; border-color:@brand; image:url(__CHECK_ICON__); }
QCheckBox::indicator:focus, QCheckBox::indicator:hover { border-color:#62B8FF; }
QLabel#statusBadge { background:#17345D; color:#62B8FF; border:0; border-radius:7px; padding:5px 10px; font-size:11px; font-weight:600; }
QLabel#statusBadge[tone="success"] { background:#16372F; color:@success; }
QLabel#statusBadge[tone="warning"] { background:#3A3025; color:@warning; }
QLabel#statusBadge[tone="error"] { background:#382333; color:@error; }
QLabel#documentCardNumber { font-size:13px; color:@text; font-weight:700; }
QLabel#documentCardInfo, QLabel#selectionSummary { color:@muted; font-size:12px; }
QLabel#emptyTitle { font-size:20px; font-weight:700; color:@text; }
QLabel#emptyHint { font-size:13px; color:@muted; }
QMenu { background:@raised; color:@text; border:1px solid @line; border-radius:8px; padding:6px; }
QMenu::item { padding:9px 24px; border-radius:5px; }
QMenu::item:selected { background:#245BDB; }
QToolTip { background:@raised; color:@text; border:1px solid #466791; padding:8px; }
QScrollArea { background:@panel; border:0; }
QScrollBar:vertical { background:@panel; width:9px; margin:0; }
QScrollBar::handle:vertical { background:#354C69; min-height:30px; border-radius:4px; }
QScrollBar:horizontal { background:@panel; height:9px; margin:0; }
QScrollBar::handle:horizontal { background:#354C69; min-width:30px; border-radius:4px; }
QScrollBar::add-line, QScrollBar::sub-line { width:0; height:0; }
QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }
"""
MAGICAL_BLUE_STYLE += """
QScrollArea > QWidget > QWidget { background:@canvas; }
QWidget#settingsContent, QWidget#waybillContent { background:@canvas; }
QDialog, QMessageBox { background:@canvas; color:@text; }
QDialog QLabel, QMessageBox QLabel { color:@text; background:transparent; }
QDialogButtonBox QPushButton { min-width:90px; }
QDialogButtonBox QPushButton#primary { background:#245BDB; border:1px solid @brand; color:white; }
QWidget#settingsPage, QWidget#settingsPage QStackedWidget, QTabWidget::pane { background:@canvas; border:0; }
QTabBar::tab { background:@panel; color:@muted; border:1px solid @line; padding:12px 20px; margin:0 0 0 6px; border-radius:8px; }
QTabBar::tab:selected { background:#183763; color:@text; border-color:#315B94; }
QTabBar::tab:hover { color:@text; background:@raised; }
QFrame#documentHero, QFrame#waybillHero { background:transparent; border:0; }
QLabel#documentHeroTitle, QLabel#waybillHeroTitle { font-size:25px; font-weight:800; color:@text; }
QLabel#documentHeroText, QLabel#waybillHeroText { font-size:12px; color:@muted; }
QLabel#documentHeroMark, QLabel#waybillHeroMark { background:#17345D; border:1px solid @line; border-radius:12px; }
QFrame#waybillSection, QFrame#platePanel { background:@panel; border:1px solid @line; border-radius:12px; }
QLabel#wizardPageTitle, QLabel#waybillSectionTitle, QLabel#platePanelTitle { color:@text; font-size:17px; font-weight:700; }
QLabel#wizardPageHint, QLabel#waybillSectionHint, QLabel#platePanelHint, QLabel#waybillFieldLabel { color:@muted; font-size:12px; }
QLabel#wizardStep { background:@canvas; color:@muted; border:1px solid @line; padding:10px; border-radius:8px; }
QLabel#wizardStep[current="true"] { color:#62B8FF; background:#17345D; border-color:#315B94; }
QLabel#wizardStep[done="true"] { color:@success; background:#16372F; border-color:#2D6353; }
QScrollArea#wizardScroll, QScrollArea#waybillScroll, QScrollArea#settingsScroll { background:transparent; border:0; }
QFrame#waybillField { background:@canvas; border:1px solid @line; border-radius:8px; }
QFrame#waybillField QLineEdit, QFrame#waybillField QSpinBox, QFrame#waybillField QComboBox { background:transparent; border:0; padding:4px; }
QFrame#waybillField:hover { border-color:#466791; }
QFrame#iranPlate { background:@canvas; border:2px solid #466791; border-radius:10px; min-height:80px; max-height:80px; }
QFrame#plateCountry { background:#245BDB; border:0; border-top-left-radius:8px; border-bottom-left-radius:8px; }
QLabel#plateCountryText { color:white; font-size:9px; font-weight:700; }
QLineEdit#plateDigits, QLineEdit#plateDigits:hover, QLineEdit#plateDigits:focus,
QLineEdit#plateRegion, QLineEdit#plateRegion:hover, QLineEdit#plateRegion:focus,
QComboBox#plateLetter, QComboBox#plateLetter:hover, QComboBox#plateLetter:focus {
 background:@canvas; color:@text; border:0; border-radius:0; min-height:76px; max-height:76px; padding:0; font-size:27px; font-weight:700;
}
QComboBox#plateLetter::drop-down { width:0; border:0; }
QComboBox#plateLetter::down-arrow { image:none; }
QFrame#plateRegionBox { background:@canvas; border:0; border-left:2px solid #466791; }
QLabel#plateIran { color:@muted; font-size:10px; padding-top:3px; }
QLabel#sectionTitle { font-size:18px; font-weight:700; color:@text; }
QLabel#mutedText { color:@muted; }
QLabel#notice { background:@raised; color:@muted; padding:12px; border-radius:8px; }
QLabel#notice[tone="success"] { background:#16372F; color:@success; }
QLabel#notice[tone="warning"] { background:#3A3025; color:@warning; }
QLabel#notice[tone="error"] { background:#382333; color:@error; }
QLabel#liveWeight { background:@raised; border:1px solid @line; color:#62B8FF; font-size:38px; font-weight:800; padding:18px; border-radius:12px; }
QPushButton[locked="true"] { background:#16372F; color:@success; border-color:#2D6353; }
QListWidget, QListView { background:@panel; color:@text; border:1px solid @line; border-radius:8px; outline:0; }
QListView::item { padding:8px; min-height:30px; }
QListView::item:hover { background:@raised; }
QListView::item:selected { background:#183763; color:@text; }
QCalendarWidget QWidget { background:@panel; color:@text; }
QCalendarWidget QAbstractItemView { background:@panel; color:@text; selection-background-color:#245BDB; selection-color:white; }
QCalendarWidget QToolButton { background:@raised; color:@text; }
QProgressBar { background:@canvas; border:1px solid @line; border-radius:6px; text-align:center; }
QProgressBar::chunk { background:@brand; border-radius:5px; }
"""

POPUP_STYLE = """
QFrame, QListView, QListView::viewport { background:#0D1B30; color:#F3F7FF; border:0; }
QListView { border:1px solid #263B56; border-radius:8px; padding:6px; outline:0; }
QListView::item { min-height:34px; padding:8px 12px; border-radius:6px; }
QListView::item:hover { background:#132842; }
QListView::item:selected { background:#183763; color:#F3F7FF; }
"""
SCROLLBAR_STYLE = """
QScrollBar:vertical { background:#0D1B30; width:9px; margin:0; }
QScrollBar::handle:vertical { background:#354C69; min-height:30px; border-radius:4px; }
QScrollBar:horizontal { background:#0D1B30; height:9px; margin:0; }
QScrollBar::handle:horizontal { background:#354C69; min-width:30px; border-radius:4px; }
QScrollBar::add-line, QScrollBar::sub-line { width:0; height:0; }
QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }
"""


def app_style(assets=None):
    from pathlib import Path
    icons = Path(assets) if assets else Path(__file__).resolve().parent / "assets" / "icons"
    return (MAGICAL_BLUE_STYLE
            .replace("__CHECK_ICON__", (icons / "check.svg").as_posix()) +
            'QComboBox::down-arrow { image:url("' + (icons / "chevron-down.svg").as_posix() + '"); width:12px; height:12px; }' + """
            QPushButton#primary, QPushButton#cardActionPrimary,
            QPushButton#primary:hover, QPushButton#cardActionPrimary:hover,
            QDialogButtonBox QPushButton#primary, QLabel#plateCountryText,
            QMenu::item:selected { color:rgb(255,255,255); }
            QPushButton#primary:disabled, QDialogButtonBox QPushButton#primary:disabled {
                color:#71839C; background:#101F33; border-color:#203249;
            }
            QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QTimeEdit, QComboBox {
                selection-background-color:#245BDB; selection-color:rgb(255,255,255);
            }
            QAbstractItemView { outline:0; }
            QCalendarWidget QAbstractItemView { selection-color:rgb(255,255,255); }
            QRadioButton { spacing:8px; }
            QRadioButton::indicator { width:16px; height:16px; border:1px solid #526987; border-radius:9px; background:#081426; }
            QRadioButton::indicator:checked { background:#245BDB; border:4px solid #315B94; }
            QSlider::groove:horizontal { height:6px; background:#263B56; border-radius:3px; }
            QSlider::sub-page:horizontal { background:#3987FF; border-radius:3px; }
            QSlider::handle:horizontal { width:20px; margin:-7px 0; background:#62B8FF; border-radius:10px; }
            QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
                color:#71839C; background:#101F33; border-color:#203249;
            }
            """)


def set_tone(widget, tone):
    widget.setProperty("tone", tone)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()

for _name, _value in TOKENS.items():
    MAGICAL_BLUE_STYLE = MAGICAL_BLUE_STYLE.replace("@" + _name, _value)
