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
import time
import random
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

OLLAMA_MODEL      = "gemma2:2b"
OLLAMA_API_URL    = "http://localhost:11434/api/generate"
OLLAMA_TIMEOUT    = 10
OLLAMA_NUM_PREDICT = 60

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
    2: "先ほどの出来事について、プレイへの評価を1文で言ってください。",
    3: "それを踏まえて、締めの一言を言ってください。",
}

DEFAULT_CHARACTER = "kid"


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
        self._load_characters()
        self.set_character(DEFAULT_CHARACTER)

    def set_user_profile(self, profile: object) -> None:
        """UserProfile インスタンスを設定する（良き隣人システム連携用）"""
        self._user_profile = profile

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
        logger.info("Character set: %s", char.get("name", name))
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
        """
        if use_template:
            templates = REACTION_TEMPLATES.get(event_type, ["おっ！"])
            return random.choice(templates)

        state_ctx = state.summary() if state else ""
        prompt = (
            f"イベント「{event_type}」が発生した。\n"
            f"現在の状態:\n{state_ctx}\n\n"
            "1文・最大15文字で感情的なリアクションをしてください。"
        )
        return self._call_with_validation(prompt, max_chars=15)

    def get_response(
        self,
        player_input: str,
        memory: dict,
        state=None,
        growth_hint: Optional[str] = None,
    ) -> str:
        """プレイヤー発言に対する会話応答を返す"""
        memory_ctx  = self._build_memory_context(memory)
        history_ctx = self._build_history_context()
        state_ctx   = state.summary() if state else "（状態情報なし）"

        prompt = (
            f"{memory_ctx}\n\n"
            f"現在の状態:\n{state_ctx}\n\n"
            f"{history_ctx}\n"
            f"プレイヤーの発言:「{player_input}」\n\n"
            "ルール: 1〜2文・最大40文字・フランク・時々質問・日本語のみ\n"
            "返答:"
        )
        response = self._call_with_validation(prompt, max_chars=40, growth_hint=growth_hint)

        self._conversation_history.append({"player": player_input, "ai": response})
        if len(self._conversation_history) > 10:
            self._conversation_history.pop(0)

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

        prompt = (
            f"直前のゲームイベント: {event_type}\n"
            f"状態:\n{state_ctx}\n\n"
            f"{step_instruction}\n"
            "1文・最大30文字で返してください。\n"
            "返答:"
        )
        return self._call_with_validation(prompt, max_chars=30)

    def get_proactive_message(
        self,
        memory: dict,
        state=None,
        growth_hint: Optional[str] = None,
    ) -> str:
        """プレイヤーが無言のとき、自発的に話題を振るメッセージを返す"""
        memory_ctx = self._build_memory_context(memory)
        state_ctx  = state.summary() if state else ""

        prompt = (
            f"{memory_ctx}\n"
            f"{state_ctx}\n\n"
            "プレイヤーがしばらく無言です。自然に話題を振ってください。\n"
            "ルール: 1文・最大30文字・フランク・質問かコメント\n"
            "発言:"
        )
        return self._call_with_validation(prompt, max_chars=30, growth_hint=growth_hint)

    def get_tendency_label(self, event_type: str) -> str | None:
        return TENDENCY_MAP.get(event_type)

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
            return self._user_profile.get_summary_for_prompt()
        except Exception:
            return ""

    def _call_with_validation(
        self,
        prompt: str,
        max_chars: int = 40,
        growth_hint: Optional[str] = None,
    ) -> str:
        """
        LLM を呼び出し、validate_response が通るまで最大 MAX_VALIDATE_RETRY 回リトライする。
        すべて失敗した場合はフォールバック文字列を返す。

        Args:
            prompt:      ユーザー向けプロンプト本文
            max_chars:   応答の最大文字数
            growth_hint: 成長観察ヒント（Noneなら注入しない）
        """
        system_prompt = self.current_character.get("system_prompt", "")
        rules_text = ""
        if self.current_character.get("rules"):
            rules_text = "ルール:\n" + "\n".join(f"- {r}" for r in self.current_character["rules"])

        # ユーザープロファイルを先頭に注入（RAG）
        profile_ctx = self._build_profile_context()
        prefix_parts = []
        if profile_ctx:
            prefix_parts.append(profile_ctx)
        if growth_hint:
            prefix_parts.append(f"（観察メモ: {growth_hint}）")
        prefix = "\n".join(prefix_parts)

        base_prompt = f"{rules_text}\n\n{prompt}" if rules_text else prompt
        full_prompt  = f"{prefix}\n\n{base_prompt}" if prefix else base_prompt

        for attempt in range(1, MAX_VALIDATE_RETRY + 1):
            raw = self._call_ollama(full_prompt, system_prompt, max_chars)  # type: ignore[arg-type]
            if self.validate_response(raw):
                if attempt > 1:
                    logger.debug("Validation passed on attempt %d", attempt)
                return raw
            logger.debug("Validation failed (attempt %d/%d): %s", attempt, MAX_VALIDATE_RETRY, raw)

        logger.warning("All %d validation attempts failed, using fallback", MAX_VALIDATE_RETRY)
        return random.choice(FALLBACK_RESPONSES)

    def _call_ollama(self, prompt: str, system_prompt: str, max_chars: int) -> str:
        """Ollama HTTP API を呼び出す。system パラメータでキャラクターを注入。"""
        start = time.time()
        try:
            payload: dict = {
                "model":  self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": OLLAMA_NUM_PREDICT,
                    "temperature": 0.8,
                    "top_p":       0.9,
                },
            }
            if system_prompt:
                payload["system"] = system_prompt

            res = requests.post(OLLAMA_API_URL, json=payload, timeout=OLLAMA_TIMEOUT)
            elapsed = time.time() - start
            logger.debug("Ollama responded in %.2fs", elapsed)

            if res.status_code != 200:
                logger.error("Ollama HTTP %d: %s", res.status_code, res.text[:100])
                return random.choice(FALLBACK_RESPONSES)

            raw = res.json().get("response", "")
            return self._clean(raw, max_chars)

        except requests.exceptions.ConnectionError:
            logger.error("Ollama not running at %s", OLLAMA_API_URL)
            return "（AI未接続）"
        except requests.exceptions.Timeout:
            logger.warning("Ollama timed out after %ds", OLLAMA_TIMEOUT)
            return "ちょっと待って"
        except Exception as e:
            logger.error("Ollama error: %s", e)
            return random.choice(FALLBACK_RESPONSES)

    @staticmethod
    def _clean(raw: str, max_chars: int) -> str:
        """<think> タグ除去・先頭行抽出・文字数切り詰め"""
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        first_line = cleaned.split("\n")[0].strip()
        return first_line[:max_chars] if len(first_line) > max_chars else (first_line or "...")

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
        if not self._conversation_history:
            return ""
        lines = [
            f"  P:「{t['player']}」 → AI:「{t['ai']}」"
            for t in self._conversation_history[-2:]
        ]
        return "直近の会話:\n" + "\n".join(lines)
