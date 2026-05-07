"""
match_review.py — 試合後レビューエンジン (LS+Curtis 複合スタイル)

Riot APIから取得した match + timeline を解析し、ADC視点の指標を抽出して
改善ポイント（ImprovementPoint）のリストを生成する。

改善ポイント検出ロジック:
- LS派ミクロ: CS@10 / CS/min / 早期デス / 視界スコア
- Curtis派マクロ: gold差@15 / damage_share / objective関与 / マッチアップ判定
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
    # Curtis派マクロ指標
    gold_diff_at_15: int = 0
    damage_share: float = 0.0
    objective_damage_share: float = 0.0
    early_solo_deaths: int = 0  # 〜10分のデス数（LS派が最も嫌うパターン）
    # 時系列データ（Chart.js用）
    minute_series: list[int] = field(default_factory=list)
    my_cs_series: list[int] = field(default_factory=list)
    enemy_cs_series: list[int] = field(default_factory=list)
    my_gold_series: list[int] = field(default_factory=list)
    enemy_gold_series: list[int] = field(default_factory=list)

    @property
    def kda(self) -> float:
        return (self.kills + self.assists) / max(1, self.deaths)


@dataclass
class ImprovementPoint:
    """改善ポイント1件"""
    category: str         # "cs", "deaths", "vision", "matchup", "macro_gold", "macro_damage", "macro_objective"
    severity: str         # "critical", "major", "minor"
    title: str
    detail: str
    suggestion: str
    school: str = "MIXED"  # "LS" / "CURTIS" / "MIXED" — どちらの哲学に基づく改善か


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
    frames = timeline.get("info", {}).get("frames", [])
    if minute >= len(frames):
        return 0
    pf = frames[minute].get("participantFrames", {}).get(str(participant_id), {})
    return pf.get("minionsKilled", 0) + pf.get("jungleMinionsKilled", 0)


def _gold_at_minute(timeline: dict, participant_id: int, minute: int) -> int:
    frames = timeline.get("info", {}).get("frames", [])
    if minute >= len(frames):
        return 0
    pf = frames[minute].get("participantFrames", {}).get(str(participant_id), {})
    return pf.get("totalGold", 0)


def _participant_series(timeline: dict, pid: int, key_func) -> list[int]:
    """各フレームから値を取得した時系列リスト"""
    frames = timeline.get("info", {}).get("frames", [])
    return [
        key_func(frame.get("participantFrames", {}).get(str(pid), {}))
        for frame in frames
    ]


def _death_timestamps(timeline: dict, participant_id: int) -> list[float]:
    deaths: list[float] = []
    for frame in timeline.get("info", {}).get("frames", []):
        for ev in frame.get("events", []):
            if ev.get("type") == "CHAMPION_KILL" and ev.get("victimId") == participant_id:
                deaths.append(round(ev.get("timestamp", 0) / 60000.0, 2))
    return deaths


def _extract_lane_opponents(match: dict, my_pid: int) -> tuple[Optional[dict], Optional[dict], Optional[dict]]:
    """敵ADC, 敵Sup, 自Supのparticipant dictを返す"""
    participants = match.get("info", {}).get("participants", [])
    me = next((p for p in participants if p.get("participantId") == my_pid), None)
    if not me:
        return None, None, None

    my_team = me.get("teamId")
    enemy_adc = next(
        (p for p in participants
         if p.get("teamId") != my_team and p.get("teamPosition") == "BOTTOM"),
        None,
    )
    enemy_sup = next(
        (p for p in participants
         if p.get("teamId") != my_team and p.get("teamPosition") == "UTILITY"),
        None,
    )
    my_sup = next(
        (p for p in participants
         if p.get("teamId") == my_team and p.get("teamPosition") == "UTILITY"),
        None,
    )
    return enemy_adc, enemy_sup, my_sup


def _team_damage_total(match: dict, my_team: int) -> int:
    return sum(
        p.get("totalDamageDealtToChampions", 0)
        for p in match.get("info", {}).get("participants", [])
        if p.get("teamId") == my_team
    )


def _team_objective_damage(match: dict, my_team: int) -> int:
    return sum(
        p.get("damageDealtToObjectives", 0)
        for p in match.get("info", {}).get("participants", [])
        if p.get("teamId") == my_team
    )


def extract_stats(match: dict, timeline: dict, my_puuid: str) -> Optional[MatchStats]:
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
    death_ts = _death_timestamps(timeline, pid)

    # gold_diff_at_15: 自分 - 敵ADC
    gold_diff_15 = 0
    if enemy_adc:
        my_gold_15 = _gold_at_minute(timeline, pid, 15)
        en_gold_15 = _gold_at_minute(timeline, enemy_adc["participantId"], 15)
        gold_diff_15 = my_gold_15 - en_gold_15

    # damage_share / objective_damage_share
    my_team = me.get("teamId")
    my_dmg = me.get("totalDamageDealtToChampions", 0)
    team_dmg = _team_damage_total(match, my_team)
    dmg_share = my_dmg / team_dmg if team_dmg else 0.0

    my_obj_dmg = me.get("damageDealtToObjectives", 0)
    team_obj_dmg = _team_objective_damage(match, my_team)
    obj_dmg_share = my_obj_dmg / team_obj_dmg if team_obj_dmg else 0.0

    early_solo = sum(1 for t in death_ts if t < 10)

    # 時系列（CS / Gold）
    cs_key = lambda pf: pf.get("minionsKilled", 0) + pf.get("jungleMinionsKilled", 0)
    gold_key = lambda pf: pf.get("totalGold", 0)
    my_cs_series = _participant_series(timeline, pid, cs_key)
    my_gold_series = _participant_series(timeline, pid, gold_key)
    if enemy_adc:
        en_pid = enemy_adc["participantId"]
        enemy_cs_series = _participant_series(timeline, en_pid, cs_key)
        enemy_gold_series = _participant_series(timeline, en_pid, gold_key)
    else:
        enemy_cs_series = []
        enemy_gold_series = []
    minute_series = list(range(len(my_cs_series)))

    return MatchStats(
        match_id=info.get("gameId") and match.get("metadata", {}).get("matchId", ""),
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
        death_timestamps_min=death_ts,
        enemy_adc=enemy_adc.get("championName") if enemy_adc else None,
        enemy_support=enemy_sup.get("championName") if enemy_sup else None,
        my_support=my_sup.get("championName") if my_sup else None,
        gold_diff_at_15=gold_diff_15,
        damage_share=round(dmg_share, 3),
        objective_damage_share=round(obj_dmg_share, 3),
        early_solo_deaths=early_solo,
        minute_series=minute_series,
        my_cs_series=my_cs_series,
        enemy_cs_series=enemy_cs_series,
        my_gold_series=my_gold_series,
        enemy_gold_series=enemy_gold_series,
    )


# ---------------------------------------------------------------------------
# 改善ポイント検出（LS派ミクロ + Curtis派マクロ）
# ---------------------------------------------------------------------------

def _gap(actual: float, target: float) -> float:
    return round(actual - target, 2)


def detect_points(stats: MatchStats, benchmark: dict) -> list[ImprovementPoint]:
    points: list[ImprovementPoint] = []
    kb = get_knowledge()

    # ============================================================
    # LS派ミクロ: 基礎ライン（崩れていたら最優先）
    # ============================================================

    # CS@10 — LS核心指標
    cs10_target = benchmark.get("cs_at_10", 75)
    if stats.cs_at_10 < cs10_target - 15:
        points.append(ImprovementPoint(
            category="cs", severity="critical", school="LS",
            title=f"CS@10が致命的に不足（{stats.cs_at_10} / 目標{cs10_target}）",
            detail=f"差: {_gap(stats.cs_at_10, cs10_target)}・LS基準で許容外",
            suggestion="プラクティスツールで10分80CS到達を3連続で出してから次のランクに入る。基礎が無い人はランク上がる資格無し。",
        ))
    elif stats.cs_at_10 < cs10_target:
        points.append(ImprovementPoint(
            category="cs", severity="major", school="LS",
            title=f"CS@10が基準未達（{stats.cs_at_10} / 目標{cs10_target}）",
            detail=f"差: {_gap(stats.cs_at_10, cs10_target)}",
            suggestion="トレード後にCSを優先する判断を意識。ハラスとCSのバランスを再点検。",
        ))

    # CS/min — LS核心指標
    csm_target = benchmark.get("cs_per_min", 7.0)
    if stats.cs_per_min < csm_target - 1.5:
        points.append(ImprovementPoint(
            category="cs", severity="critical", school="LS",
            title=f"CS/minが基準を大幅に下回る（{stats.cs_per_min} / 目標{csm_target}）",
            detail=f"全試合通じての回収力不足。{_gap(stats.cs_per_min, csm_target)}/min",
            suggestion="サイドレーンのウェーブ取り・ジャングルキャンプ取得を意識。CS/minは妥協できない指標。",
        ))

    # 早期ソロデス — LS派最大の嫌悪パターン
    if stats.early_solo_deaths >= 3:
        points.append(ImprovementPoint(
            category="deaths", severity="critical", school="LS",
            title=f"序盤ソロデス過多（10分以内に{stats.early_solo_deaths}回）",
            detail=f"全死亡時刻: {stats.death_timestamps_min}",
            suggestion="ポジショニングと wave 管理が崩壊している。Doran's Shield + サポ密着で最低5分は無事故を目指す。",
        ))

    # 総デス上限 — LS流厳しめ
    deaths_max = benchmark.get("deaths_max", 5)
    if stats.deaths > deaths_max + 2:
        severity = "critical" if stats.deaths >= deaths_max + 4 else "major"
        suggestion = f"目標は{deaths_max}デス以下。デスは全て『何かを学ぶ機会』だが、{stats.deaths}回はマップ判断・wave読みの不足。"
        points.append(ImprovementPoint(
            category="deaths", severity=severity, school="LS",
            title=f"デス過多（{stats.deaths}回 / 目標{deaths_max}以下）",
            detail=f"死亡時刻: {stats.death_timestamps_min}",
            suggestion=suggestion,
        ))

    # ============================================================
    # Curtis派マクロ: メイン鍛錬対象
    # ============================================================

    # ゴールド差@15 — Curtis派マクロ優勢度
    gold_diff_target = benchmark.get("gold_diff_at_15", 0)
    if stats.gold_diff_at_15 < gold_diff_target - 800:
        points.append(ImprovementPoint(
            category="macro_gold", severity="major", school="CURTIS",
            title=f"ゴールド差@15が大幅に劣勢（{stats.gold_diff_at_15:+d} / 目標{gold_diff_target:+d}）",
            detail=f"レーン戦のマクロ的優劣指標。差: {stats.gold_diff_at_15 - gold_diff_target}",
            suggestion="wave management 見直し。Push/Freeze/Slow Pushを目的別に使い分け。レーン勝てなくても tempo を作って tax で取り戻す。",
        ))

    # ダメージシェア — Curtis派 ADCの仕事率
    dmg_share_target = benchmark.get("damage_share", 0.30)
    if stats.damage_share < dmg_share_target - 0.05:
        points.append(ImprovementPoint(
            category="macro_damage", severity="major", school="CURTIS",
            title=f"ダメージシェア不足（{int(stats.damage_share * 100)}% / 目標{int(dmg_share_target * 100)}%+）",
            detail=f"ADCの仕事はダメージを出すこと。チーム寄与{int(stats.damage_share * 100)}%は不足。",
            suggestion="集団戦での positioning 見直し。サポ後ろ・タンク手前・スキル射程ギリギリでDPSを出し切る。",
        ))

    # 視界スコア — Curtis核心指標
    vs_target = benchmark.get("vision_score_min", 22)
    if stats.vision_score < vs_target - 5:
        points.append(ImprovementPoint(
            category="vision", severity="major", school="CURTIS",
            title=f"視界スコアが大幅に不足（{stats.vision_score} / 目標{vs_target}+）",
            detail="Curtis派核心『視界=情報=判断』。視界が無いとマクロは作れない。",
            suggestion="ベース戻り毎にコントロールワード必須購入。サポ任せにせず自分でも置く。トリンケットも切れ目に必ず使い切る。",
        ))
    elif stats.vision_score < vs_target:
        points.append(ImprovementPoint(
            category="vision", severity="minor", school="CURTIS",
            title=f"視界スコアが基準未達（{stats.vision_score} / 目標{vs_target}+）",
            detail=f"差: {_gap(stats.vision_score, vs_target)}",
            suggestion="コントロールワード購入率を上げる。トリンケットのcooldown切れ目を意識。",
        ))

    # オブジェクト関与 — Curtis派マクロ
    obj_target = benchmark.get("objective_participation", 0.50)
    if stats.objective_damage_share < obj_target - 0.10:
        points.append(ImprovementPoint(
            category="macro_objective", severity="minor", school="CURTIS",
            title=f"オブジェクト関与不足（ダメージシェア{int(stats.objective_damage_share * 100)}%）",
            detail=f"ドラ/ヘラ/タワーへのDPS寄与が低い。",
            suggestion="ドラ/ヘラ spawn 3分前から wave を整える。ADC不在でobjective取りに行くのは負け確。",
        ))

    # ============================================================
    # マッチアップ（LS+Curtis共通）
    # ============================================================

    if stats.enemy_adc:
        m = kb.matchup(stats.champion, stats.enemy_adc)
        if m and m["score"] <= -1 and stats.deaths >= 4:
            points.append(ImprovementPoint(
                category="matchup", severity="major", school="MIXED",
                title=f"不利マッチアップ {stats.champion} vs {stats.enemy_adc} で過剰なデス",
                detail=f"マッチアップ評価: {m['score']} ({m.get('source', 'inferred')})",
                suggestion=m.get("tip") or "不利マッチでは安全プレイとスケーリング重視。Doran's Shield + サポpeel で耐える。",
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
