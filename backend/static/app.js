/* 合同义务状态推演台 — 原生 JS SPA */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  branches: [],
  branchId: null,
  branch: null,
  ruleVersions: [],        // [{version, note, created_at, rules}]
  selectedRuleVersion: null,
  events: [],
  evalResult: null,
  evalAsOf: null,
  draftRules: [],          // [{rid, rtype, params, description, _deleted?}]
};

// ---------------- API ----------------

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  let data = null;
  const text = await resp.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
  }
  if (!resp.ok) {
    const detail = (data && (data.detail || data.error)) || `HTTP ${resp.status}`;
    const err = new Error(detail);
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

function toast(msg, ms = 4000) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => t.classList.add("hidden"), ms);
}

// ---------------- time ----------------

function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fromLocalInput(value) {
  if (!value) return null;
  const d = new Date(value);
  if (isNaN(d)) return null;
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------- tabs ----------------

$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const map = { tabRules: "paneRules", tabEvents: "paneEvents",
                  tabState: "paneState", tabCompare: "paneCompare" };
    Object.values(map).forEach((id) => $("#" + id).classList.add("hidden"));
    $("#" + map[btn.id]).classList.remove("hidden");
    if (btn.id === "tabState") refreshState();
  });
});

// ---------------- bootstrap ----------------

async function init() {
  bindEventsUI();
  await loadBranches();
  state.branchId = localStorage.getItem("branchId") ||
    (state.branches[0] && state.branches[0].id);
  await selectBranch(state.branchId, true);
}

async function loadBranches() {
  state.ruleVersions = await api("GET", "/api/rule-versions");
  state.branches = await api("GET", "/api/branches");
  renderBranchSelectors();
}

function renderBranchSelectors() {
  const opts = state.branches.map((b) =>
    `<option value="${esc(b.id)}" ${b.id === state.branchId ? "selected" : ""}>` +
    `${esc(b.name)}（${esc(b.id)}，规则 v${b.rule_version}）</option>`).join("");
  $("#branchSelect").innerHTML = opts;
  const cmp = state.branches.map((b) =>
    `<option value="${esc(b.id)}">${esc(b.name)}（${esc(b.id)}）</option>`).join("");
  $("#cmpBase").innerHTML = cmp;
  $("#cmpTarget").innerHTML = cmp;
  if (state.branches[1]) $("#cmpTarget").selectedIndex = 1;
  // 分叉起点事件在选中分支变化时刷新
  refreshForkEvents();
}

async function selectBranch(branchId, silent) {
  if (!branchId) return;
  state.branchId = branchId;
  localStorage.setItem("branchId", branchId);
  $("#branchSelect").value = branchId;
  state.branch = state.branches.find((b) => b.id === branchId);
  await refreshBranchData();
}

async function refreshBranchData() {
  const [branch, events] = await Promise.all([
    api("GET", "/api/branches").then((bs) => bs.find((b) => b.id === state.branchId)),
    api("GET", `/api/branches/${state.branchId}/events`),
  ]);
  state.branch = branch;
  state.events = events;
  state.ruleVersions = await api("GET", "/api/rule-versions");
  renderRulesPane();
  renderEventsPane();
  refreshForkEvents();
  renderBranchMeta();
}

function renderBranchMeta() {
  const b = state.branch;
  $("#branchMeta").textContent =
    `分支版本 v${b.version} · 绑定规则版本 v${b.rule_version} · ${b.note || ""}`;
}

$("#branchSelect").addEventListener("change", async (e) => {
  await selectBranch(e.target.value);
});

// ---------------- rules pane ----------------

function renderRulesPane() {
  const versions = state.ruleVersions;
  const vo = versions.map((v) =>
    `<option value="${v.version}" ${v.version === state.branch.rule_version ? "selected" : ""}>` +
    `v${v.version} · ${esc(v.note || "（无备注）")}</option>`).join("");
  $("#ruleVersionSelect").innerHTML = vo;
  $("#switchRuleVersionSelect").innerHTML = vo;
  if (state.selectedRuleVersion == null) {
    state.selectedRuleVersion = state.branch.rule_version;
  }
  $("#ruleVersionSelect").value = state.selectedRuleVersion;
  loadRulesIntoEditor(state.selectedRuleVersion);
  $("#branchRuleInfo").innerHTML =
    `当前分支 <b>${esc(state.branch.name)}</b> 绑定规则版本 <b>v${state.branch.rule_version}</b>` +
    `（分支版本基线 <b>v${state.branch.version}</b>）`;
}

$("#ruleVersionSelect").addEventListener("change", (e) => {
  state.selectedRuleVersion = Number(e.target.value);
  loadRulesIntoEditor(state.selectedRuleVersion);
});

function loadRulesIntoEditor(version) {
  const v = state.ruleVersions.find((x) => x.version === version);
  if (!v) return;
  state.draftRules = v.rules.map((r) => ({ ...r, params: structuredClone(r.params) }));
  $("#ruleVersionMeta").innerHTML =
    `<b>v${v.version}</b> · ${esc(v.note || "")} · 创建于 ${esc(v.created_at)} · ` +
    `${v.rules.length} 条规则`;
  renderRulesEditor();
}

function renderRulesEditor() {
  const box = $("#rulesEditor");
  box.innerHTML = "";
  state.draftRules.forEach((r, i) => {
    const div = document.createElement("div");
    div.className = "rule-item";
    div.innerHTML = `
      <div class="rule-head">
        <input class="r-rid" value="${esc(r.rid)}" placeholder="规则ID">
        <select class="r-type">
          ${["trigger", "suspend", "substitute", "dependency"].map((t) =>
            `<option ${t === r.rtype ? "selected" : ""}>${t}</option>`).join("")}
        </select>
        <input class="r-desc" value="${esc(r.description || "")}" placeholder="说明" style="flex:1">
        <button class="r-del danger">删除</button>
      </div>
      <pre class="r-params" contenteditable="true" spellcheck="false">${esc(
        JSON.stringify(r.params, null, 2))}</pre>`;
    div.querySelector(".r-rid").addEventListener("input", (e) => (r.rid = e.target.value));
    div.querySelector(".r-type").addEventListener("change", (e) => (r.rtype = e.target.value));
    div.querySelector(".r-desc").addEventListener("input", (e) => (r.description = e.target.value));
    div.querySelector(".r-params").addEventListener("input", (e) => {
      try {
        r.params = JSON.parse(e.target.textContent);
        e.target.style.outline = "";
      } catch {
        e.target.style.outline = "2px solid #dc2626";
      }
    });
    div.querySelector(".r-del").addEventListener("click", () => {
      state.draftRules.splice(i, 1);
      renderRulesEditor();
    });
    box.appendChild(div);
  });
}

$("#addRuleBtn").addEventListener("click", () => {
  state.draftRules.push({
    rid: `R-NEW-${state.draftRules.length + 1}`,
    rtype: "trigger",
    params: { event_type: "", obligation: "", deadline_days: 7 },
    description: "",
  });
  renderRulesEditor();
});

$("#newVersionBtn").addEventListener("click", () => {
  // 以当前选中版本内容为草稿起点
  loadRulesIntoEditor(state.selectedRuleVersion);
  toast("已载入当前版本规则，编辑后点击“校验并创建新版本”");
});

$("#saveVersionBtn").addEventListener("click", async () => {
  const msg = $("#rulesMsg");
  const latest = state.ruleVersions[state.ruleVersions.length - 1].version;
  const note = prompt("新版本备注（说明本次规则变更）", `基于 v${latest} 修改`);
  if (note === null) return;
  try {
    const created = await api("POST", "/api/rule-versions", {
      note, expected_version: latest,
      rules: state.draftRules.map((r) => ({
        rid: r.rid, rtype: r.rtype, params: r.params, description: r.description || "" })),
    });
    msg.className = "msg ok";
    msg.textContent = `已创建 v${created.version}（CAS 基线 v${latest}）`;
    await loadBranches();
    state.selectedRuleVersion = created.version;
    renderRulesPane();
    toast(`规则 v${created.version} 创建成功`);
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = e.status === 409 ? `冲突：${e.message}（未写入任何内容）` : e.message;
  }
});

$("#switchRuleBtn").addEventListener("click", async () => {
  const rv = Number($("#switchRuleVersionSelect").value);
  try {
    const b = await api("PUT", `/api/branches/${state.branchId}/rule-version`, {
      branch_id: state.branchId,
      rule_version: rv,
      expected_branch_version: state.branch.version,
    });
    toast(`分支已切换到规则 v${rv}，新版本 v${b.version}`);
    await refreshBranchData();
  } catch (e) {
    toast(e.status === 409 ? `CAS 冲突，未切换：${e.message}` : e.message);
  }
});

// ---------------- events pane ----------------

function renderEventsPane() {
  $("#eventsBranchMeta").textContent =
    `${state.branch.name}（${state.branchId}）· 分支版本 v${state.branch.version}`;
  $("#eventsBaseline").textContent = `v${state.branch.version}`;
  const tbody = $("#eventsTable tbody");
  tbody.innerHTML = state.events.map((e, i) => `
    <tr class="${e.withdrawn ? "withdrawn" : ""}">
      <td>${i + 1}</td>
      <td>${esc(e.eid)}</td>
      <td>${esc(e.code)}</td>
      <td>${esc(e.etype)}</td>
      <td>${esc(e.valid_at)}<br><small class="hint">seq=${e.seq}</small></td>
      <td>${e.seq}</td>
      <td><code>${esc(JSON.stringify(e.payload))}</code></td>
      <td><small>${esc(e.recorded_at)}</small></td>
      <td>${e.withdrawn ? "已撤回：" + esc(e.withdraw_reason || "") : "有效"}</td>
      <td class="ev-actions">
        ${e.withdrawn ? "" : `
          <button data-act="edit" data-id="${esc(e.eid)}">修改</button>
          <button data-act="withdraw" data-id="${esc(e.eid)}" class="danger">撤回</button>`}
      </td>
    </tr>`).join("");
  tbody.querySelectorAll("button[data-act]").forEach((btn) => {
    btn.addEventListener("click", () =>
      eventAction(btn.dataset.act, btn.dataset.id));
  });
}

function bindEventsUI() {
  $("#addEventBtn").addEventListener("click", submitEvent);
  $("#rerunBtn").addEventListener("click", () => refreshState(true));
  $("#asOfDefaultBtn").addEventListener("click", () => {
    $("#asOf").value = "";
    refreshState(true);
  });
  $("#forkBtn").addEventListener("click", createFork);
  $("#compareBtn").addEventListener("click", runCompare);
}

async function submitEvent() {
  const type = $("#evType").value.trim();
  const validLocal = $("#evValidAt").value;
  if (!type) return toast("请填写事件类型");
  if (!validLocal) return toast("请填写有效时间");
  const payload = {};
  const raw = $("#evPayload").value.trim();
  if (raw) {
    try { Object.assign(payload, JSON.parse(raw)); }
    catch { return toast("附加 payload JSON 格式错误"); }
  }
  if ($("#evObligation").value.trim()) payload.obligation = $("#evObligation").value.trim();
  const recorded = fromLocalInput($("#evRecordedAt").value);
  const ev = {
    etype: type,
    code: $("#evCode").value.trim(),
    valid_at: fromLocalInput(validLocal),
    recorded_at: recorded || undefined,
    seq: Number($("#evSeq").value || 0),
    payload,
  };
  const msg = $("#eventsMsg");
  try {
    const r = await api("POST", `/api/branches/${state.branchId}/events`, {
      events: [ev], expected_version: state.branch.version,
    });
    msg.className = "msg ok";
    msg.textContent = `已提交（${r.created_event_ids.join(", ")}），分支版本 v${r.branch.version}`;
    $("#evType").value = ""; $("#evCode").value = ""; $("#evObligation").value = "";
    $("#evPayload").value = "";
    await refreshBranchData();
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = e.status === 409 ? `CAS 冲突，未写入：${e.message}` : e.message;
  }
}

async function eventAction(act, id) {
  const ev = state.events.find((x) => x.eid === id);
  if (act === "withdraw") {
    const reason = prompt(`撤回 ${id} 的理由（撤回后其后所有状态将重算）`, "录入错误");
    if (reason === null) return;
    try {
      const r = await api("POST", `/api/events/${id}/withdraw`, {
        expected_version: state.branch.version,
      });
      toast(`已撤回 ${id}，分支版本 v${r.branch.version}，状态已重算`);
      await refreshBranchData();
    } catch (e) {
      toast(e.status === 409 ? `CAS 冲突，未撤回：${e.message}` : e.message);
    }
  } else if (act === "edit") {
    await editEventDialog(ev);
  }
}

async function editEventDialog(ev) {
  const etype = prompt("事件类型（留空取消）", ev.etype);
  if (etype === null) return;
  const valid = prompt("修改有效时间（ISO，留空表示不改）", ev.valid_at);
  if (valid === null) return;
  const seqRaw = prompt("修改同刻序号 seq", String(ev.seq));
  if (seqRaw === null) return;
  const changes = { etype: etype || ev.etype, seq: Number(seqRaw) };
  if (valid) changes.valid_at = valid;
  try {
    const r = await api("PATCH", `/api/events/${ev.eid}`, {
      ...changes,
      expected_version: state.branch.version,
    });
    toast(`已修改 ${ev.eid}，分支版本 v${r.branch.version}，状态已重算`);
    await refreshBranchData();
  } catch (e) {
    toast(e.status === 409 ? `CAS 冲突，未修改：${e.message}` : e.message);
  }
}

// ---------------- state pane ----------------

async function refreshState(useInput) {
  let asOf = null;
  if (useInput && $("#asOf").value) asOf = fromLocalInput($("#asOf").value);
  const qs = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
  try {
    state.evalResult = await api("GET", `/api/branches/${state.branchId}/state${qs}`);
  } catch (e) {
    return toast(`推演失败：${e.message}`);
  }
  renderState();
}

function renderState() {
  const r = state.evalResult;
  if (!r) return;
  if (!$("#asOf").value) $("#asOf").value = toLocalInput(r.as_of);
  $("#stateMeta").innerHTML =
    `分支 <b>${esc(r.branch.name)}</b>（${esc(r.branch.id)}，分支 v${r.branch.version}）` +
    ` · 规则 <b>v${r.branch.rule_version}</b>` +
    ` · 推演时点 <b>${esc(r.as_of)}</b>` +
    ` · 回放事件 ${r.events_replayed} 条` +
    (r.events_future ? ` · 未来预登记 ${r.events_future} 条` : "");
  const s = r.summary;
  $("#summaryBar").innerHTML = Object.entries(s).map(([k, n]) =>
    `<span class="summary-chip"><span class="pill ${k}">${k}</span><b>${n}</b></span>`
  ).join("");
  $("#obligationList").innerHTML = r.obligations.map((o, i) => obligationCard(o, i)).join("");
  $$("#obligationList .obl-head").forEach((h) =>
    h.addEventListener("click", () => h.parentElement.classList.toggle("open")));
  $("#engineNotices").innerHTML = r.notices.length
    ? `<h3>引擎提示</h3><ul>${r.notices.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`
    : "";
}

function obligationCard(o, i) {
  const ev = o.evidence.map((l) => `
    <li>
      <span class="ev-at">${esc(l.at || "")}</span>
      <span class="ev-kind">${esc(l.kind)}</span>
      ${esc(l.detail)}
      ${l.rule_id ? `<small class="hint">规则 ${esc(l.rule_id)}</small>` : ""}
      ${l.event_id ? `<small class="hint">事件 ${esc(l.event_id)}</small>` : ""}
    </li>`).join("");
  return `
  <div class="obl-card${i === 0 ? " open" : ""}">
    <div class="obl-head">
      <span class="pill ${o.status}">${o.status}</span>
      <span class="obl-name">${esc(o.obligation)}
        ${o.future ? '<span class="future-tag">未来预登记</span>' : ""}
        ${o.open_pause ? '<span class="future-tag">暂停中</span>' : ""}
      </span>
      <small class="hint">触发 ${esc(o.triggered_at)} · 有效期限 ${esc(o.effective_deadline || "—")}</small>
      <div class="obl-reason">${esc(o.reason)}</div>
    </div>
    <div class="obl-body">
      <div class="kv">
        触发事件 <code>${esc(o.trigger_event_id)}</code> · 触发规则
        <code>${esc(o.trigger_rule_id)}</code> · 谱系根
        <code>${esc(o.lineage_root_event_id)}</code> · 实例序号 ${o.occurrence}<br>
        基础期限 ${esc(o.deadline_base || "无固定期限")} ·
        有效期限 <b>${esc(o.effective_deadline || "—")}</b> ·
        依赖：${o.depends_on.length ? o.depends_on.map(esc).join("、") : "无"}<br>
        履行事件 <code>${esc(o.perform_event_id || "—")}</code>
        ${o.on_time === true ? "· 按时履行" : o.on_time === false ? "· <b style='color:var(--overdue)'>逾期履行</b>" : ""}
        ${o.substituted_by_event_id ? `· 被事件 <code>${esc(o.substituted_by_event_id)}</code> 依规则 <code>${esc(o.substituted_by_rule_id)}</code> 替代` : ""}
      </div>
      ${o.pause_intervals.length ? `<div class="kv">暂停区间：` +
        o.pause_intervals.map((p) => `${esc(p.start)} → ${esc(p.end || "（未恢复）")}`).join("；") +
        `</div>` : ""}
      <b>依据链</b>
      <ul class="evidence">${ev}</ul>
      ${o.notices.length ? `<small class="hint">${o.notices.map(esc).join("；")}</small>` : ""}
    </div>
  </div>`;
}

// ---------------- fork & compare ----------------

function refreshForkEvents() {
  if (!state.events) return;
  $("#forkEventSelect").innerHTML = state.events
    .filter((e) => !e.withdrawn || true)
    .map((e, i) => `<option value="${esc(e.eid)}">${i + 1}. ${esc(e.valid_at)} seq=${e.seq} · ${esc(e.etype)}（${esc(e.eid)}）</option>`)
    .join("");
  // 默认选择最后一个事件
  $("#forkEventSelect").selectedIndex = state.events.length - 1;
}

async function createFork() {
  const name = $("#forkName").value.trim();
  if (!name) return toast("请填写新分支名");
  const rvRaw = $("#forkRuleVersion").value.trim();
  const body = {
    name,
    fork_from_event_id: $("#forkEventSelect").value,
    note: `从 ${state.branchId} 的 ${$("#forkEventSelect").value} 分叉`,
  };
  if (rvRaw) body.rule_version = Number(rvRaw);
  try {
    const b = await api("POST", "/api/branches", body);
    toast(`已创建分叉 ${b.name}（${b.id}），规则 v${b.rule_version}`);
    $("#forkName").value = "";
    await loadBranches();
    state.branchId = b.id;
    await selectBranch(b.id);
  } catch (e) {
    toast(e.message);
  }
}

async function runCompare() {
  const body = {
    base_branch_id: $("#cmpBase").value,
    target_branch_id: $("#cmpTarget").value,
  };
  const asOf = fromLocalInput($("#cmpAsOf").value);
  if (asOf) body.as_of = asOf;
  try {
    const d = await api("POST", "/api/compare", body);
    renderCompare(d);
  } catch (e) {
    toast(`对比失败：${e.message}`);
  }
}

function renderCompare(d) {
  const chips = (sum) => Object.entries(sum).map(([k, n]) =>
    `<span class="pill ${k}" style="margin-right:6px">${k} ${n}</span>`).join("");
  let html = `
  <div class="card">
    <b>基线：</b>${esc(d.base_branch.name)}（${esc(d.base_branch.id)}，规则 v${d.base_branch.rule_version}，as-of ${esc(d.base_as_of)}）<br>
    ${chips(d.base_summary)}<br><br>
    <b>对比：</b>${esc(d.target_branch.name)}（${esc(d.target_branch.id)}，规则 v${d.target_branch.rule_version}，as-of ${esc(d.target_as_of)}）<br>
    ${chips(d.target_summary)}
  </div>`;
  if (!d.changes.length) {
    html += `<div class="card"><b>两个分支的义务状态完全一致。</b></div>`;
  } else {
    html += `<table><thead><tr><th>义务（对齐键）</th><th>变化</th><th>差异</th></tr></thead><tbody>` +
      d.changes.map((c) => {
        let detail = "";
        if (c.change === "新增") {
          detail = `<span class="diff-add">新增：${esc(c.target.status)}</span>`;
        } else if (c.change === "消失") {
          detail = `<span class="diff-del">消失（原：${esc(c.base.status)}）</span>`;
        } else {
          detail = Object.entries(c.fields).map(([f, v]) =>
            `<div><b>${esc(fieldName(f))}</b>：` +
            `<span class="diff-del">${esc(fmt(v.base))}</span> → ` +
            `<span class="diff-add">${esc(fmt(v.target))}</span></div>`).join("");
        }
        return `<tr><td><b>${esc(c.obligation)}</b><br><small class="hint">${esc(c.key)}</small></td>
          <td class="diff-${c.change === "新增" ? "add" : c.change === "消失" ? "del" : "chg"}">${c.change}</td>
          <td>${detail}</td></tr>`;
      }).join("") + "</tbody></table>";
  }
  $("#compareResult").innerHTML = html;
}

function fieldName(f) {
  return ({
    status: "状态", effective_deadline: "有效期限", performed_at: "履行时间",
    on_time: "是否按时", triggered_at: "触发时间",
    trigger_rule_id: "触发规则", reason: "判定理由",
  })[f] || f;
}
function fmt(v) { return v == null ? "—" : String(v); }

init().catch((e) => toast("初始化失败：" + e.message));
