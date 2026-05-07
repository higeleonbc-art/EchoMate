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

    _PHASE_RANK = {"weak": 0, "average": 1, "strong": 2, "dominant": 3}

    def infer_matchup_score(self, my_champ: str, enemy_champ: str) -> Optional[int]:
        """champion lane_phase 差からマッチアップスコアを推定（明示エントリ無い場合のフォールバック）。

        範囲 -2(超不利) 〜 +2(超有利)。chamonp未登録なら None。
        """
        my = self.get_champion(my_champ)
        en = self.get_champion(enemy_champ)
        if not my or not en:
            return None
        my_rank = self._PHASE_RANK.get(my.get("lane_phase", "average"), 1)
        en_rank = self._PHASE_RANK.get(en.get("lane_phase", "average"), 1)
        diff = my_rank - en_rank
        return max(-2, min(2, diff))

    def matchup(self, my_champ: str, enemy_champ: str) -> Optional[dict]:
        """自分vs敵 のマッチアップ情報。

        優先順:
          1. matchups.json の明示エントリ → そのまま返す（tipあり、scoreあれば優先）
          2. matchups.json の tip だけのエントリ → score を infer で補完
          3. 明示エントリ無し → infer で score 生成（tip は None）
          4. champion未登録 → None
        """
        explicit = (
            self._matchups.get("matchups", {})
            .get(my_champ, {})
            .get(enemy_champ)
        )
        inferred = self.infer_matchup_score(my_champ, enemy_champ)
        if explicit:
            return {
                "score": explicit.get("score", inferred if inferred is not None else 0),
                "tip": explicit.get("tip"),
                "source": "explicit",
            }
        if inferred is not None:
            return {"score": inferred, "tip": None, "source": "inferred"}
        return None

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
