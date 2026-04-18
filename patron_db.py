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
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    text TEXT NOT NULL,
                    game TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS growth_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    text TEXT NOT NULL,
                    consumed INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_logs_analyzed ON logs(analyzed);
                CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp);
                CREATE INDEX IF NOT EXISTS idx_episodes_timestamp ON episodes(timestamp);
                CREATE INDEX IF NOT EXISTS idx_obs_consumed ON growth_observations(consumed);
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

    def reset_all(self) -> None:
        """全テーブルのデータを削除してオートインクリメントをリセットする"""
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM logs")
                conn.execute("DELETE FROM summaries")
                conn.execute("DELETE FROM episodes")
                conn.execute("DELETE FROM growth_observations")
                conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN "
                    "('logs','summaries','episodes','growth_observations')"
                )
        logger.info("PatronDB: all tables reset")

    # ------------------------------------------------------------------
    # エピソード記憶 (episodes テーブル)
    # ------------------------------------------------------------------

    def add_episode(self, text: str, game: str = "", timestamp: Optional[float] = None) -> None:
        """印象的なエピソードを記録する（最大 MAX_EPISODES 件でローテーション）"""
        MAX_EPISODES = 50
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO episodes (timestamp, text, game) VALUES (?, ?, ?)",
                    (timestamp or time.time(), text, game),
                )
                count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
                if count > MAX_EPISODES:
                    excess = count - MAX_EPISODES
                    conn.execute(
                        "DELETE FROM episodes WHERE id IN "
                        "(SELECT id FROM episodes ORDER BY timestamp ASC LIMIT ?)",
                        (excess,),
                    )

    def get_episodes(self, limit: int = 10) -> list[dict]:
        """最新の limit 件のエピソードを返す"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT timestamp, text, game FROM episodes ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def migrate_episodes_from_list(self, episodes: list[dict]) -> None:
        """user_profile.json から移行用: リストを一括インサート（既存データがない場合のみ）"""
        with self._lock:
            with self._connect() as conn:
                existing = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
                if existing > 0:
                    return
                for ep in episodes:
                    conn.execute(
                        "INSERT INTO episodes (timestamp, text, game) VALUES (?, ?, ?)",
                        (ep.get("timestamp", time.time()), ep.get("text", ""), ep.get("game", "")),
                    )

    # ------------------------------------------------------------------
    # 成長観察メモ (growth_observations テーブル)
    # ------------------------------------------------------------------

    def add_growth_observation(self, text: str, timestamp: Optional[float] = None) -> None:
        """成長観察メモを追加する（最大 20 件でローテーション）"""
        MAX_OBS = 20
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO growth_observations (timestamp, text) VALUES (?, ?)",
                    (timestamp or time.time(), text),
                )
                count = conn.execute("SELECT COUNT(*) FROM growth_observations").fetchone()[0]
                if count > MAX_OBS:
                    excess = count - MAX_OBS
                    conn.execute(
                        "DELETE FROM growth_observations WHERE id IN "
                        "(SELECT id FROM growth_observations ORDER BY timestamp ASC LIMIT ?)",
                        (excess,),
                    )

    def get_growth_observations(self, limit: int = 3) -> list[dict]:
        """最新 limit 件の成長観察（未消費）を返す"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, timestamp, text FROM growth_observations "
                "WHERE consumed = 0 ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def pop_latest_growth_observation(self) -> Optional[str]:
        """最新の未消費成長観察テキストを取り出し消費済みにマークする"""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id, text FROM growth_observations "
                    "WHERE consumed = 0 ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE growth_observations SET consumed = 1 WHERE id = ?",
                    (row["id"],),
                )
                return row["text"]

    def consume_observations(self, count: int) -> int:
        """古い成長観察を count 件消費済みにする（要約後の圧縮用）。消費した件数を返す。"""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id FROM growth_observations WHERE consumed = 0 "
                    "ORDER BY timestamp ASC LIMIT ?",
                    (count,),
                ).fetchall()
                ids = [r["id"] for r in rows]
                if ids:
                    conn.execute(
                        f"UPDATE growth_observations SET consumed = 1 "
                        f"WHERE id IN ({','.join('?' * len(ids))})",
                        ids,
                    )
                return len(ids)

    def migrate_observations_from_list(self, observations: list[dict]) -> None:
        """user_profile.json から移行用: リストを一括インサート（既存データがない場合のみ）"""
        with self._lock:
            with self._connect() as conn:
                existing = conn.execute("SELECT COUNT(*) FROM growth_observations").fetchone()[0]
                if existing > 0:
                    return
                for obs in observations:
                    conn.execute(
                        "INSERT INTO growth_observations (timestamp, text) VALUES (?, ?)",
                        (obs.get("timestamp", time.time()), obs.get("text", "")),
                    )
