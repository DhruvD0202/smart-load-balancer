# Four-week roadmap

Treat these as workload stages, not fixed calendar dates. Aim for three or four
focused sessions per week, leaving time for coursework and a review rehearsal.
If time is tight, defer TLS first. Keep a passing demo at each stage.

Current progress: Weeks 1–3 are implemented. See [the Week 3 guide](week3.md)
for selectable algorithms, active health checks, JSON logs, stats definitions,
and the failure/recovery demonstration. The [final review presentation](../deliverables/F2_Team3_PPT.pptx)
is prepared with speaker notes and two hidden backup slides. Week 4's report
is available as [Word](../deliverables/F2_Team3_Report.docx)
and [PDF](../deliverables/F2_Team3_Report.pdf), including
experiment discussion, recorded evidence and a demo checklist. Rehearsal
remains. TLS is still optional.

| Week | Implementation | Review demonstration | Evidence / writing |
| --- | --- | --- | --- |
| 1 | One-backend asyncio TCP proxy, dummy HTTP server, cleanup, timeouts, basic connection logs | HTTP response through proxy; overlapping delayed requests; stop/restart backend while proxy stays up | Architecture sketch, asyncio explanation, socket tests, actual demo logs |
| 2 | Bounded HTTP request parsing and framing, JSON configuration, three named backend instances, round-robin, path-based pools | Show method/path/headers, backend rotation, /api/* and /static/* pool selection, rejected malformed requests | Explain Layer 7 decisions, routing rules, and framing limits; capture distribution output |
| 3 | Least-connections, weighted round-robin, active HTTP health checks, failure handling, JSON request logs, /stats | Compare algorithms; kill/recover a backend; inspect health, per-backend counts, active connections, and latency | Controlled experiments with workload/weights recorded; distinguish success, failure, and attempted requests |
| 4 | Stabilization, repeatable experiments, final report, README polish, PPT, and demo rehearsal | Full scenario from clean startup; show failure and recovery; explain results and limitations | Final architecture diagram, authentic screenshots/logs, report, presentation, demo checklist, resume bullet |

## Design decisions to work through as we reach them

- Use JSON initially because it is in the standard library.
- Define HTTP scope explicitly: start with HTTP/1.1 and one request per client
  connection. Bound headers and bodies; reject unsupported transfer framing
  clearly. Never assume one TCP read equals one HTTP request.
- Select a backend per HTTP request once parsing is introduced.
- Specify route precedence and segment boundaries so /api/* does not
  accidentally match /apiculture.
- Define least-connections precisely and release counters in finally blocks,
  including errors and cancellations.
- Define weighted balancing as weighted round-robin and measure its observed
  distribution under a stated workload.
- Health checks need timeouts and a clear down/up policy. Polling probes are
  separate from handling real client failures.
- Do not blindly retry requests: a failed connection may have delivered a
  request that already changed backend state. Once response bytes have reached
  a client, do not append a second HTTP error response to that stream.
- Define stats timing and whether it measures connection lifetime, first byte,
  or full response. Do not present localhost results as production capacity.

## Stretch goal

Add basic TLS termination only after the core features and final deliverables
are stable. Demonstrate HTTPS client-to-proxy and plain HTTP proxy-to-backend,
and explain certificates and where encryption ends.

## Review-ready rule

A milestone is technically review-ready when its documented commands and
failure demonstration work, relevant tests pass, and observed evidence exists.
Personal review preparation additionally requires explaining the control flow
without reading the code aloud and rehearsing the demo once.

Do not describe features from future weeks as already implemented.
