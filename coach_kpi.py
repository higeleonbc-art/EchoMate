"""
coach_kpi.py — KPIトラッキングと達成評価

LLMコーチコメント末尾の「次試合の最優先KPI」を抽出してSQLiteに保存。
次回試合のレビュー時に、保存済み未評価KPIと実績を比較して達成度を判定する。

DBスキーマ:
    kpi_history (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        set_at       TEXT NOT NULL,           -- ISO timestamp (KPIを設定した時刻)
        from_match   TEXT NOT NULL,           -- KPI生成元のmatch_id
        kpi_type     TEXT NOT NULL,           -- 'vision_score' / 'cs_at_10' / 'deaths' / 'cs_per_min'
        target       REAL NOT NULL,           -- 目標値
        op           TEXT NOT NULL,           -- '>=' / '<=' / '=='
        eval_match   TEXT,                    -- 評価試合のmatch_id (NULL=未評価)
        actual       REAL,                    -- 実績値
        achieved     INTEGER                  -- 0/1, NULL=未評価
    )
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / ".coach_kpi.db"


# ---------------------------------------------------------------------------
# KPI抽出 (LLM出力からパース)
# ---------------------------------------------------------------------------

# パターン: 「視界スコア: 22」「CS@10 = 75」「死亡数: 4以下」など
_KPI_RULES: list[tuple[re.Pattern, str, str]] = [
    # (regex, kpi_type, op)
    (re.compile(r"視界スコア(?:最低)?\s*[:：=]\s*(\d+)"), "vision_score", ">="),
    (re.compile(r"CS\s*@\s*10\s*[:：=]\s*(\d+)"),         "cs_at_10",     ">="),
    (re.compile(r"CS\s*@\s*15\s*[:：=]\s*(\d+)"),         "cs_at_15",     ">="),
    (re.compile(r"CS\s*/\s*min\s*[:：=]\s*([\d\.]+)"),    "cs_per_min",   ">="),
    (re.compile(r"(?:死亡数|デス数|デス上限)\s*[:：=]?\s*(\d+)\s*(?:以下|まで)?"), "deaths", "<="),
    (re.compile(r"KDA\s*[:：=]\s*([\d\.]+)"),             "kda",          ">="),
]


def parse_kpis(text: str) -> list[tuple[str, float, str]]:
    """LLM出力テキストからKPIを抽出し [(type, target, op), ...] を返す。

    最後の「次試合の最優先KPI」セクションを優先するが、無ければ全文走査。
    重複は最後勝ち。
    """
    if not text:
        return []
    # KPIセクションがあればそこだけ走査
    section_match = re.search(r"(?:次試合の最優先KPI|KPI)[:：\n]+(.+?)(?:```|$)", text, re.DOTALL)
    target_text = section_match.group(1) if section_match else text

    found: dict[str, tuple[str, float, str]] = {}
    for rgx, kpi_type, op in _KPI_RULES:
        for m in rgx.finditer(target_text):
            try:
                val = float(m.group(1))
            except (ValueError, IndexError):
                continue
            found[kpi_type] = (kpi_type, val, op)
    return list(found.values())


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kpi_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            set_at      TEXT NOT NULL,
            from_match  TEXT NOT NULL,
            kpi_type    TEXT NOT NULL,
            target      REAL NOT NULL,
            op          TEXT NOT NULL,
            eval_match  TEXT,
            actual      REAL,
            achieved    INTEGER
        )
    """)
    conn.commit()
    return conn


def save_kpis(from_match_id: str, kpis: list[tuple[str, float, str]]) -> int:
    """新KPI群を保存。同一match_idの重複は無視（既登録なら何もしない）"""
    if not kpis:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    with _conn() as conn:
        # 既登録チェック (同じ from_match に対して保存済みなら skip)
        existing = conn.execute(
            "SELECT COUNT(*) FROM kpi_history WHERE from_match=?", (from_match_id,)
        ).fetchone()[0]
        if existing > 0:
            logger.debug("KPIs already saved for match %s, skipping", from_match_id)
            return 0
        for kpi_type, target, op in kpis:
            conn.execute(
                "INSERT INTO kpi_history (set_at, from_match, kpi_type, target, op) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, from_match_id, kpi_type, target, op),
            )
            inserted += 1
        conn.commit()
    return inserted


def get_pending_kpis() -> list[dict]:
    """未評価のKPI（最新の試合分のみ）を返す"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM kpi_history WHERE eval_match IS NULL ORDER BY set_at DESC"
        ).fetchall()
    if not rows:
        return []
    # 最新の from_match のものだけ返す
    latest_match = rows[0]["from_match"]
    return [dict(r) for r in rows if r["from_match"] == latest_match]


def evaluate_kpis(eval_match_id: str, stats) -> list[dict]:
    """直近の未評価KPIに対し、stats (MatchStats) で実績を判定して保存。

    Returns: 評価結果のリスト [{kpi_type, target, op, actual, achieved, ...}, ...]
    """
    pending = get_pending_kpis()
    if not pending:
        return []
    # 同じ試合ではevaluateしない
    if pending[0]["from_match"] == eval_match_id:
        return []

    results: list[dict] = []
    actual_map = {
        "vision_score": stats.vision_score,
        "cs_at_10":     stats.cs_at_10,
        "cs_at_15":     stats.cs_at_15,
        "cs_per_min":   stats.cs_per_min,
        "deaths":       stats.deaths,
        "kda":          stats.kda,
    }
    with _conn() as conn:
        for k in pending:
            actual = actual_map.get(k["kpi_type"])
            if actual is None:
                continue
            target = k["target"]
            op = k["op"]
            achieved = (
                (actual >= target) if op == ">=" else
                (actual <= target) if op == "<=" else
                (actual == target)
            )
            conn.execute(
                "UPDATE kpi_history SET eval_match=?, actual=?, achieved=? WHERE id=?",
                (eval_match_id, float(actual), int(achieved), k["id"]),
            )
            results.append({
                **k,
                "actual": float(actual),
                "achieved": bool(achieved),
                "eval_match": eval_match_id,
            })
        conn.commit()
    return results


def history(limit: int = 20) -> list[dict]:
    """最近N件のKPI履歴（評価済み・未評価両方）"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM kpi_history ORDER BY set_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
