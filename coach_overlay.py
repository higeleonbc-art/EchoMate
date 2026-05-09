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
        click_through: bool = False,
    ):
        """
        Args:
            click_through: True で Win32 API による click-through 化
                マウスイベントが背後の LoL に通り、ゲーム操作を妨害しない。
                ただし ESC や drag は効かなくなるため、終了は外部 (GUI Stop ボタン
                or プロセス kill) で行う必要がある。
        """
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._closed = False
        self._click_through = click_through
        self._width = width
        self._height = height

        self.root = tk.Tk()
        self.root.title("ADC Coach")
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", alpha)
        self.root.attributes("-topmost", True)
        self.root.config(bg=BG_COLOR)

        # 保存位置があれば優先、なければ画面右下にデフォルト配置
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        default_x = sw - width - OVERLAY_MARGIN_R
        default_y = sh - height - OVERLAY_MARGIN_B
        x, y = self._load_saved_position(default_x, default_y, sw, sh)
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

        if draggable and not click_through:
            self._enable_drag()

        # ESC: Draggable Mode なら位置保存して閉じる、click-through 中は効かない
        if click_through:
            self.root.bind_all("<Escape>", lambda _e: self.close())
        else:
            self.root.bind_all("<Escape>", lambda _e: self._save_position_and_close())

        # click-through 設定（mainloop 開始前にwindow handleが必要なので after で遅延適用）
        if click_through:
            self.root.after(50, self._make_click_through)

        # キューポーリング開始
        self.root.after(POLL_INTERVAL_MS, self._poll_queue)

    # ------------------------------------------------------------------
    # 位置の永続化 (Draggable Mode で動かしたら保存、次回起動時に再現)
    # ------------------------------------------------------------------

    def _load_saved_position(self, default_x: int, default_y: int,
                               screen_w: int, screen_h: int) -> tuple[int, int]:
        """coach_profile から overlay_position を読み出す。範囲外なら default 戻す。"""
        try:
            import coach_profile
            saved = coach_profile.get("overlay_position")
        except Exception:
            return default_x, default_y
        if not isinstance(saved, dict):
            return default_x, default_y
        try:
            x = int(saved.get("x", default_x))
            y = int(saved.get("y", default_y))
        except (TypeError, ValueError):
            return default_x, default_y
        # 画面外を完全に外れていないか軽くチェック
        if x < -self._width or x > screen_w or y < -self._height or y > screen_h:
            return default_x, default_y
        return x, y

    def _save_position_and_close(self) -> None:
        """現在位置を profile に保存してから close"""
        try:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            import coach_profile
            coach_profile.update(overlay_position={"x": x, "y": y})
            logger.info("Saved overlay position: (%d, %d)", x, y)
        except Exception as e:
            logger.warning("Failed to save overlay position: %s", e)
        self.close()

    # ------------------------------------------------------------------
    # Click-through (Windows のみ)
    # ------------------------------------------------------------------

    def _make_click_through(self) -> None:
        """Win32 API でマウスイベントを背後の窗にスルーさせる"""
        import sys
        if not sys.platform.startswith("win"):
            logger.info("Click-through: non-Windows platform, skipping")
            return
        try:
            import ctypes
            self.root.update_idletasks()
            # tk frame の HWND → toplevel HWND に変換
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            GWL_EXSTYLE      = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED     = 0x00080000
            WS_EX_TOOLWINDOW  = 0x00000080  # タスクバー非表示
            cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                cur | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW,
            )
            logger.info("Click-through enabled")
        except Exception as e:
            logger.warning("Click-through setup failed: %s", e)

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

    # デモは draggable+ESC可能（click_through 無効）にしておく
    overlay = CoachOverlay(click_through=False, draggable=True)

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
