"""
coach_profile.py — ユーザープロファイルのローカル保存

Riot ID / 目標ランク / 直近指定値などを `.coach_profile.json` に保存し、
次回起動時に再入力不要にする。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PROFILE_PATH = Path(__file__).parent / ".coach_profile.json"


def load_profile() -> dict[str, Any]:
    """プロファイル読み込み（存在しなければ空dict）"""
    if not PROFILE_PATH.exists():
        return {}
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load profile: %s", e)
        return {}


def save_profile(profile: dict[str, Any]) -> None:
    """プロファイル保存（atomic write）"""
    tmp = PROFILE_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(PROFILE_PATH)


def get_riot_id() -> Optional[str]:
    return load_profile().get("riot_id")


def set_riot_id(riot_id: str) -> None:
    p = load_profile()
    p["riot_id"] = riot_id
    save_profile(p)


def update(**kwargs: Any) -> None:
    """key=value 形式で複数項目を更新"""
    p = load_profile()
    p.update(kwargs)
    save_profile(p)


def get(key: str, default: Any = None) -> Any:
    return load_profile().get(key, default)
