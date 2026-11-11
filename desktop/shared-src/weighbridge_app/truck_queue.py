"""Server-authoritative truck admission and a separate public display."""
from __future__ import annotations
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from .dates import gregorian_to_jalali
from .hall_plate import plate_parts, draw_plate_in_rect, draw_bumper_plate
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QRectF, QPointF, QSize, QEvent
from PySide6.QtGui import QColor, QPainter, QFont, QFontMetricsF, QLinearGradient, QRadialGradient, QPen, QPixmap, QPainterPath, QIcon
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QFrame, QScrollArea, QSizePolicy)
from .services import auth_headers, safe_response_error
from .design import app_style
from .theme import set_widget_style, current_theme


def digits(value):
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def iran_clock(now=None):
    # Iran currently observes UTC+03:30 year-round; no Windows timezone database
    # or extra tzdata package is required for the live display.
    instant=datetime.now(timezone.utc) if now is None else now
    return digits(instant.astimezone(timezone(timedelta(hours=3,minutes=30))).strftime("%H:%M:%S"))


def queue_day(value):
    try:
        date=datetime.strptime(str(value), "%Y-%m-%d")
        year,month,day=gregorian_to_jalali(date.year,date.month,date.day)
        return digits(f"{year}/{month:02}/{day:02}")
    except (ValueError, TypeError):
        return str(value or "")


def partition(snapshot):
    rows=sorted(snapshot.get("entries", []), key=lambda row:(row["day"],int(row["number"])))
    return [r for r in rows if r["state"]=="waiting"], [r for r in rows if r["state"]=="inside"]


def ready_entries(snapshot):
    return sorted((r for r in snapshot.get("entries",[]) if r["state"]=="ready"),key=lambda r:(r["day"],int(r["number"])))


def queue_instant(value):
    """Odoo datetimes without an offset are UTC; API dates also accept Z."""
    try:
        instant=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)
    except (TypeError,ValueError):return None


def queue_duration(row,age=0):
    inside=row.get('state')=='inside'
    value=row.get('inside_elapsed_seconds' if inside else 'wait_estimate_seconds')
    if inside and value is None:
        start=queue_instant(row.get('entered_at') or row.get('entry_at'))
        end=queue_instant(row.get('server_time'))
        if start and end:value=max(0,(end-start).total_seconds())
    if value is None:return 'زمان ورود ثبت نشده' if inside else 'در حال برآورد'
    seconds=max(0,int(value)+(int(age) if inside else -int(age)))
    if inside:
        hours,remainder=divmod(seconds,3600); minutes,secs=divmod(remainder,60)
        return 'حضور '+digits(f'{hours:02}:{minutes:02}:{secs:02}')
    return 'حدود '+digits(max(1,math.ceil(seconds/60)))+' دقیقه انتظار'


class QueueRequest(QThread):
    result=Signal(dict, str)
    def __init__(self, session, url, token, entry=None, action=None, parent=None):
        super().__init__(parent); self.http=session; self.url=url; self.token=token; self.entry=entry; self.action=action
    def run(self):
        try:
            kwargs={"headers":auth_headers(self.token),"timeout":(6,15)}
            kwargs["headers"].update({"Connection":"keep-alive","Cache-Control":"no-cache"})
            response=(self.http.get(self.url,**kwargs) if self.entry is None else
                      self.http.post(f"{self.url}/{self.entry}",json={"action":self.action},**kwargs))
            if not response.ok:raise RuntimeError(safe_response_error(response))
            payload=response.json()
            if isinstance(payload,dict) and (payload.get('success') is False or payload.get('error')):
                raise RuntimeError(str(payload.get('message') or 'عملیات توسط سرور تأیید نشد؛ وضعیت بارنامه را در اودو بررسی کنید.'))
            data=payload.get("data")
            # Odoo acknowledges a gate action with the updated waybill, whereas
            # the shared queue endpoint returns a complete snapshot.
            if self.entry is not None and isinstance(data,dict) and data.get('id')==self.entry and 'gate_state' in data:
                try:
                    snapshot=self.http.get(self.url,**kwargs)
                    if not snapshot.ok:raise RuntimeError(safe_response_error(snapshot))
                    data=snapshot.json().get('data')
                    if not isinstance(data,dict) or not isinstance(data.get('entries'),list):raise ValueError()
                except Exception:
                    raise RuntimeError('عملیات در اودو ثبت شد، اما دریافت وضعیت جدید صف انجام نشد. دکمه همگام‌سازی را بزنید؛ عملیات را تکرار نکنید.') from None
            if not isinstance(data,dict) or not isinstance(data.get("entries"),list):
                raise RuntimeError("پاسخ صف معتبر نیست؛ نسخه سرور را بررسی کنید.")
            self.result.emit(data, "")
        except requests.Timeout:
            self.result.emit({},'پاسخ سرور در زمان مقرر دریافت نشد. نتیجه عملیات مشخص نیست؛ ابتدا صف را همگام‌سازی کنید و وضعیت خودرو را بررسی کنید.')
        except Exception as exc:
            error=str(exc) if isinstance(exc,RuntimeError) else "ارتباط با سرور صف برقرار نشد."
            self.result.emit({},error)


class QueueDisplay(QWidget):
    """Independent branded hall signage, with no user details or app controls."""
    def __init__(self):
        # An owned top-level window follows its owner between Windows virtual
        # desktops. Keep this window unowned; QueuePage retains the Python ref.
        super().__init__(None, Qt.Window)
        self.setWindowTitle("نمایش نوبت‌ها"); self.resize(1000,650); self.setMinimumSize(480,320)
        self.snapshot={"entries":[]}; self.updated=0; self.healthy=False; self.page=0
        assets=Path(__file__).resolve().parent/"assets"
        self._before_fullscreen_maximized=False
        self.fullscreen_button=QPushButton("تمام‌صفحه",self)
        self.fullscreen_button.setIcon(QIcon(str(assets/'icons'/'fullscreen.svg'))); self.fullscreen_button.setIconSize(QSize(19,19))
        self.fullscreen_button.setFixedSize(140,36); self.fullscreen_button.setFocusPolicy(Qt.NoFocus); self.fullscreen_button.setCursor(Qt.PointingHandCursor)
        self.fullscreen_button.setToolTip("نمایش تمام‌صفحه؛ بازگشت فقط با Esc")
        set_widget_style(self.fullscreen_button,"QPushButton { background:#132842; color:#F3F7FF; border:1px solid #304763; border-radius:9px; padding:5px 12px; } QPushButton:hover { background:#173052; border-color:#3987FF; }")
        self.fullscreen_button.clicked.connect(self.enter_fullscreen)
        self.logo=QPixmap(str(assets/"brand"/"megasanj-icon.png"))
        self.banners={mode:QPixmap(str(assets/"display"/f"hall-banner-{mode}.png")) for mode in ("dark","light")}
        self.plate_key=None; self.live_plate=""
        self.setAttribute(Qt.WA_DeleteOnClose,False)
        self.timer=QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(1000)
        self.elapsed=0
    def tick(self):
        self.elapsed+=1
        if self.elapsed%7==0:self.page+=1
        self.update()
    def set_snapshot(self,data):
        for row in data.get('entries',[]):row['server_time']=data.get('server_time')
        def tickets(snapshot):
            return [(r.get('id'),r.get('day'),r.get('number'),r.get('state')) for r in snapshot.get('entries',[])]
        if tickets(data)!=tickets(self.snapshot):self.page=0
        self.snapshot=data; self.updated=time.monotonic(); self.healthy=True; self.update()
    def keyPressEvent(self,event):
        if event.key()==Qt.Key_F11:
            if not self.isFullScreen():self.enter_fullscreen()
            event.accept()
        elif event.key()==Qt.Key_Escape and self.isFullScreen():
            self.showMaximized() if self._before_fullscreen_maximized else self.showNormal()
            event.accept()
        else:super().keyPressEvent(event)
    def enter_fullscreen(self):
        if self.isFullScreen():return
        self._before_fullscreen_maximized=self.isMaximized()
        self.fullscreen_button.hide(); self.showFullScreen(); self.activateWindow(); self.setFocus()
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'fullscreen_button'):
            self.fullscreen_button.move((self.width()-self.fullscreen_button.width())//2,max(3,int((self.height()*.135-self.fullscreen_button.height())/2)))
    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.WindowStateChange and hasattr(self,'fullscreen_button'):
            self.fullscreen_button.setVisible(not self.isFullScreen())
    def paintEvent(self,event):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing); p.setRenderHint(QPainter.SmoothPixmapTransform)
        w,h=self.width(),self.height(); light=current_theme()=="light"
        ink="#17365C" if light else "#F3F7FF"
        muted="#607695" if light else "#91A7C5"
        mint="#087E69" if light else "#65E8C6"
        blue="#245D9F" if light else "#9CCAFF"
        line="#C8D7EC" if light else "#233D5C"
        pad=w*.045; radius=h*.025
        background=QLinearGradient(0,0,w,h)
        background.setColorAt(0,QColor("#F3F7FD" if light else "#071221"))
        background.setColorAt(1,QColor("#DFEAF8" if light else "#0D2035"))
        p.fillRect(self.rect(),background)
        glow=QRadialGradient(QPointF(w*.75,h*.55),w*.65)
        glow.setColorAt(0,QColor(50,125,170,24 if light else 30)); glow.setColorAt(1,QColor(50,125,170,0))
        p.fillRect(self.rect(),glow)
        def text(rect,value,size,color=ink,weight=QFont.Medium,alignment=Qt.AlignCenter):
            f=QFont(self.font()); f.setPixelSize(max(9,int(size))); f.setWeight(weight)
            value=str(value); bounds=QFontMetricsF(f).tightBoundingRect(value)
            scale=min(1,rect.width()*.96/max(1,bounds.width()),rect.height()*.94/max(1,bounds.height()))
            if scale<1:
                f.setPixelSize(max(7,int(f.pixelSize()*scale))); bounds=QFontMetricsF(f).tightBoundingRect(value)
            x=rect.center().x()-bounds.center().x()
            if alignment&Qt.AlignRight:x=rect.right()-bounds.right()
            elif alignment&Qt.AlignLeft:x=rect.left()-bounds.left()
            p.save(); p.setClipRect(rect); p.setFont(f); p.setPen(QColor(color))
            p.drawText(QPointF(x,rect.center().y()-bounds.center().y()),value); p.restore()
        def panel(rect,top,bottom,border,corner=None):
            if corner is None:corner=radius
            shade=QLinearGradient(rect.topLeft(),rect.bottomRight())
            shade.setColorAt(0,QColor(top)); shade.setColorAt(1,QColor(bottom))
            p.setPen(QPen(QColor(border),1)); p.setBrush(shade); p.drawRoundedRect(rect,corner,corner)
        def timing_card(rect,row,tone,inside=False):
            age=time.monotonic()-self.updated
            value=queue_duration(row,age)
            title="مدت حضور در کارخانه" if inside else "زمان تقریبی انتظار"
            value=value.removeprefix('حضور ') if inside else value.removeprefix('حدود ').removesuffix(' انتظار')
            if not inside and row.get('wait_estimate_seconds') is not None:
                minutes=max(1,math.ceil(max(0,float(row['wait_estimate_seconds'])-age)/60))
                hours,minutes=divmod(minutes,60)
                value='\u200f'+' و '.join(part for part in ((digits(hours)+' ساعت') if hours else '',(digits(minutes)+' دقیقه') if minutes else '') if part)
            x,y,cw,ch=rect.x(),rect.y(),rect.width(),rect.height()
            horizontal=cw>ch*2.4
            if horizontal:
                number=QRectF(x+cw*.67,y+ch*.16,cw*.29,ch*.66)
                caption=QRectF(x+cw*.045,y+ch*.22,cw*.55,ch*.17)
                amount=QRectF(x+cw*.045,y+ch*.47,cw*.55,ch*.29)
                p.setPen(QPen(QColor(line),1)); p.drawLine(QPointF(x+cw*.635,y+ch*.18),QPointF(x+cw*.635,y+ch*.82))
            else:
                number=QRectF(x+cw*.07,y+ch*.065,cw*.86,ch*.38)
                caption=QRectF(x+cw*.05,y+ch*.51,cw*.90,ch*.14)
                amount=QRectF(x+cw*.05,y+ch*.70,cw*.90,ch*.20)
                p.setPen(QPen(QColor(line),.8)); p.drawLine(QPointF(x+cw*.15,y+ch*.47),QPointF(x+cw*.85,y+ch*.47))
            text(number,digits(row['number']),min(ch*(.54 if horizontal else .36),cw*.36),tone,QFont.DemiBold)
            text(caption,title+" :",min(ch*.115,h*.030),muted)
            text(amount,value,min(ch*.20,h*.052),tone,QFont.DemiBold)
            if row['day']!=self.snapshot.get('day'):
                text(QRectF(x+cw*.05,y+ch*.92,cw*.9,ch*.065),queue_day(row['day']),min(h*.018,ch*.06),muted)
        # The header stays in screen coordinates; the board below has its own
        # content coordinates so neither clock nor branding steals ticket space.
        outer=w*.03; header_bottom=h*.135
        logo_rect=QRectF(w-outer-h*.105,h*.018,h*.105,h*.105)
        if not self.logo.isNull():p.drawPixmap(logo_rect,self.logo,QRectF(self.logo.rect()))
        text(QRectF(logo_rect.left()-w*.187,h*.031,w*.18,h*.048),"مگاسنج",h*.032,ink,QFont.DemiBold,Qt.AlignRight|Qt.AlignVCenter)
        text(QRectF(logo_rect.left()-w*.187,h*.083,w*.18,h*.028),"مدیریت هوشمند باسکول",h*.017,muted,QFont.Medium,Qt.AlignRight|Qt.AlignVCenter)
        clock_rect=QRectF(outer,h*.026,w*.27,h*.054)
        text(clock_rect,iran_clock(),h*.044,ink,QFont.DemiBold,Qt.AlignLeft|Qt.AlignVCenter)
        text(QRectF(clock_rect.left(),h*.087,clock_rect.width(),h*.026),"به وقت ایران",h*.018,muted,QFont.Medium,Qt.AlignLeft|Qt.AlignVCenter)
        p.setPen(QPen(QColor(line),.8)); p.drawLine(QPointF(outer,header_bottom),QPointF(w-outer,header_bottom))

        body_y=h*.168; body_h=h*.80; gutter=w*.02
        usable=w-outer*2-gutter; banner_w=usable*.25
        banner_rect=QRectF(outer,body_y,banner_w,body_h)
        banner=self.banners["light" if light else "dark"]
        fresh=self.healthy and time.monotonic()-self.updated<=15
        ready=ready_entries(self.snapshot) if fresh else []
        plate_value=ready[0].get("trailer_plate","") if ready else ""
        plate_key=(plate_value,light)
        if plate_key!=self.plate_key:
            self.live_plate=plate_value if plate_parts(plate_value) else ""; self.plate_key=plate_key
        clip=QPainterPath(); clip.addRoundedRect(banner_rect,radius,radius)
        p.save(); p.setClipPath(clip)
        if not banner.isNull():
            scale=max(banner_rect.width()/banner.width(),banner_rect.height()/banner.height())
            sw,sh=banner_rect.width()/scale,banner_rect.height()/scale
            source=QRectF((banner.width()-sw)/2,(banner.height()-sh)/2,sw,sh)
            p.drawPixmap(banner_rect,banner,source)
            draw_bumper_plate(p,self.live_plate,source,banner_rect,"light" if light else "dark")
        else:p.fillRect(banner_rect,QColor("#E4EDF8" if light else "#102740"))
        overlay=QLinearGradient(banner_rect.topLeft(),banner_rect.bottomLeft())
        overlay.setColorAt(0,QColor(244,248,255,145) if light else QColor(5,18,36,140))
        overlay.setColorAt(.42,QColor(244,248,255,0) if light else QColor(5,18,36,0))
        overlay.setColorAt(1,QColor(235,243,253,65) if light else QColor(5,18,36,55)); p.fillRect(banner_rect,overlay); p.restore()
        p.setBrush(Qt.NoBrush); p.setPen(QPen(QColor(line),1)); p.drawRoundedRect(banner_rect,radius,radius)
        inset=banner_w*.10
        text(QRectF(outer+inset,body_y+body_h*.065,banner_w-2*inset,body_h*.035),"همراه مسیر شما",h*.018,muted)
        text(QRectF(outer+inset,body_y+body_h*.125,banner_w-2*inset,body_h*.055),"ورود منظم",h*.034,ink,QFont.DemiBold)
        text(QRectF(outer+inset,body_y+body_h*.196,banner_w-2*inset,body_h*.055),"توزین دقیق",h*.034,ink,QFont.DemiBold)
        p.setPen(QPen(QColor(blue),max(1.5,h*.003),Qt.SolidLine,Qt.RoundCap)); p.drawLine(QPointF(outer+banner_w*.40,body_y+body_h*.30),QPointF(outer+banner_w*.60,body_y+body_h*.30))
        # Exactly three quarters of the usable body width belongs to the queue.
        board_x=outer+banner_w+gutter
        p.translate(board_x,body_y); w=usable*.75; h=body_h
        pad=w*.012; radius=h*.025
        waiting,inside=partition(self.snapshot)
        stale=not self.healthy or time.monotonic()-self.updated>15
        if stale:
            panel(QRectF(pad,h*.26,w-2*pad,h*.48),"#FFFFFF" if light else "#11283F","#EBF2FC" if light else "#0C1B2F",line)
            text(QRectF(pad,h*.36,w-2*pad,h*.12),"در حال دریافت نوبت‌ها" if not self.updated else "اطلاعات نوبت‌ها در دسترس نیست",h*.045)
            text(QRectF(pad,h*.52,w-2*pad,h*.08),"لطفاً به مسئول باسکول مراجعه کنید",h*.027,muted)
            p.end(); return
        # Full-height call column on the right: one third of the queue area.
        gap=w*.025; active_w=(w-2*pad-gap)/3; rest_w=active_w*2
        active_x=pad+rest_w+gap
        amber="#8D7445" if light else "#FFCF86"
        stage=QRectF(active_x,h*.035,active_w,h*.895)
        panel(stage,"#F8F5EB" if light and ready else "#FFFFFF" if light else "#2A302E" if ready else "#122E43",
              "#EEE8D8" if light and ready else "#EDF7F5" if light else "#162B35","#D9CCAA" if light and ready else "#796548" if ready else line,h*.03)
        if ready:
            called=ready[0]; margin=active_w*.08; inner=active_w-2*margin
            text(QRectF(active_x+margin,h*.105,inner,h*.05),"آماده ورود",h*.036,amber,QFont.DemiBold)
            text(QRectF(active_x+margin,h*.178,inner,h*.035),"نوبت فعال",h*.023,muted)
            text(QRectF(active_x+margin,h*.265,inner,h*.24),digits(called["number"]),min(h*.23,active_w*.65),ink,QFont.DemiBold)
            text(QRectF(active_x+margin,h*.54,inner,h*.045),"نوبت شماست",h*.034,amber,QFont.DemiBold)
            text(QRectF(active_x+margin,h*.615,inner,h*.035),"لطفاً برای ورود مراجعه کنید",h*.023,ink)
            if self.live_plate:
                plate_w=inner*.94; plate_h=plate_w*220/1040
                draw_plate_in_rect(p,self.live_plate,QRectF(active_x+(active_w-plate_w)/2,h*.72,plate_w,plate_h),dark=not light)
            text(QRectF(active_x+margin,h*.855,inner,h*.028),queue_day(called["day"]),h*.021,amber)
        else:
            text(QRectF(active_x+active_w*.08,h*.22,active_w*.84,h*.06),"نوبت فعال",h*.033,muted)
            text(QRectF(active_x+active_w*.08,h*.41,active_w*.84,h*.07),"در انتظار فراخوان",h*.032,ink)
            text(QRectF(active_x+active_w*.08,h*.59,active_w*.84,h*.045),"لطفاً منتظر بمانید",h*.023,muted)

        # The remaining two thirds contain the upcoming and inside sections.
        text(QRectF(pad,h*.04,rest_w,h*.05),"نوبت‌های بعدی",h*.030,muted)
        upcoming=waiting[:5]; cols=min(3,max(1,len(upcoming))); rows=max(1,math.ceil(len(upcoming)/cols))
        cell_gap=w*.012; cw=(rest_w-(cols-1)*cell_gap)/cols; ch=(h*.30-(rows-1)*h*.018)/rows
        for i,row in enumerate(upcoming):
            x=pad+rest_w-cw-(i%cols)*(cw+cell_gap); y=h*.12+(i//cols)*(ch+h*.018); rect=QRectF(x,y,cw,ch)
            panel(rect,"#E4EFFF" if light and i==0 else "#FFFFFF" if light else "#18375A" if i==0 else "#0F2238",
                  "#EDF4FF" if light else "#102740" if i==0 else "#0C1D31","#8EB5E9" if light and i==0 else "#4578AC" if i==0 else line,h*.018)
            timing_card(rect,row,blue if i==0 else ink)
        if not upcoming:
            panel(QRectF(pad,h*.12,rest_w,h*.30),"#FFFFFF" if light else "#0F2238","#EDF4FF" if light else "#0C1D31",line)
            text(QRectF(pad,h*.19,rest_w,h*.10),"نوبتی در انتظار ورود نیست",h*.029,muted)
        # The trucks already admitted remain visible, but never look like a call.
        inside_panel=QRectF(pad,h*.46,rest_w,h*.47)
        panel(inside_panel,"#FFFFFF" if light else "#0F2238","#F4F7FC" if light else "#0C1D31",line,h*.02)
        text(QRectF(pad+w*.02,h*.485,rest_w-w*.04,h*.04),"وارد شده‌ها · داخل کارخانه",h*.027,mint,QFont.Medium,Qt.AlignRight|Qt.AlignVCenter)
        pages=max(1,math.ceil(len(inside)/6)); page=self.page%pages; batch=inside[page*6:page*6+6]
        cols=min(3,max(1,len(batch))); rows=max(1,math.ceil(len(batch)/cols)); cell_w=rest_w/cols; cell_h=h*.32/rows
        for i,row in enumerate(batch):
            x=pad+rest_w-(i%cols+1)*cell_w; y=h*.56+(i//cols)*cell_h
            card=QRectF(x+w*.009,y+cell_h*.05,cell_w-w*.018,cell_h*.88)
            panel(card,"#F4FBF8" if light else "#153540","#E7F3EE" if light else "#102B38","#A8D7C9" if light else "#2A5D64",h*.018)
            timing_card(card,row,mint,inside=True)
        if not inside:text(QRectF(pad,h*.64,rest_w,h*.09),"فعلاً تریلی‌ای داخل کارخانه نیست",h*.026,muted)
        if pages>1:text(QRectF(pad,h*.89,rest_w,h*.024),digits(page+1)+" / "+digits(pages),h*.018,muted)
        text(QRectF(pad,h*.95,w-2*pad,h*.035),"فقط شماره آماده ورود برای ورود به کارخانه مراجعه کند",h*.023,muted)
        p.end()
        return


class QueuePage(QWidget):
    entries_changed=Signal(dict)
    def __init__(self,db,parent=None):
        super().__init__(parent); self.db=db; self.worker=None; self.snapshot={"entries":[]}; self.last_success=0; self.public=None; self.stopping=False
        self.failures=0; self.connected=False; self.next_retry=0; self.pending_action=None
        self.http=requests.Session()
        retry=Retry(total=2,connect=2,read=1,status=2,backoff_factor=.4,status_forcelist=(502,503,504),allowed_methods=frozenset(("GET",)))
        self.http.mount("https://",HTTPAdapter(max_retries=retry,pool_connections=1,pool_maxsize=1))
        self.http.mount("http://",HTTPAdapter(max_retries=retry,pool_connections=1,pool_maxsize=1))
        self.setLayoutDirection(Qt.RightToLeft)
        style=app_style()+"""
        QFrame#queueSection { background:#0D1B30; border:1px solid #263B56; border-radius:14px; }
        QLabel#queueSectionTitle { color:#F3F7FF; font-size:16px; font-weight:800; }
        QLabel#queueSectionCount { background:#17345D; color:#62B8FF; border-radius:10px; padding:4px 10px; font-weight:800; }
        QFrame#queueRow { background:#112139; border:1px solid #263B56; border-radius:12px; }
        QFrame#queueRow:hover { border-color:#466791; background:#132842; }
        QLabel#queueTicket, QLabel#queueActiveTicket { background:#17345D; color:#A9D4FF; border:1px solid #315B94; border-radius:10px; font-size:21px; font-weight:800; padding:5px; }
        QLabel#queueActiveTicket { background:#16372F; color:#32D6A0; border-color:#2D6353; }
        QLabel#queueTicket[ready="true"] { background:#3A3025; color:#FFB454; border-color:#665137; }
        QFrame#queueReady { background:#0D1B30; border:1px solid #665137; border-radius:12px; }
        QLabel#queueDetail { color:#F3F7FF; font-size:12px; font-weight:600; }
        QLabel#queueMuted { color:#9CAFC8; font-size:11px; }
        QLabel#queueEmpty { color:#9CAFC8; padding:22px; font-size:12px; }
        QPushButton#queueEnter { background:#245BDB; border:1px solid #3987FF; color:white; font-weight:700; min-height:37px; padding:0 12px; }
        QPushButton#queueExit { background:#17345D; border:1px solid #315B94; color:#A9D4FF; font-weight:700; min-height:37px; padding:0 12px; }
        QPushButton#queueEnter:disabled, QPushButton#queueExit:disabled { background:#101F33; color:#71839C; border-color:#203249; }
        """
        set_widget_style(self,style)
        root=QVBoxLayout(self); root.setContentsMargins(9,9,9,9); root.setSpacing(14)
        hero=QFrame(objectName="documentsHero"); hero_l=QHBoxLayout(hero); hero_l.setContentsMargins(24,20,24,20); hero_l.setSpacing(18)
        intro=QVBoxLayout(); intro.setSpacing(4); title=QLabel("نوبت‌دهی تریلی‌ها",objectName="heroTitle"); subtitle=QLabel("مدیریت صف انتظار، اجازه ورود و خروج تریلی‌ها",objectName="heroSubtitle"); intro.addWidget(title); intro.addWidget(subtitle); hero_l.addLayout(intro,1)
        self.status=QLabel(objectName="notice"); self.status.setWordWrap(True); self.status.hide()
        self.display_button=QPushButton("نمایشگر سالن  ↗"); self.display_button.clicked.connect(self.open_display); hero_l.addWidget(self.display_button)
        self.refresh_button=QPushButton("همگام‌سازی"); self.refresh_button.clicked.connect(lambda:self.refresh()); hero_l.addWidget(self.refresh_button); root.addWidget(hero)
        metric_row=QHBoxLayout(); metric_row.setSpacing(10); self.metric_values={}
        for key,label in (("waiting","در انتظار"),("ready","آماده ورود"),("inside","وارد شده")):
            card=QFrame(objectName="metricCard"); card.setAttribute(Qt.WA_StyledBackground,True); box=QVBoxLayout(card); box.setContentsMargins(20,14,20,14); box.setSpacing(4)
            value=QLabel("۰",objectName="metricValue"); value.setProperty("tone","success" if key=="inside" else "warning" if key=="ready" else "brand"); caption=QLabel(label,objectName="metricTitle"); box.addWidget(caption); box.addWidget(value); metric_row.addWidget(card,1); self.metric_values[key]=value
        root.addLayout(metric_row); root.addWidget(self.status)
        self.ready_panel=QFrame(objectName="queueReady"); self.ready_layout=QVBoxLayout(self.ready_panel); self.ready_layout.setContentsMargins(12,10,12,10); self.ready_layout.addWidget(QLabel("آماده ورود · ابتدا ورود این تریلی را ثبت کنید",objectName="queueSectionTitle")); root.addWidget(self.ready_panel); self.ready_panel.hide()
        sections=QHBoxLayout(); sections.setSpacing(12); self.section_lists={}
        self.section_counts={}
        for key,title in (("inside","وارد شده‌ها · داخل کارخانه"),("waiting","نوبت‌های منتظر ورود")):
            section=QFrame(objectName="queueSection"); section.setAttribute(Qt.WA_StyledBackground,True); body=QVBoxLayout(section); body.setContentsMargins(13,12,13,12); body.setSpacing(10)
            section_head=QHBoxLayout(); heading=QLabel(title,objectName="queueSectionTitle"); count=QLabel("۰",objectName="queueSectionCount"); count.setAlignment(Qt.AlignCenter); section_head.addWidget(heading,1); section_head.addWidget(count); body.addLayout(section_head); self.section_counts[key]=count
            scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            content=QWidget(); content.setAttribute(Qt.WA_StyledBackground,True); content_layout=QVBoxLayout(content); content_layout.setContentsMargins(2,2,5,2); content_layout.setSpacing(8); content_layout.addStretch(1); scroll.setWidget(content); body.addWidget(scroll,1)
            self.section_lists[key]=(content,content_layout); sections.addWidget(section,1)
        root.addLayout(sections,1)
        self.timer=QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(500)
    def showEvent(self,event):
        super().showEvent(event); self.refresh()
    def refresh(self,entry=None,action=None):
        if self.stopping:return
        if not self.db.allows(getattr(self.db,'queue_capability','weighbridge')):
            if entry is not None:QMessageBox.warning(self,'عملیات صف انجام نشد','حساب شما دسترسی عملیات صف را ندارد. دسترسی انتظامات را از مدیر سیستم درخواست کنید.')
            return
        if not self.db.session:
            if entry is not None:QMessageBox.warning(self,'عملیات صف انجام نشد','اتصال به سرور فعال نیست. ابتدا وارد حساب شوید و صف را همگام‌سازی کنید.')
            return
        if entry is not None:
            for button in self.findChildren(QPushButton):
                if button.objectName() in ("queueEnter","queueExit"):button.setEnabled(False)
        if self.worker is not None:
            if entry is not None:self.pending_action=(entry,action)
            return
        if entry is None and not self.isVisible() and not (self.public and self.public.isVisible()):return
        self.refresh_button.setEnabled(False)
        self.worker=QueueRequest(self.http,self.db.session.api_url('transportation/truck-queue'),self.db.session.token,entry,action,self)
        self.worker.result.connect(self.received); self.worker.finished.connect(self.request_closed); self.worker.start()
    def poll(self):
        if self.stopping:return
        if self.worker is None and (self.isVisible() or (self.public and self.public.isVisible())) and time.monotonic()>=self.next_retry:
            self.refresh()
    def received(self,data,error):
        if self.stopping:return
        action=getattr(self.worker,'action',None)
        if error:
            self.connected=False; self.failures+=1; delay=min(30,2**min(self.failures,5)); self.next_retry=time.monotonic()+delay
            self.status.setText(error+f" · دریافت مجدد صف تا {digits(delay)} ثانیه دیگر")
            self.status.show()
            self.refresh_button.setEnabled(True); self.render_snapshot(allow_actions=False)
            if self.public:self.public.healthy=False; self.public.update()
            if action:
                label={'call':'اجازه ورود','enter':'ثبت ورود','exit':'ثبت خروج'}.get(action,'عملیات صف')
                QMessageBox.warning(self,label+' تأیید نشد',error)
            return
        self.failures=0; self.connected=True; self.next_retry=time.monotonic()+3
        previous=[(r.get('id'),r.get('state')) for r in self.snapshot.get('entries',[])]
        current=[(r.get('id'),r.get('state')) for r in data.get('entries',[])]
        # Derive elapsed time using the server clock, never a possibly skewed PC
        # clock. Retains compatibility with servers that only send timestamps.
        for row in data.get('entries',[]):
            row['server_time']=data.get('server_time')
        self.snapshot=data
        if previous!=current:self.entries_changed.emit(data)
        self.last_success=time.monotonic(); self.status.hide()
        self.render_snapshot(allow_actions=True)
        if self.public:self.public.set_snapshot(data)
    def render_snapshot(self,allow_actions):
        waiting,inside=partition(self.snapshot)
        ready=ready_entries(self.snapshot)
        self.metric_values["waiting"].setText(digits(len(waiting))); self.metric_values["inside"].setText(digits(len(inside))); self.metric_values["ready"].setText(digits(len(ready)))
        while self.ready_layout.count()>1:
            widget=self.ready_layout.takeAt(1).widget()
            if widget:widget.deleteLater()
        for row in ready:self.ready_layout.addWidget(self.queue_card(row,allow_actions))
        self.ready_panel.setVisible(bool(ready))
        for key,rows in (("inside",inside),("waiting",waiting)):
            self.section_counts[key].setText(digits(len(rows)))
            content,layout=self.section_lists[key]
            while layout.count()>1:
                item=layout.takeAt(0); widget=item.widget()
                if widget:widget.deleteLater()
            if not rows:
                empty=QLabel("فعلاً خودرویی در این بخش نیست",objectName="queueEmpty"); empty.setAlignment(Qt.AlignCenter); layout.insertWidget(0,empty); continue
            for row in rows:layout.insertWidget(layout.count()-1,self.queue_card(row,allow_actions))
    def queue_card(self,row,allow_actions):
        inside=row["state"]=="inside"; card=QFrame(objectName="queueRow"); card.setAttribute(Qt.WA_StyledBackground,True); box=QHBoxLayout(card); box.setContentsMargins(10,9,10,9); box.setSpacing(11)
        ticket=QLabel(digits(row["number"]),objectName="queueActiveTicket" if inside else "queueTicket"); ticket.setProperty("ready",row["state"]=="ready"); ticket.setAlignment(Qt.AlignCenter); ticket.setMinimumWidth(60); ticket.setMinimumHeight(54); box.addWidget(ticket)
        details=QVBoxLayout(); details.setSpacing(4); name=QLabel(row.get("driver_name") or "راننده ثبت‌نشده",objectName="queueDetail"); plate=QLabel("پلاک  "+(row.get("trailer_plate") or "ثبت‌نشده"),objectName="queueMuted"); bill=QLabel("بارنامه  "+digits(row.get("waybill_number") or "—")+"   ·   "+queue_day(row.get("day")),objectName="queueMuted")
        for label in (name,plate,bill):label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        details.addWidget(name); details.addWidget(plate); details.addWidget(bill)
        if row['state'] in ('waiting','inside'):details.addWidget(QLabel(queue_duration(row),objectName='queueMuted'))
        box.addLayout(details,1)
        action={"waiting":"call","ready":"enter","inside":"exit"}[row["state"]]
        button=QPushButton({"call":"اجازه ورود","enter":"ثبت ورود","exit":"ثبت خروج"}[action],objectName="queueExit" if inside else "queueEnter")
        blocked=action=="call" and bool(ready_entries(self.snapshot))
        button.setEnabled(allow_actions and not blocked)
        if blocked:button.setToolTip("ابتدا ورود تریلی آماده ورود را ثبت کنید.")
        button.clicked.connect(lambda checked=False,r=dict(row),a=action:self.change(r,a)); box.addWidget(button)
        card.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed); return card
    def request_closed(self):
        worker=self.worker; self.worker=None; self.refresh_button.setEnabled(True)
        if worker:worker.deleteLater()
        if self.pending_action and not self.stopping:
            entry,action=self.pending_action; self.pending_action=None
            QTimer.singleShot(0,lambda:self.refresh(entry,action))
    def change(self,row,action):
        prompt={"call":"اجازه ورود","enter":"ثبت ورود","exit":"ثبت خروج"}[action]+f" نوبت {digits(row['number'])} ({queue_day(row['day'])}) برای {row['driver_name']}؟"
        if QMessageBox.question(self,"تغییر وضعیت نوبت",prompt,QMessageBox.Yes|QMessageBox.No)==QMessageBox.Yes:
            self.refresh(row['id'],action)
    def open_display(self):
        if self.public is None:self.public=QueueDisplay()
        if self.last_success and time.monotonic()-self.last_success<15:
            self.public.set_snapshot(self.snapshot); self.public.updated=self.last_success
        self.public.show(); self.public.raise_()
        self.refresh()
    def shutdown(self):
        self.stopping=True; self.pending_action=None; self.timer.stop()
        if self.public:self.public.close(); self.public.timer.stop()
        if self.worker:self.worker.wait()
        self.http.close()
