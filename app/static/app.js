"use strict";

const $ = (id) => document.getElementById(id);

let currentRun = null;      // 最近一次获取的演练（含全部已完成步骤）
let selectedStep = 0;       // 当前查看的步骤号
let followLatest = true;    // 是否跟随最新完成步骤
let pollTimer = null;

const LOG_KIND_TEXT = {
  originate: "始发", accept: "接受", duplicate: "重复", withdraw: "撤销",
  disconnect: "断开", reconnect: "重连", resend: "重发", loop: "环路拒绝",
  stale: "过期忽略", stale_withdraw: "旧纪元撤销", expired: "过期消息忽略",
  delivered: "投递成功", enqueue: "入队", ignored: "忽略", invalid: "无效",
  failed: "失败",
};

const MSG_STATUS = {
  pending: "待投递",
  delivered: "已投递",
  expired: "过期忽略",
  failed: "失败",
};

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  }[c]));
}

async function api(path, options) {
  const resp = await fetch(path, options);
  let data = null;
  try { data = await resp.json(); } catch (e) { /* 忽略 */ }
  return { ok: resp.ok, status: resp.status, data };
}

function showErrors(errors) {
  const box = $("errors");
  if (!errors || !errors.length) { box.innerHTML = ""; box.style.display = "none"; return; }
  box.style.display = "block";
  box.innerHTML = "<b>录入校验未通过：</b><ul>" +
    errors.map((e) => `<li>${esc(e)}</li>`).join("") + "</ul>";
}

async function loadSample() {
  const { data } = await api("/api/sample");
  if (!data) return;
  $("routers").value = data.routers_text;
  $("links").value = data.links_text;
  $("events").value = data.events_text;
}

async function startDrill() {
  const body = {
    routers_text: $("routers").value,
    links_text: $("links").value,
    events_text: $("events").value,
  };
  const resp = await fetch("/api/drills", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    showErrors((data && data.errors) || ["创建演练失败"]);
    return;
  }
  showErrors([]);
  followLatest = true;
  $("follow-chk").checked = true;
  await refresh();
}

async function refresh() {
  const { data } = await api("/api/drills/current");
  currentRun = data ? data.run : null;
  if (currentRun && followLatest) selectedStep = currentRun.completed_steps;
  render();
  if (currentRun && !currentRun.done) schedulePoll();
}

function schedulePoll() {
  if (pollTimer) return;
  pollTimer = setTimeout(async () => { pollTimer = null; await refresh(); }, 700);
}

function describeEvent(ev) {
  if (!ev) return "初始状态（尚未应用任何事件）";
  switch (ev.type) {
    case "announce":
      return ev.from === ev.to
        ? `通告：${ev.from} 本机始发前缀 ${ev.prefix}`
        : `通告：${ev.from} → ${ev.to}，前缀 ${ev.prefix}，路径 ${ev.path.join("-")}，纪元 ${ev.epoch}`;
    case "withdraw":
      return `撤销：${ev.from} → ${ev.to}，前缀 ${ev.prefix}，纪元 ${ev.epoch}`;
    case "disconnect":
      return `断开：邻接 ${ev.from} → ${ev.to}`;
    case "reconnect":
      return `重连：邻接 ${ev.from} → ${ev.to}（链路纪元递增）`;
    case "deliver":
      return `投递：${ev.src} → ${ev.dst}，消息「${ev.msg || "空载荷"}」`;
    default:
      return esc(JSON.stringify(ev));
  }
}

function renderStatus(run) {
  const el = $("run-status");
  if (!run) {
    el.innerHTML = "尚无演练：请在下方录入并开始，或载入示例。";
    el.className = "status";
    return;
  }
  const progress = run.done
    ? "后台计算完成"
    : `后台计算中… 已完成步骤 ${run.completed_steps}/${run.total}`;
  let conv = "收敛校验：待计算完成";
  let convCls = "pending";
  if (run.done) {
    if (run.converged === true) { conv = "收敛校验：恢复回放与不中断回放一致 ✓"; convCls = "ok"; }
    else { conv = "收敛校验：恢复回放与不中断回放不一致 ✗"; convCls = "bad"; }
  }
  el.innerHTML =
    `演练 #${run.seq}（${esc(run.run_id)}） · ${esc(progress)} · ` +
    `<span class="conv-${convCls}">${esc(conv)}</span>`;
  el.className = "status";
}

function renderStepBar(run) {
  const slider = $("step-slider");
  const label = $("step-label");
  if (!run) {
    slider.max = 0; slider.value = 0;
    label.textContent = "—";
    return;
  }
  slider.max = run.completed_steps;
  if (selectedStep > run.completed_steps) selectedStep = run.completed_steps;
  slider.value = selectedStep;
  label.textContent = `步骤 ${selectedStep} / ${run.total}（已完成 ${run.completed_steps}）`;
}

function renderStep(run) {
  const evEl = $("event-view");
  const logsEl = $("logs");
  if (!run || !run.steps.length) {
    evEl.textContent = "尚无步骤";
    logsEl.innerHTML = "";
    return;
  }
  const step = run.steps[selectedStep];
  evEl.innerHTML = `<b>步骤 ${step.step}：</b>${esc(describeEvent(step.event))}`;
  if (!step.logs.length) {
    logsEl.innerHTML = '<div class="log log-info">本步骤无日志</div>';
    return;
  }
  logsEl.innerHTML = step.logs.map((log) => {
    const kind = esc(log.kind);
    const tag = LOG_KIND_TEXT[log.kind] || log.kind;
    return `<div class="log log-${kind}"><span class="log-tag">[${esc(tag)}]</span> ${esc(log.text)}</div>`;
  }).join("");
}

function tableHtml(headers, rows, emptyText) {
  if (!rows.length) return `<div class="empty">${esc(emptyText)}</div>`;
  const head = headers.map((h) => `<th>${esc(h)}</th>`).join("");
  const body = rows.map((r) => "<tr>" + r.map((c) => `<td>${c}</td>`).join("") + "</tr>").join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function renderAdj(run, state) {
  const rows = Object.keys(state.adj).sort().map((key) => {
    const a = state.adj[key];
    const cls = a.up ? "up" : "down";
    return [esc(key), String(a.localpref), String(a.epoch),
            `<span class="${cls}">${a.up ? "在线" : "断开"}</span>`];
  });
  $("adj-table").innerHTML = tableHtml(["邻接", "本地偏好", "链路纪元", "状态"], rows, "无邻接");
}

function renderInbound(run, state) {
  const rows = [];
  for (const router of Object.keys(state.inbound).sort()) {
    const prefixes = state.inbound[router];
    for (const prefix of Object.keys(prefixes).sort()) {
      const routes = prefixes[prefix];
      for (const nbr of Object.keys(routes).sort()) {
        const rt = routes[nbr];
        rows.push([esc(router), esc(nbr === "" ? "（本机始发）" : nbr), esc(prefix),
                   esc(rt.path.join(" → ")), String(rt.epoch)]);
      }
    }
  }
  $("inbound-table").innerHTML =
    tableHtml(["路由器", "邻居", "前缀", "路径", "纪元"], rows, "暂无入站路由");
}

function renderBest(run, step) {
  const rows = [];
  const best = step ? step.best : {};
  for (const router of Object.keys(best).sort()) {
    for (const prefix of Object.keys(best[router]).sort()) {
      const b = best[router][prefix];
      const nh = b.neighbor === "" ? "（本机）" : b.neighbor;
      const lp = b.localpref >= (1 << 30) ? "∞" : String(b.localpref);
      rows.push([esc(router), esc(prefix), esc(nh), esc(b.path.join(" → ")), lp, String(b.path.length)]);
    }
  }
  $("best-table").innerHTML =
    tableHtml(["路由器", "前缀", "下一跳", "路径", "本地偏好", "长度"], rows, "暂无最优路径");
}

function renderMessages(run, state) {
  const rows = state.messages.map((m) => {
    const status = MSG_STATUS[m.status] || m.status;
    const cls = `msg-${m.status}`;
    const hops = m.hops && m.hops.length ? m.hops.join(" → ") : "—";
    return [`#${m.id}`, `${esc(m.src)} → ${esc(m.dst)}`, esc(m.payload || ""),
            `<span class="${cls}">${esc(status)}</span>`, esc(hops), esc(m.detail || "")];
  });
  $("msg-table").innerHTML =
    tableHtml(["#", "源 → 宿", "内容", "状态", "路径", "说明"], rows, "暂无消息");
}

function render() {
  renderStatus(currentRun);
  renderStepBar(currentRun);
  renderStep(currentRun);
  if (!currentRun || !currentRun.steps.length) {
    $("adj-table").innerHTML = '<div class="empty">无数据</div>';
    $("inbound-table").innerHTML = '<div class="empty">无数据</div>';
    $("best-table").innerHTML = '<div class="empty">无数据</div>';
    $("msg-table").innerHTML = '<div class="empty">无数据</div>';
    return;
  }
  const step = currentRun.steps[selectedStep];
  const state = step.state;
  renderAdj(currentRun, state);
  renderInbound(currentRun, state);
  renderBest(currentRun, step);
  renderMessages(currentRun, state);
}

function bind() {
  $("start-btn").addEventListener("click", () => { startDrill().catch(console.error); });
  $("sample-btn").addEventListener("click", () => { loadSample().catch(console.error); });
  $("step-slider").addEventListener("input", (e) => {
    followLatest = false;
    $("follow-chk").checked = false;
    selectedStep = Number(e.target.value);
    render();
  });
  $("follow-chk").addEventListener("change", (e) => {
    followLatest = e.target.checked;
    if (followLatest && currentRun) {
      selectedStep = currentRun.completed_steps;
      render();
    }
  });
  $("prev-btn").addEventListener("click", () => {
    if (selectedStep > 0) { selectedStep -= 1; followLatest = false; $("follow-chk").checked = false; render(); }
  });
  $("next-btn").addEventListener("click", () => {
    if (currentRun && selectedStep < currentRun.completed_steps) {
      selectedStep += 1; render();
    }
  });
}

async function boot() {
  bind();
  await loadSample();
  await refresh();  // 页面重开后恢复最后完整步骤
}

boot().catch((err) => {
  console.error(err);
  $("run-status").textContent = "初始化失败：" + err;
});
