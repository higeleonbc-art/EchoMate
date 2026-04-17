"""
voice.py - 音声処理モジュール

VoiceOutput : VOICEVOX HTTP API を使ったテキスト読み上げ
VoiceInput  : faster-whisper によるオフライン日本語音声認識

変更点（v2）:
  - Google STT → faster-whisper（ローカル処理・オフライン動作）
  - VoiceOutput.set_speaker(): キャラクター切替時に speaker_id を動的変更
  - エネルギーベース VAD: 無音検出で発話区間を自動分割
"""

import io
import os
import wave
import logging
import threading

import httpx
import numpy as np

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import pyaudiowpatch as pyaudio  # WASAPI ループバック対応版を優先
    _PYAUDIO_AVAILABLE = True
except ImportError:
    try:
        import pyaudio  # フォールバック
        _PYAUDIO_AVAILABLE = True
    except ImportError:
        _PYAUDIO_AVAILABLE = False

try:
    from faster_whisper import WhisperModel
    _WHISPER_AVAILABLE = True
except ImportError:
    _WHISPER_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

VOICEVOX_BASE_URL    = os.getenv("VOICEVOX_BASE_URL", "http://localhost:50021")
DEFAULT_SPEAKER_ID   = 7       # 7 = ずんだもん（キッドのデフォルト）
REQUEST_TIMEOUT_QUERY = 5
REQUEST_TIMEOUT_SYNTH = 10
AUDIO_CHUNK_SIZE     = 1024
DEFAULT_VOLUME       = float(os.getenv("VOICE_VOLUME", "1.0"))

# faster-whisper 設定
WHISPER_MODEL_SIZE   = os.getenv("WHISPER_MODEL_SIZE", "medium")  # base / small / medium / large-v3
WHISPER_RATE         = 16000     # Whisper 最適サンプリングレート
WHISPER_CHUNK        = 512
SILENCE_RMS          = 0.012     # この RMS 以下を「無音」と判定
SILENCE_FRAMES       = 30        # 無音フレームが続いたら発話終了（約 0.96 秒）
MAX_RECORD_FRAMES    = 8 * (WHISPER_RATE // WHISPER_CHUNK)  # 最大録音 8 秒

# LoL 専門用語辞書（Initial Prompt: 認識精度向上用）
# 2026年4月現在の最新チャンピオン、新アイテム（シーズン2）、略称を網羅
LOL_INITIAL_PROMPT = (
    "エイトロックス, アーリ, アカリ, アクシャン, アリスター, アムム, アニビア, アニー, アフェリオス, アッシュ, オレリオン・ソル, アズィール, バード, ベル＝ヴェス, ブリッツクランク, ブリッツ, ブランド, ブラウム, ブライアー, ケイトリン, カミール, カシオペア, チョ＝ガス, コーキ, ダリウス, ダイアナ, ドクター・ムンド, ドレイヴン, エコー, エリス, イぶリン, エズリアル, エズ, フィドルストリックス, フィオラ, フィズ, ガリオ, ガングプランク, ガレン, ナー, グラガス, グレイブス, グウェン, ヘカリム, ハイマーディンガー, フェイ, イラオイ, イれリア, アイバーン, ジャンナ, ジャーヴァンⅣ, ジャーヴァン, ジャックス, ジェイス, ジン, ジンクス, クサンテ, カイ＝サ, カイサ, カリスタ, カルマ, カーサス, カサディン, カタリナ, ケイル, ケイン, ケネン, カージックス, キンドレッド, クレッド, コグ＝マウ, ルブラン, リー・シン, リーシン, レオナ, リリア, リサンドラ, ルシアン, ルル, ラックス, マルファイト, マルザハール, マおカイ, マスター・イー, ミリオ, ミス・フォーチュン, モルデカイザー, モルデ, モルガナ, ナフィーリ, ナミ, ナサス, ノーチラス, ニーコ, ニダリー, ニーラ, ノクターン, ノチ, ヌヌ, オラフ, オリアナ, オーン, パンテオン, ポッピー, パイク, キヤナ, クイン, ラカン, ラムス, レク＝サイ, レル, レナータ・グラスク, レネクトン, レンガー, リヴェン, ランブル, ライズ, サミーラ, セジュアニ, セナ, セラフィーン, セット, シャコ, シェン, シヴァーナ, シンジド, サイオン, シヴィア, スカーナー, スモルダー, ソナ, ソラカ, スウェイン, サイラス, シンドラ, タム・ケンチ, タムケン, タリヤ, タロン, タリック, ティーモ, スレッシュ, トリスターナ, トランドル, トリンダメア, トリン, ツイステッド・フェイト, トゥイッチ, ウディア, アーゴット, ヴァルス, ヴェイン, ヴェイガー, ヴェル＝コズ, ヴェックス, ヴァイ, ヴィエゴ, ビエゴ, ビクター, ブラッドミア, ヴォリベア, ワーウィック, ウーコン, ザヤ, ゼラス, シン・ジャオ, ヤスオ, ヨネ, ヨリック, ユーミ, ザック, ゼド, ゼリ, ジグス, ジリアン, ゾーイ, ザイラ, ザーヘン, メル, アンベッサ, "
    "ゾーニャ, ショウジン, ステラック, ガーディアンエンジェル, デスダンス, ラバドン, ルーデン, ライアンドリー, 仮面, グイーンソー, 無限の大剣, インフィニティ, ルナーン, スタティック, ラピッドファイア, ティアマト, ハイドラ, 脅威, セリルダ, ハルブレイカー, 騎士の誓い, ソラリ, シュレリア, ミカエル, 香炉, ドランボウ, ドランヘルム, グラトナスグレイブ, 貪欲なグレイブ, 暁と黄昏, "
    "ガンク, インベード, リーシュ, カイト, ハラス, トレード, フリーズ, プッシュ, ローム, ダイブ, カバー, TP, フラッシュ, ウルト, スマイト, ヘラルド, バロン, ドラゴン, エルダー, 阻害, 重傷, クリティカル, ライフスティール, ペネトレーション, アーマー, 魔法防御, MR, FF, GG, SS, MIA, キャリー, トロール, 対面, 寄る, WASD, WASD移動, シーズン2, パッチ26"
)


# ---------------------------------------------------------------------------
# 音声出力
# ---------------------------------------------------------------------------

class VoiceOutput:
    """
    VOICEVOX を使ってテキストを音声に変換し再生する。
    VOICEVOX が起動していない場合はコンソール出力にフォールバック。
    """

    def __init__(self, speaker_id: int = DEFAULT_SPEAKER_ID) -> None:
        self.speaker_id = speaker_id
        self.volume = DEFAULT_VOLUME
        self._lock = threading.Lock()
        self._cancel_flag = False
        self._voicevox_available: bool | None = None
        self.is_speaking: bool = False   # 再生中フラグ（アバター口パク用）
        logger.info("VoiceOutput initialized (speaker_id=%d)", speaker_id)

    def set_speaker(self, speaker_id: int) -> None:
        """キャラクター切替時に VOICEVOX の話者を変更する"""
        self.speaker_id = speaker_id
        logger.info("VOICEVOX speaker changed to %d", speaker_id)

    def set_volume(self, volume: float) -> None:
        """再生音量を変更する (0.0 〜 2.0 推奨)"""
        self.volume = volume
        logger.info("VOICEVOX volume changed to %.2f", volume)

    def stop(self) -> None:
        """再生中の音声を即座にキャンセルする（Barge-in対応）"""
        self._cancel_flag = True
        logger.debug("VoiceOutput: playback cancel requested")

    def speak(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            audio = self._synthesize(text)
            if audio:
                self._play_wav(audio)
            else:
                print(f"[Voice] {text}")

    def _synthesize(self, text: str) -> bytes | None:
        try:
            q = httpx.post(
                f"{VOICEVOX_BASE_URL}/audio_query",
                params={"text": text, "speaker": self.speaker_id},
                timeout=REQUEST_TIMEOUT_QUERY,
            )
            if q.status_code != 200:
                logger.error("audio_query failed: HTTP %d", q.status_code)
                return None
            
            # 音量を反映
            query_data = q.json()
            query_data["volumeScale"] = self.volume

            s = httpx.post(
                f"{VOICEVOX_BASE_URL}/synthesis",
                params={"speaker": self.speaker_id},
                json=query_data,
                timeout=REQUEST_TIMEOUT_SYNTH,
            )
            if s.status_code != 200:
                logger.error("synthesis failed: HTTP %d", s.status_code)
                return None

            self._voicevox_available = True
            return s.content

        except httpx.ConnectError:
            if self._voicevox_available is not False:
                logger.warning("VOICEVOX not running — falling back to text output.")
            self._voicevox_available = False
            return None
        except Exception as e:
            logger.error("VOICEVOX error: %s", e)
            return None

    def _play_wav(self, wav_bytes: bytes) -> None:
        if not _PYAUDIO_AVAILABLE:
            logger.warning("pyaudio not installed — skipping playback")
            return
        try:
            buf = io.BytesIO(wav_bytes)
            with wave.open(buf, "rb") as wf:
                p = pyaudio.PyAudio()
                st = p.open(
                    format=p.get_format_from_width(wf.getsampwidth()),
                    channels=wf.getnchannels(),
                    rate=wf.getframerate(),
                    output=True,
                )
                self._cancel_flag = False  # 新しい再生開始時にリセット
                self.is_speaking = True
                data = wf.readframes(AUDIO_CHUNK_SIZE)
                while data:
                    if self._cancel_flag:
                        logger.debug("VoiceOutput: playback interrupted (barge-in)")
                        break
                    st.write(data)
                    data = wf.readframes(AUDIO_CHUNK_SIZE)
                st.stop_stream()
                st.close()
                p.terminate()
        except Exception as e:
            logger.error("Audio playback error: %s", e)
        finally:
            self.is_speaking = False


# ---------------------------------------------------------------------------
# 音声入力（faster-whisper）
# ---------------------------------------------------------------------------

class VoiceInput:
    """
    PyAudio でマイクから音声を取得し、faster-whisper でローカル認識する。

    Google STT との違い:
      - オフライン動作（ネット不要）
      - 認識精度が高い（small モデルで実用レベル）
      - 初回起動時にモデルロードで数秒かかる
    """

    def __init__(self, model_size: str = WHISPER_MODEL_SIZE, device_index: int | None = None) -> None:
        self._model: "WhisperModel | None" = None
        self._pa: "pyaudio.PyAudio | None" = None
        self._mic_available = False
        self._device_index = device_index
        self._init(model_size)

    def _init(self, model_size: str) -> None:
        if not _WHISPER_AVAILABLE:
            logger.warning("faster-whisper not installed. Run: pip install faster-whisper")
            return
        if not _PYAUDIO_AVAILABLE:
            logger.warning("pyaudio not installed.")
            return
        try:
            logger.info("Loading Whisper model '%s' (first run may take a moment)...", model_size)
            # CUDA 優先、失敗時は CPU にフォールバック
            try:
                self._model = WhisperModel(model_size, device="cuda", compute_type="float16")
                logger.info("VoiceInput (faster-whisper/%s) on CUDA/float16 ready", model_size)
            except Exception as cuda_err:
                logger.warning("CUDA unavailable (%s) — falling back to CPU/int8", cuda_err)
                self._model = WhisperModel(model_size, device="cpu", compute_type="int8")
                logger.info("VoiceInput (faster-whisper/%s) on CPU/int8 ready", model_size)
            self._pa = pyaudio.PyAudio()
            self._mic_available = True
        except Exception as e:
            logger.error("VoiceInput init error: %s", e)

    def listen(self, timeout: float = 3.0, phrase_time_limit: float = 8.0) -> str | None:
        """
        マイクから音声を取得し、認識テキストを返す。

        手順:
          1. 発話開始を最大 timeout 秒待つ
          2. 発話区間を録音（最大 phrase_time_limit 秒）
          3. SILENCE_FRAMES 連続の無音で録音終了
          4. faster-whisper で転写
        """
        if not self._mic_available or self._model is None or self._pa is None:
            return None

        stream = None
        try:
            stream = self._pa.open(
                format=pyaudio.paFloat32,
                channels=1,
                rate=WHISPER_RATE,
                input=True,
                frames_per_buffer=WHISPER_CHUNK,
                input_device_index=self._device_index,
            )

            frames: list[bytes] = []
            speech_started = False
            silence_count  = 0
            wait_count     = 0
            timeout_frames = int(timeout * WHISPER_RATE / WHISPER_CHUNK)
            max_frames     = int(phrase_time_limit * WHISPER_RATE / WHISPER_CHUNK)

            while True:
                data    = stream.read(WHISPER_CHUNK, exception_on_overflow=False)
                samples = np.frombuffer(data, dtype=np.float32)
                rms     = float(np.sqrt(np.mean(samples ** 2)))

                if not speech_started:
                    wait_count += 1
                    if wait_count > timeout_frames:
                        return None   # 発話なしでタイムアウト
                    if rms > SILENCE_RMS:
                        speech_started = True
                        frames.append(data)
                else:
                    frames.append(data)
                    if rms < SILENCE_RMS:
                        silence_count += 1
                        if silence_count >= SILENCE_FRAMES:
                            break     # 無音が続いたので発話終了
                    else:
                        silence_count = 0
                    if len(frames) >= max_frames:
                        break         # 最大録音時間に達した

            if not frames:
                return None

            audio = np.frombuffer(b"".join(frames), dtype=np.float32)
            segments, _ = self._model.transcribe(
                audio,
                language="ja",
                beam_size=5,
                vad_filter=True,   # faster-whisper 内蔵 VAD で誤認識抑制
                initial_prompt=LOL_INITIAL_PROMPT,
                vad_parameters={"min_speech_duration_ms": 100},
            )
            text = "".join(s.text for s in segments).strip()
            if text:
                logger.info("Recognized: %s", text)
                return text
            return None

        except Exception as e:
            logger.error("VoiceInput.listen error: %s", e)
            return None
        finally:
            if stream:
                stream.stop_stream()
                stream.close()

    def __del__(self) -> None:
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass

    @property
    def available(self) -> bool:
        return self._mic_available
