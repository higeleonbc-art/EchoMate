"""
input_monitor.py - プレイヤー操作強度モニタリング

pynput を使用してマウス・キーボードのイベントをカウントし、
StateManager.record_input_event() に通知して input_intensity を更新する。

依存ライブラリ:
  pip install pynput
"""

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

try:
    from pynput import mouse, keyboard
    _PYNPUT_AVAILABLE = True
except ImportError:
    _PYNPUT_AVAILABLE = False

# マウス移動イベントのスロットリング間隔（秒）。頻発しすぎるイベントを抑制する。
_MOUSE_THROTTLE_SEC = 0.08  # 最大 ~12 回/秒


class InputMonitor:
    """
    マウス・キーボードのイベントを監視し、コールバックで通知するクラス。

    使い方:
        monitor = InputMonitor(event_callback=state_manager.record_input_event)
        monitor.start()
        ...
        monitor.stop()
    """

    def __init__(self, event_callback: Optional[Callable[[], None]] = None) -> None:
        self._event_callback = event_callback
        self._mouse_listener = None
        self._keyboard_listener = None
        self._running = False
        self._last_mouse_time: float = 0.0

    @property
    def available(self) -> bool:
        return _PYNPUT_AVAILABLE

    def start(self) -> None:
        if not _PYNPUT_AVAILABLE:
            logger.warning(
                "InputMonitor: pynput not installed — input monitoring disabled. "
                "Run: pip install pynput"
            )
            return

        self._running = True

        self._mouse_listener = mouse.Listener(
            on_move=self._on_mouse_move,
            on_click=self._on_mouse_click,
        )
        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
        )
        self._mouse_listener.start()
        self._keyboard_listener.start()
        logger.info("InputMonitor started")

    def stop(self) -> None:
        self._running = False
        if self._mouse_listener:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
        if self._keyboard_listener:
            try:
                self._keyboard_listener.stop()
            except Exception:
                pass
        logger.info("InputMonitor stopped")

    def _fire(self) -> None:
        if self._running and self._event_callback:
            try:
                self._event_callback()
            except Exception:
                pass

    def _on_mouse_move(self, x: int, y: int) -> None:
        now = time.time()
        if now - self._last_mouse_time >= _MOUSE_THROTTLE_SEC:
            self._last_mouse_time = now
            self._fire()

    def _on_mouse_click(self, x: int, y: int, button, pressed: bool) -> None:
        if pressed:
            self._fire()

    def _on_key_press(self, key) -> None:
        self._fire()
