"""
gui.py - EchoMate GUI コントロールパネル

機能:
  - ゲーム認識: 実行中EXEから対象選択、プリセット適用、ウィンドウ座標取得
  - 相棒人格変更: キャラクターリストから選択
  - 起動/終了管理: VOICEVOX・Ollamaの自動起動/停止
  - 吹き出しUI: 表示/非表示の切り替え

起動:
    python gui.py
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Optional

# ── オプション依存ライブラリ ─────────────────────────────────────────────────
try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

try:
    import win32gui
    import win32process
    import win32api
    _WIN32 = True
except ImportError:
    _WIN32 = False

try:
    import mss
    _MSS = True
except ImportError:
    _MSS = False

try:
    from PIL import Image, ImageTk
    _PIL = True
except ImportError:
    _PIL = False

import requests

from bubble import SpeechBubble
from main import EchoMate
import main as _main_module

# ── 定数 ─────────────────────────────────────────────────────────────────────
PRESETS_DIR   = "presets"
KNOWN_PRESETS = ["valorant", "apex", "fortnite"]

VOICEVOX_EXE_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "VOICEVOX", "VOICEVOX.exe"),
    r"C:\Program Files\VOICEVOX\VOICEVOX.exe",
    r"C:\Program Files (x86)\VOICEVOX\VOICEVOX.exe",
]
VOICEVOX_URL = "http://localhost:50021/version"
OLLAMA_URL   = "http://localhost:11434/api/tags"

ZONE_DEFS = [
    ("hp_bar_low",   "low_hp",   "HP バーを選択（左下付近）",          "color_threshold",
     {"color": "red",  "threshold": 0.40}, 5.0),
    ("kill_feed",    "kill",     "キルフィードを選択（右上付近）",      "color_threshold",
     {"color": "yellow", "threshold": 0.12}, 3.0),
    ("death_screen", "death",    "デス暗転エリアを選択（中央部分）",    "brightness",
     {"mode": "drop", "threshold": 40}, 8.0),
    ("screen_flash", "big_play", "エフェクトエリアを選択（中央付近）",  "frame_diff",
     {"threshold": 25, "changed_ratio": 0.15}, 4.0),
]

ZONE_COLORS = ["#FF4444", "#FFDD00", "#AA44FF", "#44AAFF"]

logger = logging.getLogger(__name__)


# ── プロセス列挙 ──────────────────────────────────────────────────────────────

def list_running_exes() -> list[str]:
    """実行中プロセスの EXE 名を一覧取得する（重複なし・ソート済み）"""
    names: set[str] = set()

    if _PSUTIL:
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info["name"]
                if name and name.lower().endswith(".exe"):
                    names.add(name)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    else:
        # fallback: tasklist コマンドを利用
        try:
            result = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                parts = line.strip('"').split('","')
                if parts:
                    name = parts[0]
                    if name.lower().endswith(".exe"):
                        names.add(name)
        except Exception:
            pass

    return sorted(names, key=str.lower)


def detect_preset(exe_name: str) -> Optional[str]:
    """EXE 名からプリセット名を推定する"""
    lower = exe_name.lower()
    for preset in KNOWN_PRESETS:
        if preset in lower:
            return preset
    return None


def get_window_rect_for_exe(exe_name: str) -> Optional[dict]:
    """
    指定 EXE が持つウィンドウの座標を取得する。
    win32gui が利用できない場合は None を返す。
    """
    if not _WIN32:
        return None

    result: dict = {}

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            h = win32api.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            name = win32process.GetModuleFileNameEx(h, 0)
            win32api.CloseHandle(h)
        except Exception:
            return
        if os.path.basename(name).lower() == exe_name.lower():
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width  = right - left
            height = bottom - top
            if width > 200 and height > 150:
                result["left"]   = left
                result["top"]    = top
                result["width"]  = width
                result["height"] = height

    win32gui.EnumWindows(_cb, None)
    return result if result else None


# ── サービス管理 ──────────────────────────────────────────────────────────────

class ServiceManager:
    """VOICEVOX / Ollama の起動・停止を管理する"""

    # ── 状態確認 ──────────────────────────────────────────────────────────────

    @staticmethod
    def is_voicevox_running() -> bool:
        try:
            r = requests.get(VOICEVOX_URL, timeout=1.5)
            return r.status_code == 200
        except Exception:
            return False

    @staticmethod
    def is_ollama_running() -> bool:
        try:
            r = requests.get(OLLAMA_URL, timeout=1.5)
            return r.status_code == 200
        except Exception:
            return False

    # ── 起動 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def launch_voicevox() -> bool:
        """VOICEVOX を起動し、準備完了まで最大 15s 待つ。成功なら True。"""
        if ServiceManager.is_voicevox_running():
            return True

        exe = next((p for p in VOICEVOX_EXE_CANDIDATES if os.path.isfile(p)), None)
        if exe is None:
            logger.warning("VOICEVOX.exe が見つかりません")
            return False

        subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(15):
            time.sleep(1)
            if ServiceManager.is_voicevox_running():
                return True
        return False

    @staticmethod
    def launch_ollama() -> bool:
        """Ollama を起動し、準備完了まで最大 10s 待つ。成功なら True。"""
        if ServiceManager.is_ollama_running():
            return True

        ollama_exe = shutil.which("ollama")
        if ollama_exe is None:
            logger.warning("ollama コマンドが見つかりません")
            return False

        subprocess.Popen(
            [ollama_exe, "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        for _ in range(10):
            time.sleep(1)
            if ServiceManager.is_ollama_running():
                return True
        return False

    # ── 停止 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def terminate_process_by_name(name: str) -> None:
        if _PSUTIL:
            for proc in psutil.process_iter(["name"]):
                try:
                    if proc.info["name"] and proc.info["name"].lower() == name.lower():
                        proc.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        else:
            try:
                subprocess.run(
                    ["taskkill", "/f", "/im", name],
                    capture_output=True, timeout=5
                )
            except Exception:
                pass

    @classmethod
    def stop_voicevox(cls) -> None:
        cls.terminate_process_by_name("VOICEVOX.exe")

    @classmethod
    def stop_ollama(cls) -> None:
        cls.terminate_process_by_name("ollama.exe")
        cls.terminate_process_by_name("ollama_llama_server.exe")


# ── ROI セレクター ────────────────────────────────────────────────────────────

class ROISelectorWindow(tk.Toplevel):
    """
    スクリーンショット上でドラッグしてROIゾーンを定義するダイアログ。

    zones 属性に {name, region, event_type, method, params, cooldown, enabled}
    のリストが格納される。
    """

    MAX_CANVAS_W = 1200
    MAX_CANVAS_H = 700

    def __init__(self, parent: tk.Misc, screenshot_path: str) -> None:
        super().__init__(parent)
        self.title("EchoMate - UI 位置設定")
        self.resizable(True, True)
        self.grab_set()

        self.zones: list[dict] = []
        self._zone_idx  = 0
        self._scale     = 1.0
        self._drag_x0   = 0
        self._drag_y0   = 0
        self._rect_id   = None
        self._drawn_ids: list[int] = []

        if not _PIL:
            messagebox.showerror(
                "Pillow が必要です",
                "ROI セレクターには Pillow が必要です。\n"
                "pip install Pillow を実行してください。",
                parent=self
            )
            self.destroy()
            return

        # ── 画像読み込み・スケーリング ──────────────────────────────────────
        img = Image.open(screenshot_path)
        orig_w, orig_h = img.size
        scale_w = self.MAX_CANVAS_W / orig_w
        scale_h = self.MAX_CANVAS_H / orig_h
        self._scale = min(scale_w, scale_h, 1.0)
        disp_w = int(orig_w * self._scale)
        disp_h = int(orig_h * self._scale)
        self._photo = ImageTk.PhotoImage(img.resize((disp_w, disp_h), Image.LANCZOS))

        # ── レイアウト ─────────────────────────────────────────────────────
        self._inst_var = tk.StringVar()
        ttk.Label(self, textvariable=self._inst_var, font=("Meiryo", 13, "bold"),
                  wraplength=disp_w, justify=tk.CENTER).pack(pady=(8, 2), padx=8)

        self._canvas = tk.Canvas(self, width=disp_w, height=disp_h, cursor="crosshair")
        self._canvas.pack(padx=8, pady=4)
        self._canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=8, pady=(4, 8))
        self._btn_skip  = ttk.Button(btn_frame, text="このゾーンをスキップ", command=self._skip_zone)
        self._btn_skip.pack(side=tk.LEFT, padx=4)
        self._btn_undo  = ttk.Button(btn_frame, text="やり直し", command=self._undo_zone)
        self._btn_undo.pack(side=tk.LEFT, padx=4)
        self._btn_done  = ttk.Button(btn_frame, text="完了", command=self._finish,
                                     state=tk.DISABLED)
        self._btn_done.pack(side=tk.RIGHT, padx=4)

        self._canvas.bind("<ButtonPress-1>",   self._drag_start)
        self._canvas.bind("<B1-Motion>",       self._drag_move)
        self._canvas.bind("<ButtonRelease-1>", self._drag_end)

        self._prompt_zone()

    # ── ゾーン進行 ────────────────────────────────────────────────────────────

    def _prompt_zone(self) -> None:
        if self._zone_idx >= len(ZONE_DEFS):
            self._inst_var.set("全ゾーンの設定が完了しました。「完了」を押してください。")
            self._btn_done.config(state=tk.NORMAL)
            self._btn_skip.config(state=tk.DISABLED)
            return

        _, _, instruction, *_ = ZONE_DEFS[self._zone_idx]
        color = ZONE_COLORS[self._zone_idx % len(ZONE_COLORS)]
        self._inst_var.set(
            f"[{self._zone_idx + 1}/{len(ZONE_DEFS)}]  {instruction}\n"
            "ドラッグで範囲を囲んでください"
        )
        self._canvas.config(cursor="crosshair")
        self._btn_undo.config(state=tk.DISABLED)

    def _skip_zone(self) -> None:
        self._zone_idx += 1
        self._prompt_zone()

    def _undo_zone(self) -> None:
        if self._drawn_ids:
            self._canvas.delete(self._drawn_ids.pop())
        if self.zones:
            self.zones.pop()
        self._zone_idx = max(0, self._zone_idx - 1)
        self._prompt_zone()

    def _finish(self) -> None:
        self.destroy()

    # ── ドラッグ ──────────────────────────────────────────────────────────────

    def _drag_start(self, e: tk.Event) -> None:
        if self._zone_idx >= len(ZONE_DEFS):
            return
        self._drag_x0 = e.x
        self._drag_y0 = e.y
        if self._rect_id:
            self._canvas.delete(self._rect_id)
            self._rect_id = None

    def _drag_move(self, e: tk.Event) -> None:
        if self._zone_idx >= len(ZONE_DEFS):
            return
        if self._rect_id:
            self._canvas.delete(self._rect_id)
        color = ZONE_COLORS[self._zone_idx % len(ZONE_COLORS)]
        self._rect_id = self._canvas.create_rectangle(
            self._drag_x0, self._drag_y0, e.x, e.y,
            outline=color, width=2, dash=(4, 2)
        )

    def _drag_end(self, e: tk.Event) -> None:
        if self._zone_idx >= len(ZONE_DEFS):
            return

        x0, y0 = min(self._drag_x0, e.x), min(self._drag_y0, e.y)
        x1, y1 = max(self._drag_x0, e.x), max(self._drag_y0, e.y)
        w = x1 - x0
        h = y1 - y0

        if w < 5 or h < 5:
            if self._rect_id:
                self._canvas.delete(self._rect_id)
                self._rect_id = None
            return

        color = ZONE_COLORS[self._zone_idx % len(ZONE_COLORS)]
        name, event_type, _, method, params, cooldown = ZONE_DEFS[self._zone_idx]
        s = self._scale
        region = {
            "left":   int(x0 / s),
            "top":    int(y0 / s),
            "width":  int(w / s),
            "height": int(h / s),
        }

        # ラベル追加
        label_id = self._canvas.create_text(
            x0 + 4, y0 + 4,
            text=name,
            anchor=tk.NW,
            fill=color,
            font=("Meiryo", 10, "bold"),
        )
        self._drawn_ids.append(self._rect_id)
        self._drawn_ids.append(label_id)
        self._rect_id = None

        self.zones.append({
            "name":       name,
            "region":     region,
            "event_type": event_type,
            "method":     method,
            "params":     params,
            "cooldown":   cooldown,
            "enabled":    True,
        })

        self._zone_idx += 1
        self._btn_undo.config(state=tk.NORMAL)
        self._prompt_zone()


# ── ゲーム設定タブ ────────────────────────────────────────────────────────────

class GameSetupFrame(ttk.Frame):
    """ゲーム認識・cv_config 生成を行うタブ"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._screenshot_path: Optional[str] = None
        self._build()

    def _build(self) -> None:
        # ── プロセスリスト ────────────────────────────────────────────────
        list_frame = ttk.LabelFrame(self, text="実行中のプロセス")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 4))

        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self._proc_list = tk.Listbox(
            list_frame,
            yscrollcommand=scroll.set,
            height=8,
            selectmode=tk.SINGLE,
            font=("Consolas", 10),
            exportselection=False,
        )
        scroll.config(command=self._proc_list.yview)
        self._proc_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0), pady=4)
        scroll.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 4), pady=4)

        btn_row = ttk.Frame(list_frame)
        btn_row.pack(fill=tk.X, padx=4, pady=(0, 4))
        ttk.Button(btn_row, text="一覧を更新", command=self._refresh_procs).pack(side=tk.LEFT)
        self._proc_list.bind("<<ListboxSelect>>", self._on_proc_select)

        # ── プリセット ────────────────────────────────────────────────────
        preset_frame = ttk.LabelFrame(self, text="プリセット")
        preset_frame.pack(fill=tk.X, padx=8, pady=4)

        self._preset_var = tk.StringVar(value="（未検出）")
        ttk.Label(preset_frame, text="検出:").grid(row=0, column=0, padx=6, pady=4, sticky=tk.W)
        ttk.Label(preset_frame, textvariable=self._preset_var,
                  font=("Meiryo", 10, "bold")).grid(row=0, column=1, padx=4, pady=4, sticky=tk.W)
        self._btn_preset = ttk.Button(preset_frame, text="プリセットを適用",
                                      command=self._apply_preset, state=tk.DISABLED)
        self._btn_preset.grid(row=0, column=2, padx=6, pady=4)

        # ── カスタム設定 ──────────────────────────────────────────────────
        custom_frame = ttk.LabelFrame(self, text="カスタム設定（プリセットなし）")
        custom_frame.pack(fill=tk.X, padx=8, pady=4)

        self._btn_capture = ttk.Button(custom_frame, text="ゲーム画面をキャプチャ",
                                       command=self._capture_screenshot, state=tk.DISABLED)
        self._btn_capture.grid(row=0, column=0, padx=6, pady=4)
        ttk.Label(custom_frame, text="または").grid(row=0, column=1, padx=4)
        ttk.Button(custom_frame, text="スクショを参照...",
                   command=self._browse_screenshot).grid(row=0, column=2, padx=6, pady=4)

        self._ss_label = ttk.Label(custom_frame, text="（未選択）", foreground="gray")
        self._ss_label.grid(row=1, column=0, columnspan=3, padx=6, pady=2, sticky=tk.W)

        self._btn_roi = ttk.Button(custom_frame, text="UI 位置を設定（ROI 選択）",
                                   command=self._open_roi, state=tk.DISABLED)
        self._btn_roi.grid(row=2, column=0, columnspan=3, padx=6, pady=(2, 6))

        # ── ステータス ────────────────────────────────────────────────────
        self._status_var = tk.StringVar(value="プロセスを選択してください")
        ttk.Label(self, textvariable=self._status_var,
                  foreground="gray").pack(anchor=tk.W, padx=10, pady=(0, 4))

        self._refresh_procs()

    # ── プロセスリスト ────────────────────────────────────────────────────────

    def _refresh_procs(self) -> None:
        self._proc_list.delete(0, tk.END)
        for name in list_running_exes():
            self._proc_list.insert(tk.END, name)
        self._status_var.set(f"{self._proc_list.size()} 件のプロセスが見つかりました")

    def _on_proc_select(self, _event=None) -> None:
        sel = self._proc_list.curselection()
        if not sel:
            return
        exe = self._proc_list.get(sel[0])
        preset = detect_preset(exe)
        if preset:
            self._preset_var.set(preset)
            self._btn_preset.config(state=tk.NORMAL)
            self._btn_capture.config(state=tk.DISABLED)
        else:
            self._preset_var.set("（なし）")
            self._btn_preset.config(state=tk.DISABLED)
            can_capture = _WIN32 and _MSS
            self._btn_capture.config(state=tk.NORMAL if can_capture else tk.DISABLED)

        self._status_var.set(f"選択: {exe}")

    # ── プリセット適用 ────────────────────────────────────────────────────────

    def _apply_preset(self) -> None:
        preset = self._preset_var.get()
        if preset in ("（未検出）", "（なし）"):
            return
        src = os.path.join(PRESETS_DIR, f"{preset}.json")
        if not os.path.isfile(src):
            messagebox.showerror("エラー", f"プリセットファイルが見つかりません:\n{src}")
            return
        shutil.copy(src, "cv_config.json")
        self._status_var.set(f"プリセット '{preset}' を適用しました")
        messagebox.showinfo("適用完了",
                            f"プリセット '{preset}' を cv_config.json に適用しました。\n"
                            "EchoMate を（再）起動すると有効になります。")

    # ── スクリーンショット ────────────────────────────────────────────────────

    def _capture_screenshot(self) -> None:
        sel = self._proc_list.curselection()
        if not sel:
            return
        exe = self._proc_list.get(sel[0])
        rect = get_window_rect_for_exe(exe)
        if not rect:
            messagebox.showwarning(
                "ウィンドウ未検出",
                f"{exe} のウィンドウが見つかりません。\n"
                "ゲームを起動した状態で再試行するか、\n"
                "「スクショを参照」でファイルを指定してください。"
            )
            return

        save_path = filedialog.asksaveasfilename(
            title="スクリーンショットの保存先",
            defaultextension=".png",
            filetypes=[("PNG 画像", "*.png")],
            initialfile="game_screenshot.png",
        )
        if not save_path:
            return

        try:
            with mss.mss() as sct:
                region = {
                    "left":   rect["left"],
                    "top":    rect["top"],
                    "width":  rect["width"],
                    "height": rect["height"],
                }
                raw = sct.grab(region)
                mss.tools.to_png(raw.rgb, raw.size, output=save_path)
        except Exception as e:
            messagebox.showerror("キャプチャ失敗", str(e))
            return

        self._set_screenshot(save_path)

    def _browse_screenshot(self) -> None:
        path = filedialog.askopenfilename(
            title="スクリーンショットを選択",
            filetypes=[("画像ファイル", "*.png *.jpg *.jpeg *.bmp"), ("すべて", "*.*")],
        )
        if path:
            self._set_screenshot(path)

    def _set_screenshot(self, path: str) -> None:
        self._screenshot_path = path
        self._ss_label.config(text=os.path.basename(path), foreground="black")
        self._btn_roi.config(state=tk.NORMAL)
        self._status_var.set(f"スクリーンショット: {os.path.basename(path)}")

    # ── ROI セレクター ────────────────────────────────────────────────────────

    def _open_roi(self) -> None:
        if not self._screenshot_path:
            return
        if not _PIL:
            messagebox.showerror(
                "Pillow が必要です",
                "ROI セレクターには Pillow が必要です。\n"
                "pip install Pillow を実行してください。"
            )
            return

        dialog = ROISelectorWindow(self, self._screenshot_path)
        self.wait_window(dialog)

        if not dialog.zones:
            self._status_var.set("ROI が設定されませんでした")
            return

        from setup_wizard import default_audio_rules
        config = {
            "_generated_by": "EchoMate GUI ROI selector",
            "zones":         dialog.zones,
            "audio_rules":   default_audio_rules(),
        }
        with open("cv_config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        self._status_var.set(f"{len(dialog.zones)} ゾーンを cv_config.json に保存しました")
        messagebox.showinfo("保存完了",
                            f"{len(dialog.zones)} ゾーンの設定を cv_config.json に保存しました。\n"
                            "EchoMate を（再）起動すると有効になります。")


# ── 相棒設定タブ ──────────────────────────────────────────────────────────────

class CharacterFrame(ttk.Frame):
    """相棒の人格を選択するタブ"""

    def __init__(self, master: tk.Misc, on_change) -> None:
        super().__init__(master)
        self._on_change = on_change  # callback(character_key: str)
        self._chars: dict = {}
        self._selected = tk.StringVar()
        self._build()

    def _build(self) -> None:
        try:
            with open("characters.json", encoding="utf-8") as f:
                raw = json.load(f)
            self._chars = {k: v for k, v in raw.items() if not k.startswith("_")}
        except Exception:
            self._chars = {}

        ttk.Label(self, text="相棒を選択してください", font=("Meiryo", 11)).pack(pady=(10, 4))

        card_frame = ttk.Frame(self)
        card_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        for i, (key, char) in enumerate(self._chars.items()):
            rb = ttk.Radiobutton(
                card_frame,
                text=f"  {char.get('name', key)}",
                variable=self._selected,
                value=key,
                command=self._on_radio,
            )
            rb.grid(row=i, column=0, sticky=tk.W, padx=8, pady=2)

            speaker = char.get("voicevox", {}).get("speaker", "")
            traits  = char.get("traits", {})
            desc    = f"  {speaker}"
            if traits:
                trait_str = " / ".join(
                    f"{k}:{v:.0%}" for k, v in traits.items() if isinstance(v, float)
                )
                desc += f"  [{trait_str}]"
            ttk.Label(card_frame, text=desc, foreground="gray",
                      font=("Meiryo", 9)).grid(row=i, column=1, sticky=tk.W, padx=4)

        # デフォルト選択
        if "kid" in self._chars:
            self._selected.set("kid")
        elif self._chars:
            self._selected.set(next(iter(self._chars)))

        # プレビュー欄
        preview_frame = ttk.LabelFrame(self, text="キャラクター説明")
        preview_frame.pack(fill=tk.X, padx=12, pady=8)
        self._preview_var = tk.StringVar()
        ttk.Label(preview_frame, textvariable=self._preview_var,
                  wraplength=380, justify=tk.LEFT,
                  font=("Meiryo", 10)).pack(padx=8, pady=6)

        ttk.Button(self, text="適用", command=self._apply).pack(pady=(0, 8))
        self._update_preview()

    def _on_radio(self) -> None:
        self._update_preview()

    def _update_preview(self) -> None:
        key = self._selected.get()
        char = self._chars.get(key, {})
        prompt = char.get("system_prompt", "（説明なし）")
        self._preview_var.set(prompt)

    def _apply(self) -> None:
        key = self._selected.get()
        if key and self._on_change:
            self._on_change(key)

    def get_selected(self) -> str:
        return self._selected.get()


# ── 吹き出し設定タブ ──────────────────────────────────────────────────────────

class BubbleFrame(ttk.Frame):
    """吹き出し UI の表示制御タブ"""

    def __init__(self, master: tk.Misc, get_bubble) -> None:
        super().__init__(master)
        self._get_bubble = get_bubble  # callable -> Optional[SpeechBubble]
        self._build()

    def _build(self) -> None:
        ttk.Label(self, text="相棒の吹き出し UI", font=("Meiryo", 11)).pack(pady=(12, 6))

        info = (
            "グリーンバック背景の吹き出しウィンドウです。\n"
            "OBS の「ウィンドウキャプチャ」＋クロマキーフィルターで\n"
            "ゲーム映像に透過合成できます。"
        )
        ttk.Label(self, text=info, justify=tk.LEFT,
                  foreground="gray").pack(padx=12, anchor=tk.W)

        ctrl = ttk.Frame(self)
        ctrl.pack(pady=12)
        ttk.Button(ctrl, text="吹き出しを表示", command=self._show_bubble).pack(side=tk.LEFT, padx=6)
        ttk.Button(ctrl, text="吹き出しを隠す",  command=self._hide_bubble).pack(side=tk.LEFT, padx=6)

        # テスト送信
        test_frame = ttk.LabelFrame(self, text="テストメッセージ")
        test_frame.pack(fill=tk.X, padx=12, pady=4)
        self._test_var = tk.StringVar(value="やったじゃん！")
        ttk.Entry(test_frame, textvariable=self._test_var, width=30).pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(test_frame, text="送信", command=self._send_test).pack(side=tk.LEFT, padx=4)

    def _show_bubble(self) -> None:
        bubble = self._get_bubble()
        if bubble:
            bubble.show()

    def _hide_bubble(self) -> None:
        bubble = self._get_bubble()
        if bubble:
            bubble.hide()

    def _send_test(self) -> None:
        bubble = self._get_bubble()
        if bubble:
            bubble.update_text(self._test_var.get())
            bubble.show()


# ── メインウィンドウ ──────────────────────────────────────────────────────────

class EchoMateGUI:
    """EchoMate GUIコントロールパネル本体"""

    def __init__(self) -> None:
        _main_module._setup_logging(logging.INFO)

        self.root = tk.Tk()
        self.root.title("EchoMate Control Panel")
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._echo_mate: Optional[EchoMate] = None
        self._bubble:    Optional[SpeechBubble] = None
        self._running    = False

        self._build_ui()
        self._check_services_async()

    # ── UI 構築 ───────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── ヘッダー: サービスステータス ─────────────────────────────────
        header = ttk.Frame(self.root, relief=tk.GROOVE, borderwidth=1)
        header.pack(fill=tk.X, padx=8, pady=(6, 0))

        ttk.Label(header, text="EchoMate", font=("Meiryo", 14, "bold")).pack(side=tk.LEFT, padx=8, pady=4)

        self._vv_var  = tk.StringVar(value="VOICEVOX: 確認中...")
        self._oll_var = tk.StringVar(value="Ollama: 確認中...")
        ttk.Label(header, textvariable=self._vv_var,  font=("Consolas", 9)).pack(side=tk.LEFT, padx=8)
        ttk.Label(header, textvariable=self._oll_var, font=("Consolas", 9)).pack(side=tk.LEFT, padx=8)

        # ── タブ ─────────────────────────────────────────────────────────
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        self._game_tab = GameSetupFrame(notebook)
        notebook.add(self._game_tab, text="  ゲーム設定  ")

        self._char_tab = CharacterFrame(notebook, on_change=self._on_character_change)
        notebook.add(self._char_tab, text="  相棒設定  ")

        self._bubble_tab = BubbleFrame(notebook, get_bubble=lambda: self._bubble)
        notebook.add(self._bubble_tab, text="  吹き出し  ")

        # ── フッター: 起動/停止 ───────────────────────────────────────────
        footer = ttk.Frame(self.root)
        footer.pack(fill=tk.X, padx=8, pady=(0, 8))

        self._btn_start = ttk.Button(footer, text="▶  EchoMate 起動",
                                     command=self._start_echomate, width=18)
        self._btn_start.pack(side=tk.LEFT, padx=4)

        self._btn_stop  = ttk.Button(footer, text="■  停止",
                                     command=self._stop_echomate, width=10, state=tk.DISABLED)
        self._btn_stop.pack(side=tk.LEFT, padx=4)

        self._status_var = tk.StringVar(value="停止中")
        ttk.Label(footer, textvariable=self._status_var,
                  font=("Meiryo", 10)).pack(side=tk.LEFT, padx=12)

        self.root.minsize(480, 480)

    # ── サービスステータス確認 ────────────────────────────────────────────────

    def _check_services_async(self) -> None:
        def _check():
            vv  = ServiceManager.is_voicevox_running()
            oll = ServiceManager.is_ollama_running()
            self.root.after(0, self._update_service_labels, vv, oll)

        threading.Thread(target=_check, daemon=True, name="ServiceCheck").start()

    def _update_service_labels(self, vv: bool, oll: bool) -> None:
        self._vv_var.set(f"VOICEVOX: {'✔ 起動中' if vv else '✖ 停止中'}")
        self._oll_var.set(f"Ollama: {'✔ 起動中' if oll else '✖ 停止中'}")

    # ── EchoMate 起動/停止 ────────────────────────────────────────────────────

    def _start_echomate(self) -> None:
        if self._running:
            return

        self._btn_start.config(state=tk.DISABLED)
        self._status_var.set("起動中...")

        def _launch():
            # 1. VOICEVOX 起動
            self.root.after(0, self._status_var.set, "VOICEVOX を起動中...")
            vv_ok = ServiceManager.launch_voicevox()

            # 2. Ollama 確認
            self.root.after(0, self._status_var.set, "Ollama を確認中...")
            oll_ok = ServiceManager.is_ollama_running()
            if not oll_ok:
                oll_ok = ServiceManager.launch_ollama()

            self.root.after(0, self._update_service_labels, vv_ok, oll_ok)

            if not oll_ok:
                self.root.after(0, self._on_start_failed,
                                "Ollama が起動できませんでした。\n"
                                "タスクトレイの Ollama アイコンを確認してください。")
                return

            # 3. EchoMate 起動
            self.root.after(0, self._status_var.set, "EchoMate を起動中...")
            try:
                char_key = self._char_tab.get_selected()
                self._echo_mate = EchoMate(
                    character=char_key,
                    enable_cv=True,
                    enable_audio=True,
                    enable_dummy=False,
                    speech_callback=self._on_speech,
                )
                self._echo_mate.start_background()
            except Exception as exc:
                self.root.after(0, self._on_start_failed, f"EchoMate 起動エラー:\n{exc}")
                return

            # 4. 吹き出し作成
            self.root.after(0, self._create_bubble)
            self.root.after(0, self._on_start_success)

        threading.Thread(target=_launch, daemon=True, name="Launch").start()

    def _on_start_success(self) -> None:
        self._running = True
        self._btn_start.config(state=tk.DISABLED)
        self._btn_stop.config(state=tk.NORMAL)
        self._status_var.set("動作中")

    def _on_start_failed(self, message: str) -> None:
        self._btn_start.config(state=tk.NORMAL)
        self._status_var.set("起動失敗")
        messagebox.showerror("起動エラー", message)

    def _stop_echomate(self) -> None:
        if not self._running:
            return
        self._status_var.set("停止中...")
        self._btn_stop.config(state=tk.DISABLED)

        def _shutdown():
            if self._echo_mate:
                self._echo_mate.stop()
                self._echo_mate = None
            self.root.after(0, self._on_stop_done)

        threading.Thread(target=_shutdown, daemon=True, name="Shutdown").start()

    def _on_stop_done(self) -> None:
        self._running = False
        self._btn_start.config(state=tk.NORMAL)
        self._status_var.set("停止中")

    # ── 吹き出し ──────────────────────────────────────────────────────────────

    def _create_bubble(self) -> None:
        if self._bubble:
            self._bubble.destroy()
        self._bubble = SpeechBubble(master=self.root)
        self._bubble.show()

    def _on_speech(self, text: str) -> None:
        """EchoMate の発話コールバック（ワーカースレッドから呼ばれる）"""
        if self._bubble:
            self._bubble.update_text(text)

    # ── キャラクター変更 ──────────────────────────────────────────────────────

    def _on_character_change(self, key: str) -> None:
        if self._echo_mate and self._running:
            try:
                self._echo_mate._apply_character(key)
                self._status_var.set(f"相棒を変更しました: {key}")
            except Exception as e:
                messagebox.showerror("変更失敗", str(e))
        else:
            self._status_var.set(f"起動時のキャラクター: {key}")

    # ── 終了処理 ──────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno(
                "終了確認",
                "EchoMate は動作中です。\n"
                "終了すると VOICEVOX・Ollama も停止されます。\nよろしいですか？"
            ):
                return

        self._status_var.set("終了中...")

        def _shutdown():
            if self._echo_mate:
                try:
                    self._echo_mate.stop()
                except Exception:
                    pass

            ServiceManager.stop_voicevox()
            ServiceManager.stop_ollama()

            if self._bubble:
                try:
                    self._bubble.destroy()
                except Exception:
                    pass

            self.root.after(0, self.root.destroy)

        threading.Thread(target=_shutdown, daemon=True, name="AppShutdown").start()

    # ── メインループ ──────────────────────────────────────────────────────────

    def run(self) -> None:
        self.root.mainloop()


# ── エントリーポイント ────────────────────────────────────────────────────────

def main() -> None:
    app = EchoMateGUI()
    app.run()


if __name__ == "__main__":
    main()
