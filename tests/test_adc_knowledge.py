"""adc_knowledge.py の主要関数の動作テスト"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from adc_knowledge import get_knowledge


def test_champions_loaded():
    kb = get_knowledge()
    champs = kb.list_champions()
    assert len(champs) >= 26, f"expected 26+ ADCs, got {len(champs)}"
    # Riot championName 形式
    assert "Caitlyn" in champs
    assert "Kaisa" in champs           # Kai'Sa の内部名
    assert "MissFortune" in champs
    assert "Yunara" in champs           # 新ADC


def test_get_champion_fields():
    kb = get_knowledge()
    cait = kb.get_champion("Caitlyn")
    assert cait is not None
    assert cait["range"] == 650
    assert cait["lane_phase"] == "dominant"


def test_matchup_explicit():
    kb = get_knowledge()
    m = kb.matchup("Caitlyn", "Lucian")
    assert m is not None
    assert m["score"] == -1
    assert m["source"] == "explicit"
    assert m["tip"] is not None


def test_matchup_symmetry_sample():
    """サンプリングで対称性検証"""
    kb = get_knowledge()
    pairs = [("Caitlyn", "Lucian"), ("Draven", "Jinx"), ("Vayne", "Caitlyn")]
    for a, b in pairs:
        m1 = kb.matchup(a, b)
        m2 = kb.matchup(b, a)
        assert m1 and m2
        assert m1["score"] + m2["score"] == 0, f"asymmetric {a} vs {b}: {m1['score']}/{m2['score']}"


def test_infer_matchup_score():
    kb = get_knowledge()
    # dominant (Caitlyn) vs weak (Jinx) → +2
    score = kb.infer_matchup_score("Caitlyn", "Jinx")
    assert score == 2


def test_benchmark_master():
    kb = get_knowledge()
    bm = kb.benchmark("MASTER")
    assert bm is not None
    # LS 厳しめライン: cs/min 8.5
    assert bm["cs_per_min"] == 8.5
    # Curtis派マクロ指標
    assert "gold_diff_at_15" in bm
    assert "damage_share" in bm


def test_next_rank():
    kb = get_knowledge()
    assert kb.next_rank("SILVER") == "GOLD"
    assert kb.next_rank("GOLD") == "PLATINUM"
    assert kb.next_rank("CHALLENGER") is None  # 最高帯


if __name__ == "__main__":
    # python -m tests.test_adc_knowledge で実行可
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS {name}")
