"""
coach_cache.py — match と timeline の SQLite キャッシュ

Riot API から取得した試合データは試合終了後変化しない (immutable) ため、
ローカルにキャッシュして 2回目以降の再取得を回避する。

スキーマ:
    matches (
        match_id    TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,   -- 'match' or 'timeline'
        data        TEXT NOT NULL,   -- JSON 文字列
        fetched_at  TEXT NOT NULL,
        PRIMARY KEY (match_id, kind)
    )
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / ".coach_match_cache.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            match_id    TEXT NOT NULL,
            kind        TEXT NOT NULL,
            data        TEXT NOT NULL,
            fetched_at  TEXT NOT NULL,
            PRIMARY KEY (match_id, kind)
        )
    """)
    return conn


def get_match(match_id: str) -> Optional[dict]:
    return _get(match_id, "match")


def get_timeline(match_id: str) -> Optional[dict]:
    return _get(match_id, "timeline")


def _get(match_id: str, kind: str) -> Optional[dict]:
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT data FROM matches WHERE match_id=? AND kind=?",
                (match_id, kind),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["data"])
    except Exception as e:
        logger.warning("cache read failed %s/%s: %s", match_id, kind, e)
        return None


def put_match(match_id: str, data: dict) -> None:
    _put(match_id, "match", data)


def put_timeline(match_id: str, data: dict) -> None:
    _put(match_id, "timeline", data)


def _put(match_id: str, kind: str, data: dict) -> None:
    try:
        now = datetime.now(timezone.utc).isoformat()
        with _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO matches (match_id, kind, data, fetched_at) "
                "VALUES (?, ?, ?, ?)",
                (match_id, kind, json.dumps(data, ensure_ascii=False), now),
            )
    except Exception as e:
        logger.warning("cache write failed %s/%s: %s", match_id, kind, e)


def stats() -> dict:
    """キャッシュ統計 (デバッグ用)"""
    try:
        with _conn() as c:
            total = c.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
            by_kind = dict(c.execute(
                "SELECT kind, COUNT(*) FROM matches GROUP BY kind"
            ).fetchall())
        return {"total": total, **by_kind}
    except Exception as e:
        return {"error": str(e)}


def clear() -> int:
    try:
        with _conn() as c:
            n = c.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
            c.execute("DELETE FROM matches")
        return n
    except Exception as e:
        logger.warning("cache clear failed: %s", e)
        return 0
