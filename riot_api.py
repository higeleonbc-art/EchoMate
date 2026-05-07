"""
riot_api.py — Riot Web APIクライアント（マッチ履歴・タイムライン・ランク取得）

試合後レビューの主データソース。レート制限とリトライを内蔵。
APIキーは環境変数 RIOT_API_KEY から取得（.env も可）。

主要メソッド:
    get_account_by_riot_id(name, tag) -> puuid
    get_match_ids(puuid, count, queue) -> list[str]
    get_match(match_id) -> dict
    get_match_timeline(match_id) -> dict
    get_summoner_by_puuid(puuid) -> dict
    get_league_entries(summoner_id) -> list[dict]
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ルーティング定数
# ---------------------------------------------------------------------------

# Regional routing（match-v5, account-v1 で使う）
REGIONAL_ROUTES = {
    "asia":     {"jp1", "kr", "ph2", "sg2", "th2", "tw2", "vn2"},
    "americas": {"na1", "br1", "la1", "la2"},
    "europe":   {"euw1", "eun1", "tr1", "ru"},
    "sea":      {"oc1"},
}

DEFAULT_PLATFORM = os.environ.get("RIOT_PLATFORM", "jp1")  # jp1/kr/na1/...
DEFAULT_REGION   = os.environ.get("RIOT_REGION", "asia")    # asia/americas/europe/sea

# マッチキュー
QUEUE_RANKED_SOLO  = 420
QUEUE_RANKED_FLEX  = 440
QUEUE_NORMAL_DRAFT = 400
QUEUE_NORMAL_BLIND = 430


def platform_to_region(platform: str) -> str:
    """jp1 → asia のような変換"""
    for region, platforms in REGIONAL_ROUTES.items():
        if platform in platforms:
            return region
    return DEFAULT_REGION


# ---------------------------------------------------------------------------
# RiotAPIClient
# ---------------------------------------------------------------------------

class RiotAPIError(Exception):
    """Riot APIからのエラー応答（4xx/5xx）"""

    def __init__(self, status_code: int, message: str = ""):
        super().__init__(f"Riot API {status_code}: {message}")
        self.status_code = status_code


class RiotAPIClient:
    """Riot Web API クライアント（同期）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        platform: str = DEFAULT_PLATFORM,
        region: Optional[str] = None,
        timeout: float = 10.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.environ.get("RIOT_API_KEY", "")
        if not self.api_key:
            raise ValueError("RIOT_API_KEY not set (env or constructor arg)")

        self.platform = platform
        self.region = region or platform_to_region(platform)
        self.timeout = timeout
        self.max_retries = max_retries

        self._client = httpx.Client(
            headers={"X-Riot-Token": self.api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RiotAPIClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 内部: HTTP呼び出し（レート制限ハンドリング）
    # ------------------------------------------------------------------

    def _get(self, base: str, path: str, params: Optional[dict] = None) -> dict | list:
        url = f"https://{base}.api.riotgames.com{path}"

        for attempt in range(self.max_retries + 1):
            resp = self._client.get(url, params=params)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                # レート制限: Retry-After秒待つ
                retry_after = int(resp.headers.get("Retry-After", "1"))
                logger.warning(f"Rate limited, waiting {retry_after}s (attempt {attempt + 1})")
                time.sleep(retry_after)
                continue

            if 500 <= resp.status_code < 600 and attempt < self.max_retries:
                # サーバーエラーは指数バックオフで再試行
                wait = 2 ** attempt
                logger.warning(f"Server error {resp.status_code}, waiting {wait}s")
                time.sleep(wait)
                continue

            raise RiotAPIError(resp.status_code, resp.text[:200])

        raise RiotAPIError(429, "Max retries exhausted")

    # ------------------------------------------------------------------
    # account-v1（regional）
    # ------------------------------------------------------------------

    def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        """Riot ID（gameName#tagLine）から puuid を取得"""
        return self._get(
            self.region,
            f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}",
        )  # type: ignore[return-value]

    def get_account_by_puuid(self, puuid: str) -> dict:
        return self._get(self.region, f"/riot/account/v1/accounts/by-puuid/{puuid}")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # match-v5（regional）
    # ------------------------------------------------------------------

    def get_match_ids(
        self,
        puuid: str,
        count: int = 20,
        start: int = 0,
        queue: Optional[int] = QUEUE_RANKED_SOLO,
    ) -> list[str]:
        """puuidに紐づくマッチID一覧（最新から）"""
        params: dict = {"count": count, "start": start}
        if queue is not None:
            params["queue"] = queue
        return self._get(  # type: ignore[return-value]
            self.region,
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
            params=params,
        )

    def get_match(self, match_id: str) -> dict:
        """マッチ詳細（参加者の最終スタッツ含む）"""
        return self._get(self.region, f"/lol/match/v5/matches/{match_id}")  # type: ignore[return-value]

    def get_match_timeline(self, match_id: str) -> dict:
        """フレーム単位のタイムライン（CS/Gold/位置/イベント）"""
        return self._get(self.region, f"/lol/match/v5/matches/{match_id}/timeline")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # summoner-v4 / league-v4（platform）
    # ------------------------------------------------------------------

    def get_summoner_by_puuid(self, puuid: str) -> dict:
        return self._get(self.platform, f"/lol/summoner/v4/summoners/by-puuid/{puuid}")  # type: ignore[return-value]

    def get_league_entries_by_puuid(self, puuid: str) -> list[dict]:
        """ランク情報（solo/flex両方が含まれうる）。

        2024-2025のRiot API変更でsummonerIdが廃止されたため、puuid直のエンドポイントを使用。
        """
        return self._get(  # type: ignore[return-value]
            self.platform,
            f"/lol/league/v4/entries/by-puuid/{puuid}",
        )

    # 後方互換用エイリアス（旧コード/将来削除予定）
    def get_league_entries(self, puuid_or_summoner_id: str) -> list[dict]:
        return self.get_league_entries_by_puuid(puuid_or_summoner_id)

    # ------------------------------------------------------------------
    # 高レベルヘルパー
    # ------------------------------------------------------------------

    def get_solo_tier(self, puuid: str) -> Optional[str]:
        """puuidから RANKED_SOLO_5x5 のtier（"GOLD"等）を取得。アンランクならNone。"""
        try:
            entries = self.get_league_entries_by_puuid(puuid)
        except RiotAPIError:
            return None
        for e in entries:
            if e.get("queueType") == "RANKED_SOLO_5x5":
                return e.get("tier")
        return None
