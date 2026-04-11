"""
voice.py - 音声処理モジュール

VoiceOutput : VOICEVOX HTTP API を使ったテキスト読み上げ
VoiceInput  : SpeechRecognition を使ったマイク音声認識（Google STT）

設計方針：
  - VOICEVOX が起動していない場合はテキスト出力のみに自動フォールバック
  - pyaudio が未インストールの場合も同様にフォールバック
  - VoiceOutput はスレッドロックで同時発話を防止
"""

import io
import wave
import logging
import threading
import time

import requests
import speech_recognition as sr

try:
    import pyaudio
    _PYAUDIO_AVAILABLE = True
except ImportError:
    _PYAUDIO_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

VOICEVOX_BASE_URL = "http://localhost:50021"
DEFAULT_SPEAKER_ID = 3   # 3 = ずんだもん（ノーマル）
REQUEST_TIMEOUT_QUERY = 5    # audio_query タイムアウト（秒）
REQUEST_TIMEOUT_SYNTH = 10   # synthesis タイムアウト（秒）
AUDIO_CHUNK_SIZE = 1024


# ---------------------------------------------------------------------------
# 音声出力
# ---------------------------------------------------------------------------

class VoiceOutput:
    """
    VOICEVOX を使ってテキストを音声に変換し、再生する。
    VOICEVOX が利用できない場合はテキスト出力のみ行う。
    """

    def __init__(self, speaker_id: int = DEFAULT_SPEAKER_ID) -> None:
        self.speaker_id = speaker_id
        self._lock = threading.Lock()
        self._voicevox_available: bool | None = None  # None = 未確認
        logger.info("VoiceOutput initialized (speaker_id=%d)", speaker_id)

    def speak(self, text: str) -> None:
        """
        テキストを音声に変換して再生する。
        失敗した場合はコンソール出力のみ。
        """
        if not text:
            return
        with self._lock:
            logger.debug("Speaking: %s", text)
            audio_data = self._synthesize(text)
            if audio_data:
                self._play_wav(audio_data)
            else:
                # フォールバック：コンソール出力
                print(f"[Voice] {text}")

    # ------------------------------------------------------------------
    # プライベートメソッド
    # ------------------------------------------------------------------

    def _synthesize(self, text: str) -> bytes | None:
        """VOICEVOX API でテキストを WAV バイト列に変換する"""
        try:
            # Step 1: audio_query
            query_res = requests.post(
                f"{VOICEVOX_BASE_URL}/audio_query",
                params={"text": text, "speaker": self.speaker_id},
                timeout=REQUEST_TIMEOUT_QUERY,
            )
            if query_res.status_code != 200:
                logger.error("audio_query failed: HTTP %d", query_res.status_code)
                return None
            audio_query = query_res.json()

            # Step 2: synthesis
            synth_res = requests.post(
                f"{VOICEVOX_BASE_URL}/synthesis",
                params={"speaker": self.speaker_id},
                json=audio_query,
                timeout=REQUEST_TIMEOUT_SYNTH,
            )
            if synth_res.status_code != 200:
                logger.error("synthesis failed: HTTP %d", synth_res.status_code)
                return None

            self._voicevox_available = True
            return synth_res.content

        except requests.exceptions.ConnectionError:
            if self._voicevox_available is not False:
                logger.warning(
                    "VOICEVOX not running at %s — falling back to text output.",
                    VOICEVOX_BASE_URL,
                )
            self._voicevox_available = False
            return None
        except requests.exceptions.Timeout:
            logger.warning("VOICEVOX request timed out")
            return None
        except Exception as e:
            logger.error("VOICEVOX unexpected error: %s", e)
            return None

    def _play_wav(self, wav_bytes: bytes) -> None:
        """WAV バイト列を pyaudio で再生する"""
        if not _PYAUDIO_AVAILABLE:
            logger.warning("pyaudio not installed — skipping audio playback")
            return

        try:
            buf = io.BytesIO(wav_bytes)
            with wave.open(buf, "rb") as wf:
                p = pyaudio.PyAudio()
                stream = p.open(
                    format=p.get_format_from_width(wf.getsampwidth()),
                    channels=wf.getnchannels(),
                    rate=wf.getframerate(),
                    output=True,
                )
                data = wf.readframes(AUDIO_CHUNK_SIZE)
                while data:
                    stream.write(data)
                    data = wf.readframes(AUDIO_CHUNK_SIZE)
                stream.stop_stream()
                stream.close()
                p.terminate()
        except Exception as e:
            logger.error("Audio playback error: %s", e)


# ---------------------------------------------------------------------------
# 音声入力
# ---------------------------------------------------------------------------

class VoiceInput:
    """
    マイクから音声を取得し、Google Speech-to-Text で日本語テキストに変換する。
    マイクが利用できない場合は None を返す。
    """

    def __init__(self) -> None:
        self.recognizer = sr.Recognizer()
        self._mic: sr.Microphone | None = None
        self._mic_available = self._init_microphone()

    def _init_microphone(self) -> bool:
        """マイクを初期化し、環境ノイズに適応する"""
        try:
            self._mic = sr.Microphone()
            with self._mic as source:
                logger.info("Adjusting for ambient noise (1s)...")
                self.recognizer.adjust_for_ambient_noise(source, duration=1)
            logger.info("Microphone initialized")
            return True
        except OSError as e:
            logger.warning("Microphone not available: %s", e)
            return False
        except Exception as e:
            logger.error("Microphone init error: %s", e)
            return False

    def listen(self, timeout: float = 3.0, phrase_time_limit: float = 5.0) -> str | None:
        """
        マイクを指定秒数リッスンし、認識テキストを返す。

        Args:
            timeout: 発話開始を待つ最大秒数
            phrase_time_limit: 1発話の最大秒数
        Returns:
            認識テキスト（無音・失敗時は None）
        """
        if not self._mic_available or self._mic is None:
            return None

        try:
            with self._mic as source:
                audio = self.recognizer.listen(
                    source,
                    timeout=timeout,
                    phrase_time_limit=phrase_time_limit,
                )

            text: str = self.recognizer.recognize_google(audio, language="ja-JP")
            logger.info("Recognized speech: %s", text)
            return text

        except sr.WaitTimeoutError:
            # 発話なし：正常系
            return None
        except sr.UnknownValueError:
            logger.debug("Speech not understood")
            return None
        except sr.RequestError as e:
            logger.error("Google STT request error: %s", e)
            return None
        except Exception as e:
            logger.error("VoiceInput.listen error: %s", e)
            return None

    @property
    def available(self) -> bool:
        return self._mic_available
