#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
joymove.py — аналог JoyDance для PS Move без psmoveapi.

Управление:
  - Наклоны контроллера        → up / down / left / right
  - Вращение (гироскоп Z)      → spin_cw / spin_ccw
  - Резкий взмах вперёд (gyro Y) → punch
  - Тряска                     → shake

Подсветка шара и вибрация:
  - Покой / обычное попадание  → зелёный пульс 0.2 c
  - Perfect                    → белый 0.1 c
  - Miss (X)                   → красный 0.1 c + вибрация 0.1 c
  - Gold Move                  → жёлтый 1.0 c + вибрация 1.0 c
  - Выход                      → LED off, вибро off

Зависимости:
    pip install hidapi numpy

Linux udev (чтобы читать без root):
    sudo tee /etc/udev/rules.d/99-psmove.rules <<'EOF'
    KERNEL=="hidraw*", ATTRS{idVendor}=="054c", ATTRS{idProduct}=="03d5", MODE="0666"
    KERNEL=="hidraw*", ATTRS{idVendor}=="054c", ATTRS{idProduct}=="05c4", MODE="0666"
    EOF
    sudo udevadm control --reload-rules && sudo udevadm trigger
"""

import time
import threading
import random
from dataclasses import dataclass, field
from typing import Optional, Tuple

try:
    import hid
except ImportError as e:
    raise SystemExit("Установи hidapi:  pip install hidapi") from e


# ===========================================================================
# Константы устройства
# ===========================================================================
SONY_VID = 0x054C
MOVE_PIDS = {0x03D5, 0x05C4, 0xC5F3}   # разные ревизии PS Move
REPORT_LEN = 49


# ===========================================================================
# Палитра
# ===========================================================================
class Color:
    OFF    = (0, 0, 0)
    GREEN  = (0, 255, 0)
    PULSE  = (0, 255, 80)     # чуть ярче базового для «пульса»
    WHITE  = (255, 255, 255)
    RED    = (255, 0, 0)
    YELLOW = (255, 200, 0)


# ===========================================================================
# Состояние, читаемое с контроллера
# ===========================================================================
@dataclass
class MoveState:
    ax: float = 0.0; ay: float = 0.0; az: float = 0.0   # ускорение, g
    gx: float = 0.0; gy: float = 0.0; gz: float = 0.0   # град/с
    trigger: int = 0
    buttons: set = field(default_factory=set)
    timestamp: float = 0.0


# ===========================================================================
# Обёртка над PS Move: чтение + LED + вибро
# ===========================================================================
class PSMove:
    def __init__(self, device_path: Optional[bytes] = None):
        self.dev = hid.device()
        path = device_path or self._find_device()
        self.dev.open_path(path)
        self.dev.set_nonblocking(0)

        # Инициализация потока репортов и калибровки
        self.dev.write([0x02, 0x00, 0x00, 0x00, 0x00])
        self.dev.write([0x04, 0x00, 0x00, 0x00, 0x00])
        self.dev.write([0x02, 0x10, 0x00, 0x00, 0x00])
        time.sleep(0.2)
        self.dev.write([0x02, 0x00, 0x00, 0x00, 0x00])

        # LED / вибро
        self._led_lock = threading.Lock()
        self._vibro_lock = threading.Lock()
        self._flash_id = 0
        self._base_color: Tuple[int, int, int] = Color.GREEN
        self._current_color: Tuple[int, int, int] = Color.OFF
        self.set_led(*Color.GREEN)

    # ------------------------------------------------------------------
    @staticmethod
    def _find_device() -> bytes:
        for d in hid.enumerate(SONY_VID, 0):
            if d["product_id"] in MOVE_PIDS:
                return d["path"]
        raise RuntimeError(
            "PS Move не найден. Проверь сопряжение по Bluetooth и udev-правила."
        )

    # ------------------------------------------------------------------
    # LED
    # ------------------------------------------------------------------
    def set_led(self, r: int, g: int, b: int) -> None:
        """Мгновенно выставить цвет шара."""
        r, g, b = int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF
        with self._led_lock:
            try:
                self.dev.write([0x06, 0x00, 0x00, 0x00, r, g, b])
                self._current_color = (r, g, b)
            except OSError:
                pass

    def set_base_color(self, color: Tuple[int, int, int]) -> None:
        """Цвет «покоя» — к нему возвращаемся после вспышек."""
        self._base_color = color
        self.set_led(*color)

    def flash_led(self, color: Tuple[int, int, int], duration: float) -> None:
        """
        Асинхронная вспышка: color на duration секунд, затем base_color.
        Если за время сна была запущена более новая вспышка — не перебиваем её.
        """
        self._flash_id += 1
        my_id = self._flash_id

        def worker():
            self.set_led(*color)
            time.sleep(duration)
            if my_id == self._flash_id:
                self.set_led(*self._base_color)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Вибро
    # ------------------------------------------------------------------
    def set_vibro(self, on: bool) -> None:
        with self._vibro_lock:
            try:
                if on:
                    self.dev.write([0x05, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x00])
                else:
                    self.dev.write([0x05, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
            except OSError:
                pass

    def vibrate(self, duration: float) -> None:
        """Асинхронная вибрация на duration секунд."""
        def worker():
            self.set_vibro(True)
            time.sleep(duration)
            self.set_vibro(False)
        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------
    @staticmethod
    def _s16(lo: int, hi: int) -> int:
        v = lo | (hi << 8)
        return v - 65536 if v >= 32768 else v

    def read(self) -> Optional[MoveState]:
        data = self.dev.read(REPORT_LEN, timeout_ms=20)
        if not data or len(data) < 24:
            return None

        b1, b2 = data[1], data[2]
        buttons = set()
        if b1 & 0x08: buttons.add("select")
        if b1 & 0x01: buttons.add("start")
        if b1 & 0x10: buttons.add("triangle")
        if b1 & 0x20: buttons.add("circle")
        if b1 & 0x40: buttons.add("cross")
        if b1 & 0x80: buttons.add("square")
        if b2 & 0x01: buttons.add("move")
        trigger = data[6]

        ax = self._s16(data[7],  data[8])  / 8192.0
        ay = self._s16(data[9],  data[10]) / 8192.0
        az = self._s16(data[11], data[12]) / 8192.0
        gx = self._s16(data[15], data[16]) / 16.0
        gy = self._s16(data[17], data[18]) / 16.0
        gz = self._s16(data[19], data[20]) / 16.0

        return MoveState(ax, ay, az, gx, gy, gz, trigger, buttons, time.time())


# ===========================================================================
# Сглаживание
# ===========================================================================
class LowPass:
    def __init__(self, alpha: float = 0.2):
        self.alpha = alpha
        self.value: Optional[float] = None

    def __call__(self, x: float) -> float:
        if self.value is None:
            self.value = x
        else:
            self.value = self.alpha * x + (1 - self.alpha) * self.value
        return self.value


# ===========================================================================
# Жесты
# ===========================================================================
@dataclass
class GestureEvent:
    name: str
    power: float = 1.0


class GestureRecognizer:
    def __init__(self):
        self.lp_ax = LowPass(0.3); self.lp_ay = LowPass(0.3); self.lp_az = LowPass(0.3)
        self.lp_gx = LowPass(0.4); self.lp_gy = LowPass(0.4); self.lp_gz = LowPass(0.4)

        self.last_event_time = 0.0
        self.cooldown = 0.35

        self.tilt_thresh      = 0.45    # g
        self.gyro_spin_thresh = 120.0   # град/с
        self.punch_thresh     = 250.0   # град/с
        self.shake_thresh     = 180.0   # град/с

    def update(self, s: MoveState) -> Optional[GestureEvent]:
        now = s.timestamp
        if now - self.last_event_time < self.cooldown:
            return None

        ax = self.lp_ax(s.ax); ay = self.lp_ay(s.ay); az = self.lp_az(s.az)
        gx = self.lp_gx(s.gx); gy = self.lp_gy(s.gy); gz = self.lp_gz(s.gz)

        if abs(gy) > self.punch_thresh:
            self.last_event_time = now
            return GestureEvent("punch", abs(gy) / self.punch_thresh)

        if abs(gz) > self.gyro_spin_thresh:
            self.last_event_time = now
            return GestureEvent("spin_cw" if gz > 0 else "spin_ccw",
                                abs(gz) / self.gyro_spin_thresh)

        if abs(gx) > self.shake_thresh and abs(gy) > self.shake_thresh:
            self.last_event_time = now
            return GestureEvent("shake",
                                (abs(gx) + abs(gy)) / (2 * self.shake_thresh))

        if ay < -self.tilt_thresh:
            self.last_event_time = now
            return GestureEvent("up", abs(ay) / self.tilt_thresh)
        if ay > self.tilt_thresh:
            self.last_event_time = now
            return GestureEvent("down", abs(ay) / self.tilt_thresh)
        if ax > self.tilt_thresh:
            self.last_event_time = now
            return GestureEvent("right", abs(ax) / self.tilt_thresh)
        if ax < -self.tilt_thresh:
            self.last_event_time = now
            return GestureEvent("left", abs(ax) / self.tilt_thresh)

        return None


# ===========================================================================
# Игра
# ===========================================================================
MOVES = ["up", "down", "left", "right",
         "spin_cw", "spin_ccw", "punch", "shake"]


class JoyMoveGame:
    def __init__(self, move: PSMove, bpm: float = 90.0):
        self.move = move
        self.bpm = bpm
        self.beat_interval = 60.0 / bpm
        self.beat_index = 0
        self.next_beat_time = time.time() + 2.0    # разогрев

        self.current_target: Optional[str] = None
        self.target_is_gold = False
        self.target_expires = 0.0
        self.hit_window = 0.45

        self.score = 0
        self.combo = 0
        self.best_combo = 0
        self.hits = 0
        self.misses = 0
        self.last_msg = ""
        self.msg_until = 0.0

        # Базовый цвет — зелёный
        self.move.set_base_color(Color.GREEN)

    # ------------------------------------------------------------------
    def _schedule_next(self):
        self.beat_index += 1
        self.current_target = random.choice(MOVES)
        self.target_is_gold = (self.beat_index % 8 == 0) or (random.random() < 0.05)
        self.target_expires = self.next_beat_time + self.hit_window
        self.next_beat_time += self.beat_interval

        # Намёк на Gold — короткая жёлтая вспышка до начала окна
        if self.target_is_gold:
            self.move.flash_led(Color.YELLOW, min(self.beat_interval * 0.3, 0.35))

    # ------------------------------------------------------------------
    def update(self, ev: Optional[GestureEvent]) -> None:
        now = time.time()

        if self.current_target is None and now >= self.next_beat_time - self.beat_interval:
            self._schedule_next()

        if self.current_target and now > self.target_expires:
            self._on_miss()

        if ev and self.current_target and now <= self.target_expires:
            if ev.name == self.current_target:
                self._on_hit(ev)

    # ------------------------------------------------------------------
    def _on_hit(self, ev: GestureEvent):
        self.hits += 1
        self.combo += 1
        self.best_combo = max(self.best_combo, self.combo)

        time_left = self.target_expires - time.time()
        is_perfect = (time_left > self.hit_window * 0.66) or (ev.power > 1.5)

        if self.target_is_gold:
            points = int(300 * (1 + self.combo // 5))
            self.score += points
            self.move.flash_led(Color.YELLOW, 1.0)
            self.move.vibrate(1.0)
            self._say(f"★ GOLD MOVE! +{points}")

        elif is_perfect:
            points = int(150 * (1 + self.combo // 5))
            self.score += points
            self.move.flash_led(Color.WHITE, 0.1)
            self._say(f"PERFECT! +{points}")

        else:
            points = int(100 * (1 + self.combo // 5))
            self.score += points
            self.move.flash_led(Color.PULSE, 0.2)
            self._say(f"HIT! +{points}")

        self._schedule_next()

    # ------------------------------------------------------------------
    def _on_miss(self):
        self.misses += 1
        self.combo = 0
        self.move.flash_led(Color.RED, 0.1)
        self.move.vibrate(0.1)
        self._say(f"MISS ({self.current_target})")
        self._schedule_next()

    # ------------------------------------------------------------------
    def _say(self, text: str):
        self.last_msg = text
        self.msg_until = time.time() + 0.7

    # ------------------------------------------------------------------
    def render(self) -> str:
        now = time.time()
        time_left = max(0.0, self.target_expires - now)
        progress = int(30 * (1 - time_left / max(self.hit_window, 0.001)))
        progress = min(max(progress, 0), 30)
        bar = "=" * 30

        gold_tag = " ★ GOLD" if self.target_is_gold else ""
        lines = [
            "",
            "┌───────────────────────────────┐",
            "│        JOY  MOVE  (PS)        │",
            "└───────────────────────────────┘",
            f"  Счёт: {self.score:>6}   Комбо: x{self.combo} (best {self.best_combo})",
            f"  Hits: {self.hits:>3}  Miss: {self.misses:>3}",
            "",
            f"  Цель:  {self.current_target or '-':<10}{gold_tag}",
            f"  Окно:  [{bar[:progress]}{'.'*(30-progress)}]",
            "",
        ]
        lines.append(f"  >> {self.last_msg}" if now < self.msg_until else "")
        return "\n".join(lines) + "\n"


# ===========================================================================
# Точка входа
# ===========================================================================
def main():
    print("Ищу PS Move...")
    move = PSMove()
    print("Подключено. Начинаем!")
    print("Жесты: наклоны, вращения, удары, тряска. Ctrl+C — выход.\n")

    rec = GestureRecognizer()
    game = JoyMoveGame(move, bpm=90)

    try:
        while True:
            state = move.read()
            if state is None:
                continue
            ev = rec.update(state)
            game.update(ev)
            # ANSI clear-screen + cursor home, чтобы не мигало
            print("\033[H\033[J" + game.render(), end="", flush=True)
            time.sleep(0.01)

    except KeyboardInterrupt:
        print("\n\nФинал:")
        print(f"  Счёт:        {game.score}")
        print(f"  Лучшее комбо: x{game.best_combo}")
        print(f"  Hits:        {game.hits}")
        print(f"  Miss:        {game.misses}")

    finally:
        # Всегда гасим свет и вибро, даже при ошибке
        try:
            move.set_led(*Color.OFF)
            move.set_vibro(False)
        except Exception:
            pass


if __name__ == "__main__":
    main()