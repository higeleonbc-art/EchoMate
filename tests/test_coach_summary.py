"""coach_summary.py の集計ロジックテスト"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from coach_summary import MultiMatchSummary
from match_review import MatchStats


def _make_stats(champ="Caitlyn", win=True, kills=5, deaths=2, assists=3,
                cs_at_10=80, cs_per_min=7.5, vision=25):
    return MatchStats(
        match_id="TEST", champion=champ, win=win, duration_min=30.0,
        kills=kills, deaths=deaths, assists=assists,
        cs_total=int(cs_per_min * 30), cs_at_10=cs_at_10,
        cs_at_15=int(cs_at_10 * 1.5), cs_per_min=cs_per_min,
        vision_score=vision, death_timestamps_min=[],
        damage_share=0.30,
    )


def test_empty_summary():
    s = MultiMatchSummary(matches=[], target_rank="GOLD")
    assert s.n == 0
    assert s.win_rate == 0.0


def test_aggregate_stats():
    matches = [
        _make_stats(win=True,  kills=8, deaths=2, cs_per_min=8.0),
        _make_stats(win=False, kills=3, deaths=6, cs_per_min=6.5),
        _make_stats(win=True,  kills=5, deaths=4, cs_per_min=7.0),
    ]
    s = MultiMatchSummary(matches=matches, target_rank="GOLD")
    assert s.n == 3
    assert abs(s.win_rate - 2/3) < 0.001
    assert abs(s.avg_cs_per_min - 7.166666) < 0.01
    assert abs(s.avg_deaths - 4.0) < 0.001


def test_champion_breakdown():
    matches = [
        _make_stats(champ="Caitlyn", win=True),
        _make_stats(champ="Caitlyn", win=False),
        _make_stats(champ="Jinx", win=True),
    ]
    s = MultiMatchSummary(matches=matches, target_rank="GOLD")
    bd = s.champion_breakdown
    assert bd["Caitlyn"]["games"] == 2
    assert bd["Caitlyn"]["wins"] == 1
    assert bd["Caitlyn"]["win_rate"] == 0.5
    assert bd["Jinx"]["games"] == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS {name}")
