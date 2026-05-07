"""
adc_knowledge.py — ADC知識ベースのアクセサ

data/adc/ 以下のJSONを読み込み、コーチング/マッチアップ判断のヘルパーを提供する。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data" / "adc"


class ADCKnowledge:
    """ADCチャンプ・マッチアップ・ベンチマークの統合アクセサ"""

    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self._champions = self._load("champions.json")
        self._matchups = self._load("matchups.json")
        self._benchmarks = self._load("benchmarks.json")

    def _load(self, filename: str) -> dict:
        path = self.data_dir / filename
        with path.open(encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------------
    # チャンピオン
    # ------------------------------------------------------------------

    def get_champion(self, name: str) -> Optional[dict]:
        return self._champions.get("champions", {}).get(name)

    def list_champions(self) -> list[str]:
        return list(self._champions.get("champions", {}).keys())

    # ------------------------------------------------------------------
    # マッチアップ
    # ------------------------------------------------------------------

    def matchup(self, my_champ: str, enemy_champ: str) -> Optional[dict]:
        """自分vs敵 のマッチアップ情報。未収録なら None"""
        return (
            self._matchups.get("matchups", {})
            .get(my_champ, {})
            .get(enemy_champ)
        )

    def matchup_score(self, my_champ: str, enemy_champ: str) -> Optional[int]:
        m = self.matchup(my_champ, enemy_champ)
        return m["score"] if m else None

    # ------------------------------------------------------------------
    # ベンチマーク
    # ------------------------------------------------------------------

    def benchmark(self, rank: str) -> Optional[dict]:
        """rank: 'GOLD' / 'PLATINUM' など。階級記号(I/II/III/IV)は無視。"""
        rank_upper = rank.upper().split()[0]
        return self._benchmarks.get("benchmarks", {}).get(rank_upper)

    def next_rank(self, current_rank: str) -> Optional[str]:
        order: list[str] = self._benchmarks.get("ranks_order", [])
        cur = current_rank.upper().split()[0]
        if cur not in order:
            return None
        idx = order.index(cur)
        if idx + 1 >= len(order):
            return None
        return order[idx + 1]

    def gap_to_master(self, stats: dict) -> dict:
        """現状値とマスター基準の差分を返す。

        stats: {"cs_per_min": 5.2, "kda": 1.8, "cs_at_10": 60, ...}
        return: {"cs_per_min": -2.8, "kda": -1.2, ...}（負=不足、正=超過）
        """
        master = self.benchmark("MASTER") or {}
        gaps: dict[str, float] = {}
        for key, target in master.items():
            if not isinstance(target, (int, float)):
                continue
            current = stats.get(key)
            if current is None:
                continue
            gaps[key] = round(current - target, 2)
        return gaps

    def death_pattern_label(self, pattern_key: str) -> Optional[str]:
        return self._benchmarks.get("death_patterns", {}).get(pattern_key)


_singleton: Optional[ADCKnowledge] = None


def get_knowledge() -> ADCKnowledge:
    """シングルトンアクセサ"""
    global _singleton
    if _singleton is None:
        _singleton = ADCKnowledge()
    return _singleton
