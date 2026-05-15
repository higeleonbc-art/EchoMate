/* ADC Coach Hub - main.js
 * pywebview の Python API は window.pywebview.api 経由で呼べる。
 */

// ======== タブ切替 ========
function switchTab(name) {
  document.querySelectorAll(".tab").forEach(t => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  document.querySelectorAll(".panel").forEach(p => {
    p.classList.toggle("active", p.dataset.panel === name);
  });
}
document.querySelectorAll(".tab").forEach(t => {
  t.addEventListener("click", () => switchTab(t.dataset.tab));
});

// ======== Toast ========
function toast(message, kind = "info", durationMs = 3000) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = "toast show " + (kind === "error" ? "error" : kind === "warn" ? "warn" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.className = "toast"; }, durationMs);
}

// API_KEY_EXPIRED 検出ヘルパ
function isApiKeyExpired(errOrHtml) {
  if (!errOrHtml) return false;
  const s = String(errOrHtml);
  return s.includes("API_KEY_EXPIRED");
}
function handleApiKeyExpired() {
  toast("Riot APIキーが失効しています。Settings タブで Update してください", "error", 8000);
  switchTab("settings");
}

// ======== Status ========
function setStatus(text, kind = "grey") {
  document.getElementById("statusDot").className = "dot dot-" + kind;
  document.getElementById("statusText").textContent = text;
}

// ======== Latest Match ========
async function loadMatchList() {
  setStatus("loading match list…", "yellow");
  try {
    const res = await pywebview.api.list_recent_matches(10);
    if (isApiKeyExpired(res.error)) { handleApiKeyExpired(); setStatus("expired", "red"); return; }
    if (res.error) { toast(res.error, "error"); setStatus("error", "red"); return; }
    const sel = document.getElementById("matchPicker");
    sel.innerHTML = "";
    res.matches.forEach((m, i) => {
      const opt = document.createElement("option");
      opt.value = m.match_id;
      opt.textContent = `${i+1}. ${m.date} ${m.result} ${m.queue} ${m.champion} ${m.kda}`;
      sel.appendChild(opt);
    });
    if (res.matches.length > 0) {
      await loadLatestMatch(res.matches[0].match_id);
    } else {
      document.getElementById("latestFrame").srcdoc = "<body style='background:#141414;color:#9a9a9a;padding:40px;font-family:sans-serif'>No matches found</body>";
    }
    setStatus("ready", "green");
  } catch (e) {
    toast("Failed: " + e, "error");
    setStatus("error", "red");
  }
}
async function loadLatestMatch(matchId = null) {
  setStatus("loading review…", "yellow");
  try {
    const html = await pywebview.api.render_match_review(matchId, /*useLLM=*/true);
    if (isApiKeyExpired(html)) { handleApiKeyExpired(); setStatus("expired", "red"); return; }
    if (html.startsWith("ERROR:")) { toast(html, "error"); setStatus("error", "red"); return; }
    document.getElementById("latestFrame").srcdoc = html;
    setStatus("ready", "green");
  } catch (e) {
    toast("Failed to render: " + e, "error");
    setStatus("error", "red");
  }
}
document.getElementById("refreshLatest").addEventListener("click", loadMatchList);
document.getElementById("matchPicker").addEventListener("change", (e) => {
  loadLatestMatch(e.target.value);
});

// ======== Trend ========
async function loadTrend() {
  setStatus("loading trend…", "yellow");
  try {
    const count = parseInt(document.getElementById("trendCount").value, 10);
    const html = await pywebview.api.render_trend(count);
    if (isApiKeyExpired(html)) { handleApiKeyExpired(); setStatus("expired", "red"); return; }
    if (html.startsWith("ERROR:")) { toast(html, "error"); setStatus("error", "red"); return; }
    document.getElementById("trendFrame").srcdoc = html;
    setStatus("ready", "green");
  } catch (e) {
    toast("Failed: " + e, "error");
    setStatus("error", "red");
  }
}
document.getElementById("refreshTrend").addEventListener("click", loadTrend);
document.getElementById("trendCount").addEventListener("change", loadTrend);

// ======== Personal Benchmark ========
function renderPersonal(data, gap, rankBenchmark, targetRank) {
  const root = document.getElementById("personalContent");
  if (!data || !data.sample_count) {
    root.innerHTML = '<div class="empty">No personal data. Click Recompute.</div>';
    return;
  }
  const fields = [
    ["CS / min",      "cs_per_min",     "cs_per_min"],
    ["CS @10",        "cs_at_10",       "cs_at_10"],
    ["CS @15",        "cs_at_15",       "cs_at_15"],
    ["KDA",           "kda",            "kda"],
    ["Avg Deaths",    "deaths_avg",     "deaths_max"],     // 反対符号
    ["Vision Score",  "vision_score",   "vision_score_min"],
    ["DMG Share",     "damage_share",   "damage_share"],
    ["Gold Δ@15",     "gold_diff_at_15","gold_diff_at_15"],
    ["Obj DMG Share", "objective_damage_share", "objective_participation"],
    ["Win Rate",      "win_rate",       null],
  ];
  const cards = fields.map(([label, key, bmKey]) => {
    const value = data[key];
    const bm = bmKey ? rankBenchmark[bmKey] : null;
    let cls = "delta-zero";
    let deltaHtml = "";
    if (bm !== null && bm !== undefined && value !== null && value !== undefined) {
      let d = value - bm;
      if (key === "deaths_avg") d = -d;  // 少ない方が良い
      if (d > 0.001) { cls = "delta-pos"; deltaHtml = `<span class="delta-num pos">+${d.toFixed(2)}</span>`; }
      else if (d < -0.001) { cls = "delta-neg"; deltaHtml = `<span class="delta-num neg">${d.toFixed(2)}</span>`; }
    }
    let valStr = typeof value === "number" ? formatVal(key, value) : value;
    let targetStr = (bm !== null && bm !== undefined) ? `target: ${formatVal(bmKey, bm)}` : "";
    return `<div class="bench-card ${cls}">
      <div class="bench-label">${label}</div>
      <div class="bench-value">${valStr}${deltaHtml}</div>
      <div class="bench-target">${targetStr}</div>
    </div>`;
  }).join("");

  root.innerHTML = `
    <div class="section-title">From your last ${data.sample_count} games (target rank: ${targetRank}, skipped ${data.skipped})</div>
    <div class="bench-grid">${cards}</div>
    <div class="section-title">Last computed</div>
    <div style="color:var(--fg-muted);font-size:12px">${data.computed_at || ""}</div>
  `;
}
function formatVal(key, v) {
  if (key === "win_rate" || key === "damage_share" || key === "objective_damage_share" || key === "objective_participation") {
    return Math.round(v * 100) + "%";
  }
  if (key === "gold_diff_at_15") {
    return (v >= 0 ? "+" : "") + Math.round(v);
  }
  if (Number.isInteger(v)) return String(v);
  return Number(v).toFixed(2);
}
async function loadPersonal() {
  try {
    const res = await pywebview.api.get_personal();
    renderPersonal(res.personal, res.gap, res.rank_benchmark, res.target_rank);
  } catch (e) { toast("Personal load failed: " + e, "error"); }
}
document.getElementById("recomputePersonal").addEventListener("click", async () => {
  setStatus("computing personal benchmark…", "yellow");
  const btn = document.getElementById("recomputePersonal");
  btn.disabled = true; btn.textContent = "Computing…";
  try {
    const count = parseInt(document.getElementById("personalCount").value, 10);
    const res = await pywebview.api.recompute_personal(count);
    if (res.error) { toast(res.error, "error"); setStatus("error", "red"); }
    else {
      renderPersonal(res.personal, res.gap, res.rank_benchmark, res.target_rank);
      setStatus("ready", "green");
      toast(`Updated from ${res.personal.sample_count} games`);
    }
  } finally {
    btn.disabled = false; btn.textContent = "Recompute";
  }
});

// ======== KPI History ========
async function loadKpi() {
  try {
    const rows = await pywebview.api.get_kpi_history(50);
    const root = document.getElementById("kpiContent");
    if (!rows || rows.length === 0) {
      root.innerHTML = '<div class="empty">KPIはまだ未保存です。Latest Matchタブでコーチコメントを生成すると自動保存されます。</div>';
      return;
    }
    const tableRows = rows.map(r => {
      const status = r.achieved === 1 ? '<span class="achieved">[OK]</span>'
                    : r.achieved === 0 ? '<span class="missed">[NG]</span>'
                    : '<span class="pending">pending</span>';
      const actual = r.actual === null ? "-" : r.actual;
      return `<tr data-id="${r.id}">
        <td>${r.set_at.substring(0, 16)}</td>
        <td>${r.from_match}</td>
        <td>${r.kpi_type}</td>
        <td class="num">${r.op} ${r.target}</td>
        <td class="num">${actual}</td>
        <td>${status}</td>
        <td><button class="delete-btn" data-del-id="${r.id}">delete</button></td>
      </tr>`;
    }).join("");
    root.innerHTML = `
      <table class="kpi-table">
        <thead><tr>
          <th>Set at</th><th>Match</th><th>Type</th>
          <th>Target</th><th>Actual</th><th>Result</th><th></th>
        </tr></thead>
        <tbody>${tableRows}</tbody>
      </table>
    `;
    // 個別delete
    root.querySelectorAll(".delete-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = parseInt(btn.dataset.delId, 10);
        const res = await pywebview.api.delete_kpi_entry(id);
        if (res.deleted) {
          toast("Deleted");
          loadKpi();
        } else {
          toast("Delete failed", "error");
        }
      });
    });
  } catch (e) { toast("KPI load failed: " + e, "error"); }
}
document.getElementById("refreshKpi").addEventListener("click", loadKpi);
document.getElementById("clearKpi").addEventListener("click", async () => {
  if (!confirm("全てのKPI履歴を削除します。よろしいですか？")) return;
  try {
    const res = await pywebview.api.clear_kpi_history();
    toast(`Cleared ${res.cleared} entries`);
    loadKpi();
  } catch (e) { toast("Clear failed: " + e, "error"); }
});

// ======== Champ Select ========
async function loadChampSelect() {
  try {
    const res = await pywebview.api.get_champselect_info();
    const root = document.getElementById("champContent");
    const conn = document.getElementById("lcuConn");
    if (!res.connected) {
      conn.textContent = "LCU: disconnected";
      conn.className = "badge bad";
      root.innerHTML = '<div class="empty">League client not running.</div>';
      return;
    }
    conn.textContent = "LCU: connected";
    conn.className = "badge ok";
    if (!res.in_champselect) {
      root.innerHTML = `<div class="empty">Phase: ${res.phase || "?"}<br>Waiting for Champ Select…</div>`;
      return;
    }
    const tip = res.tip;
    const cls = tip.severity === "danger" ? "danger" : tip.severity === "warn" ? "warn" : "ok";
    root.innerHTML = `
      <div class="cs-card ${cls}">
        <div class="cs-header">${tip.header}</div>
        <div class="cs-tip">${escapeHtml(tip.body)}</div>
        <div class="cs-meta">your sup: ${tip.my_sup || "?"} · enemy sup: ${tip.enemy_sup || "?"}</div>
      </div>
    `;
  } catch (e) {
    toast("Champ select fetch failed: " + e, "error");
  }
}
function escapeHtml(s) {
  if (!s) return "";
  return String(s).replace(/[&<>"']/g, m => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[m]));
}

document.getElementById("refreshChamp").addEventListener("click", loadChampSelect);
function renderPersonalStats(stats) {
  const root = document.getElementById("champPersonalStats");
  if (!stats || stats.error) {
    root.style.display = "block";
    root.innerHTML = `<div class="empty">個人実績取得に失敗: ${escapeHtml(stats?.error || "?")}</div>`;
    return;
  }
  const wr = stats.champion_wr;
  const muwr = stats.matchup_wr;
  const bf = stats.build_freq || {};
  let html = `<div class="section-title">Your Track Record (last ${stats.sample} games)</div>`;
  if (wr) {
    html += `<div style="margin-bottom:8px;font-size:13px">
      <strong>${escapeHtml(stats.my_champ)}</strong>:
      ${wr.wins}W-${wr.losses}L (<strong>${Math.round(wr.win_rate*100)}%</strong>, n=${wr.games})
      ・avg KDA ${wr.avg_kda} ・avg CS/min ${wr.avg_cs_per_min}
    </div>`;
  } else {
    html += `<div style="color:var(--fg-muted);font-size:12px;margin-bottom:8px">この${stats.sample}試合で ${escapeHtml(stats.my_champ)} の試合なし</div>`;
  }
  if (muwr) {
    html += `<div style="margin-bottom:8px;font-size:13px">
      vs <strong>${escapeHtml(muwr.enemy_champ)}</strong>:
      ${muwr.wins}W-${muwr.games - muwr.wins}L (<strong>${Math.round(muwr.win_rate*100)}%</strong>, n=${muwr.games})
      ・avg KDA ${muwr.avg_kda} ・avg Deaths ${muwr.avg_deaths}
    </div>`;
  }
  if (bf.games && bf.games > 0 && Array.isArray(bf.positional)) {
    html += `<div class="section-title" style="margin-top:14px">Your build frequency (${bf.games} ${escapeHtml(stats.my_champ)} games)</div>`;
    html += `<div style="display:flex;gap:14px;flex-wrap:wrap">`;
    ["1st core", "2nd core", "3rd core"].forEach((label, idx) => {
      const pos = bf.positional[idx] || [];
      const rows = pos.map(it =>
        `<div style="font-size:12px">${escapeHtml(it.item_name)} <span style="color:var(--fg-muted)">×${it.count}</span></div>`
      ).join("");
      html += `<div class="bench-card" style="flex:1;min-width:160px">
        <div class="bench-label">${label}</div>
        ${rows || '<div style="color:var(--fg-muted);font-size:12px">no data</div>'}
      </div>`;
    });
    html += `</div>`;
    if (bf.first_item_winrate && bf.first_item_winrate.length > 0) {
      html += `<div class="section-title" style="margin-top:14px">1st core win rate</div>`;
      html += `<table class="kpi-table"><thead><tr><th>Item</th><th>Games</th><th>Wins</th><th>WR</th></tr></thead><tbody>`;
      bf.first_item_winrate.forEach(r => {
        const wrPct = Math.round(r.win_rate * 100);
        const cls = wrPct >= 60 ? "achieved" : wrPct < 40 ? "missed" : "";
        html += `<tr>
          <td>${escapeHtml(r.item_name)}</td>
          <td class="num">${r.games}</td>
          <td class="num">${r.wins}</td>
          <td class="num ${cls}">${wrPct}%</td>
        </tr>`;
      });
      html += `</tbody></table>`;
    }
  }
  root.style.display = "block";
  root.innerHTML = html;
}

document.getElementById("genChampCoaching").addEventListener("click", async () => {
  const btn = document.getElementById("genChampCoaching");
  const wrapper = document.getElementById("champCoaching");
  const placeholder = document.getElementById("champCoachingPlaceholder");
  const body = document.getElementById("champCoachingBody");
  const personalRoot = document.getElementById("champPersonalStats");
  wrapper.style.display = "block";
  placeholder.style.display = "block";
  placeholder.textContent = "Generating coaching (LLM: 30〜60秒)…";
  body.style.display = "none";
  personalRoot.style.display = "block";
  personalRoot.innerHTML = '<div class="empty">個人実績を取得中…</div>';
  btn.disabled = true; btn.textContent = "Generating…";

  // チャンプセレ session を取って my/enemy を判定 → personal stats を並行fetch
  let infoPromise = pywebview.api.get_champselect_info();
  let coachingPromise = pywebview.api.generate_champselect_coaching();

  try {
    const info = await infoPromise;
    if (info?.in_champselect && info?.tip) {
      // tip.header から自分のchamp と敵advを抽出するのは難しい → 別 API で picks 取り直す
      const csInfo = await pywebview.api.generate_champselect_coaching();  // cache hit想定
      // picks は coachingPromise の戻りに含まれる
    }
    const res = await coachingPromise;
    if (res.error) {
      placeholder.textContent = "ERROR: " + res.error;
      toast(res.error, "warn");
      return;
    }
    const html = escapeHtml(res.coaching || "").replace(/\n/g, "<br>");
    body.innerHTML = html;
    placeholder.style.display = "none";
    body.style.display = "block";

    // 個人実績を picks から取得
    if (res.picks && res.picks.me_champion) {
      const enemy_adc = (res.picks.their_team || []).find(p => p.position === "BOTTOM");
      const stats = await pywebview.api.get_personal_stats_for_champ(
        res.picks.me_champion, enemy_adc?.champion || null, 30,
      );
      renderPersonalStats(stats);
    } else {
      personalRoot.style.display = "none";
    }
    if (res.cached) toast("(cached)", "info", 1500);
    else toast("Coaching generated");
  } catch (e) {
    placeholder.textContent = "ERROR: " + e;
  } finally {
    btn.disabled = false; btn.textContent = "Generate Coaching";
  }
});

// ======== Settings ========
async function loadSettings() {
  try {
    const s = await pywebview.api.get_settings();
    document.getElementById("settingRiotId").value = s.riot_id || "";
    document.getElementById("settingPlatform").value = s.platform || "jp1";
    document.getElementById("settingRank").value = s.target_rank || "auto";
    const status = document.getElementById("apiKeyStatus");
    if (s.api_key_set) {
      status.textContent = `現在のキー: ${s.api_key_masked}`;
      status.style.color = "var(--fg-muted)";
    } else {
      status.textContent = "⚠ APIキー未設定。Update で設定してください";
      status.style.color = "var(--warn)";
    }
  } catch (e) { toast("Settings load failed: " + e, "error"); }
}
document.getElementById("updateApiKey").addEventListener("click", async () => {
  const key = document.getElementById("settingApiKey").value.trim();
  if (!key) { toast("APIキーを入力してください", "warn"); return; }
  const btn = document.getElementById("updateApiKey");
  btn.disabled = true; btn.textContent = "Updating…";
  try {
    const res = await pywebview.api.update_api_key(key);
    if (!res.updated) {
      toast("Update failed: " + (res.error || "unknown"), "error");
    } else if (res.valid === false) {
      toast("保存しましたが検証失敗: " + (res.warning || ""), "warn");
    } else {
      toast("APIキー更新成功・検証OK");
      document.getElementById("settingApiKey").value = "";
      await loadSettings();
      await loadMatchList();  // 401で失敗していた可能性のあるパネルを再ロード
    }
  } catch (e) { toast("Update failed: " + e, "error"); }
  finally { btn.disabled = false; btn.textContent = "Update"; }
});
document.getElementById("saveSettings").addEventListener("click", async () => {
  const data = {
    riot_id: document.getElementById("settingRiotId").value.trim(),
    platform: document.getElementById("settingPlatform").value,
    target_rank: document.getElementById("settingRank").value,
  };
  try {
    await pywebview.api.save_settings(data);
    toast("Settings saved");
  } catch (e) { toast("Save failed: " + e, "error"); }
});
document.getElementById("reloadAll").addEventListener("click", async () => {
  await loadMatchList();
  await loadTrend();
  await loadPersonal();
  await loadKpi();
  toast("Reloaded all panels");
});

// ======== Live Overlay 制御 ========
async function startOverlay(draggable = false) {
  try {
    const res = await pywebview.api.start_live_overlay(null, draggable);
    if (res.error) toast("Start failed: " + res.error, "error");
    else if (res.already_running) toast("Live overlay は既に起動中", "warn");
    else toast(`Live overlay started${draggable ? " (Draggable)" : ""}`);
  } catch (e) { toast("Start failed: " + e, "error"); }
}
document.getElementById("startLiveOverlay").addEventListener("click", () => startOverlay(false));
document.getElementById("startLiveDraggable").addEventListener("click", () => startOverlay(true));
document.getElementById("stopLiveOverlay").addEventListener("click", async () => {
  try {
    const res = await pywebview.api.stop_live_overlay();
    if (res.stopped) toast("Live overlay stopped");
    else toast("Live overlay は起動していません", "warn");
  } catch (e) { toast("Stop failed: " + e, "error"); }
});

// ======== LCU phase event hook (called from Python) ========
window.onLCUPhaseChange = function(phase) {
  const conn = document.getElementById("lcuConn");
  conn.textContent = "LCU: " + phase;
  if (phase === "ChampSelect") {
    switchTab("champ");
    loadChampSelect();
  } else if (phase === "EndOfGame") {
    // Riot API に試合データが反映されるまで2-5分かかる
    toast("試合終了検知。Riot API反映後に自動更新します", "info", 5000);
    // 即時 + 1分後 + 3分後 + 5分後 でリロード試行
    loadMatchList();
    setTimeout(() => { loadMatchList(); }, 60_000);
    setTimeout(() => { loadMatchList(); toast("再取得中…"); }, 180_000);
    setTimeout(() => { loadMatchList(); toast("再取得中…"); }, 300_000);
  }
};
window.onLiveOverlayLaunched = function() {
  toast("Live overlay started", "info", 4000);
};
window.onLiveOverlayError = function(msg) {
  toast("Live overlay: " + msg, "warn");
};

// ======== 起動時 ========
async function bootstrap() {
  setStatus("loading…", "yellow");
  await loadSettings();
  // 並列ロード
  loadMatchList();
  loadTrend();
  loadPersonal();
  loadKpi();
  loadChampSelect();
}

// pywebview の API が ready になるまで待つ
window.addEventListener("pywebviewready", bootstrap);
// fallback: pywebview が即時ready の場合
if (window.pywebview && window.pywebview.api) {
  setTimeout(bootstrap, 100);
}
