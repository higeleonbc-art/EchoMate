"""
coach_lcu_history.py — LCU 試合履歴を Riot v5 形式に変換

Riot Web API (match-v5) はカスタム試合を返さないため、LCU API の
/lol-match-history/v1/* 経由でローカルキャッシュ済みの全試合（カスタム含む）
を取得し、build_review() などが期待する riot v5 形式に変換する。

match_id 形式:
    Riot v5:  "JP1_582626034"
    LCU adapter: "LCU_582626034"  ← 接頭辞で判別

呼び出し側で `match_id.startswith("LCU_")` を見て、LCU 経由 fetch するか
RiotAPI で fetch するかを切り替える。
"""

from __future__ import annotations

import logging
from typing import Optional

from lcu_client import LCUClient, LCUNotRunning

logger = logging.getLogger(__name__)

LCU_MATCH_ID_PREFIX = "LCU_"


def is_lcu_match_id(match_id: str) -> bool:
    return isinstance(match_id, str) and match_id.startswith(LCU_MATCH_ID_PREFIX)


def lcu_game_to_riot_v5(lcu_game: dict, champ_map) -> dict:
    """LCU の game オブジェクトを Riot match-v5 形式に変換"""
    game_id = lcu_game.get("gameId")
    duration = lcu_game.get("gameDuration") or 0
    queue_id = lcu_game.get("queueId") or 0
    game_type = lcu_game.get("gameType") or ""
    is_custom = game_type == "CUSTOM_GAME"

    # custom 試合は queueId が 0 の場合があるので、QUEUE_LABELS で "Custom" 表示用に -1 にする
    effective_queue = -1 if is_custom else queue_id

    # puuid と participantId の対応取得
    pid_to_puuid: dict[int, str] = {}
    for pi in lcu_game.get("participantIdentities", []) or []:
        pid = pi.get("participantId")
        puuid = (pi.get("player") or {}).get("puuid", "")
        if pid is not None:
            pid_to_puuid[pid] = puuid

    # 各 participant を Riot v5 形式に
    participants_v5: list[dict] = []
    for p in lcu_game.get("participants", []) or []:
        pid = p.get("participantId")
        stats = p.get("stats") or {}
        timeline = p.get("timeline") or {}
        cid = p.get("championId") or 0
        champ_name = champ_map.name(cid) if cid and champ_map else None
        team_id = p.get("teamId") or 0
        # LCU の lane/role → Riot v5 teamPosition
        lane = (timeline.get("lane") or "").upper()
        role = (timeline.get("role") or "").upper()
        team_position = _lane_role_to_position(lane, role)

        participants_v5.append({
            "puuid":         pid_to_puuid.get(pid, ""),
            "participantId": pid,
            "championId":    cid,
            "championName":  champ_name or "",
            "teamId":        team_id,
            "teamPosition":  team_position,
            "kills":         stats.get("kills", 0),
            "deaths":        stats.get("deaths", 0),
            "assists":       stats.get("assists", 0),
            "totalMinionsKilled":      stats.get("totalMinionsKilled", 0),
            "neutralMinionsKilled":    stats.get("neutralMinionsKilled", 0),
            "visionScore":             stats.get("visionScore", 0),
            "totalDamageDealtToChampions": stats.get("totalDamageDealtToChampions", 0),
            "damageDealtToObjectives": stats.get("damageDealtToObjectives", 0),
            "win":                     stats.get("win", False),
        })

    return {
        "metadata": {
            "matchId":      f"{LCU_MATCH_ID_PREFIX}{game_id}",
            "participants": [p["puuid"] for p in participants_v5 if p["puuid"]],
        },
        "info": {
            "gameId":       game_id,
            "gameCreation": lcu_game.get("gameCreation", 0),
            "gameDuration": duration,
            "gameMode":     lcu_game.get("gameMode", ""),
            "gameType":     game_type,
            "queueId":      effective_queue,
            "participants": participants_v5,
            "_is_custom":   is_custom,
        },
    }


def _lane_role_to_position(lane: str, role: str) -> str:
    """LCU の lane + role → Riot v5 teamPosition の近似変換"""
    if lane == "BOTTOM":
        if role in ("DUO_CARRY", "SOLO"):
            return "BOTTOM"
        if role == "DUO_SUPPORT":
            return "UTILITY"
        return "BOTTOM"
    if lane in ("MID", "MIDDLE"):
        return "MIDDLE"
    if lane == "TOP":
        return "TOP"
    if lane == "JUNGLE":
        return "JUNGLE"
    return ""


def lcu_timeline_to_riot_v5(lcu_timeline: dict) -> dict:
    """LCU timeline → Riot match-v5 timeline 形式。

    LCU の構造は v5 と概ね同じだが、外側のラッパーが違う。
    LCU: {"frames": [...], "frameInterval": ..., "interval": ...}
    v5:  {"info": {"frames": [...], "frameInterval": ...}}
    """
    if "info" in lcu_timeline:
        return lcu_timeline  # 既にv5形式
    return {
        "info": {
            "frames":        lcu_timeline.get("frames", []) or [],
            "frameInterval": lcu_timeline.get("frameInterval") or lcu_timeline.get("interval", 60000),
        }
    }


# ---------------------------------------------------------------------------
# 取得ラッパー
# ---------------------------------------------------------------------------

def fetch_lcu_history(puuid: str, count: int = 20,
                      include_matchmaker: bool = False) -> list[dict]:
    """LCU から試合履歴を取得。custom のみフィルタ可能。

    Returns:
        list of raw LCU games dict
    """
    import httpx
    try:
        with LCUClient() as lcu:
            data = lcu.get_match_history(puuid, count=count)
    except LCUNotRunning:
        logger.info("LCU not running, skip LCU history fetch")
        return []
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = (e.response.text or "")[:300]
        except Exception:
            pass
        logger.warning(
            "LCU match history HTTP %s for %s body=%r",
            e.response.status_code if e.response is not None else "?",
            e.request.url if e.request is not None else "?",
            body,
        )
        return []
    except Exception as e:
        logger.warning("LCU match history fetch failed: %s", e)
        return []

    games = ((data.get("games") or {}).get("games") or [])
    logger.info("LCU history: %d games retrieved", len(games))

    # デバッグ: 直近5件の gameType / queueId / mapId / champion を log に
    for i, g in enumerate(games[:5]):
        me = next(
            (p for p in (g.get("participants") or [])
             if any(pi.get("participantId") == p.get("participantId")
                    and (pi.get("player") or {}).get("puuid") == puuid
                    for pi in (g.get("participantIdentities") or []))),
            None,
        )
        logger.info(
            "  game[%d] type=%s queueId=%s mapId=%s gameMode=%s puuid_match=%s",
            i,
            g.get("gameType"),
            g.get("queueId"),
            g.get("mapId"),
            g.get("gameMode"),
            bool(me),
        )

    if include_matchmaker:
        return games

    # custom判定の緩和: gameType に "CUSTOM" 含む OR queueId == 0 (matchmaker IDではない)
    def is_custom(g: dict) -> bool:
        gt = (g.get("gameType") or "").upper()
        if "CUSTOM" in gt:
            return True
        if g.get("queueId") in (0, None):
            # queueId が無い/ゼロ = matchmaker キューじゃない試合
            return True
        return False

    customs = [g for g in games if is_custom(g)]
    logger.info("LCU history: %d custom games after filter", len(customs))
    return customs


def fetch_lcu_match_full(puuid: str, lcu_match_id: str, champ_map) -> Optional[tuple[dict, dict]]:
    """単一の LCU 試合 (match + timeline) を v5 形式で返す。

    Args:
        lcu_match_id: "LCU_xxx" 形式
    Returns:
        (match_v5, timeline_v5) or None
    """
    if not is_lcu_match_id(lcu_match_id):
        return None
    try:
        game_id = int(lcu_match_id[len(LCU_MATCH_ID_PREFIX):])
    except ValueError:
        return None
    try:
        with LCUClient() as lcu:
            raw_match = lcu.get_match_detail_by_game_id(game_id)
            try:
                raw_tl = lcu.get_match_timeline_by_game_id(puuid, game_id)
            except Exception as e:
                logger.warning("LCU timeline fetch failed for %s: %s", game_id, e)
                raw_tl = {"frames": [], "frameInterval": 60000}
    except LCUNotRunning:
        return None
    except Exception as e:
        logger.warning("LCU match fetch failed: %s", e)
        return None
    return lcu_game_to_riot_v5(raw_match, champ_map), lcu_timeline_to_riot_v5(raw_tl)
