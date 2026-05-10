"""
coach_champselect.py — チャンプセレクトアシスタント

LCU API の /lol-champ-select/v1/session を 1秒ごとにポーリングし、
チャンプ確定時に matchups.json と adc_knowledge.py から tip を生成して
coach_overlay にプッシュする。

champion ID は Data Dragon から取得した最新マップを使用。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import httpx

from adc_knowledge import get_knowledge
from coach_overlay import CoachOverlay
from lcu_client import LCUClient, LCUNotRunning

logger = logging.getLogger(__name__)


POLL_INTERVAL_SEC = 1.5
DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"


# ---------------------------------------------------------------------------
# Champion ID マップ (Data Dragon)
# ---------------------------------------------------------------------------

class ChampionMap:
    """Data Dragon から championId → 内部名 をロード"""

    def __init__(self):
        self._by_id: dict[int, str] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        with httpx.Client(timeout=10.0) as c:
            versions = c.get(DDRAGON_VERSIONS_URL).json()
            ver = versions[0]
            data = c.get(
                f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/champion.json"
            ).json()["data"]
        self._by_id = {int(v["key"]): k for k, v in data.items()}
        self._loaded = True
        logger.info("ChampionMap loaded: %d champions (patch %s)", len(self._by_id), ver)

    def name(self, champion_id: int) -> Optional[str]:
        if champion_id == 0:
            return None
        if not self._loaded:
            self.load()
        return self._by_id.get(champion_id)


# ---------------------------------------------------------------------------
# Champ select 状態抽出
# ---------------------------------------------------------------------------

def extract_lane_picks(
    session: dict,
    cmap: ChampionMap,
) -> Optional[dict]:
    """セッションから自分・敵ADC・自sup・敵sup の情報を抽出。

    フォールバック経路（複数モード対応）:
    1. championId / championPickIntent (resolved teamから直)
    2. session.actions の picks (cellId → championId マップで補完)
    3. position が無いモード(blind等) は cellId 並び順で推定（bot=3番目, sup=4番目）
    """
    my_cell_id = session.get("localPlayerCellId")
    my_team = session.get("myTeam", []) or []
    their_team = session.get("theirTeam", []) or []

    # actions[][]からcompleted pickをcellId-indexedマップに集約
    pick_by_cell: dict[int, int] = {}
    for action_group in session.get("actions", []) or []:
        for action in action_group or []:
            if action.get("type") != "pick":
                continue
            cid = action.get("championId") or 0
            cell = action.get("actorCellId")
            if cell is None or not cid:
                continue
            # completed=Trueを優先するが、未完でも値があれば暫定として保持
            if action.get("completed") or cell not in pick_by_cell:
                pick_by_cell[cell] = int(cid)

    def champion_id(p: Optional[dict]) -> int:
        if not p:
            return 0
        cid = p.get("championId") or 0
        if not cid:
            cid = pick_by_cell.get(p.get("cellId"), 0)
        if not cid:
            cid = p.get("championPickIntent") or 0
        try:
            return int(cid)
        except (TypeError, ValueError):
            return 0

    me = next((p for p in my_team if p.get("cellId") == my_cell_id), None)
    if not me:
        return None

    def by_position(team: list, position: str) -> Optional[dict]:
        return next(
            (p for p in team
             if (p.get("assignedPosition") or "").lower() == position),
            None,
        )

    def by_cell_order(team: list, offset: int) -> Optional[dict]:
        """assignedPosition が無いblindモード等で cellId 並び順から推定。
        通常 bot=offset 3, support=offset 4 (TOP/JG/MID/BOT/SUP)"""
        if len(team) <= offset:
            return None
        return sorted(team, key=lambda p: p.get("cellId", 0))[offset]

    my_adc = by_position(my_team, "bottom") or by_cell_order(my_team, 3) or me
    my_sup = by_position(my_team, "utility") or by_cell_order(my_team, 4)
    en_adc = by_position(their_team, "bottom") or by_cell_order(their_team, 3)
    en_sup = by_position(their_team, "utility") or by_cell_order(their_team, 4)

    def cname(p: Optional[dict]) -> Optional[str]:
        cid = champion_id(p)
        return cmap.name(cid) if cid else None

    me_cell = me.get("cellId")
    return {
        "i_am_adc":  (me_cell == (my_adc or {}).get("cellId")),
        "my_champ":  cname(me),
        "my_adc":    cname(my_adc),
        "my_sup":    cname(my_sup),
        "enemy_adc": cname(en_adc),
        "enemy_sup": cname(en_sup),
    }


# ---------------------------------------------------------------------------
# Tip生成
# ---------------------------------------------------------------------------

def build_champselect_tip(picks: dict) -> tuple[str, str, str]:
    """(severity, header, body) のタプルを返す"""
    kb = get_knowledge()
    my_champ = picks.get("my_champ")
    enemy_adc = picks.get("enemy_adc")
    my_sup = picks.get("my_sup") or "?"
    enemy_sup = picks.get("enemy_sup") or "?"

    if not picks.get("i_am_adc"):
        return ("ok", "CHAMP SELECT",
                f"あなたのロールはADCではない可能性。観察モード。\n"
                f"BOT: {picks.get('my_adc') or '?'} vs {enemy_adc or '?'}")

    if not (my_champ and enemy_adc):
        return ("ok", "CHAMP SELECT",
                f"待機中… {my_champ or '?'} vs {enemy_adc or '?'}\n"
                f"sup: {my_sup} / {enemy_sup}")

    m = kb.matchup(my_champ, enemy_adc)
    if not m:
        return ("ok", f"{my_champ} vs {enemy_adc}",
                f"未収録マッチアップ。\nsup: {my_sup} / {enemy_sup}")

    score = m.get("score", 0)
    tip = m.get("tip") or ""
    sev = "danger" if score <= -1 else "warn" if score == 0 else "ok"
    arrow = {-2: "⇊", -1: "↓", 0: "→", 1: "↑", 2: "⇈"}.get(score, "→")
    header = f"{my_champ} {arrow} {enemy_adc} ({score:+d})"
    body_lines = [tip, f"sup: {my_sup} / {enemy_sup}"]
    return (sev, header, "\n".join(body_lines))


# ---------------------------------------------------------------------------
# ポーリングループ
# ---------------------------------------------------------------------------

class ChampSelectLoop:
    """別スレッドで LCU をポーリングし、チャンプセレ状態をオーバーレイに反映"""

    def __init__(self, overlay: CoachOverlay):
        self.overlay = overlay
        self._stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._cmap = ChampionMap()
        self._last_signature: Optional[str] = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # まず Champion map をロード（ddragon遅いので別スレッドで）
        try:
            self._cmap.load()
        except Exception as e:
            logger.warning("Failed to load ChampionMap: %s", e)
            self.overlay.update_text(
                "Data Dragon取得失敗。チャンプ名解決不可。",
                header_text="CHAMP SELECT", severity="danger",
            )
            return

        client: Optional[LCUClient] = None
        while not self._stop.is_set():
            if client is None:
                try:
                    client = LCUClient()
                except LCUNotRunning:
                    self.overlay.update_text(
                        "LoLクライアントを起動してください",
                        header_text="WAITING", severity="ok",
                    )
                    time.sleep(3.0)
                    continue

            try:
                session = client.champ_select_session()
            except Exception as e:
                logger.debug("LCU session fetch failed: %s", e)
                session = None
                # クライアント再接続
                try:
                    client.close()
                except Exception:
                    pass
                client = None
                time.sleep(2.0)
                continue

            if session is None:
                self.overlay.update_text(
                    "チャンプセレクト待機中…",
                    header_text="STANDBY", severity="ok",
                )
                time.sleep(POLL_INTERVAL_SEC * 2)
                continue

            picks = extract_lane_picks(session, self._cmap)
            if not picks:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            # 同じピック構成なら更新スキップ
            sig = (
                f"{picks.get('my_champ')}|{picks.get('enemy_adc')}|"
                f"{picks.get('my_sup')}|{picks.get('enemy_sup')}"
            )
            if sig != self._last_signature:
                sev, header, body = build_champselect_tip(picks)
                self.overlay.update_text(body, header_text=header, severity=sev)
                self._last_signature = sig

            time.sleep(POLL_INTERVAL_SEC)

        if client:
            try:
                client.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# スタンドアロン起動
# ---------------------------------------------------------------------------

def run_champselect() -> None:
    overlay = CoachOverlay()
    loop = ChampSelectLoop(overlay)
    loop.start()
    try:
        overlay.start()
    finally:
        loop.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_champselect()
