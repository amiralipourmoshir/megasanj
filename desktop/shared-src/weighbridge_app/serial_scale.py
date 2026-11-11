from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

try:
    import serial
except ImportError:  # permits database/domain tests without optional hardware
    serial = None


@dataclass(frozen=True)
class ScaleReading:
    weight_kg: Decimal
    stable: bool
    raw_payload: str
    captured_at: float
    zero: bool = False
    status_code: int | None = None


class WeightParser:
    """Legacy ASCII parser kept for installations with a text indicator."""

    def __init__(self, pattern: str = r"[-+]?\d+(?:[.,]\d+)?", stability_samples: int = 5,
                 tolerance_kg: float = 1.0, multiplier: float = 1.0):
        self.pattern = re.compile(pattern)
        self.samples: deque[Decimal] = deque(maxlen=max(2, stability_samples))
        self.tolerance = Decimal(str(tolerance_kg))
        self.multiplier = Decimal(str(multiplier))

    def parse(self, raw: bytes | str) -> ScaleReading | None:
        text = raw.decode("ascii", "replace") if isinstance(raw, bytes) else raw
        matches = self.pattern.findall(text)
        if not matches:
            return None
        token = matches[-1] if isinstance(matches[-1], str) else matches[-1][0]
        try:
            value = Decimal(token.replace(",", ".")) * self.multiplier
        except Exception:
            return None
        self.samples.append(value)
        stable = len(self.samples) == self.samples.maxlen and max(self.samples) - min(self.samples) <= self.tolerance
        return ScaleReading(value, stable, text.strip(), time.time(), value == 0)


class Teta10AParser:
    """Streaming parser for the captured binary TETA10-A RS232 protocol."""

    FRAME_SIZE = 5
    VALID_MASK = 0x80
    STABLE_MASK = 0x10
    ZERO_MASK = 0x08
    OBSERVED_STATUSES = frozenset((0x80, 0x88, 0x90, 0x98))
    SYNC_FRAMES = 2

    def __init__(self, maximum_weight_kg: int = 60_000):
        self.maximum_weight_kg = max(1, int(maximum_weight_kg))
        self._buffer = bytearray()
        self._synchronized = False
        self.discarded_bytes = 0

    @staticmethod
    def _hex(frame: bytes) -> str:
        return " ".join(f"{byte:02X}" for byte in frame)

    @staticmethod
    def _decode_weight(payload: bytes) -> int:
        """Decode the indicator's four 7-bit weight digits.

        TETA10-A does not transmit the weight as a normal 32-bit integer.
        Each payload byte contributes seven data bits, so the number is base
        128.  Treating it as base 256 happens to work below 128 kg but grows
        progressively wrong at higher weights (for example 00 01 01 30 is
        16,560 kg in base 128, not 65,840 kg).
        """
        weight = 0
        for byte in payload:
            weight = weight * 128 + byte
        return weight

    def _is_frame(self, frame: bytes) -> bool:
        if len(frame) != self.FRAME_SIZE or frame[4] not in self.OBSERVED_STATUSES:
            return False
        # Weight bytes are seven-bit digits. A set high bit means the stream is
        # not aligned to a genuine frame boundary.
        if any(byte >= 0x80 for byte in frame[:4]):
            return False
        return self._decode_weight(frame[:4]) <= self.maximum_weight_kg

    def parse_frame(self, frame: bytes) -> ScaleReading | None:
        if not self._is_frame(frame):
            return None
        status = frame[4]
        weight = self._decode_weight(frame[:4])
        return ScaleReading(
            weight_kg=Decimal(weight),
            stable=bool(status & self.STABLE_MASK),
            raw_payload=self._hex(frame),
            captured_at=time.time(),
            zero=bool(status & self.ZERO_MASK),
            status_code=status,
        )

    def feed(self, raw: bytes) -> list[ScaleReading]:
        """Consume arbitrary chunks and recover the real five-byte boundary.

        A status-looking byte can also occur inside a large weight.  Accepting a
        single candidate frame therefore occasionally shifted the stream by one
        or more bytes and produced values close to 65535 kg.  We now acquire
        synchronization only after two consecutive valid frames and drop the
        lock as soon as the framing stops being valid.
        """
        if raw:
            self._buffer.extend(raw)
        readings: list[ScaleReading] = []

        required = self.FRAME_SIZE * self.SYNC_FRAMES
        while True:
            if not self._synchronized:
                if len(self._buffer) < required:
                    break

                sync_offset = None
                for offset in range(self.FRAME_SIZE):
                    if len(self._buffer) < offset + required:
                        break
                    candidates = (
                        bytes(self._buffer[offset + index * self.FRAME_SIZE:
                                           offset + (index + 1) * self.FRAME_SIZE])
                        for index in range(self.SYNC_FRAMES)
                    )
                    if all(self._is_frame(frame) for frame in candidates):
                        sync_offset = offset
                        break

                if sync_offset is None:
                    # Keep enough trailing bytes for a pair that may be split
                    # across the next serial read, but discard stale noise.
                    keep = required - 1
                    discard = max(1, len(self._buffer) - keep)
                    del self._buffer[:discard]
                    self.discarded_bytes += discard
                    continue

                if sync_offset:
                    del self._buffer[:sync_offset]
                    self.discarded_bytes += sync_offset
                self._synchronized = True

            if len(self._buffer) < self.FRAME_SIZE:
                break
            frame = bytes(self._buffer[:self.FRAME_SIZE])
            reading = self.parse_frame(frame)
            if reading is None:
                # The cable can inject/drop a byte. Reacquire instead of
                # interpreting the following control byte as weight data.
                del self._buffer[0]
                self.discarded_bytes += 1
                self._synchronized = False
                continue
            del self._buffer[:self.FRAME_SIZE]
            readings.append(reading)
        return readings


class SerialScale:
    """Background TETA10-A serial reader. Callbacks run on its worker thread."""

    NO_DATA_TIMEOUT_SECONDS = 2.5

    def __init__(self, port="COM1", baudrate=9600, bytesize=8, parity="N", stopbits=1,
                 parser: Teta10AParser | None = None):
        self.config = dict(
            port=port,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=0.2,
            write_timeout=0.2,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        self.parser = parser or Teta10AParser()
        self.connected = False
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _friendly_open_error(self, exc: Exception) -> str:
        port = self.config["port"]
        error = str(exc).strip()
        lowered = error.lower()
        if "permission" in lowered or "access is denied" in lowered or "دسترسی" in lowered:
            reason = "پورت توسط برنامه دیگری اشغال شده یا ویندوز اجازه دسترسی نداده است؛ نرم‌افزار قدیمی باسکول را ببندید."
        elif "file not found" in lowered or "cannot find" in lowered or "no such file" in lowered:
            reason = "این پورت در ویندوز وجود ندارد؛ شماره COM را در Device Manager و تنظیمات برنامه بررسی کنید."
        else:
            reason = f"بازکردن پورت ممکن نشد ({error or type(exc).__name__})."
        return f"باسکول قطع است — ارتباط با {port} برقرار نشد: {reason}"

    @staticmethod
    def _reading_state(reading: ScaleReading) -> str:
        if reading.zero and reading.stable:
            detail = "نمایشگر روی صفر و وزن پایدار است"
        elif reading.zero:
            detail = "نمایشگر نزدیک صفر است، اما وزن هنوز ناپایدار است"
        elif reading.stable:
            detail = "داده معتبر دریافت می‌شود و وزن پایدار است"
        else:
            detail = "داده معتبر دریافت می‌شود، اما وزن در حال تغییر است"
        return f"ارتباط کامل با باسکول — {detail}"

    def start(self, on_reading: Callable[[ScaleReading], None], on_state: Callable[[bool, str], None]):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def run():
            if serial is None:
                self.last_error = "باسکول قطع است — کتابخانه ارتباط سریال نصب نیست."
                on_state(False, self.last_error)
                return
            port_name = self.config["port"]
            try:
                on_state(False, f"در حال بازکردن پورت {port_name}…")
                with serial.Serial(**self.config) as port:
                    self.connected = False
                    self.last_error = ""
                    on_state(False, f"پورت {port_name} باز شد، اما هنوز هیچ داده‌ای از TETA10-A دریافت نشده است.")
                    opened_at = time.monotonic()
                    last_byte_at: float | None = None
                    last_valid_at: float | None = None
                    last_state_key: tuple[bool, bool] | None = None
                    no_data_reported = False
                    invalid_reported = False

                    while not self._stop.is_set():
                        waiting = int(getattr(port, "in_waiting", 0) or 0)
                        raw = port.read(max(1, min(waiting, 4096)))
                        now = time.monotonic()
                        if raw:
                            last_byte_at = now
                            readings = self.parser.feed(raw)
                            if readings:
                                for reading in readings:
                                    last_valid_at = now
                                    self.connected = True
                                    on_reading(reading)
                                    state_key = (reading.stable, reading.zero)
                                    if state_key != last_state_key:
                                        on_state(True, self._reading_state(reading))
                                        last_state_key = state_key
                                no_data_reported = False
                                invalid_reported = False
                            elif last_valid_at is None and not invalid_reported and now - opened_at >= 0.8:
                                on_state(False, f"پورت {port_name} داده دریافت می‌کند، اما قالب آن با پروتکل پنج‌بایتی TETA10-A سازگار نیست؛ تنظیمات سریال یا نوع دستگاه را بررسی کنید.")
                                invalid_reported = True

                        reference = last_valid_at if last_valid_at is not None else opened_at
                        if now - reference >= self.NO_DATA_TIMEOUT_SECONDS and not no_data_reported:
                            self.connected = False
                            if last_byte_at is None:
                                message = f"باسکول قطع است — پورت {port_name} باز است، اما هیچ داده‌ای از دستگاه نمی‌رسد؛ کابل RS232، خروجی نمایشگر و شماره پورت را بررسی کنید."
                            elif last_valid_at is None:
                                message = f"باسکول قابل خواندن نیست — از {port_name} داده می‌رسد، اما حتی یک فریم معتبر TETA10-A شناسایی نشد؛ Baud/8N1 یا سیم‌بندی را بررسی کنید."
                            else:
                                message = f"ارتباط داده قطع شد — پورت {port_name} هنوز باز است، اما بیش از {self.NO_DATA_TIMEOUT_SECONDS:g} ثانیه فریم معتبر دریافت نشده است."
                            self.last_error = message
                            on_state(False, message)
                            no_data_reported = True
                            last_state_key = None
            except Exception as exc:
                self.last_error = self._friendly_open_error(exc)
                on_state(False, self.last_error)
            finally:
                self.connected = False

        self._thread = threading.Thread(target=run, daemon=True, name="scale-serial")
        self._thread.start()

    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(2)
