"""
ai_memory.py - AI会話記憶システム

セッション内の会話を一時DBに蓄積し、セッション終了時に長期DBへ圧縮保存する。
次回起動時に長期記憶をプロンプトへ注入することで、AIが過去の会話を覚えられる。

DB構成:
  ai_temp_memory.db  — セッション中の一時保存（起動ごとにクリーン）
  ai_memory.db       — セッション間の長期記憶（要約＋既知ファクト）
"""

import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Optional

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

OLLAMA_API_URL  = os.environ.get("OLLAMA_API_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL    = os.environ.get("LLM_MODEL", "qwen3:8b")
OLLAMA_TIMEOUT  = int(os.environ.get("OLLAMA_TIMEOUT", "25"))

TEMP_DB_PATH    = os.environ.get("AI_TEMP_DB", "ai_temp_memory.db")
LONG_DB_PATH    = os.environ.get("AI_MEMORY_DB", "ai_memory.db")

# 即時フィルター: プレイヤー発言がこれ以下の文字数ならノイズ扱い
NOISE_MIN_CHARS = 4

# ノイズ判定ワード（笑い声・感嘆詞系）
_NOISE_WORDS = {
    "ははは", "あははは", "ふふふ", "えー", "うーん", "んー",
    "笑", "wwww", "www", "ww",
}

# 長期記憶をプロンプトへ注入する最大文字数
MAX_LONG_TERM_CHARS = 300

# セッション終了時に圧縮対象とする最小ターン数（少なすぎる場合はスキップ）
MIN_TURNS_TO_COMPRESS = 3


class AIMemory:
    """
    AIの会話記憶を管理するクラス。

    使い方:
        mem = AIMemory()
        # 会話のたびに
        mem.add_turn(player_text, ai_text)
        # プロンプト生成時
        context = mem.get_long_term_context()  → system_promptの先頭に注入
        # セッション終了時
        await mem.compress_and_save(game_name)
    """

    def __init__(
        self,
        temp_db: str = TEMP_DB_PATH,
        long_db: str = LONG_DB_PATH,
        session_id: Optional[str] = None,
    ) -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self._temp_db = temp_db
        self._long_db = long_db
        self._init_temp_db()
        self._init_long_db()
        logger.info("AIMemory initialized (session=%s)", self.session_id[:8])

    # ------------------------------------------------------------------
    # 公開メソッド
    # ------------------------------------------------------------------

    def add_turn(self, player_text: str, ai_text: str) -> None:
        """会話1ターンを一時DBに保存する。ノイズ判定も行う。"""
        is_noise = self._is_noise(player_text)
        with sqlite3.connect(self._temp_db) as conn:
            conn.execute(
                "INSERT INTO turns (session_id, timestamp, player_text, ai_text, is_noise) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.session_id, time.time(), player_text, ai_text, int(is_noise)),
            )

    def get_session_context(self, skip_recent: int = 3, limit: int = 5) -> str:
        """
        今セッションの会話履歴のうち、直近 skip_recent 件を除いた過去の話題を返す。
        会話履歴ウィンドウ（3ターン）では見えなくなった話題を復活させるために使う。

        Args:
            skip_recent: AIの会話履歴ウィンドウと重複しないようスキップする件数
            limit:       返す最大件数（古い順）
        """
        with sqlite3.connect(self._temp_db) as conn:
            # 新しい順で skip_recent+limit 件取得し、古い skip_recent 件を除外する
            rows = conn.execute(
                "SELECT player_text, ai_text FROM turns "
                "WHERE session_id=? AND is_noise=0 "
                "ORDER BY timestamp DESC LIMIT ?",
                (self.session_id, skip_recent + limit),
            ).fetchall()

        # 直近 skip_recent 件は会話履歴と重複するので除外、残りを時系列順に戻す
        older = list(reversed(rows[skip_recent:]))
        if not older:
            return ""

        lines = [f"P: {p[:50]}  A: {a[:50]}" for p, a in older]
        return "【今セッションの過去の話題】\n" + "\n".join(lines)

    def get_long_term_context(self) -> str:
        """
        長期記憶DBから直近セッションの要約とknown_factsを取得し、
        プロンプト注入用テキストを返す。情報がなければ空文字を返す。
        """
        parts: list[str] = []

        with sqlite3.connect(self._long_db) as conn:
            # 直近3セッション分の要約
            rows = conn.execute(
                "SELECT session_date, summary, game FROM memories "
                "ORDER BY timestamp DESC LIMIT 3"
            ).fetchall()
            if rows:
                lines = []
                for date, summary, game in rows:
                    game_note = f"（{game}）" if game else ""
                    lines.append(f"[{date}{game_note}] {summary}")
                parts.append("【過去の会話記憶】\n" + "\n".join(lines))

            # 信頼度が高いknown_facts（上位5件）
            facts = conn.execute(
                "SELECT fact FROM known_facts ORDER BY confidence DESC, timestamp DESC LIMIT 5"
            ).fetchall()
            if facts:
                fact_lines = [f"- {f[0]}" for f in facts]
                parts.append("【プレイヤーについて知っていること】\n" + "\n".join(fact_lines))

        if not parts:
            return ""

        result = "\n\n".join(parts)
        # 長すぎる場合は切り詰め
        if len(result) > MAX_LONG_TERM_CHARS:
            result = result[:MAX_LONG_TERM_CHARS] + "…"
        return result

    def compress_and_save(self, game: str = "") -> None:
        """
        セッション内の有効ターンをLLMで圧縮し、長期DBに保存する。
        ターン数が少なすぎる場合はスキップ。
        """
        turns = self._get_session_turns()
        if len(turns) < MIN_TURNS_TO_COMPRESS:
            logger.debug("AIMemory: too few turns (%d), skipping compression", len(turns))
            return

        logger.info("AIMemory: compressing %d turns into long-term memory...", len(turns))
        summary = self._call_llm_compress(turns)
        if not summary:
            logger.warning("AIMemory: LLM compression failed, skipping save")
            return

        facts = self._call_llm_extract_facts(turns)

        today = time.strftime("%Y-%m-%d")
        with sqlite3.connect(self._long_db) as conn:
            conn.execute(
                "INSERT INTO memories (timestamp, session_date, summary, raw_turn_count, game) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), today, summary, len(turns), game),
            )
            for fact in facts:
                conn.execute(
                    "INSERT INTO known_facts (timestamp, fact, confidence, source_session) "
                    "VALUES (?, ?, ?, ?)",
                    (time.time(), fact, 0.8, self.session_id),
                )
            # 古いメモリをローテーション（最大20セッション・最大50ファクト）
            conn.execute(
                "DELETE FROM memories WHERE id NOT IN "
                "(SELECT id FROM memories ORDER BY timestamp DESC LIMIT 20)"
            )
            conn.execute(
                "DELETE FROM known_facts WHERE id NOT IN "
                "(SELECT id FROM known_facts ORDER BY confidence DESC, timestamp DESC LIMIT 50)"
            )

        logger.info(
            "AIMemory: saved summary (%d chars) + %d facts", len(summary), len(facts)
        )

    def get_session_turn_count(self) -> int:
        """現セッションの有効ターン数（ノイズ除く）を返す"""
        with sqlite3.connect(self._temp_db) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE session_id=? AND is_noise=0",
                (self.session_id,),
            ).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # プライベートメソッド
    # ------------------------------------------------------------------

    def _init_temp_db(self) -> None:
        with sqlite3.connect(self._temp_db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS turns (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT    NOT NULL,
                    timestamp   REAL    NOT NULL,
                    player_text TEXT    NOT NULL,
                    ai_text     TEXT    NOT NULL,
                    is_noise    INTEGER DEFAULT 0
                )
            """)

    def _init_long_db(self) -> None:
        with sqlite3.connect(self._long_db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       REAL    NOT NULL,
                    session_date    TEXT    NOT NULL,
                    summary         TEXT    NOT NULL,
                    raw_turn_count  INTEGER,
                    game            TEXT    DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS known_facts (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       REAL    NOT NULL,
                    fact            TEXT    NOT NULL,
                    confidence      REAL    DEFAULT 0.8,
                    source_session  TEXT
                )
            """)

    def _is_noise(self, text: str) -> bool:
        """プレイヤー発言がノイズかどうか判定する"""
        if len(text.strip()) <= NOISE_MIN_CHARS:
            return True
        if text.strip() in _NOISE_WORDS:
            return True
        # 同じ文字の繰り返し（ははははは等）
        stripped = text.strip()
        if len(set(stripped)) <= 2 and len(stripped) >= 4:
            return True
        return False

    def _get_session_turns(self) -> list[dict]:
        """現セッションのノイズ以外のターンを返す"""
        with sqlite3.connect(self._temp_db) as conn:
            rows = conn.execute(
                "SELECT player_text, ai_text FROM turns "
                "WHERE session_id=? AND is_noise=0 ORDER BY timestamp",
                (self.session_id,),
            ).fetchall()
        return [{"player": r[0], "ai": r[1]} for r in rows]

    def _call_llm_compress(self, turns: list[dict]) -> str:
        """会話ターンをLLMで要約する"""
        turns_text = "\n".join(
            f"プレイヤー: {t['player']}\nAI: {t['ai']}" for t in turns
        )
        prompt = (
            "以下は今日のゲームセッション中の会話記録です。\n"
            "ノイズ（笑い声・意味不明な繰り返し発言・ゲーム音の誤認識）を除外し、\n"
            "「ユーザーが話したこと・感じたこと・話題になったこと」として価値ある情報だけを\n"
            "200字以内の日本語で箇条書き要約してください。\n\n"
            f"{turns_text}\n\n要約:"
        )
        return self._ollama_call(prompt, max_tokens=250)

    def _call_llm_extract_facts(self, turns: list[dict]) -> list[str]:
        """会話から「プレイヤーについての事実」を抽出する"""
        turns_text = "\n".join(
            f"プレイヤー: {t['player']}" for t in turns
        )
        prompt = (
            "以下のプレイヤーの発言から、次回以降も覚えておくべき事実を最大3件抽出してください。\n"
            "例: 「エズリアルが好き」「朝のプレイが多い」「ゴールド帯でプレイしている」\n"
            "抽出できなければ空行のみを返してください。JSONや番号は不要、1件1行で出力してください。\n\n"
            f"{turns_text}\n\n事実:"
        )
        raw = self._ollama_call(prompt, max_tokens=150)
        if not raw:
            return []
        facts = [line.strip().lstrip("・-・") for line in raw.split("\n") if line.strip()]
        return [f for f in facts if 5 <= len(f) <= 50][:3]

    def _ollama_call(self, prompt: str, max_tokens: int = 200) -> str:
        """Ollama API を呼び出してテキストを生成する"""
        try:
            res = httpx.post(
                OLLAMA_API_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,
                    "options": {"num_predict": max_tokens, "temperature": 0.3},
                },
                timeout=OLLAMA_TIMEOUT,
            )
            if res.status_code != 200:
                logger.error("AIMemory Ollama HTTP %d", res.status_code)
                return ""
            import re
            raw = res.json().get("response", "")
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            return raw
        except Exception as e:
            logger.error("AIMemory Ollama error: %s", e)
            return ""
