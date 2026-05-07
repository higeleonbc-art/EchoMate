"""
live_client.py — Live Client Data API クライアント（試合中の認証不要API）

ベース: https://127.0.0.1:2999/liveclientdata/*
試合に参加していない時は接続エラー。

主な用途:
    - 試合中のHP/MP/CS/ゴールド/レベルのリアルタイム取得
    - キル/タワー/ドラゴン等のイベント取得（軽量警告のソース）
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


LIVE_BASE = "https://127.0.0.1:2999/liveclientdata"


class LiveGameNotRunning(Exception):
    """試合中ではない（Live Client APIに接続不可）"""


class LiveClient:
    """Live Client Data API（同期、verify=False）"""

    def __init__(self, timeout: float = 2.0):
        self._client = httpx.Client(verify=False, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LiveClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _get(self, path: str) -> dict | list:
        try:
            resp = self._client.get(f"{LIVE_BASE}{path}")
        except httpx.ConnectError as e:
            raise LiveGameNotRunning(f"Live Client API unreachable: {e}") from e
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # 主要エンドポイント
    # ------------------------------------------------------------------

    def all_game_data(self) -> dict:
        """全情報まとめ（重いが1リクエストで済む）"""
        return self._get("/allgamedata")  # type: ignore[return-value]

    def active_player(self) -> dict:
        """自プレイヤー（HP/MP/レベル/ルーン/アビリティ）"""
        return self._get("/activeplayer")  # type: ignore[return-value]

    def player_list(self) -> list[dict]:
        """全プレイヤー（CS/KDA/アイテム/サモスペ）"""
        return self._get("/playerlist")  # type: ignore[return-value]

    def event_data(self) -> dict:
        """ゲーム内イベント（キル/タワー/モンスター）"""
        return self._get("/eventdata")  # type: ignore[return-value]

    def game_stats(self) -> dict:
        """ゲーム時間・モード"""
        return self._get("/gamestats")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # ヘルパー: ADCコーチ用に整形
    # ------------------------------------------------------------------

    def active_player_summary(self) -> Optional[dict]:
        """自プレイヤーのコーチング向け要約。試合中でなければ None。"""
        try:
            ap = self.active_player()
            pl = self.player_list()
        except LiveGameNotRunning:
            return None

        # active_player には summonerName しかないので、playerlistから自分を引く
        my_name = ap.get("summonerName") or ap.get("riotIdGameName")
        me = next(
            (p for p in pl if p.get("summonerName") == my_name or p.get("riotIdGameName") == my_name),
            None,
        )
        scores = (me or {}).get("scores", {})
        return {
            "champion":     (me or {}).get("championName"),
            "level":        ap.get("level"),
            "current_hp":   ap.get("championStats", {}).get("currentHealth"),
            "max_hp":       ap.get("championStats", {}).get("maxHealth"),
            "current_gold": ap.get("currentGold"),
            "cs":           scores.get("creepScore"),
            "kills":        scores.get("kills"),
            "deaths":       scores.get("deaths"),
            "assists":      scores.get("assists"),
        }
