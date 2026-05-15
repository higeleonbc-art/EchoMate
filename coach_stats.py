"""
coach_stats.py — 個人実績ベースの動的統計

自分のキャッシュ済み match + timeline から:
- チャンプ別 win rate
- マッチアップ別 win rate (自分のチャンプ vs 敵 ADC)
- ビルド頻度 (よく積むコアアイテム順序)
- 勝つ試合 vs 負ける試合のビルド差

ddragon の item.json をキャッシュロードして完成アイテム判定する。
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ItemMap (ddragon)
# ---------------------------------------------------------------------------

DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
# 完成アイテム判定の閾値 (gold total >= COMPLETED_GOLD_THRESHOLD)
COMPLETED_GOLD_THRESHOLD = 1300
# 表示・集計対象外 (薬・ワード・ブーツ系)
EXCLUDED_TAGS = {"Consumable", "Trinket", "Vision"}
EXCLUDED_ITEMS = {3340, 3363, 3364, 3330,  # 各種トリンケット
                  2003, 2031, 2033, 2055,  # ポーション・コントロールワード等
                  3340, 3363, 3364,
                  1054, 1055, 1056, 1083, 2052,  # Doran系
                  1001, 1004, 1006,  # Boots, Faerie Charm 等基礎
                  }


class ItemMap:
    """ddragon item.json をロードして itemId → 情報を引く"""

    def __init__(self):
        self._items: dict[int, dict] = {}
        self._loaded = False
        self._version: Optional[str] = None

    def load(self) -> None:
        if self._loaded:
            return
        with httpx.Client(timeout=15.0) as c:
            versions = c.get(DDRAGON_VERSIONS_URL).json()
            ver = versions[0]
            data = c.get(
                f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/item.json"
            ).json()
        for sid, info in data.get("data", {}).items():
            try:
                self._items[int(sid)] = info
            except ValueError:
                continue
        self._loaded = True
        self._version = ver
        logger.info("ItemMap loaded: %d items (patch %s)", len(self._items), ver)

    def get(self, item_id: int) -> Optional[dict]:
        if not self._loaded:
            self.load()
        return self._items.get(int(item_id))

    def name(self, item_id: int) -> Optional[str]:
        info = self.get(item_id)
        return info.get("name") if info else None

    def is_completed(self, item_id: int) -> bool:
        """完成アイテム判定: 一定 gold以上 / 除外タグなし / 除外IDなし"""
        if item_id in EXCLUDED_ITEMS:
            return False
        info = self.get(item_id)
        if not info:
            return False
        # 除外タグ含むものはfalse
        tags = set(info.get("tags", []) or [])
        if tags & EXCLUDED_TAGS:
            return False
        gold = info.get("gold", {}).get("total", 0)
        if gold < COMPLETED_GOLD_THRESHOLD:
            return False
        return True


# シングルトン
_item_map = ItemMap()


def get_item_map() -> ItemMap:
    return _item_map


# ---------------------------------------------------------------------------
# Build extraction
# ---------------------------------------------------------------------------

def extract_completed_build_order(match: dict, timeline: dict, puuid: str,
                                    item_map: Optional[ItemMap] = None,
                                    max_items: int = 5) -> list[int]:
    """自分の試合で最初に完成したN個のアイテム順序を返す"""
    item_map = item_map or _item_map
    pid = next(
        (p["participantId"] for p in match.get("info", {}).get("participants", [])
         if p.get("puuid") == puuid),
        None,
    )
    if not pid:
        return []

    out: list[int] = []
    seen: set[int] = set()
    for frame in timeline.get("info", {}).get("frames", []):
        for ev in frame.get("events", []):
            if ev.get("type") != "ITEM_PURCHASED":
                continue
            if ev.get("participantId") != pid:
                continue
            item_id = ev.get("itemId")
            if not item_id or item_id in seen:
                continue
            if not item_map.is_completed(item_id):
                continue
            out.append(int(item_id))
            seen.add(int(item_id))
            if len(out) >= max_items:
                return out
    return out


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------

def champion_winrate(stat_list: list, champ_name: str) -> Optional[dict]:
    games = [s for s in stat_list if s.champion == champ_name]
    if not games:
        return None
    wins = sum(1 for s in games if s.win)
    return {
        "champion":  champ_name,
        "games":     len(games),
        "wins":      wins,
        "losses":    len(games) - wins,
        "win_rate":  round(wins / len(games), 3),
        "avg_kda":   round(sum(s.kda for s in games) / len(games), 2),
        "avg_cs_per_min": round(sum(s.cs_per_min for s in games) / len(games), 2),
    }


def matchup_winrate(stat_list: list, my_champ: str, enemy_champ: str) -> Optional[dict]:
    games = [s for s in stat_list
             if s.champion == my_champ and s.enemy_adc == enemy_champ]
    if not games:
        return None
    wins = sum(1 for s in games if s.win)
    return {
        "my_champ":     my_champ,
        "enemy_champ":  enemy_champ,
        "games":        len(games),
        "wins":         wins,
        "win_rate":     round(wins / len(games), 3),
        "avg_kda":      round(sum(s.kda for s in games) / len(games), 2),
        "avg_deaths":   round(sum(s.deaths for s in games) / len(games), 2),
    }


def aggregate_builds(matches: list, timelines: list, puuid: str,
                      champ_name: str, top_n: int = 3,
                      item_map: Optional[ItemMap] = None) -> dict:
    """champ_name でのコアアイテム順序頻度を集計"""
    item_map = item_map or _item_map
    if not item_map._loaded:
        try:
            item_map.load()
        except Exception as e:
            logger.warning("ItemMap load failed: %s", e)
            return {"error": "ItemMap load failed", "champion": champ_name}

    # 各試合の (build order, win) を集める
    builds_all: list[tuple[list[int], bool]] = []
    for m, t in zip(matches, timelines):
        if not m or not t:
            continue
        info = m.get("info", {})
        me = next((p for p in info.get("participants", []) if p.get("puuid") == puuid), None)
        if not me or me.get("championName") != champ_name:
            continue
        order = extract_completed_build_order(m, t, puuid, item_map, max_items=top_n)
        if order:
            builds_all.append((order, me.get("win", False)))

    if not builds_all:
        return {"champion": champ_name, "games": 0}

    # 各 position (1コア / 2コア / 3コア) の頻度
    positional_freq: list[Counter] = [Counter() for _ in range(top_n)]
    for order, _ in builds_all:
        for i, item_id in enumerate(order[:top_n]):
            positional_freq[i][item_id] += 1

    # 勝ち試合のみのビルド
    win_builds = [b for b in builds_all if b[1]]
    loss_builds = [b for b in builds_all if not b[1]]

    def first_item_winrate() -> list[dict]:
        """1コア item ごとの勝率"""
        by_first: dict[int, list[bool]] = {}
        for order, win in builds_all:
            if not order:
                continue
            by_first.setdefault(order[0], []).append(win)
        rows = []
        for item_id, wins_list in by_first.items():
            if len(wins_list) < 2:  # サンプル少なすぎは除外
                continue
            rows.append({
                "item_id":   item_id,
                "item_name": item_map.name(item_id) or f"item:{item_id}",
                "games":     len(wins_list),
                "wins":      sum(wins_list),
                "win_rate":  round(sum(wins_list) / len(wins_list), 3),
            })
        return sorted(rows, key=lambda r: -r["games"])

    return {
        "champion":      champ_name,
        "games":         len(builds_all),
        "win_games":     len(win_builds),
        "loss_games":    len(loss_builds),
        "positional":    [
            [{"item_id": iid,
              "item_name": item_map.name(iid) or f"item:{iid}",
              "count": cnt}
             for iid, cnt in pos.most_common(5)]
            for pos in positional_freq
        ],
        "first_item_winrate": first_item_winrate(),
    }
