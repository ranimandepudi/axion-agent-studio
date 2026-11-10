"use strict";

// Run with: node --test tests/frontend_live_console.test.cjs
// These are logic/DOM-write contract tests, not a browser or layout emulator.
// No browser engine, network access, generated app code, or live URL is used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const webDir = path.join(__dirname, "..", "web");
const source = fs.readFileSync(path.join(webDir, "app.js"), "utf8");
assert.match(source, /\binitialize\(\);\s*$/);
const sourceWithoutStartup = source.replace(/\binitialize\(\);\s*$/, "");
const roles = ["backend", "frontend", "qa"];
const stageNames = ["request", "manager", "coordinator", ...roles, "validator", "result"];

// A write sink records only the properties touched by the functions under test.
// It does not parse HTML, render, resolve resources, or navigate iframe URLs.
function element(tag = "div") {
  const attributes = new Map();
  const classes = new Set();
  const selectors = new Map();
  const el = {
    tag, children: [], dataset: {}, style: {}, hidden: false, value: "",
    attributes, messages: [], listeners: new Map(),
    classList: {
      add: (...items) => items.forEach((item) => classes.add(item)),
      remove: (...items) => items.forEach((item) => classes.delete(item)),
      toggle(item, force) {
        const enabled = force ?? !classes.has(item);
        if (enabled) classes.add(item); else classes.delete(item);
        return enabled;
      },
      contains: (item) => classes.has(item),
    },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; this.ownText = ""; },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    removeAttribute(name) { attributes.delete(name); if (name === "src") delete this.src; },
    getAttribute(name) { return attributes.get(name) ?? null; },
    addEventListener(name, handler) { this.listeners.set(name, handler); },
    focus() { this.focused = true; },
    querySelector(selector) {
      const matches = (child) => selector.startsWith(".") ? child.className?.split(" ").includes(selector.slice(1)) : child.tag === selector;
      const find = (children) => {
        for (const child of children) {
          if (matches(child)) return child;
          const nested = find(child.children || []);
          if (nested) return nested;
        }
        return null;
      };
      const existing = find(this.children);
      if (existing) return existing;
      if (!selectors.has(selector)) selectors.set(selector, element(selector));
      return selectors.get(selector);
    },
    contentWindow: { postMessage(payload, target) { el.messages.push({ payload, target }); } },
  };
  Object.defineProperty(el, "textContent", {
    get() { return (this.ownText || "") + this.children.map((child) => child.textContent ?? String(child)).join(" "); },
    set(value) { this.ownText = String(value); this.children = []; },
  });
  Object.defineProperty(el, "firstElementChild", { get() { return this.children[0] || this.querySelector("first-child"); } });
  return el;
}

function harness(query = "") {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  const tabs = ["team", "result"].map((name) => {
    const tab = get(`booth-tab-${name}`); tab.dataset.boothPanel = name; return tab;
  });
  const inspect = stageNames.map((name) => {
    const button = element("button"); button.dataset.inspect = name; return button;
  });
  const placements = ["cluster", "coordinator", ...roles, "validator", "result"].map((name) => {
    const badge = element("span"); badge.dataset.placement = name; return badge;
  });
  const flows = stageNames.map((name) => {
    const flow = element("span"); flow.dataset.flowStage = name; return flow;
  });
  const agentStatuses = roles.map((role) => {
    const status = element("span"); status.dataset.agentStatus = role; return status;
  });
  const vmHosts = ["manager", ...roles, "validator", "result"].map((role) => {
    const label = element("span"); label.dataset.vmHost = role; return label;
  });
  const document = {
    body: element("body"),
    getElementById: get,
    createElement: element,
    createElementNS(namespaceURI, tag) { return Object.assign(element(tag), { namespaceURI }); },
    createTextNode: (text) => ({ textContent: String(text) }),
    querySelectorAll(selector) {
      if (selector === "button[data-booth-panel]") return tabs;
      if (selector === "[data-inspect]" || selector === "button[data-inspect]") return inspect;
      if (selector === "[data-placement]") return placements;
      if (selector === "[data-flow-stage]") return flows;
      if (selector === "[data-agent-status]") return agentStatuses;
      if (selector === "[data-vm-host]") return vmHosts;
      return [];
    },
    querySelector(selector) {
      return tabs.find((tab) => selector === `button[data-booth-panel="${tab.dataset.boothPanel}"]`) || null;
    },
  };
  const location = new URL(`https://studio.invalid/${query}`);
  const urls = [], timers = [], clearedTimers = [];
  const context = vm.createContext({
    document, URL, URLSearchParams, Intl, AbortController,
    Date: class extends Date { static now() { return Date.parse("2026-10-20T10:00:05Z"); } },
    window: {
      location,
      history: { replaceState(_state, _title, url) {
        urls.push(String(url)); location.href = String(url);
      } },
      setInterval: () => 1,
    },
    requestAnimationFrame: (callback) => callback(),
    setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearTimeout: (id) => clearedTimers.push(id),
    fetch: () => { throw new Error("Network access is forbidden in these tests"); },
  });
  vm.runInContext(`${sourceWithoutStartup}
globalThis.subject = {
  state, workflowStages, workflowActivity, architecturePlacement, currentArchitecturePlacement, currentVmTopology, cpuUsage, cpuSparkline, workloadTopology, updatedShopPath,
  selectBoothRun, selectRun, syncBoothPanel, setBoothPanel, refreshQuotes,
  renderPreview, renderBoothOverview, renderRun, renderEnvironment, renderHistory,
  boothLabel, startRun, initialize, selectMode, refreshConnection, refreshRun,
  selectBoothNode, renderBoothInspector, renderMetrics, renderBooth, playBoothTour,
  acceptCurrentMetrics, refreshCurrentMetrics, currentCpuState, scheduleCurrentMetrics
  , runtimeConfiguration, renderRuntimeConfiguration, toggleRuntimeConfiguration
};`, context, { filename: "web/app.js" });
  return {
    ...context.subject, context, get, tabs, inspect, placements, flows, agentStatuses, vmHosts, document, urls, timers, clearedTimers,
    replace(code) { vm.runInContext(code, context); },
  };
}

function run(overrides = {}) {
  return {
    id: "run-123", mode: "live", status: "running", plan: [{ role: "backend" }],
    agents: roles.map((role) => ({ role, status: "pending" })), ...overrides,
  };
}

function plain(value) { return JSON.parse(JSON.stringify(value)); }
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test("idle and queued states do not invent model activity", () => {
  const h = harness();
  assert.deepEqual(plain(h.workflowStages(null)), Object.fromEntries(stageNames.map((key) => [key, "idle"])));
  assert.equal(h.workflowActivity(null), "Ready for your request");
  const queued = h.workflowStages(run({ status: "queued", plan: [] }));
  assert.equal(queued.coordinator, "running");
  assert.equal(queued.manager, "idle");
  assert.equal(queued.backend, "idle");
  assert.match(h.workflowActivity(run({ status: "queued" })), /Request accepted/);
});

test("planning distinguishes hosted Gemini from deterministic rehearsal", () => {
  const h = harness();
  const planning = run({ status: "planning", plan: [] });
  assert.equal(h.workflowStages(planning).manager, "running");
  assert.equal(h.workflowStages(planning).validator, "idle");
  assert.match(h.workflowActivity(planning), /Gemini.*planning/);
  assert.match(h.workflowActivity({ ...planning, mode: "rehearsal" }), /rehearsal plan/);
});

test("parallel workers, then QA, have distinct active stages", () => {
  const h = harness();
  const parallel = run({ agents: [
    { role: "backend", status: "running" }, { role: "frontend", status: "running" },
    { role: "qa", status: "pending" },
  ] });
  const stages = h.workflowStages(parallel);
  assert.equal(stages.manager, "completed");
  assert.equal(stages.backend, "running");
  assert.equal(stages.frontend, "running");
  assert.equal(stages.qa, "idle");
  assert.equal(stages.validator, "idle");
  assert.match(h.workflowActivity(parallel), /Backend Engineer \+ Frontend Engineer working/);
  const qa = run({ agents: roles.map((role) => ({ role, status: role === "qa" ? "running" : "completed" })) });
  assert.equal(h.workflowActivity(qa), "QA Engineer working");
  assert.equal(h.workflowActivity({ ...qa, mode: "rehearsal" }), "QA Engineer loading fixtures");
});

test("architecture placement never substitutes today's pool for a recorded worker", () => {
  const h = harness();
  const config = { services: { backend: { status: "ready", machine_family: "n4a" } } };
  const past = run({ status: "passed", agents: [{ role: "backend", status: "completed", machine_family: "c4a" }] });
  assert.equal(h.architecturePlacement("backend", past, config).family, "C4A");
  assert.equal(h.architecturePlacement("frontend", past, config).family, undefined);
  assert.equal(h.architecturePlacement("backend", null, config).family, "N4A");
  assert.equal(h.architecturePlacement("backend", run(), config).family, "N4A");
  config.services.backend.status = "error";
  assert.equal(h.architecturePlacement("backend", null, config).family, undefined);
  config.services.backend = { status: "ready", machine_family: "   " };
  assert.equal(h.architecturePlacement("backend", null, config).family, undefined);
  for (const family of ["unknown", "Not reported", "unavailable", "N/A"]) {
    config.services.backend.machine_family = family;
    assert.equal(h.architecturePlacement("backend", null, config).family, undefined);
    assert.equal(h.architecturePlacement("backend", { ...past, agents: [{ role: "backend", machine_family: family }] }, config).family, undefined);
  }
  assert.equal(h.architecturePlacement("backend", { ...past, mode: "rehearsal" }, config).family, undefined);
});

test("architecture cannot infer other machine families or unlock an unverified shop", () => {
  const h = harness();
  const config = { booth: { manager_node_vcpus: 2, worker_node_vcpus: 16 }, services: { backend: { status: "ready", machine_family: "c4a" } } };
  for (const role of ["coordinator", "validator", "result"]) {
    assert.equal(h.architecturePlacement(role, null, config).family, undefined);
    assert.doesNotMatch(h.architecturePlacement(role, null, config).label, /C4A|N4A/i);
  }
  assert.match(h.architecturePlacement("cluster", null, config).label, /C4A.*partial/);
  assert.equal(h.architecturePlacement("result", run({ status: "validating", preview_url: "/preview" }), config).label, "After checks pass");
  assert.equal(h.architecturePlacement("result", run({ status: "passed", preview_url: "/preview" }), config).label, "Verified preview");
});

test("architecture paths and placement badges follow execution independently of selection", () => {
  const h = harness();
  h.state.connected = true;
  h.state.config = { live_available: true, placement: { qa: { source: "kubernetes_node", machine_family: "c4a", architecture: "arm64" } } };
  h.state.run = run({ agents: roles.map((role) => ({
    role, status: role === "qa" ? "pending" : "running", machine_family: role === "qa" ? null : "c4a",
  })) });
  h.state.booth.inspect = "result";
  h.renderBoothOverview();
  const pathState = Object.fromEntries(h.flows.map((flow) => [flow.dataset.flowStage, flow.dataset.execution]));
  assert.equal(pathState.backend, "running");
  assert.equal(pathState.frontend, "running");
  assert.equal(pathState.qa, "idle");
  assert.equal(pathState.result, "idle");
  assert.equal(h.placements.find((badge) => badge.dataset.placement === "qa").textContent, "C4A · Arm");
  assert.equal(h.document.body.dataset.workflowActive, "true");
  h.state.connected = false;
  h.renderBoothOverview();
  assert.equal(h.document.body.dataset.workflowActive, "false");
  h.state.run.status = "passed";
  h.state.connected = true;
  h.renderBoothOverview();
  assert.equal(h.document.body.dataset.workflowActive, "false");
});

test("repair workers take precedence over the run's validating status", () => {
  const h = harness();
  const repairing = run({
    status: "validating", tests: { total: 20, passed: 19, failed: 1 },
    agents: roles.map((role) => ({ role, status: role === "backend" ? "running" : "completed" })),
  });
  const stages = h.workflowStages(repairing);
  assert.equal(stages.backend, "running");
  assert.equal(stages.validator, "failed");
  assert.equal(stages.manager, "completed");
  assert.equal(h.workflowActivity(repairing), "Backend Engineer working");
});

test("independent validation and the final summary are separate phases", () => {
  const h = harness();
  const validation = run({ status: "validating", agents: roles.map((role) => ({ role, status: "completed" })) });
  assert.equal(h.workflowStages(validation).validator, "running");
  assert.match(h.workflowActivity(validation), /Independent validator/);
  const summary = { ...validation, tests: { total: 20, passed: 20, failed: 0 } };
  assert.equal(h.workflowStages(summary).validator, "completed");
  assert.equal(h.workflowStages(summary).manager, "running");
  assert.equal(h.workflowStages(summary).result, "idle");
  assert.match(h.workflowActivity(summary), /Gemini.*summarizing/);
  assert.match(h.workflowActivity({ ...summary, mode: "rehearsal" }), /finalizing the rehearsal/);
  assert.doesNotMatch(h.workflowActivity({ ...summary, mode: "rehearsal" }), /Gemini/);
});

test("a validator connection failure is attributed only with recorded validation-start evidence", () => {
  const h = harness();
  const failed = run({
    status: "failed", agents: roles.map((role) => ({ role, status: "completed" })),
    tests: { total: 0, passed: 0, failed: 0 },
    events: [{ type: "validation_started" }, { type: "failed" }],
  });
  assert.equal(h.workflowStages(failed).validator, "failed");
  assert.equal(h.workflowStages(failed).result, "idle");
  assert.equal(h.workflowStages({ ...failed, events: [{ type: "worker_completed" }, { type: "failed" }] }).validator, "idle");
});

test("passed runs stop activity and failures never expose a successful result", () => {
  const h = harness();
  const passed = run({ status: "passed", tests: { total: 20, passed: 20, failed: 0 } });
  assert.equal(h.workflowStages(passed).result, "completed");
  assert.equal(h.workflowStages(passed).coordinator, "completed");
  const failed = run({ status: "failed", plan: [], agents: [{ role: "backend", status: "running" }] });
  const stages = h.workflowStages(failed);
  assert.equal(stages.manager, "failed");
  assert.equal(stages.backend, "failed");
  assert.equal(stages.result, "idle");
  assert.ok(!Object.values(stages).includes("running"));
  assert.match(h.workflowActivity(failed), /stopped/);
});

test("CPU percentages use the measured sandbox limit, never threads or node size", () => {
  const h = harness();
  assert.deepEqual(plain(h.cpuUsage({ cpu_cores: 3, cpu_limit_cores: 12, inference_threads: 4, node_vcpus: 16 })),
    { cores: 3, limit: 12, percent: 25 });
  assert.deepEqual(plain(h.cpuUsage({ cpu_cores: 0, cpu_limit_cores: 12 })), { cores: 0, limit: 12, percent: 0 });
  assert.equal(h.cpuUsage({ cpu_cores: 15, cpu_limit_cores: 12 }).percent, 125);
});

test("missing or invalid CPU values stay unavailable instead of becoming zero", () => {
  const h = harness();
  for (const sample of [undefined, {}, { cpu_cores: null }, { cpu_cores: "3", cpu_limit_cores: 12 }]) {
    assert.equal(h.cpuUsage(sample).cores, null);
    assert.equal(h.cpuUsage(sample).percent, null);
  }
  for (const limit of [undefined, null, 0, -1, NaN, Infinity, "12"]) {
    assert.deepEqual(plain(h.cpuUsage({ cpu_cores: 3, cpu_limit_cores: limit })), { cores: 3, limit: null, percent: null });
  }
  for (const cores of [-1, NaN, Infinity]) assert.equal(h.cpuUsage({ cpu_cores: cores, cpu_limit_cores: 12 }).percent, null);
});

test("a zero CPU trace stays at zero on the shared percentage scale", () => {
  const h = harness();
  const chart = h.cpuSparkline([
    { cpu_cores: 0, cpu_limit_cores: 6.5, timestamp: "2026-10-15T10:00:00Z" },
    { cpu_cores: 0, cpu_limit_cores: 6.5, timestamp: "2026-10-15T10:00:30Z" },
  ], "backend");
  assert.match(chart.getAttribute("aria-label"), /shared 0–100%.*highest observed 0%/);
  assert.equal(chart.querySelector("polyline").getAttribute("points"), "4.00,52.00 236.00,52.00");
});

test("CPU traces compare percentages, with each sample's own measured Pod limit", () => {
  const h = harness();
  const series = (cores, limit) => [0, 30].map((second) => ({ cpu_cores: cores, cpu_limit_cores: limit, timestamp: `2026-10-20T10:00:${String(second).padStart(2, "0")}Z` }));
  const manager = h.cpuSparkline(series(0.5, 1), "manager");
  const worker = h.cpuSparkline(series(3.25, 6.5), "backend");
  assert.equal(manager.querySelector("polyline").getAttribute("points"), "4.00,29.00 236.00,29.00");
  assert.equal(worker.querySelector("polyline").getAttribute("points"), manager.querySelector("polyline").getAttribute("points"));
  assert.match(manager.getAttribute("aria-label"), /Arm coordinator/);
  const changingLimit = series(0.5, 1); changingLimit[1].cpu_limit_cores = 2;
  assert.equal(h.cpuSparkline(changingLimit, "manager").querySelector("polyline").getAttribute("points"), "4.00,29.00 236.00,40.50");
});

test("missing CPU, missing limits and invalid timestamps create gaps, never zeroes or joins", () => {
  const h = harness();
  const sample = (second, extra = {}) => ({ cpu_cores: 0.5, cpu_limit_cores: 1, timestamp: `2026-10-20T10:00:${String(second).padStart(2, "0")}Z`, ...extra });
  for (const gap of [{ cpu_cores: null }, { cpu_limit_cores: null }, { timestamp: "invalid" }]) {
    const chart = h.cpuSparkline([sample(0), sample(10, gap), sample(20)], "manager");
    assert.equal(chart.children.filter((child) => child.tag === "polyline").length, 0);
    const dots = chart.children.filter((child) => child.tag === "circle");
    assert.equal(dots.length, 2);
    assert.ok(dots.every((dot) => dot.getAttribute("cy") === "29.00"));
  }
  const missing = h.cpuSparkline([sample(0, { cpu_limit_cores: null }), sample(20, { cpu_cores: null })], "manager");
  assert.equal(missing.tag, "p");
  assert.match(missing.textContent, /Awaiting measured CPU and Pod limits/);
  assert.equal(missing.children.length, 0);
});

test("manager placement requires observed node evidence and never rewrites history", () => {
  const h = harness();
  const config = { placement: { manager: { source: "kubernetes_node", machine_family: "c4a", machine_type: "c4a-standard-2", architecture: "arm64", observed_at: "2026-10-20T10:00:00Z" } } };
  for (const selection of [null, run()]) {
    const placement = h.architecturePlacement("coordinator", selection, config);
    assert.equal(placement.label, "C4A · Arm");
    assert.match(placement.source, /Current observed.*c4a-standard-2.*2026-10-20/);
  }
  const historical = run({ status: "passed" });
  assert.equal(h.architecturePlacement("coordinator", historical, config).family, undefined);
  const metrics = { samples: [{ role: "manager", placement_source: "kubernetes_node", machine_family: "n4a", machine_type: "n4a-standard-2", architecture: "arm64", placement_observed_at: "2026-10-15T10:00:00Z" }] };
  const past = h.architecturePlacement("coordinator", historical, config, metrics);
  assert.equal(past.label, "N4A · Arm");
  assert.match(past.source, /This run's recorded.*n4a-standard-2.*2026-10-15/);
  metrics.samples[0].placement_source = "unavailable";
  assert.equal(h.architecturePlacement("coordinator", historical, config, metrics).family, undefined);
  for (const source of [undefined, "unavailable", "configured"]) {
    config.placement.manager.source = source;
    assert.equal(h.architecturePlacement("coordinator", null, config).family, undefined);
  }
  config.placement.manager.source = "kubernetes_node";
  for (const family of [null, "unknown", "Not reported", " "]) {
    config.placement.manager.machine_family = family;
    assert.equal(h.architecturePlacement("coordinator", null, config).family, undefined);
  }
});

test("topology counts observed nodes only when all four workload placements are available", () => {
  const h = harness();
  const config = { placement: Object.fromEntries(["manager", ...roles].map((role) => [role, {
    source: "kubernetes_node", node: role === "manager" ? "manager-node" : "worker-node", architecture: "arm64",
  }])) };
  assert.equal(h.workloadTopology(null, config), "Four workloads share 2 Arm VMs. Pod CPU, not whole-machine usage.");
  config.placement.qa.node = "additional-worker-node";
  assert.match(h.workloadTopology(null, config), /share 3 Arm VMs/);
  config.placement.qa.architecture = null;
  assert.match(h.workloadTopology(null, config), /share 3 VMs/);
  config.placement.qa.source = "unavailable";
  assert.equal(h.workloadTopology(null, config), "Workload usage, not whole-machine usage.");
});

test("a recorded topology uses only that run's samples, never current node configuration", () => {
  const h = harness();
  const current = { placement: Object.fromEntries(["manager", ...roles].map((role) => [role, { source: "kubernetes_node", node: "one-current-node", architecture: "arm64" }])) };
  const historical = run({ status: "passed" });
  const samples = roles.map((role) => ({ role, placement_source: "kubernetes_node", node: "saved-worker", architecture: "arm64" }));
  assert.equal(h.workloadTopology(historical, current, { samples }), "Workload usage, not whole-machine usage.");
  samples.push({ role: "manager", placement_source: "kubernetes_node", node: "saved-manager", architecture: "arm64" });
  assert.match(h.workloadTopology(historical, current, { samples }), /share 2 Arm VMs/);
  assert.equal(h.workloadTopology({ ...historical, mode: "rehearsal" }, current, { samples }), "Workload usage, not whole-machine usage.");
});

function currentPlacementConfig() {
  return { live_available: true, placement: Object.fromEntries(["manager", ...roles, "validator"].map((role) => {
    const group = role === "manager" ? "manager" : role === "validator" ? "validator" : "engineers";
    return [role, { source: "kubernetes_node", node: `${group}-node`, node_uid: `${group}-uid`,
      machine_family: group === "engineers" ? "c4a" : "n4a", architecture: "arm64",
      machine_type: group === "engineers" ? "c4a-standard-16" : group === "manager" ? "n4a-standard-2" : "n4a-standard-8",
      observed_at: "2026-10-20T10:00:00Z" }];
  })) };
}

test("observed machine boundaries group three engineers and link preview to the manager VM", () => {
  const h = harness();
  h.state.connected = true;
  h.state.config = currentPlacementConfig();
  h.renderBoothOverview();
  assert.equal(h.get("booth-engineer-vm").textContent, "VM 2 · C4A · 16 vCPUs");
  assert.equal(h.get("booth-engineer-vm-detail").textContent, "Three separate sandboxes share this machine");
  assert.equal(h.get("booth-engineer-vm-group").dataset.shared, "true");
  assert.equal(h.get("booth-vm-summary").textContent, "3 workload VMs · 4 CPU cards → 2 VMs");
  assert.match(h.get("booth-cpu-workload-map").textContent, /Four workload cards · across 2 VMs/);
  const labels = Object.fromEntries(h.vmHosts.map((label) => [label.dataset.vmHost, label]));
  assert.equal(labels.manager.textContent, "VM 1 · N4A · 2 vCPUs");
  assert.equal(labels.validator.textContent, "VM 3 · N4A · 8 vCPUs");
  assert.equal(labels.result.textContent, "VM 1 · same as manager");
  assert.equal(labels.qa.hidden, true);
  const cards = h.get("booth-overview-metrics").children;
  assert.match(cards[0].querySelector(".v3-metric-placement").textContent, /N4A.*VM 1/);
  for (const card of cards.slice(1)) assert.match(card.querySelector(".v3-metric-placement").textContent, /C4A.*VM 2/);
});

test("moving QA to a different observed VM removes the shared-machine assertion", () => {
  const h = harness();
  h.state.config = currentPlacementConfig();
  h.state.currentMetrics = { samples: [{ role: "qa", placement_source: "kubernetes_node", node: "qa-node", node_uid: "qa-uid",
    machine_family: "n4a", machine_type: "n4a-standard-4", architecture: "arm64", placement_observed_at: "2026-10-20T10:01:00Z" }] };
  h.renderBoothOverview();
  assert.equal(h.get("booth-engineer-vm-group").dataset.shared, "false");
  assert.equal(h.get("booth-engineer-vm").textContent, "2 VMs host the engineers");
  assert.equal(h.get("booth-vm-summary").textContent, "4 workload VMs · 4 CPU cards → 3 VMs");
  const qa = h.vmHosts.find((label) => label.dataset.vmHost === "qa");
  assert.equal(qa.hidden, false);
  assert.equal(qa.textContent, "VM 3 · N4A · 4 vCPUs");
});

test("incomplete, unverified or conflicting Node evidence never invents shared VMs or capacity", () => {
  const h = harness();
  for (const change of [
    (config) => { config.placement.qa.source = "unavailable"; },
    (config) => { delete config.placement.qa.node; },
    (config) => { config.placement.qa.node_uid = "different-node-generation"; },
    (config) => { config.placement.qa.machine_type = "c4a-standard-32"; },
  ]) {
    const config = currentPlacementConfig(); change(config);
    const topology = h.currentVmTopology(config, null);
    assert.equal(topology.sharedWorkers, false);
    assert.equal(topology.count, null);
    assert.equal(topology.cpuCount, null);
    assert.equal(topology.workerLabel, "Engineer VM placement incomplete");
  }
  const config = currentPlacementConfig();
  config.booth = { worker_node_vcpus: 128 };
  for (const role of roles) config.placement[role].machine_type = "unrecognized-machine-type";
  const topology = h.currentVmTopology(config, null);
  assert.equal(topology.count, 3);
  assert.equal(topology.workerLabel, "VM 2 · C4A · capacity unreported");
  assert.doesNotMatch(topology.workerLabel, /128|16/);
});

test("newer config placement wins over retained older telemetry after a worker moves", () => {
  const h = harness();
  h.state.config = currentPlacementConfig();
  const old = { ...h.state.config.placement.qa };
  h.state.config.placement.qa = { ...old, node: "qa-new-node", node_uid: "qa-new-uid", machine_family: "n4a", machine_type: "n4a-standard-4", observed_at: "2026-10-20T10:02:00Z" };
  h.state.currentMetrics = { samples: [{ ...old, role: "qa", placement_source: "kubernetes_node", placement_observed_at: old.observed_at }] };
  h.state.currentMetricsError = "Telemetry request failed";
  h.renderBoothOverview();
  assert.equal(h.currentArchitecturePlacement("qa").family, "N4A");
  assert.equal(h.get("booth-engineer-vm-group").dataset.shared, "false");
  assert.equal(h.get("booth-vm-summary").textContent, "4 workload VMs · 4 CPU cards → 3 VMs");
  h.state.currentMetrics.samples[0].placement_observed_at = "2026-10-20T10:02:00Z";
  assert.equal(h.currentArchitecturePlacement("qa").family, undefined, "Conflicting equal-time placements must be unresolved");
  delete h.state.currentMetrics.samples[0].placement_observed_at;
  delete h.state.currentMetrics.samples[0].observed_at;
  assert.equal(h.currentArchitecturePlacement("qa").family, undefined, "An undated conflicting observation cannot claim to be newest");
});

test("runtime chips use reported models, threads, ready runtimes and observed families", () => {
  const h = harness();
  h.state.config = { ...currentPlacementConfig(), models: { planner: "gemini-3.8-flash", worker: "gemma-4-E4B-it-Q4_0" },
    booth: { inference_threads_per_worker: 4 }, deployment: { execution_environment: "GKE Agent Sandbox on Google Axion" },
    services: { ...Object.fromEntries(roles.map((role) => [role, { status: "ready", model: "gemma-4-E4B-it-Q4_0", runtime: "llama.cpp", inference_location: "loopback_sidecar" }])),
      validator: { status: "ready", runtime: "gvisor", isolated_execution: true } } };
  const chips = plain(h.runtimeConfiguration());
  assert.deepEqual(chips.map((chip) => chip.text), ["Hosted · Gemini 3.8 Flash", "Local · Gemma 4 E4B · Q4_0", "llama.cpp · CPU inference", "4 inference threads / worker", "GKE Agent Sandbox · gVisor", "Observed nodes · C4A + N4A"]);
  assert.match(chips[0].title, /gemini-3\.8-flash/);
  assert.match(chips[1].title, /gemma-4-E4B-it-Q4_0/);
  assert.match(chips[3].title, /not measured CPU usage/);
  h.state.config.services.qa.status = "unavailable";
  assert.ok(!h.runtimeConfiguration().some((chip) => /CPU inference/.test(chip.text)));
  h.state.config = { models: { planner: "Not configured", worker: "Not verified" }, booth: { inference_threads_per_worker: 0 } };
  assert.equal(h.runtimeConfiguration().length, 1);
  assert.equal(h.runtimeConfiguration()[0].text, "Waiting for runtime configuration");
});

test("the configuration strip preserves animation nodes and pause state across polling renders", () => {
  const h = harness();
  h.state.config = { models: { planner: "gemini-3.8-flash", worker: "gemma-4-E4B-it-Q4_0" } };
  h.renderRuntimeConfiguration();
  const track = h.get("booth-runtime-track"), original = track.children[0];
  assert.equal(track.children.length, 2);
  assert.equal(original.getAttribute("aria-hidden"), null);
  assert.equal(track.children[1].getAttribute("aria-hidden"), "true");
  h.toggleRuntimeConfiguration();
  h.renderBoothOverview();
  assert.equal(track.children[0], original, "Routine polling must not restart the moving track");
  assert.equal(h.get("booth-runtime-strip").dataset.paused, "true");
  assert.equal(h.get("booth-runtime-pause").getAttribute("aria-pressed"), "true");
  assert.equal(h.get("booth-runtime-pause").getAttribute("aria-label"), "Resume configuration strip");
  h.toggleRuntimeConfiguration();
  assert.equal(h.get("booth-runtime-pause").textContent, "Pause");
});

test("four CPU cards use actual per-Pod limits and preserve source, window and timestamps", () => {
  const h = harness();
  h.state.connected = true;
  h.state.run = run({ status: "passed" });
  h.state.currentMetrics = { scope: "current", samples: ["manager", ...roles].map((role) => ({
    role, cpu_cores: role === "manager" ? 0.25 : 3.25, cpu_limit_cores: role === "manager" ? 1 : 6.5,
    cpu_source: "kubelet_summary_pod_cpu", window: "30s", cpu_timestamp: "2026-10-20T10:00:00Z",
    memory_bytes: 1024 ** 2, memory_timestamp: "2026-10-20T10:00:02Z",
  })) };
  h.renderBoothOverview();
  const cards = h.get("booth-overview-metrics").children;
  assert.deepEqual(Array.from(cards, (card) => card.dataset.role), ["manager", ...roles]);
  assert.equal(cards[0].querySelector("strong").textContent, "25%");
  assert.equal(cards[1].querySelector("strong").textContent, "50%");
  assert.equal(cards[0].querySelector(".v3-metric-percent").textContent, "0.25 / 1 cores");
  assert.equal(cards[1].querySelector(".v3-metric-percent").textContent, "3.25 / 6.5 cores");
  assert.match(cards[0].title, /Arm coordinator Pod only; hosted Gemini inference runs externally/);
  for (const card of cards) {
    assert.match(card.title, /kubelet_summary_pod_cpu · 30s average/);
    assert.match(card.title, /CPU timestamp: 2026-10-20T10:00:00Z/);
    assert.match(card.title, /Memory: 1 MiB · timestamp: 2026-10-20T10:00:02Z/);
    assert.equal(card.querySelector("details").open, false);
  }
  cards[0].querySelector("details").open = true;
  h.renderBoothOverview();
  assert.equal(h.get("booth-overview-metrics").children[0].querySelector("details").open, true);
  assert.equal(h.get("booth-overview-metrics").children[1].querySelector("details").open, false);
});

test("recorded metrics cannot fill current CPU gaps or rewrite saved placement", () => {
  const h = harness();
  h.state.connected = true;
  h.state.config = { live_available: true, placement: { manager: { source: "kubernetes_node", machine_family: "c4a", architecture: "arm64" } } };
  h.state.run = run({ status: "passed" });
  h.state.metrics = { samples: [{ role: "backend", cpu_cores: 3.25, cpu_limit_cores: 6.5 }] };
  h.renderBoothOverview();
  let manager = h.get("booth-overview-metrics").children[0];
  assert.equal(manager.querySelector("strong").textContent, "—");
  assert.match(manager.textContent, /C4A · Arm/);
  assert.equal(h.architecturePlacement("coordinator").family, undefined);
  h.state.metrics.samples.push({ role: "manager", cpu_cores: 0.5, cpu_limit_cores: null, timestamp: "2026-10-20T10:00:00Z" });
  h.renderBoothOverview();
  manager = h.get("booth-overview-metrics").children[0];
  assert.equal(manager.querySelector("strong").textContent, "—");
  assert.equal(manager.querySelector(".v3-metric-percent").textContent, "CPU not measured");
  assert.equal(manager.children.filter((child) => child.tag === "svg").length, 0);
  h.state.metrics.samples[1].cpu_limit_cores = 1;
  h.renderBoothOverview();
  manager = h.get("booth-overview-metrics").children[0];
  assert.equal(manager.querySelector("strong").textContent, "—");
  assert.match(manager.textContent, /C4A · Arm/);
  assert.equal(h.state.metrics.samples[1].cpu_cores, 0.5);
});

test("coordinator selection opens details without changing hosted Gemini or opening during render", () => {
  const h = harness();
  h.state.config = { live_available: true, placement: { manager: { source: "kubernetes_node", machine_family: "c4a", architecture: "arm64" } } };
  h.state.booth.inspect = "manager";
  h.renderBoothInspector();
  assert.match(h.get("booth-inspector-copy").textContent, /Gemini inference runs externally/);
  assert.ok(!h.get("booth-inspector-disclosure").open);
  h.selectBoothNode("coordinator");
  assert.equal(h.get("booth-inspector-disclosure").open, true);
  assert.match(h.get("booth-inspector-facts").textContent, /C4A · Arm/);
  assert.match(h.get("booth-inspector-facts").textContent, /Hosted Gemini is external/);
  h.get("booth-inspector-disclosure").open = false;
  h.renderBoothOverview(); h.renderBoothInspector();
  assert.equal(h.get("booth-inspector-disclosure").open, false);
});

test("the updated-shop link exists only for a passed run with a valid identifier", () => {
  const h = harness();
  for (const status of ["queued", "planning", "running", "validating", "failed"]) {
    assert.equal(h.updatedShopPath(run({ status, preview_url: "/preview/run-123/" })), null);
  }
  assert.equal(h.updatedShopPath(null), null);
  assert.equal(h.updatedShopPath(run({ status: "passed" })), null);
  for (const id of [undefined, null, 123, "", "../escape", "x?run=other", "a".repeat(101)]) {
    assert.equal(h.updatedShopPath(run({ id, status: "passed", preview_url: "/preview/" })), null);
  }
  // A model-supplied preview URL is only a readiness signal, never the destination.
  assert.equal(h.updatedShopPath(run({ status: "passed", preview_url: "https://untrusted.invalid/" })),
    "/?view=booth&panel=result&run=run-123");
});

test("ordinary entry never auto-selects a historical run or a configured archive favorite", async () => {
  const h = harness("?autoplay=1");
  h.state.config = { live_available: true, booth: { preferred_run_id: "old-result" } };
  h.state.runs = [run({ id: "old-result", status: "passed" })];
  h.context.selected = [];
  h.replace("selectRun = async (id) => selected.push(id); renderBooth = () => {}; startRun = () => { throw Error('Unexpected run submission'); };");
  await h.selectBoothRun();
  assert.deepEqual(h.context.selected, []);
  assert.equal(h.state.run, null);
});

test("a normal booth refresh clears a completed run query and resumes only active work", async () => {
  for (const active of [false, true]) {
    const h = harness("?view=booth&run=old-result&autoplay=1");
    h.state.runs = [run({ id: "old-result", status: "passed" }), ...(active ? [run({ id: "active-result" })] : [])];
    h.context.selected = [];
    h.replace("selectRun = async (id) => selected.push(id); renderBooth = () => {}; startRun = () => { throw Error('Unexpected submission'); };");
    await h.selectBoothRun();
    assert.deepEqual(h.context.selected, active ? ["active-result"] : []);
    assert.equal(h.context.window.location.search.includes("old-result"), false);
    assert.equal(h.state.run, null);
  }
});

test("initialization starts empty and never replaces a draft with configured examples", async () => {
  const h = harness(), config = deferred();
  h.get("prompt").value = "A browser-restored old prompt";
  h.context.bootstrapApi = async (url) => url === "/api/config" ? config.promise : [];
  h.replace("api = bootstrapApi; setBoothView = () => {}; renderAgents = renderExamples = renderProducts = resetCart = renderHistory = renderRun = renderBooth = () => {};");
  const boot = h.initialize();
  assert.equal(h.get("prompt").value, "");
  h.get("prompt").value = "My freshly pasted custom request";
  config.resolve({ live_available: true, supported_examples: ["A static sample request"] });
  await boot;
  assert.equal(h.get("prompt").value, "My freshly pasted custom request");
  assert.equal(h.state.run, null);
});

test("history selection and rendering never overwrite the draft or cycle the submitted request", async () => {
  const h = harness();
  h.get("prompt").value = "My next request draft";
  h.state.runs = [run({ status: "passed", prompt: "The accepted business request", preview_url: "/preview/run-123/" })];
  h.replace("refreshRun = refreshArtifacts = async () => {}; renderRun = renderMetrics = () => {};");
  await h.selectRun("run-123");
  assert.equal(h.get("prompt").value, "My next request draft");
  h.renderBooth();
  assert.equal(h.get("booth-request-summary").textContent, "The accepted business request");
  h.state.booth.enabled = true;
  h.playBoothTour();
  assert.equal(h.state.booth.playing, false);
  assert.equal(h.state.booth.timer, null);
  assert.equal(h.get("booth-request-summary").textContent, "The accepted business request");
  h.state.run = null; h.renderBooth();
  assert.equal(h.get("booth-request-summary").textContent, "No request submitted");
});

test("booth standby follows service readiness and its run detail never advertises a duration", () => {
  const h = harness();
  h.state.config = { live_available: true, services: Object.fromEntries(roles.map((role) => [role, { status: "ready" }])) };
  h.state.connected = true; h.renderBooth();
  assert.equal(h.get("booth-overview-status").textContent, "Ready");
  assert.equal(h.get("booth-coordinator-status").textContent, "Ready");
  assert.ok(h.agentStatuses.every((status) => status.textContent === "Ready"));
  h.state.connected = false; h.renderBooth();
  assert.equal(h.get("booth-overview-status").textContent, "Connection required");
  assert.equal(h.get("booth-coordinator-status").textContent, "Connection required");
  assert.ok(h.agentStatuses.every((status) => status.textContent === "Unavailable"));
  h.state.connected = true; h.state.config.live_available = false; h.renderBooth();
  assert.equal(h.get("booth-overview-status").textContent, "Connection required");
  for (const status of ["running", "passed", "failed"]) {
    h.state.run = run({ status, elapsed_seconds: 180 }); h.renderBooth();
    assert.doesNotMatch(h.get("booth-run-detail").textContent, /elapsed|duration|180|3m/);
  }
  assert.match(h.get("booth-run-detail").textContent, /request stopped/);
});

test("current metrics poll in idle and completed states without changing saved evidence or creating work", async () => {
  for (const selected of [null, run({ status: "passed" })]) {
    const h = harness();
    h.state.connected = true; h.state.run = selected;
    const saved = { source: "kubernetes", samples: [{ role: "backend", cpu_cores: 3.835, cpu_limit_cores: 6.5, timestamp: "2026-10-15T10:00:00Z" }] };
    h.state.metrics = saved;
    h.context.requests = [];
    h.context.currentResponse = { scope: "current", source: "kubernetes", collected_at: "2026-10-20T10:00:04Z", samples: [{ role: "backend", pod_uid: "current-pod", cpu_cores: 0.065, cpu_limit_cores: 6.5, cpu_timestamp: "2026-10-20T10:00:00Z", cpu_fresh: true }] };
    h.replace("api = async (path, options, timeout) => { requests.push({path, options, timeout}); return currentResponse; }; renderRun = () => {};");
    await h.refreshCurrentMetrics();
    assert.deepEqual(plain(h.context.requests), [{ path: "/api/metrics/current", options: {}, timeout: 15000 }]);
    assert.equal(h.state.metrics, saved);
    assert.equal(h.state.run, selected);
    assert.equal(h.timers[h.state.currentMetricsTimer - 1].delay, 5000);
    h.renderBoothOverview();
    const backend = h.get("booth-overview-metrics").children.find((card) => card.dataset.role === "backend");
    assert.equal(backend.querySelector("strong").textContent, "1%");
    assert.doesNotMatch(backend.textContent, /59%/);
    assert.equal(saved.samples[0].cpu_cores, 3.835);
  }
});

test("current polling is single-flight and failures hide old percentages until fresh evidence returns", async () => {
  const h = harness(), pending = deferred();
  h.state.connected = true;
  h.acceptCurrentMetrics({ scope: "current", source: "kubernetes", samples: [{ role: "backend", cpu_cores: 3.835, cpu_limit_cores: 6.5, cpu_timestamp: "2026-10-20T10:00:00Z" }] });
  h.context.requests = [];
  h.context.currentApi = (path) => { h.context.requests.push(path); return pending.promise; };
  h.replace("api = currentApi; renderRun = () => {};");
  const first = h.refreshCurrentMetrics();
  await h.refreshCurrentMetrics();
  assert.equal(h.context.requests.length, 1);
  pending.reject(new Error("Metrics service unavailable")); await first;
  h.renderBoothOverview();
  const card = h.get("booth-overview-metrics").children.find((item) => item.dataset.role === "backend");
  assert.equal(card.querySelector("strong").textContent, "—");
  assert.ok(card.children.some((item) => item.className === "v3-metric-stale"));
  assert.equal(h.get("booth-cpu-observation").textContent, "Current CPU unavailable");
  assert.match(h.get("booth-cpu-observation").title, /Metrics service unavailable/);
  assert.equal(h.timers[h.state.currentMetricsTimer - 1].delay, 5000);
  h.acceptCurrentMetrics({ scope: "current", source: "unavailable", samples: [], note: "No fresh measurements" });
  h.renderBoothOverview();
  assert.equal(h.get("booth-overview-metrics").children.find((item) => item.dataset.role === "backend").querySelector("strong").textContent, "—");
  assert.equal(h.get("booth-cpu-observation").title, "No fresh measurements");
});

test("cache refresh time cannot make an old CPU sample fresh and archive/offline states do not poll", async () => {
  const h = harness();
  h.state.connected = true;
  const old = { role: "backend", cpu_cores: 3.835, cpu_limit_cores: 6.5, cpu_timestamp: "2026-10-20T09:58:00Z", cpu_fresh: true };
  h.acceptCurrentMetrics({ scope: "current", source: "kubernetes", samples: [old], collected_at: "2026-10-20T10:00:05Z", cache_age_seconds: 0 });
  assert.equal(h.currentCpuState(old).stale, true);
  assert.equal(h.currentCpuState(old).usage.percent, null);
  const actualZero = { ...old, cpu_timestamp: "2026-10-20T10:00:00Z", cpu_cores: 0 };
  assert.equal(h.currentCpuState(actualZero).usage.percent, 0);
  assert.equal(h.currentCpuState({ ...actualZero, cpu_fresh: false }).usage.percent, null);
  for (const setup of [() => { h.state.connected = false; }, () => { h.state.connected = true; h.state.config = { preview_read_only: true }; }]) {
    setup(); h.scheduleCurrentMetrics(); await h.refreshCurrentMetrics();
    assert.equal(h.state.currentMetricsTimer, null);
  }
});

test("session traces deduplicate cached samples, stay bounded and reset when a Pod changes", () => {
  const h = harness();
  const sample = { role: "manager", pod_uid: "first-pod", cpu_cores: 0.1, cpu_limit_cores: 1, cpu_timestamp: "2026-10-20T10:00:00Z" };
  const snapshot = { scope: "current", source: "kubernetes", samples: [sample], collected_at: "2026-10-20T10:00:02Z" };
  h.acceptCurrentMetrics(snapshot);
  h.acceptCurrentMetrics({ ...snapshot, cache_age_seconds: 4, collected_at: "2026-10-20T10:00:06Z" });
  assert.equal(h.state.currentMetricHistory.filter((item) => item.role === "manager").length, 1);
  for (let i = 0; i < 75; i++) h.acceptCurrentMetrics({ ...snapshot, samples: [{ ...sample, cpu_timestamp: new Date(Date.parse(sample.cpu_timestamp) + i * 5000).toISOString() }] });
  assert.equal(h.state.currentMetricHistory.filter((item) => item.role === "manager").length, 60);
  h.acceptCurrentMetrics({ ...snapshot, samples: [{ ...sample, pod_uid: "replacement-pod" }] });
  const history = h.state.currentMetricHistory.filter((item) => item.role === "manager");
  assert.equal(history.length, 1);
  assert.equal(history[0].pod_uid, "replacement-pod");
  assert.throws(() => h.acceptCurrentMetrics({ scope: "run", samples: [sample] }), /current workload telemetry/);
});

test("current placement distinguishes N4A manager/validator from C4A specialists and preserves old records", () => {
  const h = harness();
  h.state.config = { placement: Object.fromEntries(["manager", "validator", ...roles].map((role) => [role, {
    source: "kubernetes_node", machine_family: ["manager", "validator"].includes(role) ? "n4a" : "c4a", architecture: "arm64",
    machine_type: role === "manager" ? "n4a-standard-2" : role === "validator" ? "n4a-standard-8" : "c4a-standard-16",
    observed_at: "2026-10-20T10:00:00Z",
  }])) };
  h.state.run = run({ status: "passed", agents: roles.map((role) => ({ role, machine_family: "c4a" })) });
  assert.equal(h.currentArchitecturePlacement("coordinator").label, "N4A · Arm");
  assert.equal(h.currentArchitecturePlacement("validator").label, "N4A · Arm");
  assert.match(h.currentArchitecturePlacement("validator").source, /n4a-standard-8/);
  assert.equal(h.currentArchitecturePlacement("result").label, "N4A · Arm");
  assert.match(h.currentArchitecturePlacement("result").source, /Shop HTTP service in coordinator Pod.*quotes execute in the independent validator/);
  assert.equal(h.currentArchitecturePlacement("backend").family, "C4A");
  assert.equal(h.architecturePlacement("coordinator").family, undefined);
  h.state.currentMetrics = { samples: [{ role: "backend", machine_family: "n4a", architecture: "arm64", placement_source: "kubernetes_node", placement_observed_at: "2026-10-20T10:01:00Z" }] };
  assert.equal(h.currentArchitecturePlacement("backend").family, "N4A");
  assert.equal(h.architecturePlacement("backend").family, "C4A");
  h.state.config.placement.validator.source = "unavailable";
  assert.equal(h.currentArchitecturePlacement("validator").family, undefined);
});

test("cluster placement includes verified manager and validator families alongside the specialists", () => {
  const h = harness();
  const placement = Object.fromEntries(["manager", ...roles, "validator"].map((role) => [role, {
    source: "kubernetes_node", machine_family: role === "manager" || role === "validator" ? "n4a" : "c4a", architecture: "arm64",
  }]));
  const config = { placement };
  assert.equal(h.currentArchitecturePlacement("cluster", config).label, "C4A + N4A · workload nodes");
  placement.manager.machine_family = "c4a";
  assert.equal(h.currentArchitecturePlacement("cluster", config).label, "C4A + N4A · workload nodes", "Validator-only N4A placement must remain visible");
  placement.validator.source = "unavailable";
  assert.equal(h.currentArchitecturePlacement("cluster", config).label, "C4A · partial workload placement");
  placement.manager.machine_family = "n4a";
  assert.equal(h.currentArchitecturePlacement("cluster", config).label, "C4A + N4A · partial workload placement", "Manager-only N4A placement must remain visible");
  h.state.run = run({ status: "passed" }); h.renderEnvironment();
  assert.match(h.get("environment-description").textContent, /CPU cards show current workload usage.*selected run's recorded measurements/);
});

test("CPU platform badge includes manager while the Gemma caption covers only specialists", () => {
  const h = harness();
  h.state.config = { placement: Object.fromEntries(["manager", ...roles, "validator"].map((role) => [role, {
    source: "kubernetes_node", machine_family: role === "manager" || role === "validator" ? "n4a" : "c4a", architecture: "arm64",
  }])) };
  h.renderBooth();
  assert.equal(h.get("booth-platform-label").textContent, "AXION C4A / N4A · ARM CPU");
  assert.match(h.get("booth-local-compute").textContent, /Google Axion C4A$/);
  assert.doesNotMatch(h.get("booth-local-compute").textContent, /N4A/);
  h.state.config.placement.manager.machine_family = "c4a"; h.renderBooth();
  assert.equal(h.get("booth-platform-label").textContent, "AXION C4A · ARM CPU", "Validator has no CPU card and must not expand this badge's scope");
  h.state.config.placement.manager.source = "unavailable"; h.renderBooth();
  assert.equal(h.get("booth-platform-label").textContent, "C4A · PARTIAL PLACEMENT");
});

test("default entry may resume an active workflow without replaying completed work", async () => {
  const h = harness();
  h.state.config = { live_available: true };
  h.state.runs = [run({ id: "old", status: "passed" }), run({ id: "active", status: "validating" })];
  h.context.selected = [];
  h.replace("selectRun = async (id) => selected.push(id); renderBooth = () => {};");
  await h.selectBoothRun();
  assert.deepEqual(h.context.selected, ["active"]);
});

test("explicit output links select that run and open the result panel", async () => {
  const h = harness("?view=booth&panel=result&run=saved-123");
  h.context.selected = [];
  h.context.panels = [];
  h.replace("selectRun = async (id) => selected.push(id); setBoothPanel = (panel, options) => panels.push({panel, options});");
  await h.selectBoothRun();
  assert.deepEqual(h.context.selected, ["saved-123"]);
  assert.deepEqual(plain(h.context.panels), [{ panel: "result", options: { automatic: true } }]);
});

test("read-only archives require an explicit saved-result link", async () => {
  const h = harness();
  h.state.config = { preview_read_only: true, live_available: false, booth: { preferred_run_id: "archive-123" } };
  h.context.selected = [];
  h.replace("selectRun = async (id) => selected.push(id); renderBooth = () => {};");
  await h.selectBoothRun();
  assert.deepEqual(h.context.selected, []);
  h.context.window.location.search = "?panel=result&run=archive-123";
  await h.selectBoothRun();
  assert.deepEqual(h.context.selected, ["archive-123"]);
  h.state.connected = true;
  assert.match(h.boothLabel(), /Archived preview.*recorded/);
});

test("invalid result query does not invent a queued workflow", async () => {
  const h = harness("?run=..%2Fescape");
  h.context.selected = [];
  h.replace("selectRun = async (id) => selected.push(id); renderBooth = () => {};");
  await h.selectBoothRun();
  assert.deepEqual(h.context.selected, []);
  assert.equal(h.state.run, null);
});

test("opening an unknown run reports failure and resumes polling the existing active run", async () => {
  const h = harness();
  const current = run();
  h.state.run = current;
  h.replace("stopBoothTour = () => {}; api = async () => { throw Error('Run not found'); };");
  await h.selectRun("missing");
  assert.equal(h.state.run, current);
  assert.match(h.get("request-error").textContent, /Run not found/);
  assert.equal(h.timers.at(-1).delay, 1500);
  assert.equal(h.urls.length, 0);
});

test("reselecting the same run invalidates pending quotes and the old iframe", async () => {
  const h = harness();
  const saved = run({ status: "passed", preview_url: "/preview/run-123/" });
  h.state.run = saved;
  h.state.runs = [saved];
  h.state.frameRunId = saved.id;
  h.state.candidateQuote = { total_cents: 123 };
  h.state.candidateQuoteStatus = "ready";
  h.state.quoteSeq = 8;
  h.get("candidate-preview").src = "/preview/run-123/";
  h.replace("stopBoothTour = () => {}; renderRun = () => {}; renderMetrics = () => {}; refreshRun = async () => {}; refreshArtifacts = async () => {};");
  await h.selectRun(saved.id);
  assert.equal(h.state.quoteSeq, 9);
  assert.equal(h.state.frameRunId, null);
  assert.equal(h.state.candidateQuote, null);
  assert.equal(h.state.candidateQuoteStatus, "idle");
  assert.equal(h.get("candidate-preview").src, undefined);
  assert.equal(new URL(h.urls.at(-1)).searchParams.get("run"), saved.id);
});

test("two tabs synchronize visibility, accessibility state, focus and the result URL", () => {
  const h = harness("?view=booth&run=run-123");
  h.replace("stopBoothTour = () => {}; postQuote = () => {};");
  h.setBoothPanel("result", { focus: true });
  assert.equal(h.get("booth-team-view").hidden, true);
  assert.equal(h.get("booth-result-view").hidden, false);
  assert.equal(h.tabs[0].getAttribute("aria-selected"), "false");
  assert.equal(h.tabs[0].tabIndex, -1);
  assert.equal(h.tabs[1].getAttribute("aria-selected"), "true");
  assert.equal(h.tabs[1].tabIndex, 0);
  assert.equal(h.tabs[1].focused, true);
  assert.equal(new URL(h.urls.at(-1)).searchParams.get("panel"), "result");
  assert.equal(new URL(h.urls.at(-1)).searchParams.get("run"), "run-123");
  h.setBoothPanel("compute");
  assert.equal(h.state.booth.panel, "result");
  h.setBoothPanel("team");
  assert.equal(h.get("booth-team-view").hidden, false);
  assert.equal(h.get("booth-result-view").hidden, true);
  assert.equal(new URL(h.urls.at(-1)).searchParams.has("panel"), false);
});

function quoteHarness() {
  const h = harness();
  h.state.catalog = [{ sku: "TOTE", price_cents: 1000 }];
  h.state.cart.set("TOTE", 1);
  h.state.run = run({ status: "passed", preview_url: "/preview/run-123/" });
  h.state.frameRunId = h.state.run.id;
  h.state.candidateQuote = { total_cents: 9999 };
  h.state.candidateQuoteStatus = "ready";
  h.state.baselineQuote = { total_cents: 9999 };
  h.replace("renderBooth = () => {};");
  return h;
}

test("a quote failure clears old totals and hides the preview instead of showing stale success", async () => {
  const h = quoteHarness();
  h.context.quoteApi = async (url) => {
    if (url === "/api/baseline/quote") return { total_cents: 1799 };
    throw new Error("No saved quote for this basket");
  };
  h.replace("api = quoteApi;");
  await h.refreshQuotes();
  assert.equal(h.state.baselineQuoteStatus, "ready");
  assert.equal(h.state.candidateQuote, null);
  assert.equal(h.state.candidateQuoteStatus, "error");
  assert.equal(h.get("candidate-preview").hidden, true);
  assert.equal(h.get("preview-empty").hidden, false);
  assert.match(h.get("quote-error").textContent, /No saved quote/);
  assert.equal(h.get("candidate-preview").messages.length, 0);
});

test("a later basket wins even when an earlier quote finishes last", async () => {
  const h = quoteHarness(), requests = [];
  h.context.quoteApi = (url, options) => {
    const pending = deferred();
    requests.push({ ...pending, url, body: JSON.parse(options.body) });
    return pending.promise;
  };
  h.replace("api = quoteApi;");
  const first = h.refreshQuotes();
  assert.equal(h.get("candidate-preview").hidden, true);
  h.state.cart.set("TOTE", 2);
  const second = h.refreshQuotes();
  assert.equal(requests[0].body.items[0].quantity, 1);
  assert.equal(requests[2].body.items[0].quantity, 2);
  requests[2].resolve({ total_cents: 2799 });
  requests[3].resolve({ total_cents: 2500 });
  await second;
  requests[0].resolve({ total_cents: 1799 });
  requests[1].resolve({ total_cents: 1500 });
  await first;
  assert.equal(h.state.baselineQuote.total_cents, 2799);
  assert.equal(h.state.candidateQuote.total_cents, 2500);
  assert.equal(h.get("candidate-preview").messages.length, 1);
  assert.equal(h.get("candidate-preview").messages[0].payload.quote.total_cents, 2500);
  assert.equal(h.get("candidate-preview").hidden, false);
});

test("a quote response for a run that is no longer selected cannot update the preview", async () => {
  const h = quoteHarness(), pending = deferred();
  h.context.quoteApi = async (url) => url === "/api/baseline/quote" ? { total_cents: 1799 } : pending.promise;
  h.replace("api = quoteApi;");
  const refreshing = h.refreshQuotes();
  h.state.run = run({ id: "another-run", status: "failed" });
  pending.resolve({ total_cents: 1500 });
  await refreshing;
  assert.equal(h.state.candidateQuote, null);
  assert.equal(h.get("candidate-preview").messages.length, 0);
  assert.equal(h.get("candidate-preview").hidden, true);
});

test("baseline failure is reported separately from a successful candidate quote", async () => {
  const h = quoteHarness();
  h.context.quoteApi = async (url) => {
    if (url === "/api/baseline/quote") throw new Error("Baseline unavailable");
    return { total_cents: 1500 };
  };
  h.replace("api = quoteApi;");
  await h.refreshQuotes();
  assert.equal(h.state.baselineQuoteStatus, "error");
  assert.equal(h.state.baselineQuote, null);
  assert.equal(h.state.candidateQuoteStatus, "ready");
  h.renderBoothOverview();
  assert.match(h.get("booth-overview-result").textContent, /original checkout is unavailable/i);
  assert.doesNotMatch(h.get("booth-overview-result").textContent, /Loading the verified checkout/);
});

test("current CPU cards use current placement and label their actual denominator", () => {
  const h = harness();
  h.state.connected = true;
  h.state.config = { placement: { backend: { source: "kubernetes_node", machine_family: "n4a", architecture: "arm64" } } };
  h.state.run = run({ status: "passed", agents: [{ role: "backend", status: "completed", machine_family: "c4a" }] });
  h.state.metrics = { samples: [{ role: "backend", cpu_cores: 3.25, cpu_limit_cores: 6.5, timestamp: "2026-10-15T10:00:00Z" }] };
  h.state.currentMetrics = { scope: "current", samples: [{ role: "backend", cpu_cores: 15, cpu_limit_cores: 12, timestamp: "2026-10-20T10:00:00Z" }] };
  h.renderBoothOverview();
  const card = h.get("booth-overview-metrics").children.find((item) => item.dataset.role === "backend");
  assert.match(card.textContent, /N4A/);
  assert.doesNotMatch(card.textContent, /C4A/);
  assert.equal(h.architecturePlacement("backend").family, "C4A");
  assert.match(card.querySelector(".v3-metric-current").textContent, /125% of Pod CPU limit/);
  assert.match(card.querySelector(".v3-metric-percent").textContent, /15 \/ 12 cores/);
  assert.equal(card.querySelector("circle").getAttribute("cy"), "6.00");
  assert.match(card.title, /125%.*above 100% clipped only in chart/);
  assert.equal(h.document.body.dataset.executionMode, "recorded");
});

test("rehearsal cards suppress any carried-over cloud metrics", () => {
  const h = harness();
  h.state.connected = true;
  h.state.run = run({ mode: "rehearsal" });
  h.state.metrics = { samples: [{ role: "backend", cpu_cores: 6, cpu_limit_cores: 12, memory_bytes: 999999999 }] };
  h.renderBoothOverview();
  const text = h.get("booth-overview-metrics").textContent;
  assert.match(text, /No cloud utilization in rehearsal/);
  assert.doesNotMatch(text, /50%|954 MiB/);
  assert.equal(h.document.body.dataset.executionMode, "rehearsal");
});

test("old current CPU is visibly stale even when no workflow is active", () => {
  const h = harness();
  h.state.connected = true;
  h.state.run = run();
  h.state.currentMetrics = { scope: "current", samples: [{ role: "backend", cpu_cores: 2, cpu_limit_cores: 6.5, timestamp: "2000-01-01T00:00:00Z" }] };
  h.renderBoothOverview();
  assert.match(h.get("booth-overview-metrics").textContent, /stale sample/);
  assert.match(h.get("booth-overview-metrics").textContent, /CPU % unavailable/);
  assert.doesNotMatch(h.get("booth-overview-metrics").textContent, /0% of/);
  const card = h.get("booth-overview-metrics").children.find((item) => item.dataset.role === "backend");
  assert.equal(card.dataset.stale, "true");
  assert.ok(card.children.some((child) => child.className === "v3-metric-stale" && child.textContent === "Stale sample"));
  h.state.run.status = "passed";
  h.renderBoothOverview();
  const saved = h.get("booth-overview-metrics").children.find((item) => item.dataset.role === "backend");
  assert.equal(saved.dataset.stale, "true");
  assert.equal(saved.querySelector("strong").textContent, "—");
  h.state.run = null;
  h.renderBoothOverview();
  assert.equal(h.get("booth-overview-metrics").children.find((item) => item.dataset.role === "backend").dataset.stale, "true");
});

test("failure and disconnection preserve the distinction between stopped and active work", () => {
  const h = harness();
  h.state.connected = true;
  h.state.run = run({ status: "failed", error: "Planner credentials expired" });
  h.renderBoothOverview();
  assert.match(h.get("booth-overview-result").textContent, /Workflow failed.*Planner credentials expired/);
  assert.equal(h.get("booth-open-shop-link").hidden, true);
  assert.equal(h.get("booth-open-shop-link").getAttribute("href"), null);
  assert.equal(h.get("booth-cta-result").disabled, true);
  h.state.connected = false;
  h.state.config = { live_available: true };
  h.renderBoothOverview();
  assert.match(h.get("booth-current-stage").textContent, /Connection lost/);
  assert.equal(h.document.body.dataset.executionMode, "disconnected");
});

test("a recorded planning failure exposes no verified basket or application from an older run", () => {
  const h = harness();
  h.state.connected = true;
  h.state.config = { live_available: true };
  const accepted = run({ id: "accepted-run", status: "passed", preview_url: "/preview/accepted-run/", tests: { total: 14, passed: 14, failed: 0 } });
  const acceptedBefore = plain(accepted);
  const failed = run({
    id: "e8a60f2ba675", status: "failed", plan: [], preview_url: null,
    tests: { total: 0, passed: 0, failed: 0, cases: [] },
    error: "Invalid JSON: EOF while parsing a string",
    events: [{ type: "planning" }, { type: "failed" }],
  });
  h.state.runs = [accepted, failed];
  h.state.run = failed;
  h.state.frameRunId = accepted.id;
  h.state.candidateQuoteStatus = "ready";
  h.state.candidateQuote = { total_cents: 10285 };
  h.state.baselineQuote = { total_cents: 12899 };
  h.get("candidate-preview").src = "/preview/accepted-run/";
  h.state.booth.inspect = "result";
  h.renderEnvironment(); h.renderBooth();
  assert.match(h.get("booth-current-stage").textContent, /Planning failed.*specialists did not start/);
  assert.match(h.get("booth-overview-result").textContent, /Planning failed.*Updated application unavailable.*Invalid JSON/);
  assert.match(h.get("booth-overview-result").textContent, /No acceptance checks ran/);
  assert.doesNotMatch(h.get("booth-overview-result").textContent, /Combined change verified|Independent checks protect|Loading the verified/);
  for (const id of ["booth-verified-basket", "booth-cta-result", "booth-open-shop-link", "booth-impact"]) assert.equal(h.get(id).hidden, true, id);
  assert.equal(h.get("booth-verified-basket").disabled, true);
  assert.equal(h.get("booth-open-shop-link").getAttribute("href"), null);
  assert.equal(h.get("booth-total-after").textContent, "—");
  assert.equal(h.get("booth-validation-count").textContent, "Not started");
  assert.ok(h.agentStatuses.every((item) => item.textContent === "Not started"));
  assert.match(h.get("booth-inspector-copy").textContent, /This request produced no updated application/);
  assert.match(h.get("booth-result-description").textContent, /no updated application/);
  assert.match(h.get("environment-description").textContent, /This run failed; no updated application/);
  assert.doesNotMatch(h.get("environment-description").textContent, /completed application/);
  const stages = h.workflowStages(failed);
  assert.equal(stages.manager, "failed");
  assert.equal(stages.validator, "idle");
  assert.ok(roles.every((role) => stages[role] === "idle"));
  h.renderPreview();
  assert.equal(h.get("candidate-preview").hidden, true);
  assert.equal(h.get("candidate-preview").src, undefined);
  assert.equal(h.state.candidateQuote, null);
  assert.equal(h.get("preview-empty").querySelector("strong").textContent, "Updated application unavailable");
  assert.match(h.get("preview-empty").querySelector("p").textContent, /Planning failed before the specialists or validation started/);
  assert.equal(h.state.run, failed);
  assert.deepEqual(plain(accepted), acceptedBefore);
  assert.equal(h.updatedShopPath(accepted), "/?view=booth&panel=result&run=accepted-run");
});

test("verified basket requires a matching accepted run and current successful quotes, including an empty basket", () => {
  const h = harness();
  h.state.connected = true;
  h.state.run = run({ status: "passed", preview_url: "/preview/run-123/", tests: { total: 14, passed: 14, failed: 0 } });
  h.state.frameRunId = h.state.run.id;
  h.state.candidateQuoteStatus = "ready";
  h.state.candidateQuote = { total_cents: 0 };
  h.state.baselineQuote = { total_cents: 0 };
  h.renderBooth();
  assert.equal(h.get("booth-verified-basket").hidden, false);
  assert.equal(h.get("booth-verified-basket").disabled, false);
  assert.equal(h.get("booth-cta-result").hidden, false);
  assert.equal(h.get("booth-impact").hidden, false);
  assert.equal(h.get("booth-total-after").textContent, "$0.00");
  assert.match(h.get("booth-overview-result").textContent, /Combined change verified/);
  for (const status of ["loading", "error"]) {
    h.state.candidateQuoteStatus = status; h.renderBooth();
    assert.equal(h.get("booth-verified-basket").hidden, true);
    assert.equal(h.get("booth-impact").hidden, true);
    assert.doesNotMatch(h.get("booth-overview-result").textContent, /Combined change verified/);
  }
  h.state.candidateQuoteStatus = "ready";
  h.state.frameRunId = "different-run"; h.renderBooth();
  assert.equal(h.get("booth-verified-basket").hidden, true);
  h.state.frameRunId = h.state.run.id; h.state.submitting = true; h.renderBooth();
  assert.equal(h.get("booth-verified-basket").hidden, true);
  assert.equal(h.get("booth-cta-result").hidden, true);
});

test("planning failure copy needs recorded planning evidence and cannot replace later validation failure", () => {
  const h = harness();
  h.state.connected = true;
  h.state.run = run({
    status: "failed", plan: [], tests: { total: 0, passed: 0, failed: 0 },
    agents: roles.map((role) => ({ role, status: "completed" })),
    events: [{ type: "planning" }, { type: "validation_started" }, { type: "failed" }],
    error: "Validator connection unavailable",
  });
  h.renderBooth();
  assert.doesNotMatch(h.get("booth-current-stage").textContent, /Planning failed/);
  assert.doesNotMatch(h.get("booth-overview-result").textContent, /Planning failed|No acceptance checks ran/);
  assert.match(h.get("booth-overview-result").textContent, /Workflow failed.*Validator connection unavailable.*No acceptance results/);
  assert.equal(h.get("booth-validation-count").textContent, "No results reported");
  assert.equal(h.workflowStages(h.state.run).validator, "failed");
  h.state.run = run({ status: "failed", plan: [], events: [] }); h.renderBoothOverview();
  assert.doesNotMatch(h.get("booth-overview-result").textContent, /Planning failed|No acceptance checks ran/);
});

test("archive, submitting and already active workflows cannot submit another run", async () => {
  for (const setup of [
    (h) => { h.state.config = { preview_read_only: true }; },
    (h) => { h.state.submitting = true; },
    (h) => { h.state.run = run({ status: "validating" }); },
  ]) {
    const h = harness();
    h.context.requests = [];
    h.replace("api = async (...args) => { requests.push(args); throw Error('Unexpected request'); };");
    setup(h);
    await h.startRun();
    assert.deepEqual(h.context.requests, []);
  }
});

test("live-ready initialization selects live mode but neither submits nor opens old work", async () => {
  const h = harness();
  h.context.requests = [];
  h.context.bootstrapApi = async (url, options) => {
    h.context.requests.push({ url, method: options?.method || "GET" });
    if (url === "/api/config") return { live_available: true, default_mode: "rehearsal", booth: { preferred_run_id: "old" } };
    if (url === "/api/catalog") return [];
    if (url === "/api/runs") return [run({ id: "old", status: "passed" })];
    throw new Error(`Unexpected API path: ${url}`);
  };
  h.replace(`
    api = bootstrapApi;
    setBoothView = (enabled) => { state.booth.enabled = enabled; };
    renderAgents = renderExamples = renderProducts = resetCart = renderHistory = renderRun = renderBooth = () => {};
  `);
  await h.initialize();
  assert.equal(h.state.mode, "live");
  assert.equal(h.state.run, null);
  assert.deepEqual(h.context.requests, [
    { url: "/api/config", method: "GET" }, { url: "/api/catalog", method: "GET" }, { url: "/api/runs", method: "GET" },
  ]);
});

test("live is the only selectable frontend mode even before services are ready", () => {
  const h = harness();
  assert.equal(h.state.mode, "live");
  h.selectMode("rehearsal");
  assert.equal(h.state.mode, "live");
  h.renderEnvironment();
  assert.equal(h.get("mode-rehearsal").hidden, true);
  assert.equal(h.get("mode-rehearsal").disabled, true);
  assert.doesNotMatch(h.get("mode-note").textContent, /offline|fixture|rehearsal/i);
  assert.match(h.get("live-note").textContent, /Live unavailable/);
});

test("unavailable live services never cause a fixture fallback during initialization", async () => {
  const h = harness();
  h.context.bootstrapApi = async (url) => {
    if (url === "/api/config") return { live_available: false, default_mode: "rehearsal", live_reason: "Google Cloud authentication expired." };
    if (url === "/api/catalog") return [];
    if (url === "/api/runs") return [
      run({ id: "fixture-active", mode: "rehearsal" }), run({ id: "old-live", status: "passed" }),
    ];
    throw new Error(`Unexpected API path: ${url}`);
  };
  h.replace(`
    api = bootstrapApi;
    setBoothView = (enabled) => { state.booth.enabled = enabled; };
    renderAgents = renderExamples = renderProducts = resetCart = renderHistory = renderRun = renderBooth = () => {};
  `);
  await h.initialize();
  assert.equal(h.state.mode, "live");
  assert.equal(h.state.run, null);
  assert.deepEqual(Array.from(h.state.runs, (item) => item.id), ["old-live"]);
  assert.equal(h.timers[h.state.connectionTimer - 1].delay, 5000);
  assert.equal(h.get("refresh-connection").listeners.get("click"), h.refreshConnection);
});

test("Run stays disabled with the actual unavailable reason even when Studio HTTP is connected", async () => {
  const h = harness();
  h.state.connected = true;
  h.state.config = { live_available: false, live_reason: "Google Cloud authentication expired." };
  h.get("prompt").value = "Add a discount";
  h.context.requests = [];
  h.replace(`
    api = async (...args) => { requests.push(args); throw Error("Unexpected request"); };
    renderAgents = renderPlan = renderTests = renderEvents = renderPipeline = renderBooth = () => {};
  `);
  h.renderRun();
  assert.equal(h.get("run-button").disabled, true);
  assert.match(h.get("live-note").textContent, /Google Cloud authentication expired/);
  assert.match(h.boothLabel(), /Live connection required/);
  await h.startRun();
  assert.deepEqual(h.context.requests, []);
  assert.match(h.get("request-error").textContent, /Google Cloud authentication expired/);
  assert.doesNotMatch(h.get("request-error").textContent, /rehearsal|fixture/i);
  assert.equal(h.state.mode, "live");
});

test("a lost Studio connection also disables Run despite an old ready configuration", () => {
  const h = harness();
  h.state.config = { live_available: true, live_reason: "Workers were ready." };
  h.state.connected = false;
  h.replace("renderAgents = renderPlan = renderTests = renderEvents = renderPipeline = renderBooth = () => {};");
  h.renderRun();
  assert.equal(h.get("run-button").disabled, true);
  assert.match(h.get("live-note").textContent, /Studio connection was lost/);
  assert.doesNotMatch(h.get("live-note").textContent, /Workers were ready/);
});

test("explicit submission always sends mode live and never silently accepts a fixture result", async () => {
  for (const responseMode of ["live", "rehearsal"]) {
    const h = harness();
    h.state.config = { live_available: true };
    h.state.connected = true;
    h.state.mode = "rehearsal"; // A stale internal value must not affect the request.
    h.get("prompt").value = "Add a 10% discount";
    h.context.requests = [];
    h.context.startApi = async (url, options) => {
      h.context.requests.push({ url, ...options });
      return run({ mode: responseMode, status: "queued" });
    };
    h.replace("api = startApi; stopBoothTour = renderRun = renderMetrics = recordRun = () => {}; refreshRun = async () => {};");
    await h.startRun();
    assert.equal(h.context.requests.length, 1);
    assert.equal(h.context.requests[0].url, "/api/runs");
    assert.deepEqual(JSON.parse(h.context.requests[0].body), { prompt: "Add a 10% discount", mode: "live" });
    assert.equal(h.state.mode, "live");
    if (responseMode === "live") assert.equal(h.state.run.mode, "live");
    else {
      assert.equal(h.state.run, null);
      assert.match(h.get("request-error").textContent, /not a live execution record/);
    }
  }
});

test("history excludes every fixture status while keeping completed live records labelled", () => {
  const h = harness();
  h.state.runs = [
    run({ id: "fixture-passed", mode: "rehearsal", status: "passed" }),
    run({ id: "fixture-active", mode: "rehearsal", status: "running" }),
    run({ id: "live-passed", status: "passed" }), run({ id: "live-active", status: "running" }),
  ];
  h.renderHistory();
  const options = h.get("run-history").children;
  assert.deepEqual(options.map((item) => item.value), ["", "live-passed", "live-active"]);
  assert.match(options[1].textContent, /Recorded live/);
  assert.doesNotMatch(h.get("run-history").textContent, /Rehearsal/);
});

test("a fixture URL is rejected whether cached locally or returned by the API", async () => {
  for (const cached of [true, false]) {
    const h = harness("?run=fixture");
    const fixture = run({ id: "fixture", mode: "rehearsal", status: "running" });
    h.state.runs = cached ? [fixture] : [];
    h.context.fixture = fixture;
    h.context.requests = [];
    h.replace("stopBoothTour = () => {}; api = async (url) => { requests.push(url); return fixture; };");
    await h.selectRun("fixture");
    assert.equal(h.state.run, null);
    assert.equal(h.state.frameRunId, null);
    assert.match(h.get("request-error").textContent, /Offline test fixtures are unavailable/);
    assert.equal(h.urls.length, 0);
    assert.equal(h.timers.length, 0);
    assert.equal(h.context.requests.length, cached ? 0 : 1);
  }
});

test("only an active live run can be resumed on default entry", async () => {
  const h = harness();
  h.state.runs = [run({ id: "fixture", mode: "rehearsal" }), run({ id: "real-live" })];
  h.context.selected = [];
  h.replace("selectRun = async (id) => selected.push(id); renderBooth = () => {};");
  await h.selectBoothRun();
  assert.deepEqual(h.context.selected, ["real-live"]);
});

test("even passed fixture results cannot expose a shop link or a candidate quote", async () => {
  const h = quoteHarness();
  h.state.run.mode = "rehearsal";
  h.context.requests = [];
  h.replace("api = async (url) => { requests.push(url); return {total_cents: 1799}; };");
  assert.equal(h.updatedShopPath(h.state.run), null);
  await h.refreshQuotes();
  assert.deepEqual(h.context.requests, ["/api/baseline/quote"]);
  assert.equal(h.get("candidate-preview").hidden, true);
  assert.equal(h.state.candidateQuote, null);
  assert.equal(h.state.frameRunId, null);
});

test("connection refresh recovers readiness without navigating or submitting work", async () => {
  const h = harness(), pending = deferred();
  h.state.config = { live_available: false, live_reason: "Cloud unavailable" };
  h.context.requests = [];
  h.context.checkApi = (url) => { h.context.requests.push(url); return pending.promise; };
  h.replace("api = checkApi; renderRun = renderEnvironment;");
  const refreshing = h.refreshConnection();
  assert.equal(h.get("refresh-connection").disabled, true);
  assert.equal(h.get("refresh-connection").getAttribute("aria-busy"), "true");
  await h.refreshConnection();
  assert.equal(h.context.requests.length, 1, "Concurrent refreshes must be deduplicated");
  pending.resolve({ live_available: true });
  await refreshing;
  assert.equal(h.state.mode, "live");
  assert.equal(h.state.connected, true);
  assert.equal(h.state.config.live_available, true);
  assert.equal(h.get("refresh-connection").disabled, false);
  assert.equal(h.get("refresh-connection").getAttribute("aria-busy"), "false");
  assert.match(h.get("connection-status").textContent, /Studio connected/);
  assert.match(h.get("live-note").textContent, /Gemini \+ GKE agents connected/);
  assert.equal(h.state.connectionTimer, null);
  assert.equal(h.state.run, null);
  assert.deepEqual(h.context.requests, ["/api/config"]);
  assert.equal(h.urls.length, 0);
});

test("an unavailable config keeps HTTP connected while retrying readiness every five seconds", async () => {
  const h = harness();
  h.context.checkApi = async () => ({ live_available: false, live_reason: "Gemini credentials expired" });
  h.replace("api = checkApi; renderRun = renderEnvironment;");
  await h.refreshConnection();
  assert.equal(h.state.connected, true);
  assert.equal(h.state.mode, "live");
  assert.match(h.get("connection-status").textContent, /Studio connected/);
  assert.match(h.get("live-note").textContent, /Gemini credentials expired/);
  assert.equal(h.timers[h.state.connectionTimer - 1].delay, 5000);
  assert.equal(h.timers[h.state.currentMetricsTimer - 1].delay, 0);
  assert.equal(h.state.run, null);
});

test("a failed connection refresh revokes old readiness and exposes the failure", async () => {
  const h = harness();
  h.state.config = { live_available: true, booth: { worker_node_vcpus: 16 } };
  h.state.connected = true;
  h.replace("api = async () => { throw Error('Connection refused'); }; renderRun = renderEnvironment;");
  await h.refreshConnection();
  assert.equal(h.state.config.live_available, false);
  assert.equal(h.state.config.booth.worker_node_vcpus, 16);
  assert.equal(h.state.connected, false);
  assert.equal(h.state.mode, "live");
  assert.match(h.get("live-note").textContent, /Connection refused/);
  assert.equal(h.timers.at(-1).delay, 5000);
});

function htmlTags() {
  const html = fs.readFileSync(path.join(webDir, "index.html"), "utf8");
  return [...html.matchAll(/<([a-z][\w:-]*)\b([^<>]*)>/gi)].map((match) => {
    const attributes = {};
    for (const attr of match[2].matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g)) {
      attributes[attr[1]] = attr[2] ?? attr[3] ?? attr[4] ?? "";
    }
    return { tag: match[1].toLowerCase(), attributes };
  });
}

test("static markup has unique IDs and every literal app.js ID target", () => {
  const ids = htmlTags().map((tag) => tag.attributes.id).filter(Boolean);
  assert.equal(new Set(ids).size, ids.length, "Duplicate HTML id");
  for (const match of source.matchAll(/\$\("([\w-]+)"\)/g)) {
    assert.ok(ids.includes(match[1]), `Missing app.js target #${match[1]}`);
  }
});

test("static markup exposes exactly two related tabs and keeps compute within Overview", () => {
  const tags = htmlTags();
  const tabs = tags.filter(({ attributes: a }) => "data-booth-panel" in a);
  assert.deepEqual(tabs.map(({ attributes: a }) => a["data-booth-panel"]), ["team", "result"]);
  for (const { attributes: a } of tabs) {
    assert.equal(a.role, "tab");
    const panel = tags.find((tag) => tag.attributes.id === a["aria-controls"]);
    assert.equal(panel.attributes.role, "tabpanel");
    assert.equal(panel.attributes["aria-labelledby"], a.id);
  }
  const compute = tags.find((tag) => tag.attributes.id === "booth-compute-view").attributes;
  assert.notEqual(compute.role, "tabpanel");
  assert.ok(!("hidden" in compute));
  const sectionStack = [];
  for (const match of fs.readFileSync(path.join(webDir, "index.html"), "utf8").matchAll(/<(\/?)section\b([^>]*)>/g)) {
    if (match[1]) { sectionStack.pop(); continue; }
    const id = match[2].match(/\bid="([^"]+)"/)?.[1] || null;
    if (id === "booth-compute-view") assert.ok(sectionStack.includes("booth-team-view"), "Compute must stay inside Overview");
    sectionStack.push(id);
  }
  const inspected = tags.filter((tag) => "data-inspect" in tag.attributes).map((tag) => tag.attributes["data-inspect"]).sort();
  assert.deepEqual([...new Set(inspected)], [...stageNames].sort());
  assert.ok(inspected.filter((key) => key !== "result").every((key, i, rest) => rest.indexOf(key) === i));
});

test("the new-window output link starts gated and the generated preview stays sandboxed", () => {
  const tags = htmlTags();
  const link = tags.find((tag) => tag.attributes.id === "booth-open-shop-link");
  assert.equal(link.tag, "a");
  assert.equal(link.attributes.target, "_blank");
  assert.match(link.attributes.rel, /\bnoopener\b/);
  assert.ok("hidden" in link.attributes);
  assert.ok(!("href" in link.attributes));
  const preview = tags.find((tag) => tag.attributes.id === "candidate-preview");
  assert.equal(preview.tag, "iframe");
  assert.equal(preview.attributes.sandbox, "allow-scripts");
  assert.equal(preview.attributes.referrerpolicy, "no-referrer");
  assert.ok("hidden" in preview.attributes);
  assert.ok(!("src" in preview.attributes));
  for (const id of ["booth-verified-basket", "booth-cta-result"]) {
    const button = tags.find((tag) => tag.attributes.id === id);
    assert.equal(button.tag, "button");
    assert.ok("hidden" in button.attributes, id);
    assert.ok("disabled" in button.attributes, id);
  }
});

test("static controls start live-only and provide connection refresh in both views", () => {
  const tags = htmlTags();
  for (const id of ["mode-live", "mode-rehearsal"]) {
    assert.ok("hidden" in tags.find((tag) => tag.attributes.id === id).attributes);
  }
  assert.ok("disabled" in tags.find((tag) => tag.attributes.id === "run-button").attributes);
  assert.equal(tags.find((tag) => tag.attributes.id === "refresh-connection").attributes.type, "button");
  assert.ok(tags.some((tag) => tag.attributes.id === "booth-connection-mount"));
  const markup = fs.readFileSync(path.join(webDir, "index.html"), "utf8");
  assert.match(markup, /<textarea\b[^>]*\bid="prompt"[^>]*>\s*<\/textarea>/);
  assert.ok(tags.some((tag) => tag.attributes.id === "booth-cpu-observation"));
});
