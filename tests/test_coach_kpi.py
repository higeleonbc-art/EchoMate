"""coach_kpi.py のテスト（KPI抽出・保存・評価）"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import coach_kpi


def _temp_db():
    """テスト用に DB_PATH を一時ファイルに差し替え"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    coach_kpi.DB_PATH = Path(tmp.name)
    return Path(tmp.name)


def test_parse_kpis_vision():
    text = "次試合の最優先KPI\n視界スコア: 22"
    kpis = coach_kpi.parse_kpis(text)
    assert kpis == [("vision_score", 22.0, ">=")]


def test_parse_kpis_cs10():
    text = "次試合の最優先KPI\nCS@10: 75"
    kpis = coach_kpi.parse_kpis(text)
    assert kpis == [("cs_at_10", 75.0, ">=")]


def test_parse_kpis_deaths():
    text = "次試合の最優先KPI: 死亡数 4以下"
    kpis = coach_kpi.parse_kpis(text)
    assert ("deaths", 4.0, "<=") in kpis


def test_parse_kpis_empty():
    assert coach_kpi.parse_kpis("") == []
    assert coach_kpi.parse_kpis("コーチコメントだけ") == []


def test_save_and_evaluate_kpi():
    _temp_db()
    n = coach_kpi.save_kpis("MATCH_A", [("vision_score", 22.0, ">="),
                                          ("cs_at_10", 75.0, ">=")])
    assert n == 2

    pending = coach_kpi.get_pending_kpis()
    assert len(pending) == 2

    # 達成評価
    class S:
        vision_score = 25
        cs_at_10 = 70
        cs_at_15 = 0
        cs_per_min = 0
        deaths = 0
        kda = 0
    results = coach_kpi.evaluate_kpis("MATCH_B", S())
    assert len(results) == 2
    by_type = {r["kpi_type"]: r for r in results}
    assert by_type["vision_score"]["achieved"] is True   # 25 >= 22
    assert by_type["cs_at_10"]["achieved"] is False      # 70 < 75


def test_no_double_save():
    _temp_db()
    n1 = coach_kpi.save_kpis("MATCH_X", [("kda", 3.0, ">=")])
    n2 = coach_kpi.save_kpis("MATCH_X", [("kda", 3.0, ">=")])
    assert n1 == 1
    assert n2 == 0  # 同match_idの再保存はskip


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS {name}")
