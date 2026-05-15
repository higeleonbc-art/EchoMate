"""
coach_gui.py — ADC Coach Hub GUI（pywebview ベース）

メインエントリ。タブ型UIで Latest / Trend / Personal / KPI / ChampSelect / Settings を提供。
バックグラウンドで LCU phase を監視し、ChampSelect/EndOfGame/InProgress 検知時に
JS イベントを送信して自動切替・自動更新する。

起動:
    python coach_gui.py
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import webview

from riot_api import RiotAPIClient, RiotAPIError
from match_review import build_review
from coach_review_view import render_review_html
from coach_summary import MultiMatchSummary, render_summary_html
from coach_prompts import build_review_prompt, build_full_champselect_prompt
from coach_ai import coach_chat
from coach_rank import resolve_target_rank
from coach_champselect import (
    ChampionMap, extract_lane_picks, extract_full_picks, build_champselect_tip,
)
from lcu_client import LCUClient, LCUNotRunning
from adc_knowledge import get_knowledge
import coach_profile
import coach_kpi
import coach_personal

logger = logging.getLogger("coach_gui")

JST = timezone(timedelta(hours=9))

QUEUE_LABELS: dict[int, str] = {
    420: "RankedSolo", 440: "RankedFlex", 400: "NormalDraft",
    430: "NormalBlind", 450: "ARAM", 700: "Clash",
    1700: "Arena", 490: "Quickplay", 480: "SwiftPlay",
}


# ---------------------------------------------------------------------------
# ヘルパー: Riot API クライアントをsingletonで保持
# ---------------------------------------------------------------------------

_riot_client: Optional[RiotAPIClient] = None
_riot_lock = threading.Lock()


def _get_riot_client() -> Optional[RiotAPIClient]:
    global _riot_client
    with _riot_lock:
        if _riot_client is not None:
            return _riot_client
        api_key = os.environ.get("RIOT_API_KEY", "")
        if not api_key:
            return None
        platform = coach_profile.get("platform") or os.environ.get("RIOT_PLATFORM", "jp1")
        try:
            _riot_client = RiotAPIClient(api_key=api_key, platform=platform)
        except Exception as e:
            logger.warning("Riot client init failed: %s", e)
            return None
        return _riot_client


def _reset_riot_client() -> None:
    """Settings変更時にplatformを再読込するため再生成"""
    global _riot_client
    with _riot_lock:
        if _riot_client:
            try:
                _riot_client.close()
            except Exception:
                pass
        _riot_client = None


# ---------------------------------------------------------------------------
# JSから呼ばれる API
# ---------------------------------------------------------------------------

class CoachAPI:
    """pywebview の js_api 経由でJSから呼ばれる"""

    def __init__(self):
        self._window: Optional[webview.Window] = None
        self._live_overlay_proc: Optional[subprocess.Popen] = None
        self._champ_map = ChampionMap()
        self._cmap_loaded = False

    def attach(self, window: webview.Window) -> None:
        self._window = window

    # ============================================================
    # Settings
    # ============================================================

    def get_settings(self) -> dict:
        # API key は最後の8文字だけ見せる（先頭隠す）
        key = os.environ.get("RIOT_API_KEY", "")
        masked = ""
        if key:
            masked = ("•" * max(0, len(key) - 8)) + key[-8:]
        return {
            "riot_id":     coach_profile.get_riot_id() or "",
            "platform":    coach_profile.get("platform") or os.environ.get("RIOT_PLATFORM", "jp1"),
            "target_rank": coach_profile.get("target_rank") or "auto",
            "api_key_masked": masked,
            "api_key_set":    bool(key),
        }

    def save_settings(self, data: dict) -> bool:
        coach_profile.update(**(data or {}))
        _reset_riot_client()
        return True

    def update_api_key(self, new_key: str) -> dict:
        """新しい Riot API Key を .env に書き込み、現プロセスにも即反映"""
        new_key = (new_key or "").strip()
        if not new_key:
            return {"updated": False, "error": "empty"}
        if not new_key.startswith("RGAPI-"):
            return {"updated": False, "error": "format invalid (must start with RGAPI-)"}
        try:
            from dotenv import set_key
            env_path = Path(__file__).parent / ".env"
            # .env が無ければ作る
            if not env_path.exists():
                env_path.write_text("", encoding="utf-8")
            set_key(str(env_path), "RIOT_API_KEY", new_key, quote_mode="never")
        except Exception as e:
            logger.warning("Failed to write .env: %s", e)
            return {"updated": False, "error": str(e)}
        # 現プロセスの環境変数も更新
        os.environ["RIOT_API_KEY"] = new_key
        # 既存クライアント破棄
        _reset_riot_client()
        # 軽くvalidationを試みる（最大10秒）
        client = _get_riot_client()
        if not client:
            return {"updated": True, "valid": False, "warning": "client init failed"}
        try:
            riot_id = coach_profile.get_riot_id()
            if riot_id and "#" in riot_id:
                name, tag = riot_id.split("#", 1)
                client.get_account_by_riot_id(name.strip(), tag.strip())
            return {"updated": True, "valid": True}
        except Exception as e:
            return {"updated": True, "valid": False, "warning": f"validation: {e}"}

    # ============================================================
    # Latest Match
    # ============================================================

    def list_recent_matches(self, count: int = 10) -> dict:
        """直近N試合のメタデータ一覧"""
        client = _get_riot_client()
        if not client:
            return {"error": "Riot API key not configured (.env RIOT_API_KEY)", "matches": []}
        riot_id = coach_profile.get_riot_id()
        if not riot_id or "#" not in riot_id:
            return {"error": "Riot ID not set in Settings", "matches": []}

        try:
            name, tag = riot_id.split("#", 1)
            account = client.get_account_by_riot_id(name.strip(), tag.strip())
            puuid = account["puuid"]
            ids = client.get_match_ids(puuid, count=count, queue=None)
            rows = []
            for mid in ids:
                try:
                    m = client.get_match(mid)
                except RiotAPIError as e:
                    logger.debug("Skip %s: %s", mid, e)
                    continue
                info = m["info"]
                me = next((p for p in info["participants"] if p["puuid"] == puuid), None)
                if not me:
                    continue
                ts = datetime.fromtimestamp(info["gameCreation"] / 1000, tz=JST)
                rows.append({
                    "match_id": mid,
                    "date":     ts.strftime("%m-%d %H:%M"),
                    "result":   "WIN " if me.get("win") else "LOSS",
                    "queue":    QUEUE_LABELS.get(info["queueId"], f"Q{info['queueId']}"),
                    "champion": me["championName"],
                    "kda":      f"{me['kills']}/{me['deaths']}/{me['assists']}",
                })
            return {"matches": rows}
        except RiotAPIError as e:
            if e.status_code == 401:
                return {"error": "API_KEY_EXPIRED", "matches": []}
            return {"error": f"Riot API error: {e}", "matches": []}
        except Exception as e:
            logger.exception("list_recent_matches failed")
            return {"error": str(e), "matches": []}

    def render_match_review(self, match_id: Optional[str], use_llm: bool = True) -> str:
        """指定matchまたは最新match のレビューHTMLを返す（iframe srcdoc向け）"""
        client = _get_riot_client()
        if not client:
            return "ERROR: Riot API key not configured"
        riot_id = coach_profile.get_riot_id()
        if not riot_id or "#" not in riot_id:
            return "ERROR: Riot ID not set in Settings"

        try:
            name, tag = riot_id.split("#", 1)
            account = client.get_account_by_riot_id(name.strip(), tag.strip())
            puuid = account["puuid"]

            if not match_id:
                ids = client.get_match_ids(puuid, count=1, queue=None)
                if not ids:
                    return "ERROR: No matches found"
                match_id = ids[0]

            target_rank, _ = resolve_target_rank(client, puuid,
                                                  coach_profile.get("target_rank"))

            match = client.get_match(match_id)
            timeline = client.get_match_timeline(match_id)
            review = build_review(match, timeline, puuid, rank=target_rank)
            if not review:
                return "ERROR: Player not found in match"

            # 前回KPI評価
            try:
                kpi_results = coach_kpi.evaluate_kpis(review.stats.match_id, review.stats)
            except Exception as e:
                logger.warning("KPI evaluate failed: %s", e)
                kpi_results = []

            # LLMコメント
            comment = None
            if use_llm:
                try:
                    system, user = build_review_prompt(review)
                    comment = coach_chat(system, user)
                except Exception as e:
                    logger.warning("LLM call failed: %s", e)
                    comment = f"(LLM unavailable: {e})"

            # 新KPI保存
            if comment:
                try:
                    new_kpis = coach_kpi.parse_kpis(comment)
                    coach_kpi.save_kpis(review.stats.match_id, new_kpis)
                except Exception as e:
                    logger.warning("KPI save failed: %s", e)

            return render_review_html(review, comment, prev_kpi_results=kpi_results)

        except RiotAPIError as e:
            if e.status_code == 401:
                return "ERROR: API_KEY_EXPIRED"
            return f"ERROR: Riot API: {e}"
        except Exception as e:
            logger.exception("render_match_review failed")
            return f"ERROR: {e}"

    # ============================================================
    # Trend
    # ============================================================

    def render_trend(self, count: int = 5) -> str:
        client = _get_riot_client()
        if not client:
            return "ERROR: Riot API key not configured"
        riot_id = coach_profile.get_riot_id()
        if not riot_id or "#" not in riot_id:
            return "ERROR: Riot ID not set"

        try:
            name, tag = riot_id.split("#", 1)
            account = client.get_account_by_riot_id(name.strip(), tag.strip())
            puuid = account["puuid"]
            target_rank, _ = resolve_target_rank(client, puuid,
                                                  coach_profile.get("target_rank"))
            ids = client.get_match_ids(puuid, count=count, queue=None)
            statlist = []
            for mid in ids:
                try:
                    m = client.get_match(mid)
                    t = client.get_match_timeline(mid)
                except RiotAPIError:
                    continue
                review = build_review(m, t, puuid, rank=target_rank)
                if review:
                    statlist.append(review.stats)
            if not statlist:
                return "ERROR: No reviewable matches"
            bm = get_knowledge().benchmark(target_rank) or {}
            summary = MultiMatchSummary(matches=statlist, target_rank=target_rank, benchmark=bm)
            return render_summary_html(summary)
        except RiotAPIError as e:
            if e.status_code == 401:
                return "ERROR: API_KEY_EXPIRED"
            return f"ERROR: Riot API: {e}"
        except Exception as e:
            logger.exception("render_trend failed")
            return f"ERROR: {e}"

    # ============================================================
    # Personal Benchmark
    # ============================================================

    def get_personal(self) -> dict:
        personal = coach_personal.load_personal()
        target_rank = self._target_rank_or_default()
        bm = get_knowledge().benchmark(target_rank) or {}
        gap = coach_personal.gap_to_rank(personal, bm) if personal else {}
        return {
            "personal": personal,
            "rank_benchmark": bm,
            "target_rank": target_rank,
            "gap": gap,
        }

    def recompute_personal(self, count: int = 30) -> dict:
        client = _get_riot_client()
        if not client:
            return {"error": "Riot API key not configured"}
        riot_id = coach_profile.get_riot_id()
        if not riot_id or "#" not in riot_id:
            return {"error": "Riot ID not set"}

        try:
            name, tag = riot_id.split("#", 1)
            account = client.get_account_by_riot_id(name.strip(), tag.strip())
            data = coach_personal.compute_personal_benchmark(client, account["puuid"], count=count)
            if not data or data.get("sample_count", 0) == 0:
                return {"error": "No valid ADC matches found"}
            coach_personal.save_personal(data)

            target_rank = self._target_rank_or_default()
            bm = get_knowledge().benchmark(target_rank) or {}
            gap = coach_personal.gap_to_rank(data, bm)
            return {
                "personal": data,
                "rank_benchmark": bm,
                "target_rank": target_rank,
                "gap": gap,
            }
        except RiotAPIError as e:
            if e.status_code == 401:
                return {"error": "API_KEY_EXPIRED"}
            return {"error": f"Riot API: {e}"}
        except Exception as e:
            logger.exception("recompute_personal failed")
            return {"error": str(e)}

    def _target_rank_or_default(self) -> str:
        client = _get_riot_client()
        riot_id = coach_profile.get_riot_id()
        user_specified = coach_profile.get("target_rank")
        if user_specified and user_specified.lower() != "auto":
            return user_specified.upper()
        if not (client and riot_id and "#" in riot_id):
            return "GOLD"
        try:
            name, tag = riot_id.split("#", 1)
            account = client.get_account_by_riot_id(name.strip(), tag.strip())
            target, _ = resolve_target_rank(client, account["puuid"], user_specified)
            return target
        except Exception:
            return "GOLD"

    # ============================================================
    # KPI
    # ============================================================

    def get_kpi_history(self, limit: int = 50) -> list[dict]:
        try:
            return coach_kpi.history(limit)
        except Exception as e:
            logger.warning("KPI history failed: %s", e)
            return []

    def clear_kpi_history(self) -> dict:
        try:
            n = coach_kpi.clear_all()
            return {"cleared": n}
        except Exception as e:
            logger.warning("clear_kpi_history failed: %s", e)
            return {"cleared": 0, "error": str(e)}

    def delete_kpi_entry(self, entry_id: int) -> dict:
        try:
            ok = coach_kpi.delete_by_id(int(entry_id))
            return {"deleted": ok}
        except Exception as e:
            return {"deleted": False, "error": str(e)}

    # ============================================================
    # Champ Select
    # ============================================================

    def get_champselect_info(self) -> dict:
        try:
            client = LCUClient()
        except LCUNotRunning:
            return {"connected": False}
        try:
            phase = client.gameflow_phase()
        except Exception as e:
            client.close()
            return {"connected": False, "error": str(e)}

        result: dict[str, Any] = {"connected": True, "phase": phase, "in_champselect": False}

        if phase != "ChampSelect":
            client.close()
            return result

        try:
            session = client.champ_select_session()
        except Exception as e:
            client.close()
            return {**result, "error": f"session fetch failed: {e}"}
        client.close()

        if not session:
            return result

        # Champion map ロード（lazy）
        if not self._cmap_loaded:
            try:
                self._champ_map.load()
                self._cmap_loaded = True
            except Exception as e:
                return {**result, "error": f"ddragon load failed: {e}"}

        picks = extract_lane_picks(session, self._champ_map)
        if not picks:
            return result

        sev, header, body = build_champselect_tip(picks)
        return {
            **result,
            "in_champselect": True,
            "tip": {
                "severity": sev,
                "header":   header,
                "body":     body,
                "my_sup":   picks.get("my_sup"),
                "enemy_sup": picks.get("enemy_sup"),
            },
        }

    def generate_champselect_coaching(self) -> dict:
        """現セッションの全構成情報をLLMに投げて総合コーチングテキストを生成。

        マッチアップ・サポ連携・コアアイテム・集団戦のpositioning を含む。
        前回と同じ構成なら cache を返す。
        """
        # 1. LCU からfull picks
        try:
            client = LCUClient()
            phase = client.gameflow_phase()
            session = client.champ_select_session() if phase == "ChampSelect" else None
            client.close()
        except LCUNotRunning:
            return {"error": "LCU not running"}
        except Exception as e:
            return {"error": f"LCU error: {e}"}

        if not session:
            return {"error": "Not in champ select"}

        if not self._cmap_loaded:
            try:
                self._champ_map.load()
                self._cmap_loaded = True
            except Exception as e:
                return {"error": f"ddragon load failed: {e}"}

        full = extract_full_picks(session, self._champ_map)
        if not full.get("me_champion"):
            return {"error": "Your champion not yet picked"}

        # 2. キャッシュキー（同じ構成なら再生成しない）
        signature = self._champselect_signature(full)
        if getattr(self, "_champselect_cache", None) and self._champselect_cache[0] == signature:
            return {"cached": True, "coaching": self._champselect_cache[1], "picks": full}

        # 3. マッチアップ + チャンプデータ取得
        kb = get_knowledge()
        enemy_adc_entry = next(
            (p for p in full["their_team"] if p["position"] == "BOTTOM"),
            None,
        )
        matchup_data = None
        if enemy_adc_entry and enemy_adc_entry.get("champion"):
            matchup_data = kb.matchup(full["me_champion"], enemy_adc_entry["champion"])
        champion_data = kb.get_champion(full["me_champion"])

        # 4. LLM 呼び出し
        try:
            system, user = build_full_champselect_prompt(
                full["me_champion"], full["me_position"] or "BOTTOM",
                full["my_team"], full["their_team"],
                matchup_data=matchup_data, champion_data=champion_data,
            )
            coaching = coach_chat(system, user)
        except Exception as e:
            logger.exception("LLM champselect coaching failed")
            return {"error": f"LLM call failed: {e}"}

        # cache保存
        self._champselect_cache = (signature, coaching)
        return {"cached": False, "coaching": coaching, "picks": full}

    @staticmethod
    def _champselect_signature(full: dict) -> str:
        my = "|".join(f"{p['position']}:{p['champion']}" for p in full["my_team"])
        en = "|".join(f"{p['position']}:{p['champion']}" for p in full["their_team"])
        return f"{my}__VS__{en}"

    # ============================================================
    # Live Overlay (別プロセス)
    # ============================================================

    def start_live_overlay(self, rank: Optional[str] = None,
                            draggable: bool = False) -> dict:
        """Live Overlay を別プロセスで起動。

        Args:
            rank: target rank。None なら設定から自動
            draggable: True ならクリックスルー無効・ドラッグ移動/ESC可能
                通常は False（クリックスルーON）でゲーム操作を妨害しない
        """
        # 既に動いていれば何もしない
        if self._live_overlay_proc and self._live_overlay_proc.poll() is None:
            return {"started": False, "already_running": True}

        rank = rank or self._target_rank_or_default()
        py = sys.executable
        script = str(Path(__file__).parent / "coach_live.py")
        cmd = [py, script, "--rank", rank]
        if draggable:
            cmd.append("--draggable")
        try:
            self._live_overlay_proc = subprocess.Popen(
                cmd,
                cwd=str(Path(__file__).parent),
                # tkinter ウィンドウは独立プロセス
                creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
            )
            return {"started": True, "rank": rank, "draggable": draggable,
                    "pid": self._live_overlay_proc.pid}
        except Exception as e:
            logger.exception("Failed to launch live overlay")
            return {"started": False, "error": str(e)}

    def stop_live_overlay(self) -> dict:
        if self._live_overlay_proc and self._live_overlay_proc.poll() is None:
            self._live_overlay_proc.terminate()
            return {"stopped": True}
        return {"stopped": False}


# ---------------------------------------------------------------------------
# Background LCU phase monitor
# ---------------------------------------------------------------------------

def lcu_monitor(api: CoachAPI, window: webview.Window, stop_event: threading.Event) -> None:
    """LCU の gameflow phase を3秒ごとにポーリングし、変化をJSへ通知。

    InProgress 検知時に Live Overlay を自動起動。
    """
    last_phase: Optional[str] = None
    while not stop_event.is_set():
        try:
            client = LCUClient()
            phase = client.gameflow_phase()
            client.close()
        except LCUNotRunning:
            phase = None
        except Exception as e:
            logger.debug("LCU monitor error: %s", e)
            phase = None

        phase_str = phase or "None"
        if phase_str != last_phase:
            try:
                window.evaluate_js(
                    f'window.onLCUPhaseChange && window.onLCUPhaseChange({_js_str(phase_str)})'
                )
            except Exception as e:
                logger.debug("evaluate_js failed: %s", e)

            # InProgress 検知 → Live Overlay 自動起動
            if phase == "InProgress" and last_phase != "InProgress":
                res = api.start_live_overlay()
                if res.get("started"):
                    try:
                        window.evaluate_js("window.onLiveOverlayLaunched && window.onLiveOverlayLaunched()")
                    except Exception:
                        pass
                else:
                    msg = res.get("reason", "?")
                    try:
                        window.evaluate_js(
                            f"window.onLiveOverlayError && window.onLiveOverlayError({_js_str(msg)})"
                        )
                    except Exception:
                        pass
            last_phase = phase_str

        time.sleep(3.0)


def _js_str(s: str) -> str:
    """JS呼び出し用にエスケープした '"..."' を返す"""
    safe = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{safe}"'


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    api = CoachAPI()
    gui_dir = Path(__file__).parent / "gui"
    html_path = gui_dir / "main.html"

    window = webview.create_window(
        title="ADC Coach Hub",
        url=str(html_path),
        js_api=api,
        width=1280,
        height=820,
        min_size=(960, 640),
        background_color="#141414",
    )
    api.attach(window)

    stop_event = threading.Event()

    def on_loaded():
        # JS側の bootstrap が動くように pywebview ready を待ってからスレッド起動
        threading.Thread(
            target=lcu_monitor,
            args=(api, window, stop_event),
            daemon=True,
        ).start()

    window.events.loaded += on_loaded

    try:
        webview.start(debug=False)
    finally:
        stop_event.set()
        api.stop_live_overlay()
    return 0


if __name__ == "__main__":
    sys.exit(main())
