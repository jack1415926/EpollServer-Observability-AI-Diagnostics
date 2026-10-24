"use strict";
const $ = id => document.getElementById(id);
let token = "", generation = 0, timer = null, refreshing = false, experiments = [];
const descriptions = {overview: ["运行总览", "观察当前实例的连接、队列与采集状态。"], workers: ["Worker 状态", "查看当前实例的进程身份、指标与数据有效性。"], experiments: ["性能实验", "保存运行证据，在明确条件下比较实验结果。"]};
const labels = {complete: "采集完整", partial: "部分数据", stopping: "实例停止中", unavailable: "指标不可用", instance_changed: "实例变化", fresh: "有效", stale: "已过期", dead: "进程已退出", missing: "快照缺失", invalid_json: "快照损坏", collection_failed: "采集失败"};
const fmt = (value, digits = 0) => value == null || !Number.isFinite(Number(value)) ? "—" : Number(value).toLocaleString("zh-CN", {maximumFractionDigits: digits});
function text(tag, value, className) { const el = document.createElement(tag); el.textContent = value; if (className) el.className = className; return el; }
function badge(status) { return text("span", labels[status] || status, `badge ${status === "complete" || status === "fresh" ? "good" : status === "partial" || status === "stopping" ? "warn" : "bad"}`); }
function cells(row, values) { for (const value of values) { const td = document.createElement("td"); td.append(value instanceof Node ? value : document.createTextNode(String(value))); row.append(td); } }
function notice(message, error = false) { $("notice").textContent = message; $("notice").className = `notice${error ? " error" : ""}`; }
async function api(path, options = {}) {
  const response = await fetch(`/api/v1/${path}`, {...options, headers: {Authorization: `Bearer ${token}`, ...(options.body ? {"Content-Type": "application/json"} : {})}, signal: AbortSignal.timeout(5000), cache: "no-store"});
  const result = await response.json();
  if (!response.ok && !(response.status === 503 && ["overview", "workers", "health"].includes(path) && result.data)) {
    const err = new Error(response.status === 401 ? "令牌无效，请重新连接。" : `${result.code || "http_error"}（HTTP ${response.status}）`); err.status = response.status; throw err;
  }
  return result;
}
function clearLive() {
  for (const id of ["connections", "valid-workers", "receive-queue", "send-queue"]) $(id).textContent = "—";
  $("expected-workers").textContent = "预期数量待采集"; $("instance").replaceChildren(); $("worker-rows").replaceChildren();
  $("worker-empty").hidden = false; $("worker-empty").textContent = "当前没有有效采集结果。"; $("worker-count").textContent = "暂无数据";
  $("updated").textContent = "尚未采集"; $("partial-note").textContent = "缺失指标显示「—」，不会补零。部分数据不作为实例完整总量。";
}
function renderLive(overview, workers) {
  const data = overview.data, totals = data.totals || {};
  $("connections").textContent = fmt(totals.connections_current); $("receive-queue").textContent = fmt(totals.receive_queue_messages); $("send-queue").textContent = fmt(totals.send_queue_messages);
  $("valid-workers").textContent = fmt(data.valid_workers); $("expected-workers").textContent = `预期 ${fmt(data.expected_workers)} 个 Worker`;
  const entries = [["采集状态", labels[overview.validity] || overview.validity], ["Master PID", data.master?.master_pid ?? "—"], ["实例启动刻度", data.master?.master_start_ticks ?? "—"], ["名单状态", data.master?.state ?? "—"], ["数据年龄上限", `${data.max_age_ms} ms`], ["采集原因", data.reason]];
  $("instance").replaceChildren(...entries.map(([key, value]) => { const row = document.createElement("div"); row.append(text("dt", key), text("dd", value)); return row; }));
  $("connection").replaceWith(Object.assign(badge(overview.validity), {id: "connection"}));
  const rows = workers.data.workers || [];
  $("worker-count").textContent = `${rows.length} 个 Worker`;
  $("worker-empty").hidden = rows.length > 0;
  $("worker-rows").replaceChildren(...rows.map(worker => {
    const row = document.createElement("tr"), name = text("span", `Worker ${worker.worker_index}`);
    name.append(text("small", `PID ${worker.pid} · ticks ${worker.process_start_ticks}`));
    cells(row, [name, badge(worker.status), fmt(worker.metrics?.connections_current), fmt(worker.metrics?.receive_queue_messages), fmt(worker.metrics?.send_queue_messages), worker.age_ms == null ? "—" : `${fmt(worker.age_ms)} ms`]);
    return row;
  }));
  if (overview.validity !== "complete") $("partial-note").textContent = `完整总量不可用。有效部分：连接 ${fmt(data.partial_sums?.connections_current)}，接收队列 ${fmt(data.partial_sums?.receive_queue_messages)}，发送队列 ${fmt(data.partial_sums?.send_queue_messages)}；仅代表已采集 Worker。`;
  else $("partial-note").textContent = "当前基础指标完整。逐字段采样，不是所有进程同一瞬间的事务快照。";
  $("updated").textContent = data.collected_at_unix_ms ? `采集于 ${new Date(data.collected_at_unix_ms).toLocaleTimeString("zh-CN")} · 每 2 秒刷新` : "当前无有效采集时间";
  notice(overview.validity === "complete" ? "已连接 · 实时指标每 2 秒刷新。TCP 业务健康未主动探测。" : `监控服务可达，采集状态：${labels[overview.validity] || overview.validity} · ${data.reason}`, overview.validity !== "complete");
}
async function refreshLive() {
  if (!token || refreshing) return;
  refreshing = true; const current = generation;
  try {
    const [overview, workers] = await Promise.all([api("overview"), api("workers")]);
    if (current === generation) renderLive(overview, workers);
  } catch (error) {
    if (current !== generation) return;
    clearLive(); $("connection").textContent = "连接失败"; $("connection").className = "badge bad";
    notice(`实时数据不可用：${error.message}`, true);
    if (error.status === 401) disconnect("令牌无效，请重新连接。");
  } finally { refreshing = false; }
}
function disconnect(message = "已断开。输入有效令牌后可重新连接。") {
  token = ""; generation++; clearInterval(timer); timer = null; $("token").value = ""; $("disconnect").hidden = true; $("refresh").disabled = true;
  $("connection").textContent = "未连接"; $("connection").className = "badge"; clearLive(); experiments = []; renderExperiments();
  $("comparison").hidden = true; $("comparison-rows").replaceChildren(); $("comparison-conditions").textContent = "";
  $("detail").close(); $("detail-body").textContent = ""; notice(message);
}
function renderExperiments() {
  $("experiment-rows").replaceChildren(...experiments.map(item => {
    const row = document.createElement("tr"), name = text("span", item.name);
    name.append(text("small", item.timestamp_utc));
    const detail = text("button", "查看详情"); detail.type = "button";
    detail.addEventListener("click", async () => {
      const current = generation;
      try { const result = await api(`experiments/${item.id}`); if (current !== generation) return; $("detail-title").textContent = item.name; $("detail-body").textContent = JSON.stringify(result.data.report, null, 2); $("detail").showModal(); }
      catch (error) { if (current === generation) $("experiment-notice").textContent = error.message; }
    });
    cells(row, [name, `${item.load.concurrency} / ${item.load.processes}`, fmt(item.result.qps, 2), `${fmt(item.result.latency_ms.p95, 3)}${item.load.latency_mode === "sampled" ? "（抽样）" : ""}`, fmt(item.result.failed + item.result.not_attempted), detail]);
    return row;
  }));
  for (const id of ["baseline", "candidate"]) {
    const previous = $(id).value;
    $(id).replaceChildren(new Option(id === "baseline" ? "选择基准" : "选择候选", ""), ...experiments.map(item => new Option(item.name, item.id)));
    if (experiments.some(item => item.id === previous)) $(id).value = previous;
  }
  $("experiment-notice").textContent = token ? (experiments.length ? `${experiments.length} 条实验记录；可查看详情或选择两次进行对比。` : "暂无实验。先运行压测，生成并导入实验 JSON。") : "连接后读取实验记录。";
}
async function refreshExperiments() {
  if (!token) return;
  const current = generation;
  try { const value = await api("experiments"); if (current !== generation) return; experiments = value.data.experiments; renderExperiments(); }
  catch (error) { if (current !== generation) return; experiments = []; renderExperiments(); $("experiment-notice").textContent = `实验记录不可用：${error.message}。实时监控独立运行。`; }
}
function navigate() {
  const page = Object.hasOwn(descriptions, location.hash.slice(1)) ? location.hash.slice(1) : "overview";
  for (const id of Object.keys(descriptions)) $(id).hidden = id !== page;
  for (const link of document.querySelectorAll("nav a")) { if (link.dataset.page === page) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); }
  $("page-title").textContent = $("breadcrumb").textContent = descriptions[page][0]; $("page-description").textContent = descriptions[page][1];
  if (page === "experiments") refreshExperiments();
}
$("auth-form").addEventListener("submit", async event => {
  event.preventDefault(); const submitted = $("token").value;
  disconnect(); token = submitted; const current = generation; $("disconnect").hidden = false; $("refresh").disabled = false;
  notice("正在连接监控服务…");
  await refreshLive();
  if (token && current === generation) { timer = setInterval(refreshLive, 2000); await refreshExperiments(); }
});
$("disconnect").addEventListener("click", () => disconnect());
$("refresh").addEventListener("click", () => { refreshLive(); refreshExperiments(); });
$("compare-form").addEventListener("submit", async event => {
  event.preventDefault(); if (!token) { $("experiment-notice").textContent = "请先连接监控服务。"; return; }
  const current = generation;
  const baseline = $("baseline").value, candidate = $("candidate").value;
  try {
    const value = await api("experiments/compare", {method: "POST", body: JSON.stringify({baseline, candidate})});
    if (current !== generation || baseline !== $("baseline").value || candidate !== $("candidate").value) return;
    $("comparison").hidden = false;
    const d = value.data;
    const conditions = {"/benchmark/load": "压测负载与采样参数", "/benchmark/server": "服务端配置", "/benchmark/system": "操作系统", "/benchmark/python": "Python 版本", "/benchmark/cpu_count": "CPU 数量", "/benchmark/memory_total_mib": "总内存", "/benchmark/git_commit": "代码版本", "/benchmark/git_dirty": "工作区修改状态", "/benchmark/target/host": "目标主机", "/context/environment_id": "实验环境", "/context/binary_sha256": "服务端二进制", "/context/client_sha256": "压测脚本", "/context/server_cpus": "服务端 CPU 绑定", "/context/client_cpus": "客户端 CPU 绑定", "/context/snapshot_enabled": "快照开关", "/context/redis_window_sec": "限流时间窗口", "/context/redis_max_requests": "限流阈值"};
    $("comparison-conditions").textContent = d.same_conditions ? "已记录的运行条件一致；仍需多轮实验排除波动。" : `运行条件有差异：${d.condition_differences.map(key => conditions[key] || "其他运行条件").join("、")}。请结合实验设计解释数值变化。`;
    $("comparison-conditions").className = `notice${d.same_conditions ? "" : " error"}`;
    const names = {qps: "吞吐量 QPS", p95_ms: "P95 延迟 (ms)", mean_latency_ms: "延迟样本均值 (ms)", unsuccessful_percent: "未成功请求占比 (%)"};
    $("comparison-rows").replaceChildren(...Object.entries(names).map(([key, name]) => { const row = document.createElement("tr"), metric = d.metrics[key]; cells(row, [name, fmt(metric.baseline, 3), fmt(metric.candidate, 3), fmt(metric.delta, 3), metric.change_percent == null ? "—" : `${fmt(metric.change_percent, 2)}%`]); return row; }));
  } catch (error) { if (current === generation) { $("comparison").hidden = true; $("experiment-notice").textContent = error.message; } }
});
$("import-file").addEventListener("change", async event => {
  const file = event.target.files[0], current = generation;
  if (!file) return;
  try {
    if (!token) throw new Error("请先连接监控服务。");
    if (file.size > 65536) throw new Error("实验 JSON 不能超过 64 KiB。");
    const body = await file.text();
    JSON.parse(body);
    if (current !== generation) return;
    await api("experiments", {method: "POST", body});
    if (current === generation) await refreshExperiments();
  } catch (error) { if (current === generation) $("experiment-notice").textContent = `导入失败：${error.message}`; }
  finally { event.target.value = ""; }
});
$("close-detail").addEventListener("click", () => $("detail").close());
for (const id of ["baseline", "candidate"]) $(id).addEventListener("change", () => { $("comparison").hidden = true; });
window.addEventListener("hashchange", navigate);
clearLive(); navigate();
