"""coach_profile.py のテスト（プロファイル保存・読込）"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import coach_profile


def _temp_profile():
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    coach_profile.PROFILE_PATH = Path(tmp.name)
    Path(tmp.name).unlink()  # まっさら
    return Path(tmp.name)


def test_load_empty():
    _temp_profile()
    assert coach_profile.load_profile() == {}


def test_set_and_get_riot_id():
    _temp_profile()
    coach_profile.set_riot_id("Foo#BAR")
    assert coach_profile.get_riot_id() == "Foo#BAR"


def test_update_multiple():
    _temp_profile()
    coach_profile.update(riot_id="A#B", target_rank="MASTER")
    p = coach_profile.load_profile()
    assert p == {"riot_id": "A#B", "target_rank": "MASTER"}


def test_get_with_default():
    _temp_profile()
    assert coach_profile.get("nonexistent", "fallback") == "fallback"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS {name}")
