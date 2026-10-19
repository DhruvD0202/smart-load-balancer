# Week 3 — algorithms, health checks, and observable behavior

**Status: technically review-ready.** The complete suite passed 54 tests on
Windows / Python 3.13.0. The real-process demo also passed, with saved evidence
for every algorithm, backend failure/recovery, incomplete responses, and stats.
Rehearse the commands and explanations below before your academic review.

## 1. Start with the one-command demonstration

~~~powershell
python demo_week3.py
~~~

The script starts its own three backend processes on temporary loopback ports.
It starts a fresh balancer for each algorithm, then exercises failure handling.
Your normal configuration is not rewritten, and the script stops its own
processes afterwards. No manually running servers are required.

The captured run produced:

| Experiment | Observation |
| --- | --- |
| Round-robin | 1, 2, 3, 1, 2, 3 |
| Weighted, twelve requests, weights 3:2:1 | Counts 6, 4, 2 |
| Least-connections, backend-1 processing a slow request | Six fast requests alternated 2, 3, 2, 3, 2, 3 |
| Stop backend-2 | Probes marked it unhealthy; new requests used only 1 and 3 |
| Restart backend-2 | Successful recovery probes restored it to rotation |
| Request /truncate | Client detected an incomplete body; balancer remained usable |
| Stop the static pool's only backend | /static returned 503; /api kept working |
| Stop every backend | 503 for traffic, working /stats, no active reservations left |

The demo fixes weights at 3:2:1 and uses a 3-second slow request for the
least-connections experiment. Its health interval/timeout are 0.2/0.4 seconds
with failure/recovery thresholds of two. The normal config uses 2/1 seconds.
These are functional experiments, not a comparison of maximum throughput.

## 2. How the algorithms differ

Routing still happens first: determine the pool using the path. Then exclude
unhealthy/unknown members and apply the selected algorithm.

**Round-robin** takes turns among eligible members. A pool cursor continues
around the configured member list, skipping ineligible servers.

**Least-connections** selects the eligible backend with the fewest active
assignments. Our one-request-per-connection design treats an active assignment
as a pending connection attempt or a request currently transferring.

For example, with active counts 5, 1, and 3, an eligible backend-2 wins. These
counts are global per backend, so a slow API request affects selection in the
default pool if the same backend belongs to both. Ties rotate to avoid always
choosing the first member when the system is idle.

The sequence matters:

~~~text
choose backend -> reserve active slot -> await network work
                                        ...
finally: close sockets -> release slot
~~~

If incrementing happened after await open_connection, multiple handlers could
all see the same old count and choose the same server. Selection and reservation
contain no await, so they complete together within this event-loop thread.

**Weighted round-robin** sends a larger share of requests to higher-weight
servers. The configured weights describe intended relative capacity; the
balancer does not measure CPU capacity or learn weights automatically.

For weights 3:2:1, the target shares while all servers are eligible are 1/2,
1/3, and 1/6. We use a smooth version with one score per backend in each pool:

1. Add each eligible backend's weight to its score.
2. Choose the largest score (configuration order breaks a tie).
3. Subtract the sum of eligible weights from the chosen score.

| Selection | Scores after adding weights | Winner |
| --- | --- | --- |
| 1 | 3, 2, 1 | backend-1 |
| 2 | 0, 4, 2 | backend-2 |
| 3 | 3, 0, 3 | backend-1 |
| 4 | 0, 2, 4 | backend-3 |
| 5 | 3, 4, -1 | backend-2 |
| 6 | 6, 0, 0 | backend-1 |

After selection 6, scores return to zero. Counts are 3:2:1, spread across the
cycle. When eligibility changes, that pool's scores reset. Health probes do not
advance scores or round-robin cursors.

The [nginx load-balancing documentation](https://nginx.org/en/docs/http/load_balancing.html)
provides a useful comparison of round-robin, least-connected, and weighted
policies. Our implementation is deliberately smaller.

## 3. The background asyncio pattern

Health detection must work with zero client traffic. A background coroutine
performs the periodic work:

~~~python
while True:
    await asyncio.sleep(interval)
    await check_once()
~~~

The first startup check runs before accepting client requests. Subsequent rounds
sleep after the previous round completes. check_once probes all backends
concurrently using gather, so one slow backend does not require waiting for
every other backend sequentially.

The listener and checker are supervised together:

~~~python
async with asyncio.TaskGroup() as group:
    group.create_task(checker.run())
    await server.serve_forever()
~~~

This is the central pattern; the actual function also handles configuration,
startup checks, and cleanup. TaskGroup awaits its child tasks on exit. An
unexpected checker failure cancels the group's work rather than silently
leaving a running listener with stale health state. Ctrl+C causes cancellation,
and finally blocks clean up clients, sockets, and active reservations.

Use await asyncio.sleep in this coroutine. time.sleep would block the event-loop
thread, delaying client requests too. The separate watch_stats CLI can use
ordinary sleep because it is a synchronous program in another process.

See [Python's TaskGroup and cancellation documentation](https://docs.python.org/3.13/library/asyncio-task.html).

## 4. What counts as healthy?

The balancer sends GET /health directly to each backend. This is an HTTP
application check, not an ICMP ping and not a check routed through the balancer.
It requires status 200, valid supported response framing, and the entire
declared body within the timeout. Health bodies are limited to 64 KiB.

| State | Can receive application requests? | Transition |
| --- | --- | --- |
| unknown | No | First successful startup probe makes it healthy |
| healthy | Yes | Two consecutive failures mark it unhealthy by default |
| unhealthy | No | Two consecutive successes restore it by default |
| unchecked | Yes | Health checks were explicitly disabled in configuration |

A success resets the failure streak. A failure resets the success streak.
Thresholds reduce rapid down/up changes caused by one transient probe failure.
Restoring an unhealthy backend uses the recovery threshold, whereas admitting
an unknown backend uses its first successful check.

The interval is a delay after a concurrent probe round; it is not a fixed-rate
schedule. Detection time depends on when failure occurs and how long probes
take. Allow a few seconds with the normal settings and observe /stats.

There is a detection window: a client can receive 502/504 before the checker
has enough failures to remove a backend. Existing requests are not moved to
another server. If no member of the requested pool is eligible, return 503;
do not send a static request to an unrelated API pool.

Health and correctness are different. Our /truncate route intentionally sends
an incomplete response while /health continues to return 200. That request
fails, but the health checker still sees a responsive health endpoint.
Client errors are not treated as health probe failures.

## 5. Read /stats accurately

~~~powershell
python watch_stats.py
# Or a one-time snapshot:
python watch_stats.py --once
curl.exe --max-time 5 "http://127.0.0.1:8080/stats"
~~~

The endpoint is handled locally. It supports GET and HEAD, sets Cache-Control:
no-store, and works with no healthy backend. POST /stats returns 405.
The exact /stats path is reserved; it is never forwarded to a pool.

| Metric | Meaning |
| --- | --- |
| requests | Assignments to this backend, including later failures/cancellations |
| active | Reservations currently connecting/transferring/cleaning up |
| completed | Full supported HTTP response transferred, including origin 4xx/5xx |
| failed | Assigned request did not complete, including a truncated body |
| cancelled | Assigned request cancelled, for example during shutdown |
| http_error_responses | Completed responses whose HTTP status was at least 400 |
| avg_response_ms | Mean duration of completed assigned requests only |
| last_response_ms | Duration of the most recently completed assigned request |
| health_checks | Probe count, separate from application requests |
| unassigned_errors | Connections ending before assignment, including invalid requests and no-healthy-pool errors; excludes recognized /stats requests |

For every backend, requests = active + completed + failed + cancelled.

Response duration begins when a client handler is accepted and ends when the
response has been forwarded through the writer. It includes request upload,
backend connection/waits, and output backpressure; socket cleanup is excluded.
It is not a guarantee of when the client application consumed the response.
No completed sample is shown as null / "-" rather than a misleading zero.

State is in memory and resets when the balancer restarts. Backends can remain
running while changing algorithms, but restart the balancer for the new setting.
Health probes and stats polling never inflate application request totals.

## 6. Structured logs and failure semantics

~~~powershell
python load_balancer.py --log-file balancer.jsonl
~~~

Each line is one JSON object. Request events contain a UTC timestamp, request ID,
client IP/port, method, path without query, pool, selected backend, HTTP status,
outcome, response_time_ms, and error. Headers and bodies are not logged.
Health changes, startup, shutdown, and stats reads have separate event types.

A completed origin error may have status=500 and outcome=complete. A backend
that sends status 200 and then stops early is logged as status=200 with
outcome=incomplete. The original status cannot be replaced after sending it.
The connection closes; no second response is appended.

There are no automatic retries. A failed POST may already have changed backend
state, so replaying it without an explicit idempotency policy can duplicate work.

## 7. Manual review checklist

For ordinary traffic, use run_backends.py and load_balancer.py as in the README.
For a single-backend failure demo, stop the group launcher and run three
independent backend terminals:

~~~powershell
python demo_backend.py --port 9001 --name backend-1 --delay 3
python demo_backend.py --port 9002 --name backend-2
python demo_backend.py --port 9003 --name backend-3
~~~

Start the balancer in another terminal:

~~~powershell
python load_balancer.py --algorithm round_robin --log-file balancer.jsonl
~~~

| Step | Action | Expected observation |
| --- | --- | --- |
| 1 | Run watch_stats.py --once | All three healthy, zero application requests after startup |
| 2 | Send /hello several times | Backend rotation; request counts rise |
| 3 | Stop only backend-2 with Ctrl+C | Health changes to unhealthy after the threshold |
| 4 | Send /hello after the health change | Only backends 1 and 3 receive requests |
| 5 | Restart backend-2 | Recovery probes restore its eligibility |
| 6 | Stop backend-3 and wait for unhealthy | /static yields 503; /api still works |
| 7 | Restart backend-3; request /truncate | curl reports an incomplete response; balancer remains usable |
| 8 | Look at logs and final stats | Explain status versus outcome; counters reconcile |

Client commands:

~~~powershell
curl.exe --max-time 5 -i "http://127.0.0.1:8080/hello"
curl.exe --max-time 5 -i "http://127.0.0.1:8080/static/file.css"
curl.exe --max-time 5 -i "http://127.0.0.1:8080/api/items"
curl.exe --max-time 5 -i "http://127.0.0.1:8080/truncate"
~~~

To try another policy, stop only the balancer and restart it with:

~~~powershell
python load_balancer.py --algorithm least_connections
# Or:
python load_balancer.py --algorithm weighted_round_robin
~~~

A sequential workload usually leaves every backend idle before the next request.
Least-connections can then resemble round-robin because of its tie-break.
Use the controlled overlapping workload in demo_week3.py to demonstrate the
actual difference. The Week 2 checker specifically expects round-robin.

## 8. Code reading order and review questions

1. config.json / lb_config.py: algorithm names, weights, health settings.
2. scheduler.py: pool_for, select, release, then record_probe.
3. health_checks.py: probe, check_once, and the periodic run loop.
4. load_balancer.py: the /stats branch, reservation cleanup, and serve's TaskGroup.
5. event_log.py / watch_stats.py: JSON records and the live table.

**Why use a reservation before connecting?** It avoids selecting a backend
using a stale active count while another connection attempt is pending.

**Why use finally?** Timeout, cancellation, broken connections, and normal
completion must all release the same reservation exactly once.

**Why two failed probes?** One transient failure should not necessarily remove
a backend. The threshold trades detection speed for stability.

**Why probe HTTP instead of just TCP connect?** A process can accept connections
while its application returns errors or stalls.

**Why is the weighted distribution only approximate over short windows?**
Weighted scheduling follows cycles, and eligibility changes reset the scores.
Complete controlled cycles with stable membership show the expected ratio.

**Why don't health probes count as connections for least-connections?** The
counter represents application work assigned by the balancer; probes are
separate monitoring traffic and should not distort scheduling.

**What if every backend is unhealthy?** Return 503 for application traffic,
continue probing, and keep the local /stats endpoint available.

## 9. Evidence and remaining work

Actual [demo output](evidence/week3/demo.txt),
[JSON events](evidence/week3/health_recovery.jsonl), and
[final stats](evidence/week3/final-stats.json) are saved for your report.
The final failure scenario recorded 15 assignments: 14 complete, 1 failed,
0 cancelled, and 0 still active. These are observations from that run.

The supported HTTP subset remains bounded and closes after one request.
There is no TLS termination, HTTP/2, chunked-body support, global connection cap,
or production deployment claim. The local stats endpoint has no authentication.

Week 4 focuses on the final report, PPT, screenshots, experiment discussion,
architecture diagram, and rehearsal. TLS remains optional after those are stable.
