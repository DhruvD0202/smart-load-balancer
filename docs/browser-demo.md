# Browser classroom: demonstrate and explain the real project

## Start with one command

From the project folder:

```powershell
python demo_app.py
```

The browser opens at **http://127.0.0.1:8090**. Leave that terminal running.
If the browser does not open, enter the address manually. If 8090 is already
occupied, run `python demo_app.py --port 8091` and use the printed address.
Ctrl+C in the app terminal stops its balancer and three backend processes.

You do not need the four-terminal setup. This classroom creates its own servers
on temporary loopback ports. It can run alongside the original demo on 8080.
It writes a separate configuration inside `demo-output/<session>/`; it does not
rewrite the project's `config.json` or control servers started elsewhere.

## Five-minute review checklist

1. **Take turns.** Click the first guided experiment. Show the PASS result and
   2 completed requests per backend. Explain that selection follows a pool's
   round-robin cursor.
2. **Give capacity a weight.** Click the second experiment. Show 6, 4, 2 requests
   for weights 3, 2, 1. These are configured relative shares, not measured CPU use.
3. **Keep a busy server free.** Click the third experiment. Watch backend-1's
   active count become 1 during its three-second request. Six fast requests go
   to backends 2 and 3. A slow request gives the event loop other work to run.
4. **Explain routing.** Select Round-robin (which starts a fresh run), choose
   `/api/items`, choose 6 requests, and send. Only backends 1 and 2 answer. Try
   `/static/style.css` (backend-3) and `/apiculture` (the default pool).
5. **Show automatic health changes.** Stop backend-2 with its card. Wait for
   `unhealthy`, then send `/hello` traffic. Start it and wait for `healthy`.
   The app stops/restarts the actual child process; it does not set health flags.
6. **Show a failed transfer.** Send one `/truncate` request. Explain why the
   result says `200 · incomplete`: the status header arrived, but the declared
   body did not. Show the failure counter and the actual JSON event.
7. **Keep the evidence.** Click **Save evidence** for a JSON snapshot, and capture
   the visible results for your report. Click **Start fresh** before the next run.

To demonstrate pool isolation, stop backend-3, wait for unhealthy, and send a
static request (503) followed by an API request (200). Stop all three to show
503 for application requests while the dashboard remains accessible.

Every guided experiment restores all backends and resets counters/history.
The algorithm dropdown also starts a fresh run. The default route and weights
come from the project fixture; this classroom fixes weights at 3:2:1 and uses
0.75-second health intervals, 0.5-second timeouts, and thresholds of two.
Detection is not instantaneous: there are two probe rounds plus connection
and scheduling time. A request sent before detection may receive 502/504.

## What the app actually does

```text
Browser controls (:8090)
       |
       v
demo_app.py: local teaching controller / HTTP client
       |
       | actual HTTP requests over loopback TCP
       v
load_balancer.py: the existing asyncio Layer 7 balancer
       |
       +--> demo_backend.py (backend-1)
       +--> demo_backend.py (backend-2)
       +--> demo_backend.py (backend-3)
```

The controller serves plain HTML/CSS/JavaScript and starts the existing Python
scripts as separate processes. Its blocking process/HTTP operations run in
HTTP server worker threads. It has not replaced the core asyncio implementation.
The core still uses await, concurrent request handlers, and a background health
checker. The browser's request history comes from real HTTP responses, and
its statistics come from actual GET requests to the balancer's `/stats` endpoint.

Browser polling uses `fetch` without blocking rendering. In the busy-server
experiment, the controller uses a second worker thread for the slow client so
it can send fast requests while that slow one is still waiting. **Client
concurrency and the balancer's asyncio concurrency are separate mechanisms.**

All probe and stats traffic is excluded from backend assignment counts.
The request table shows completion order, not necessarily arrival order.
Client time includes the client's round trip; the backend cards' hover text uses
the balancer's own duration metric. A highlight represents a returned response,
not a packet trace. Zero active assignments after completion is normal.

The monitor remains reachable if backends are stopped. If the balancer is
temporarily unavailable during a reset, the UI reports that state; if the app
itself is stopped, displayed values are explicitly identified as the last
received snapshot. Reopen/reload the page after restarting the app.

## Reading and saving evidence

The page keeps the last 80 completed browser exchanges; it displays the most
recent 12 and lets you inspect response bodies. The JSON event viewer reads
the last 64 KiB of the real balancer log and shows up to 18 request/health events.
Full logs remain in `demo-output/<session>/balancer-<run>.jsonl`; each new run
has a separate log. Backend output and the private config live beside it.
These generated files are ignored by Git. Save selected evidence under
`docs/evidence/` when preparing the report.

The app is a loopback-only teaching controller with bounded, predefined
traffic actions and a per-session control token. It is not a deployment or
remote administration interface. No additional packages or internet connection
are required. Demo traffic is small and controlled, not a throughput benchmark.

## Source reading order

1. `demo_ui/index.html`: page structure, teaching text, and controls.
2. `demo_ui/app.js`: fetch live state, send actions, render actual results.
3. `demo_app.py`: manage owned processes and make HTTP requests.
4. Follow those requests into the existing `load_balancer.py`, `scheduler.py`,
   and `health_checks.py` to explain how the core works.

## Verification

```powershell
python -m unittest discover -s tests -p test_demo_app.py -v
```

The integration checks start actual child servers. They cover all three guided
experiments, path pools, POST body/header preservation, health failure/recovery,
truncated responses, all-down monitoring, reset, bounded controls, and cleanup.
Run the full existing suite with `python -m unittest discover -s tests -v`.

The verified run passed **60 tests**. The app's round-robin, weighted,
least-connections, incomplete-response, stop, and recovery actions were also
checked through the browser. See [verification evidence](evidence/browser-demo/verification.txt).
