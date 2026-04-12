"""
patron_db.py - 会話ログDBモジュール（良き隣人システム）

SQLiteを使用してユーザーとの会話ログを保存・管理する。
- 最大1000件のログを保持（ローテーション）
- 古いログは要約として圧縮
- すべてのデータはローカル保存
"""

import sqlite3
import json
import logging
import time
import threading
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = "patron_logs.db"
MAX_RAW_LOGS = 1000
ANALYZE_BATCH_SIZE = 50  # この件数ごとに分析をトリガー


class PatronDB:
    """会話ログと要約をSQLiteで管理するクラス"""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """テーブルを初期化する"""
        # executescript は自動コミットするため direct connect を使用
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    user_input TEXT DEFAULT '',
                    ai_response TEXT NOT NULL,
                    tags TEXT DEFAULT '[]',
                    emotion_score REAL DEFAULT 0.0,
                    analyzed INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    summary TEXT NOT NULL,
                    confidence REAL DEFAULT 0.5,
                    log_count INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_logs_analyzed ON logs(analyzed);
                CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp);
            """)
        finally:
            conn.close()
        logger.info("PatronDB initialized: %s", self.db_path)

    @contextmanager
    def _connect(self):
        """接続を自動クローズするコンテキストマネージャー（Windows対応）"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_log(
        self,
        event_type: str,
        ai_response: str,
        user_input: Optional[str] = None,
        tags: Optional[list] = None,
        emotion_score: float = 0.0,
    ) -> None:
        """会話ログを追加する"""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO logs
                       (timestamp, event_type, user_input, ai_response, tags, emotion_score)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        time.time(),
                        event_type,
                        user_input or "",
                        ai_response,
                        json.dumps(tags or [], ensure_ascii=False),
                        emotion_score,
                    ),
                )
                # ローテーションチェック
                count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
                if count > MAX_RAW_LOGS:
                    excess = count - MAX_RAW_LOGS
                    conn.execute(
                        "DELETE FROM logs WHERE id IN "
                        "(SELECT id FROM logs ORDER BY timestamp ASC LIMIT ?)",
                        (excess,),
                    )
                    logger.debug("Rotated %d old logs", excess)

    def get_unanalyzed_logs(self, limit: int = 100) -> list[dict]:
        """未分析ログをlimit件取得する"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM logs WHERE analyzed = 0 ORDER BY timestamp ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_unanalyzed(self) -> int:
        """未分析ログの件数を返す"""
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM logs WHERE analyzed = 0"
            ).fetchone()[0]

    def mark_analyzed(self, log_ids: list[int]) -> None:
        """指定IDのログを分析済みにマークする"""
        if not log_ids:
            return
        with self._lock:
            with self._connect() as conn:
                conn.executemany(
                    "UPDATE logs SET analyzed = 1 WHERE id = ?",
                    [(i,) for i in log_ids],
                )

    def add_summary(self, summary: str, confidence: float, log_count: int) -> None:
        """要約を保存する"""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO summaries (timestamp, summary, confidence, log_count) "
                    "VALUES (?, ?, ?, ?)",
                    (time.time(), summary, confidence, log_count),
                )
        logger.info("Summary added (confidence=%.2f, logs=%d)", confidence, log_count)

    def get_summaries(self, limit: int = 5) -> list[dict]:
        """最新の要約をlimit件取得する"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM summaries ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_logs(self, n: int = 20) -> list[dict]:
        """最新のn件のログを取得する"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?",
                (n,),
            ).fetchall()
        return [dict(r) for r in rows]

    def should_trigger_analysis(self, batch_size: int = ANALYZE_BATCH_SIZE) -> bool:
        """バッチ分析をトリガーすべきか判定する"""
        return self.count_unanalyzed() >= batch_size

    def get_total_log_count(self) -> int:
        """総ログ件数を返す"""
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
