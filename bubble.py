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

import math
import os
import random
import tkinter as tk
import threading

try:
    from PIL import Image, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

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


# ── アバター立ち絵ウィンドウ ────────────────────────────────────────────────

class AvatarWindow:
    """
    アバター立ち絵ウィンドウ。

    assets/sprites/ フォルダにユーザーが用意した画像パーツを PIL で合成して表示する。
    PIL 未インストール or スプライト未配置時はプレースホルダーを描画する。

    ベース画像: base_{emotion}_{mouth}.png  (emotion: idle/smile, mouth: close/open)
    目画像    : eyes_{state}.png           (state: open/half/close/smile)

    Parameters
    ----------
    master        : 親ウィジェット（None なら独立 Tk ウィンドウ）
    voice_output  : VoiceOutput インスタンス（is_speaking 参照用）
    state_manager : StateManager インスタンス（tension 参照用）
    """

    SPRITE_DIR = "assets/sprites"
    FRAME_MS   = 80   # アニメーションフレーム間隔 (ms) ≈ 12fps

    # ベース画像ファイル: (emotion, mouth) → filename
    _BASE_FILES: dict[tuple[str, str], str] = {
        ("idle",  "close"): "base_idle_close.png",
        ("idle",  "open"):  "base_idle_open.png",
        ("smile", "close"): "base_smile_close.png",
        ("smile", "open"):  "base_smile_open.png",
    }
    # 目画像ファイル: state → filename
    _EYE_FILES: dict[str, str] = {
        "open":  "eyes_open.png",
        "half":  "eyes_half.png",
        "close": "eyes_close.png",
        "smile": "eyes_smile.png",
    }

    def __init__(
        self,
        master: "tk.Misc | None" = None,
        voice_output=None,
        state_manager=None,
    ) -> None:
        self._voice_output  = voice_output
        self._state_manager = state_manager

        if master is not None:
            self.window = tk.Toplevel(master)
        else:
            self.window = tk.Tk()

        self.window.title("EchoMate アバター")
        self.window.geometry("220x320+820+80")
        self.window.configure(bg=CHROMA_KEY_COLOR)
        self.window.attributes("-topmost", True)
        self.window.resizable(True, True)
        self.window.protocol("WM_DELETE_WINDOW", self.hide)

        # ドラッグ移動用
        self._drag_x = 0
        self._drag_y = 0

        self._canvas = tk.Canvas(
            self.window, bg=CHROMA_KEY_COLOR, highlightthickness=0
        )
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._canvas.bind("<ButtonPress-1>", self._drag_start)
        self._canvas.bind("<B1-Motion>",     self._drag_move)

        # スプライトキャッシュ
        self._base_imgs: dict[tuple[str, str], "Image.Image"] = {}
        self._eye_imgs:  dict[str, "Image.Image"] = {}
        self._photo: object = None   # GC防止用参照

        # アニメーション状態
        self._tension    = 0.3
        self._anim_phase = 0.0
        self._offset_x   = 0
        self._offset_y   = 0

        # まばたき状態機械
        # stages: 0=開く, 1=半閉じ(閉方向), 2〜3=閉じる, 4=半閉じ(開方向)
        self._blink_stage   = 0
        self._blink_counter = 0
        self._blink_trigger = self._next_blink_frames()

        self._load_sprites()
        # sprites フォルダが未作成なら作る
        os.makedirs(self.SPRITE_DIR, exist_ok=True)
        self._animate()

    # ── スプライト読み込み ────────────────────────────────────────────────────

    def _load_sprites(self) -> None:
        if not _PIL_AVAILABLE:
            return
        for (emo, mouth), fname in self._BASE_FILES.items():
            path = os.path.join(self.SPRITE_DIR, fname)
            if os.path.isfile(path):
                try:
                    self._base_imgs[(emo, mouth)] = Image.open(path).convert("RGBA")
                except Exception:
                    pass
        for state, fname in self._EYE_FILES.items():
            path = os.path.join(self.SPRITE_DIR, fname)
            if os.path.isfile(path):
                try:
                    self._eye_imgs[state] = Image.open(path).convert("RGBA")
                except Exception:
                    pass

    # ── アニメーションループ ──────────────────────────────────────────────────

    def _next_blink_frames(self) -> int:
        """次のまばたきまでのフレーム数（3〜6秒）"""
        return random.randint(
            int(3000 / self.FRAME_MS),
            int(6000 / self.FRAME_MS),
        )

    def _tick_blink(self, tension: float) -> str:
        """まばたき状態機械を1フレーム進め、目の状態文字列を返す"""
        self._blink_counter += 1

        if self._blink_stage == 0:
            if self._blink_counter >= self._blink_trigger:
                self._blink_stage   = 1
                self._blink_counter = 0
            return "smile" if tension >= 0.6 else "open"

        if self._blink_stage == 1:          # 半閉じ（閉方向）2フレーム
            if self._blink_counter >= 2:
                self._blink_stage   = 2
                self._blink_counter = 0
            return "half"

        if self._blink_stage in (2, 3):     # 閉じた状態 3フレーム
            if self._blink_counter >= 3:
                self._blink_stage   = 4
                self._blink_counter = 0
            return "close"

        # stage == 4: 半閉じ（開方向）2フレーム
        if self._blink_counter >= 2:
            self._blink_stage   = 0
            self._blink_counter = 0
            self._blink_trigger = self._next_blink_frames()
        return "half"

    def _animate(self) -> None:
        # テンション取得
        if self._state_manager is not None:
            try:
                self._tension = self._state_manager.get_state().tension
            except Exception:
                pass

        tension = self._tension
        emotion = "smile" if tension >= 0.6 else "idle"

        # 口パク
        is_speaking = False
        if self._voice_output is not None:
            try:
                is_speaking = self._voice_output.is_speaking
            except Exception:
                pass
        mouth = "open" if is_speaking else "close"

        # まばたき
        eye_state = self._tick_blink(tension)

        # 感情連動モーション
        self._anim_phase = (self._anim_phase + 0.2) % (2 * math.pi)
        if tension >= 0.6:
            # ピョンピョン跳ねる
            self._offset_y = int(math.sin(self._anim_phase * 2) * 6)
            self._offset_x = 0
        elif tension < 0.3:
            # ランダムに震える
            self._offset_x = random.randint(-2, 2)
            self._offset_y = random.randint(-2, 2)
        else:
            self._offset_x = 0
            self._offset_y = 0

        self._draw(emotion, mouth, eye_state)

        try:
            self.window.after(self.FRAME_MS, self._animate)
        except tk.TclError:
            pass

    # ── 描画 ─────────────────────────────────────────────────────────────────

    def _draw(self, emotion: str, mouth: str, eye_state: str) -> None:
        self._canvas.delete("all")
        w = self._canvas.winfo_width()  or 220
        h = self._canvas.winfo_height() or 320

        if _PIL_AVAILABLE and (self._base_imgs or self._eye_imgs):
            self._draw_sprite(emotion, mouth, eye_state, w, h)
        else:
            self._draw_placeholder(emotion, mouth, eye_state, w, h)

    def _draw_sprite(
        self, emotion: str, mouth: str, eye_state: str, w: int, h: int
    ) -> None:
        # ベース画像: 感情+口 → fallback chain
        base = (
            self._base_imgs.get((emotion, mouth))
            or self._base_imgs.get(("idle", mouth))
            or self._base_imgs.get(("idle", "close"))
            or (next(iter(self._base_imgs.values()), None))
        )
        if base is None:
            self._draw_placeholder(emotion, mouth, eye_state, w, h)
            return

        composed = base.copy()

        # 目レイヤー合成
        eye = self._eye_imgs.get(eye_state) or self._eye_imgs.get("open")
        if eye is not None:
            eye_sized = eye.resize(composed.size, Image.LANCZOS) if eye.size != composed.size else eye
            composed.alpha_composite(eye_sized)

        # ウィンドウサイズにフィット（アスペクト比維持）
        cw, ch = composed.size
        scale = min(w / cw, h / ch)
        if scale < 1.0:
            composed = composed.resize((int(cw * scale), int(ch * scale)), Image.LANCZOS)
            cw, ch = composed.size

        cx = (w - cw) // 2 + self._offset_x
        cy = (h - ch) // 2 + self._offset_y

        self._photo = ImageTk.PhotoImage(composed)
        self._canvas.create_image(cx, cy, anchor=tk.NW, image=self._photo)

    def _draw_placeholder(
        self, emotion: str, mouth: str, eye_state: str, w: int, h: int
    ) -> None:
        """スプライト未配置時の簡易プレースホルダー描画"""
        cx = w // 2 + self._offset_x
        cy = h // 2 + self._offset_y
        r  = min(w, h) // 3

        face_color = "#FFD080" if emotion == "smile" else "#FFCC66"
        self._canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            fill=face_color, outline="#CC8800", width=2,
        )

        # 目（左右）
        ew = max(r // 5, 4)
        eye_y = cy - r // 4
        for ex in (cx - r // 3, cx + r // 3):
            if eye_state in ("close", "half"):
                self._canvas.create_line(
                    ex - ew, eye_y, ex + ew, eye_y,
                    fill="#333333", width=3,
                )
            else:
                er = ew if eye_state == "open" else ew // 2
                self._canvas.create_oval(
                    ex - er, eye_y - er, ex + er, eye_y + er,
                    fill="#333333", outline="",
                )

        # 口
        mouth_y = cy + r // 3
        if mouth == "open":
            self._canvas.create_oval(
                cx - r // 5, mouth_y - r // 10,
                cx + r // 5, mouth_y + r // 6,
                fill="#CC4444", outline="#883333", width=1,
            )
        elif emotion == "smile":
            self._canvas.create_arc(
                cx - r // 3, mouth_y - r // 6,
                cx + r // 3, mouth_y + r // 6,
                start=200, extent=140,
                style=tk.ARC, outline="#333333", width=2,
            )
        else:
            self._canvas.create_line(
                cx - r // 5, mouth_y,
                cx + r // 5, mouth_y,
                fill="#333333", width=2,
            )

    # ── 公開 API ─────────────────────────────────────────────────────────────

    def show(self) -> None:
        try:
            self.window.deiconify()
            self.window.lift()
            self.window.attributes("-topmost", True)
        except tk.TclError:
            pass

    def hide(self) -> None:
        try:
            self.window.withdraw()
        except tk.TclError:
            pass

    def destroy(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    # ── ドラッグ ─────────────────────────────────────────────────────────────

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
