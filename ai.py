"""
ai.py - AI処理モジュール

Ollama（ローカルLLM）を使って以下を実行する：
  - get_reaction()  : ゲームイベントへの即時リアクション（テンプレ or LLM）
  - get_response()  : プレイヤー発言への会話応答（LLM）

設計方針：
  - テンプレート優先でリアクション速度を確保（< 0.5 秒目標）
  - LLM 呼び出しはサブプロセスで行い、タイムアウトで保護
  - 将来的に API 型 LLM へ差し替えやすいよう _call_ollama を分離
"""

import subprocess
import json
import logging
import time
import random
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

OLLAMA_MODEL = "gemma2:2b"  # 軽量モデル推奨（gemma2:2b / llama3.2:1b など）
OLLAMA_TIMEOUT = 10          # Ollama 呼び出しのタイムアウト秒数

# リアクション用テンプレート（高速応答のため LLM を使わずに返す）
REACTION_TEMPLATES: dict[str, list[str]] = {
    "kill":     ["うまっ！", "やったじゃん！", "えぐっ！", "天才か", "完璧すぎ"],
    "death":    ["それ無理w", "あちゃー", "まあしゃーない", "惜しっ！", "次いこ"],
    "low_hp":   ["危なすぎ！", "逃げて！", "HP見てる？", "やばいって", "回復！"],
    "big_play": ["神プレイ！", "えぐいじゃん", "やべえ！", "うそやろ", "最高！"],
}

# イベント種別 → プレイヤー傾向マッピング
TENDENCY_MAP: dict[str, str] = {
    "kill":     "攻撃的",
    "death":    "リスキー",
    "big_play": "大胆",
    "low_hp":   "ギリギリ系",
}

# 会話応答のフォールバック（LLM 失敗時）
FALLBACK_RESPONSES = [
    "マジで？", "それな", "うーん...", "で、どうした？", "へー"
]


class AICompanion:
    """
    ゲーム相棒 AI。
    リアクション（即時）と会話応答（LLM）の2つの機能を持つ。
    """

    def __init__(self, model: str = OLLAMA_MODEL) -> None:
        self.model = model
        # 直近の会話履歴（最大10件）
        self._conversation_history: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # 公開メソッド
    # ------------------------------------------------------------------

    def get_reaction(self, event_type: str, use_template: bool = True) -> str:
        """
        ゲームイベントへの短いリアクションを返す。

        Args:
            event_type: イベント種別（kill / death / low_hp / big_play）
            use_template: True の場合テンプレートを使用（高速）
        Returns:
            最大15文字のリアクション文字列
        """
        if use_template:
            templates = REACTION_TEMPLATES.get(event_type, ["おっ！"])
            reaction = random.choice(templates)
            logger.debug("Template reaction [%s]: %s", event_type, reaction)
            return reaction

        # LLM でリアクション生成（遅いが多様）
        prompt = (
            f"ゲームイベント「{event_type}」に対して、"
            "1文・最大15文字で感情的なリアクションをしてください。\n"
            "指示や説明は不要。リアクションのみ返してください。"
        )
        response = self._call_ollama(prompt, max_chars=15)
        logger.debug("LLM reaction [%s]: %s", event_type, response)
        return response

    def get_response(self, player_input: str, memory: dict) -> str:
        """
        プレイヤー発言に対する会話応答を返す。

        Args:
            player_input: プレイヤーの発言テキスト
            memory: EventManager から取得したメモリ辞書
        Returns:
            最大40文字の返答文字列
        """
        memory_context = self._build_memory_context(memory)
        history_context = self._build_history_context()

        prompt = (
            "あなたはゲーム中の相棒AIです。\n"
            f"{memory_context}\n"
            f"{history_context}\n"
            f"プレイヤーの発言:「{player_input}」\n\n"
            "ルール：\n"
            "- 1〜2文で返す\n"
            "- 最大40文字\n"
            "- フランクで感情あり\n"
            "- 時々質問を入れる\n"
            "- 日本語のみ\n"
            "- 余計な説明不要\n\n"
            "返答:"
        )

        response = self._call_ollama(prompt, max_chars=40)

        # 会話履歴を更新
        self._conversation_history.append({"player": player_input, "ai": response})
        if len(self._conversation_history) > 10:
            self._conversation_history.pop(0)

        logger.debug("LLM response: %s", response)
        return response

    def get_tendency_label(self, event_type: str) -> str | None:
        """イベント種別からプレイヤー傾向ラベルを取得する"""
        return TENDENCY_MAP.get(event_type)

    # ------------------------------------------------------------------
    # プライベートメソッド
    # ------------------------------------------------------------------

    def _build_memory_context(self, memory: dict) -> str:
        """メモリ辞書をプロンプト用文字列に変換する"""
        parts = []
        if memory.get("last_event"):
            parts.append(f"直近イベント: {memory['last_event']}")
        if memory.get("player_tendency"):
            parts.append(f"プレイヤー傾向: {memory['player_tendency']}")
        if memory.get("recent_topics"):
            topics = "、".join(memory["recent_topics"][-3:])
            parts.append(f"最近の話題: {topics}")
        return "\n".join(parts) if parts else "（メモリなし）"

    def _build_history_context(self) -> str:
        """直近2件の会話履歴をプロンプト用文字列に変換する"""
        if not self._conversation_history:
            return ""
        recent = self._conversation_history[-2:]
        lines = []
        for turn in recent:
            lines.append(f"  P:「{turn['player']}」 → AI:「{turn['ai']}」")
        return "直近の会話:\n" + "\n".join(lines)

    def _call_ollama(self, prompt: str, max_chars: int = 40) -> str:
        """
        Ollama をサブプロセスで呼び出し、応答テキストを返す。

        Args:
            prompt: LLM に渡すプロンプト
            max_chars: 返答の最大文字数（超過時は切り詰め）
        Returns:
            応答テキスト（失敗時はフォールバック）
        """
        start = time.time()
        try:
            result = subprocess.run(
                ["ollama", "run", self.model],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=OLLAMA_TIMEOUT,
                encoding="utf-8",
            )
            elapsed = time.time() - start
            logger.debug("Ollama responded in %.2fs", elapsed)

            if result.returncode != 0:
                logger.error("Ollama returncode %d: %s", result.returncode, result.stderr.strip())
                return self._fallback()

            return self._clean_response(result.stdout, max_chars)

        except subprocess.TimeoutExpired:
            logger.warning("Ollama timed out after %ds", OLLAMA_TIMEOUT)
            return "ちょっと待って"
        except FileNotFoundError:
            logger.error("Ollama binary not found. Install from https://ollama.com")
            return "（AI未接続）"
        except Exception as e:
            logger.error("Ollama unexpected error: %s", e)
            return self._fallback()

    @staticmethod
    def _clean_response(raw: str, max_chars: int) -> str:
        """
        LLM の生出力をクリーンアップする。
        - <think>...</think> タグなど思考ブロックを除去
        - 先頭1文のみ抽出
        - max_chars で切り詰め
        """
        # 思考ブロック除去（DeepSeek-R1 系など）
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        # 余分な空白・改行を整理
        cleaned = cleaned.strip()
        # 最初の行だけ使う
        first_line = cleaned.split("\n")[0].strip()
        # 文字数制限
        if len(first_line) > max_chars:
            first_line = first_line[:max_chars]
        return first_line or "..."

    @staticmethod
    def _fallback() -> str:
        """LLM 失敗時のフォールバック応答"""
        return random.choice(FALLBACK_RESPONSES)
