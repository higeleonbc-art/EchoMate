"""
match_review.py — 試合後レビューエンジン

Riot APIから取得した match + timeline を解析し、ADC視点の指標を抽出して
改善ポイント（ImprovementPoint）のリストを生成する。

主要関数:
    build_review(match, timeline, my_puuid, rank) -> Review
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from adc_knowledge import get_knowledge

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class MatchStats:
    """1試合のADCスタッツ要約"""
    match_id: str
    champion: str
    win: bool
    duration_min: float
    kills: int
    deaths: int
    assists: int
    cs_total: int
    cs_at_10: int
    cs_at_15: int
    cs_per_min: float
    vision_score: int
    death_timestamps_min: list[float]
    enemy_adc: Optional[str] = None
    enemy_support: Optional[str] = None
    my_support: Optional[str] = None

    @property
    def kda(self) -> float:
        return (self.kills + self.assists) / max(1, self.deaths)


@dataclass
class ImprovementPoint:
    """改善ポイント1件"""
    category: str         # "cs", "deaths", "vision", "matchup", "macro"
    severity: str         # "critical", "major", "minor"
    title: str            # 「CS@10が基準を大幅に下回る」
    detail: str           # 数値や具体的事実
    suggestion: str       # 「Lv1からウェーブ管理を意識し、ラストヒットの精度を上げる」


@dataclass
class Review:
    stats: MatchStats
    target_rank: str
    benchmark: dict
    points: list[ImprovementPoint] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 抽出ロジック
# ---------------------------------------------------------------------------

def _find_participant_id(match: dict, puuid: str) -> Optional[int]:
    for p in match.get("info", {}).get("participants", []):
        if p.get("puuid") == puuid:
            return p.get("participantId")
    return None


def _cs_at_minute(timeline: dict, participant_id: int, minute: int) -> int:
    """timeline frames から指定分のCS数を取得（minionsKilled + jungleMinionsKilled）"""
    frames = timeline.get("info", {}).get("frames", [])
    if minute >= len(frames):
        return 0
    pf = frames[minute].get("participantFrames", {}).get(str(participant_id), {})
    return pf.get("minionsKilled", 0) + pf.get("jungleMinionsKilled", 0)


def _death_timestamps(timeline: dict, participant_id: int) -> list[float]:
    """自分が死んだ時刻（分）のリスト"""
    deaths: list[float] = []
    for frame in timeline.get("info", {}).get("frames", []):
        for ev in frame.get("events", []):
            if ev.get("type") == "CHAMPION_KILL" and ev.get("victimId") == participant_id:
                deaths.append(round(ev.get("timestamp", 0) / 60000.0, 2))
    return deaths


def _extract_lane_opponents(match: dict, my_pid: int) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """敵ADC, 敵Sup, 自Supのチャンプ名を返す"""
    participants = match.get("info", {}).get("participants", [])
    me = next((p for p in participants if p.get("participantId") == my_pid), None)
    if not me:
        return None, None, None

    my_team = me.get("teamId")
    enemy_adc = next(
        (p for p in participants
         if p.get("teamId") != my_team
         and p.get("teamPosition") == "BOTTOM"),
        None,
    )
    enemy_sup = next(
        (p for p in participants
         if p.get("teamId") != my_team
         and p.get("teamPosition") == "UTILITY"),
        None,
    )
    my_sup = next(
        (p for p in participants
         if p.get("teamId") == my_team
         and p.get("teamPosition") == "UTILITY"),
        None,
    )
    return (
        enemy_adc.get("championName") if enemy_adc else None,
        enemy_sup.get("championName") if enemy_sup else None,
        my_sup.get("championName") if my_sup else None,
    )


def extract_stats(match: dict, timeline: dict, my_puuid: str) -> Optional[MatchStats]:
    """match + timeline → MatchStats。puuidが見つからなければ None。"""
    pid = _find_participant_id(match, my_puuid)
    if pid is None:
        return None

    info = match.get("info", {})
    participants = info.get("participants", [])
    me = next((p for p in participants if p.get("participantId") == pid), None)
    if not me:
        return None

    duration_sec = info.get("gameDuration", 0)
    duration_min = max(1.0, duration_sec / 60.0)
    cs_total = me.get("totalMinionsKilled", 0) + me.get("neutralMinionsKilled", 0)

    enemy_adc, enemy_sup, my_sup = _extract_lane_opponents(match, pid)

    return MatchStats(
        match_id=match.get("metadata", {}).get("matchId", ""),
        champion=me.get("championName", ""),
        win=me.get("win", False),
        duration_min=round(duration_min, 1),
        kills=me.get("kills", 0),
        deaths=me.get("deaths", 0),
        assists=me.get("assists", 0),
        cs_total=cs_total,
        cs_at_10=_cs_at_minute(timeline, pid, 10),
        cs_at_15=_cs_at_minute(timeline, pid, 15),
        cs_per_min=round(cs_total / duration_min, 2),
        vision_score=me.get("visionScore", 0),
        death_timestamps_min=_death_timestamps(timeline, pid),
        enemy_adc=enemy_adc,
        enemy_support=enemy_sup,
        my_support=my_sup,
    )


# ---------------------------------------------------------------------------
# 改善ポイント抽出（ルールベース）
# ---------------------------------------------------------------------------

def _gap(actual: float, target: float) -> float:
    return round(actual - target, 2)


def detect_points(stats: MatchStats, benchmark: dict) -> list[ImprovementPoint]:
    points: list[ImprovementPoint] = []
    kb = get_knowledge()

    # CS@10
    cs10_target = benchmark.get("cs_at_10", 75)
    if stats.cs_at_10 < cs10_target - 15:
        points.append(ImprovementPoint(
            category="cs",
            severity="critical",
            title=f"CS@10が基準を大幅に下回る（{stats.cs_at_10} / 目標{cs10_target}）",
            detail=f"差: {_gap(stats.cs_at_10, cs10_target)}",
            suggestion="ラストヒットの基礎練習を1日10分。プラクティスツールでミニオン全取りを目指す。",
        ))
    elif stats.cs_at_10 < cs10_target:
        points.append(ImprovementPoint(
            category="cs",
            severity="major",
            title=f"CS@10が基準より低い（{stats.cs_at_10} / 目標{cs10_target}）",
            detail=f"差: {_gap(stats.cs_at_10, cs10_target)}",
            suggestion="トレード後にCSを優先する判断を意識。ハラスとCSのバランス見直し。",
        ))

    # CS/min
    csm_target = benchmark.get("cs_per_min", 7.0)
    if stats.cs_per_min < csm_target - 1.5:
        points.append(ImprovementPoint(
            category="cs",
            severity="critical",
            title=f"CS/minが基準を大幅に下回る（{stats.cs_per_min} / 目標{csm_target}）",
            detail=f"全試合通じての回収力不足。{_gap(stats.cs_per_min, csm_target)}/min",
            suggestion="サイドレーンのウェーブ取り・ジャングルキャンプ取得を意識。デス減もCS/min向上に直結。",
        ))

    # デス
    deaths_max = benchmark.get("deaths_max", 5)
    if stats.deaths > deaths_max:
        # 早期ソロデスの判定
        early_solo = sum(1 for t in stats.death_timestamps_min if t < 10)
        severity = "critical" if stats.deaths >= deaths_max + 3 else "major"
        suggestion = f"目標は{deaths_max}デス以下。"
        if early_solo >= 2:
            suggestion += f" 序盤ソロデスが{early_solo}回あり、レーンのpositioningとwave管理を見直す。"
        points.append(ImprovementPoint(
            category="deaths",
            severity=severity,
            title=f"デス過多（{stats.deaths}回 / 目標{deaths_max}以下）",
            detail=f"死亡時刻: {stats.death_timestamps_min}",
            suggestion=suggestion,
        ))

    # 視界スコア
    vs_target = benchmark.get("vision_score_min", 20)
    if stats.vision_score < vs_target:
        points.append(ImprovementPoint(
            category="vision",
            severity="minor",
            title=f"視界スコアが低い（{stats.vision_score} / 目標{vs_target}+）",
            detail="コントロールワード購入とトリンケット切れ目を意識する。",
            suggestion="ベースに戻る度にコントロールワードを買い、敵ジャングル侵入時に置く習慣をつける。",
        ))

    # マッチアップ
    if stats.enemy_adc:
        m = kb.matchup(stats.champion, stats.enemy_adc)
        if m and m["score"] <= -1 and stats.deaths >= 4:
            points.append(ImprovementPoint(
                category="matchup",
                severity="major",
                title=f"不利マッチアップ {stats.champion} vs {stats.enemy_adc} で過剰なデス",
                detail=f"マッチアップ評価: {m['score']} ({m.get('source', 'inferred')})",
                suggestion=m.get("tip") or "不利マッチでは安全プレイとスケーリング重視。",
            ))

    return points


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def build_review(
    match: dict,
    timeline: dict,
    my_puuid: str,
    rank: str = "GOLD",
) -> Optional[Review]:
    stats = extract_stats(match, timeline, my_puuid)
    if not stats:
        logger.warning("Player puuid not found in match")
        return None

    kb = get_knowledge()
    benchmark = kb.benchmark(rank) or kb.benchmark("GOLD") or {}
    points = detect_points(stats, benchmark)

    return Review(stats=stats, target_rank=rank, benchmark=benchmark, points=points)
