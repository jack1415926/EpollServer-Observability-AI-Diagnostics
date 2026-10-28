"use strict";
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ""; this.hidden = true; this.dataset = {}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  get childElementCount() { return this.children.length; }
}
const source = fs.readFileSync("monitor/web/app.js", "utf8");
const render = source.slice(0, source.indexOf("async function refreshDiagnoses"));
const textHelper = source.match(/function text\(tag, value, className\) \{[^\n]+\}/)[0];
function texts(element) { return [element.textContent, ...element.children.flatMap(texts)].filter(Boolean); }
for (const [kind, expected] of [["knowledge", "技术资料回答"], ["experiments", "历史实验分析"], ["clarify", "需要补充信息"], ["out_of_scope", "支持范围提示"], [undefined, "证据不足，无法确定根因。"]]) {
  const target = new Element("article");
  const context = vm.createContext({document: {createElement: tag => new Element(tag)}, generation: 0, $: () => target});
  vm.runInContext(render + textHelper + "\nglobalThis.renderTest=renderDiagnosis;", context);
  const task = {id: "test", status: "succeeded", question: "<img onerror=alert(1)>", report: {
    kind, analysis: {conclusion: "insufficient_evidence", summary: "说明", facts: [], possible_causes: [], unknowns: [], next_steps: []}, evidence: [], limits: []}};
  context.renderTest(task);
  const values = texts(target);
  assert(values.includes(expected));
  assert(values.includes(task.question));
  if (kind) assert(!values.includes("证据不足，无法确定根因。"));
  assert(!values.includes("可能原因"));
  assert(!values.includes("证据与来源"));
}
console.log("PASS: routed report labels, empty sections and legacy report rendering");
{
  const target = new Element("article");
  const context = vm.createContext({document: {createElement: tag => new Element(tag)}, generation: 0, $: () => target});
  vm.runInContext(render + textHelper + "\nglobalThis.renderTest=renderDiagnosis;", context);
  const task = {id: "supported", status: "succeeded", question: "核验", report: {
    kind: "runtime", analysis: {conclusion: "insufficient_evidence", summary: "仍未知",
      facts: [{text: "另一台机器很慢", basis: "user_statement", evidence_ids: ["E1"]}],
      excluded_causes: [{text: "单纯暂停不能解释dead", evidence_ids: ["E2"]}],
      possible_causes: [], unknowns: [], next_steps: []}, evidence: [], limits: []}};
  context.renderTest(task);
  assert(texts(target).includes("可排除的解释"));
  assert(texts(target).some(t => t.startsWith("用户陈述：")));
  task.id = "failure"; task.status = "failed"; task.report.kind = "failure";
  task.report.analysis.facts = []; task.report.analysis.excluded_causes = [];
  context.renderTest(task);
  assert(texts(target).includes("诊断未完成"));
  task.id="observation";task.status="succeeded";task.report.kind="runtime";task.report.answer_mode="observation";
  context.renderTest(task);
  assert(texts(target).includes("取证时的观测"));
  assert(!texts(target).includes("证据不足，无法确定根因。"));
}
console.log("PASS: support provenance, exclusions and failed-run record rendering");
{
  const elements = new Map();
  const document = {createElement: tag => new Element(tag), addEventListener() {},
    createTextNode: value => Object.assign(new Element("text"), {textContent: value}),
    getElementById: id => {
      if (!elements.has(id)) elements.set(id, new Element("div"));
      return elements.get(id);
    }};
  Element.prototype.replaceWith = function () {};
  const context = vm.createContext({document, Node: Element});
  vm.runInContext(source.slice(0, source.indexOf("async function refreshLive")) + "\nglobalThis.renderLiveTest=renderLive;", context);
  const worker = {worker_index: 0, pid: 12, process_start_ticks: 34, status: "stale", metrics: {}, age_ms: 5000,
    resources: {status:"available", cpu_percent:12.5, rss_bytes:1572864, interval_ms:500, sampled_at_unix_ms:1793000000000}};
  const overview = {validity:"partial", data:{totals:{},partial_sums:{},master:{},valid_workers:0,expected_workers:1}};
  context.renderLiveTest(overview, {data:{workers:[worker]}});
  let values = texts(elements.get("worker-rows"));
  assert(values.includes("12.5%")); assert(values.includes("1.5 MiB")); assert(values.includes("已过期"));
  assert(values.some(v => v.includes("CPU 窗口 500 ms")));
  worker.resources.cpu_percent = null; worker.resources.status="partial"; worker.resources.reason="cpu_warming_up";
  context.renderLiveTest(overview, {data:{workers:[worker]}});
  values = texts(elements.get("worker-rows"));
  assert(!values.includes("0%")); assert(values.includes("CPU 等待第二次采样"));
  worker.resources.cpu_percent = 0;
  context.renderLiveTest(overview, {data:{workers:[worker]}});
  assert(texts(elements.get("worker-rows")).includes("0%"));
  delete worker.resources;
  context.renderLiveTest(overview, {data:{workers:[worker]}});
  assert(texts(elements.get("worker-rows")).includes("未采集"));
}
console.log("PASS: Worker CPU/RSS, warmup null, zero, stale business and legacy rendering");
