/* 合同义务状态推演台 —— 前端逻辑（原生 JavaScript） */

const S = {
  view: "main",        // 当前视角：main 或分叉 id
  stateVersion: 0,     // 主线 CAS 版本
  scenarioRev: 0,      // 当前分叉 CAS 版本
  scenarios: [],
  state: null,
};

const $ = (sel) => document.querySelector(sel);

async function api(method, url, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const resp = await fetch(url, opts);
  if (resp.status === 409) {
    const err = await resp.json().catch(() => ({}));
    alert("版本冲突：" + (err.detail || "数据已被他人修改") + "\n页面将自动刷新。");
    await loadAll();
    throw new Error("conflict");
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail));
  }
  return resp.json();
}

function fmt(t) {
  if (!t) return "—";
  return t.replace("T", " ").replace("+00:00", "Z").slice(0, 19);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function isMain() { return S.view === "main"; }

function casBody(extra) {
  return isMain()
    ? { base_version: S.stateVersion, ...(extra || {}) }
    : { base_rev: S.scenarioRev, ...(extra || {}) };
}

/* ---------- 数据加载 ---------- */

async function loadAll() {
  const [scen, state] = await Promise.all([
    api("GET", "/api/scenarios"),
    api("GET", "/api/state?scenario=" + encodeURIComponent(S.view)),
  ]);
  S.scenarios = scen.scenarios;
  S.state = state;
  S.stateVersion = state.state_version;
  S.scenarioRev = isMain() ? 0 : state.rev;
  renderViewSelect();
  renderDashboard();
  await Promise.all([renderEvents(), renderRules(), renderScenarios()]);
}

/* ---------- 视角切换 ---------- */

function renderViewSelect() {
  const sel = $("#view-select");
  sel.innerHTML =
    `<option value="main">主线</option>` +
    S.scenarios.map((s) =>
      `<option value="${esc(s.id)}" ${s.id === S.view ? "selected" : ""}>分叉：${esc(s.name)}</option>`
    ).join("");
  const info = isMain()
    ? `主线版本 v${S.stateVersion} · 规则 v${S.state.rules_version}`
    : `分叉修订 r${S.state.rev} · 基于主线规则 v${S.state.base_rule_version} · 分叉点 seq=${S.state.fork_seq}`;
  $("#version-info").textContent = info;
}

/* ---------- 义务状态 ---------- */

function renderDashboard() {
  $("#eval-info").textContent =
    `评估时刻：${fmt(S.state.evaluation_time)}（取事件时间线末端，确定性回放，不随系统时钟变化）`;
  const obs = S.state.obligations;
  if (!obs.length) {
    $("#obligations").innerHTML = `<div class="card muted">当前视角下暂无义务实例。</div>`;
    return;
  }
  const rows = obs.map((o) => {
    const ev = o.evidence.map((e) =>
      `<li><span class="time">${fmt(e.at)}</span>${esc(e.detail)}` +
      (e.event_id ? ` <span class="mono muted">[${esc(e.event_id)}#${e.seq}]</span>` : "") +
      `</li>`
    ).join("");
    return `<tr>
      <td><b>${esc(o.rule_name)}</b><br><span class="mono muted">${esc(o.rule_id)}</span></td>
      <td>${esc(o.scope)}</td>
      <td><span class="badge ${o.state}">${o.state_label}</span>${o.paused ? " ⏸" : ""}</td>
      <td>${fmt(o.triggered_at)}</td>
      <td>${fmt(o.started_at)}</td>
      <td>${fmt(o.deadline)}</td>
      <td>${fmt(o.fulfilled_at || o.substituted_at || o.overdue_at)}</td>
      <td><details><summary>依据链（${o.evidence.length}）</summary><ul class="evidence">${ev}</ul></details></td>
    </tr>`;
  }).join("");
  $("#obligations").innerHTML = `<table>
    <thead><tr><th>义务</th><th>范围</th><th>状态</th><th>触发时间</th><th>起算时间</th><th>截止时间</th><th>终结/逾期</th><th>依据链</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

/* ---------- 事件 ---------- */

async function renderEvents() {
  const data = await api("GET", "/api/events?scenario=" + encodeURIComponent(S.view));
  const rows = data.events.map((e) => {
    const retracted = isMain() ? e.retracted : e.effective_retracted;
    const canFork = isMain() && !e.retracted;
    const action = retracted
      ? `<button class="btn small" data-act="restore" data-id="${esc(e.event_id)}">恢复</button>`
      : `<button class="btn small danger" data-act="retract" data-id="${esc(e.event_id)}">撤回</button>`;
    const fork = canFork
      ? `<button class="btn small" data-act="fork" data-seq="${e.seq}">从此分叉</button>` : "";
    const tag = !isMain()
      ? (e.inherited ? '<span class="muted">继承</span>' : '<span class="badge performing">分叉新增</span>')
      : "";
    return `<tr class="${retracted ? "retracted" : ""}">
      <td class="mono">${e.seq}</td>
      <td>${fmt(e.valid_time)}</td>
      <td class="mono">${esc(e.event_type)}</td>
      <td>${esc(e.scope)}</td>
      <td class="mono">${esc(JSON.stringify(e.payload))}</td>
      <td class="mono muted">${esc(e.event_id)}</td>
      <td>${tag}</td>
      <td>${action}${fork}</td>
    </tr>`;
  }).join("");
  $("#events-table").innerHTML = `<table>
    <thead><tr><th>序号</th><th>有效时间</th><th>类型</th><th>范围</th><th>载荷</th><th>ID</th><th>来源</th><th>操作</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="8" class="muted">暂无事件</td></tr>'}</tbody></table>`;

  $("#events-table").querySelectorAll("button").forEach((btn) => {
    btn.onclick = async () => {
      const { act, id, seq } = btn.dataset;
      try {
        if (act === "fork") {
          const name = prompt("分叉名称：", `从 #${seq} 分叉`);
          if (!name) return;
          await api("POST", "/api/scenarios", { name, fork_seq: Number(seq) });
        } else if (isMain()) {
          await api("POST", `/api/events/${id}/${act}`, casBody());
        } else {
          await api("POST", `/api/scenarios/${S.view}/${act}`, casBody({ event_id: id }));
        }
        await loadAll();
      } catch (e) { if (e.message !== "conflict") alert(e.message); }
    };
  });
}

async function importEvents() {
  let parsed;
  try {
    parsed = JSON.parse($("#event-json").value);
  } catch {
    $("#import-msg").textContent = "JSON 解析失败";
    return;
  }
  const events = Array.isArray(parsed) ? parsed : [parsed];
  try {
    const url = isMain() ? "/api/events" : `/api/scenarios/${S.view}/events`;
    const res = await api("POST", url, casBody({ events }));
    $("#import-msg").textContent = `已导入 ${res.event_ids.length} 条`;
    $("#event-json").value = "";
    await loadAll();
  } catch (e) {
    if (e.message !== "conflict") $("#import-msg").textContent = "失败：" + e.message;
  }
}

/* ---------- 规则 ---------- */

async function renderRules() {
  const data = await api("GET", "/api/rules?scenario=" + encodeURIComponent(S.view));
  $("#rules-json").value = JSON.stringify(data.rules, null, 2);
  $("#rules-meta").textContent = isMain()
    ? `主线规则版本：v${data.rules_version}（保存后生成新版本，CAS 基线：主线版本 v${S.stateVersion}）`
    : `分叉规则（基于主线 v${data.base_rule_version} 复制，CAS 基线：分叉修订 r${S.scenarioRev}）`;
  const vers = await api("GET", "/api/rules/versions");
  $("#rule-versions").innerHTML = `<table>
    <thead><tr><th>版本</th><th>备注</th><th>创建时间</th></tr></thead>
    <tbody>${vers.versions.map((v) =>
      `<tr><td>v${v.version}</td><td>${esc(v.note)}</td><td>${fmt(v.created_at)}</td></tr>`
    ).join("")}</tbody></table>`;
}

async function saveRules() {
  let rules;
  try {
    rules = JSON.parse($("#rules-json").value);
  } catch {
    $("#rules-msg").textContent = "JSON 解析失败";
    return;
  }
  try {
    if (isMain()) {
      await api("POST", "/api/rules", casBody({ rules, note: $("#rules-note").value }));
    } else {
      await api("PUT", `/api/scenarios/${S.view}/rules`, casBody({ rules }));
    }
    $("#rules-msg").textContent = "已保存";
    await loadAll();
  } catch (e) {
    if (e.message !== "conflict") $("#rules-msg").textContent = "失败：" + e.message;
  }
}

/* ---------- 分叉与比较 ---------- */

async function renderScenarios() {
  const rows = S.scenarios.map((s) => `<tr>
    <td>${esc(s.name)}</td>
    <td class="mono">${esc(s.id)}</td>
    <td class="mono">seq ≤ ${s.fork_seq}</td>
    <td>主线规则 v${s.base_rule_version}</td>
    <td>r${s.rev}</td>
    <td>
      <button class="btn small" data-act="open" data-id="${esc(s.id)}">打开视角</button>
      <button class="btn small danger" data-act="del" data-id="${esc(s.id)}">删除</button>
    </td>
  </tr>`).join("");
  $("#scenario-list").innerHTML = S.scenarios.length
    ? `<table><thead><tr><th>名称</th><th>ID</th><th>分叉点</th><th>基线规则</th><th>修订</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>`
    : `<div class="card muted">暂无分叉。可在「事件时间线」中对任一事件点击「从此分叉」。</div>`;

  $("#scenario-list").querySelectorAll("button").forEach((btn) => {
    btn.onclick = async () => {
      const { act, id } = btn.dataset;
      if (act === "open") {
        S.view = id;
        await loadAll();
      } else if (act === "del" && confirm("确认删除该分叉？")) {
        await api("DELETE", "/api/scenarios/" + id);
        if (S.view === id) S.view = "main";
        await loadAll();
      }
    };
  });

  const opts = [`<option value="main">主线</option>`]
    .concat(S.scenarios.map((s) => `<option value="${esc(s.id)}">分叉：${esc(s.name)}</option>`))
    .join("");
  $("#compare-a").innerHTML = opts;
  $("#compare-b").innerHTML = opts;
  if (S.scenarios.length) $("#compare-b").value = S.scenarios[S.scenarios.length - 1].id;
}

async function runCompare() {
  const a = $("#compare-a").value, b = $("#compare-b").value;
  const data = await api("GET", `/api/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
  const cell = (o) => o
    ? `<span class="badge ${o.state}">${o.state_label}</span><br><span class="muted">截止 ${fmt(o.deadline)}</span>`
    : '<span class="muted">不存在</span>';
  const rows = data.rows.map((r) => `<tr class="${r.changed ? "changed" : ""}">
    <td><b>${esc(r.rule_name)}</b><br><span class="mono muted">${esc(r.key)}</span></td>
    <td>${esc(r.scope)}</td>
    <td>${cell(r.a)}</td>
    <td>${cell(r.b)}</td>
    <td>${r.changed ? "有差异" : "一致"}</td>
  </tr>`).join("");
  $("#compare-result").innerHTML = `
    <p class="muted">A 评估时刻 ${fmt(data.a_evaluation_time)} · B 评估时刻 ${fmt(data.b_evaluation_time)}</p>
    <table><thead><tr><th>义务</th><th>范围</th><th>A 状态</th><th>B 状态</th><th>结论</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

/* ---------- 启动 ---------- */

function init() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      $("#tab-" + tab.dataset.tab).classList.add("active");
    };
  });
  $("#view-select").onchange = async (e) => { S.view = e.target.value; await loadAll(); };
  $("#btn-refresh").onclick = loadAll;
  $("#btn-import").onclick = importEvents;
  $("#btn-save-rules").onclick = saveRules;
  $("#btn-compare").onclick = () => runCompare().catch((e) => alert(e.message));
  loadAll().catch((e) => alert("加载失败：" + e.message));
}

document.addEventListener("DOMContentLoaded", init);
