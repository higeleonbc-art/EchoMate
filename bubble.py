"""
bubble.py - EchoMate 吹き出しウィンドウ

相棒の発言をグリーンバック背景の吹き出しとして表示する。
OBS の「ウィンドウキャプチャ」＋クロマキーフィルターで
ゲーム映像に透過合成することを想定している。

使い方:
    from bubble import SpeechBubble
    bubble = SpeechBubble(master=root)
    bubble.update_text("やったじゃん！")
    bubble.show()
"""

import tkinter as tk
import threading

# ── カラー定数 ──────────────────────────────────────────────────────────────
CHROMA_KEY_COLOR = "#00FF00"   # グリーンバック（OBS クロマキー用）
BUBBLE_BG        = "#FFFFFF"   # 吹き出し背景色（白）
BUBBLE_BORDER    = "#DDDDDD"   # 吹き出し枠色
TEXT_COLOR       = "#1A1A1A"   # テキスト色
TEXT_FONT_FAMILY = "Meiryo"    # 日本語フォント
TEXT_FONT_SIZE   = 20          # テキストサイズ（pt）
CORNER_RADIUS    = 22          # 吹き出し角丸半径
PADDING          = 18          # ウィンドウ端から吹き出しまでの余白

DEFAULT_WIDTH  = 640
DEFAULT_HEIGHT = 160


class SpeechBubble:
    """
    グリーンバック背景の吹き出し表示ウィンドウ。

    Parameters
    ----------
    master : tk.Misc, optional
        親ウィジェット。指定すると Toplevel として作成される。
        None の場合は独立した Tk ウィンドウとして作成される。
    """

    def __init__(self, master: tk.Misc | None = None) -> None:
        if master is not None:
            self.window = tk.Toplevel(master)
        else:
            self.window = tk.Tk()

        self.window.title("EchoMate 吹き出し")
        self.window.geometry(f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT}+120+80")
        self.window.configure(bg=CHROMA_KEY_COLOR)
        self.window.attributes("-topmost", True)
        self.window.resizable(True, True)

        # ウィンドウを閉じようとしても非表示にするだけにする
        self.window.protocol("WM_DELETE_WINDOW", self.hide)

        # ドラッグ移動用
        self._drag_x = 0
        self._drag_y = 0

        # 描画用 Canvas（背景はグリーンバック）
        self._canvas = tk.Canvas(
            self.window,
            bg=CHROMA_KEY_COLOR,
            highlightthickness=0,
        )
        self._canvas.pack(fill=tk.BOTH, expand=True)

        # ドラッグ移動のバインド
        self._canvas.bind("<ButtonPress-1>",   self._drag_start)
        self._canvas.bind("<B1-Motion>",       self._drag_move)
        self._canvas.bind("<Configure>",       self._on_resize)

        self._current_text = ""
        self._after_id = None

        # 初期描画
        self.window.after(50, self._redraw)

    # ── 公開 API ────────────────────────────────────────────────────────────

    def update_text(self, text: str) -> None:
        """テキストを更新する（スレッドセーフ）"""
        self._current_text = text
        try:
            self.window.after(0, self._redraw)
        except tk.TclError:
            pass  # ウィンドウ破棄後に呼ばれた場合は無視

    def show(self) -> None:
        """吹き出しウィンドウを表示する"""
        try:
            self.window.deiconify()
            self.window.lift()
            self.window.attributes("-topmost", True)
        except tk.TclError:
            pass

    def hide(self) -> None:
        """吹き出しウィンドウを非表示にする（破棄はしない）"""
        try:
            self.window.withdraw()
        except tk.TclError:
            pass

    def is_visible(self) -> bool:
        """ウィンドウが表示状態かどうかを返す"""
        try:
            return self.window.state() != "withdrawn"
        except tk.TclError:
            return False

    def destroy(self) -> None:
        """ウィンドウを完全に破棄する"""
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    # ── 描画 ────────────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        """キャンバスを再描画する"""
        self._canvas.delete("all")
        w = self._canvas.winfo_width()
        h = self._canvas.winfo_height()
        if w <= 1 or h <= 1:
            self.window.after(100, self._redraw)
            return

        pad = PADDING
        r   = CORNER_RADIUS
        x0, y0 = pad, pad
        x1, y1 = w - pad, h - pad

        if x1 <= x0 + 2 * r or y1 <= y0 + 2 * r:
            return  # ウィンドウが小さすぎる

        # 角丸四角形を描画（白い吹き出し）
        self._draw_rounded_rect(x0, y0, x1, y1, r)

        # テキスト描画
        cx = (x0 + x1) // 2
        cy = (y0 + y1) // 2
        max_width = x1 - x0 - 20

        self._canvas.create_text(
            cx, cy,
            text=self._current_text,
            font=(TEXT_FONT_FAMILY, TEXT_FONT_SIZE, "bold"),
            fill=TEXT_COLOR,
            width=max_width,
            anchor="center",
            justify=tk.CENTER,
            tags="bubble_text",
        )

    def _draw_rounded_rect(self, x0: int, y0: int, x1: int, y1: int, r: int) -> None:
        """角丸四角形を Canvas に描画する"""
        d = 2 * r
        points = [
            x0 + r, y0,
            x1 - r, y0,
            x1, y0,
            x1, y0 + r,
            x1, y1 - r,
            x1, y1,
            x1 - r, y1,
            x0 + r, y1,
            x0, y1,
            x0, y1 - r,
            x0, y0 + r,
            x0, y0,
        ]
        self._canvas.create_polygon(
            points,
            smooth=True,
            fill=BUBBLE_BG,
            outline=BUBBLE_BORDER,
            width=2,
            tags="bubble_bg",
        )

    # ── リサイズ・ドラッグ ───────────────────────────────────────────────────

    def _on_resize(self, event: tk.Event) -> None:
        """ウィンドウリサイズ時に再描画"""
        if self._after_id:
            self.window.after_cancel(self._after_id)
        self._after_id = self.window.after(50, self._redraw)

    def _drag_start(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - self.window.winfo_x()
        self._drag_y = event.y_root - self.window.winfo_y()

    def _drag_move(self, event: tk.Event) -> None:
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.window.geometry(f"+{x}+{y}")


# ── スタンドアロン起動（テスト用）──────────────────────────────────────────

if __name__ == "__main__":
    import time

    root = tk.Tk()
    root.withdraw()  # メインウィンドウは非表示

    bubble = SpeechBubble(master=root)
    bubble.show()

    demo_texts = [
        "やったじゃん！",
        "うまっ！天才かよ",
        "まあ…悪くなかったけど",
        "確認: キルが検出された",
        "",
    ]

    def cycle(i: int = 0) -> None:
        bubble.update_text(demo_texts[i % len(demo_texts)])
        root.after(2000, cycle, i + 1)

    root.after(500, cycle)
    root.mainloop()
