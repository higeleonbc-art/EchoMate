"""
vision_analyzer.py - Vision LLM によるゲーム画面解析モジュール

moondream (Ollama) を使用して、ゲーム画面の状況をテキスト化する。
バックグラウンドスレッドが一定間隔で画面をキャプチャ・解析し、
最新コンテキストをキャッシュする。AIの返答生成時に自動で注入される。

モデルの切り替えは VISION_MODEL 定数を変更するだけでよい:
  - "moondream" : 軽量（約2GB VRAM）・速い（推奨スタート）
  - "llava:7b"  : 高品質（約8GB VRAM）・やや遅い

依存ライブラリ:
  pip install mss Pillow
  ollama pull moondream
"""

import base64
import io
import logging
import threading
from typing import Optional

import requests

try:
    import mss
    _MSS_AVAILABLE = True
except ImportError:
    _MSS_AVAILABLE = False

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

VISION_MODEL    = "moondream"
OLLAMA_API_URL  = "http://localhost:11434/api/generate"
VISION_INTERVAL = 5.0   # 画面解析の間隔（秒）
VISION_TIMEOUT  = 15    # Ollama 呼び出しタイムアウト（秒）

# 画面解析プロンプト（日本語で簡潔に状況を返してもらう）
VISION_PROMPT = (
    "This is a game screen. "
    "Briefly describe: HP status, any danger, notable events. "
    "Answer in Japanese, 1-2 sentences only."
)

# 音トリガー時のイベント判定プロンプト（AudioDetector連携用）
VISION_EVENT_PROMPT = (
    "This is a game screen captured right after a loud sound. "
    "Did the player just: get a kill, receive damage (low HP), or have a big play? "
    "Reply with ONLY one word: KILL / LOW_HP / BIG_PLAY / NONE"
)

# スクリーンショットのリサイズ上限（処理負荷・トークン削減）
CAPTURE_MAX_WIDTH  = 1280
CAPTURE_MAX_HEIGHT = 720


# ---------------------------------------------------------------------------
# VisionAnalyzer
# ---------------------------------------------------------------------------

class VisionAnalyzer:
    """
    ゲーム画面を Vision LLM で定期解析するクラス。

    使用方法:
        analyzer = VisionAnalyzer()
        analyzer.start()
        context = analyzer.get_context()   # 最新の画面状況テキスト
        analyzer.stop()
    """

    def __init__(
        self,
        model: str = VISION_MODEL,
        interval: float = VISION_INTERVAL,
    ) -> None:
        self.model    = model
        self.interval = interval
        self._latest_context: str = ""
        self._lock    = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        VisionAnalyzer を有効化する（定期バックグラウンドループは廃止）。
        解析は analyze_now() によるオンデマンド方式で実行する。
        """
        if not _MSS_AVAILABLE:
            logger.warning("mss not installed — vision disabled. Run: pip install mss")
            return
        if not _PIL_AVAILABLE:
            logger.warning("Pillow not installed — vision disabled. Run: pip install Pillow")
            return
        self._running = True
        logger.info("VisionAnalyzer ready (model=%s, on-demand mode)", self.model)

    def stop(self) -> None:
        """VisionAnalyzer を停止する"""
        self._running = False
        logger.info("VisionAnalyzer stopped")

    def is_available(self) -> bool:
        """必要ライブラリが揃っているか確認する"""
        return _MSS_AVAILABLE and _PIL_AVAILABLE

    # ------------------------------------------------------------------
    # 公開メソッド
    # ------------------------------------------------------------------

    def get_context(self) -> str:
        """最新の画面解析結果を返す（未解析の場合は空文字）"""
        with self._lock:
            return self._latest_context

    def analyze_now(self, prompt: str = VISION_PROMPT) -> Optional[str]:
        """
        即座にスクリーンショットを撮影してVLMで解析する（同期・オンデマンド）。
        AudioDetectorの音スパイクトリガーや任意のタイミングで呼び出す。

        Returns:
            VLMの応答テキスト（失敗時はNone）
        """
        if not self._running:
            return None
        image_b64 = self._capture_screenshot()
        if not image_b64:
            return None
        result = self._call_vision(image_b64, prompt)
        if result:
            with self._lock:
                self._latest_context = result
            logger.debug("Vision context updated (on-demand): %s", result[:80])
        return result

    # ------------------------------------------------------------------
    # プライベートメソッド
    # ------------------------------------------------------------------

    def _capture_screenshot(self) -> Optional[str]:
        """
        プライマリモニターをキャプチャし JPEG base64 で返す。
        解像度を縮小してトークンと処理負荷を削減する。
        """
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]  # プライマリモニター
                raw     = sct.grab(monitor)
                img     = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                img.thumbnail(
                    (CAPTURE_MAX_WIDTH, CAPTURE_MAX_HEIGHT),
                    Image.LANCZOS,
                )
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=75)
                return base64.b64encode(buffer.getvalue()).decode("utf-8")
        except Exception as e:
            logger.error("Screenshot capture failed: %s", e)
            return None

    def _call_vision(self, image_b64: str, prompt: str = VISION_PROMPT) -> Optional[str]:
        """Ollama の Vision API（multimodal）を呼び出す"""
        try:
            payload = {
                "model":  self.model,
                "prompt": prompt,
                "images": [image_b64],
                "stream": False,
                "options": {
                    "num_predict": 80,
                    "temperature": 0.3,
                },
            }
            res = requests.post(OLLAMA_API_URL, json=payload, timeout=VISION_TIMEOUT)
            if res.status_code != 200:
                logger.error("Vision API HTTP %d: %s", res.status_code, res.text[:100])
                return None
            return res.json().get("response", "").strip()
        except requests.exceptions.ConnectionError:
            logger.error("Ollama not running at %s", OLLAMA_API_URL)
            return None
        except requests.exceptions.Timeout:
            logger.warning("Vision API timed out after %ds", VISION_TIMEOUT)
            return None
        except Exception as e:
            logger.error("Vision API error: %s", e)
            return None
