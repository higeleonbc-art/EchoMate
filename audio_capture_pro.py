"""
audio_capture_pro.py - プロセス指定音声キャプチャラッパー（Phase 5）

process-audio-capture ライブラリを使い、特定プロセスの音量レベルを取得する。
実 API:
    ProcessAudioCapture(pid, output_path=None, mode=0,
                        level_callback=Callable[[float], None], dll_path=None)
    - level_callback(level_db: float) で音量(dB)を受け取る
    - output_path 省略時はファイル出力なし（レベル監視のみ）
    - is_capturing, level_db プロパティあり

dB → 線形 RMS 変換 (10^(dB/20)) を行い、
audio_detector の既存スパイク検出ロジックと互換性を保つ。
"""

import logging
import queue

logger = logging.getLogger(__name__)

try:
    from process_audio_capture import ProcessAudioCapture
    _PAC_AVAILABLE = True
except ImportError:
    _PAC_AVAILABLE = False
    logger.debug("process-audio-capture not installed — ProcessAudio unavailable")


def is_available() -> bool:
    return _PAC_AVAILABLE


class AudioCaptureProWrapper:
    """
    ProcessAudioCapture の level_callback を使って RMS 相当値を定期取得するラッパー。

    read_rms() で float (0.0〜1.0 の線形振幅) を返す。
    audio_detector._audio_loop_pac がこの値でスパイク検出を行う。
    """

    def __init__(self, pid: int | None, queue_maxsize: int = 64) -> None:
        if not _PAC_AVAILABLE:
            raise RuntimeError(
                "process-audio-capture is not installed. Run: pip install process-audio-capture"
            )
        self._pid = pid
        self._queue: queue.Queue[float] = queue.Queue(maxsize=queue_maxsize)
        self._capture: "ProcessAudioCapture | None" = None
        self.running = False

    def _on_level(self, level_db: float) -> None:
        """level_callback。dB 値を線形 RMS に変換してキューに積む。"""
        # dB が -inf や非常に小さい値(無音)は 0 に丸める
        rms = 10.0 ** (level_db / 20.0) if level_db > -100.0 else 0.0
        try:
            self._queue.put_nowait(rms)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(rms)
            except queue.Empty:
                pass

    def start(self) -> None:
        self._capture = ProcessAudioCapture(
            pid=self._pid,
            level_callback=self._on_level,
        )
        self._capture.start()
        self.running = True
        logger.info("AudioCaptureProWrapper started (pid=%s)", self._pid)

    def stop(self) -> None:
        self.running = False
        if self._capture:
            try:
                self._capture.stop()
            except Exception as e:
                logger.warning("AudioCaptureProWrapper stop error: %s", e)
            self._capture = None
        logger.info("AudioCaptureProWrapper stopped (pid=%d)", self._pid)

    def read_rms(self, timeout: float = 0.1) -> float | None:
        """キューから RMS 相当値 (0.0〜1.0) を取り出す。タイムアウト時は None。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
