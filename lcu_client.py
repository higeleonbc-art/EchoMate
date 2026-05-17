"""
lcu_client.py — League Client (LCU) APIクライアント

クライアント常駐情報（チャンプセレクト・ロビー・現サモナー）にアクセスする。
lockfileから認証情報を取得する。試合中の情報は live_client.py を使うこと。

lockfile location候補（順に試行）:
    - %LOCALAPPDATA%\\Riot Games\\League of Legends\\lockfile
    - C:\\Riot Games\\League of Legends\\lockfile
    - $RIOT_LCU_LOCKFILE（環境変数で明示指定）
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


LOCKFILE_CANDIDATES: list[Path] = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Riot Games" / "League of Legends" / "lockfile",
    Path("C:/Riot Games/League of Legends/lockfile"),
    Path("D:/Riot Games/League of Legends/lockfile"),
]


class LCUNotRunning(Exception):
    """LoLクライアントが起動していない（lockfileが見つからない）"""


def find_lockfile() -> Path:
    """環境変数 → 既知の場所の順で lockfile を探す"""
    explicit = os.environ.get("RIOT_LCU_LOCKFILE")
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p

    for candidate in LOCKFILE_CANDIDATES:
        if candidate.exists():
            return candidate

    raise LCUNotRunning("LCU lockfile not found in any known location")


def parse_lockfile(path: Path) -> tuple[int, str, str]:
    """lockfile を読んで (port, password, protocol) を返す。

    形式: LeagueClient:<pid>:<port>:<password>:<protocol>
    """
    raw = path.read_text(encoding="utf-8").strip()
    parts = raw.split(":")
    if len(parts) < 5:
        raise ValueError(f"Malformed lockfile: {raw!r}")
    _name, _pid, port, password, protocol = parts[:5]
    return int(port), password, protocol


class LCUClient:
    """LCU APIへの同期クライアント。verify=False（自己署名）。"""

    def __init__(self, lockfile: Optional[Path] = None, timeout: float = 5.0):
        self.lockfile_path = lockfile or find_lockfile()
        port, password, protocol = parse_lockfile(self.lockfile_path)

        self.base_url = f"{protocol}://127.0.0.1:{port}"
        token = base64.b64encode(f"riot:{password}".encode()).decode()

        self._client = httpx.Client(
            headers={"Authorization": f"Basic {token}"},
            verify=False,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LCUClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def get(self, path: str) -> dict | list:
        resp = self._client.get(f"{self.base_url}{path}")
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # 高頻度に使うエンドポイント
    # ------------------------------------------------------------------

    def current_summoner(self) -> dict:
        return self.get("/lol-summoner/v1/current-summoner")  # type: ignore[return-value]

    def champ_select_session(self) -> Optional[dict]:
        """チャンプセレクト中ならセッション情報、それ以外は None"""
        try:
            return self.get("/lol-champ-select/v1/session")  # type: ignore[return-value]
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def gameflow_phase(self) -> str:
        """現在のゲームフェーズ: None / Lobby / ChampSelect / InProgress / EndOfGame など"""
        return self.get("/lol-gameflow/v1/gameflow-phase")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Match History (カスタム試合を含むローカル履歴)
    # ------------------------------------------------------------------

    def get_match_history(self, puuid: str, count: int = 20) -> dict:
        """ローカルクライアントの試合履歴（カスタム含む）。

        返り値は LCU 形式 (games.games[]). custom 試合の gameType は "CUSTOM_GAME"。
        通常 matchmaker 試合は "MATCHED_GAME"。
        """
        end = max(0, count - 1)
        return self.get(  # type: ignore[return-value]
            f"/lol-match-history/v1/products/lol/{puuid}/matches"
            f"?begIndex=0&endIndex={end}"
        )

    def get_match_detail_by_game_id(self, game_id: int) -> dict:
        """LCU から単一試合詳細を取得"""
        return self.get(f"/lol-match-history/v1/games/{game_id}")  # type: ignore[return-value]

    def get_match_timeline_by_game_id(self, puuid: str, game_id: int) -> dict:
        """LCU から timeline を取得 (custom含む)"""
        return self.get(  # type: ignore[return-value]
            f"/lol-match-history/v1/products/lol/{puuid}/timelines/{game_id}"
        )
