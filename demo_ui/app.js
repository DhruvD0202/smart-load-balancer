"use strict";
const $ = (id) => document.getElementById(id);
const colors = {"backend-1":"#087c73", "backend-2":"#356cd4", "backend-3":"#b76b26"};
const labels = {round_robin:"Round-robin", least_connections:"Least-connections", weighted_round_robin:"Weighted round-robin"};
const explanations = {
  round_robin:"Round-robin takes turns among the healthy members of the selected pool. Equal turns apply within each pool, not across all paths combined.",
  least_connections:"Least-connections picks the healthy backend with the fewest active assignments. It reserves a slot before waiting for a connection. Ties rotate fairly; an idle system can look like round-robin.",
  weighted_round_robin:"Smooth weighted round-robin uses weights 3 : 2 : 1. With all three servers healthy, 12 default-pool requests split 6 : 4 : 2. Weights are configured shares, not measured CPU capacity."
};
const routes = {
  "/hello":"Default pool → backend-1, backend-2, backend-3.",
  "/api/items":"API pool → backend-1 and backend-2. The third server is outside this pool.",
  "/static/style.css":"Static pool → backend-3 only. If it is unhealthy, this path returns 503.",
  "/apiculture":"Default pool → all three. /apiculture does not match the /api path-segment boundary.",
  "/api/echo":"API pool → backends 1 and 2. Sends a JSON body and an X-Demo header for the backend to echo.",
  "/slow":"Default pool. Backend-1 waits 3 seconds; the others wait 0.1 seconds. Use the busy-server experiment for a controlled comparison.",
  "/truncate":"Default pool. The selected backend declares 64 body bytes but sends 16. Expect an incomplete transfer, even with a 200 header."
};
let state = null, working = false, online = false, refreshInFlight = false;
let tableSignature = "", lastCompletion = "", selectedResponse = null;
const cards = new Map();

function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").classList.toggle("error", error);
}
function controls() {
  const disabled = !online || working || Boolean(state?.busy);
  document.querySelectorAll("[data-control]").forEach((element) => { element.disabled = disabled; });
  $("path-select").disabled = disabled;
  $("count-select").disabled = disabled || $("path-select").value === "/slow";
  $("send-button").firstChild.textContent = working ? "Working… " : "Send traffic ";
  $("export-button").disabled = !state?.stats;
}
function createCard(backend) {
  const card = document.createElement("article");
  card.className = "backend-card";
  card.style.setProperty("--backend-color", colors[backend.name]);
  // All markup is fixed; response/log strings are inserted with textContent.
  card.innerHTML = '<div class="backend-title"><strong></strong><span class="health-badge"></span></div><div class="backend-stats"><span class="requests"></span><span class="active"></span><span class="weight"></span></div><div class="backend-bottom"><span class="process"></span><button class="backend-button" data-control></button></div>';
  card.querySelector("strong").textContent = backend.name;
  const button = card.querySelector("button");
  button.addEventListener("click", () => action("backend", {name:backend.name, command:state.running[backend.name] ? "stop" : "start"}));
  cards.set(backend.name, card);
  $("backend-cards").append(card);
  return card;
}
function showResponse(row, open = true) {
  selectedResponse = row.id;
  $("response-label").textContent = `#${row.id} · ${row.method} ${row.path} · ${row.complete ? "complete body" : "incomplete or unavailable body"}`;
  try { $("response-body").textContent = JSON.stringify(JSON.parse(row.body), null, 2); }
  catch { $("response-body").textContent = row.body || "No response body."; }
  if (open) $("response-details").open = true;
}
function renderHistory(next) {
  const signature = `${next.generation}:${next.history.map(r => r.id).join(",")}`;
  if (signature === tableSignature) return;
  if (state && next.generation !== state.generation) selectedResponse = null;
  tableSignature = signature;
  const body = $("request-rows");
  body.replaceChildren();
  if (!next.history.length) {
    const tr = body.insertRow(); const td = tr.insertCell(); td.colSpan = 5; td.className = "empty";
    td.textContent = "Your first request starts the story. Send traffic above.";
    $("response-label").textContent = "— select a request";
    $("response-body").textContent = "No response selected yet.";
    selectedResponse = null;
    return;
  }
  for (const row of next.history.slice(-12).reverse()) {
    const tr = body.insertRow();
    tr.insertCell().textContent = row.id;
    const request = document.createElement("button"); request.className = "row-path";
    request.textContent = `${row.method} ${row.path}`;
    request.addEventListener("click", () => showResponse(row));
    tr.insertCell().append(request);
    const backend = document.createElement("span"); backend.className = "backend-name";
    backend.style.setProperty("--backend-color", colors[row.backend] || "#9daab5");
    backend.append(document.createElement("i"), document.createTextNode(row.backend || "See event log"));
    tr.insertCell().append(backend);
    const result = tr.insertCell(); result.className = row.outcome === "success" ? "result-good" : "result-bad";
    result.textContent = row.outcome === "incomplete" ? `${row.status} · incomplete` : row.status ? `HTTP ${row.status}` : "Connection error";
    const duration = tr.insertCell(); duration.className = "result-time";
    duration.textContent = row.duration_ms === null ? "—" : `${row.duration_ms.toFixed(1)} ms`;
  }
  const newest = next.history.at(-1);
  const selected = next.history.find(row => row.id === selectedResponse);
  showResponse(selected || newest, false);
  const completion = `${next.generation}:${newest.id}`;
  if (completion !== lastCompletion && newest.backend && cards.has(newest.backend)) {
    const card = cards.get(newest.backend); card.classList.add("hit");
    setTimeout(() => card.classList.remove("hit"), 650);
  }
  lastCompletion = completion;
}
function render(next) {
  const wasOnline = online;
  online = Boolean(next.stats);
  $("connection-label").textContent = online ? "Connected to live servers" : next.busy ? "Preparing experiment…" : "Balancer unavailable";
  $("connection-dot").className = `dot ${online ? "online" : "offline"}`;
  const stats = next.stats;
  if (stats) {
    $("metric-requests").textContent = stats.totals.requests;
    $("metric-completed").textContent = `${stats.totals.completed} completed · ${stats.totals.cancelled} cancelled`;
    $("metric-active").textContent = stats.totals.active;
    $("metric-healthy").textContent = `${stats.backends.filter(b => b.health === "healthy").length} / ${stats.backends.length}`;
    $("metric-failed").textContent = stats.totals.failed;
    $("metric-rejected").textContent = `Unassigned errors: ${stats.unassigned_errors}`;
    if (!cards.size) $("backend-cards").replaceChildren();
    for (const backend of stats.backends) {
      const card = cards.get(backend.name) || createCard(backend);
      const health = card.querySelector(".health-badge"); health.textContent = backend.health;
      health.className = `health-badge ${backend.health}`;
      card.querySelector(".requests").textContent = `${backend.requests} req`;
      card.querySelector(".active").textContent = `${backend.active} active`;
      card.querySelector(".weight").textContent = `w ${backend.weight}`;
      card.querySelector(".process").textContent = `:${backend.port} · ${next.running[backend.name] ? "process running" : "process stopped"}`;
      const button = card.querySelector("button"); button.textContent = next.running[backend.name] ? "Stop" : "Start";
      button.setAttribute("aria-label", `${button.textContent} ${backend.name}`);
      card.title = `${backend.completed} completed, ${backend.failed} failed. Average completed duration: ${backend.avg_response_ms ?? "—"} ms.`;
    }
  }
  $("algorithm-select").value = next.algorithm;
  $("journey-algorithm").textContent = labels[next.algorithm];
  $("algorithm-explanation").textContent = explanations[next.algorithm];
  $("balancer-link").textContent = next.balancer_url;
  $("balancer-link").href = next.balancer_url + "/stats";
  renderHistory(next);
  $("event-log").textContent = next.events.length ? next.events.map(e => JSON.stringify(e)).join("\n\n") : "No application requests or health changes yet.";
  const first = state === null;
  state = next;
  if (first) notice("Ready. Send your first request, or choose a guided experiment.");
  else if (!wasOnline && online && !working && !next.busy) notice("Connection restored. Your live statistics are updating again.");
  controls();
}
async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const response = await fetch("/api/state", {cache:"no-store", signal:AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error("Could not read live state.");
    render(await response.json());
  } catch {
    online = false;
    $("connection-label").textContent = "Disconnected";
    $("connection-dot").className = "dot offline";
    notice("The classroom is not responding. Keep its terminal running, or restart it with python demo_app.py. Displayed values are the last received snapshot.", true);
    controls();
  } finally { refreshInFlight = false; }
}
async function action(kind, payload) {
  if (working || !state) return;
  working = true; controls();
  notice(kind === "experiment" ? "Experiment running. Watch the active count and request results as real traffic flows…" : "Applying your action. Live statistics continue updating…");
  if (kind === "experiment") {
    $("path-select").value = "/hello";
    $("journey-path").textContent = payload.kind === "busy" ? "/slow + /hello" : "/hello";
    $("route-note").textContent = routes["/hello"];
    $("experiment-result").className = "experiment-result";
    $("experiment-result").textContent = "Running a fresh experiment…";
  }
  try {
    const response = await fetch(`/api/${kind}`, {method:"POST", headers:{"Content-Type":"application/json", "X-Demo-Token":state.token}, body:JSON.stringify(payload), signal:AbortSignal.timeout(30000)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    notice(result.message, result.passed === false);
    if (kind === "experiment") {
      $("experiment-result").textContent = result.message;
      $("experiment-result").className = `experiment-result ${result.passed ? "pass" : "fail"}`;
    } else if (kind === "reset") {
      $("experiment-result").className = "experiment-result";
      $("experiment-result").textContent = "New run ready. Choose an experiment to check its behavior.";
    }
  } catch (error) {
    notice(error.message, true);
    if (kind === "experiment") {
      $("experiment-result").className = "experiment-result fail";
      $("experiment-result").textContent = `Experiment did not finish: ${error.message}`;
    }
  } finally { working = false; await refresh(); controls(); }
}
$("send-button").addEventListener("click", () => {
  $("journey-path").textContent = $("path-select").value;
  action("traffic", {path:$("path-select").value, count:Number($("count-select").value)});
});
$("reset-button").addEventListener("click", () => action("reset", {algorithm:$("algorithm-select").value}));
$("algorithm-select").addEventListener("change", () => action("reset", {algorithm:$("algorithm-select").value}));
$("path-select").addEventListener("change", () => {
  const path = $("path-select").value;
  $("journey-path").textContent = path;
  $("route-note").textContent = routes[path];
  if (path === "/slow") $("count-select").value = "1";
  controls();
});
document.querySelectorAll("[data-experiment]").forEach(button => button.addEventListener("click", () => action("experiment", {kind:button.dataset.experiment})));
$("export-button").addEventListener("click", () => {
  if (!state) return;
  const {token, ...evidence} = state;
  evidence.note = "Snapshot of this browser classroom run. History is capped at 80 completions; full JSON logs are in log_directory. Functional demo, not a throughput benchmark.";
  const url = URL.createObjectURL(new Blob([JSON.stringify(evidence, null, 2)], {type:"application/json"}));
  const link = document.createElement("a"); link.href = url; link.download = `traffic-lab-run-${state.generation}.json`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
controls();
(async function poll() { await refresh(); setTimeout(poll, 700); })();
