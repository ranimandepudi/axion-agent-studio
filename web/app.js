"use strict";

// The dashboard displays backend evidence. It never fabricates model activity,
// test results, cloud deployment status, or utilization samples.
const $ = (id) => document.getElementById(id);
const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const roleInfo = {
  backend: { name: "Backend Engineer", icon: "{ }", description: "Business rules, calculations, and application behavior." },
  frontend: { name: "Frontend Engineer", icon: "</>", description: "A clear checkout experience that reflects the new rules." },
  qa: { name: "QA Engineer", icon: "✓", description: "Writes tests for thresholds, rounding, and empty carts." },
};
// Telemetry's manager is the Arm coordination Pod, not hosted Gemini inference.
const telemetryRoles = { manager: "Manager", backend: "Backend", frontend: "Frontend", qa: "QA" };
const terminalStatuses = new Set(["passed", "failed"]);
const activeStatuses = new Set(["queued", "planning", "running", "validating"]);
const supportedExamples = [
  "Add a 10% discount for orders of $100 or more and free shipping for orders of $150 or more. Show the savings and delivery charge at checkout.",
  "Add a 15% discount for orders of $75 or more and free shipping for orders of $100 or more. Show the savings and delivery charge at checkout.",
  "Add a 5% discount for orders of $50 or more and free shipping for orders of $125 or more. Show the savings and delivery charge at checkout.",
];
const state = { config: null, catalog: [], cart: new Map(), mode: "live", run: null, runs: [], submitting: false, pollTimer: null, connectionTimer: null, checkingConnection: false, quoteSeq: 0, frameRunId: null, candidateQuote: null, candidateQuoteStatus: "idle", baselineQuote: null, metrics: null, currentMetrics: null, currentMetricHistory: [], currentMetricsError: null, currentMetricsTimer: null, checkingMetrics: false, artifactsRunId: null, statusAnnouncement: null, connected: false, runtimeStripPaused: false, booth: { enabled: false, step: 0, playing: false, timer: null, panel: "team", inspect: "request" } };
const STORY_INTERVAL_MS = 12000;
const boothPlacements = [];
const storyInspect = ["request", "manager", "backend", "validator", "result"];

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined && text !== null) el.textContent = String(text);
  return el;
}
function money(cents) { return Number.isFinite(cents) ? currency.format(cents / 100) : "—"; }
function number(value, digits = 1) { return Number.isFinite(value) ? value.toLocaleString("en-US", { maximumFractionDigits: digits }) : "—"; }
function duration(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.max(0, seconds).toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
}
function elapsedForRun(run, now = Date.now()) {
  if (!run) return null;
  // A completed execution keeps its recorded duration. Only a real active
  // run with a valid start time gets a live wall-clock display.
  if (!activeStatuses.has(run.status)) return run.elapsed_seconds;
  const started = Date.parse(run.created_at);
  if (!Number.isFinite(started) || started > now) return run.elapsed_seconds;
  return (now - started) / 1000;
}
function renderElapsed() {
  const value = duration(elapsedForRun(state.run));
  $("elapsed").textContent = value;
  boothText("booth-overview-duration", value);
}
function memory(bytes) {
  if (!Number.isFinite(bytes)) return "Not measured";
  return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : `${(bytes / 1024 ** 2).toFixed(0)} MiB`;
}
function roleName(role) { return roleInfo[role]?.name || String(role || "Manager"); }
function metricRoleName(role) { return role === "manager" ? "Arm coordinator" : roleName(role); }
function sentenceStatus(status) { return ({ pending: "Waiting", queued: "Queued", planning: "Planning", running: "Working", validating: "Verifying", completed: "Complete", passed: "Verified", failed: "Needs attention" })[status] || "Ready"; }
function setConnection(connected) {
  const reconnected = connected && !state.connected;
  state.connected = connected;
  const status = $("connection-status");
  status.className = `connection ${connected ? "connected" : "failed"}`;
  status.replaceChildren(node("i"), document.createTextNode(connected ? "Studio connected" : "Connection unavailable"));
  if (!connected && state.config && !state.checkingConnection) scheduleConnectionCheck();
  if (!connected) { clearTimeout(state.currentMetricsTimer); state.currentMetricsTimer = null; }
  else if (reconnected && state.config) scheduleCurrentMetrics(0);
}
function errorText(value) {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") return value.message || value.reason || JSON.stringify(value);
  return "The request could not be completed.";
}
async function api(path, options = {}, timeoutMs = 45000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, { ...options, headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers }, signal: controller.signal });
    let data;
    const body = await response.text();
    try { data = body ? JSON.parse(body) : {}; } catch { throw new Error(`The service returned an unreadable response (${response.status}).`); }
    if (!response.ok) throw new Error(errorText(data.detail || data.error || data.message || `Request failed (${response.status}).`));
    setConnection(true);
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The service took too long to respond. Check the connection and try again.");
    if (error instanceof TypeError) setConnection(false);
    throw error;
  } finally { clearTimeout(timeout); }
}
function showError(id, message) { const el = $(id); el.textContent = message || ""; el.hidden = !message; }
function emptyState(symbol, title, description) {
  const wrapper = node("div", "empty-state small");
  const icon = node("span", "empty-symbol", symbol); icon.setAttribute("aria-hidden", "true");
  wrapper.append(icon, node("strong", "", title), node("p", "", description));
  return wrapper;
}
function liveAvailable() { return state.config?.live_available === true; }
function archivePreview() { return state.config?.preview_read_only === true; }
function liveRun(run) { return run?.mode === "live"; }
function liveConnectionReason() {
  if (!state.connected && liveAvailable()) return "The Studio connection was lost. Refresh the connection to verify live services.";
  return state.config?.live_reason || (!state.connected ? "Connect to the Studio service, then refresh the connection." : "The live services have not been verified.");
}
function unsupportedRunMessage() {
  return "This result is not a live execution record. Offline test fixtures are unavailable in this console.";
}
function scheduleConnectionCheck() {
  clearTimeout(state.connectionTimer);
  state.connectionTimer = null;
  if (!archivePreview() && (!liveAvailable() || !state.connected)) state.connectionTimer = setTimeout(refreshConnection, 5000);
}
async function refreshConnection() {
  if (state.checkingConnection) return;
  clearTimeout(state.connectionTimer);
  state.connectionTimer = null;
  state.checkingConnection = true;
  renderConnectionControl();
  try {
    state.config = await api("/api/config");
    state.mode = "live";
    setConnection(true);
  } catch (error) {
    state.config = { ...state.config, live_available: false, live_reason: `Unable to verify live services: ${error.message}` };
    setConnection(false);
  } finally {
    state.checkingConnection = false;
    renderRun();
    scheduleConnectionCheck();
    scheduleCurrentMetrics(0);
  }
}
function scheduleCurrentMetrics(delay = 5000) {
  clearTimeout(state.currentMetricsTimer); state.currentMetricsTimer = null;
  if (state.connected && !archivePreview()) state.currentMetricsTimer = setTimeout(refreshCurrentMetrics, delay);
}
function acceptCurrentMetrics(data) {
  if (data?.scope !== "current" || !Array.isArray(data.samples)) throw new Error("The service did not return current workload telemetry.");
  // This short trace belongs to this browser session and the observed Pod.
  // Cached responses never create new measurement points; a replacement Pod
  // starts a fresh trace instead of inheriting a previous Pod's readings.
  state.currentMetricHistory = Object.keys(telemetryRoles).flatMap((role) => {
    const sample = data.samples.find((item) => item.role === role) || { role, timestamp: data.collected_at, cpu_cores: null, cpu_limit_cores: null, cpu_fresh: false };
    let history = state.currentMetricHistory.filter((item) => item.role === role);
    if (history.length && sample.pod_uid && history.at(-1).pod_uid !== sample.pod_uid) history = [];
    const key = (item) => [item.pod_uid, item.cpu_timestamp || item.timestamp, item.cpu_cores, item.cpu_limit_cores, item.cpu_fresh].join("|");
    if (!history.length || key(history.at(-1)) !== key(sample)) history.push({ ...sample });
    return history.slice(-60);
  });
  state.currentMetrics = data; state.currentMetricsError = null;
}
async function refreshCurrentMetrics() {
  if (state.checkingMetrics || !state.connected || archivePreview()) return;
  clearTimeout(state.currentMetricsTimer); state.currentMetricsTimer = null;
  state.checkingMetrics = true;
  renderBoothOverview();
  try { acceptCurrentMetrics(await api("/api/metrics/current", {}, 15000)); }
  catch (error) { state.currentMetricsError = `Current CPU unavailable: ${error.message}`; }
  finally { state.checkingMetrics = false; renderRun(); scheduleCurrentMetrics(); }
}
function currentCpuState(sample, now = Date.now()) {
  const timestamp = Date.parse(sample?.cpu_timestamp || sample?.timestamp);
  const hasCpuEvidence = Boolean(sample?.cpu_timestamp) || cpuUsage(sample).cores !== null;
  const stale = hasCpuEvidence && (sample.cpu_fresh === false || !Number.isFinite(timestamp) || now - timestamp > 60000 || timestamp - now > 5000);
  const unavailable = !state.connected || Boolean(state.currentMetricsError) || state.currentMetrics?.source === "unavailable" || !sample || sample.cpu_fresh === false || stale;
  return { timestamp, stale, unavailable, usage: unavailable ? cpuUsage(null) : cpuUsage(sample) };
}
function renderConnectionControl() {
  const button = $("refresh-connection");
  if (!button) return;
  button.disabled = state.checkingConnection;
  button.setAttribute("aria-busy", String(state.checkingConnection));
  button.textContent = state.checkingConnection ? "Checking connection…" : "Refresh connection";
}
function planningFailed(run) {
  const lastStep = (run?.events || []).filter((event) => event.type !== "failed").at(-1);
  return run?.status === "failed" && !run.plan?.length && !run.tests?.total && lastStep?.type === "planning"
    && !(run.agents || []).some((agent) => ["running", "completed", "failed"].includes(agent.status));
}
function workflowStages(run) {
  const stages = { request: "idle", manager: "idle", coordinator: "idle", backend: "idle", frontend: "idle", qa: "idle", validator: "idle", result: "idle" };
  if (!run) return stages;
  stages.request = "completed";
  stages.coordinator = activeStatuses.has(run.status) ? "running" : run.status === "passed" ? "completed" : "failed";
  const workers = run.agents || [];
  for (const role of Object.keys(roleInfo)) {
    const status = workers.find((agent) => agent.role === role)?.status;
    stages[role] = ["running", "completed", "failed"].includes(status) ? status : "idle";
  }
  const workerActive = workers.some((agent) => agent.status === "running");
  const checked = run.tests?.total > 0;
  stages.manager = run.status === "planning" ? "running" : run.plan?.length ? "completed" : "idle";
  // During a repair the run remains "validating"; actual worker state takes
  // precedence so the diagram never pretends validation and repair are the same.
  stages.validator = checked ? run.tests.failed ? "failed" : "completed" : "idle";
  if (run.status === "validating" && !workerActive && (!checked || run.tests.failed)) stages.validator = "running";
  if (run.status === "validating" && !workerActive && checked && !run.tests.failed) stages.manager = "running";
  if (run.status === "passed") stages.result = "completed";
  if (run.status === "failed") {
    for (const key of Object.keys(stages)) if (stages[key] === "running") stages[key] = "failed";
    if (!run.plan?.length) stages.manager = "failed";
    const lastStep = (run.events || []).filter((event) => event.type !== "failed").at(-1);
    if (!checked && lastStep?.type === "validation_started") stages.validator = "failed";
  }
  return stages;
}
function workflowActivity(run, stages = workflowStages(run)) {
  if (!run) return "Ready for your request";
  if (planningFailed(run)) return "Planning failed · specialists did not start";
  if (run.status === "failed") return "Workflow stopped · review the error";
  if (run.status === "passed") return "Verified · updated shop ready";
  if (run.status === "queued") return "Request accepted · starting coordination";
  if (run.status === "planning") return run.mode === "rehearsal" ? "Loading the rehearsal plan" : "Gemini is planning the change";
  const working = Object.keys(roleInfo).filter((role) => stages[role] === "running").map(roleName);
  if (working.length) return `${working.join(" + ")} ${run.mode === "rehearsal" ? "loading fixtures" : "working"}`;
  if (stages.validator === "running") return "Independent validator is checking the combined change";
  if (stages.manager === "running") return run.mode === "rehearsal" ? "Fixture checks passed · finalizing the rehearsal" : "Checks passed · Gemini is summarizing the result";
  return "Coordinator is collecting results and dispatching the next step";
}
function placementFamily(value) {
  const family = typeof value === "string" ? value.trim() : "";
  return /^(unknown|not[ -](reported|measured|available)|unavailable|n\/a|none|null)$/i.test(family) ? "" : family;
}
function architecturePlacement(role, run = state.run, config = state.config, metrics = run === state.run ? state.metrics : null) {
  if (run && !liveRun(run)) return { label: "No cloud execution", source: "No live execution evidence" };
  if (role === "coordinator") {
    const sample = run ? [...(metrics?.samples || [])].reverse().find((item) => item.role === "manager") : null;
    const recorded = sample?.placement_source === "kubernetes_node" && placementFamily(sample.machine_family) ? sample : null;
    const current = (!run || activeStatuses.has(run.status)) && config?.placement?.manager?.source === "kubernetes_node" ? config.placement.manager : null;
    const placement = recorded || current;
    const family = placementFamily(placement?.machine_family).toUpperCase();
    if (family) {
      const arm = /^(arm64|aarch64)$/i.test(placement.architecture || "");
      const observedAt = recorded ? placement.placement_observed_at : placement.observed_at;
      const source = `${recorded ? "This run's recorded" : "Current observed"} coordinator Pod placement from Kubernetes Node labels${placement.machine_type ? ` · ${placement.machine_type}` : ""}${observedAt ? ` · observed ${observedAt}` : ""}`;
      return { label: `${family}${arm ? " · Arm" : ""}`, family, source };
    }
    return { label: "Placement not reported", source: "No verified coordinator node placement is available for this selection" };
  }
  if (role === "validator") return { label: "Independent sandbox", source: "Runs protected acceptance checks separately from the coding agents" };
  if (role === "result") return { label: updatedShopPath(run) ? "Verified preview" : "After checks pass", source: "The updated application opens only after independent validation" };
  if (role === "cluster") {
    const placements = Object.keys(roleInfo).map((key) => architecturePlacement(key, run, config));
    const families = [...new Set(placements.filter((item) => item.family).map((item) => item.family))];
    return { label: families.length ? `${families.join(" + ")} · ${placements.some((item) => !item.family) ? "partial placement" : run && terminalStatuses.has(run.status) ? "recorded workers" : "reported workers"}` : "Worker placement pending", source: "Worker families from execution records or ready services; no additional node pools are inferred" };
  }
  const agent = run?.agents?.find((item) => item.role === role);
  const recordedFamily = placementFamily(agent?.machine_family);
  if (recordedFamily) return { label: recordedFamily.toUpperCase(), family: recordedFamily.toUpperCase(), source: "Machine family reported by this run's worker" };
  // Current services can describe a pending worker, never rewrite a historic run.
  const service = config?.services?.[role];
  const readyFamily = (!run || activeStatuses.has(run.status)) && ["ready", "ok", "healthy"].includes(service?.status) ? placementFamily(service.machine_family) : "";
  if (readyFamily) return { label: readyFamily.toUpperCase(), family: readyFamily.toUpperCase(), source: "Current ready worker service; execution placement pending" };
  return { label: "Placement pending", source: "No machine family is reported for this worker" };
}
function currentArchitecturePlacement(role, config = state.config, metrics = state.currentMetrics) {
  if (role === "cluster") {
    const placements = ["coordinator", ...Object.keys(roleInfo), "validator"].map((key) => currentArchitecturePlacement(key, config, metrics));
    const families = [...new Set(placements.map((item) => item.family).filter(Boolean))].sort();
    return { label: families.length ? `${families.join(" + ")} · ${placements.every((item) => item.family) ? "workload nodes" : "partial workload placement"}` : "Current placement unavailable", source: "Current observed manager, specialist and validator Kubernetes Node labels, separate from saved execution placement" };
  }
  // The API contract hosts the shop HTTP service in the manager Pod. Generated
  // pricing still executes in the independent validator, never in that Pod.
  const key = ({ coordinator: "manager", result: "manager" })[role] || role;
  const sample = metrics?.samples?.find((item) => item.role === key);
  const observed = [sample, config?.placement?.[key]].filter((item) => (item?.placement_source === "kubernetes_node" || item?.source === "kubernetes_node") && placementFamily(item.machine_family));
  let placement = observed[0];
  if (observed.length === 2) {
    const times = observed.map((item) => Date.parse(item.placement_observed_at || item.observed_at));
    const conflict = ["node_uid", "node", "pod_uid", "pod", "machine_type", "machine_family", "architecture"].some((field) => observed.every((item) => item[field]) && observed[0][field] !== observed[1][field]);
    if (times.every(Number.isFinite) && times[0] !== times[1]) placement = observed[times[0] > times[1] ? 0 : 1];
    else if (conflict) placement = null;
    else if (!Number.isFinite(times[0]) && Number.isFinite(times[1])) placement = observed[1];
  }
  const verified = placement?.placement_source === "kubernetes_node" || placement?.source === "kubernetes_node";
  const family = verified ? placementFamily(placement.machine_family).toUpperCase() : "";
  if (!family) return { label: "Placement not reported", source: "Current node placement has not been verified" };
  const arm = /^(arm64|aarch64)$/i.test(placement.architecture || "");
  const observedAt = placement.placement_observed_at || placement.observed_at;
  const hosting = role === "result" ? "Shop HTTP service in coordinator Pod; generated quotes execute in the independent validator. " : "";
  return { label: `${family}${arm ? " · Arm" : ""}`, family, placement, source: `${hosting}Current observed Kubernetes Node labels${placement.machine_type ? ` · ${placement.machine_type}` : ""}${observedAt ? ` · observed ${observedAt}` : ""}` };
}
function currentWorkloadTopology() {
  const topology = currentVmTopology();
  return topology.cpuCount === null ? "Workload usage, not whole-machine usage." : `Four workloads share ${topology.cpuCount} VM${topology.cpuCount === 1 ? "" : "s"}. Pod CPU, not whole-machine usage.`;
}
function currentVmTopology(config = state.config, metrics = state.currentMetrics) {
  const roles = ["manager", ...Object.keys(roleInfo), "validator"];
  const placements = Object.fromEntries(roles.map((role) => [role, currentArchitecturePlacement(role, config, metrics).placement]));
  const groups = new Map(), hosts = {};
  for (const role of roles) {
    const placement = placements[role];
    if (!placementFamily(placement?.node)) continue;
    // A name reused for a replacement Node must not merge two identities.
    const sameIdentity = Object.values(placements).filter((other) => other?.node === placement.node || placement.node_uid && other?.node_uid === placement.node_uid);
    const uids = new Set(sameIdentity.map((other) => other.node_uid).filter(Boolean));
    const types = new Set(sameIdentity.map((other) => other.machine_type).filter(Boolean));
    const names = new Set(sameIdentity.map((other) => other.node).filter(Boolean));
    if (uids.size > 1 || types.size > 1 || names.size > 1) continue;
    const key = [...uids][0] || placement.node;
    if (!groups.has(key)) groups.set(key, { number: groups.size + 1, roles: [], placements: [] });
    const group = groups.get(key);
    group.roles.push(role); group.placements.push(placement); hosts[role] = group;
  }
  for (const group of groups.values()) {
    const types = [...new Set(group.placements.map((item) => item.machine_type).filter(Boolean))];
    const families = [...new Set(group.placements.map((item) => placementFamily(item.machine_family).toUpperCase()).filter(Boolean))];
    const type = types.length === 1 ? types[0] : null;
    const capacity = type?.match(/^(c4a|n4a)-(?:standard|highmem|highcpu)-([1-9]\d*)$/i);
    const family = families.length === 1 ? families[0] : "";
    group.label = `VM ${group.number}${family ? ` · ${family}` : ""} · ${capacity && capacity[1].toUpperCase() === family ? `${Number(capacity[2])} vCPUs` : "capacity unreported"}`;
    group.source = `Observed Kubernetes node: ${group.placements[0].node}. Machine type: ${type || "unavailable or inconsistent"}. ${group.placements.map((item) => item.placement_observed_at || item.observed_at).filter(Boolean).join(" · ")}`;
  }
  const workers = Object.keys(roleInfo).map((role) => hosts[role]);
  const workerGroups = new Set(workers.filter(Boolean));
  const allWorkersKnown = workers.every(Boolean);
  const cpuHosts = Object.keys(telemetryRoles).map((role) => hosts[role]);
  const cpuCount = cpuHosts.every(Boolean) ? new Set(cpuHosts).size : null;
  return {
    hosts,
    count: roles.every((role) => hosts[role]) ? groups.size : null,
    cpuCount,
    sharedWorkers: allWorkersKnown && workerGroups.size === 1,
    workerLabel: !allWorkersKnown ? "Engineer VM placement incomplete" : workerGroups.size === 1 ? workers[0].label : `${workerGroups.size} VMs host the engineers`,
    workerDetail: !allWorkersKnown ? "Waiting for verified node identities" : workerGroups.size === 1 ? "Three separate sandboxes share this machine" : "Separate sandboxes · VM shown on each engineer",
  };
}
function renderVmTopology() {
  const topology = currentVmTopology();
  const shared = topology.sharedWorkers;
  boothText("booth-engineer-vm", topology.workerLabel);
  boothText("booth-engineer-vm-detail", topology.workerDetail);
  $("booth-engineer-vm-group").dataset.shared = String(shared);
  document.querySelectorAll("[data-vm-host]").forEach((element) => {
    const role = element.dataset.vmHost;
    const host = topology.hosts[role === "result" ? "manager" : role];
    element.textContent = host ? role === "result" ? `VM ${host.number} · same as manager` : host.label : "VM placement unavailable";
    element.title = host?.source || "A verified Kubernetes node identity is required; no machine is inferred from configured capacity.";
    element.hidden = shared && Boolean(roleInfo[role]);
  });
  const cpuText = topology.cpuCount === null ? "CPU-card VM count unavailable" : `4 CPU cards → ${topology.cpuCount} VM${topology.cpuCount === 1 ? "" : "s"}`;
  boothText("booth-vm-summary", `${topology.count === null ? "Workload VM count unavailable" : `${topology.count} workload VM${topology.count === 1 ? "" : "s"}`} · ${cpuText}`);
  boothText("booth-vm-evidence", state.connected ? "Observed node placement · cards measure workloads, not whole machines" : "Last observed placement · connection unavailable");
  boothText("booth-cpu-workload-map", topology.cpuCount === null ? "Four workload cards · VM placement incomplete" : `Four workload cards · across ${topology.cpuCount} VM${topology.cpuCount === 1 ? "" : "s"}`);
}
function runtimeConfiguration(config = state.config) {
  const chips = [];
  const usable = (value) => typeof value === "string" && value.trim() && !/^(unknown|not[ -](verified|reported|configured|available)|unavailable|pending|n\/a|none|null)$/i.test(value.trim());
  const add = (text, title, tone = "compute") => chips.push({ text, title, tone });
  if (usable(config?.models?.planner)) {
    const model = config.models.planner;
    const label = model.replace(/^gemini-/i, "Gemini ").replace(/-/g, " ").replace(/\b(flash|pro)\b/gi, (word) => word[0].toUpperCase() + word.slice(1).toLowerCase());
    add(`Hosted · ${label}`, `Configured hosted planner: ${model}. Inference runs remotely.`, "hosted");
  }
  const services = Object.keys(roleInfo).map((role) => config?.services?.[role]);
  const ready = services.every((service) => ["ready", "ok", "healthy"].includes(service?.status));
  const models = ready ? [...new Set(services.map((service) => service.model).filter(usable))] : [];
  if (models.length > 1) add("Local workers · mixed models", `Ready worker model aliases: ${models.join("; ")}.`, "local");
  else if (usable(models[0] || config?.models?.worker)) {
    const model = models[0] || config.models.worker;
    const label = model.replace(/^gemma-/i, "Gemma ").replace(/-it(?=-|$)/i, "").replace(/-(Q\d[^-]*)$/i, " · $1").replace(/-/g, " ");
    add(`Local · ${label}`, `${models.length ? "Ready worker model alias" : "Configured local model"}: ${model}.`, "local");
  }
  if (ready && services.every((service) => service.runtime === "llama.cpp" && service.inference_location === "loopback_sidecar")) {
    add("llama.cpp · CPU inference", "The demo model server is configured with zero GPU layers. Ready workers report llama.cpp inference in their own loopback sidecars.", "local");
  }
  const threads = config?.booth?.inference_threads_per_worker;
  if (Number.isInteger(threads) && threads > 0 && threads <= 4096) add(`${threads} inference threads / worker`, `Deployment configuration: ${threads} inference threads per worker. This is not measured CPU usage.`);
  const validator = config?.services?.validator;
  if (["ready", "ok", "healthy"].includes(validator?.status) && validator.isolated_execution === true && validator.runtime === "gvisor") {
    add(/GKE Agent Sandbox/i.test(config?.deployment?.execution_environment || "") ? "GKE Agent Sandbox · gVisor" : "Validator · gVisor", "Ready validator reports isolated execution under gVisor. Worker isolation is defined by the GKE Agent Sandbox deployment.", "sandbox");
  }
  const families = [...new Set(["manager", ...Object.keys(roleInfo), "validator"].map((role) => currentArchitecturePlacement(role, config).family).filter(Boolean))].sort();
  if (families.length) add(`Observed nodes · ${families.join(" + ")}`, "Machine families observed from Kubernetes Node labels; not a performance comparison.");
  return chips.length ? chips : [{ text: "Waiting for runtime configuration", title: "Models and compute details appear when reported by the service.", tone: "compute" }];
}
function renderRuntimeConfiguration() {
  const chips = runtimeConfiguration(), track = $("booth-runtime-track");
  const signature = JSON.stringify(chips);
  if (track.dataset.signature !== signature) {
    // The visual repeat is excluded from accessibility; only one list is read.
    track.replaceChildren(...[false, true].map((repeat) => {
      const list = node("ul", "v8-runtime-items");
      if (repeat) list.setAttribute("aria-hidden", "true");
      for (const chip of chips) {
        const item = node("li", `v8-runtime-chip ${chip.tone}`, chip.text); item.title = chip.title; list.append(item);
      }
      return list;
    }));
    track.dataset.signature = signature;
  }
  $("booth-runtime-strip").dataset.paused = String(state.runtimeStripPaused);
  $("booth-runtime-pause").setAttribute("aria-pressed", String(state.runtimeStripPaused));
  $("booth-runtime-pause").textContent = state.runtimeStripPaused ? "Play" : "Pause";
  $("booth-runtime-pause").setAttribute("aria-label", state.runtimeStripPaused ? "Resume configuration strip" : "Pause configuration strip");
}
function toggleRuntimeConfiguration() {
  state.runtimeStripPaused = !state.runtimeStripPaused;
  renderRuntimeConfiguration();
}
function cpuUsage(sample) {
  const cores = Number.isFinite(sample?.cpu_cores) && sample.cpu_cores >= 0 ? sample.cpu_cores : null;
  const limit = Number.isFinite(sample?.cpu_limit_cores) && sample.cpu_limit_cores > 0 ? sample.cpu_limit_cores : null;
  const percent = cores !== null && limit !== null ? cores / limit * 100 : null;
  return { cores, limit, percent: Number.isFinite(percent) ? percent : null };
}
function workloadTopology(run = state.run, config = state.config, metrics = state.metrics) {
  const unavailable = "Workload usage, not whole-machine usage.";
  if (run && !liveRun(run)) return unavailable;
  const placements = Object.keys(telemetryRoles).map((role) => run
    ? [...(metrics?.samples || [])].reverse().find((sample) => sample.role === role)
    : config?.placement?.[role]);
  if (!placements.every((placement) => (run ? placement?.placement_source : placement?.source) === "kubernetes_node" && placementFamily(placement.node))) return unavailable;
  const count = new Set(placements.map((placement) => placement.node_uid || placement.node)).size;
  const arm = placements.every((placement) => /^(arm64|aarch64)$/i.test(placement.architecture || ""));
  return `Four workloads share ${count} ${arm ? "Arm " : ""}VM${count === 1 ? "" : "s"}. Pod CPU, not whole-machine usage.`;
}
function updatedShopPath(run) {
  if (!liveRun(run) || run.status !== "passed" || !run.preview_url || typeof run.id !== "string" || !/^[a-zA-Z0-9_-]{1,100}$/.test(run.id)) return null;
  // Use the full application shell: the opaque iframe needs its parent to
  // supply genuine quotes. Linking directly to its HTML would break checkout.
  return `/?view=booth&panel=result&run=${encodeURIComponent(run.id)}`;
}
function verifiedBasketReady() {
  return Boolean(updatedShopPath(state.run) && !state.submitting && state.frameRunId === state.run.id
    && state.candidateQuoteStatus === "ready" && Number.isFinite(state.candidateQuote?.total_cents)
    && Number.isFinite(state.baselineQuote?.total_cents));
}
function renderEnvironment() {
  document.body.classList.toggle("archive-preview", archivePreview());
  const completed = liveRun(state.run) && terminalStatuses.has(state.run.status);
  const ready = liveAvailable() && state.connected;
  $("environment-banner").classList.toggle("live", ready || liveRun(state.run));
  $("environment-title").textContent = completed ? (state.run.status === "passed" ? "Completed live run — recorded results" : "Failed live run — recorded results") : state.run ? "Live agent workflow in progress" : ready ? "Live agent workflow ready" : state.config ? "Live connection required" : "Checking live services";
  $("environment-description").textContent = completed
    ? `${state.run.status === "failed" ? "This run failed; no updated application is available. Its error and run evidence are saved." : "The completed application and run evidence are saved; models are not running again."} The CPU cards show current workload usage, separate from the selected run's recorded measurements.`
    : ready || state.run ? "Model calls and worker results come from the configured services. Resource measurements identify their actual source." : liveConnectionReason();
  $("project-label").textContent = state.config?.deployment?.project ? `Target project: ${state.config.deployment.project}` : "";
  $("mode-note").textContent = "Live execution calls hosted Gemini and local-model workers on GKE. Generated changes must pass independent checks.";
  $("live-note").textContent = ready ? "Gemini + GKE agents connected" : `Live unavailable: ${liveConnectionReason()}`;
  ["live", "rehearsal"].forEach((mode) => {
    const button = $(`mode-${mode}`);
    if (!button) return;
    button.hidden = true; button.disabled = true;
    button.classList.toggle("selected", mode === "live");
    button.setAttribute("aria-pressed", String(mode === "live"));
  });
  renderConnectionControl();
  $("prompt").readOnly = archivePreview();
  document.querySelectorAll("#examples button").forEach((button) => { button.disabled = archivePreview(); });
  if (archivePreview()) {
    $("request-title").textContent = "The recorded request";
    $("mode-note").textContent = "Recorded cloud execution · Explore the saved work and measurements below.";
    $("live-note").textContent = state.config.live_reason;
  }
  const resultCaption = $("booth-result-caption");
  if (resultCaption) resultCaption.textContent = archivePreview() ? "Saved response from the verified cloud execution." : "Verified rules, calculated by the actual application.";
  $("architecture-local-caption").textContent = "Local model inference · placement in run evidence";
}
function renderExamples(examples) {
  $("examples").replaceChildren(...examples.slice(0, 3).map((example, index) => {
    const percent = example.match(/\d+%/);
    const threshold = example.match(/\$\d+/);
    const label = percent && threshold ? `${percent[0]} promotion · ${threshold[0]}+` : `Example ${index + 1}`;
    const button = node("button", "example-button", label);
    button.type = "button"; button.title = example;
    button.addEventListener("click", () => { stopBoothTour(); $("prompt").value = example; $("prompt").focus(); });
    return button;
  }));
}
function selectMode(mode) {
  if (mode !== "live" || archivePreview()) return;
  stopBoothTour(); state.mode = "live"; renderRun();
}
function renderAgents(agents = []) {
  const cards = Object.entries(roleInfo).map(([role, info]) => {
    const agent = agents.find((item) => item.role === role) || {};
    const card = node("article", `agent-card ${role}`);
    const top = node("div", "agent-top");
    const avatar = node("div", "agent-avatar", info.icon); avatar.setAttribute("aria-hidden", "true");
    const knownStatus = ["pending", "running", "completed", "failed"].includes(agent.status) ? agent.status : "pending";
    top.append(avatar, node("span", `status-badge ${knownStatus}`, sentenceStatus(knownStatus)));
    card.append(top, node("h3", "", info.name), node("p", "agent-description", info.description));
    const meta = node("dl", "agent-meta");
    const noRun = !state.run;
    const rehearsal = state.run?.mode === "rehearsal";
    const service = state.config?.services?.[role];
    const serviceFamily = ["ready", "ok", "healthy"].includes(service?.status) && typeof service.machine_family === "string" ? service.machine_family.trim() : null;
    // Current readiness can fill a pending card, but cannot rewrite a previous
    // run's placement after workers have moved to different machine families.
    const family = agent.machine_family || ((noRun || !terminalStatuses.has(state.run.status)) ? serviceFamily : null);
    const values = [
      ["Model", rehearsal ? "Offline fixture" : agent.model || (noRun ? "Gemma · planned" : "Not reported")],
      ["Compute", rehearsal ? "Local rehearsal" : family || "Placement unverified"],
      ["Sandbox", rehearsal ? "No GKE execution" : agent.sandbox || (noRun ? "GKE · planned" : "Not reported")],
      ["Elapsed", duration(agent.elapsed_seconds)],
    ];
    values.forEach(([label, value]) => { const group = node("div"); group.append(node("dt", "", label), node("dd", "", value)); meta.append(group); });
    card.append(meta);
    if (agent.summary) card.append(node("p", "agent-summary", agent.summary));
    return card;
  });
  $("agents").replaceChildren(...cards);
}
function renderPlan(plan = []) {
  $("plan-empty").hidden = plan.length > 0;
  $("plan-items").replaceChildren(...plan.map((item) => {
    const el = node("div", "plan-item");
    el.append(node("strong", "", item.title || roleName(item.role)), node("p", "", item.instructions || "No instructions reported."));
    if (Array.isArray(item.files) && item.files.length) { const pills = node("div", "file-pills"); item.files.forEach((file) => pills.append(node("span", "file-pill", file))); el.append(pills); }
    return el;
  }));
}
function renderTests(tests) {
  const badge = $("test-count");
  if (!tests || !Number.isFinite(tests.total) || tests.total === 0) {
    badge.textContent = "Not run"; badge.className = "subtle-badge";
    $("tests-content").replaceChildren(emptyState("✓", "The change must earn its green check", "Protected tests verify the requested behavior before the preview becomes available.")); return;
  }
  badge.textContent = `${number(tests.passed, 0)} / ${number(tests.total, 0)} passed`;
  badge.className = `subtle-badge ${tests.failed > 0 ? "failure" : "success"}`;
  const list = node("ul", "test-list");
  (tests.cases || []).forEach((test) => {
    const li = node("li", "test-case");
    const result = node("span", `test-result${test.passed ? "" : " failed"}`, test.passed ? "✓" : "×");
    result.setAttribute("aria-label", test.passed ? "Passed" : "Failed");
    const info = node("div"); info.append(node("span", "", test.name || "Acceptance check"));
    if (test.detail) info.append(node("p", "", test.detail));
    li.append(result, info); list.append(li);
  });
  if (!(tests.cases || []).length) list.append(node("li", "test-case", "The service reported totals without individual test details."));
  $("tests-content").replaceChildren(list);
}
function renderEvents(events = []) {
  const list = $("events");
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 45;
  const oldTop = list.scrollTop;
  $("event-count").textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
  if (!events.length) { list.replaceChildren(node("li", "timeline-empty", "Run the workflow to see its recorded events.")); return; }
  list.replaceChildren(...events.map((event) => {
    const li = node("li", "timeline-entry");
    const when = new Date(event.timestamp);
    const time = node("time", "", Number.isNaN(when.getTime()) ? "—" : when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
    if (!Number.isNaN(when.getTime())) time.dateTime = when.toISOString();
    li.append(time, node("span", "event-role", event.role ? roleName(event.role) : "Manager"), node("p", "", event.message || event.type || "Event"));
    return li;
  }));
  list.scrollTop = atBottom ? list.scrollHeight : oldTop;
}
function renderPipeline(status) {
  const stages = ["planning", "running", "validating", "passed"];
  const current = stages.indexOf(status);
  document.querySelectorAll(".pipeline [data-stage]").forEach((el, index) => {
    el.classList.toggle("active", index === current);
    el.classList.toggle("done", current > index);
    if (index === current) el.setAttribute("aria-current", "step"); else el.removeAttribute("aria-current");
  });
}
function renderRun() {
  const run = state.run;
  renderEnvironment(); renderAgents(run?.agents || []); renderPlan(run?.plan || []); renderTests(run?.tests); renderEvents(run?.events || []); renderPipeline(run?.status);
  $("run-status").textContent = run ? sentenceStatus(run.status) : "Ready to begin";
  $("run-status").className = `status-badge ${["queued", "planning", "running", "validating", "passed", "failed"].includes(run?.status) ? run.status : "idle"}`;
  renderElapsed();
  $("refresh-artifacts").disabled = !run;
  $("run-context").hidden = !run;
  if (run) {
    const source = run.mode === "rehearsal" ? "Offline rehearsal" : terminalStatuses.has(run.status) ? "Recorded live result" : "Live run";
    $("run-context").replaceChildren(node("strong", "", `${source} · `), document.createTextNode(run.prompt || ""));
    if (run.error) { const error = node("p", "error-message", errorText(run.error)); $("run-context").append(error); }
    const announcement = `${run.id}:${run.status}`;
    if (state.statusAnnouncement !== announcement) { $("live-announcement").textContent = `${source}: ${sentenceStatus(run.status)}.`; state.statusAnnouncement = announcement; }
  }
  renderPreview();
  const inProgress = liveRun(run) && !terminalStatuses.has(run.status);
  $("run-button").disabled = archivePreview() || !liveAvailable() || !state.connected || state.submitting || Boolean(inProgress);
  $("run-history").disabled = state.submitting || Boolean(inProgress);
  $("run-button").firstElementChild.textContent = archivePreview() ? "Recorded preview" : state.submitting ? "Starting…" : inProgress ? "Workflow in progress" : !liveAvailable() || !state.connected ? "Live connection required" : "Run live agents";
  renderBooth();
}
function renderHistory() {
  const selected = state.run?.id || "";
  const options = state.runs.filter(liveRun).map((run) => {
    const date = new Date(run.created_at);
    const time = Number.isNaN(date.getTime()) ? String(run.id).slice(0, 8) : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const source = terminalStatuses.has(run.status) ? "Recorded live" : "Live";
    const option = node("option", "", `${time} · ${source} · ${sentenceStatus(run.status)}`); option.value = String(run.id); return option;
  });
  if (!archivePreview() || !options.length) {
    const fresh = node("option", "", archivePreview() ? "No recorded runs" : "New request");
    fresh.value = ""; options.unshift(fresh);
  }
  $("run-history").replaceChildren(...options); $("run-history").value = selected;
}
function recordRun(run) {
  if (!liveRun(run)) return;
  const index = state.runs.findIndex((item) => item.id === run.id);
  if (index < 0) state.runs.unshift(run); else state.runs[index] = run;
  renderHistory();
}
function renderMetrics(metrics) {
  state.metrics = metrics;
  const sources = { kubernetes: "Kubernetes metrics", local_process: "Local process", unavailable: "Unavailable" };
  const source = metrics?.source || "unavailable";
  $("metrics-source").textContent = sources[source] || String(source);
  const samples = Array.isArray(metrics?.samples) ? metrics.samples : [];
  if (source === "unavailable" || !samples.length) {
    const title = state.run?.mode === "rehearsal" ? "No Axion telemetry in offline rehearsal" : "No measured resource data yet";
    $("metrics-content").replaceChildren(emptyState("▥", title, metrics?.note || "CPU and memory appear only when a real telemetry source is available."));
  } else {
    const latest = new Map(); samples.forEach((sample) => latest.set(sample.role || "worker", sample));
    const rows = [...latest].map(([role, sample]) => {
      const row = node("div", "metric-row");
      row.dataset.role = role;
      const title = node("div", "metric-row-title");
      const date = new Date(sample.timestamp);
      title.append(node("span", "", metricRoleName(role)), node("span", "", Number.isNaN(date.getTime()) ? "Timestamp not reported" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })));
      const values = node("div", "metric-values");
      const cpu = node("div", "metric-value"); cpu.dataset.testid = `cpu-${role}`;
      const cpuPeriod = sample.window ? `Averaged over ${sample.window}` : sample.cpu_source === "kubelet_summary_pod_cpu" ? "Pod CPU sample · window unspecified" : "Measurement window not reported";
      cpu.append(node("strong", "", Number.isFinite(sample.cpu_cores) ? `${number(sample.cpu_cores, 3)} CPU cores` : "Not measured"), node("span", "", Number.isFinite(sample.cpu_limit_cores) ? `of ${number(sample.cpu_limit_cores)} cores in declared container limits` : cpuPeriod));
      const ram = node("div", "metric-value"); ram.dataset.testid = `memory-${role}`;
      ram.append(node("strong", "", memory(sample.memory_bytes)), node("span", "", Number.isFinite(sample.memory_limit_bytes) ? `of ${memory(sample.memory_limit_bytes)} allocated` : sample.memory_source === "kubelet_summary_pod_working_set" ? "Pod working set · includes overhead" : "Memory source in recorded evidence"));
      ram.title = sample.memory_timestamp ? `Memory measured at ${sample.memory_timestamp}` : "Memory timestamp not reported";
      [[cpu, sample.cpu_cores, sample.cpu_limit_cores], [ram, sample.memory_bytes, sample.memory_limit_bytes]].forEach(([element, value, limit]) => {
        if (Number.isFinite(value) && Number.isFinite(limit) && limit > 0) { const track = node("div", "metric-track"); const fill = node("span"); fill.style.width = `${Math.min(100, Math.max(0, value / limit * 100))}%`; track.append(fill); element.append(track); }
      });
      const observed = samples.filter((item) => (item.role || "worker") === role && Number.isFinite(item.cpu_cores)).map((item) => item.cpu_cores);
      const peak = node("span", "booth-only metric-observed", observed.length ? `Highest sampled CPU use: ${number(Math.max(...observed), 2)} cores` : "CPU use was not measured");
      values.append(cpu, ram); row.append(title, values, peak); return row;
    });
    $("metrics-content").replaceChildren(...rows);
  }
  $("metrics-note").textContent = `${metrics?.note || ""}${metrics?.note ? " " : ""}Gemini API inference runs remotely and is separate from coordinator and worker resource usage.`;
  renderBooth();
}
function renderProducts() {
  $("products").replaceChildren(...state.catalog.map((product) => {
    const item = node("article", "product");
    const icon = node("div", "product-illustration", product.emoji || "✦"); icon.setAttribute("aria-hidden", "true");
    const bottom = node("div", "product-bottom"); const add = node("button", "add-button", "+"); add.type = "button"; add.setAttribute("aria-label", `Add ${product.name} to basket`); add.addEventListener("click", () => updateQuantity(product.sku, 1));
    bottom.append(node("span", "", money(product.price_cents)), add);
    item.append(icon, node("h4", "", product.name), node("p", "product-category", product.category), bottom); item.title = product.description || ""; return item;
  }));
}
function resetCart() {
  state.cart.clear();
  const defaults = ["HEADPHONES", "TOTE"];
  defaults.forEach((sku) => { if (state.catalog.some((p) => p.sku === sku)) state.cart.set(sku, 1); });
  if (!state.cart.size && state.catalog[0]) state.cart.set(state.catalog[0].sku, 1);
  renderBasket(); refreshQuotes();
}
function updateQuantity(sku, change) {
  const quantity = Math.max(0, Math.min(20, (state.cart.get(sku) || 0) + change));
  if (quantity === 0) state.cart.delete(sku); else state.cart.set(sku, quantity);
  renderBasket(); refreshQuotes();
}
function renderBasket() {
  const count = [...state.cart.values()].reduce((total, quantity) => total + quantity, 0);
  $("cart-count").textContent = `${count} item${count === 1 ? "" : "s"}`;
  if (!count) { $("basket").replaceChildren(node("p", "muted", "Your basket is empty. Add a product to try the checkout.")); return; }
  $("basket").replaceChildren(...[...state.cart].map(([sku, quantity]) => {
    const product = state.catalog.find((item) => item.sku === sku);
    const row = node("div", "basket-row");
    row.append(node("span", "basket-name", product?.name || sku));
    const control = node("div", "quantity-control");
    const minus = node("button", "", "−"); minus.type = "button"; minus.setAttribute("aria-label", `Remove one ${product?.name || sku}`); minus.addEventListener("click", () => updateQuantity(sku, -1));
    const plus = node("button", "", "+"); plus.type = "button"; plus.disabled = quantity >= 20; plus.setAttribute("aria-label", `Add one ${product?.name || sku}`); plus.addEventListener("click", () => updateQuantity(sku, 1));
    control.append(minus, node("span", "", quantity), plus); row.append(control, node("span", "basket-price", money(product?.price_cents * quantity))); return row;
  }));
}
function renderBaselineQuote(quote) {
  state.baselineQuote = quote;
  const lines = [["Subtotal", quote.subtotal_cents], ["Savings", quote.discount_cents ? -quote.discount_cents : 0], ["Delivery", quote.shipping_cents], ["Total", quote.total_cents]];
  const elements = lines.map(([label, value], index) => { const line = node("div", `quote-line${index === 3 ? " total" : ""}`); line.append(node("span", "", label), node("strong", "", money(value))); return line; });
  elements.push(node("p", "baseline-description", "Starting rules: no promotion · $7.99 delivery on nonempty carts"));
  $("baseline-quote").replaceChildren(...elements);
}
function postQuote() {
  if (state.candidateQuote && state.frameRunId === state.run?.id && liveRun(state.run) && state.run.status === "passed") {
    // The sandboxed document has an opaque origin. Sending to * is required;
    // the recipient is this specific iframe and the payload contains no secrets.
    $("candidate-preview").contentWindow?.postMessage({ type: "quote", quote: state.candidateQuote }, "*");
  }
}
function renderPreview() {
  const run = state.run;
  const verified = liveRun(run) && run.status === "passed" && Boolean(run.preview_url);
  const ready = verified && state.candidateQuoteStatus === "ready" && state.frameRunId === run.id;
  $("preview-empty").hidden = ready;
  $("candidate-preview").hidden = !ready;
  $("candidate-preview").setAttribute("aria-busy", String(verified && state.candidateQuoteStatus === "loading"));
  const placeholder = $("preview-empty");
  placeholder.querySelector("strong").textContent = run?.status === "failed" ? "Updated application unavailable" : !verified ? "The next version starts with your brief." : state.candidateQuoteStatus === "error" ? "Checkout calculation unavailable" : "Calculating this basket…";
  placeholder.querySelector("p").textContent = run?.status === "failed" ? planningFailed(run) ? "Planning failed before the specialists or validation started. Review the recorded error before retrying." : "This run failed and did not publish an updated application. Review its recorded error." : !verified ? "Once the combined change passes acceptance tests, the updated checkout appears here." : state.candidateQuoteStatus === "error" ? "The previous totals are hidden. Change the basket or reset it to try again." : "The verified application is calculating fresh totals for your selected items.";
  $("preview-status").textContent = verified ? (run.mode === "rehearsal" ? "Verified fixture" : "Verified change") : run?.status === "failed" ? "Not verified" : "Awaiting verification";
  $("preview-status").className = `subtle-badge${verified ? " success" : ""}`;
  if (!verified) {
    if (state.frameRunId) { $("candidate-preview").removeAttribute("src"); state.frameRunId = null; state.candidateQuote = null; state.candidateQuoteStatus = "idle"; }
    showError("quote-error", "");
    return;
  }
  if (state.frameRunId !== run.id) {
    state.frameRunId = run.id; state.candidateQuote = null; state.candidateQuoteStatus = "loading";
    // Build this path locally. Never navigate the iframe to a URL supplied by a model.
    $("candidate-preview").src = `/preview/${encodeURIComponent(run.id)}/`;
    refreshQuotes();
  }
}
async function refreshQuotes() {
  if (!state.catalog.length) return;
  const seq = ++state.quoteSeq;
  const run = state.run;
  const items = [...state.cart].map(([sku, quantity]) => ({ sku, quantity }));
  const body = JSON.stringify({ items });
  showError("quote-error", "");
  const verified = liveRun(run) && run.status === "passed" && Boolean(run.preview_url);
  state.baselineQuote = null;
  state.baselineQuoteStatus = "loading";
  state.candidateQuote = null;
  state.candidateQuoteStatus = verified ? "loading" : "idle";
  $("baseline-quote").replaceChildren(node("p", "muted", "Calculating this basket…"));
  renderPreview();
  renderBooth();
  const baseline = api("/api/baseline/quote", { method: "POST", body });
  const candidate = verified ? api(`/api/runs/${encodeURIComponent(run.id)}/quote`, { method: "POST", body }) : Promise.resolve(null);
  const results = await Promise.allSettled([baseline, candidate]);
  if (seq !== state.quoteSeq) return;
  if (results[0].status === "fulfilled") { state.baselineQuoteStatus = "ready"; renderBaselineQuote(results[0].value.quote || results[0].value); }
  else { state.baselineQuoteStatus = "error"; $("baseline-quote").replaceChildren(node("p", "muted", `Checkout unavailable: ${results[0].reason.message}`)); }
  if (run?.id !== state.run?.id) return;
  if (results[1].status === "fulfilled" && results[1].value) { state.candidateQuote = results[1].value.quote || results[1].value; state.candidateQuoteStatus = "ready"; postQuote(); }
  else if (results[1].status === "rejected") { state.candidateQuote = null; state.candidateQuoteStatus = "error"; showError("quote-error", results[1].reason.message); }
  renderPreview();
  renderBooth();
}
function artifactEntries(data) {
  const collection = data?.artifacts || data?.files || data;
  if (Array.isArray(collection)) return collection.map((entry, i) => ({ name: entry.path || entry.name || entry.filename || `Artifact ${i + 1}`, content: entry.diff ?? entry.content ?? entry.text ?? entry }));
  if (collection && typeof collection === "object") return Object.entries(collection).filter(([key]) => !["run_id", "id", "mode", "note", "status"].includes(key)).map(([name, value]) => ({ name, content: typeof value === "object" && value !== null ? value.diff ?? value.content ?? value.text ?? value : value }));
  return [];
}
async function refreshArtifacts(force = false) {
  const runId = state.run?.id;
  if (!runId || (!force && state.artifactsRunId === runId)) return;
  try {
    const data = await api(`/api/runs/${encodeURIComponent(runId)}/artifacts`);
    if (state.run?.id !== runId) return;
    const entries = artifactEntries(data);
    if (!entries.length) { $("artifacts").replaceChildren(emptyState("⟨/⟩", "No source artifacts available yet", "Source files and diffs will appear as the workflow produces them.")); return; }
    const note = node("p", "artifact-note", state.run.mode === "rehearsal" ? "These changes are deterministic rehearsal fixtures, not AI-generated work." : "These artifacts are returned by the workflow. Inspect the source alongside the test evidence.");
    const details = entries.map(({ name, content }) => { const el = node("details", "artifact"); el.append(node("summary", "", name), node("pre", "", typeof content === "string" ? content : JSON.stringify(content, null, 2))); return el; });
    $("artifacts").replaceChildren(note, ...details);
    if (terminalStatuses.has(state.run.status)) state.artifactsRunId = runId;
  } catch (error) { if (state.run?.id === runId) $("artifacts").replaceChildren(node("p", "muted", `Artifacts unavailable: ${error.message}`)); }
}
async function refreshRun() {
  const runId = state.run?.id;
  if (!runId) return;
  const results = await Promise.allSettled([api(`/api/runs/${encodeURIComponent(runId)}`), api(`/api/runs/${encodeURIComponent(runId)}/metrics`)]);
  if (state.run?.id !== runId) return;
  if (results[0].status === "fulfilled") {
    const received = results[0].value.run || results[0].value;
    if (!liveRun(received)) {
      state.run = null; state.quoteSeq += 1;
      renderMetrics(null); renderRun(); renderHistory();
      showError("request-error", unsupportedRunMessage());
      return;
    }
    state.run = received;
    renderRun(); recordRun(state.run);
    if (terminalStatuses.has(state.run.status)) refreshArtifacts();
  } else { setConnection(false); showError("request-error", `Unable to refresh this run: ${results[0].reason.message}`); }
  if (results[1].status === "fulfilled") renderMetrics(results[1].value);
  else renderMetrics({ source: "unavailable", samples: [], note: `Metrics unavailable: ${results[1].reason.message}` });
  schedulePoll();
}
function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (liveRun(state.run) && !terminalStatuses.has(state.run.status)) state.pollTimer = setTimeout(refreshRun, 1500);
}
async function selectRun(id) {
  stopBoothTour();
  clearTimeout(state.pollTimer);
  showError("request-error", "");
  // Fetch unknown IDs before showing any active state. An invalid result link
  // must not invent a queued run or disable the request controls indefinitely.
  let selected = state.runs.find((run) => run.id === id);
  if (id && !selected) {
    try { const response = await api(`/api/runs/${encodeURIComponent(id)}`); selected = response.run || response; }
    catch (error) { showError("request-error", `Unable to open this run: ${error.message}`); schedulePoll(); return; }
  }
  if (selected && !liveRun(selected)) {
    showError("request-error", unsupportedRunMessage());
    renderHistory(); schedulePoll(); return;
  }
  state.run = selected || null;
  state.quoteSeq += 1;
  state.frameRunId = null;
  $("candidate-preview").removeAttribute("src");
  state.candidateQuote = null; state.candidateQuoteStatus = "idle";
  state.artifactsRunId = null;
  const url = new URL(window.location.href);
  if (id) url.searchParams.set("run", id); else url.searchParams.delete("run");
  window.history.replaceState(null, "", url);
  if (!id) {
    renderRun(); renderHistory(); renderMetrics(null); refreshQuotes();
    $("artifacts").replaceChildren(emptyState("⟨/⟩", "No run selected", "Submit a request to start a new workflow."));
    return;
  }
  $("artifacts").replaceChildren(emptyState("⟨/⟩", "Loading source evidence", "Retrieving the artifacts for this run."));
  renderRun(); renderMetrics(null); await refreshRun(); await refreshArtifacts(true);
}
async function startRun() {
  // Only the explicit Run action creates work. Navigation and autoplay never do.
  if (archivePreview() || state.submitting || (liveRun(state.run) && activeStatuses.has(state.run.status))) return;
  state.mode = "live";
  if (!liveAvailable() || !state.connected) {
    showError("request-error", `Live execution is unavailable. ${liveConnectionReason()}`);
    renderRun(); scheduleConnectionCheck(); return;
  }
  const prompt = $("prompt").value.trim();
  if (!prompt) { showError("request-error", "Enter a business requirement before starting the workflow."); $("prompt").focus(); return; }
  stopBoothTour();
  if (state.booth.enabled) setBoothPanel("team", { automatic: true });
  state.submitting = true; showError("request-error", ""); renderRun();
  try {
    const response = await api("/api/runs", { method: "POST", body: JSON.stringify({ prompt, mode: "live" }) });
    const run = response.run || response;
    if (!run.id) throw new Error("The service did not return a run identifier.");
    if (!liveRun(run)) throw new Error(unsupportedRunMessage());
    state.run = run; state.artifactsRunId = null;
    const url = new URL(window.location.href); url.searchParams.set("run", run.id);
    window.history.replaceState(null, "", url);
    $("artifacts").replaceChildren(emptyState("⟨/⟩", "Waiting for source changes", "The workflow will publish its available files and diffs here."));
    renderMetrics(null); recordRun(run); renderRun(); refreshRun();
  } catch (error) { showError("request-error", error.message); }
  finally { state.submitting = false; renderRun(); }
}

function recordedLiveResult() { return state.run?.mode === "live" && state.run?.status === "passed"; }
function boothLabel() {
  if (!state.connected && state.config) return "Connection unavailable · last received state";
  if (archivePreview()) return "Archived preview · recorded cloud execution";
  if (!state.run) return liveAvailable() ? "Live services ready · submit a request" : "Live connection required · no workflow running";
  if (!liveRun(state.run)) return "Unsupported result · live execution required";
  if (state.run.status === "passed") return "Completed live run · recorded results";
  if (state.run.status === "failed") return "Failed live run · recorded results";
  return "Live workflow · execution in progress";
}
function boothText(id, text) { if ($(id)) $(id).textContent = text; }
function moveBoothEvidence(enabled) {
  // Reuse the genuine checkout and telemetry components. No duplicated or
  // simulated preview is introduced by the visitor experience.
  if (!boothPlacements.length) {
    [[".shop-section", "booth-shop-mount"], [".metrics-card", "booth-metrics-mount"], [".tests-card", "booth-tests-mount"], [".request-section", "booth-request-mount"], [".run-controls", "booth-run-controls-mount"], ["#connection-status", "booth-connection-mount"]].forEach(([selector, mountId]) => {
      const element = document.querySelector(selector), mount = $(mountId);
      if (!element || !mount) return;
      const marker = document.createComment(`original ${selector} position`);
      element.before(marker); boothPlacements.push({ element, mount, marker });
    });
  }
  boothPlacements.forEach(({ element, mount, marker }) => {
    if (enabled && element.parentNode !== mount) mount.append(element);
    else if (!enabled && element.parentNode !== marker.parentNode) marker.after(element);
  });
}
function syncBoothPanel() {
  document.body.dataset.boothPanel = state.booth.panel;
  document.querySelectorAll("button[data-booth-panel]").forEach((button) => {
    const active = button.dataset.boothPanel === state.booth.panel;
    button.classList.toggle("selected", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  ["team", "result"].forEach((name) => {
    const panel = $(`booth-${name}-view`);
    if (panel) panel.hidden = name !== state.booth.panel;
  });
}
function setBoothPanel(panel, { automatic = false, focus = false } = {}) {
  if (!["team", "result"].includes(panel)) return;
  if (!automatic) stopBoothTour();
  state.booth.panel = panel; syncBoothPanel();
  const url = new URL(window.location.href);
  if (panel === "result") url.searchParams.set("panel", "result"); else url.searchParams.delete("panel");
  window.history.replaceState(null, "", url);
  if (focus) document.querySelector(`button[data-booth-panel="${panel}"]`)?.focus();
  // Re-send the genuine quote when the opaque-origin frame becomes visible.
  if (panel === "result") requestAnimationFrame(postQuote);
}
function renderBoothInspector() {
  const run = state.run, key = state.booth.inspect;
  const agent = (run?.agents || []).find((item) => item.role === key);
  const plan = (run?.plan || []).find((item) => item.role === key);
  const recorded = recordedLiveResult();
  let kicker, title, copy, facts = [];
  if (roleInfo[key]) {
    kicker = "Local AI specialist"; title = roleName(key);
    copy = ({backend: "Writes the pricing logic that turns the request into working discount and delivery rules.", frontend: "Builds the checkout interface so customers can see their savings and delivery charge.", qa: "Writes additional tests for the combined change. A separate validator runs those tests alongside protected acceptance checks."})[key];
    const sample = [...(state.metrics?.samples || [])].reverse().find((item) => item.role === key);
    facts = [
      ["Model", run?.mode === "rehearsal" ? "Offline fixture" : agent?.model || "Not reported"],
      ["Recorded run placement", run?.mode === "rehearsal" ? "No cloud inference" : agent?.machine_family ? `${/^(c4a|n4a)$/i.test(agent.machine_family) ? "Arm · " : ""}${String(agent.machine_family).toUpperCase()}` : "Placement unverified"],
      ["Current placement", currentArchitecturePlacement(key).label],
      ["Recorded work", agent?.elapsed_seconds === undefined ? "Not reported" : duration(agent.elapsed_seconds)],
      ["Own workspace", plan?.files?.length ? plan.files.join(", ") : "Not reported"],
    ];
    if (sample && Number.isFinite(sample.memory_bytes)) facts.push(["Recorded run memory", memory(sample.memory_bytes)]);
  } else if (key === "manager") {
    kicker = "Hosted planning"; title = "Gemini makes the plan.";
    copy = "Hosted Gemini uses the request and starter code to assign work, then summarizes worker reports and test results. The coordination service runs on Arm; Gemini inference runs externally.";
    facts = [["Configured planner", state.config?.models?.planner || "Not reported"], ["Recorded assignments", run?.plan?.length ? String(run.plan.length) : "Not reported"], ["Execution", "Local Gemma in GKE Sandboxes"]];
  } else if (key === "coordinator") {
    kicker = "Arm coordination"; title = "The service that keeps the team moving.";
    copy = "The coordinator calls Gemini, gives each specialist its code and instructions, collects their files, and sends the combined change for independent validation. Gemma runs on the specialist CPUs; the independent validator executes the generated code and tests.";
    const placement = architecturePlacement("coordinator");
    const sample = [...(state.metrics?.samples || [])].reverse().find((item) => item.role === "manager");
    const usage = cpuUsage(sample);
    facts = [["Responsibility", "Dispatch · collect · validate · publish"], ["Current placement", currentArchitecturePlacement("coordinator").label], ["Recorded placement", run ? placement.label : "No run selected"], ["Recorded coordinator Pod CPU", usage.percent === null ? "Not measured with a reported limit" : `${number(usage.percent)}% · ${number(usage.cores, 3)} / ${number(usage.limit, 2)} cores`], ["Model inference", "Hosted Gemini is external; it is not included in this Pod's CPU"]];
  } else if (key === "validator") {
    kicker = "Independent verification"; title = "The change has to pass.";
    copy = "A separate isolated validator combines the work and checks the actual behavior. Protected acceptance tests and the QA agent’s tests must pass before the updated checkout is available.";
    facts = [["Result", run?.tests?.total ? `${run.tests.passed} / ${run.tests.total} checks passed` : "Not reported"], ["Current placement", currentArchitecturePlacement("validator").label], ["Isolation", "Separate GKE Agent Sandbox"], ["Preview rule", "Only after successful validation"]];
  } else if (key === "result") {
    kicker = "Try the result"; title = "Same shop. New behavior.";
    copy = "Change the basket and compare the original rules with the verified application. These totals are calculated by the actual preserved code inside the isolated validator.";
    facts = [["Before", money(state.baselineQuote?.total_cents)], ["After", verifiedBasketReady() ? money(state.candidateQuote?.total_cents) : "Awaiting calculation"]];
    if (archivePreview()) copy = "This preview displays saved cloud responses for recorded baskets. The generated checkout is isolated in the browser; its pricing code is not executing locally.";
    if (!updatedShopPath(run)) {
      title = "Updated application unavailable";
      copy = planningFailed(run) ? "Planning failed before the specialists or validation started. This request produced no updated application." : run?.status === "failed" ? "This run failed and did not publish an updated application. Previously accepted applications remain separate saved runs." : "The updated application becomes available after this request passes independent validation.";
      facts = [["Selected run", run?.id || "No run selected"], ["Validation", run?.tests?.total ? `${run.tests.passed} / ${run.tests.total} checks passed` : planningFailed(run) ? "Not started" : "No results reported"]];
    }
  } else {
    kicker = "Start here"; title = "One request sets the team in motion.";
    copy = run?.prompt || "Give a prepared shop a new discount and delivery rule. The manager divides the work between three specialists.";
    facts = [["Starting point", "An existing sample shop"], ["What changes", "Pricing, checkout and tests"], ["Evidence", recorded ? "Completed real execution" : boothLabel()]];
  }
  if (run?.mode === "rehearsal") {
    if (key === "manager" || key === "coordinator") {
      kicker = "Offline rehearsal"; title = "A prepared plan, without model calls.";
      copy = "This rehearsal uses deterministic fixtures to demonstrate the workflow. Hosted Gemini is not called and no Arm cloud execution is demonstrated.";
      facts = [["Planning", "Trusted rehearsal fixture"], ["Model inference", "None"]];
    } else if (key === "validator") {
      kicker = "Offline rehearsal checks"; title = "Fixture checks demonstrate the gate.";
      copy = "These results belong to the local rehearsal. They exercise the demo flow and do not prove execution in GKE or a cloud sandbox.";
      facts = [["Recorded checks", run.tests?.total ? `${run.tests.passed} / ${run.tests.total} passed` : "Not reported"], ["Environment", "Local rehearsal"]];
    } else if (key === "result") {
      copy = "Try the prepared rehearsal checkout. Its totals come from trusted local fixtures, not AI-generated code executing in a cloud sandbox.";
    } else if (roleInfo[key]) {
      copy = "This role uses a trusted offline fixture in the selected rehearsal. No Gemma inference or cloud sandbox execution is demonstrated.";
    }
  } else if (!run || run.mode !== "live") {
    kicker = `${kicker} · architecture`;
    facts.push(["Evidence", "Submit a request to begin; no execution claimed yet"]);
  }
  boothText("booth-inspector-kicker", kicker); boothText("booth-inspector-title", title); boothText("booth-inspector-copy", copy);
  const container = $("booth-inspector-facts");
  if (container) container.replaceChildren(...facts.map(([label, value]) => {
    const item = node("div", "inspector-fact"); item.append(node("dt", "", label), node("dd", "", value)); return item;
  }));
  document.querySelectorAll("button[data-inspect]").forEach((button) => {
    const active = button.dataset.inspect === key;
    button.classList.toggle("is-selected", active); button.setAttribute("aria-pressed", String(active));
  });
}
function selectBoothNode(key) {
  const step = { request: 0, manager: 1, coordinator: 1, backend: 2, frontend: 2, qa: 2, validator: 3, result: 4 }[key];
  if (step === undefined) return;
  stopBoothTour(); state.booth.inspect = key; state.booth.step = step;
  setBoothPanel(key === "result" ? "result" : "team", { automatic: true, focus: key === "result" }); renderBooth();
  if (key !== "result" && $("booth-inspector-disclosure")) $("booth-inspector-disclosure").open = true;
}
function cpuSparkline(samples, role) {
  const observations = samples.map((sample) => ({ time: Date.parse(sample.cpu_timestamp || sample.timestamp), percent: cpuUsage(sample).percent }));
  const valid = observations.filter((sample) => sample.percent !== null && Number.isFinite(sample.time));
  if (!valid.length) return node("p", "metric-chart-empty", "Awaiting measured CPU and Pod limits");
  const times = observations.filter((sample) => Number.isFinite(sample.time)).map((sample) => sample.time);
  const start = Math.min(...times), span = Math.max(...times) - start || 1;
  const peak = Math.max(...valid.map((sample) => sample.percent));
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 240 58"); svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img"); svg.setAttribute("aria-label", `${metricRoleName(role)} sampled CPU history; shared 0–100% of Pod CPU limit scale, highest observed ${number(peak, 1)}%.${peak > 100 ? " Values above 100% are clipped in the chart only." : ""} Missing measurements leave gaps.`);
  svg.classList.add("metric-chart");
  let segment = [];
  const drawSegment = () => {
    if (!segment.length) return;
    const points = segment.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
    if (segment.length === 1) {
      const dot = document.createElementNS(svg.namespaceURI, "circle");
      dot.setAttribute("cx", segment[0][0].toFixed(2)); dot.setAttribute("cy", segment[0][1].toFixed(2)); dot.setAttribute("r", "2"); dot.setAttribute("fill", "currentColor"); svg.append(dot);
    } else {
      const area = document.createElementNS(svg.namespaceURI, "polygon");
      area.setAttribute("points", `${segment[0][0].toFixed(2)},52 ${points} ${segment.at(-1)[0].toFixed(2)},52`); area.setAttribute("fill", "currentColor"); area.setAttribute("opacity", ".12");
      const line = document.createElementNS(svg.namespaceURI, "polyline");
      line.setAttribute("points", points); line.setAttribute("fill", "none"); line.setAttribute("stroke", "currentColor"); line.setAttribute("stroke-width", "2"); line.setAttribute("vector-effect", "non-scaling-stroke"); svg.append(area, line);
    }
    segment = [];
  };
  for (const sample of observations) {
    if (sample.percent === null || !Number.isFinite(sample.time)) { drawSegment(); continue; }
    segment.push([4 + (sample.time - start) / span * 232, 52 - Math.min(100, sample.percent) / 100 * 46]);
  }
  drawSegment(); return svg;
}
function renderBoothOverview() {
  const run = state.run, rehearsal = run?.mode === "rehearsal";
  const samples = rehearsal || archivePreview() ? [] : state.currentMetrics?.samples || [];
  const history = rehearsal || archivePreview() ? [] : state.currentMetricHistory;
  const stages = workflowStages(run);
  const standby = state.connected && liveAvailable() ? "Ready" : state.config ? "Connection required" : "Checking services";
  boothText("booth-overview-status", run ? sentenceStatus(run.status) : standby);
  boothText("booth-workload-topology", currentWorkloadTopology());
  renderVmTopology();
  renderRuntimeConfiguration();
  const fresh = samples.filter((sample) => !currentCpuState(sample).unavailable && cpuUsage(sample).cores !== null);
  const latestTime = fresh.length ? Math.max(...fresh.map((sample) => currentCpuState(sample).timestamp)) : null;
  boothText("booth-cpu-observation", state.currentMetricsError || !state.connected || state.currentMetrics?.source === "unavailable" ? "Current CPU unavailable" : latestTime !== null ? `CPU sampled ${new Date(latestTime).toLocaleTimeString()}` : "Waiting for a current CPU sample");
  if ($("booth-cpu-observation")) $("booth-cpu-observation").title = state.currentMetricsError || state.currentMetrics?.note || "These are current workload samples, separate from the selected run's saved measurements. Measurement timestamps are not the time this page refreshed.";
  boothText("booth-overview-duration", duration(elapsedForRun(run)));
  boothText("booth-current-stage", !state.connected && state.config ? "Connection lost · waiting for a fresh workflow status" : !run && !liveAvailable() ? "Live connection required · no workflow running" : workflowActivity(run, stages));
  boothText("booth-coordinator-status", run && !state.connected ? "Status unavailable" : stages.coordinator === "running" ? "Coordinating" : stages.coordinator === "completed" ? "Complete" : stages.coordinator === "failed" ? "Stopped" : standby);
  document.body.dataset.executionMode = !state.connected ? "disconnected" : rehearsal ? "rehearsal" : activeStatuses.has(run?.status) ? "live" : run ? "recorded" : "idle";
  document.body.dataset.workflowActive = String(state.connected && activeStatuses.has(run?.status));
  document.querySelectorAll("[data-inspect]").forEach((element) => {
    const key = element.dataset.inspect;
    element.dataset.execution = stages[key] || "idle";
    if (stages[key] === "running") element.setAttribute("aria-current", "step"); else element.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-placement]").forEach((element) => {
    const placement = currentArchitecturePlacement(element.dataset.placement);
    element.textContent = placement.label;
    element.title = placement.source;
  });
  document.querySelectorAll("[data-flow-stage]").forEach((element) => {
    element.dataset.execution = stages[element.dataset.flowStage] || "idle";
  });
  const shopLink = $("booth-open-shop-link"), path = updatedShopPath(run);
  if (shopLink) {
    shopLink.hidden = !path || state.submitting;
    if (path) shopLink.setAttribute("href", path); else shopLink.removeAttribute("href");
  }
  if ($("booth-cta-result")) {
    $("booth-cta-result").disabled = !path || state.submitting;
    $("booth-cta-result").hidden = !path || state.submitting;
  }
  const basketReady = verifiedBasketReady();
  $("booth-verified-basket").hidden = !basketReady;
  $("booth-verified-basket").disabled = !basketReady;
  boothText("booth-result-description", run?.status === "failed" ? "This request has no updated application. The original shop remains available below." : path ? "Change the basket to explore this accepted application's discount and delivery rules." : "Explore the original shop. An updated checkout appears only after validation passes.");
  const metrics = $("booth-overview-metrics");
  const vmHosts = currentVmTopology().hosts;
  const expandedMetrics = new Set([...(metrics?.children || [])].filter((card) => card.querySelector("details")?.open).map((card) => card.dataset.role));
  if (metrics) metrics.replaceChildren(...Object.entries(telemetryRoles).map(([role, name]) => {
    const observations = (history.length ? history : samples).filter((sample) => sample.role === role);
    const measured = observations.map(cpuUsage).filter((usage) => usage.percent !== null);
    const latest = samples.find((sample) => sample.role === role);
    const { usage, stale, unavailable } = currentCpuState(latest);
    const observedUsage = cpuUsage(latest);
    const placement = currentArchitecturePlacement(role === "manager" ? "coordinator" : role);
    const family = rehearsal ? "LOCAL REHEARSAL" : placement.family ? placement.label : "PLACEMENT NOT REPORTED";
    const peak = measured.length ? Math.max(...measured.map((sample) => sample.percent)) : null;
    const card = node("article", `booth-overview-metric ${role}`); card.dataset.role = role; card.dataset.stale = String(stale || unavailable && observations.length > 0);
    card.append(node("span", "v3-metric-placement", `${family}${!rehearsal && vmHosts[role] ? ` · VM ${vmHosts[role].number}` : ""}`), node("h3", "", name));
    if (stale || unavailable && observations.length > 0) card.append(node("span", "v3-metric-stale", stale ? "Stale sample" : "Updates unavailable"));
    const value = node("div", "metric-value v3-metric-current");
    value.append(node("strong", "", usage.percent !== null ? `${number(usage.percent, 1)}%` : "—"), node("span", "", `${stale ? "stale sample · " : ""}${usage.percent !== null ? "of Pod CPU limit" : "CPU % unavailable"}`));
    card.append(value);
    const percent = node("p", "v3-metric-percent", usage.cores !== null ? `${number(usage.cores, 3)} / ${usage.limit !== null ? number(usage.limit, 2) : "unreported limit"} cores` : "CPU not measured");
    if (rehearsal) percent.textContent = "No cloud utilization in rehearsal";
    card.append(percent);
    card.append(rehearsal ? node("p", "metric-chart-empty", "No inference in rehearsal") : cpuSparkline(observations, role));
    const details = node("details", "metric-details"); details.open = expandedMetrics.has(role);
    details.append(node("summary", "", "Measurement details"));
    const meta = node("div", "metric-meta");
    const evidence = [
      role === "manager" ? "Arm coordinator Pod only; hosted Gemini inference runs externally" : `${roleName(role)} Pod · local model and worker service`,
      `Last observed CPU: ${observedUsage.cores === null ? "not measured" : number(observedUsage.cores, 3) + " cores"} · Pod CPU limit: ${observedUsage.limit === null ? "not reported" : number(observedUsage.limit, 2) + " cores"}`,
      `CPU source: ${latest?.cpu_source || "not reported"}${latest?.window ? ` · ${latest.window} average` : " · measurement window not reported"}`,
      `CPU timestamp: ${latest?.cpu_timestamp || latest?.timestamp || "not reported"}${stale ? " · stale sample" : ""}`,
      `Current session peak: ${peak === null ? "not measured" : number(peak, 1) + "%"} · shared 0–100% chart scale${peak > 100 ? " · values above 100% clipped only in chart" : ""}`,
      state.currentMetricsError || "Current workload telemetry · saved run measurements remain in Full studio",
      `Memory: ${memory(latest?.memory_bytes)} · timestamp: ${latest?.memory_timestamp || "not reported"}`,
      `Placement: ${placement.source}`,
    ];
    evidence.forEach((text) => meta.append(node("span", "", text)));
    card.title = evidence.join(". "); details.append(meta); card.append(details); return card;
  }));
  const events = $("booth-overview-events");
  if (events) {
    const recent = (run?.events || []).slice(-4).reverse();
    events.replaceChildren(...recent.map((event) => {
      const item = node("li", "booth-overview-event");
      const at = Date.parse(event.timestamp), start = Date.parse(run.created_at);
      const when = node("time", "", Number.isFinite(at) && Number.isFinite(start) ? `+${duration(Math.max(0, (at - start) / 1000))}` : "—");
      if (Number.isFinite(at)) { when.dateTime = new Date(at).toISOString(); when.title = new Date(at).toLocaleString(); }
      const content = node("div");
      const title = ({completed:"Checkout verified", validation_started:"Independent validation", review_completed:"Gemini summary", worker_completed:"Specialist finished", worker_started:"Specialist started"})[event.type] || String(event.type || "Workflow event").replaceAll("_", " ");
      content.append(node("strong", "", event.role ? `${roleName(event.role)} · ${title}` : title), node("p", "", event.message || ""));
      content.title = event.message || "";
      item.append(when, content); return item;
    }));
    if (!recent.length) events.append(node("li", "muted", "The execution timeline appears here when a workflow starts."));
  }
  const outcome = $("booth-overview-result");
  if (outcome) {
    const ready = basketReady;
    const summary = node("div", "booth-result-summary");
    if (ready) {
      summary.append(node("strong", "", "✓ Combined change verified"));
    } else if (run?.status === "failed") {
      summary.append(node("strong", "", planningFailed(run) ? "Planning failed" : "Workflow failed"));
      summary.append(node("p", "", `Updated application unavailable.${run.error ? ` ${errorText(run.error)}` : " Review the recorded error."}`));
    } else summary.append(node("p", "", state.candidateQuoteStatus === "error" ? "This basket has no available verified quote. Reset the basket to try again." : state.baselineQuoteStatus === "error" ? "The original checkout is unavailable. Reset the basket to retry the comparison." : path ? "Loading the verified checkout…" : "The updated shop opens after the combined changes pass validation."));
    const meta = node("p", "booth-result-meta", run?.tests?.total ? `${run.tests.passed} / ${run.tests.total} checks passed · ${ready ? "Same basket, before and after" : "Validation evidence available"}` : planningFailed(run) ? "Specialists did not start. No acceptance checks ran." : run?.status === "failed" ? "No acceptance results were reported for this run." : "No acceptance results yet. The updated application remains unavailable.");
    outcome.replaceChildren(summary, meta);
    if (archivePreview()) outcome.append(node("p", "booth-result-meta", "Recorded Oct 15 · Saved cloud responses"));
    else if (rehearsal) outcome.append(node("p", "booth-result-meta", "Offline fixtures · No model inference"));
  }
}
function renderBooth() {
  const run = state.run;
  const recorded = recordedLiveResult();
  $("booth-evidence-label").textContent = boothLabel();
  $("booth-evidence-label").classList.toggle("verified", recorded);
  $("booth-run-detail").textContent = run ? !state.connected ? "Last received workflow state · connection unavailable." : run.status === "passed" ? "Completed application · models are not running again." : run.status === "failed" ? "This request stopped. Review its recorded error." : "The live services are executing this request." : state.connected && liveAvailable() ? "Paste a request, then run the live agents." : "Connect the live services to run your request.";
  $("booth-run-detail").title = run ? `Selected run: ${run.id}` : "No run selected";
  const families = [...new Set(Object.keys(roleInfo).map((role) => currentArchitecturePlacement(role).family).filter(Boolean))];
  const isAxion = families.length > 0 && families.every((family) => /^(c4a|n4a)$/i.test(family));
  const cpuPlacements = Object.keys(telemetryRoles).map((role) => currentArchitecturePlacement(role));
  const cpuFamilies = [...new Set(cpuPlacements.map((placement) => placement.family).filter(Boolean))].sort();
  const allCpuAxion = cpuPlacements.every((placement) => /^(c4a|n4a)$/i.test(placement.family || ""));
  boothText("booth-platform-label", run?.mode === "rehearsal" ? "OFFLINE FIXTURES" : allCpuAxion ? `AXION ${cpuFamilies.join(" / ")} · ARM CPU` : cpuFamilies.length ? `${cpuFamilies.join(" / ")} · PARTIAL PLACEMENT` : "PLACEMENT NOT REPORTED");
  $("booth-local-compute").textContent = run?.mode === "rehearsal" ? "Offline fixtures · no Arm execution" : isAxion ? `Arm CPUs · Google Axion ${families.map((family) => family.toUpperCase()).join(" / ")}` : "Local Gemma · placement unverified";
  const metadata = state.config?.booth;
  const coordinatorPlacement = currentArchitecturePlacement("coordinator");
  boothText("booth-coordinator-evidence", run?.mode === "rehearsal" ? "This run uses local fixtures; no Arm cloud execution is claimed." : `${coordinatorPlacement.label}. Manager CPU measures the coordination Pod; hosted Gemini inference is external.`);
  const configured = [];
  if (Number.isInteger(metadata?.inference_threads_per_worker) && metadata.inference_threads_per_worker > 0) configured.push(`${metadata.inference_threads_per_worker} inference threads per worker`);
  if (Number.isInteger(metadata?.worker_node_vcpus) && metadata.worker_node_vcpus > 0) configured.push(`${metadata.worker_node_vcpus}-vCPU worker node`);
  $("booth-hardware-note").textContent = run?.mode === "rehearsal" ? "The selected result does not demonstrate Gemini, Gemma, or GKE." : configured.length ? `Configured demo: ${configured.join(" · ")}. Usage is measured below.` : "Each sandbox runs its model on the underlying server CPU. Capacity is not reported.";
  $("booth-hardware-note").title = metadata?.source || "Configuration and observed use are different measurements.";
  const tests = run?.tests;
  const baseline = state.baselineQuote;
  const quote = verifiedBasketReady() ? state.candidateQuote : null;
  $("booth-impact").hidden = !quote || !baseline;
  $("booth-total-before").textContent = money(baseline?.total_cents);
  $("booth-total-after").textContent = money(quote?.total_cents);
  const measured = new Set((state.metrics?.samples || []).filter((sample) => roleInfo[sample.role] && Number.isFinite(sample.cpu_cores)).map((sample) => sample.role));
  const story = [
    { kicker: "01 / The business request", title: "Give an existing shop a new rule.", copy: run?.prompt || "A prepared sample shop is the starting codebase. A request describes the new discount and delivery behavior.", evidence: "The same basket is used before and after. The team changes a small, supported part of the shop." },
    { kicker: "02 / Hosted Gemini", title: "Turn the request into a plan.", copy: "Gemini uses the request and relevant starter code to assign separate backend, frontend and QA tasks. Its model inference runs through a hosted API.", evidence: run?.plan?.length ? `Recorded plan: ${run.plan.map((item) => item.title || roleName(item.role)).join(" · ")}` : "No recorded plan is available for this selection." },
    { kicker: "03 / Local Gemma on Arm", title: "The CPU does the AI work, too.", copy: "Each specialist runs local Gemma inference inside its GKE Agent Sandbox. Arm CPUs generate the code and tests; a coordination service passes work and results between the agents.", evidence: measured.size ? `Saved measurements exist for ${measured.size} worker roles in Full studio. The CPU cards show current workload usage, including idle periods.` : "The CPU cards show current workload usage. Saved run measurements are separate." },
    { kicker: "04 / Independent verification", title: "Checks pass before the shop updates.", copy: "A separate validator runs independent acceptance checks and the QA agent’s generated tests. It checks the combined change, including discount boundaries and empty baskets.", evidence: tests?.total ? `${tests.passed} of ${tests.total} checks passed. ${tests.failed ? "The preview stays locked when validation fails." : "Only a passing result can become the updated checkout."}` : "No acceptance results have been reported. The updated checkout remains unavailable." },
    { kicker: "05 / The working result", title: "See what changed for the customer.", copy: "Compare the original checkout with the verified version. Try the basket controls: the updated application calculates the savings, delivery charge and total.", evidence: quote && baseline ? `This basket: ${money(baseline.total_cents)} before → ${money(quote.total_cents)} after. ${money(quote.discount_cents)} in promotion savings.` : "The result appears only after validation and a successful checkout calculation." },
  ][state.booth.step];
  $("booth-step-kicker").textContent = story.kicker;
  $("booth-step-title").textContent = story.title;
  $("booth-step-copy").textContent = story.copy;
  $("booth-step-evidence").textContent = story.evidence;
  $("booth-step-count").textContent = `${state.booth.step + 1} / 5`;
  document.querySelectorAll("button[data-story-step]").forEach((button) => {
    const selected = Number(button.dataset.storyStep) === state.booth.step;
    button.classList.toggle("selected", selected);
    if (selected) button.setAttribute("aria-current", "step"); else button.removeAttribute("aria-current");
  });
  document.body.dataset.storyFocus = String(state.booth.step);
  $("booth-play").disabled = true;
  if (state.booth.playing) stopBoothTour();
  boothText("booth-request-summary", run?.prompt || "No request submitted");
  boothText("booth-validation-count", tests?.total ? `${tests.passed}/${tests.total} checks passed` : planningFailed(run) ? "Not started" : run?.status === "failed" ? "No results reported" : "Awaiting verification");
  document.querySelectorAll("[data-agent-status]").forEach((element) => {
    const agent = (run?.agents || []).find((item) => item.role === element.dataset.agentStatus);
    const service = state.config?.services?.[element.dataset.agentStatus];
    element.textContent = run?.mode === "rehearsal" ? "Offline fixture" : run?.status === "failed" && agent?.status === "pending" ? "Not started" : agent ? sentenceStatus(agent.status) : state.connected && ["ready", "ok", "healthy"].includes(service?.status) ? "Ready" : state.config ? "Unavailable" : "Checking";
  });
  const capacity = Number.isInteger(metadata?.worker_node_vcpus) && metadata.worker_node_vcpus > 0 ? metadata.worker_node_vcpus : null;
  const threads = Number.isInteger(metadata?.inference_threads_per_worker) && metadata.inference_threads_per_worker > 0 ? metadata.inference_threads_per_worker : null;
  boothText("booth-capacity-value", capacity ? `${capacity} vCPUs` : "Not reported");
  boothText("booth-thread-value", threads ? `${threads} threads / model` : "Not reported");
  const cores = $("booth-core-grid");
  if (cores && cores.dataset.capacity !== String(capacity)) {
    cores.dataset.capacity = String(capacity);
    cores.setAttribute("aria-hidden", "true");
    cores.replaceChildren(...Array.from({ length: Math.min(capacity || 0, 64) }, () => node("span", "core-tile")));
  }
  renderBoothOverview(); renderBoothInspector(); syncBoothPanel();
}
function stopBoothTour() {
  clearTimeout(state.booth.timer); state.booth.timer = null; state.booth.playing = false;
  $("booth-play").setAttribute("aria-pressed", "false"); $("booth-play").textContent = "Play tour";
  document.body.classList.remove("tour-playing");
}
function scheduleBoothTour() {
  clearTimeout(state.booth.timer);
  if (!state.booth.playing || !state.booth.enabled || !recordedLiveResult()) return;
  state.booth.timer = setTimeout(() => {
    if (!document.hidden) {
      state.booth.step = (state.booth.step + 1) % 5;
      state.booth.inspect = storyInspect[state.booth.step];
      setBoothPanel(state.booth.step === 4 ? "result" : "team", { automatic: true }); renderBooth();
    }
    scheduleBoothTour();
  }, STORY_INTERVAL_MS);
}
function playBoothTour() {
  if ($("booth-play").disabled || !state.booth.enabled || !recordedLiveResult()) return;
  state.booth.playing = true; $("booth-play").setAttribute("aria-pressed", "true"); $("booth-play").textContent = "Pause tour";
  document.body.classList.add("tour-playing");
  setBoothPanel(state.booth.step === 4 ? "result" : "team", { automatic: true });
  scheduleBoothTour();
}
function selectStoryStep(step, focus = false) {
  stopBoothTour(); state.booth.step = (step + 5) % 5; state.booth.inspect = storyInspect[state.booth.step];
  setBoothPanel(state.booth.step === 4 ? "result" : "team", { automatic: true }); renderBooth();
  if (focus) document.querySelector(`button[data-story-step="${state.booth.step}"]`)?.focus();
}
function setBoothView(enabled) {
  stopBoothTour();
  state.booth.enabled = enabled;
  document.body.classList.toggle("booth-mode", enabled);
  moveBoothEvidence(enabled);
  const measurementDetails = document.querySelector(".v2-measurement-details");
  if (measurementDetails) measurementDetails.open = !enabled;
  if (enabled) { document.body.classList.remove("presentation-mode"); $("presentation-toggle").setAttribute("aria-pressed", "false"); $("presentation-toggle").textContent = "Presentation view"; }
  else stopBoothTour();
  $("booth-toggle").setAttribute("aria-pressed", String(enabled));
  $("booth-toggle").replaceChildren(document.createTextNode(enabled ? "Full studio " : "Booth view "), node("span", "", enabled ? "↗" : "↗"));
  const url = new URL(window.location.href);
  if (enabled) url.searchParams.set("view", "booth"); else { url.searchParams.set("view", "studio"); url.searchParams.delete("autoplay"); }
  window.history.replaceState(null, "", url);
  window.scrollTo({ top: 0, behavior: "instant" });
  renderBooth();
}
async function selectBoothRun() {
  const params = new URLSearchParams(window.location.search);
  const requested = params.get("run");
  const active = state.runs.find((run) => liveRun(run) && activeStatuses.has(run.status));
  const requestedActive = state.runs.find((run) => run.id === requested && liveRun(run) && activeStatuses.has(run.status));
  const explicitResult = params.get("panel") === "result" && requested && /^[a-zA-Z0-9_-]{1,100}$/.test(requested);
  const selected = explicitResult ? requested : requestedActive?.id || active?.id;
  if (requested && selected !== requested) {
    const url = new URL(window.location.href); url.searchParams.delete("run");
    window.history.replaceState(null, "", url);
  }
  if (selected) await selectRun(selected);
  else if (state.run) await selectRun("");
  else renderBooth();
  if (explicitResult) setBoothPanel("result", { automatic: true });
}
async function initialize() {
  // A refreshed booth is ready for a new request; configuration and history
  // must never overwrite a visitor's draft while asynchronous reads finish.
  $("prompt").value = "";
  setBoothView(new URLSearchParams(window.location.search).get("view") !== "studio");
  renderAgents(); renderExamples(supportedExamples);
  $("booth-toggle").addEventListener("click", async () => { setBoothView(!state.booth.enabled); if (state.booth.enabled) await selectBoothRun(); });
  document.querySelector(".v2-studio-link")?.addEventListener("click", (event) => { event.preventDefault(); setBoothView(false); });
  document.querySelectorAll("button[data-booth-panel]").forEach((button) => {
    button.addEventListener("click", () => setBoothPanel(button.dataset.boothPanel));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const panels = ["team", "result"], index = panels.indexOf(state.booth.panel);
      setBoothPanel(event.key === "Home" ? panels[0] : event.key === "End" ? panels.at(-1) : panels[(index + (event.key === "ArrowRight" ? 1 : panels.length - 1)) % panels.length], { focus: true });
    });
  });
  document.querySelectorAll("button[data-inspect]").forEach((button) => button.addEventListener("click", () => selectBoothNode(button.dataset.inspect)));
  $("booth-cta-explore")?.addEventListener("click", () => { selectBoothNode("backend"); document.querySelector('button[data-inspect="backend"]')?.focus({ preventScroll: true }); });
  $("booth-cta-result")?.addEventListener("click", () => setBoothPanel("result"));
  document.querySelectorAll("button[data-story-step]").forEach((button) => button.addEventListener("click", () => selectStoryStep(Number(button.dataset.storyStep))));
  $("booth-prev").addEventListener("click", () => selectStoryStep(state.booth.step - 1));
  $("booth-next").addEventListener("click", () => selectStoryStep(state.booth.step + 1));
  $("booth-play").addEventListener("click", () => state.booth.playing ? stopBoothTour() : playBoothTour());
  $("booth-story-nav").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault(); selectStoryStep(event.key === "Home" ? 0 : event.key === "End" ? 4 : state.booth.step + (event.key === "ArrowRight" ? 1 : -1), true);
  });
  $("presentation-toggle").addEventListener("click", () => {
    const active = document.body.classList.toggle("presentation-mode");
    $("presentation-toggle").setAttribute("aria-pressed", String(active));
    $("presentation-toggle").textContent = active ? "Full studio" : "Presentation view";
    window.scrollTo({ top: 0, behavior: "instant" });
  });
  $("refresh-connection")?.addEventListener("click", refreshConnection);
  $("booth-runtime-pause").addEventListener("click", toggleRuntimeConfiguration);
  $("run-button").addEventListener("click", startRun);
  $("reset-cart").addEventListener("click", resetCart);
  $("run-history").addEventListener("change", (event) => selectRun(event.target.value));
  $("refresh-artifacts").addEventListener("click", () => refreshArtifacts(true));
  $("candidate-preview").addEventListener("load", postQuote);
  $("prompt").addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !$("run-button").disabled) { event.preventDefault(); startRun(); } });
  $("prompt").addEventListener("focus", stopBoothTour);
  $("run-history").addEventListener("focus", stopBoothTour);
  const results = await Promise.allSettled([api("/api/config"), api("/api/catalog"), api("/api/runs")]);
  if (results[0].status === "fulfilled") {
    state.config = results[0].value;
    // Readiness controls availability, never a fallback to fixture execution.
    state.mode = "live";
    if (Array.isArray(state.config.supported_examples) && state.config.supported_examples.length) renderExamples(state.config.supported_examples);
  } else { state.config = { live_available: false, live_reason: "Configuration service is unavailable." }; showError("request-error", results[0].reason.message); setConnection(false); }
  if (results[1].status === "fulfilled") {
    state.catalog = Array.isArray(results[1].value) ? results[1].value : results[1].value.catalog || [];
    renderProducts(); resetCart();
  } else { $("products").replaceChildren(node("p", "muted", `The sample catalog is unavailable: ${results[1].reason.message}`)); }
  if (results[2].status === "fulfilled") { const data = results[2].value; state.runs = (Array.isArray(data) ? data : data.runs || []).filter(liveRun); renderHistory(); }
  renderAgents();
  renderRun();
  if (state.booth.enabled) await selectBoothRun();
  scheduleConnectionCheck();
  scheduleCurrentMetrics(0);
  window.setInterval(renderElapsed, 1000);
}
initialize();
