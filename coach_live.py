"""
coach_live.py — Live Client Data ポーリング + オーバーレイ更新ループ

LoLが試合中の間、Live Client APIを定期ポーリングしてオーバーレイに
ステータスを反映する。プレイの邪魔にならない情報量に絞る。

使い方（スタンドアロン）:
    python coach_live.py --rank GOLD

挙動:
    - 起動 → 試合開始まで「待機中」表示
    - 試合中 → 2秒ごとにHP/CS/レベル更新、警告判定
    - 試合終了 → 自動終了
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from adc_knowledge import get_knowledge
from coach_overlay import CoachOverlay
from live_client import LiveClient, LiveGameNotRunning

logger = logging.getLogger(__name__)


POLL_INTERVAL_SEC      = 2.0
WAIT_INTERVAL_SEC      = 5.0     # 試合外のときの再試行間隔
HP_DANGER_RATIO        = 0.25
HP_WARN_RATIO          = 0.45
CS_GAP_WARN            = 1.5     # CS/min が target から これ以上下がったら警告
CS_GAP_DANGER          = 2.5


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    """1ポーリングサイクル分のサマリ"""
    game_time_sec: float
    cs: int
    cs_per_min: float
    hp_ratio: float
    level: int
    kills: int
    deaths: int
    assists: int
    champion: str

    @classmethod
    def from_live(cls, summary: dict, game_time_sec: float) -> "Snapshot":
        cs = summary.get("cs") or 0
        max_hp = max(1.0, float(summary.get("max_hp") or 1))
        cur_hp = float(summary.get("current_hp") or 0)
        return cls(
            game_time_sec=game_time_sec,
            cs=cs,
            cs_per_min=round(cs / max(0.1, game_time_sec / 60.0), 2),
            hp_ratio=round(cur_hp / max_hp, 2),
            level=summary.get("level") or 1,
            kills=summary.get("kills") or 0,
            deaths=summary.get("deaths") or 0,
            assists=summary.get("assists") or 0,
            champion=summary.get("champion") or "?",
        )


# ---------------------------------------------------------------------------
# 警告ロジック
# ---------------------------------------------------------------------------

def evaluate(snap: Snapshot, cs_target_per_min: float) -> tuple[str, str, str]:
    """Snapshotから (severity, header, body) を作る"""
    minutes = snap.game_time_sec / 60.0

    # HP最優先
    if snap.hp_ratio <= HP_DANGER_RATIO:
        return (
            "danger",
            "HP CRITICAL",
            f"HP {int(snap.hp_ratio * 100)}%\nLv{snap.level}  CS{snap.cs}\n下がる判断を",
        )

    # CS/min 評価（試合開始3分以降から）
    if minutes >= 3.0:
        gap = snap.cs_per_min - cs_target_per_min
        if gap <= -CS_GAP_DANGER:
            return (
                "danger",
                "CS FALLING BEHIND",
                f"CS/min {snap.cs_per_min} (target {cs_target_per_min})\nミニオン取り戻し優先\n{int(minutes)}分 / Lv{snap.level}",
            )
        if gap <= -CS_GAP_WARN:
            return (
                "warn",
                "CS LOW",
                f"CS/min {snap.cs_per_min} (target {cs_target_per_min})\nラストヒット意識\n{int(minutes)}分 / Lv{snap.level}",
            )

    if snap.hp_ratio <= HP_WARN_RATIO:
        return (
            "warn",
            "HP LOW",
            f"HP {int(snap.hp_ratio * 100)}%\nLv{snap.level}  CS{snap.cs}\nトレード控えめに",
        )

    return (
        "ok",
        f"{snap.champion.upper()}  Lv{snap.level}",
        f"CS {snap.cs}  ({snap.cs_per_min}/min)\nKDA {snap.kills}/{snap.deaths}/{snap.assists}\n{int(minutes)}分経過",
    )


# ---------------------------------------------------------------------------
# ポーリングループ
# ---------------------------------------------------------------------------

class LiveCoachLoop:
    """別スレッドで動くポーリングループ"""

    def __init__(self, overlay: CoachOverlay, rank: str = "GOLD"):
        self.overlay = overlay
        self.rank = rank.upper()
        self._stop = threading.Event()
        self.thread: Optional[threading.Thread] = None

        kb = get_knowledge()
        bm = kb.benchmark(self.rank) or kb.benchmark("GOLD") or {}
        self.cs_target = float(bm.get("cs_per_min", 7.0))

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        client = LiveClient()
        in_game = False
        last_game_mode: Optional[str] = None
        try:
            while not self._stop.is_set():
                try:
                    summary = client.active_player_summary()
                    stats = client.game_stats()
                except LiveGameNotRunning:
                    summary = None
                    stats = None
                except Exception as e:
                    logger.warning("Live Client error: %s", e)
                    summary = None
                    stats = None

                if summary and stats:
                    in_game = True
                    last_game_mode = stats.get("gameMode")
                    snap = Snapshot.from_live(summary, stats.get("gameTime", 0))
                    sev, header, body = evaluate(snap, self.cs_target)
                    self.overlay.update_text(body, header_text=header, severity=sev)
                    time.sleep(POLL_INTERVAL_SEC)
                else:
                    if in_game:
                        # 試合終了を検知
                        is_practice = last_game_mode in ("PRACTICETOOL", "TUTORIAL")
                        if is_practice:
                            body = (
                                "プラクティスツール / チュートリアルは "
                                "Riot APIに記録されないためレビュー対象外です。\n"
                                "ESCで閉じて、ランク戦でお試しを。"
                            )
                        else:
                            body = (
                                "Coach Hub (GUI) を開いている場合は\n"
                                "Latest Match タブが自動更新されます。\n"
                                "（Riot API反映に2〜5分かかる場合あり）\n\n"
                                "ESCで閉じて結果を確認してください。"
                            )
                        self.overlay.update_text(
                            body,
                            header_text="MATCH ENDED",
                            severity="ok",
                        )
                        return
                    self.overlay.update_text(
                        f"target rank: {self.rank}\nCS/min target: {self.cs_target}\n試合開始を待機中…",
                        header_text="STANDBY",
                        severity="ok",
                    )
                    time.sleep(WAIT_INTERVAL_SEC)
        finally:
            client.close()


# ---------------------------------------------------------------------------
# スタンドアロンエントリ
# ---------------------------------------------------------------------------

def run_live(rank: str = "GOLD", click_through: bool = True) -> None:
    """
    Args:
        click_through: True (既定) でマウスイベントをLoLにスルー。
            終了は外部から (GUI の Stop Live Overlay ボタン or プロセス kill)
            False ならドラッグ移動・ESC終了が可能 (位置調整・テスト用)
    """
    overlay = CoachOverlay(click_through=click_through, draggable=not click_through)
    loop = LiveCoachLoop(overlay, rank=rank)
    loop.start()
    try:
        overlay.start()  # mainloopブロック
    finally:
        loop.stop()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LoL ADC live overlay")
    parser.add_argument("--rank", default="GOLD")
    parser.add_argument("--draggable", action="store_true",
                        help="クリックスルーを無効化し、ドラッグ移動・ESC終了を可能にする (位置調整時用)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    run_live(args.rank, click_through=not args.draggable)
