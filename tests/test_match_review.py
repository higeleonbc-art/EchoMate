"""match_review.py のロジックテスト（ダミーデータ）"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from match_review import build_review


def _dummy_match_timeline(my_puuid="me", my_champ="Caitlyn",
                          enemy_champ="Lucian", deaths_at=None):
    deaths_at = deaths_at or []
    match = {
        "metadata": {"matchId": "TEST_DUMMY"},
        "info": {
            "gameDuration": 1800,  # 30min
            "participants": [
                {
                    "puuid": my_puuid, "participantId": 1, "teamId": 100,
                    "championName": my_champ, "teamPosition": "BOTTOM",
                    "kills": 5, "deaths": len(deaths_at), "assists": 3,
                    "totalMinionsKilled": 220, "neutralMinionsKilled": 5,
                    "visionScore": 25, "win": True,
                    "totalDamageDealtToChampions": 30000,
                    "damageDealtToObjectives": 8000,
                },
                {
                    "puuid": "p2", "participantId": 2, "teamId": 100,
                    "championName": "Lulu", "teamPosition": "UTILITY",
                    "totalDamageDealtToChampions": 12000,
                    "damageDealtToObjectives": 3000,
                },
                {
                    "puuid": "p6", "participantId": 6, "teamId": 200,
                    "championName": enemy_champ, "teamPosition": "BOTTOM",
                    "totalDamageDealtToChampions": 25000,
                },
                {
                    "puuid": "p7", "participantId": 7, "teamId": 200,
                    "championName": "Thresh", "teamPosition": "UTILITY",
                },
            ],
        },
    }
    # timeline frames: 20分分
    frames = []
    for i in range(20):
        pf = {str(p): {"minionsKilled": 0, "jungleMinionsKilled": 0, "totalGold": 500*i}
              for p in range(1, 11)}
        frames.append({"participantFrames": pf, "events": []})
    # CSセット
    frames[10]["participantFrames"]["1"] = {"minionsKilled": 78, "jungleMinionsKilled": 0,
                                              "totalGold": 5000}
    frames[15]["participantFrames"]["1"] = {"minionsKilled": 118, "jungleMinionsKilled": 2,
                                              "totalGold": 8500}
    frames[15]["participantFrames"]["6"] = {"minionsKilled": 110, "jungleMinionsKilled": 0,
                                              "totalGold": 8000}
    # デス追加
    for t_min in deaths_at:
        frame_idx = int(t_min)
        if frame_idx < len(frames):
            frames[frame_idx]["events"].append({
                "type": "CHAMPION_KILL",
                "victimId": 1,
                "timestamp": int(t_min * 60000),
            })
    timeline = {"info": {"frames": frames}}
    return match, timeline


def test_build_review_basic():
    m, t = _dummy_match_timeline(deaths_at=[5.0, 12.0])
    review = build_review(m, t, "me", rank="GOLD")
    assert review is not None
    assert review.stats.champion == "Caitlyn"
    assert review.stats.cs_at_10 == 78
    assert review.stats.cs_at_15 == 120
    assert review.stats.deaths == 2
    assert review.stats.damage_share > 0


def test_build_review_micro_critical():
    """ミクロ崩壊シナリオ: CS@10 = 30 / 早期ソロデス3回"""
    m, t = _dummy_match_timeline(deaths_at=[3.0, 5.0, 7.0])
    # CS@10 を低く上書き
    t["info"]["frames"][10]["participantFrames"]["1"] = {
        "minionsKilled": 30, "jungleMinionsKilled": 0, "totalGold": 2500,
    }
    review = build_review(m, t, "me", rank="GOLD")
    assert review is not None
    # critical な改善ポイントが LS タグで含まれている
    critical_ls = [p for p in review.points if p.severity == "critical" and p.school == "LS"]
    assert len(critical_ls) >= 1


def test_build_review_returns_none_for_unknown_puuid():
    m, t = _dummy_match_timeline()
    review = build_review(m, t, "no_such_puuid", rank="GOLD")
    assert review is None


def test_time_series_extracted():
    m, t = _dummy_match_timeline()
    review = build_review(m, t, "me", rank="GOLD")
    assert review is not None
    assert len(review.stats.minute_series) == 20
    assert len(review.stats.my_cs_series) == 20
    assert len(review.stats.enemy_cs_series) == 20


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS {name}")
