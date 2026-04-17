"""
audio_detector.py - 音声ベースイベント検出モジュール

マイクの音量を常時監視し、急激な音量スパイクをゲームイベントとして検出する。
爆発音、銃声、キル音などの大音量を「big_play」等のイベントに変換する。

検出アルゴリズム:
  - PyAudio でマイクから CHUNK サイズずつ取得
  - RMS（二乗平均平方根）= 音量の実効値 を計算
  - 動的ベースライン（指数移動平均）と比較してスパイクを検出
  - ベースラインは環境音に自動適応するため誤検知を抑制

設定は cv_config.json の "audio_rules" セクションで管理。

注意:
  - マイクをゲームスピーカーの近くに置くか、
    仮想オーディオケーブル（VB-Cable 等）でループバック設定すると精度が上がる
  - ヘッドセット使用時はヘッドセットマイクが推奨
"""

import logging
import threading
import time
import json

import numpy as np

try:
    import pyaudiowpatch as pyaudio   # WASAPI ループバック対応版を優先
    _PYAUDIO_AVAILABLE   = True
    _WPATCH_AVAILABLE    = True
except ImportError:
    _WPATCH_AVAILABLE = False
    try:
        import pyaudio
        _PYAUDIO_AVAILABLE = True
    except ImportError:
        _PYAUDIO_AVAILABLE = False

from event import EventManager, GameEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 音声キャプチャ定数
# ---------------------------------------------------------------------------

CHUNK       = 1024          # 1 チャンクのサンプル数
CHANNELS    = 1             # モノラル
RATE        = 44100         # サンプリングレートのフォールバック値（Hz）

# 動的ベースラインの指数移動平均係数
# 小さいほどゆっくり適応（= 急変に敏感）
EMA_ALPHA   = 0.02

# ベースライン安定化のための初期フレーム数（この間はイベント発火しない）
WARMUP_FRAMES = 30


# ---------------------------------------------------------------------------
# AudioDetector
# ---------------------------------------------------------------------------

class AudioDetector:
    """
    マイク音量を監視してゲームイベントを検出するクラス。

    使用方法:
        detector = AudioDetector(event_manager)
        detector.start()
        ...
        detector.stop()
    """

    def __init__(
        self,
        event_manager: EventManager,
        config_path: str = "cv_config.json",
        device_index: int | None = None,
        vision_trigger_fn=None,          # Callable[[str, float, float], None] | None
        voice_output=None,               # VoiceOutput | None — フォールバックモードの自己応答防止用
        target_exe: str | None = None,   # キャプチャ対象プロセスの EXE 名（PID を動的解決）
        exclude_pids: list[int] | None = None,  # 除外 PID（VOICEVOX 等）
    ) -> None:
        self.event_manager = event_manager
        self.device_index = device_index
        self.vision_trigger_fn = vision_trigger_fn  # 設定時は音スパイクをVisionに委譲
        self.voice_output = voice_output
        self.target_exe = target_exe
        self.exclude_pids = exclude_pids or []
        # EXE 指定なしはシステム全体取得（フォールバック）→ 自己応答防止ガード有効
        self.is_fallback_mode: bool = (target_exe is None)
        self.current_rms: float = 0.0
        self.running = False
        self._thread: threading.Thread | None = None
        self._last_fired: dict[str, float] = {}
        self.rules: list[dict] = []
        self._load_rules(config_path)

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------

    def start(self) -> None:
        """音声監視スレッドを起動する"""
        if not _PYAUDIO_AVAILABLE:
            logger.warning("pyaudio not installed. Run: pip install pyaudio")
            return

        self.running = True
        self._thread = threading.Thread(
            target=self._audio_loop,
            daemon=True,
            name="AudioDetector",
        )
        self._thread.start()
        logger.info("AudioDetector started (%d rules)", len(self.rules))

    def stop(self) -> None:
        """音声監視スレッドを停止する"""
        self.running = False
        logger.info("AudioDetector stopped")

    def is_available(self) -> bool:
        return _PYAUDIO_AVAILABLE

    # ------------------------------------------------------------------
    # 設定読み込み
    # ------------------------------------------------------------------

    def _load_rules(self, config_path: str) -> None:
        """cv_config.json の audio_rules セクションを読み込む"""
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
            raw_rules = config.get("audio_rules", [])
            self.rules = [r for r in raw_rules if r.get("enabled", True)]
            logger.info("Audio rules loaded: %d active rules", len(self.rules))
        except FileNotFoundError:
            logger.info("cv_config.json not found, using default audio rule")
            self.rules = self._default_rules()
        except (json.JSONDecodeError, KeyError) as e:
            logger.error("Audio rule load error: %s — using defaults", e)
            self.rules = self._default_rules()

    @staticmethod
    def _default_rules() -> list[dict]:
        return [
            {
                "name":       "explosion",
                "event_type": "big_play",
                "threshold":  0.35,   # RMS の絶対値（0.0〜1.0）
                "cooldown":   3.0,
            }
        ]

    # ------------------------------------------------------------------
    # 音声ループ
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_rate(p: "pyaudio.PyAudio", device_index: int | None) -> int:
        """デバイスのネイティブサンプルレートを返す。取得できなければ RATE を使用。"""
        if device_index is None:
            return RATE
        try:
            info = p.get_device_info_by_index(device_index)
            return int(info.get("defaultSampleRate", RATE))
        except Exception:
            return RATE

    @staticmethod
    def _find_pid_by_exe(exe_name: str) -> int | None:
        try:
            import psutil
            for proc in psutil.process_iter(["name", "pid"]):
                if proc.info["name"] and proc.info["name"].lower() == exe_name.lower():
                    return proc.info["pid"]
        except Exception:
            pass
        return None

    def _wait_for_process(self, exe_name: str, poll_interval: float = 2.0) -> int | None:
        """EXE が起動するまでポーリングし、PID を返す。running=False になったら None。"""
        logger.info("Waiting for process: %s", exe_name)
        while self.running:
            pid = self._find_pid_by_exe(exe_name)
            if pid is not None:
                logger.info("Process found: %s (pid=%d)", exe_name, pid)
                return pid
            time.sleep(poll_interval)
        return None

    def _audio_loop(self) -> None:
        """マイクまたはループバックデバイスから連続取得してRMSを評価するメインループ"""
        from audio_capture_pro import is_available as is_pac_available
        if is_pac_available() and (self.target_exe or self.exclude_pids):
            self.is_fallback_mode = (self.target_exe is None)
            self._audio_loop_pac()
            return

        p = pyaudio.PyAudio()
        stream = None
        try:
            rate = self._resolve_rate(p, self.device_index)
            stream = p.open(
                format=pyaudio.paFloat32,
                channels=CHANNELS,
                rate=rate,
                input=True,
                frames_per_buffer=CHUNK,
                input_device_index=self.device_index,
            )
            logger.info("Audio stream opened (device=%s, rate=%d, chunk=%d)",
                        self.device_index, rate, CHUNK)

            # 動的ベースライン（環境音に適応）
            baseline = 0.0
            frame_count = 0

            while self.running:
                try:
                    raw = stream.read(CHUNK, exception_on_overflow=False)
                    samples = np.frombuffer(raw, dtype=np.float32)
                    rms = float(np.sqrt(np.mean(samples ** 2)))
                    self.current_rms = rms

                    # ベースラインを指数移動平均で更新
                    baseline = EMA_ALPHA * rms + (1.0 - EMA_ALPHA) * baseline
                    frame_count += 1

                    # ウォームアップ中は発火しない
                    if frame_count < WARMUP_FRAMES:
                        continue

                    # フォールバックモード: AI 発話中は自己ループを防ぐため判定を一時停止
                    if self.is_fallback_mode and self.voice_output and self.voice_output.is_speaking:
                        continue

                    # スパイク量（= ベースラインからの乖離）
                    spike = rms - baseline
                    logger.debug("RMS=%.4f baseline=%.4f spike=%.4f", rms, baseline, spike)

                    for rule in self.rules:
                        # threshold を「スパイク量」と比較（絶対値よりも誤検知しにくい）
                        if spike >= rule["threshold"]:
                            self._fire_event(rule, rms, spike)

                except OSError as e:
                    logger.warning("Audio read error: %s", e)
                    time.sleep(0.1)

        except Exception as e:
            logger.error("AudioDetector fatal error: %s", e)
        finally:
            self.current_rms = 0.0
            if stream:
                stream.stop_stream()
                stream.close()
            p.terminate()
            logger.info("Audio stream closed")

    def _audio_loop_pac(self) -> None:
        """process-audio-capture ラッパーを使ったキャプチャループ。
        target_exe 指定時はプロセス終了後も待機して再接続する。"""
        from audio_capture_pro import AudioCaptureProWrapper

        ALIVE_CHECK_INTERVAL = 5.0  # プロセス生存確認の間隔（秒）

        while self.running:
            # EXE名モード: プロセスが起動するまで待つ
            if self.target_exe:
                pid = self._wait_for_process(self.target_exe)
                if pid is None:
                    break  # running=False で停止
            else:
                pid = None  # フォールバック（exclude_pids のみ指定）

            wrapper = AudioCaptureProWrapper(pid=pid)
            wrapper.start()
            logger.info("PAC wrapper started (exe=%s, pid=%s, fallback=%s)",
                        self.target_exe, pid, self.is_fallback_mode)

            baseline = 0.0
            frame_count = 0
            last_alive_check = time.time()

            try:
                while self.running:
                    rms = wrapper.read_rms()
                    now = time.time()

                    if rms is None:
                        # プロセス生存確認（EXE名モードのみ）
                        if self.target_exe and now - last_alive_check > ALIVE_CHECK_INTERVAL:
                            last_alive_check = now
                            if self._find_pid_by_exe(self.target_exe) is None:
                                logger.info("Process exited: %s — waiting for restart", self.target_exe)
                                break
                        time.sleep(0.01)
                        continue

                    last_alive_check = now
                    self.current_rms = rms
                    baseline = EMA_ALPHA * rms + (1.0 - EMA_ALPHA) * baseline
                    frame_count += 1

                    if frame_count < WARMUP_FRAMES:
                        continue

                    if self.is_fallback_mode and self.voice_output and self.voice_output.is_speaking:
                        continue

                    spike = rms - baseline
                    logger.debug("PAC RMS=%.4f baseline=%.4f spike=%.4f", rms, baseline, spike)

                    for rule in self.rules:
                        if spike >= rule["threshold"]:
                            self._fire_event(rule, rms, spike)

            except Exception as e:
                logger.error("AudioDetector PAC loop error: %s", e)
            finally:
                wrapper.stop()
                self.current_rms = 0.0
                logger.info("PAC audio loop closed (exe=%s, pid=%s)", self.target_exe, pid)

            # EXE名モードでなければ再試行しない
            if not self.target_exe:
                break

    # ------------------------------------------------------------------
    # イベント発火
    # ------------------------------------------------------------------

    def _fire_event(self, rule: dict, rms: float, spike: float) -> None:
        """クールダウンを確認してイベントをキューに追加する"""
        now = time.time()
        last = self._last_fired.get(rule["name"], 0.0)
        if now - last < rule.get("cooldown", 3.0):
            return

        self._last_fired[rule["name"]] = now
        event_type = rule["event_type"]

        if self.vision_trigger_fn is not None:
            # Vision確認を経てイベント発火（非同期・VLMが判定）
            self.vision_trigger_fn(event_type, rms, spike)
            logger.info(
                "Audio spike → vision trigger called (rule=%s, rms=%.3f, spike=%.3f)",
                rule["name"], rms, spike,
            )
        else:
            # 従来の直接発火
            event = GameEvent(
                event_type,
                {"source": "audio", "rms": round(rms, 4), "spike": round(spike, 4)},
            )
            self.event_manager.add_event(event)
            logger.info(
                "Audio event fired: %s (rule=%s, rms=%.3f, spike=%.3f)",
                event_type, rule["name"], rms, spike,
            )
