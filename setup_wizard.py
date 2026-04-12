"""
setup_wizard.py - EchoMate セットアップウィザード

起動中のウィンドウを一覧表示し、選択したゲームウィンドウの
座標を自動取得して cv_config.json を生成する。

使い方:
    python setup_wizard.py

必要ライブラリ:
    pip install pywin32
"""

import json
import os
import sys

try:
    import win32gui
    import win32con
    _WIN32_AVAILABLE = True
except ImportError:
    _WIN32_AVAILABLE = False


# ---------------------------------------------------------------------------
# ウィンドウ列挙
# ---------------------------------------------------------------------------

def get_visible_windows() -> list[dict]:
    """
    デスクトップ上の可視ウィンドウを列挙する。
    タイトルなし・極小ウィンドウは除外。
    """
    windows: list[dict] = []

    def _callback(hwnd: int, _) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title or len(title) < 2:
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top
        if width < 200 or height < 150:
            return
        windows.append({
            "hwnd": hwnd,
            "title": title,
            "left": left,
            "top": top,
            "width": width,
            "height": height,
        })

    win32gui.EnumWindows(_callback, None)
    # タイトル昇順でソート（見やすくする）
    return sorted(windows, key=lambda w: w["title"].lower())


# ---------------------------------------------------------------------------
# ゾーン自動生成
# ---------------------------------------------------------------------------

def suggest_zones(win: dict) -> list[dict]:
    """
    ウィンドウのサイズ・位置から検出ゾーンを比率で自動計算する。
    FPS ゲームの一般的なレイアウトを想定した初期値。
    ゲームに合わせて cv_config.json を手動調整すること。
    """
    l = win["left"]
    t = win["top"]
    w = win["width"]
    h = win["height"]

    def region(rx: float, ry: float, rw: float, rh: float) -> dict:
        """ウィンドウ比率 (0.0〜1.0) でリージョンを定義するヘルパー"""
        return {
            "top":    t + int(h * ry),
            "left":   l + int(w * rx),
            "width":  max(1, int(w * rw)),
            "height": max(1, int(h * rh)),
        }

    return [
        {
            "name": "hp_bar_low",
            "region": region(0.03, 0.90, 0.15, 0.025),
            "event_type": "low_hp",
            "method": "color_threshold",
            "params": {"color": "red", "threshold": 0.40},
            "cooldown": 5.0,
            "enabled": True,
        },
        {
            "name": "kill_feed",
            "region": region(0.75, 0.01, 0.22, 0.12),
            "event_type": "kill",
            "method": "color_threshold",
            "params": {"color": "yellow", "threshold": 0.12},
            "cooldown": 3.0,
            "enabled": True,
        },
        {
            "name": "screen_flash",
            "region": region(0.25, 0.25, 0.50, 0.50),
            "event_type": "big_play",
            "method": "frame_diff",
            "params": {"threshold": 25, "changed_ratio": 0.15},
            "cooldown": 4.0,
            "enabled": True,
        },
        {
            "name": "death_screen",
            "region": region(0.20, 0.20, 0.60, 0.60),
            "event_type": "death",
            "method": "brightness",
            "params": {"mode": "drop", "threshold": 40},
            "cooldown": 8.0,
            "enabled": True,
        },
    ]


def default_audio_rules() -> list[dict]:
    return [
        {
            "name": "explosion",
            "event_type": "big_play",
            "threshold": 0.35,
            "cooldown": 3.0,
            "enabled": True,
        },
        {
            "name": "quiet_sustained",
            "event_type": "low_hp",
            "threshold": 0.05,
            "cooldown": 10.0,
            "enabled": False,
        },
    ]


# ---------------------------------------------------------------------------
# ウィザード本体
# ---------------------------------------------------------------------------

def run_wizard() -> None:
    _print_header()

    if not _WIN32_AVAILABLE:
        print("[!!] pywin32 が見つかりません。")
        print("     pip install pywin32  を実行してください。\n")
        _fallback_manual()
        return

    # ── ウィンドウ一覧表示 ─────────────────────────────────────
    print("起動中のウィンドウを取得中...\n")
    windows = get_visible_windows()

    if not windows:
        print("表示可能なウィンドウが見つかりませんでした。")
        return

    _print_window_list(windows)

    # ── ウィンドウ選択 ─────────────────────────────────────────
    win = _prompt_select(windows)
    if win is None:
        return

    print(f"\n選択: 「{win['title']}」")
    print(f"位置: ({win['left']}, {win['top']})  サイズ: {win['width']} x {win['height']}")

    # ── ゾーン生成 ─────────────────────────────────────────────
    zones = suggest_zones(win)
    print("\n─ 自動生成された検出ゾーン ─────────────────────────")
    _print_zones(zones)

    # ── 個別 ON/OFF ────────────────────────────────────────────
    if _ask_yn("\n個別に有効/無効を変更しますか？"):
        _toggle_zones(zones)

    # ── 検出方式の説明 ─────────────────────────────────────────
    print("\n─ 検出方式の説明 ──────────────────────────────────")
    print("  color_threshold : 指定色の占有率で判定（HP バー、キルフィード）")
    print("  frame_diff      : フレーム間差分で画面変化を検出（エフェクト、フラッシュ）")
    print("  brightness      : 明度の急変を検出（spike=明るくなる / drop=暗くなる）")
    print("  template        : テンプレート画像とのマッチング（アイコン検出）")

    # ── 音声ルール ─────────────────────────────────────────────
    audio_rules = default_audio_rules()
    print("\n─ 音声検出ルール ───────────────────────────────────")
    print("  [ON ] explosion  → big_play  (RMS >= 0.35)")
    print("  [OFF] quiet      → low_hp    (無音継続、デフォルト無効)")

    # ── 保存 ───────────────────────────────────────────────────
    config = {
        "_generated_for": win["title"],
        "_window_size": f"{win['width']}x{win['height']}",
        "zones": zones,
        "audio_rules": audio_rules,
    }

    output_path = "cv_config.json"
    _save_config(config, output_path)

    print(f"\n[OK] 設定を保存しました: {os.path.abspath(output_path)}")
    print("\n次のステップ:")
    print("  1. start.bat をダブルクリックして EchoMate を起動")
    print("  2. ゲームをプレイして動作確認")
    print("  3. 誤検知が多い場合は cv_config.json の threshold を上げる")
    print("  4. 検出されない場合は threshold を下げるか region 座標を調整する\n")


# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------

def _print_header() -> None:
    print("=" * 60)
    print("  EchoMate セットアップウィザード")
    print("  ゲームウィンドウを選択して検出設定を自動生成します")
    print("=" * 60)
    print()


def _print_window_list(windows: list[dict]) -> None:
    print(f"{'#':>3}  {'タイトル':<46}  {'サイズ':>12}")
    print("─" * 65)
    for i, w in enumerate(windows, 1):
        title = w["title"][:46]
        size = f"{w['width']}x{w['height']}"
        print(f"{i:>3}  {title:<46}  {size:>12}")
    print()


def _prompt_select(windows: list[dict]) -> dict | None:
    while True:
        try:
            raw = input(f"番号を入力してください (1〜{len(windows)}、q で終了): ").strip()
            if raw.lower() == "q":
                print("キャンセルしました。")
                return None
            n = int(raw)
            if 1 <= n <= len(windows):
                return windows[n - 1]
            print(f"  1〜{len(windows)} の数字を入力してください。")
        except ValueError:
            print("  数字を入力してください。")
        except KeyboardInterrupt:
            print("\nキャンセルしました。")
            return None


def _print_zones(zones: list[dict]) -> None:
    for z in zones:
        status = "ON " if z["enabled"] else "OFF"
        r = z["region"]
        coord = f"({r['left']},{r['top']}) {r['width']}x{r['height']}"
        print(f"  [{status}] {z['name']:<20} → {z['event_type']:<10}  {z['method']:<16} {coord}")


def _toggle_zones(zones: list[dict]) -> None:
    print()
    for z in zones:
        current = "Y" if z["enabled"] else "N"
        try:
            ans = input(f"  {z['name']:<20} 有効にする？ (Y/n, 現在:{current}): ").strip().lower()
            z["enabled"] = ans != "n"
        except KeyboardInterrupt:
            break


def _ask_yn(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} (y/N): ").strip().lower()
        return ans == "y"
    except KeyboardInterrupt:
        return False


def _save_config(config: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _fallback_manual() -> None:
    """pywin32 なしの場合の手動設定ガイド"""
    print("手動設定の方法:")
    print("  1. ゲームウィンドウの左上座標と解像度を確認する")
    print("     （ウィンドウを右クリック → プロパティ、または Alt+Space → プロパティ）")
    print("  2. cv_config.json の region を直接編集する")
    print("     例: { \"top\": 900, \"left\": 50, \"width\": 200, \"height\": 20 }")
    print()


# ---------------------------------------------------------------------------
# ROI セレクターモード
# ---------------------------------------------------------------------------

# ゾーン定義テンプレート（ROI モード用）
_ROI_ZONE_TEMPLATES = [
    ("hp_bar_low",   "low_hp",   "HP バーを囲んでください（左下付近）",           "color_threshold", {"color": "red",    "threshold": 0.40, "min_hits": 2, "window": 3}, 5.0),
    ("kill_feed",    "kill",     "キルフィードエリアを囲んでください（右上付近）", "frame_diff",      {"threshold": 20,   "changed_ratio": 0.08, "min_hits": 2, "window": 3}, 3.0),
    ("death_screen", "death",    "デス演出の中央部分を囲んでください",             "brightness",      {"mode": "drop",    "threshold": 40,  "min_hits": 3, "window": 4}, 8.0),
    ("screen_flash", "big_play", "エフェクトが出やすいエリアを囲んでください",     "frame_diff",      {"threshold": 25,   "changed_ratio": 0.15, "min_hits": 2, "window": 3}, 4.0),
]


def run_roi_mode() -> None:
    """
    スクリーンショットをキャプチャし、OpenCV ウィンドウ上で
    マウスドラッグにより検出ゾーンを直感的に設定する。

    使い方: python setup_wizard.py --roi
    """
    try:
        import cv2
        import mss
        import numpy as np
    except ImportError as e:
        print(f"[!!] 必要なライブラリがありません: {e}")
        print("     pip install opencv-python mss")
        return

    print("=" * 60)
    print("  EchoMate ROI セレクター")
    print("=" * 60)
    print()
    print("ゲームを起動してゲーム画面が見える状態にしてください。")
    try:
        input("準備できたら Enter を押してください...")
    except KeyboardInterrupt:
        return

    # スクリーンショット取得
    with mss.mss() as sct:
        monitor = sct.monitors[1]   # プライマリモニター
        raw = sct.grab(monitor)
    img = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)

    zones = []
    print("\n各検出ゾーンをドラッグで選択してください。")
    print("  Space/Enter → 確定   C → スキップ\n")

    for name, event_type, instruction, method, params, cooldown in _ROI_ZONE_TEMPLATES:
        win_title = f"[EchoMate ROI] {instruction}"
        print(f"  {instruction}")
        roi = cv2.selectROI(win_title, img, fromCenter=False, showCrosshair=True)
        cv2.destroyAllWindows()

        x, y, w, h = roi
        if w > 0 and h > 0:
            zones.append({
                "name":       name,
                "region":     {"top": int(y), "left": int(x), "width": int(w), "height": int(h)},
                "event_type": event_type,
                "method":     method,
                "params":     params,
                "cooldown":   cooldown,
                "enabled":    True,
            })
            print(f"  [OK] ({x}, {y}) {w}x{h} で登録\n")
        else:
            print("  [SKIP]\n")

    if not zones:
        print("ゾーンが1件も登録されませんでした。")
        return

    config = {
        "_generated_by": "ROI selector",
        "zones": zones,
        "audio_rules": default_audio_rules(),
    }
    output_path = "cv_config.json"
    _save_config(config, output_path)
    print(f"[OK] {len(zones)} ゾーンを {os.path.abspath(output_path)} に保存しました。")
    print("     EchoMate を起動してください: start.bat\n")


# ---------------------------------------------------------------------------
# プリセット読込モード
# ---------------------------------------------------------------------------

AVAILABLE_PRESETS = ["valorant", "apex", "fortnite"]


def run_preset_mode(preset_name: str) -> None:
    """
    presets/<name>.json を cv_config.json としてコピーする。

    使い方: python setup_wizard.py --preset valorant
    """
    import shutil

    preset_path = os.path.join("presets", f"{preset_name}.json")
    if not os.path.exists(preset_path):
        print(f"[!!] プリセットが見つかりません: {preset_path}")
        print(f"     利用可能: {', '.join(AVAILABLE_PRESETS)}")
        return

    shutil.copy(preset_path, "cv_config.json")
    print(f"[OK] プリセット '{preset_name}' を cv_config.json に適用しました。")
    print("     座標は 1920x1080 基準です。別解像度の場合は --roi で再設定してください。\n")


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    args = sys.argv[1:]

    if "--roi" in args:
        run_roi_mode()
    elif "--preset" in args:
        idx = args.index("--preset")
        if idx + 1 < len(args):
            run_preset_mode(args[idx + 1])
        else:
            print("使い方: python setup_wizard.py --preset <name>")
            print(f"  利用可能: {', '.join(AVAILABLE_PRESETS)}")
    else:
        run_wizard()
