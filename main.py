"""
main.py - EchoMate メインスクリプト

ゲーム中に動作する相棒 AI ツール。
プレイヤーの発言とゲームイベントに反応し、
リアルタイムで短いリアクションと軽い会話を返す。

起動方法:
    python main.py

前提:
    - Ollama が起動済みで指定モデルがインストール済みであること
    - VOICEVOX が起動済みであること（なければテキスト出力のみ）
    - マイクが接続済みであること（なければキーボード入力にフォールバック）
    - OpenCV 検出を使う場合は pip install opencv-python mss

アーキテクチャ:
    ┌──────────────────────────────────────────────────┐
    │  VoiceInputThread   EventGeneratorThread         │
    │       │                    │                    │
    │  CVDetectorThread ──────────┤ (OpenCV, optional) │
    │                             │                    │
    │                   EventQueue (PriorityQueue)     │
    │                             │                    │
    │                    EventProcessorThread          │
    │                    │              │             │
    │               AICompanion    VoiceOutput        │
    └──────────────────────────────────────────────────┘
"""

import logging
import sys
import threading
import time
import random

from event import EventManager, GameEvent, generate_dummy_event
from ai import AICompanion
from voice import VoiceOutput, VoiceInput
from opencv_detector import OpenCVDetector
from audio_detector import AudioDetector

# ---------------------------------------------------------------------------
# ロギング設定
# ---------------------------------------------------------------------------

def _setup_logging(level: int = logging.DEBUG) -> None:
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("echomate.log", encoding="utf-8"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


# ---------------------------------------------------------------------------
# EchoMate 本体
# ---------------------------------------------------------------------------

class EchoMate:
    """
    ゲーム相棒 AI のメインクラス。
    スレッドを管理しイベントドリブンで動作する。
    """

    DUMMY_EVENT_INTERVAL_MIN = 5.0   # ダミーイベント最小間隔（秒）
    DUMMY_EVENT_INTERVAL_MAX = 15.0  # ダミーイベント最大間隔（秒）

    SILENCE_THRESHOLD   = 45.0   # この秒数以上無言なら話題を振る
    PROACTIVE_COOLDOWN  = 120.0  # 話題振りの最小間隔（連投防止）
    PROACTIVE_CHECK_INTERVAL = 5.0  # 無言チェックの頻度（秒）

    def __init__(self, enable_cv: bool = True, enable_audio: bool = True) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.event_manager = EventManager()
        self.ai = AICompanion()
        self.voice_output = VoiceOutput()
        self.voice_input = VoiceInput()
        self.cv_detector    = OpenCVDetector(self.event_manager) if enable_cv    else None
        self.audio_detector = AudioDetector(self.event_manager)  if enable_audio else None
        self.running = False
        self._threads: list[threading.Thread] = []
        self._last_speech_time    = time.time()  # 最後にプレイヤーが発話した時刻
        self._last_proactive_time = 0.0          # 最後に話題を振った時刻

        # 起動時にメモリを復元
        self.event_manager.load_memory()
        self.logger.info("EchoMate initialized (cv=%s, audio=%s)", enable_cv, enable_audio)

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------

    def start(self) -> None:
        """EchoMate を起動する"""
        self.running = True
        self._print_banner()

        thread_specs = [
            ("VoiceInput",      self._voice_input_loop),
            ("EventGenerator",  self._event_generator_loop),
            ("EventProcessor",  self._event_processor_loop),
            ("ProactiveChat",   self._proactive_loop),
        ]
        for name, target in thread_specs:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
            self.logger.info("Thread started: %s", name)

        # OpenCV 検出器を起動（ライブラリがあれば）
        if self.cv_detector and self.cv_detector.is_available():
            self.cv_detector.start()
            print("[CV]    OpenCV screen detection enabled")
        elif self.cv_detector:
            print("[CV]    mss not installed — screen detection disabled (pip install mss opencv-python)")

        # 音声検出器を起動（pyaudio があれば）
        if self.audio_detector and self.audio_detector.is_available():
            self.audio_detector.start()
            print("[Audio] Audio spike detection enabled")
        elif self.audio_detector:
            print("[Audio] pyaudio not installed — audio detection disabled")

        try:
            while self.running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n\nStopping EchoMate...")
            self.stop()

    def stop(self) -> None:
        """EchoMate を停止してメモリを保存する"""
        self.running = False
        if self.cv_detector:
            self.cv_detector.stop()
        if self.audio_detector:
            self.audio_detector.stop()
        self.event_manager.save_memory()
        self.logger.info("EchoMate stopped. Memory saved.")
        print("EchoMate stopped.")

    # ------------------------------------------------------------------
    # スレッドループ
    # ------------------------------------------------------------------

    def _voice_input_loop(self) -> None:
        """音声入力を監視し、発話があればイベントキューに追加する"""
        self.logger.info("VoiceInput loop started (mic_available=%s)", self.voice_input.available)

        while self.running:
            try:
                text = self.voice_input.listen(timeout=3.0, phrase_time_limit=5.0)
                if text:
                    self.logger.info("Player said: %s", text)
                    event = GameEvent("player_speech", {"text": text})
                    self.event_manager.add_event(event)
            except Exception as e:
                self.logger.error("VoiceInput loop error: %s", e)
                time.sleep(1.0)

    def _event_generator_loop(self) -> None:
        """ダミーのゲームイベントをランダムな間隔で生成する"""
        self.logger.info("EventGenerator loop started")

        while self.running:
            try:
                interval = random.uniform(
                    self.DUMMY_EVENT_INTERVAL_MIN,
                    self.DUMMY_EVENT_INTERVAL_MAX,
                )
                time.sleep(interval)

                event = generate_dummy_event()
                self.logger.info("Game event generated: %s", event.event_type)
                self.event_manager.add_event(event)

            except Exception as e:
                self.logger.error("EventGenerator loop error: %s", e)
                time.sleep(1.0)

    def _event_processor_loop(self) -> None:
        """イベントキューを監視し、優先度順に処理する"""
        self.logger.info("EventProcessor loop started")

        while self.running:
            try:
                event = self.event_manager.get_event(timeout=0.1)
                if event:
                    self._process_event(event)
            except Exception as e:
                self.logger.error("EventProcessor loop error: %s", e)
                time.sleep(0.1)

    # ------------------------------------------------------------------
    # イベント処理
    # ------------------------------------------------------------------

    def _process_event(self, event: GameEvent) -> None:
        """単一イベントを処理して AI の応答を生成・出力する"""
        if event.event_type == "player_speech":
            self._handle_player_speech(event)
        else:
            self._handle_game_event(event)

    def _proactive_loop(self) -> None:
        """
        プレイヤーの無言時間を監視し、一定時間を超えたら自発的に話題を振る。

        SILENCE_THRESHOLD 秒以上発話がなく、
        かつ前回の話題振りから PROACTIVE_COOLDOWN 秒以上経過していれば発火。
        """
        self.logger.info("ProactiveChat loop started (threshold=%.0fs)", self.SILENCE_THRESHOLD)

        while self.running:
            time.sleep(self.PROACTIVE_CHECK_INTERVAL)
            try:
                now = time.time()
                silence        = now - self._last_speech_time
                since_last_pro = now - self._last_proactive_time

                if silence >= self.SILENCE_THRESHOLD and since_last_pro >= self.PROACTIVE_COOLDOWN:
                    self.logger.info("Silence %.0fs — triggering proactive message", silence)
                    memory = self.event_manager.get_memory()
                    message = self.ai.get_proactive_message(memory)

                    print(f"\n[EchoMate→] {message}")
                    self._last_proactive_time = now
                    self._speak_async(message)

            except Exception as e:
                self.logger.error("ProactiveChat loop error: %s", e)

    def _handle_player_speech(self, event: GameEvent) -> None:
        """プレイヤー発言への会話応答を生成する"""
        player_text = event.data.get("text", "")
        if not player_text:
            return

        self.logger.info("Handling player speech: %s", player_text)
        self._last_speech_time = time.time()  # 無言タイマーをリセット
        memory = self.event_manager.get_memory()

        response = self.ai.get_response(player_text, memory)

        print(f"\n{'─' * 40}")
        print(f"[Player]   {player_text}")
        print(f"[EchoMate] {response}")
        print(f"{'─' * 40}")

        # メモリ更新
        self.event_manager.update_memory("player_speech", player_text)

        # 音声出力（非同期）
        self._speak_async(response)

    def _handle_game_event(self, event: GameEvent) -> None:
        """ゲームイベントへの即時リアクションを生成する"""
        self.logger.info("Handling game event: %s", event.event_type)

        reaction = self.ai.get_reaction(event.event_type, use_template=True)

        event_label = {
            "kill":     "KILL",
            "death":    "DEATH",
            "low_hp":   "LOW HP",
            "big_play": "BIG PLAY",
        }.get(event.event_type, event.event_type.upper())

        print(f"\n[{event_label}] → {reaction}")

        # メモリ更新
        self.event_manager.update_memory(event.event_type)
        tendency = self.ai.get_tendency_label(event.event_type)
        if tendency:
            self.event_manager.update_player_tendency(tendency)

        # 音声出力（非同期）
        self._speak_async(reaction)

    # ------------------------------------------------------------------
    # ユーティリティ
    # ------------------------------------------------------------------

    def _speak_async(self, text: str) -> None:
        """音声出力を別スレッドで非同期実行する"""
        t = threading.Thread(
            target=self.voice_output.speak,
            args=(text,),
            daemon=True,
            name="VoiceOutput",
        )
        t.start()

    @staticmethod
    def _print_banner() -> None:
        print("=" * 50)
        print("  EchoMate - Game AI Companion")
        print("=" * 50)
        print("Voice input active. Speak or wait for events.")
        print("Press Ctrl+C to stop.\n")


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def main() -> None:
    _setup_logging(level=logging.DEBUG)
    companion = EchoMate()
    companion.start()


if __name__ == "__main__":
    main()
