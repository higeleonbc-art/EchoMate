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
from tkinter import ttk, filedialog, messagebox, simpledialog
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

try:
    import pyaudiowpatch as _pyaudio  # WASAPI ループバック対応版を優先
    import numpy as _np
    _PYAUDIO_AVAILABLE = True
    _WPATCH_AVAILABLE  = True
except ImportError:
    _WPATCH_AVAILABLE = False
    try:
        import pyaudio as _pyaudio
        import numpy as _np
        _PYAUDIO_AVAILABLE = True
    except ImportError:
        _PYAUDIO_AVAILABLE = False

# PyAudioが実際に初期化できるか確認（PortAudioの assert crash を防ぐ）
if _PYAUDIO_AVAILABLE:
    import subprocess as _subprocess
    _test = _subprocess.run(
        [sys.executable, "-c", "import pyaudiowpatch as p; p.PyAudio().terminate()" if _WPATCH_AVAILABLE
         else "import pyaudio as p; p.PyAudio().terminate()"],
        capture_output=True, timeout=10
    )
    if _test.returncode != 0:
        _PYAUDIO_AVAILABLE = False

import httpx

from bubble import SpeechBubble, AvatarWindow
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
    # (name, event_type, instruction, method, params, cooldown, min_hits)
    ("hp_bar_low",   "low_hp",   "HP バーを選択（左下付近）",          "color_threshold",
     {"color": "red",  "threshold": 0.40}, 5.0, 2),
    ("kill_feed",    "kill",     "キルフィードを選択（右上付近）",      "color_threshold",
     {"color": "yellow", "threshold": 0.12}, 3.0, 2),  # 瞬間ノイズを2フレーム連続で確認
    ("death_screen", "death",    "デス暗転エリアを選択（中央部分）",    "brightness",
     {"mode": "drop", "threshold": 40}, 8.0, 1),
    ("screen_flash", "big_play", "エフェクトエリアを選択（中央付近）",  "frame_diff",
     {"threshold": 25, "changed_ratio": 0.15}, 4.0, 1),
]

ZONE_COLORS = ["#FF4444", "#FFDD00", "#AA44FF", "#44AAFF"]

logger = logging.getLogger(__name__)

# PortAudio の Pa_Initialize() はスレッドアンセーフなため同時呼び出しを防ぐ
_pyaudio_lock = threading.Lock()


# ── オーディオデバイス列挙 ────────────────────────────────────────────────────

def list_audio_input_devices() -> list[tuple[int, str]]:
    """通常の入力チャンネルを持つオーディオデバイスの (index, name) リストを返す"""
    if not _PYAUDIO_AVAILABLE:
        return []
    try:
        with _pyaudio_lock:
            p = _pyaudio.PyAudio()
        devices: list[tuple[int, str]] = []
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            # ループバックデバイスは除外（別関数で列挙）
            if info.get("maxInputChannels", 0) > 0 and not info.get("isLoopbackDevice", False):
                devices.append((i, info.get("name", f"Device {i}")))
        p.terminate()
        return devices
    except Exception:
        return []


def list_loopback_devices() -> list[tuple[int, str]]:
    """
    WASAPIループバックデバイスの (index, name) リストを返す。
    pyaudiowpatch が必要。未インストールの場合は空リストを返す。
    """
    if not _PYAUDIO_AVAILABLE or not _WPATCH_AVAILABLE:
        return []
    try:
        with _pyaudio_lock:
            p = _pyaudio.PyAudio()
        devices: list[tuple[int, str]] = []
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get("isLoopbackDevice", False):
                devices.append((i, info.get("name", f"Loopback {i}")))
        p.terminate()
        return devices
    except Exception:
        return []


# ── マイク常時モニター ────────────────────────────────────────────────────────

class MicMonitor:
    """
    選択されたデバイスからマイク入力レベルを常時監視する軽量クラス。
    GUIのレベルインジケーターに current_rms を公開する。
    """

    _CHUNK        = 512
    _FALLBACK_RATE = 44100

    def __init__(self) -> None:
        self.current_rms: float = 0.0
        self.device_index: int | None = None
        self._gen = 0   # スレッド世代番号（デバイス変更時にインクリメント）

    def start(self) -> None:
        """現在の device_index でモニタリングを開始する"""
        if not _PYAUDIO_AVAILABLE or self.device_index is None:
            return
        self._gen += 1
        t = threading.Thread(
            target=self._loop,
            args=(self._gen,),
            daemon=True,
            name="MicMonitor",
        )
        t.start()

    def set_device(self, device_index: int | None) -> None:
        """デバイスを切り替えてモニタリングを再起動する"""
        self.device_index = device_index
        self.current_rms = 0.0
        self.start()

    def stop(self) -> None:
        """モニタリングを停止する（世代番号を変えてスレッドを終了させる）"""
        self._gen += 1
        self.current_rms = 0.0

    def _loop(self, gen: int) -> None:
        with _pyaudio_lock:
            p = _pyaudio.PyAudio()
        stream = None
        try:
            # デバイスのネイティブサンプルレートを使用（ループバック互換性のため）
            rate = self._FALLBACK_RATE
            if self.device_index is not None:
                try:
                    info = p.get_device_info_by_index(self.device_index)
                    rate = int(info.get("defaultSampleRate", self._FALLBACK_RATE))
                except Exception:
                    pass

            stream = p.open(
                format=_pyaudio.paFloat32,
                channels=1,
                rate=rate,
                input=True,
                frames_per_buffer=self._CHUNK,
                input_device_index=self.device_index,
            )
            while self._gen == gen:
                try:
                    raw = stream.read(self._CHUNK, exception_on_overflow=False)
                    samples = _np.frombuffer(raw, dtype=_np.float32)
                    self.current_rms = float(_np.sqrt(_np.mean(samples ** 2)))
                except OSError:
                    time.sleep(0.05)
        except Exception as e:
            logger.debug("MicMonitor error: %s", e)
        finally:
            self.current_rms = 0.0
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            p.terminate()




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
            r = httpx.get(VOICEVOX_URL, timeout=1.5)
            return r.status_code == 200
        except Exception:
            return False

    @staticmethod
    def is_ollama_running() -> bool:
        try:
            r = httpx.get(OLLAMA_URL, timeout=1.5)
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

    @staticmethod
    def get_voicevox_pids() -> list[int]:
        """実行中の VOICEVOX エンジン関連プロセスの PID リストを返す。"""
        targets = {"voicevox.exe", "run.exe"}  # VOICEVOX 本体 + 内蔵エンジン
        pids: list[int] = []
        if not _PSUTIL:
            return pids
        try:
            for proc in psutil.process_iter(["name", "pid"]):
                name = (proc.info["name"] or "").lower()
                if name in targets:
                    pids.append(proc.info["pid"])
        except Exception:
            pass
        return pids

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
        name, event_type, _, method, params, cooldown, min_hits = ZONE_DEFS[self._zone_idx]
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
            "min_hits":   min_hits,
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
        self._exe_path: Optional[str] = None
        self._build()

    def _build(self) -> None:
        # ── EXE 参照 ──────────────────────────────────────────────────────
        exe_frame = ttk.LabelFrame(self, text="ゲーム EXE")
        exe_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        exe_row = ttk.Frame(exe_frame)
        exe_row.pack(fill=tk.X, padx=4, pady=6)
        self._exe_var = tk.StringVar(value="（未選択）")
        ttk.Entry(exe_row, textvariable=self._exe_var, state="readonly",
                  width=36, font=("Consolas", 9)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(exe_row, text="参照...", command=self._browse_exe).pack(side=tk.LEFT, padx=(4, 0))

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

        # ── Vision モニター選択 ───────────────────────────────────────────
        monitor_frame = ttk.LabelFrame(self, text="Vision 解析モニター")
        monitor_frame.pack(fill=tk.X, padx=8, pady=4)

        mon_row = ttk.Frame(monitor_frame)
        mon_row.pack(fill=tk.X, padx=6, pady=6)
        ttk.Label(mon_row, text="キャプチャ対象:").pack(side=tk.LEFT)
        self._monitor_var = tk.StringVar()
        self._monitor_combo = ttk.Combobox(
            mon_row, textvariable=self._monitor_var,
            state="readonly", width=28, font=("Meiryo", 9),
        )
        self._monitor_combo.pack(side=tk.LEFT, padx=6)
        ttk.Button(mon_row, text="更新",
                   command=self._refresh_monitors).pack(side=tk.LEFT)
        self._refresh_monitors()

        # ── ステータス ────────────────────────────────────────────────────
        self._status_var = tk.StringVar(value="EXE を選択してください")
        ttk.Label(self, textvariable=self._status_var,
                  foreground="gray").pack(anchor=tk.W, padx=10, pady=(0, 4))

    # ── モニターリスト ────────────────────────────────────────────────────────

    def _refresh_monitors(self) -> None:
        """利用可能なモニターを列挙してコンボボックスを更新する"""
        if not _MSS:
            self._monitor_combo["values"] = ["（mss 未インストール）"]
            self._monitor_combo.current(0)
            return
        try:
            from vision_analyzer import VisionAnalyzer
            monitors = VisionAnalyzer.list_monitors()
            if monitors:
                labels = [
                    f"モニター {m['index']}  ({m['width']}x{m['height']})"
                    for m in monitors
                ]
                self._monitor_combo["values"] = labels
                self._monitor_combo.current(0)
            else:
                self._monitor_combo["values"] = ["（検出なし）"]
                self._monitor_combo.current(0)
        except Exception as e:
            self._monitor_combo["values"] = [f"（エラー: {e})"]
            self._monitor_combo.current(0)

    def get_monitor_index(self) -> int:
        """選択中のモニターインデックスを返す（mss 番号: 1=プライマリ）"""
        sel = self._monitor_combo.current()
        return max(1, sel + 1)  # combobox は 0-origin なので +1

    def get_selected_exe(self) -> Optional[str]:
        """選択中の EXE のファイル名（basename）を返す。未選択時は None。"""
        if not self._exe_path:
            return None
        return os.path.basename(self._exe_path)

    # ── EXE 参照 ──────────────────────────────────────────────────────────────

    def _browse_exe(self) -> None:
        path = filedialog.askopenfilename(
            title="ゲーム EXE を選択",
            filetypes=[("EXE ファイル", "*.exe"), ("すべてのファイル", "*.*")],
        )
        if not path:
            return
        self._exe_path = path
        exe_name = os.path.basename(path)
        self._exe_var.set(exe_name)
        self._status_var.set(f"選択: {exe_name}")

        preset = detect_preset(exe_name)
        if preset:
            self._preset_var.set(preset)
            self._btn_preset.config(state=tk.NORMAL)
            self._btn_capture.config(state=tk.DISABLED)
        else:
            self._preset_var.set("（なし）")
            self._btn_preset.config(state=tk.DISABLED)
            can_capture = _WIN32 and _MSS
            self._btn_capture.config(state=tk.NORMAL if can_capture else tk.DISABLED)

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
        exe = self.get_selected_exe()
        if not exe:
            return
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


# ── マイク設定タブ ────────────────────────────────────────────────────────────

class MicSetupFrame(ttk.Frame):
    """
    マイク設定タブ。

    用途別にデバイスを分けて管理する:
      - 音声認識デバイス (VoiceInput)  : 常にマイク入力 → レベルバーで確認
      - ゲームイベント検知デバイス (AudioDetector) : マイクまたはWASAPIループバック
    """

    _RMS_SCALE = 800  # RMS → プログレスバー変換係数

    def __init__(self, master: tk.Misc, mic_monitor: MicMonitor,
                 audio_monitor: MicMonitor) -> None:
        super().__init__(master)
        self._monitor = mic_monitor          # 音声認識デバイスのレベル監視
        self._audio_monitor = audio_monitor  # ゲーム音（ループバック）のレベル監視
        self._mic_devices:  list[tuple[int, str]] = []
        self._detect_devices: list[tuple[int, str]] = []
        self._audio_peak_rms:   float = 0.0
        self._audio_peak_decay: int   = 0
        self._build()
        self._poll_level()

    def _build(self) -> None:
        # ════════════════════════════════════════════════════════════════
        # 1. 音声認識デバイス（VoiceInput 専用・常にマイク）
        # ════════════════════════════════════════════════════════════════
        mic_frame = ttk.LabelFrame(self, text="音声認識デバイス（マイク）")
        mic_frame.pack(fill=tk.X, padx=8, pady=(10, 4))

        mic_row = ttk.Frame(mic_frame)
        mic_row.pack(fill=tk.X, padx=6, pady=(6, 2))
        self._mic_var   = tk.StringVar()
        self._mic_combo = ttk.Combobox(
            mic_row, textvariable=self._mic_var,
            state="readonly", width=44, font=("Meiryo", 9),
        )
        self._mic_combo.pack(side=tk.LEFT)
        ttk.Button(mic_row, text="更新",
                   command=self._refresh_mic_devices).pack(side=tk.LEFT, padx=4)

        # レベルバー（音声認識デバイスのレベルを表示）
        self._canvas = tk.Canvas(mic_frame, height=22, bg="#1e1e1e",
                                  highlightthickness=0)
        self._canvas.pack(fill=tk.X, padx=6, pady=(4, 2))
        self._bar_id  = self._canvas.create_rectangle(0, 0, 0, 22, fill="#44dd44", outline="")
        self._peak_id = self._canvas.create_rectangle(0, 0, 0, 22, fill="#ffffff", outline="")
        self._peak_rms:   float = 0.0
        self._peak_decay: int   = 0

        mic_info = ttk.Frame(mic_frame)
        mic_info.pack(fill=tk.X, padx=6, pady=(0, 6))
        self._rms_var = tk.StringVar(value="RMS: 0.0000")
        ttk.Label(mic_info, textvariable=self._rms_var,
                  font=("Consolas", 8), foreground="gray").pack(side=tk.LEFT)
        self._mic_status_var = tk.StringVar(value="")
        ttk.Label(mic_info, textvariable=self._mic_status_var,
                  font=("Meiryo", 8), foreground="gray").pack(side=tk.RIGHT)

        # ════════════════════════════════════════════════════════════════
        # 2. ゲームイベント検知デバイス（AudioDetector 専用）
        # ════════════════════════════════════════════════════════════════
        detect_frame = ttk.LabelFrame(self, text="ゲームイベント検知デバイス（WASAPIループバック）")
        detect_frame.pack(fill=tk.X, padx=8, pady=4)

        if not _WPATCH_AVAILABLE:
            ttk.Label(
                detect_frame, text="※ pip install pyaudiowpatch が必要",
                foreground="#cc6600", font=("Meiryo", 8),
            ).pack(anchor=tk.W, padx=8, pady=(4, 0))

        # ループバックデバイス選択
        self._detect_row = ttk.Frame(detect_frame)
        self._detect_row.pack(fill=tk.X, padx=8, pady=(4, 2))
        self._detect_var   = tk.StringVar()
        self._detect_combo = ttk.Combobox(
            self._detect_row, textvariable=self._detect_var,
            state="readonly", width=44, font=("Meiryo", 9),
        )
        self._detect_combo.pack(side=tk.LEFT)
        ttk.Button(self._detect_row, text="更新",
                   command=self._refresh_detect_devices).pack(side=tk.LEFT, padx=4)
        self._detect_status_var = tk.StringVar(value="")
        ttk.Label(self._detect_row, textvariable=self._detect_status_var,
                  font=("Meiryo", 8), foreground="gray").pack(side=tk.LEFT, padx=6)

        # ゲーム音レベルバー（ループバックモード時のみ有効）
        self._audio_canvas = tk.Canvas(detect_frame, height=22, bg="#1e1e1e",
                                       highlightthickness=0)
        self._audio_canvas.pack(fill=tk.X, padx=6, pady=(2, 2))
        self._audio_bar_id  = self._audio_canvas.create_rectangle(0, 0, 0, 22, fill="#44aaff", outline="")
        self._audio_peak_id = self._audio_canvas.create_rectangle(0, 0, 0, 22, fill="#ffffff", outline="")

        audio_info = ttk.Frame(detect_frame)
        audio_info.pack(fill=tk.X, padx=6, pady=(0, 6))
        self._audio_rms_var = tk.StringVar(value="RMS: 0.0000")
        ttk.Label(audio_info, textvariable=self._audio_rms_var,
                  font=("Consolas", 8), foreground="gray").pack(side=tk.LEFT)

        if not _PYAUDIO_AVAILABLE:
            self._mic_status_var.set("pyaudio が未インストールです")

        # 初期構築
        self._refresh_mic_devices()
        self._refresh_detect_devices()

    # ── デバイスリスト更新 ────────────────────────────────────────────────────

    def _refresh_mic_devices(self) -> None:
        self._mic_devices = list_audio_input_devices()
        if self._mic_devices:
            self._mic_combo["values"] = [f"[{i}] {n}" for i, n in self._mic_devices]
            self._mic_combo.current(0)
            self._mic_combo.bind("<<ComboboxSelected>>", self._on_mic_select)
            self._on_mic_select()
            self._mic_status_var.set(f"{len(self._mic_devices)} デバイス検出")
        else:
            self._mic_combo["values"] = ["（入力デバイスが見つかりません）"]
            self._mic_combo.current(0)
            self._mic_status_var.set("デバイスなし")

    def _refresh_detect_devices(self) -> None:
        self._detect_devices = list_loopback_devices()
        if self._detect_devices:
            self._detect_combo["values"] = [f"[{i}] {n}" for i, n in self._detect_devices]
            self._detect_combo.current(0)
            self._detect_combo.bind("<<ComboboxSelected>>", self._on_detect_select)
            self._detect_status_var.set(f"{len(self._detect_devices)} デバイス検出")
            self._on_detect_select()
        else:
            self._detect_combo["values"] = ["（ループバックデバイスなし）"]
            self._detect_combo.current(0)
            self._detect_status_var.set("デバイスなし")
            self._audio_monitor.stop()

    def _on_detect_select(self, _event=None) -> None:
        sel = self._detect_combo.current()
        if 0 <= sel < len(self._detect_devices):
            dev_idx, _ = self._detect_devices[sel]
            self._audio_monitor.set_device(dev_idx)
        else:
            self._audio_monitor.stop()

    def _on_mic_select(self, _event=None) -> None:
        sel = self._mic_combo.current()
        if 0 <= sel < len(self._mic_devices):
            dev_idx, _ = self._mic_devices[sel]
            self._monitor.set_device(dev_idx)

    # ── レベルポーリング ──────────────────────────────────────────────────────

    def _poll_level(self) -> None:
        # マイクレベルバー
        rms = self._monitor.current_rms
        self._rms_var.set(f"RMS: {rms:.4f}")

        if rms > self._peak_rms:
            self._peak_rms   = rms
            self._peak_decay = 0
        else:
            self._peak_decay += 1
            if self._peak_decay > 30:
                self._peak_rms = max(0.0, self._peak_rms - 0.001)

        try:
            w = self._canvas.winfo_width()
            if w < 2:
                w = 400
            scaled      = min(rms * self._RMS_SCALE, 100) / 100
            peak_scaled = min(self._peak_rms * self._RMS_SCALE, 100) / 100
            bar_x  = int(w * scaled)
            peak_x = int(w * peak_scaled)
            color  = "#44dd44" if scaled < 0.5 else ("#ffaa00" if scaled < 0.8 else "#ff4444")
            self._canvas.coords(self._bar_id,  0, 0, bar_x, 22)
            self._canvas.itemconfig(self._bar_id, fill=color)
            self._canvas.coords(self._peak_id, peak_x - 2, 0, peak_x + 2, 22)
        except Exception:
            pass

        # ゲーム音（ループバック）レベルバー
        arms = self._audio_monitor.current_rms
        self._audio_rms_var.set(f"RMS: {arms:.4f}")

        if arms > self._audio_peak_rms:
            self._audio_peak_rms   = arms
            self._audio_peak_decay = 0
        else:
            self._audio_peak_decay += 1
            if self._audio_peak_decay > 30:
                self._audio_peak_rms = max(0.0, self._audio_peak_rms - 0.001)

        try:
            w = self._audio_canvas.winfo_width()
            if w < 2:
                w = 400
            ascaled      = min(arms * self._RMS_SCALE, 100) / 100
            apeak_scaled = min(self._audio_peak_rms * self._RMS_SCALE, 100) / 100
            abar_x  = int(w * ascaled)
            apeak_x = int(w * apeak_scaled)
            acolor  = "#44aaff" if ascaled < 0.5 else ("#aa66ff" if ascaled < 0.8 else "#ff4444")
            self._audio_canvas.coords(self._audio_bar_id,  0, 0, abar_x, 22)
            self._audio_canvas.itemconfig(self._audio_bar_id, fill=acolor)
            self._audio_canvas.coords(self._audio_peak_id, apeak_x - 2, 0, apeak_x + 2, 22)
        except Exception:
            pass

        self.after(80, self._poll_level)

    # ── デバイス取得（EchoMate 起動時に呼ばれる） ────────────────────────────

    def get_voice_input_device(self) -> int | None:
        """VoiceInput（音声認識）用デバイスインデックスを返す"""
        sel = self._mic_combo.current()
        if 0 <= sel < len(self._mic_devices):
            return self._mic_devices[sel][0]
        return None

    def get_audio_detect_device(self) -> int | None:
        """AudioDetector（ゲームイベント検知）用ループバックデバイスインデックスを返す。"""
        sel = self._detect_combo.current()
        if 0 <= sel < len(self._detect_devices):
            return self._detect_devices[sel][0]
        return None


# ── 相棒設定タブ ──────────────────────────────────────────────────────────────

class CharacterFrame(ttk.Frame):
    """相棒の人格を選択するタブ"""

    def __init__(self, master: tk.Misc, on_change) -> None:
        super().__init__(master)
        self._on_change = on_change  # callback(character_key: str)
        self._chars: dict = {}
        self._selected = tk.StringVar()
        self._build()

    # 現在有効なキャラクター（他はデータを保持しつつ非公開）
    _ENABLED_CHARACTERS = ["echo"]

    def _build(self) -> None:
        try:
            with open("characters.json", encoding="utf-8") as f:
                raw = json.load(f)
            all_chars = {k: v for k, v in raw.items() if not k.startswith("_")}
            self._chars = {k: v for k, v in all_chars.items() if k in self._ENABLED_CHARACTERS}
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
        if "echo" in self._chars:
            self._selected.set("echo")
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
    """吹き出し UI・アバター立ち絵の表示制御タブ"""

    def __init__(self, master: tk.Misc, get_bubble, get_avatar=None) -> None:
        super().__init__(master)
        self._get_bubble = get_bubble   # callable -> Optional[SpeechBubble]
        self._get_avatar = get_avatar   # callable -> Optional[AvatarWindow]
        self._build()

    def _build(self) -> None:
        ttk.Label(self, text="オーバーレイ UI", font=("Meiryo", 11)).pack(pady=(12, 6))

        info = (
            "グリーンバック背景のウィンドウです。\n"
            "OBS の「ウィンドウキャプチャ」＋クロマキーフィルターで\n"
            "ゲーム映像に透過合成できます。"
        )
        ttk.Label(self, text=info, justify=tk.LEFT,
                  foreground="gray").pack(padx=12, anchor=tk.W)

        # ── 吹き出し制御 ─────────────────────────────────────────────
        bubble_frame = ttk.LabelFrame(self, text="吹き出し")
        bubble_frame.pack(fill=tk.X, padx=12, pady=(8, 4))

        ctrl = ttk.Frame(bubble_frame)
        ctrl.pack(pady=6)
        ttk.Button(ctrl, text="表示", command=self._show_bubble).pack(side=tk.LEFT, padx=6)
        ttk.Button(ctrl, text="隠す",  command=self._hide_bubble).pack(side=tk.LEFT, padx=6)

        # テスト送信
        test_row = ttk.Frame(bubble_frame)
        test_row.pack(fill=tk.X, padx=6, pady=(0, 6))
        ttk.Label(test_row, text="テスト:").pack(side=tk.LEFT)
        self._test_var = tk.StringVar(value="やったじゃん！")
        ttk.Entry(test_row, textvariable=self._test_var, width=24).pack(side=tk.LEFT, padx=4)
        ttk.Button(test_row, text="送信", command=self._send_test).pack(side=tk.LEFT)

        # ── アバター立ち絵制御 ────────────────────────────────────────
        avatar_frame = ttk.LabelFrame(self, text="アバター立ち絵")
        avatar_frame.pack(fill=tk.X, padx=12, pady=4)

        avatar_ctrl = ttk.Frame(avatar_frame)
        avatar_ctrl.pack(pady=6)
        ttk.Button(avatar_ctrl, text="表示", command=self._show_avatar).pack(side=tk.LEFT, padx=6)
        ttk.Button(avatar_ctrl, text="隠す",  command=self._hide_avatar).pack(side=tk.LEFT, padx=6)

        sprite_info = (
            "assets/sprites/ に画像を配置すると立ち絵が表示されます。\n"
            "ファイル名: base_idle_close.png, eyes_open.png 等"
        )
        ttk.Label(avatar_frame, text=sprite_info, justify=tk.LEFT,
                  foreground="gray", font=("Meiryo", 8)).pack(padx=8, pady=(0, 6), anchor=tk.W)

    # ── 吹き出し ──────────────────────────────────────────────────────────────

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

    # ── アバター ──────────────────────────────────────────────────────────────

    def _show_avatar(self) -> None:
        avatar = self._get_avatar() if self._get_avatar else None
        if avatar:
            avatar.show()

    def _hide_avatar(self) -> None:
        avatar = self._get_avatar() if self._get_avatar else None
        if avatar:
            avatar.hide()


# ── 学習ノートタブ ────────────────────────────────────────────────────────────

class LearningFrame(ttk.Frame):
    """相棒の学習ノート（気になること）タブ"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._build()

    def _build(self) -> None:
        ttk.Label(self, text="相棒の学習ノート（気になること）",
                  font=("Meiryo", 11)).pack(pady=(10, 4))

        # ── 気になることリスト ──────────────────────────────────────────────
        list_frame = ttk.LabelFrame(self, text="相棒が知りたがっていること")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 4))

        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self._word_list = tk.Listbox(
            list_frame, yscrollcommand=scroll.set, height=6,
            selectmode=tk.SINGLE, font=("Meiryo", 10), exportselection=False,
        )
        scroll.config(command=self._word_list.yview)
        self._word_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0), pady=4)
        scroll.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 4), pady=4)

        btn_row = ttk.Frame(list_frame)
        btn_row.pack(fill=tk.X, padx=4, pady=(0, 4))
        ttk.Button(btn_row, text="一覧を更新", command=self._refresh).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="名前を変更",
                   command=self._rename_curiosity).pack(side=tk.LEFT, padx=(6, 0))

        # ── 学習入力タブ（URL / ノート） ────────────────────────────────────
        input_nb = ttk.Notebook(self)
        input_nb.pack(fill=tk.X, padx=8, pady=4)

        # URL タブ
        url_tab = ttk.Frame(input_nb)
        input_nb.add(url_tab, text="  URLから学習  ")
        ttk.Label(url_tab, text="URL:").grid(row=0, column=0, padx=6, pady=6, sticky=tk.W)
        self._url_var = tk.StringVar()
        ttk.Entry(url_tab, textvariable=self._url_var,
                  width=36).grid(row=0, column=1, padx=4, pady=6, sticky=tk.EW)
        ttk.Button(url_tab, text="学習させる",
                   command=self._learn_url).grid(row=0, column=2, padx=6, pady=6)
        url_tab.columnconfigure(1, weight=1)

        # ノート タブ
        note_tab = ttk.Frame(input_nb)
        input_nb.add(note_tab, text="  ノートから学習  ")
        ttk.Label(note_tab, text="内容を直接入力してください:").pack(
            anchor=tk.W, padx=6, pady=(6, 2))
        note_inner = ttk.Frame(note_tab)
        note_inner.pack(fill=tk.X, padx=6, pady=(0, 4))
        note_scroll = ttk.Scrollbar(note_inner, orient=tk.VERTICAL)
        self._note_text = tk.Text(
            note_inner, height=4, wrap=tk.WORD, font=("Meiryo", 9),
            yscrollcommand=note_scroll.set,
        )
        note_scroll.config(command=self._note_text.yview)
        self._note_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        note_scroll.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Button(note_tab, text="ノートから学習させる",
                   command=self._learn_note).pack(anchor=tk.E, padx=6, pady=(0, 6))

        # ステータス
        self._status_var = tk.StringVar(value="単語を選択して学習方法を選んでください")
        ttk.Label(self, textvariable=self._status_var,
                  foreground="gray").pack(anchor=tk.W, padx=10, pady=(0, 2))

        # ── 学習済み知識 ────────────────────────────────────────────────────
        knowledge_frame = ttk.LabelFrame(self, text="学習済みのゲーム知識")
        knowledge_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        scroll2 = ttk.Scrollbar(knowledge_frame, orient=tk.VERTICAL)
        self._knowledge_list = tk.Listbox(
            knowledge_frame, yscrollcommand=scroll2.set, height=4,
            font=("Meiryo", 9), exportselection=False,
        )
        scroll2.config(command=self._knowledge_list.yview)
        self._knowledge_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                                   padx=(4, 0), pady=4)
        scroll2.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 4), pady=4)

        k_btn_row = ttk.Frame(knowledge_frame)
        k_btn_row.pack(fill=tk.X, padx=4, pady=(0, 4))
        ttk.Button(k_btn_row, text="名前を変更",
                   command=self._rename_knowledge).pack(side=tk.LEFT)

        # ── 辞書リセット ────────────────────────────────────────────────────
        reset_frame = ttk.Frame(self)
        reset_frame.pack(fill=tk.X, padx=8, pady=(0, 6))
        ttk.Button(reset_frame, text="気になることリストをリセット",
                   command=self._reset_curiosity).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(reset_frame, text="学習済み知識をリセット",
                   command=self._reset_knowledge).pack(side=tk.LEFT)

        self._refresh()

    def _refresh(self) -> None:
        try:
            from web_learner import load_curiosity_list, load_game_knowledge
            self._word_list.delete(0, tk.END)
            for item in load_curiosity_list():
                self._word_list.insert(tk.END, item.get("word", ""))
            self._knowledge_list.delete(0, tk.END)
            for word, desc in load_game_knowledge().items():
                display = f"{word}: {desc[:45]}..." if len(desc) > 45 else f"{word}: {desc}"
                self._knowledge_list.insert(tk.END, display)
        except Exception as e:
            self._status_var.set(f"読み込みエラー: {e}")

    def _get_selected_word(self) -> Optional[str]:
        sel = self._word_list.curselection()
        if not sel:
            self._status_var.set("単語をリストから選択してください")
            return None
        return self._word_list.get(sel[0])

    def _learn_url(self) -> None:
        word = self._get_selected_word()
        if not word:
            return
        url = self._url_var.get().strip()
        if not url:
            self._status_var.set("URLを入力してください")
            return
        self._status_var.set(f"「{word}」を学習中... しばらくお待ちください")

        def _do() -> None:
            try:
                from web_learner import learn_from_url
                success, message = learn_from_url(word, url)
                display = message[:60] + "..." if len(message) > 60 else message
                self.after(0, lambda: self._status_var.set(
                    f"学習完了: {display}" if success else f"学習失敗: {display}"))
                self.after(0, self._refresh)
            except RuntimeError as e:
                self.after(0, lambda: self._status_var.set(f"⚠ {e}"))
            except Exception as e:
                self.after(0, lambda: self._status_var.set(f"エラー: {e}"))

        threading.Thread(target=_do, daemon=True, name="WebLearner").start()

    def _learn_note(self) -> None:
        word = self._get_selected_word()
        if not word:
            return
        note = self._note_text.get("1.0", tk.END).strip()
        if not note:
            self._status_var.set("ノートの内容を入力してください")
            return
        self._status_var.set(f"「{word}」をノートから学習中...")

        def _do() -> None:
            try:
                from web_learner import learn_from_note
                success, message = learn_from_note(word, note)
                display = message[:60] + "..." if len(message) > 60 else message
                self.after(0, lambda: self._status_var.set(
                    f"学習完了: {display}" if success else f"学習失敗: {display}"))
                self.after(0, self._refresh)
            except RuntimeError as e:
                self.after(0, lambda: self._status_var.set(f"⚠ {e}"))
            except Exception as e:
                self.after(0, lambda: self._status_var.set(f"エラー: {e}"))

        threading.Thread(target=_do, daemon=True, name="NoteLearner").start()

    def _rename_curiosity(self) -> None:
        word = self._get_selected_word()
        if not word:
            return
        new_name = simpledialog.askstring(
            "名前を変更", f"「{word}」の新しい名前を入力してください:", initialvalue=word, parent=self)
        if not new_name or new_name.strip() == word:
            return
        try:
            from web_learner import rename_curiosity
            if rename_curiosity(word, new_name.strip()):
                self._status_var.set(f"「{word}」→「{new_name.strip()}」に変更しました")
                self._refresh()
            else:
                self._status_var.set("名前の変更に失敗しました")
        except Exception as e:
            self._status_var.set(f"エラー: {e}")

    def _reset_curiosity(self) -> None:
        if not messagebox.askyesno(
            "リセット確認", "気になることリストをすべて削除しますか？", parent=self
        ):
            return
        try:
            from web_learner import clear_curiosity_list
            count = clear_curiosity_list()
            self._status_var.set(f"気になることリストをリセットしました（{count}件削除）")
            self._refresh()
        except Exception as e:
            self._status_var.set(f"エラー: {e}")

    def _reset_knowledge(self) -> None:
        if not messagebox.askyesno(
            "リセット確認", "学習済み知識をすべて削除しますか？", parent=self
        ):
            return
        try:
            from web_learner import clear_game_knowledge
            count = clear_game_knowledge()
            self._status_var.set(f"学習済み知識をリセットしました（{count}件削除）")
            self._refresh()
        except Exception as e:
            self._status_var.set(f"エラー: {e}")

    def _rename_knowledge(self) -> None:
        sel = self._knowledge_list.curselection()
        if not sel:
            self._status_var.set("変更する項目を選択してください")
            return
        entry = self._knowledge_list.get(sel[0])
        old_word = entry.split(":")[0].strip()
        new_name = simpledialog.askstring(
            "名前を変更", f"「{old_word}」の新しい名前を入力してください:", initialvalue=old_word, parent=self)
        if not new_name or new_name.strip() == old_word:
            return
        try:
            from web_learner import rename_knowledge
            if rename_knowledge(old_word, new_name.strip()):
                self._status_var.set(f"「{old_word}」→「{new_name.strip()}」に変更しました")
                self._refresh()
            else:
                self._status_var.set("名前の変更に失敗しました")
        except Exception as e:
            self._status_var.set(f"エラー: {e}")


# ── プロファイルタブ ──────────────────────────────────────────────────────────

class ProfileFrame(ttk.Frame):
    """相棒との仲（bond_level・プレイスタイル・記憶）を表示するタブ"""

    POLL_MS = 5000  # 自動更新間隔 (ms)

    def __init__(self, master: tk.Misc, get_echo_mate) -> None:
        super().__init__(master)
        self._get_echo_mate = get_echo_mate
        self._build()
        self._poll()

    def _build(self) -> None:
        ttk.Label(self, text="相棒との仲", font=("Meiryo", 11)).pack(pady=(10, 4))

        # ── 親密度 ────────────────────────────────────────────────────────
        bond_frame = ttk.LabelFrame(self, text="親密度 (Bond Level)")
        bond_frame.pack(fill=tk.X, padx=12, pady=(4, 4))

        self._bond_var = tk.StringVar(value="0.00")
        ttk.Label(bond_frame, textvariable=self._bond_var,
                  font=("Meiryo", 10, "bold")).pack(anchor=tk.W, padx=8, pady=(4, 0))
        self._bond_bar = ttk.Progressbar(
            bond_frame, orient=tk.HORIZONTAL, maximum=1.0, mode="determinate"
        )
        self._bond_bar.pack(fill=tk.X, padx=8, pady=(2, 8))

        # ── プレイスタイルタグ ─────────────────────────────────────────────
        style_frame = ttk.LabelFrame(self, text="プレイスタイル")
        style_frame.pack(fill=tk.X, padx=12, pady=4)
        self._style_inner = ttk.Frame(style_frame)
        self._style_inner.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(self._style_inner, text="（まだ分析されていません）",
                  foreground="gray").pack(side=tk.LEFT)

        # ── 記憶に残る出来事 ───────────────────────────────────────────────
        ep_frame = ttk.LabelFrame(self, text="記憶に残る出来事")
        ep_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        scroll = ttk.Scrollbar(ep_frame, orient=tk.VERTICAL)
        self._ep_list = tk.Listbox(
            ep_frame, yscrollcommand=scroll.set, height=6,
            font=("Meiryo", 9), exportselection=False,
        )
        scroll.config(command=self._ep_list.yview)
        self._ep_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0), pady=4)
        scroll.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 4), pady=4)

        btn_row = ttk.Frame(self)
        btn_row.pack(pady=(0, 8))
        ttk.Button(btn_row, text="更新", command=self._refresh).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="プロファイルをリセット",
                   command=self._reset_profile).pack(side=tk.LEFT, padx=4)

    def _poll(self) -> None:
        self._refresh()
        self.after(self.POLL_MS, self._poll)

    def _refresh(self) -> None:
        em = self._get_echo_mate()
        if em is None or not hasattr(em, "user_profile"):
            return
        try:
            profile = em.user_profile
            bond = profile.get_bond_level()
            self._bond_var.set(f"{bond:.2f}")
            self._bond_bar["value"] = bond

            # プレイスタイルタグを再描画
            for w in self._style_inner.winfo_children():
                w.destroy()
            labels = profile.get().get("playstyle_labels", [])
            if labels:
                for lbl in labels:
                    tk.Label(
                        self._style_inner, text=f"  {lbl}  ",
                        bg="#445577", fg="white",
                        font=("Meiryo", 9, "bold"),
                        relief=tk.FLAT, padx=4, pady=2,
                    ).pack(side=tk.LEFT, padx=3)
            else:
                ttk.Label(self._style_inner, text="（まだ分析されていません）",
                          foreground="gray").pack(side=tk.LEFT)

            # エピソード一覧（新しい順）
            self._ep_list.delete(0, tk.END)
            for ep in reversed(profile.get_memorable_episodes()):
                game = ep.get("game", "")
                text = ep.get("text", "")
                entry = f"[{game}] {text}" if game else text
                self._ep_list.insert(tk.END, entry)
        except Exception:
            pass

    def _reset_profile(self) -> None:
        em = self._get_echo_mate()
        if em is None or not hasattr(em, "user_profile"):
            messagebox.showwarning("リセット不可", "EchoMate が起動していません", parent=self)
            return
        if not messagebox.askyesno(
            "プロファイルリセット",
            "プロファイルをすべて初期値に戻します。\n親密度・プレイスタイル・記憶もリセットされます。\n\nよろしいですか？",
            parent=self,
        ):
            return
        try:
            em.user_profile.reset()
            self._refresh()
            messagebox.showinfo("完了", "プロファイルをリセットしました", parent=self)
        except Exception as e:
            messagebox.showerror("エラー", f"リセットに失敗しました:\n{e}", parent=self)


# ── メンタルグラフタブ ────────────────────────────────────────────────────────

class MentalGraphFrame(ttk.Frame):
    """
    テンション（興奮度）の推移をリアルタイムで折れ線グラフ表示するタブ。

    StateManager.get_state().tension を定期的にポーリングし、
    直近 HISTORY_MAX ポイントを Canvas に描画する。
    """

    HISTORY_MAX = 60    # 保持する最大データポイント数
    POLL_MS     = 3000  # ポーリング間隔 (ms)

    _PAD_L = 36         # 左余白（Y軸ラベル用）
    _PAD_R = 8
    _PAD_T = 10
    _PAD_B = 22         # 下余白（X軸）

    def __init__(self, master: tk.Misc, get_echo_mate) -> None:
        super().__init__(master)
        self._get_echo_mate = get_echo_mate
        self._history: list[float] = []
        self._build()
        self._poll()

    def _build(self) -> None:
        ttk.Label(
            self, text="テンション（興奮度）推移",
            font=("Meiryo", 11),
        ).pack(pady=(10, 4))

        self._canvas = tk.Canvas(
            self, bg="#1a1a2e", height=180,
            highlightthickness=1, highlightbackground="#333355",
        )
        self._canvas.pack(fill=tk.X, padx=8, pady=4)
        self._canvas.bind("<Configure>", lambda _: self._draw())

        # 数値表示行
        info = ttk.Frame(self)
        info.pack(fill=tk.X, padx=8)
        self._tension_var = tk.StringVar(value="テンション: -- （停止中）")
        ttk.Label(info, textvariable=self._tension_var,
                  font=("Meiryo", 10, "bold")).pack(side=tk.LEFT)
        self._status_var = tk.StringVar(value="")
        ttk.Label(info, textvariable=self._status_var,
                  font=("Meiryo", 8), foreground="gray").pack(side=tk.RIGHT)

        # 凡例
        legend = ttk.Frame(self)
        legend.pack(fill=tk.X, padx=8, pady=(2, 4))
        for color, label in [
            ("#ff4444", "高 (≥0.6)"),
            ("#ffaa00", "中 (0.3〜0.6)"),
            ("#44aaff", "低 (<0.3)"),
        ]:
            f = ttk.Frame(legend)
            f.pack(side=tk.LEFT, padx=6)
            tk.Canvas(f, bg=color, width=12, height=12,
                      highlightthickness=0).pack(side=tk.LEFT)
            ttk.Label(f, text=label, font=("Meiryo", 8),
                      foreground="gray").pack(side=tk.LEFT, padx=2)

    def _poll(self) -> None:
        em = self._get_echo_mate()
        if em is not None and hasattr(em, "state_manager"):
            try:
                tension = em.state_manager.get_state().tension
                self._history.append(tension)
                if len(self._history) > self.HISTORY_MAX:
                    del self._history[0]
                label = (
                    "非常に高い" if tension >= 0.8 else
                    "高い"       if tension >= 0.6 else
                    "普通"        if tension >= 0.3 else
                    "低い"
                )
                self._tension_var.set(f"テンション: {tension:.2f}  ({label})")
                self._status_var.set(f"{len(self._history)} pt")
                self._draw()
            except Exception:
                pass
        else:
            self._tension_var.set("テンション: -- （停止中）")
            self._status_var.set("")

        self.after(self.POLL_MS, self._poll)

    def _draw(self) -> None:
        c = self._canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            return

        pl = self._PAD_L
        pr = self._PAD_R
        pt = self._PAD_T
        pb = self._PAD_B
        gw = w - pl - pr   # グラフ描画幅
        gh = h - pt - pb   # グラフ描画高さ
        if gw <= 0 or gh <= 0:
            return

        # ── グリッド & ラベル ──────────────────────────────────────────
        for val, lbl in [(0.0, "0.0"), (0.3, "0.3"), (0.6, "0.6"), (1.0, "1.0")]:
            y = pt + int(gh * (1.0 - val))
            grid_color = "#333355" if val not in (0.0, 1.0) else "#444466"
            c.create_line(pl, y, pl + gw, y, fill=grid_color, width=1)
            c.create_text(pl - 4, y, text=lbl, anchor=tk.E,
                          fill="#8888aa", font=("Consolas", 8))

        # 軸線
        c.create_line(pl, pt, pl, pt + gh, fill="#555577", width=1)
        c.create_line(pl, pt + gh, pl + gw, pt + gh, fill="#555577", width=1)

        if len(self._history) < 2:
            c.create_text(
                pl + gw // 2, pt + gh // 2,
                text="データ待機中...",
                fill="#556688", font=("Meiryo", 10),
            )
            return

        # ── 折れ線 ────────────────────────────────────────────────────
        n    = len(self._history)
        span = self.HISTORY_MAX - 1
        xs   = [pl + int(gw * i / span) for i in range(n)]
        ys   = [pt + int(gh * (1.0 - v)) for v in self._history]

        for i in range(n - 1):
            v = self._history[i]
            color = (
                "#ff4444" if v >= 0.6 else
                "#ffaa00" if v >= 0.3 else
                "#44aaff"
            )
            c.create_line(xs[i], ys[i], xs[i + 1], ys[i + 1],
                          fill=color, width=2)

        # 最新値のドット
        lx, ly = xs[-1], ys[-1]
        v = self._history[-1]
        dot = "#ff4444" if v >= 0.6 else "#ffaa00" if v >= 0.3 else "#44aaff"
        c.create_oval(lx - 4, ly - 4, lx + 4, ly + 4,
                      fill=dot, outline="#ffffff", width=1)


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
        self._avatar:    Optional[AvatarWindow] = None
        self._running      = False
        self._mic_monitor  = MicMonitor()
        self._audio_monitor = MicMonitor()

        self._build_ui()
        self._mic_monitor.start()
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

        self._mic_tab = MicSetupFrame(notebook, self._mic_monitor, self._audio_monitor)
        notebook.add(self._mic_tab, text="  マイク設定  ")

        self._bubble_tab = BubbleFrame(
            notebook,
            get_bubble=lambda: self._bubble,
            get_avatar=lambda: self._avatar,
        )
        notebook.add(self._bubble_tab, text="  吹き出し  ")

        self._learning_tab = LearningFrame(notebook)
        notebook.add(self._learning_tab, text="  学習ノート  ")

        self._mental_tab = MentalGraphFrame(
            notebook, get_echo_mate=lambda: self._echo_mate
        )
        notebook.add(self._mental_tab, text="  メンタルグラフ  ")

        self._profile_tab = ProfileFrame(
            notebook, get_echo_mate=lambda: self._echo_mate
        )
        notebook.add(self._profile_tab, text="  プロファイル  ")

        # ── フッター: 起動/停止 ───────────────────────────────────────────
        footer = ttk.Frame(self.root)
        footer.pack(fill=tk.X, padx=8, pady=(0, 2))

        self._btn_start = ttk.Button(footer, text="▶  EchoMate 起動",
                                     command=self._start_echomate, width=18)
        self._btn_start.pack(side=tk.LEFT, padx=4)

        self._btn_stop  = ttk.Button(footer, text="■  停止",
                                     command=self._stop_echomate, width=10, state=tk.DISABLED)
        self._btn_stop.pack(side=tk.LEFT, padx=4)

        self._status_var = tk.StringVar(value="停止中")
        ttk.Label(footer, textvariable=self._status_var,
                  font=("Meiryo", 10)).pack(side=tk.LEFT, padx=12)

        # AI 思考中インジケーター（LLM 呼び出し中のみ表示）
        self._thinking_var = tk.StringVar(value="")
        self._thinking_label = ttk.Label(
            footer, textvariable=self._thinking_var,
            font=("Meiryo", 9), foreground="#2288ff",
        )
        self._thinking_label.pack(side=tk.RIGHT, padx=8)

        # ── フッター2: ゲーム音レベルバー（常時表示） ────────────────────
        audio_footer = ttk.Frame(self.root, relief=tk.GROOVE, borderwidth=1)
        audio_footer.pack(fill=tk.X, padx=8, pady=(0, 6))

        ttk.Label(audio_footer, text="ゲーム音:", font=("Meiryo", 8),
                  foreground="gray").pack(side=tk.LEFT, padx=(6, 2))
        self._footer_audio_canvas = tk.Canvas(
            audio_footer, height=14, bg="#1e1e1e", highlightthickness=0,
        )
        self._footer_audio_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=3)
        self._footer_audio_bar = self._footer_audio_canvas.create_rectangle(
            0, 0, 0, 14, fill="#44aaff", outline="",
        )
        self._footer_audio_rms_var = tk.StringVar(value="0.0000")
        ttk.Label(audio_footer, textvariable=self._footer_audio_rms_var,
                  font=("Consolas", 8), foreground="gray", width=7).pack(side=tk.LEFT, padx=(0, 6))

        self.root.minsize(480, 500)
        self._poll_footer_audio()

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
                char_key       = self._char_tab.get_selected()
                voice_device   = self._mic_tab.get_voice_input_device()
                detect_device  = self._mic_tab.get_audio_detect_device()
                monitor_idx    = self._game_tab.get_monitor_index()
                target_exe     = self._game_tab.get_selected_exe()
                exclude_pids   = ServiceManager.get_voicevox_pids()
                dialogue_mode  = not target_exe
                self._echo_mate = EchoMate(
                    character=char_key,
                    enable_cv=not dialogue_mode,
                    enable_audio=not dialogue_mode,
                    enable_dummy=False,
                    speech_callback=self._on_speech,
                    voice_input_device=voice_device,
                    audio_detect_device=detect_device,
                    vision_monitor_index=monitor_idx,
                    audio_target_exe=target_exe,
                    audio_exclude_pids=exclude_pids or None,
                )
                self._echo_mate.start_background()
            except Exception as exc:
                self.root.after(0, self._on_start_failed, f"EchoMate 起動エラー:\n{exc}")
                return

            # 4. AI 思考中インジケーターを接続
            if self._echo_mate:
                self._echo_mate.ai.set_thinking_callback(self._on_ai_thinking)

            # 5. 吹き出し・アバター作成
            self.root.after(0, self._create_bubble)
            self.root.after(0, self._create_avatar)
            self.root.after(0, self._on_start_success, dialogue_mode)

        threading.Thread(target=_launch, daemon=True, name="Launch").start()

    def _on_start_success(self, dialogue_mode: bool = False) -> None:
        self._running = True
        self._btn_start.config(state=tk.DISABLED)
        self._btn_stop.config(state=tk.NORMAL)
        if dialogue_mode:
            self._status_var.set("動作中（対話モード）")
        else:
            self._status_var.set("動作中（ゲームモード）")

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

    def _on_ai_thinking(self, is_thinking: bool) -> None:
        """AICompanion の LLM 呼び出し開始/終了時に呼ばれる（ワーカースレッドから）"""
        self.root.after(0, self._thinking_var.set,
                        "AI 思考中..." if is_thinking else "")

    def _on_stop_done(self) -> None:
        self._running = False
        self._thinking_var.set("")
        if self._avatar:
            self._avatar.destroy()
            self._avatar = None
        self._btn_start.config(state=tk.NORMAL)
        self._status_var.set("停止中")

    # ── 吹き出し ──────────────────────────────────────────────────────────────

    def _create_bubble(self) -> None:
        if self._bubble:
            self._bubble.destroy()
        self._bubble = SpeechBubble(master=self.root)
        self._bubble.show()

    def _create_avatar(self) -> None:
        if self._avatar:
            self._avatar.destroy()
        self._avatar = AvatarWindow(
            master=self.root,
            voice_output=self._echo_mate.voice_output if self._echo_mate else None,
            state_manager=self._echo_mate.state_manager if self._echo_mate else None,
        )
        self._avatar.show()

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

            if self._avatar:
                try:
                    self._avatar.destroy()
                except Exception:
                    pass

            self.root.after(0, self.root.destroy)

        threading.Thread(target=_shutdown, daemon=True, name="AppShutdown").start()

    # ── ゲーム音フッターポーリング ────────────────────────────────────────────

    _FOOTER_RMS_SCALE = 800

    def _poll_footer_audio(self) -> None:
        rms = self._audio_monitor.current_rms
        self._footer_audio_rms_var.set(f"{rms:.4f}")
        try:
            w = self._footer_audio_canvas.winfo_width()
            if w < 2:
                w = 400
            scaled = min(rms * self._FOOTER_RMS_SCALE, 100) / 100
            bar_x  = int(w * scaled)
            color  = "#44aaff" if scaled < 0.5 else ("#aa66ff" if scaled < 0.8 else "#ff4444")
            self._footer_audio_canvas.coords(self._footer_audio_bar, 0, 0, bar_x, 14)
            self._footer_audio_canvas.itemconfig(self._footer_audio_bar, fill=color)
        except Exception:
            pass
        self.root.after(100, self._poll_footer_audio)

    # ── メインループ ──────────────────────────────────────────────────────────

    def run(self) -> None:
        self.root.mainloop()


# ── エントリーポイント ────────────────────────────────────────────────────────

def main() -> None:
    app = EchoMateGUI()
    app.run()


if __name__ == "__main__":
    main()
