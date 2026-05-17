"""
coach_mmr.py — 推定 MMR (Riot は本物の MMR を公開していないため概算)

ロジック:
  - Riot Web API で 現 rank (tier/division/LP) + ranked solo wins/losses を取得
  - 直近 N ranked 試合 (queue=420) の勝率を計算
  - 勝率と現rank から「実MMR ≈ rank / rank+1 / rank-1」を推定

精度限定の概算なので "Estimated" "rough" と明示すること。
"""

from __future__ import annotations

import logging
from typing import Optional

from riot_api import RiotAPIClient, RiotAPIError, QUEUE_RANKED_SOLO

logger = logging.getLogger(__name__)


TIER_ORDER = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD",
              "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
DIVISION_TO_NUM = {"IV": 0, "III": 1, "II": 2, "I": 3}


def _rank_to_score(tier: str, division: str, lp: int) -> float:
    """tier/division/lp をフラット数値に。1 tier = 400 score, 1 division = 100 score, LP = +0~99"""
    try:
        ti = TIER_ORDER.index(tier.upper())
    except ValueError:
        return 0.0
    di = DIVISION_TO_NUM.get((division or "").upper(), 0)
    # MASTER以上はdivisionなし
    if ti >= TIER_ORDER.index("MASTER"):
        return ti * 400 + 200 + lp
    return ti * 400 + di * 100 + lp


def _score_to_label(score: float) -> str:
    """フラット数値 → "GOLD III 42LP" 風表記"""
    ti_master = TIER_ORDER.index("MASTER")
    tier_idx = int(score // 400)
    if tier_idx >= ti_master:
        return f"{TIER_ORDER[min(tier_idx, len(TIER_ORDER)-1)]} {int(score % 400)} LP"
    in_tier = score % 400
    div_idx = int(in_tier // 100)
    lp = int(in_tier % 100)
    div_name = ["IV", "III", "II", "I"][div_idx]
    return f"{TIER_ORDER[tier_idx]} {div_name} {lp}LP"


def estimate_mmr(client: RiotAPIClient, puuid: str,
                  recent_count: int = 20) -> dict:
    """直近 ranked solo 試合の勝率と current rank から推定 MMR を算出。

    Returns:
        {
            "current_rank": "SILVER IV 17 LP",
            "current_score": ...,
            "ranked_record": "8W-9L",
            "overall_winrate": 0.47,
            "recent_winrate": 0.55,
            "recent_sample": 18,
            "estimated_mmr_score": ...,
            "estimated_mmr_label": "SILVER III 32 LP" 等,
            "trend": "above_rank" | "near_rank" | "below_rank",
            "note": "..."
        }
    """
    try:
        entries = client.get_league_entries_by_puuid(puuid)
    except RiotAPIError as e:
        return {"error": f"league fetch: {e}"}

    solo = next((e for e in entries if e.get("queueType") == "RANKED_SOLO_5x5"), None)
    if not solo:
        return {"error": "no_ranked_solo_data"}

    tier = solo.get("tier", "UNRANKED")
    division = solo.get("rank", "")
    lp = solo.get("leaguePoints", 0)
    wins = solo.get("wins", 0)
    losses = solo.get("losses", 0)
    overall = wins / max(1, wins + losses)
    current_score = _rank_to_score(tier, division, lp)
    current_label = _score_to_label(current_score)

    # 直近 ranked solo の試合履歴
    try:
        ids = client.get_match_ids(puuid, count=recent_count, queue=QUEUE_RANKED_SOLO)
    except RiotAPIError as e:
        return {"error": f"match_ids: {e}"}
    if not ids:
        recent_wr: Optional[float] = None
        recent_sample = 0
    else:
        matches = client.get_matches_parallel(ids)
        recent_results = []
        for m in matches:
            if not m:
                continue
            me = next((p for p in m["info"]["participants"] if p["puuid"] == puuid), None)
            if me:
                recent_results.append(bool(me.get("win")))
        recent_sample = len(recent_results)
        recent_wr = (sum(recent_results) / recent_sample) if recent_sample else None

    # 推定MMR offset
    # 勝率 > 60% → +1 division 相当
    # 55-60% → +0.5 division
    # 48-55% → ≈ rank
    # 40-48% → -0.5 division
    # < 40% → -1 division 相当
    wr_for_calc = recent_wr if recent_sample >= 10 else overall
    delta_score = 0
    trend = "near_rank"
    if wr_for_calc is None:
        trend = "no_data"
    elif wr_for_calc >= 0.60:
        delta_score = 150  # 1.5 division 相当
        trend = "above_rank"
    elif wr_for_calc >= 0.55:
        delta_score = 75
        trend = "above_rank"
    elif wr_for_calc >= 0.48:
        delta_score = 0
        trend = "near_rank"
    elif wr_for_calc >= 0.40:
        delta_score = -75
        trend = "below_rank"
    else:
        delta_score = -150
        trend = "below_rank"

    est_score = max(0, current_score + delta_score)
    est_label = _score_to_label(est_score)

    trend_text = {
        "above_rank": f"MMR は現rankより高い (勝率 {int((wr_for_calc or 0)*100)}%)。昇格傾向。",
        "near_rank":  f"MMR は現rankとほぼ一致 (勝率 {int((wr_for_calc or 0)*100)}%)。適正帯。",
        "below_rank": f"MMR は現rankより低い (勝率 {int((wr_for_calc or 0)*100)}%)。降格に注意。",
        "no_data":    "推定に必要な試合データが不足。",
    }[trend]

    return {
        "current_rank":  f"{tier} {division} {lp} LP".strip(),
        "current_score": int(current_score),
        "current_label": current_label,
        "ranked_record": f"{wins}W-{losses}L",
        "overall_winrate": round(overall, 3),
        "recent_winrate":  round(recent_wr, 3) if recent_wr is not None else None,
        "recent_sample":   recent_sample,
        "estimated_mmr_score": int(est_score),
        "estimated_mmr_label": est_label,
        "trend":         trend,
        "trend_text":    trend_text,
        "note": "Riot は本当のMMRを公開していないため、勝率と現rankからの概算値です。",
    }
