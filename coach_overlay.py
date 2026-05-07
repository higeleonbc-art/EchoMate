"""
coach_overlay.py — インゲーム用半透明オーバーレイ（tkinter, 依存ゼロ）

画面右下に固定された半透明・常時最前面・枠なしウィンドウ。
プレイの邪魔にならない非侵襲的なテキスト表示が目的。

スレッドセーフ:
    - update_text(text) は別スレッドから呼べる（Queue + after でmain threadで反映）
    - start() は呼び出しスレッドをmainloopでブロックする
    - close() で安全終了

スタンドアロン実行（動作確認用）:
    python coach_overlay.py
"""

from __future__ import annotations

import logging
import queue
import sys
import tkinter as tk
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# スタイル定数
# ---------------------------------------------------------------------------

OVERLAY_W       = 340
OVERLAY_H       = 180
OVERLAY_MARGIN_R = 20    # 画面右からの余白
OVERLAY_MARGIN_B = 80    # 画面下からの余白（タスクバー回避）

BG_COLOR        = "#0d0d0d"
FG_COLOR        = "#e8e8e8"
ACCENT_OK       = "#5fd17a"
ACCENT_WARN     = "#f5b942"
ACCENT_DANGER   = "#ef5350"
FONT_FAMILY     = "Consolas"
FONT_SIZE       = 11
ALPHA           = 0.78
POLL_INTERVAL_MS = 100   # キューチェック間隔


# ---------------------------------------------------------------------------
# CoachOverlay
# ---------------------------------------------------------------------------

class CoachOverlay:
    """画面右下に半透明テキストを出すオーバーレイ。

    使い方:
        overlay = CoachOverlay()
        # 別スレッドで update_text() を呼びつつ
        overlay.start()  # メインスレッドでブロックしてmainloop実行
    """

    def __init__(
        self,
        width: int = OVERLAY_W,
        height: int = OVERLAY_H,
        alpha: float = ALPHA,
        draggable: bool = True,
    ):
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._closed = False

        self.root = tk.Tk()
        self.root.title("ADC Coach")
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", alpha)
        self.root.attributes("-topmost", True)
        self.root.config(bg=BG_COLOR)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = sw - width - OVERLAY_MARGIN_R
        y = sh - height - OVERLAY_MARGIN_B
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        # ヘッダ
        self.header = tk.Label(
            self.root,
            text="ADC COACH",
            fg=ACCENT_OK,
            bg=BG_COLOR,
            font=(FONT_FAMILY, 9, "bold"),
            anchor="w",
        )
        self.header.pack(fill="x", padx=10, pady=(8, 2))

        # 本文
        self.body = tk.Label(
            self.root,
            text="待機中…",
            fg=FG_COLOR,
            bg=BG_COLOR,
            font=(FONT_FAMILY, FONT_SIZE),
            justify="left",
            anchor="nw",
            wraplength=width - 20,
        )
        self.body.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        if draggable:
            self._enable_drag()

        # ESCで閉じる
        self.root.bind_all("<Escape>", lambda _e: self.close())

        # キューポーリング開始
        self.root.after(POLL_INTERVAL_MS, self._poll_queue)

    # ------------------------------------------------------------------
    # ドラッグ移動
    # ------------------------------------------------------------------

    def _enable_drag(self) -> None:
        self._drag_origin: tuple[int, int] = (0, 0)

        def start(e):
            self._drag_origin = (e.x_root - self.root.winfo_x(),
                                 e.y_root - self.root.winfo_y())

        def move(e):
            x = e.x_root - self._drag_origin[0]
            y = e.y_root - self._drag_origin[1]
            self.root.geometry(f"+{x}+{y}")

        for w in (self.root, self.header, self.body):
            w.bind("<Button-1>", start)
            w.bind("<B1-Motion>", move)

    # ------------------------------------------------------------------
    # 公開API（スレッドセーフ）
    # ------------------------------------------------------------------

    def update_text(self, body_text: str, header_text: Optional[str] = None,
                    severity: str = "ok") -> None:
        """別スレッドからの更新。severity: ok / warn / danger"""
        self._queue.put(("update", f"{severity}|{header_text or ''}|{body_text}"))

    def close(self) -> None:
        self._queue.put(("close", ""))

    # ------------------------------------------------------------------
    # mainloop
    # ------------------------------------------------------------------

    def start(self) -> None:
        try:
            self.root.mainloop()
        finally:
            self._closed = True

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _poll_queue(self) -> None:
        try:
            while True:
                op, payload = self._queue.get_nowait()
                if op == "close":
                    self.root.destroy()
                    return
                if op == "update":
                    severity, header, body = payload.split("|", 2)
                    self._apply_update(severity, header, body)
        except queue.Empty:
            pass

        if not self._closed:
            self.root.after(POLL_INTERVAL_MS, self._poll_queue)

    def _apply_update(self, severity: str, header: str, body: str) -> None:
        color = {"ok": ACCENT_OK, "warn": ACCENT_WARN, "danger": ACCENT_DANGER}.get(
            severity, ACCENT_OK,
        )
        self.header.config(fg=color, text=header or "ADC COACH")
        self.body.config(text=body)


# ---------------------------------------------------------------------------
# スタンドアロン動作確認
# ---------------------------------------------------------------------------

def _demo() -> None:
    """python coach_overlay.py で表示確認できる簡易デモ"""
    import threading
    import time

    overlay = CoachOverlay()

    def feed():
        samples = [
            ("ok",     "LANE PHASE",   "CS@5min: 32 / target 40\nLulu E available\n敵JG bot側にいる可能性"),
            ("warn",   "CS LOW",       "CS/min: 5.2 / target 7.0\nミニオン取り戻すかロームか判断"),
            ("danger", "HP CRITICAL",  "HP 23%\nFlash on cooldown\n下がってリコール"),
            ("ok",     "MID GAME",     "Drake spawn in 45s\n視界準備\n敵ADC missing"),
        ]
        for sev, h, b in samples:
            time.sleep(2.5)
            overlay.update_text(b, header_text=h, severity=sev)
        time.sleep(2)
        overlay.close()

    threading.Thread(target=feed, daemon=True).start()
    overlay.start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _demo()
