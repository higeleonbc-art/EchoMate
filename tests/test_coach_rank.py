"""coach_rank.py のターゲットランク自動決定ロジックテスト（モッククライアント）"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from coach_rank import resolve_target_rank, AUTO


def _mock_client(tier: str | None):
    c = MagicMock()
    c.get_solo_tier.return_value = tier
    return c


def test_explicit_rank_wins():
    c = _mock_client("BRONZE")
    target, info = resolve_target_rank(c, "puuid", "MASTER")
    assert target == "MASTER"
    assert "MASTER" in info


def test_auto_with_silver():
    c = _mock_client("SILVER")
    target, _ = resolve_target_rank(c, "puuid", AUTO)
    assert target == "GOLD"


def test_auto_with_unranked():
    c = _mock_client(None)
    target, info = resolve_target_rank(c, "puuid", None)
    assert target == "GOLD"  # フォールバック
    assert "アンランク" in info


def test_auto_with_top_tier():
    c = _mock_client("CHALLENGER")
    target, info = resolve_target_rank(c, "puuid", AUTO)
    assert target == "CHALLENGER"  # 最高帯は現状維持
    assert "現状維持" in info


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS {name}")
