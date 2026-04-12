"""
opencv_detector.py - OpenCV 画面検出モジュール

ゲーム画面をスクリーンキャプチャし、映像解析でイベントを自動検出する。
検出したイベントは EventManager のキューに直接注入する。

検出方式:
  1. color_threshold : 指定領域の特定色の割合で判定（HP バー等）
  2. template        : テンプレート画像とのマッチングで判定（アイコン等）
  3. frame_diff      : フレーム間差分で画面変化を検出（フラッシュ・エフェクト）
  4. brightness      : 明度の急変を検出（spike=爆発フラッシュ / drop=デス暗転）

設定は cv_config.json で管理。setup_wizard.py で自動生成可能。

将来の拡張例:
  - YOLO によるキャラクター/敵検出
  - OCR によるキルフィード文字認識
"""

import cv2
import numpy as np
import threading
import time
import logging
import json
from dataclasses import dataclass, field

try:
    import mss
    _MSS_AVAILABLE = True
except ImportError:
    _MSS_AVAILABLE = False

from event import EventManager, GameEvent

logger = logging.getLogger(__name__)

# 検出ループの FPS（高いほど CPU 負荷増）
DETECTION_FPS = 10
DETECTION_INTERVAL = 1.0 / DETECTION_FPS


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class DetectionZone:
    """
    1 つの画面領域と検出ルールを定義する。

    Attributes:
        name       : 識別名（ログ・クールダウン管理に使用）
        region     : キャプチャ領域 {"top", "left", "width", "height"}
        event_type : 検出時に発火する GameEvent の種別
        method     : 検出方式 ("color_threshold" or "template")
        params     : 検出方式固有のパラメータ
        cooldown   : 同一ゾーンのイベント最小間隔（秒）
        enabled    : False にすると検出をスキップ
    """
    name: str
    region: dict
    event_type: str
    method: str
    params: dict = field(default_factory=dict)
    cooldown: float = 3.0
    enabled: bool = True


# ---------------------------------------------------------------------------
# OpenCV 検出器
# ---------------------------------------------------------------------------

class OpenCVDetector:
    """
    スクリーンキャプチャ + OpenCV でゲームイベントを検出するクラス。

    使用方法:
        detector = OpenCVDetector(event_manager)
        detector.start()   # 非同期スレッドで検出開始
        ...
        detector.stop()
    """

    # 明度 EMA の適応係数（小さいほどゆっくり適応 = 急変に敏感）
    _BRIGHTNESS_EMA_ALPHA = 0.05

    def __init__(
        self,
        event_manager: EventManager,
        config_path: str = "cv_config.json",
    ) -> None:
        self.event_manager = event_manager
        self.config_path = config_path
        self.running = False
        self._thread: threading.Thread | None = None
        self._last_fired: dict[str, float] = {}
        self.zones: list[DetectionZone] = []
        # frame_diff 用：ゾーンごとの直前フレームを保持
        self._prev_frames: dict[str, np.ndarray] = {}
        # brightness 用：ゾーンごとの明度 EMA を保持
        self._brightness_ema: dict[str, float] = {}
        self._load_config()

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------

    def start(self) -> None:
        """検出スレッドを起動する"""
        if not _MSS_AVAILABLE:
            logger.warning("mss not installed. Run: pip install mss")
            return

        self._load_config()
        self.running = True
        self._thread = threading.Thread(
            target=self._detection_loop,
            daemon=True,
            name="CVDetector",
        )
        self._thread.start()
        logger.info("OpenCV detector started (%d zones, %d FPS)", len(self.zones), DETECTION_FPS)

    def stop(self) -> None:
        """検出スレッドを停止する"""
        self.running = False
        logger.info("OpenCV detector stopped")

    def is_available(self) -> bool:
        """検出に必要なライブラリが揃っているか確認する"""
        return _MSS_AVAILABLE

    # ------------------------------------------------------------------
    # 設定管理
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        """cv_config.json からゾーン定義を読み込む"""
        try:
            with open(self.config_path, encoding="utf-8") as f:
                config = json.load(f)
            # "_" で始まるキーはコメント・メタ情報なので除外してから渡す
            raw_zones = [
                {k: v for k, v in z.items() if not k.startswith("_")}
                for z in config.get("zones", [])
            ]
            self.zones = [DetectionZone(**z) for z in raw_zones]
            logger.info("CV config loaded: %d zones from %s", len(self.zones), self.config_path)
        except FileNotFoundError:
            logger.info("cv_config.json not found, using default zones")
            self._setup_default_zones()
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("CV config parse error: %s — using defaults", e)
            self._setup_default_zones()

    def _setup_default_zones(self) -> None:
        """
        デフォルト検出ゾーン（汎用ゲーム向けサンプル）。
        実際のゲームに合わせて cv_config.json で上書きすること。
        """
        self.zones = [
            DetectionZone(
                name="hp_bar_low",
                region={"top": 880, "left": 60, "width": 220, "height": 18},
                event_type="low_hp",
                method="color_threshold",
                params={"color": "red", "threshold": 0.4},
                cooldown=5.0,
            ),
            DetectionZone(
                name="kill_flash",
                region={"top": 10, "left": 1700, "width": 200, "height": 80},
                event_type="kill",
                method="color_threshold",
                params={"color": "yellow", "threshold": 0.15},
                cooldown=3.0,
            ),
        ]

    # ------------------------------------------------------------------
    # 検出ループ
    # ------------------------------------------------------------------

    def _detection_loop(self) -> None:
        """メイン検出ループ（別スレッドで動作）"""
        with mss.mss() as sct:
            while self.running:
                loop_start = time.time()
                try:
                    for zone in self.zones:
                        if zone.enabled:
                            self._check_zone(sct, zone)
                except Exception as e:
                    logger.error("Detection loop error: %s", e)

                # FPS 維持のためにスリープ
                elapsed = time.time() - loop_start
                sleep_time = max(0.0, DETECTION_INTERVAL - elapsed)
                time.sleep(sleep_time)

    def _check_zone(self, sct: "mss.mss", zone: DetectionZone) -> None:
        """1 つのゾーンを検査してイベントを発火する"""
        try:
            raw = sct.grab(zone.region)
            frame = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)

            detected = False
            if zone.method == "color_threshold":
                detected = self._detect_by_color(frame, zone.params)
            elif zone.method == "template":
                detected = self._detect_by_template(frame, zone.params)
            elif zone.method == "frame_diff":
                detected = self._detect_by_frame_diff(frame, zone.name, zone.params)
            elif zone.method == "brightness":
                detected = self._detect_by_brightness(frame, zone.name, zone.params)
            else:
                logger.warning("Unknown detection method: %s", zone.method)

            if detected:
                self._fire_event(zone)

        except Exception as e:
            logger.debug("Zone check failed [%s]: %s", zone.name, e)

    # ------------------------------------------------------------------
    # 検出メソッド
    # ------------------------------------------------------------------

    # HSV 色範囲テーブル（lower, upper）
    _COLOR_RANGES: dict[str, tuple[list, list]] = {
        "red":    ([0,  80,  80], [10, 255, 255]),
        "red2":   ([170, 80, 80], [180, 255, 255]),  # HSV の赤は 0 と 180 の両端
        "green":  ([40,  50, 50], [80,  255, 255]),
        "yellow": ([20,  80, 80], [40,  255, 255]),
        "blue":   ([100, 80, 80], [130, 255, 255]),
        "white":  ([0,   0, 200], [180,  30, 255]),
    }

    def _detect_by_color(self, frame: np.ndarray, params: dict) -> bool:
        """
        指定色の占有率が threshold を超えたら True を返す。

        params:
            color     : "red" / "green" / "yellow" / "blue" / "white"
            threshold : 0.0〜1.0（デフォルト 0.3）
        """
        color = params.get("color", "red")
        threshold = float(params.get("threshold", 0.3))

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # 赤は HSV 上で 0 付近と 170 付近の両端に存在するため合算
        if color == "red":
            m1 = cv2.inRange(hsv, np.array(self._COLOR_RANGES["red"][0]),
                             np.array(self._COLOR_RANGES["red"][1]))
            m2 = cv2.inRange(hsv, np.array(self._COLOR_RANGES["red2"][0]),
                             np.array(self._COLOR_RANGES["red2"][1]))
            mask = cv2.bitwise_or(m1, m2)
        else:
            if color not in self._COLOR_RANGES:
                logger.warning("Unknown color: %s", color)
                return False
            lo, hi = self._COLOR_RANGES[color]
            mask = cv2.inRange(hsv, np.array(lo), np.array(hi))

        ratio = float(np.count_nonzero(mask)) / mask.size
        logger.debug("Color detect [%s] ratio=%.3f threshold=%.3f", color, ratio, threshold)
        return ratio >= threshold

    def _detect_by_template(self, frame: np.ndarray, params: dict) -> bool:
        """
        テンプレート画像とのマッチング。

        params:
            template_path : テンプレート画像のパス
            threshold     : 一致スコア閾値（0.0〜1.0、デフォルト 0.8）
        """
        template_path = params.get("template_path", "")
        threshold = float(params.get("threshold", 0.8))

        if not template_path:
            logger.warning("template_path not specified")
            return False

        template = cv2.imread(template_path)
        if template is None:
            logger.warning("Template image not found: %s", template_path)
            return False

        # テンプレートがフレームより大きい場合はスキップ
        th, tw = template.shape[:2]
        fh, fw = frame.shape[:2]
        if th > fh or tw > fw:
            logger.debug("Template larger than frame, skipping")
            return False

        result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        logger.debug("Template match score=%.3f threshold=%.3f", max_val, threshold)
        return float(max_val) >= threshold

    def _detect_by_frame_diff(
        self, frame: np.ndarray, zone_name: str, params: dict
    ) -> bool:
        """
        直前フレームとの差分で画面変化を検出する。
        爆発フラッシュ・スキルエフェクト・スコア表示などに有効。

        params:
            threshold      : グレー差分の平均値閾値（0〜255、デフォルト 25）
            changed_ratio  : 変化ピクセルの割合閾値（0.0〜1.0、デフォルト 0.15）
            pixel_threshold: 1ピクセルを「変化あり」と見なす差分値（デフォルト 15）
        """
        threshold       = float(params.get("threshold", 25))
        changed_ratio   = float(params.get("changed_ratio", 0.15))
        pixel_threshold = int(params.get("pixel_threshold", 15))

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        prev = self._prev_frames.get(zone_name)

        # 初回は比較対象がないのでフレームを記録して終了
        if prev is None or prev.shape != gray.shape:
            self._prev_frames[zone_name] = gray
            return False

        diff = cv2.absdiff(gray, prev)
        self._prev_frames[zone_name] = gray

        mean_diff = float(diff.mean())
        ratio = float(np.count_nonzero(diff > pixel_threshold)) / diff.size

        logger.debug(
            "FrameDiff [%s] mean=%.1f threshold=%.1f ratio=%.3f changed_ratio=%.3f",
            zone_name, mean_diff, threshold, ratio, changed_ratio,
        )
        return mean_diff >= threshold or ratio >= changed_ratio

    def _detect_by_brightness(
        self, frame: np.ndarray, zone_name: str, params: dict
    ) -> bool:
        """
        明度の急変を指数移動平均との差分で検出する。
        環境光に自動適応するためシーン変化に強い。

        params:
            mode      : "spike"（明るくなる）or "drop"（暗くなる）
            threshold : EMA からの乖離量（0〜255、デフォルト 40）
        """
        mode      = params.get("mode", "spike")
        threshold = float(params.get("threshold", 40))

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness = float(gray.mean())

        # EMA を初期化または更新
        if zone_name not in self._brightness_ema:
            self._brightness_ema[zone_name] = brightness
            return False

        ema = self._brightness_ema[zone_name]
        self._brightness_ema[zone_name] = (
            self._BRIGHTNESS_EMA_ALPHA * brightness
            + (1.0 - self._BRIGHTNESS_EMA_ALPHA) * ema
        )

        delta = brightness - ema
        logger.debug(
            "Brightness [%s] current=%.1f ema=%.1f delta=%.1f threshold=%.1f",
            zone_name, brightness, ema, delta, threshold,
        )

        if mode == "spike":
            return delta >= threshold
        elif mode == "drop":
            return delta <= -threshold
        return False

    # ------------------------------------------------------------------
    # イベント発火
    # ------------------------------------------------------------------

    def _fire_event(self, zone: DetectionZone) -> None:
        """クールダウンを確認してイベントをキューに追加する"""
        now = time.time()
        last = self._last_fired.get(zone.name, 0.0)
        if now - last < zone.cooldown:
            return

        self._last_fired[zone.name] = now
        event = GameEvent(
            zone.event_type,
            {"source": "opencv", "zone": zone.name},
        )
        self.event_manager.add_event(event)
        logger.info("CV event fired: %s (zone=%s)", zone.event_type, zone.name)
