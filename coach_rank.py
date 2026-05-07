"""
coach_rank.py — 目標ランクの自動決定ロジック

入力されたサモナーの現ソロランク tier から 1つ上 の tier を
ターゲットとして返す。アンランクの場合は GOLD を既定とする。
"""

from __future__ import annotations

import logging
from typing import Optional

from adc_knowledge import get_knowledge
from riot_api import RiotAPIClient

logger = logging.getLogger(__name__)


# argparse から「auto / 未指定」を受けるためのセンチネル
AUTO = "auto"

UNRANKED_FALLBACK = "GOLD"


def resolve_target_rank(
    client: RiotAPIClient,
    puuid: str,
    user_specified: Optional[str],
) -> tuple[str, str]:
    """目標ランクを決定し、(target_rank, info_text) を返す。

    user_specified が "auto" / None / 空文字なら自動決定:
        現ランクが取れる:    target = next_rank(current)
        最高ランク帯:        target = current（現状維持）
        アンランク:          target = GOLD
    user_specified が明示なら強制使用。
    """
    if user_specified and user_specified.lower() != AUTO:
        return user_specified.upper(), f"指定ランク: {user_specified.upper()}"

    current = client.get_solo_tier(puuid)
    if not current:
        return (
            UNRANKED_FALLBACK,
            f"現ソロランク: アンランク → 目標: {UNRANKED_FALLBACK} (既定)",
        )

    kb = get_knowledge()
    next_rank = kb.next_rank(current)
    if not next_rank:
        # CHALLENGER等の最高帯
        return (
            current,
            f"現ソロランク: {current} (最高帯) → 目標: 現状維持",
        )

    return (
        next_rank,
        f"現ソロランク: {current} → 目標: {next_rank}",
    )
