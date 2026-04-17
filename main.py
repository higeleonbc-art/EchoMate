"""
main.py - EchoMate メインスクリプト（v3 - 良き隣人システム）

変更点（v3）:
  - PatronDB:       会話ログをSQLiteに記録（最大1000件・ローテーション）
  - UserProfile:    ユーザー特性をJSONで永続管理
  - PatronAnalyzer: 50〜100件蓄積時にバッチ分析してプロファイルを更新
  - ObserverModule: 依存誘導・ロマンチック表現のフィルタリング・ストレス検知
  - AICompanion:    UserProfileをRAG注入（プロファイルに基づいた応答調整）
  - セッション終了時に分析を同期実行してプロファイルを最終更新

アーキテクチャ:
  VoiceInputThread ──────────────────────────────────────┐
  EventGeneratorThread ──────────────────────────────────┤
  CVDetectorThread ──────────────────────────────────────┤→ EventQueue
  AudioDetectorThread ───────────────────────────────────┘
                                    │
                           EventProcessorThread
                           ↓                 ↓
                      StateManager       AICompanion ← UserProfile（RAG）
                                              ↓
                               MiniConversationManager
                               (step2: +5s, step3: +10s)
                                              ↓
                                       ObserverModule（安全フィルター）
                                              ↓
                                       VoiceOutput
                                       PatronDB（ログ記録）
"""

import json
import logging
import logging.handlers
import re
import sys
import threading
import time
import random
import argparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

from event import EventManager, GameEvent, generate_dummy_event
from input_monitor import InputMonitor
from typing import Callable, Optional
from ai import AICompanion
from voice import VoiceOutput, VoiceInput
from opencv_detector import OpenCVDetector
from audio_detector import AudioDetector
from vision_analyzer import VisionAnalyzer, VISION_EVENT_PROMPT
from state_manager import StateManager
from patron_db import PatronDB
from user_profile import UserProfile
from patron_analyzer import PatronAnalyzer
from observer import ObserverModule
from sentiment_analyzer import analyze as analyze_sentiment

# ---------------------------------------------------------------------------
# ロギング
# ---------------------------------------------------------------------------

def _setup_logging(level: int = logging.INFO) -> None:
    """
    ログを標準出力とファイルの両方に出力する。

    デフォルトは INFO レベル。
    echomate.log にはプレイヤーの発言・イベントが記録される。
    ファイルを他者と共有する際はプライバシーに注意すること。
    デバッグ時は --debug フラグで DEBUG レベルに切り替える。

    RotatingFileHandler により echomate.log は最大 10MB × 5世代で
    自動ローテーションされ、無限肥大化を防ぐ。
    """
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    rotating = logging.handlers.RotatingFileHandler(
        "echomate.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
        errors="replace",
    )
    # Windows コンソールの文字化け防止
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            rotating,
        ],
    )


# ---------------------------------------------------------------------------
# ミニ会話マネージャー
# ---------------------------------------------------------------------------

class MiniConversationManager:
    """
    1 イベントにつき最大 3 ステップの発話を時間差で行う。

      step1: get_reaction() → イベント処理時に即座に実行（呼び出し元が担当）
      step2: get_followup(step=2) → STEP2_DELAY 秒後に自動実行
      step3: get_followup(step=3) → STEP3_DELAY 秒後に自動実行
    """

    STEP2_DELAY = 5.0   # 秒
    STEP3_DELAY = 10.0  # 秒

    def __init__(self, ai: AICompanion, speak_fn) -> None:
        self.ai = ai
        self.speak_fn = speak_fn
        self._active: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, event: GameEvent, state_snapshot: dict, memory: dict) -> None:
        """イベントのミニ会話を開始し、step2/3 をタイマーで予約する"""
        payload = {
            "event_type":     event.event_type,
            "state_snapshot": state_snapshot,
            "memory":         memory.copy(),
        }
        with self._lock:
            self._active[event.id] = payload

        threading.Timer(self.STEP2_DELAY, self._fire_step, args=(event.id, 2)).start()
        threading.Timer(self.STEP3_DELAY, self._fire_step, args=(event.id, 3)).start()

    def is_busy(self) -> bool:
        """未処理のミニ会話ステップが残っている場合 True を返す（スロットリング用）"""
        with self._lock:
            return len(self._active) > 0

    def cancel_all(self) -> None:
        """全アクティブなミニ会話をキャンセルする（プレイヤーが集中し始めたら空気を読む）"""
        with self._lock:
            count = len(self._active)
            self._active.clear()
        if count:
            logger.debug("MiniConversation: %d active step(s) cancelled", count)

    def _fire_step(self, event_id: str, step: int) -> None:
        with self._lock:
            payload = self._active.get(event_id)
        if payload is None:
            return

        text = self.ai.get_followup(
            payload["event_type"],
            step,
            payload["state_snapshot"],
            payload["memory"],
        )
        print(f"\n[EchoMate] {text}")
        self.speak_fn(text)

        if step >= 3:
            with self._lock:
                self._active.pop(event_id, None)


# ---------------------------------------------------------------------------
# EchoMate 本体
# ---------------------------------------------------------------------------

class EchoMate:
    """ゲーム相棒 AI のメインクラス。スレッドを管理しイベントドリブンで動作する。"""

    DUMMY_EVENT_INTERVAL_MIN  = 5.0
    DUMMY_EVENT_INTERVAL_MAX  = 15.0
    SILENCE_THRESHOLD         = 45.0
    PROACTIVE_COOLDOWN        = 120.0
    PROACTIVE_CHECK_INTERVAL  = 5.0
    STATE_TICK_INTERVAL       = 3.0   # StateManager.tick() の呼び出し間隔（秒）
    PROACTIVE_VISION_INTERVAL = 20.0  # 自発的 VLM 解析の間隔（秒）
    # イベント種別ごとのクールダウン（秒）。連続発生しやすい kill/death は短め
    _EVENT_COOLDOWNS: dict = {
        "kill":     5.0,
        "death":    5.0,
        "low_hp":  10.0,
        "big_play": 10.0,
    }
    _EVENT_COOLDOWN_DEFAULT = 10.0

    def __init__(
        self,
        character: str = "kid",
        enable_cv: bool = True,
        enable_audio: bool = True,
        enable_dummy: bool = False,
        enable_vision: bool = True,
        speech_callback: Optional[Callable[[str], None]] = None,
        voice_input_device: Optional[int] = None,    # 音声認識用マイク
        audio_detect_device: Optional[int] = None,   # ゲームイベント検知用（ループバック可）
        vision_monitor_index: int = 1,               # Vision 解析対象モニター（1=プライマリ）
        audio_target_pid: Optional[int] = None,      # 音声キャプチャ対象プロセス PID
        audio_exclude_pids: Optional[list] = None,   # 音声キャプチャ除外 PID リスト
    ) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)

        # コアコンポーネント
        self.event_manager = EventManager()
        self.state_manager = StateManager()
        self.ai            = AICompanion()
        self.voice_output  = VoiceOutput()
        self.voice_input   = VoiceInput(device_index=voice_input_device)

        # 良き隣人システム
        self.patron_db   = PatronDB()
        self.user_profile = UserProfile(patron_db=self.patron_db)
        self.analyzer    = PatronAnalyzer(self.patron_db, self.user_profile)
        self.observer    = ObserverModule(self.user_profile)

        # AIにプロファイルを接続（RAG）
        self.ai.set_user_profile(self.user_profile)

        # キャラクター適用
        self._apply_character(character)

        # ミニ会話マネージャー
        self.mini_conv = MiniConversationManager(self.ai, self._speak_async)

        # 検出器
        self.cv_detector    = OpenCVDetector(self.event_manager) if enable_cv    else None
        self.audio_detector = AudioDetector(self.event_manager, device_index=audio_detect_device, voice_output=self.voice_output, target_pid=audio_target_pid, exclude_pids=audio_exclude_pids) if enable_audio else None
        self.vision_analyzer = VisionAnalyzer(monitor_index=vision_monitor_index) if enable_vision else None

        # VisionAnalyzer と AudioDetector を音トリガー型で連携
        if self.audio_detector and self.vision_analyzer:
            self.audio_detector.vision_trigger_fn = self._on_audio_spike
            self.logger.info("Audio+Vision hybrid event detection enabled")

        # デバッグ・コールバック
        self.enable_dummy      = enable_dummy
        self._speech_callback  = speech_callback

        # 操作強度モニタリング
        self.input_monitor = InputMonitor(
            event_callback=self.state_manager.record_input_event
        )

        # タイミング管理
        self.running                   = False
        self._last_speech_time         = time.time()
        self._last_proactive_time      = 0.0
        self._last_proactive_vision    = 0.0
        self._event_cooldowns: dict[str, float] = {}
        self._threads: list[threading.Thread] = []

        self.event_manager.load_memory()
        self.logger.info("EchoMate initialized (character=%s)", character)

    def _apply_character(self, name: str) -> None:
        """キャラクターを設定し、VOICEVOX 話者を連動させる"""
        char = self.ai.set_character(name)
        speaker_id = char.get("voicevox", {}).get("speaker_id")
        if speaker_id is not None:
            self.voice_output.set_speaker(speaker_id)

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------

    def start(self) -> None:
        """起動してメインスレッドをブロックする（CLI用）"""
        self._start_threads()
        try:
            while self.running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n\nStopping EchoMate...")
            self.stop()

    def start_background(self) -> None:
        """GUI から呼び出す非ブロッキング起動。スレッドを開始して即座に返る。"""
        self._start_threads()

    def _check_game_change(self) -> None:
        """
        cv_config.json の _generated_for とプロファイルの current_game を比較し、
        ゲームが変わっていればメタ挨拶を生成・再生する。
        """
        try:
            with open("cv_config.json", encoding="utf-8") as f:
                cv_cfg = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        new_game = cv_cfg.get("_generated_for", "").strip()
        if not new_game:
            return

        old_game = self.user_profile.get_current_game()

        if old_game != new_game:
            self.logger.info(
                "Game change detected: '%s' → '%s'", old_game, new_game
            )
            greeting = self.ai.get_game_change_greeting(old_game, new_game)
            print(f"\n[EchoMate→] {greeting}")
            self._speak_async(greeting)
            self._log_interaction(
                event_type="game_change",
                ai_response=greeting,
                tags=["game_change"],
                emotion_score=0.2,
            )
            self.user_profile.set_current_game(new_game)
            self.user_profile.save()

    def _start_threads(self) -> None:
        """スレッドと検出器を起動する共通処理"""
        self.running = True
        self._check_game_change()
        self._print_banner()

        thread_targets = [
            ("VoiceInput",     self._voice_input_loop),
            ("EventProcessor", self._event_processor_loop),
            ("ProactiveChat",  self._proactive_loop),
            ("StateTick",      self._state_tick_loop),
        ]
        # ダミーイベントは --debug フラグが指定された場合のみ起動
        if self.enable_dummy:
            thread_targets.insert(1, ("EventGenerator", self._event_generator_loop))
            self.logger.info("Debug mode: dummy event generator enabled")

        for name, target in thread_targets:
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

        if self.cv_detector and self.cv_detector.is_available():
            self.cv_detector.start()
            print("[CV]    OpenCV screen detection enabled")
        elif self.cv_detector:
            print("[CV]    mss not installed — screen detection disabled")

        if self.audio_detector and self.audio_detector.is_available():
            self.audio_detector.start()
            print("[Audio] Audio spike detection enabled")

        if self.vision_analyzer and self.vision_analyzer.is_available():
            self.vision_analyzer.start()
            print(f"[Vision] Screen analysis enabled (model={self.vision_analyzer.model})")
        elif self.vision_analyzer:
            print("[Vision] mss or Pillow not installed — screen analysis disabled")
        elif self.audio_detector:
            print("[Audio] pyaudio not installed — audio detection disabled")

        if self.input_monitor.available:
            self.input_monitor.start()
            print("[Input]  Player input intensity monitoring enabled")
        else:
            print("[Input]  pynput not installed — input monitoring disabled")

    def stop(self) -> None:
        self.running = False
        if self.cv_detector:
            self.cv_detector.stop()
        if self.audio_detector:
            self.audio_detector.stop()
        if self.vision_analyzer:
            self.vision_analyzer.stop()
        self.input_monitor.stop()
        self.event_manager.save_memory()

        # セッション終了時の分析・プロファイル更新
        try:
            session_logs = self.patron_db.get_total_log_count()
            self.user_profile.increment_session(session_logs)
            # セッション終了ボーナス親密度（プレイ継続の報酬）
            self.user_profile.add_bond(0.02)
            if self.patron_db.count_unanalyzed() > 0:
                self.logger.info("Running final patron analysis on session end...")
                self.analyzer.analyze_sync()
            else:
                self.user_profile.save()
        except Exception as e:
            self.logger.error("Session-end analysis error: %s", e)

        self.logger.info("EchoMate stopped.")
        print("EchoMate stopped.")

    # ------------------------------------------------------------------
    # スレッドループ
    # ------------------------------------------------------------------

    def _voice_input_loop(self) -> None:
        self.logger.info("VoiceInput loop started (available=%s)", self.voice_input.available)
        while self.running:
            try:
                text = self.voice_input.listen(timeout=3.0, phrase_time_limit=8.0)
                if text:
                    self.logger.info("Player said: %s", text)
                    self.event_manager.add_event(GameEvent("player_speech", {"text": text}))
            except Exception as e:
                self.logger.error("VoiceInput loop error: %s", e)
                time.sleep(1.0)

    def _event_generator_loop(self) -> None:
        self.logger.info("EventGenerator loop started")
        while self.running:
            try:
                time.sleep(random.uniform(self.DUMMY_EVENT_INTERVAL_MIN, self.DUMMY_EVENT_INTERVAL_MAX))
                event = generate_dummy_event()
                self.logger.info("Dummy event: %s", event.event_type)
                self.event_manager.add_event(event)
            except Exception as e:
                self.logger.error("EventGenerator error: %s", e)
                time.sleep(1.0)

    def _event_processor_loop(self) -> None:
        self.logger.info("EventProcessor loop started")
        while self.running:
            try:
                event = self.event_manager.get_event(timeout=0.1)
                if event:
                    self._process_event(event)
            except Exception as e:
                self.logger.error("EventProcessor error: %s", e)
                time.sleep(0.1)

    def _proactive_loop(self) -> None:
        self.logger.info("ProactiveChat loop started (threshold=%.0fs)", self.SILENCE_THRESHOLD)
        while self.running:
            time.sleep(self.PROACTIVE_CHECK_INTERVAL)
            try:
                now            = time.time()

                # 自発的 VLM 解析（PROACTIVE_VISION_INTERVAL ごとに画面を観察）
                if (
                    self.vision_analyzer
                    and self.vision_analyzer.is_available()
                    and now - self._last_proactive_vision >= self.PROACTIVE_VISION_INTERVAL
                ):
                    self._last_proactive_vision = now
                    def _run_proactive_vision() -> None:
                        try:
                            from vision_analyzer import VISION_PROMPT
                            result = self.vision_analyzer.analyze_now(VISION_PROMPT)
                            if result:
                                self.ai.set_vision_context(result)
                                self.logger.debug("Proactive vision updated: %s", result[:60])
                        except Exception as e:
                            self.logger.error("Proactive vision error: %s", e)
                    threading.Thread(
                        target=_run_proactive_vision, daemon=True, name="ProactiveVision"
                    ).start()

                silence        = now - self._last_speech_time
                since_last_pro = now - self._last_proactive_time
                if silence >= self.SILENCE_THRESHOLD and since_last_pro >= self.PROACTIVE_COOLDOWN:
                    # tension が高い（戦闘中）場合は空気を読んで発言しない
                    _state_check = self.state_manager.get_state()
                    _tension = _state_check.to_dict().get("tension", 0.0) if hasattr(_state_check, "to_dict") else 0.0
                    if _tension > 0.6:
                        self.logger.debug("Proactive skipped: tension too high (%.2f)", _tension)
                        continue

                    self.logger.info("Silence %.0fs — triggering proactive message", silence)
                    memory = self.event_manager.get_memory()
                    state  = self.state_manager.get_state()

                    # 画面状況をAIに注入
                    if self.vision_analyzer:
                        self.ai.set_vision_context(self.vision_analyzer.get_context())

                    # 成長観察ヒントを低頻度で注入
                    growth_hint = None
                    if self.observer.should_show_growth_hint():
                        growth_hint = self.observer.get_growth_hint_message()

                    message = self.ai.get_proactive_message(memory, state, growth_hint=growth_hint)

                    # 安全フィルター
                    tension      = state.to_dict().get("tension", 0.0) if hasattr(state, "to_dict") else 0.0
                    stress_score = self.observer.estimate_stress(tension)
                    message      = self.observer.filter_response(message, stress_score)

                    print(f"\n[EchoMate→] {message}")
                    self._last_proactive_time = now
                    self._speak_async(message)

                    # ログ記録
                    self._log_interaction(
                        event_type="proactive",
                        ai_response=message,
                        tags=["proactive"],
                        emotion_score=0.0,
                    )
            except Exception as e:
                self.logger.error("ProactiveChat error: %s", e)

    def _state_tick_loop(self) -> None:
        """テンション減衰など時間依存の状態更新を定期実行する"""
        self.logger.info("StateTick loop started")
        while self.running:
            time.sleep(self.STATE_TICK_INTERVAL)
            try:
                self.state_manager.tick()
            except Exception as e:
                self.logger.error("StateTick error: %s", e)

    # ------------------------------------------------------------------
    # イベント処理
    # ------------------------------------------------------------------

    def _process_event(self, event: GameEvent) -> None:
        if event.event_type == "player_speech":
            self._handle_player_speech(event)
        else:
            self._handle_game_event(event)

    def _handle_player_speech(self, event: GameEvent) -> None:
        player_text = event.data.get("text", "")
        if not player_text:
            return

        # Task3: プレイヤーが話し始めたら現在のAI音声を即座に停止（Barge-in）
        self.voice_output.stop()

        # ── 雑音・相槌のフィルタリング処理 ──
        # 空白や句読点といった記号を除去し、純粋なテキストを抽出
        clean_text = re.sub(r'[、。！？\s]', '', player_text)

        # 除外する相槌のリスト
        ignore_words = {"ん", "うん", "は", "あ", "え","はっ","はっは","はっはっ","はっはっは", "あー", "えー", "ふふ", "あはは", "ははは", "ふーん", "ええ", "おう", "んー", "はは", "なるほど", "そっか"}

        def _get_repeating_unit(text: str) -> str:
            """'ははははは' → 'は' のように最小繰り返し単位を返す"""
            n = len(text)
            for size in range(1, n // 2 + 1):
                if n % size == 0 and text[:size] * (n // size) == text:
                    return text[:size]
            return text

        def _matches_ignore(text: str) -> bool:
            if text in ignore_words:
                return True
            if re.fullmatch(r'[あはひふへほっ]+', text):
                return True
            unit = _get_repeating_unit(text)
            return unit != text and unit in ignore_words

        # 実質1文字以下の文字列、または無視リストに一致する場合はスルー
        if len(clean_text) <= 1 or _matches_ignore(clean_text):
            self.logger.info("Ignored short/filler speech: %s", player_text)
            return
        # ────────────────────────────

        self._last_speech_time = time.time()
        self.logger.info("Player speech: %s", player_text)

        # プレイヤーが話し始めたらミニ会話の続きをキャンセル（空気を読む）
        self.mini_conv.cancel_all()

        memory = self.event_manager.get_memory()
        state  = self.state_manager.get_state()

        # 画面状況をAIに注入
        if self.vision_analyzer:
            self.ai.set_vision_context(self.vision_analyzer.get_context())

        # 成長観察ヒントを低頻度で注入
        growth_hint = None
        if self.observer.should_show_growth_hint():
            growth_hint = self.observer.get_growth_hint_message()

        # Task2: フィラー先行非同期再生（LLM生成遅延を自然に見せる）
        if len(clean_text) >= 10 and random.random() < 0.3:
            self._speak_async(random.choice(["うーん…", "あー、", "なるほど…"]))

        sentiment = analyze_sentiment(player_text)
        response = self.ai.get_response(
            player_text, memory, state,
            growth_hint=growth_hint,
            sentiment_context=sentiment.to_prompt_string(),
        )

        # 安全フィルター（ObserverModule）
        tension      = state.to_dict().get("tension", 0.0) if hasattr(state, "to_dict") else 0.0
        stress_score = self.observer.estimate_stress(tension)
        response     = self.observer.filter_response(response, stress_score)

        print(f"\n{'─' * 40}")
        print(f"[Player]   {player_text}")
        print(f"[EchoMate] {response}")
        print(f"{'─' * 40}")

        self.event_manager.update_memory("player_speech", player_text)
        self._speak_async(response)

        # ログ記録（PatronDB）
        self._log_interaction(
            event_type="player_speech",
            ai_response=response,
            user_input=player_text,
            tags=["speech"],
            emotion_score=0.0,
        )

    # ログ記録とバッチ分析トリガーの共通処理
    _EMOTION_SCORE_MAP: dict = {
        "kill":     0.3,
        "big_play": 0.4,
        "death":   -0.3,
        "low_hp":  -0.2,
    }
    _TAGS_MAP: dict = {
        "kill":     ["game", "kill"],
        "death":    ["game", "death"],
        "low_hp":   ["game", "danger"],
        "big_play": ["game", "achievement"],
        "player_speech": ["speech"],
    }

    def _log_interaction(
        self,
        event_type: str,
        ai_response: str,
        user_input: Optional[str] = None,
        tags: Optional[list] = None,
        emotion_score: float = 0.0,
    ) -> None:
        """PatronDB にログを記録し、必要ならバッチ分析をトリガーする"""
        try:
            self.patron_db.add_log(
                event_type=event_type,
                ai_response=ai_response,
                user_input=user_input,
                tags=tags or self._TAGS_MAP.get(event_type, ["game"]),
                emotion_score=emotion_score,
            )
            if self.patron_db.should_trigger_analysis():
                self.logger.info("Batch analysis triggered (%d unanalyzed logs)",
                                 self.patron_db.count_unanalyzed())
                self.analyzer.analyze_async()
        except Exception as e:
            self.logger.error("PatronDB log error: %s", e)

    def _handle_game_event(self, event: GameEvent) -> None:
        now = time.time()
        cooldown = self._EVENT_COOLDOWNS.get(event.event_type, self._EVENT_COOLDOWN_DEFAULT)
        last = self._event_cooldowns.get(event.event_type, 0.0)
        if now - last < cooldown:
            self.logger.debug(
                "Event '%s' skipped: cooldown (%.1fs remaining)",
                event.event_type,
                cooldown - (now - last),
            )
            return
        self._event_cooldowns[event.event_type] = now

        # 中・低優先度イベント(priority >= 2)はシステムビジー時にスキップ
        if event.priority >= 2 and (
            self.mini_conv.is_busy() or (now - self._last_speech_time < 5.0)
        ):
            self.logger.info(
                "Event '%s' throttled: system busy (mini_conv=%s, speech_gap=%.1fs)",
                event.event_type, self.mini_conv.is_busy(), now - self._last_speech_time,
            )
            return

        self.logger.info("Game event: %s", event.event_type)

        # 状態を更新
        state = self.state_manager.update(event.event_type)

        # 親密度加算（ポジティブなイベント）
        if event.event_type in ("kill", "big_play"):
            self.user_profile.add_bond(0.01)

        # デス記録（ストレス推定に使用）
        if event.event_type == "death":
            self.observer.record_death()

        # step1: 即時リアクション
        reaction = self.ai.get_reaction(event.event_type, state, use_template=False)

        # 安全フィルター
        tension      = state.to_dict().get("tension", 0.0) if hasattr(state, "to_dict") else 0.0
        stress_score = self.observer.estimate_stress(tension)
        reaction     = self.observer.filter_response(reaction, stress_score)

        label = {
            "kill": "KILL", "death": "DEATH",
            "low_hp": "LOW HP", "big_play": "BIG PLAY",
        }.get(event.event_type, event.event_type.upper())

        print(f"\n[{label}] → {reaction}")
        self._speak_async(reaction)

        # メモリ更新
        self.event_manager.update_memory(event.event_type)
        tendency = self.ai.get_tendency_label(event.event_type)
        if tendency:
            self.event_manager.update_player_tendency(tendency)

        # step2 / step3: ミニ会話予約（player_speech は対象外）
        memory = self.event_manager.get_memory()
        self.mini_conv.start(event, state.to_dict(), memory)

        # ログ記録（PatronDB）
        self._log_interaction(
            event_type=event.event_type,
            ai_response=reaction,
            emotion_score=self._EMOTION_SCORE_MAP.get(event.event_type, 0.0),
        )

    # ------------------------------------------------------------------
    # ユーティリティ
    # ------------------------------------------------------------------

    def _on_audio_spike(self, event_type: str, rms: float, spike: float) -> None:
        """
        音スパイク検知時にVLMで画面確認し、イベントを判定して発火する。
        AudioDetector の vision_trigger_fn として登録される。
        VLM呼び出しは別スレッドで非同期実行するので音声キャプチャを止めない。
        """
        self.logger.debug(
            "Audio spike received (hint=%s, rms=%.3f, spike=%.3f) — checking screen",
            event_type, rms, spike,
        )

        def _check() -> None:
            try:
                result = self.vision_analyzer.analyze_now(VISION_EVENT_PROMPT)
                if not result:
                    # VLM が完全失敗 → 話題フォールバックで沈黙を防ぐ
                    if self.vision_analyzer.should_use_topic_fallback():
                        fallback = self.vision_analyzer.get_fallback_topic()
                        self.logger.info("VLM unavailable — using topic fallback: %s", fallback)
                        self._speak_async(fallback)
                    return
                upper = result.strip().upper()
                if "KILL" in upper:
                    detected = "kill"
                elif "LOW_HP" in upper or "LOW HP" in upper:
                    detected = "low_hp"
                elif "BIG_PLAY" in upper or "BIG PLAY" in upper:
                    detected = "big_play"
                else:
                    self.logger.debug("Vision: no event confirmed (raw=%s)", result[:60])
                    return
                self.logger.info("Vision confirmed event: %s (rms=%.3f)", detected, rms)
                self.event_manager.add_event(
                    GameEvent(detected, {"source": "audio+vision", "rms": round(rms, 4)})
                )
            except Exception as e:
                self.logger.error("Audio spike vision check error: %s", e)

        threading.Thread(target=_check, daemon=True, name="VisionCheck").start()

    def _speak_async(self, text: str) -> None:
        # GUI 吹き出し UI へテキストを通知する
        if self._speech_callback:
            try:
                self._speech_callback(text)
            except Exception as e:
                self.logger.debug("speech_callback error: %s", e)

        # Task5: 感情別VOICEVOXスピーカー選択
        voicevox_cfg = self.ai.current_character.get("voicevox", {})
        emotion_speakers = voicevox_cfg.get("emotion_speakers", {})
        normal_id = voicevox_cfg.get("speaker_id")
        speaker_id = normal_id

        if emotion_speakers and normal_id is not None:
            try:
                state = self.state_manager.get_state()
                tension = state.to_dict().get("tension", 0.5) if hasattr(state, "to_dict") else 0.5
            except Exception:
                tension = 0.5
            if tension >= 0.6:
                speaker_id = emotion_speakers.get("high", normal_id)
            elif tension <= 0.3:
                speaker_id = emotion_speakers.get("low", normal_id)
            else:
                speaker_id = emotion_speakers.get("normal", normal_id)

        def _speak_with_emotion() -> None:
            if speaker_id is not None and speaker_id != self.voice_output.speaker_id:
                self.voice_output.set_speaker(speaker_id)
            self.voice_output.speak(text)
            # 再生完了後にnormal IDへ戻す
            if normal_id is not None and self.voice_output.speaker_id != normal_id:
                self.voice_output.set_speaker(normal_id)

        threading.Thread(
            target=_speak_with_emotion,
            daemon=True,
            name="VoiceOutput",
        ).start()

    def _print_banner(self) -> None:
        char_name = self.ai.current_character.get("name", "?")
        print("=" * 50)
        print(f"  EchoMate v2 — {char_name} が相棒です")
        print("=" * 50)
        print("Voice input active. Speak or wait for events.")
        print("Press Ctrl+C to stop.\n")


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="EchoMate - Game AI Companion")
    p.add_argument(
        "--character", "-c",
        default="kid",
        choices=["michiko", "kid", "rei", "ryu", "akane", "echo"],
        help="使用するキャラクター（デフォルト: kid）",
    )
    p.add_argument("--no-cv",    action="store_true", help="OpenCV 検出を無効化")
    p.add_argument("--no-audio", action="store_true", help="音声検出を無効化")
    p.add_argument(
        "--debug",
        action="store_true",
        help="DEBUG レベルのログを有効化（echomate.log に詳細を記録）",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    log_level = logging.DEBUG if args.debug else logging.INFO
    _setup_logging(level=log_level)
    companion = EchoMate(
        character=args.character,
        enable_cv=not args.no_cv,
        enable_audio=not args.no_audio,
        enable_dummy=args.debug,
    )
    companion.start()


if __name__ == "__main__":
    main()
