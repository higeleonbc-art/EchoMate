"""
ai.py - AI処理モジュール

キャラクタープロファイルに基づいて Ollama LLM を呼び出し、
リアクション・会話応答・ミニ会話フォローアップを生成する。

主な変更点（v2）:
  - キャラクターシステム: characters.json からプロファイルをロード
  - 状態連携: PlayerState をプロンプトに組み込む
  - validate_response: 文字列検証可能な制約をコードで強制（max 3 retry）
  - get_followup: ミニ会話の step2 / step3 用メソッド
  - Ollama API: system パラメータでキャラクターの system_prompt を分離

主な変更点（v3 - 良き隣人システム）:
  - UserProfile をオプションで受け取り、全プロンプトにRAG注入
  - set_user_profile(): 外部からプロファイルを差し込める
  - 成長観察ヒントのプロンプト内注入サポート
"""

import json
import logging
import os
import time
import random
import re
from collections import deque
from typing import Optional

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数（.env で上書き可能）
# ---------------------------------------------------------------------------

OLLAMA_MODEL      = os.environ.get("LLM_MODEL", "qwen3:8b")
OLLAMA_API_URL    = os.environ.get("OLLAMA_API_URL", "http://localhost:11434/api/generate")
OLLAMA_CHAT_URL   = os.environ.get("OLLAMA_CHAT_API_URL", "http://localhost:11434/api/chat")
OLLAMA_TIMEOUT    = int(os.environ.get("OLLAMA_TIMEOUT", "25"))
OLLAMA_NUM_PREDICT = 200

MAX_VALIDATE_RETRY = 3   # validate_response 失敗時の最大再生成回数

# リアクション用テンプレート（高速応答のため LLM を使わずに返す）
REACTION_TEMPLATES: dict[str, list[str]] = {
    "kill":     ["うまっ！", "やったじゃん！", "えぐっ！", "天才か", "完璧すぎ"],
    "death":    ["それ無理w", "あちゃー", "まあしゃーない", "惜しっ！", "次いこ"],
    "low_hp":   ["危なすぎ！", "逃げて！", "HP見てる？", "やばいって", "回復！"],
    "big_play": ["神プレイ！", "えぐいじゃん", "やべえ！", "うそやろ", "最高！"],
}

TENDENCY_MAP: dict[str, str] = {
    "kill":     "攻撃的",
    "death":    "リスキー",
    "big_play": "大胆",
    "low_hp":   "ギリギリ系",
}

FALLBACK_RESPONSES  = ["マジで？", "それな", "うーん...", "で、どうした？", "へー"]
PROACTIVE_TEMPLATES = ["ねえ、調子どう？", "何か作戦ある？", "敵強い？", "次どうするの？", "集中してる？w"]

# ミニ会話 step ごとのプロンプト補足
FOLLOWUP_STEP_PROMPTS: dict[int, str] = {
    2: "先ほどの出来事について、直前とはまったく違う角度から一言コメントしてください。詩的な表現は禁止です。",
    3: "話題を変えて、プレイヤーへの短い声かけや質問をしてください。ゲームの状況に絡めてください。",
}

DEFAULT_CHARACTER = "echo"


# ---------------------------------------------------------------------------
# AICompanion
# ---------------------------------------------------------------------------

class AICompanion:
    """
    キャラクタープロファイルを持つゲーム相棒 AI。

    使い方:
        ai = AICompanion()
        ai.set_character("michiko")
        response = ai.get_response(player_text, memory, state)
    """

    def __init__(self, model: str = OLLAMA_MODEL) -> None:
        self.model = model
        self._conversation_history: list[dict[str, str]] = []
        self._characters: dict = {}
        self.current_character: dict = {}
        self._user_profile: Optional[object] = None   # UserProfile（良き隣人システム）
        self._dynamics: Optional[object] = None        # ConversationDynamics（会話力学システム）
        self._ai_memory: Optional[object] = None      # AIMemory（長期記憶システム）
        self._long_term_cache: str = ""               # 長期記憶キャッシュ（セッション開始時に1回読む）
        self._vision_context: str = ""                # 最新の画面解析結果（VisionAnalyzer）
        self._vision_history: list[str] = []          # 直近 VLM 解析履歴（推移把握用）
        self._thinking_callback: Optional[object] = None  # (is_thinking: bool) -> None
        self._recent_responses: deque = deque(maxlen=10)  # 繰り返し防止用直近応答履歴
        self._load_characters()
        self.set_character(DEFAULT_CHARACTER)

    def set_thinking_callback(self, callback) -> None:
        """LLM 呼び出し開始/終了時に呼ばれるコールバックを登録する。
        callback(is_thinking: bool) の形式。GUI インジケーター用。"""
        self._thinking_callback = callback

    def set_user_profile(self, profile: object) -> None:
        """UserProfile インスタンスを設定する（良き隣人システム連携用）"""
        self._user_profile = profile
        # 会話力学システムも初期化（距離感制御）
        try:
            from conversation_dynamics import ConversationDynamics
            self._dynamics = ConversationDynamics(profile)
            logger.info("ConversationDynamics initialized (stage=%s)", self._dynamics.get_stage_label())
        except Exception as e:
            logger.warning("ConversationDynamics init failed: %s", e)

    def set_ai_memory(self, memory: object) -> None:
        """AIMemory インスタンスを設定する（長期記憶システム連携用）"""
        self._ai_memory = memory
        # セッション開始時に1回だけ長期記憶を読み込んでキャッシュする
        # （セッション中に ai_memory.db は変化しないため毎回読む必要がない）
        self._long_term_cache: str = ""
        try:
            self._long_term_cache = memory.get_long_term_context()  # type: ignore[union-attr]
            if self._long_term_cache:
                logger.info("Long-term memory loaded (%d chars)", len(self._long_term_cache))
        except Exception:
            pass

    def set_vision_context(self, context: str) -> None:
        """最新の画面解析テキストを設定し、履歴にも追記する（VisionAnalyzer連携用）"""
        if context and context != self._vision_context:
            self._vision_context = context
            self._vision_history.append(context)
            if len(self._vision_history) > 3:
                self._vision_history.pop(0)

    # ------------------------------------------------------------------
    # キャラクター管理
    # ------------------------------------------------------------------

    def _load_characters(self, path: str = "characters.json") -> None:
        """characters.json からすべてのキャラクターをロードする"""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # "_" 始まりのキーはメタ情報なので除外
            self._characters = {k: v for k, v in data.items() if not k.startswith("_")}
            logger.info("Characters loaded: %s", list(self._characters.keys()))
        except FileNotFoundError:
            logger.warning("characters.json not found — using empty character")
            self._characters = {}

    def set_character(self, name: str) -> dict:
        """
        使用するキャラクターを切り替える。
        存在しない名前の場合はデフォルト（kid）にフォールバック。

        Returns:
            選択されたキャラクターの dict
        """
        char = self._characters.get(name)
        if not char:
            logger.warning("Character '%s' not found, falling back to '%s'", name, DEFAULT_CHARACTER)
            char = self._characters.get(DEFAULT_CHARACTER, {})
        self.current_character = char
        logger.debug("Character set: %s", char.get("name", name))
        return char

    def list_characters(self) -> list[str]:
        return list(self._characters.keys())

    # ------------------------------------------------------------------
    # 公開メソッド
    # ------------------------------------------------------------------

    def get_reaction(self, event_type: str, state=None, use_template: bool = True) -> str:
        """
        ゲームイベントへの即時リアクションを返す（step 1）。
        use_template=True の場合テンプレートで高速応答。
        キャラクター固有の reaction_templates が定義されていればそちらを優先。
        LLM 使用時は軽量プロンプト（RAG なし）で低レイテンシを優先。
        """
        if use_template:
            # キャラ固有テンプレートを優先、なければデフォルトテンプレート
            char_templates = self.current_character.get("reaction_templates", {})
            templates = char_templates.get(event_type) or REACTION_TEMPLATES.get(event_type, ["おっ"])
            return random.choice(templates)

        state_ctx = state.summary() if state else ""
        prompt = (
            f"ゲームイベント「{event_type}」が起きた。\n"
            f"現在の状態:\n{state_ctx}\n\n"
            "状況を説明せず、プレイヤーへの自然な一言リアクションを返してください。"
        )
        # 即時リアクションは軽量モード（RAG/ビジョン/履歴を注入しない）で速度優先
        return self._call_with_validation(prompt, max_chars=60, lightweight=True)

    def get_response(
        self,
        player_input: str,
        memory: dict,
        state=None,
        growth_hint: Optional[str] = None,
        sentiment_context: Optional[str] = None,
    ) -> str:
        """プレイヤー発言に対する会話応答を返す（/api/chat エンドポイントを使用）"""
        # 会話履歴の汚染チェック: 直近2ターンのAI応答が似ていたら古い方を削除
        self._prune_repetitive_history()

        memory_ctx = self._build_memory_context(memory)

        tension   = state.to_dict().get("tension", 0.0)         if (state and hasattr(state, "to_dict")) else 0.0
        intensity = state.to_dict().get("input_intensity", 0.0) if (state and hasattr(state, "to_dict")) else 0.0

        # ── system プロンプトを構築 ──────────────────────────────────────────
        sys_parts: list[str] = []

        # 会話力学プロンプト（最上位 — 距離感制御の核心）
        # echomate-* モデルは Layer 1 が Modelfile に焼き込み済みなので Layer 2 のみ注入
        if self._dynamics is not None:
            if self.model.startswith("echomate-"):
                sys_parts.append(self._dynamics.build_modifier_prompt())
            else:
                sys_parts.append(self._dynamics.build_full_dynamics_prompt())
            # ステージ遷移チェック
            self._dynamics.check_stage_transition()
            transition_prompt = self._dynamics.get_transition_prompt()
            if transition_prompt:
                sys_parts.append(transition_prompt)

        # キャラクター人格
        char_sys = self.current_character.get("system_prompt", "")
        if char_sys:
            sys_parts.append(char_sys)
        if self._long_term_cache:
            sys_parts.append(self._long_term_cache)

        # ルール・制約
        if self.current_character.get("rules"):
            sys_parts.append("ルール:\n" + "\n".join(f"- {r}" for r in self.current_character["rules"]))
        constraints = self.current_character.get("constraints", {})
        if constraints:
            c_lines = []
            if "must_start" in constraints:
                c_lines.append(f"【厳命】必ず次のいずれかの言葉で文を書き始めてください（例外なし）: {', '.join(constraints['must_start'])}")
            if "forbidden" in constraints:
                c_lines.append(f"以下の言葉は絶対に使わないでください: {', '.join(constraints['forbidden'])}")
            if "must_include" in constraints:
                c_lines.append(f"以下の言葉のいずれかを必ず含めてください: {', '.join(constraints['must_include'])}")
            if constraints.get("no_exclamation"):
                c_lines.append("「！」（感嘆符）は絶対に使わないでください。")
            if "must_sequence" in constraints:
                c_lines.append(f"次の順番で内容を構成してください: {' -> '.join(constraints['must_sequence'])}")
            if constraints.get("must_include_advice"):
                c_lines.append("必ずプレイヤーへの具体的な助言や行動指示を含めてください。")
            if c_lines:
                sys_parts.append("【制約事項】\n" + "\n".join(f"- {c}" for c in c_lines))

        # 常時ルール
        sys_parts.append("【最重要】プレイヤーの最新発言に必ず反応すること。話題が変わったら即座に切り替え、古い話題を引きずらない。")
        sys_parts.append("プレイヤーの言葉をそのまま繰り返してはいけない。必ず自分の言葉で返すこと。")
        sys_parts.append("「〇〇は無理？」「〇〇できる？」のような確認・テスト質問を連続して繰り返すことは絶対禁止。プレイヤーが肯定したらその件は終了し次の話題に移れ。否定されたら深追いせず話題を変えろ。")
        if intensity >= 0.5:
            sys_parts.append("（一言で簡潔に返すこと）")

        # RAG: プロファイル・ゲーム知識・画面状況
        profile_ctx = self._build_profile_context()
        if profile_ctx:
            sys_parts.append(profile_ctx)
        game_knowledge_ctx = self._build_game_knowledge_context()
        if game_knowledge_ctx:
            sys_parts.append(game_knowledge_ctx)
        if self._vision_history:
            if len(self._vision_history) >= 2:
                history_lines = " → ".join(f"[{i + 1}]{ctx}" for i, ctx in enumerate(self._vision_history))
                sys_parts.append(f"【画面推移】{history_lines}")
            else:
                sys_parts.append(f"【画面状況】{self._vision_history[-1]}")

        # プレイスタイルに応じたトーン補正
        if self._user_profile is not None:
            try:
                labels = self._user_profile.get().get("playstyle_labels", [])
                tone_hints = []
                if "ゴリ押し" in labels:
                    tone_hints.append("もっとイケイケな口調で応援しろ")
                if "慎重" in labels:
                    tone_hints.append("一歩引いた視点で分析しろ")
                if tone_hints:
                    sys_parts.append(f"【トーン指示】{' / '.join(tone_hints)}")
            except Exception:
                pass

        # 感情トーン・観察メモ・会話拡張ヒント
        if sentiment_context:
            sys_parts.append(f"参考（感情トーン）: {sentiment_context}")
        if growth_hint:
            sys_parts.append(f"（観察メモ: {growth_hint}）")
        if tension < 0.4 and not self._conversation_history and random.random() < 0.3:
            sys_parts.append("（最後に相手に何か聞いてもいい）")

        # セッション内の過去の話題（会話履歴ウィンドウ外）
        if self._ai_memory is not None:
            try:
                session_ctx = self._ai_memory.get_session_context(skip_recent=3, limit=5)
                if session_ctx:
                    sys_parts.append(session_ctx)
            except Exception:
                pass

        # ゲーム状況・記憶
        state_ctx = state.summary() if state else ""
        if state_ctx:
            sys_parts.append(f"【補足・ゲーム状況】{state_ctx}")
        if memory_ctx and memory_ctx != "（メモリなし）":
            sys_parts.append(f"【補足・記憶】{memory_ctx}")

        # 繰り返し防止
        if self._recent_responses:
            avoid = "、".join(f"「{r[:20]}」" for r in list(self._recent_responses)[-5:])
            sys_parts.append(f"【絶対禁止】直前の発言と同じ内容・テーマ・表現を繰り返すな。禁止発言: {avoid}")

        system_prompt = "\n\n".join(filter(None, sys_parts))

        # ── messages を構築（会話履歴 + 現在のプレイヤー発言）─────────────────
        messages: list[dict] = []
        for turn in self._conversation_history[-3:]:
            messages.append({"role": "user",      "content": turn["player"]})
            messages.append({"role": "assistant", "content": turn["ai"]})
        messages.append({"role": "user", "content": player_input})

        # ── Chat API を呼び出し・バリデーション・繰り返し検出・エコー除去 ──────────
        response = ""
        _CHAT_MAX_RETRY = max(MAX_VALIDATE_RETRY, 5)
        for attempt in range(1, _CHAT_MAX_RETRY + 1):
            raw = self._call_ollama_chat(messages, system_prompt, max_chars=120)
            raw = self._remove_echo_prefix(raw, player_input)
            if raw in self._recent_responses or self._is_near_duplicate(raw):
                logger.warning("Repetition/near-dup detected %r, retrying (%d/%d)", raw[:30], attempt, _CHAT_MAX_RETRY)
                continue
            if self.validate_response(raw):
                if attempt > 1:
                    logger.debug("Chat validation passed on attempt %d", attempt)
                response = raw
                break
            logger.debug("Chat validation failed (attempt %d/%d): %s", attempt, _CHAT_MAX_RETRY, raw)
        else:
            logger.warning("All %d chat attempts failed/repeated, using fallback", _CHAT_MAX_RETRY)
            available = [f for f in FALLBACK_RESPONSES if f not in self._recent_responses]
            response = random.choice(available if available else FALLBACK_RESPONSES)

        self._recent_responses.append(response)

        is_fallback = response in FALLBACK_RESPONSES
        self._conversation_history.append({"player": player_input, "ai": response})
        if is_fallback and len(self._conversation_history) > 0:
            # フォールバック応答は会話履歴から除外してモデルへの汚染を防ぐ
            self._conversation_history.pop()
        if len(self._conversation_history) > 12:
            self._conversation_history.pop(0)

        if self._ai_memory is not None:
            try:
                self._ai_memory.add_turn(player_input, response)
            except Exception:
                pass

        return response

    def get_followup(
        self,
        event_type: str,
        step: int,
        state_snapshot: dict,
        memory: dict,
    ) -> str:
        """
        ミニ会話の step2 / step3 用フォローアップ発言を生成する。

        Args:
            event_type: 元のイベント種別
            step: 2 or 3
            state_snapshot: イベント発生時の PlayerState.to_dict()
            memory: イベント発生時のメモリスナップショット
        """
        state_ctx = (
            f"HP: {state_snapshot.get('hp_state', 'SAFE')}\n"
            f"連続キル: {state_snapshot.get('momentum', 0)}\n"
            f"テンション: {state_snapshot.get('tension', 0.5):.2f}"
        )
        step_instruction = FOLLOWUP_STEP_PROMPTS.get(step, "一言コメントしてください。")
        memory_ctx = self._build_memory_context(memory)

        prompt = (
            f"{memory_ctx}\n\n"
            f"直前のゲームイベント: {event_type}\n"
            f"状態:\n{state_ctx}\n\n"
            f"{step_instruction}\n"
            "返答:"
        )
        return self._call_with_validation(prompt, max_chars=80)

    def get_proactive_message(
        self,
        memory: dict,
        state=None,
        growth_hint: Optional[str] = None,
    ) -> str:
        """プレイヤーが無言のとき、自発的に話題を振るメッセージを返す"""
        memory_ctx = self._build_memory_context(memory)
        state_ctx  = state.summary() if state else ""

        # 親密度が高く過去エピソードがある場合、15%の確率で過去の出来事を話題にする
        if self._user_profile is not None:
            try:
                bond = self._user_profile.get_bond_level()
                episodes = self._user_profile.get_memorable_episodes()
                if bond >= 0.5 and episodes and random.random() < 0.15:
                    current_game = self._user_profile.get_current_game()
                    if current_game:
                        game_eps = [e for e in episodes if e.get("game") == current_game]
                        ep = random.choice(game_eps) if game_eps else random.choice(episodes)
                    else:
                        ep = random.choice(episodes)
                    ep_text = ep.get("text", "")
                    ep_game = ep.get("game", "")
                    game_note = f"（{ep_game}での出来事）" if ep_game else ""
                    prompt = (
                        f"{memory_ctx}\n"
                        f"{state_ctx}\n\n"
                        f"過去の印象的な出来事{game_note}:「{ep_text}」\n"
                        "この出来事を自然に蒸し返して話題にしてください。\n"
                        "発言:"
                    )
                    return self._call_with_validation(prompt, max_chars=80, growth_hint=growth_hint)
            except Exception:
                pass

        # Task4: 過去の話題が存在する場合、40%の確率で蒸し返す
        recent_topics = memory.get("recent_topics", [])
        if recent_topics and random.random() < 0.4:
            prompt = (
                f"{memory_ctx}\n"
                f"{state_ctx}\n\n"
                "さっきの会話の話題を自然に蒸し返して一言コメントや質問を投げてください。\n"
                "発言:"
            )
        else:
            prompt = (
                f"{memory_ctx}\n"
                f"{state_ctx}\n\n"
                "プレイヤーがしばらく無言です。自然に話しかけてください。\n"
                "発言:"
            )
        return self._call_with_validation(prompt, max_chars=80, growth_hint=growth_hint)

    def get_tendency_label(self, event_type: str) -> str | None:
        return TENDENCY_MAP.get(event_type)

    def get_game_change_greeting(self, old_game: str, new_game: str) -> str:
        """
        ゲームが変わった際のメタ的な挨拶文を生成する。
        「前回は〇〇だったけど今日は××だね！」のように自然につなぐ。
        """
        if old_game and new_game:
            prompt = (
                f"前回は「{old_game}」をプレイしていたが、今日は「{new_game}」に変わった。\n"
                "ゲームが変わったことに触れながら自然に挨拶してください。\n"
                "発言:"
            )
        elif new_game:
            prompt = (
                f"今日は「{new_game}」をプレイするんだね。\n"
                "軽く挨拶してください。\n"
                "発言:"
            )
        else:
            return random.choice(PROACTIVE_TEMPLATES)

        return self._call_with_validation(prompt, max_chars=80)

    # ------------------------------------------------------------------
    # バリデーション
    # ------------------------------------------------------------------

    def validate_response(self, text: str) -> bool:
        """
        現在のキャラクター制約に基づいてテキストを検証する。

        コード検証対象（文字列マッチング可能）:
          - no_exclamation : 「！」を含まないか
          - must_start     : 指定のいずれかで始まるか
          - forbidden      : 禁止ワードを含まないか
          - must_include   : 必須ワードをどれか含むか

        ※ must_sequence / praise_limit / max_toxicity 等の意味論的制約は
          system_prompt + rules でプロンプト制御するためここでは検証しない。
        """
        c = self.current_character.get("constraints", {})

        if c.get("no_exclamation") and "！" in text:
            return False

        if "must_start" in c:
            if not any(text.startswith(p) for p in c["must_start"]):
                return False

        if "forbidden" in c:
            if any(f in text for f in c["forbidden"]):
                return False

        if "must_include" in c:
            if not any(w in text for w in c["must_include"]):
                return False

        return True

    # ------------------------------------------------------------------
    # プライベートメソッド
    # ------------------------------------------------------------------

    def _build_profile_context(self) -> str:
        """UserProfile からRAGコンテキストを生成する（プロファイル未設定なら空文字）"""
        if self._user_profile is None:
            return ""
        try:
            basic = self._user_profile.get_summary_for_prompt()
            growth = self._user_profile.get_growth_summary_for_prompt()
            context_summary = self._user_profile.get_context_summary()
            parts = [p for p in [basic, growth] if p]
            if context_summary:
                parts.append(f"【最近の文脈】{context_summary}")
            return "\n".join(parts)
        except Exception:
            return ""

    def _build_game_knowledge_context(self) -> str:
        """game_knowledge.json から学習済みゲーム知識をRAG注入用テキストとして返す（Task6d）"""
        try:
            with open("game_knowledge.json", encoding="utf-8") as f:
                knowledge = json.load(f)
            if not knowledge:
                return ""
            lines = [f"- {word}: {desc}" for word, desc in list(knowledge.items())[:20]]
            return "【学習したゲーム知識】\n" + "\n".join(lines)
        except (FileNotFoundError, json.JSONDecodeError):
            return ""

    def _call_with_validation(
        self,
        prompt: str,
        max_chars: int = 100,
        growth_hint: Optional[str] = None,
        lightweight: bool = False,
    ) -> str:
        """
        LLM を呼び出し、validate_response が通るまで最大 MAX_VALIDATE_RETRY 回リトライする。
        すべて失敗した場合はフォールバック文字列を返す。

        Args:
            prompt:      ユーザー向けプロンプト本文
            max_chars:   応答の最大文字数
            growth_hint: 成長観察ヒント（Noneなら注入しない）
            lightweight: True の場合 RAG/ビジョン/プロファイルを注入しない（即時リアクション用）
        """
        system_prompt = self.current_character.get("system_prompt", "")

        # 会話力学プロンプトを先頭に注入（距離感制御の核心）
        # echomate-* モデルは Layer 1 が Modelfile に焼き込み済みなので Layer 2 のみ注入
        if not lightweight and self._dynamics is not None:
            if self.model.startswith("echomate-"):
                dynamics_prompt = self._dynamics.build_modifier_prompt()
            else:
                dynamics_prompt = self._dynamics.build_full_dynamics_prompt()
            system_prompt = f"{dynamics_prompt}\n\n{system_prompt}" if system_prompt else dynamics_prompt

        # 長期記憶を注入
        if not lightweight and self._long_term_cache:
            system_prompt = f"{system_prompt}\n\n{self._long_term_cache}" if system_prompt else self._long_term_cache

        # 最新発言への反応を最優先にする常時ルール
        if not lightweight:
            topic_rule = "【最重要】プレイヤーの最新発言に必ず反応すること。話題が変わったら即座に切り替え、古い話題を引きずらない。"
            system_prompt = f"{system_prompt}\n{topic_rule}" if system_prompt else topic_rule

        rules_text = ""
        if self.current_character.get("rules"):
            rules_text = "ルール:\n" + "\n".join(f"- {r}" for r in self.current_character["rules"])

        prefix_parts = []

        if not lightweight:
            # ユーザープロファイルを先頭に注入（RAG）
            profile_ctx = self._build_profile_context()
            game_knowledge_ctx = self._build_game_knowledge_context()
            if profile_ctx:
                prefix_parts.append(profile_ctx)
            if game_knowledge_ctx:
                prefix_parts.append(game_knowledge_ctx)
            if self._vision_history:
                if len(self._vision_history) >= 2:
                    history_lines = " → ".join(
                        f"[{i + 1}]{ctx}" for i, ctx in enumerate(self._vision_history)
                    )
                    prefix_parts.append(f"【画面推移】{history_lines}")
                else:
                    prefix_parts.append(f"【画面状況】{self._vision_history[-1]}")
            if growth_hint:
                prefix_parts.append(f"（観察メモ: {growth_hint}）")

        prefix = "\n".join(prefix_parts)

        # キャラクター制約を動的にプロンプトへ注入
        constraints = self.current_character.get("constraints", {})
        if constraints:
            constraint_lines = []
            if "must_start" in constraints:
                constraint_lines.append(f"【厳命】必ず次のいずれかの言葉で文を書き始めてください（例外なし）: {', '.join(constraints['must_start'])}")
            if "forbidden" in constraints:
                constraint_lines.append(f"以下の言葉は絶対に使わないでください: {', '.join(constraints['forbidden'])}")
            if "must_include" in constraints:
                constraint_lines.append(f"以下の言葉のいずれかを必ず含めてください: {', '.join(constraints['must_include'])}")
            if constraints.get("no_exclamation"):
                constraint_lines.append("「！」（感嘆符）は絶対に使わないでください。")
            if "must_sequence" in constraints:
                constraint_lines.append(f"次の順番で内容を構成してください: {' -> '.join(constraints['must_sequence'])}")
            if constraints.get("must_include_advice"):
                constraint_lines.append("必ずプレイヤーへの具体的な助言や行動指示を含めてください。")
            
            if constraint_lines:
                c_text = "【制約事項】\n" + "\n".join(f"- {c}" for c in constraint_lines)
                rules_text = f"{rules_text}\n\n{c_text}" if rules_text else c_text

        base_prompt = f"{rules_text}\n\n{prompt}" if rules_text else prompt
        full_prompt  = f"{prefix}\n\n{base_prompt}" if prefix else base_prompt

        # プレイスタイルに応じたトーン補正（軽量モード時はスキップ）
        if not lightweight and self._user_profile is not None:
            try:
                labels = self._user_profile.get().get("playstyle_labels", [])
                tone_hints = []
                if "ゴリ押し" in labels:
                    tone_hints.append("もっとイケイケな口調で応援しろ")
                if "慎重" in labels:
                    tone_hints.append("一歩引いた視点で分析しろ")
                if tone_hints:
                    full_prompt = f"【トーン指示: {' / '.join(tone_hints)}】\n{full_prompt}"
            except Exception:
                pass

        # 直近の応答をシステムプロンプトに注入して繰り返しを防ぐ
        # （ユーザープロンプトより system_prompt 側が LLM に強く効く）
        if self._recent_responses:
            avoid = "、".join(f"「{r[:20]}」" for r in list(self._recent_responses)[-5:])
            anti_repeat = f"【絶対禁止】直前の発言と同じ内容・テーマ・表現を繰り返すな。禁止発言: {avoid}"
            system_prompt = f"{system_prompt}\n{anti_repeat}" if system_prompt else anti_repeat

        for attempt in range(1, MAX_VALIDATE_RETRY + 1):
            raw = self._call_ollama(full_prompt, system_prompt, max_chars)  # type: ignore[arg-type]
            if self.validate_response(raw):
                if attempt > 1:
                    logger.debug("Validation passed on attempt %d", attempt)
                self._recent_responses.append(raw)
                return raw
            logger.debug("Validation failed (attempt %d/%d): %s", attempt, MAX_VALIDATE_RETRY, raw)

        logger.warning("All %d validation attempts failed, using fallback", MAX_VALIDATE_RETRY)
        fallback = random.choice(FALLBACK_RESPONSES)
        self._recent_responses.append(fallback)
        return fallback

    def _call_ollama(self, prompt: str, system_prompt: str, max_chars: int) -> str:
        """Ollama HTTP API を呼び出す。system パラメータでキャラクターを注入。"""
        if self._thinking_callback:
            try:
                self._thinking_callback(True)
            except Exception:
                pass
        start = time.time()
        try:
            payload: dict = {
                "model":  self.model,
                "prompt": prompt,
                "stream": False,
                "think":  False,
                "options": {
                    "num_predict":    OLLAMA_NUM_PREDICT,
                    "temperature":    0.8,
                    "top_p":          0.9,
                    "repeat_penalty": 1.3,
                    "repeat_last_n":  64,
                },
            }
            if system_prompt:
                payload["system"] = system_prompt

            res = httpx.post(OLLAMA_API_URL, json=payload, timeout=OLLAMA_TIMEOUT)
            elapsed = time.time() - start
            logger.debug("Ollama responded in %.2fs", elapsed)

            if res.status_code != 200:
                logger.error("Ollama HTTP %d: %s", res.status_code, res.text[:100])
                return random.choice(FALLBACK_RESPONSES)

            raw = res.json().get("response", "")
            return self._clean(raw, max_chars)

        except httpx.ConnectError:
            logger.error("Ollama not running at %s", OLLAMA_API_URL)
            return "（AI未接続）"
        except httpx.TimeoutException:
            logger.warning("Ollama timed out after %ds", OLLAMA_TIMEOUT)
            return "ちょっと待って"
        except Exception as e:
            logger.error("Ollama error: %s", e)
            return random.choice(FALLBACK_RESPONSES)
        finally:
            if self._thinking_callback:
                try:
                    self._thinking_callback(False)
                except Exception:
                    pass

    def _call_ollama_chat(self, messages: list[dict], system_prompt: str, max_chars: int) -> str:
        """Ollama /api/chat エンドポイントを呼び出す（chat モデル向け）。"""
        if self._thinking_callback:
            try:
                self._thinking_callback(True)
            except Exception:
                pass
        start = time.time()
        try:
            all_messages: list[dict] = []
            if system_prompt:
                all_messages.append({"role": "system", "content": system_prompt})
            all_messages.extend(messages)

            payload: dict = {
                "model":  self.model,
                "messages": all_messages,
                "stream": False,
                "think":  False,
                "options": {
                    "num_predict":    OLLAMA_NUM_PREDICT,
                    "temperature":    0.8,
                    "top_p":          0.9,
                    "repeat_penalty": 1.3,
                    "repeat_last_n":  64,
                },
            }

            res = httpx.post(OLLAMA_CHAT_URL, json=payload, timeout=OLLAMA_TIMEOUT)
            elapsed = time.time() - start
            logger.debug("Ollama chat responded in %.2fs", elapsed)

            if res.status_code != 200:
                logger.error("Ollama Chat HTTP %d: %s", res.status_code, res.text[:100])
                return random.choice(FALLBACK_RESPONSES)

            raw = res.json().get("message", {}).get("content", "")
            return self._clean(raw, max_chars)

        except httpx.ConnectError:
            logger.error("Ollama not running at %s", OLLAMA_CHAT_URL)
            return "（AI未接続）"
        except httpx.TimeoutException:
            logger.warning("Ollama chat timed out after %ds", OLLAMA_TIMEOUT)
            return "ちょっと待って"
        except Exception as e:
            logger.error("Ollama chat error: %s", e)
            return random.choice(FALLBACK_RESPONSES)
        finally:
            if self._thinking_callback:
                try:
                    self._thinking_callback(False)
                except Exception:
                    pass

    def _remove_echo_prefix(self, response: str, player_input: str) -> str:
        """
        モデルがプレイヤー発言を先頭に繰り返すパターン（例: 「{player}？ {実際の返答}」）を除去する。
        純粋なエコー（残りがない）場合はフォールバックを返す。
        """
        r = response.strip()
        # 末尾の句読点を取り除いた player_input でプレフィックス一致を確認
        p = player_input.strip().rstrip("？?。！!、, 　")
        if not p or not r.startswith(p):
            return response
        suffix = r[len(p):].lstrip("？?。！!、, 　").strip()
        if suffix:
            logger.warning("Echo prefix stripped: %r → %r", r[:40], suffix[:40])
            return suffix
        # 残りがない = 純粋なエコー
        logger.warning("Pure echo detected, using fallback")
        return random.choice(FALLBACK_RESPONSES)

    @staticmethod
    def _clean(raw: str, max_chars: int) -> str:
        """<think> タグ除去・先頭行抽出・文末で自然に切り詰め"""
        # <think>タグと「返答:」「あなた:」などのプレフィックスを除去
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        cleaned = re.sub(r"^(返答|あなた|AI|エコー|ミチコ|キッド|レイ|リュウ|アカネ|プレイヤー|Player|相棒)[:：]\s*", "", cleaned).strip()
        first_line = cleaned.split("\n")[0].strip()
        if len(first_line) <= max_chars:
            return first_line or "..."
        # max_chars を超える場合、最後の「。」「！」「？」で自然に切り詰める
        truncated = first_line[:max_chars]
        last_punct = max(truncated.rfind("。"), truncated.rfind("！"), truncated.rfind("？"), truncated.rfind("."))
        if last_punct > max_chars // 2:
            return truncated[:last_punct + 1]
        return truncated

    def _build_memory_context(self, memory: dict) -> str:
        parts = []
        if memory.get("last_event"):
            parts.append(f"直近イベント: {memory['last_event']}")
        if memory.get("player_tendency"):
            parts.append(f"プレイヤー傾向: {memory['player_tendency']}")
        if memory.get("recent_topics"):
            parts.append(f"最近の話題: {'、'.join(memory['recent_topics'][-3:])}")
        return "\n".join(parts) if parts else "（メモリなし）"

    def _build_history_context(self) -> str:
        """直近3ターンをチャット形式で返す。古い話題に引きずられないよう意図的に短く保つ。"""
        if not self._conversation_history:
            return ""
        recent = self._conversation_history[-3:]
        lines = []
        for turn in recent:
            lines.append(f"プレイヤー: {turn['player']}")
            lines.append(f"あなた: {turn['ai']}")
        return "\n".join(lines) + "\n"

    def _is_near_duplicate(self, response: str, threshold: float = 0.58) -> bool:
        """直近の応答と文字集合レベルで重複しているか判定する（意味的繰り返し検出）。"""
        r_chars = set(response.strip())
        if not r_chars:
            return False
        for prev in list(self._recent_responses)[-3:]:
            p_chars = set(prev.strip())
            if not p_chars:
                continue
            overlap = len(r_chars & p_chars) / max(len(r_chars), len(p_chars))
            if overlap >= threshold:
                return True
        return False

    def _prune_repetitive_history(self) -> None:
        """会話履歴のAI応答が同一パターンに汚染されていたら削除する。
        2ペア以上重複 → 直近3ターンを全削除、1ペアのみ → 古い方1件を削除。"""
        if len(self._conversation_history) < 2:
            return
        recent = self._conversation_history[-4:]
        ai_responses = [t["ai"] for t in recent]
        dup_pairs = 0
        for i in range(len(ai_responses) - 1):
            a_chars = set(ai_responses[i].strip())
            b_chars = set(ai_responses[i + 1].strip())
            if not a_chars or not b_chars:
                continue
            overlap = len(a_chars & b_chars) / max(len(a_chars), len(b_chars))
            if overlap >= 0.5:
                dup_pairs += 1
        if dup_pairs >= 2:
            keep = self._conversation_history[:-3] if len(self._conversation_history) > 3 else []
            removed = len(self._conversation_history) - len(keep)
            self._conversation_history[:] = keep
            logger.warning("Heavy history contamination (%d dup pairs) — cleared %d entries", dup_pairs, removed)
        elif dup_pairs == 1:
            removed_entry = self._conversation_history.pop(-2)
            logger.debug("Pruned 1 repetitive history entry: %r", removed_entry["ai"][:30])
