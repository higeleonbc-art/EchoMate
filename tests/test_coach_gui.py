"""coach_gui.CoachAPI のpywebview非依存メソッドテスト

pywebview のウィンドウ起動はテストしない（GUI起動が必要なため）。
Bridge クラスのうち、API呼び出しに依存しないメソッドのみ検証。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_coachapi_importable():
    """CoachAPI クラスがインポート可能"""
    from coach_gui import CoachAPI
    api = CoachAPI()
    assert api is not None


def test_get_settings_default():
    """設定未保存でも空dictが返る（エラーで落ちない）"""
    from coach_gui import CoachAPI
    import coach_profile

    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    Path(tmp.name).unlink()
    coach_profile.PROFILE_PATH = Path(tmp.name)

    api = CoachAPI()
    settings = api.get_settings()
    assert "riot_id" in settings
    assert "platform" in settings
    assert "target_rank" in settings


def test_save_settings_roundtrip():
    """save_settings → get_settings の往復テスト"""
    from coach_gui import CoachAPI
    import coach_profile

    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    Path(tmp.name).unlink()
    coach_profile.PROFILE_PATH = Path(tmp.name)

    api = CoachAPI()
    api.save_settings({
        "riot_id":     "Foo#BAR",
        "platform":    "kr",
        "target_rank": "MASTER",
    })
    s = api.get_settings()
    assert s["riot_id"] == "Foo#BAR"
    assert s["platform"] == "kr"
    assert s["target_rank"] == "MASTER"


def test_get_kpi_history_returns_list():
    """KPI履歴取得（DBが空でもlistを返す）"""
    from coach_gui import CoachAPI
    import coach_kpi

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    coach_kpi.DB_PATH = Path(tmp.name)

    api = CoachAPI()
    h = api.get_kpi_history()
    assert isinstance(h, list)


def test_target_rank_default_when_no_riot_id():
    """Riot ID未設定時の default rank ロジック"""
    from coach_gui import CoachAPI
    import coach_profile

    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    Path(tmp.name).unlink()
    coach_profile.PROFILE_PATH = Path(tmp.name)

    api = CoachAPI()
    rank = api._target_rank_or_default()
    # アンランク・Riot ID無 → GOLD フォールバック
    assert rank == "GOLD"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except Exception as e:
                print(f"  FAIL {name}: {e}")
