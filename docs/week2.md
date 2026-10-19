# Week 2 — HTTP-aware routing and round-robin load balancing

This milestone builds on the working Week 1 TCP proxy. The HTTP program is
load_balancer.py; proxy.py remains runnable for the earlier review.

Historical note: Week 3 now adds health checks, algorithms, JSON logs, and /stats
to the same HTTP entry point. The Week 2 routing checker still works with
round_robin and all servers healthy. tests/smoke_week2.py explicitly disables
health checks to preserve this milestone's original failure behavior.

**Status: technically review-ready.** The full suite passed 35 tests on Windows
with Python 3.13.0, and the three-process backend demonstration passed. Rehearse
the checklist and the design explanations below for your personal review prep.

## 1. What makes this Layer 7?

Week 1 copied a TCP byte stream to one destination. The current program extracts
the HTTP method, target, and headers, then uses the path to choose a pool.
That decision uses application-protocol information.

A request looks like this on the wire:

~~~http
POST /api/echo?course=CN HTTP/1.1
Host: 127.0.0.1:8080
Content-Type: text/plain
Content-Length: 5

hello
~~~

Each displayed line ends with CRLF (the bytes \r\n). The empty line terminates
the headers. The body in this example is exactly five bytes.

| Field | Value / purpose |
| --- | --- |
| Method | POST: submit data |
| Target | /api/echo?course=CN: sent unchanged to the backend |
| Path | /api/echo: used for routing |
| Query | course=CN: not used for pool selection |
| Host | Required once in this HTTP/1.1 subset; preserved when forwarding |
| Content-Length | Number of body bytes, not the number of Unicode characters |

HTTP describes a message carried inside TCP. TCP may split the headers or body
across reads, or deliver several pieces together. We therefore locate the header
terminator and count body bytes. The underlying specification is
[RFC 9112](https://www.rfc-editor.org/rfc/rfc9112.html).

## 2. New asyncio patterns, before reading the code

We now know where an HTTP message ends, so we can use more precise reads:

~~~python
raw_headers = await reader.readuntil(b"\r\n\r\n")
body = await reader.readexactly(content_length)
~~~

readuntil accumulates data until the delimiter, even when headers arrive in
fragments. readexactly waits for the declared body byte count and raises if
the connection ends too early. Both can suspend this client handler while other
handlers progress. See the [Python streams reference](https://docs.python.org/3.13/library/asyncio-stream.html).

We bound header length through the stream limit and a final size check. We bound
the body before reading it. A whole-phase deadline prevents a peer from keeping
a request open indefinitely by sending one byte before every new read deadline.

The new flow is:

~~~text
accept client
  -> read/validate headers
  -> read bounded request body
  -> choose pool and backend
  -> connect to backend
  -> forward request
  -> validate response headers
  -> stream response body
  -> close both connections
~~~

Unlike the Week 1 generic TCP relay, this version buffers one bounded request
before contacting a backend. It then streams the response in chunks. That
avoids forwarding an incomplete upload, but adds memory use and upload latency.
It is a deliberate learning-stage tradeoff, not unlimited upload streaming.

There is still one asyncio handler task per client, scheduled by start_server.
Other handlers continue while this handler awaits network I/O.

## 3. Understand pools and round-robin

A pool is a named group of backend servers eligible for a particular request.
Our config has:

~~~text
default: [backend-1, backend-2, backend-3]
api:     [backend-1, backend-2]
static:  [backend-3]
~~~

Round-robin maintains an index into that list:

~~~python
backend = members[index]
next_index = (index + 1) % len(members)
~~~

The modulo operator wraps the index to zero after the last member. In a
three-member pool, the indices are 0, 1, 2, 0, 1, 2.

Each pool owns its own index. An API request advances only the API pool, even
though it shares physical servers with the default pool. The index advances
when a complete validated request selects a backend, including if that backend
subsequently fails. It does not promise order by client connection arrival.

There is no await between reading and updating the index. In this single
event-loop thread, another handler cannot interrupt that operation at a
coroutine suspension point. Multiple threads or processes would require a
different coordination design.

Round-robin equalizes request selection counts over complete cycles. Requests
can have different costs, so equal counts do not guarantee equal CPU usage,
response time, or numbers of active connections.

## 4. Understand path matching

The config uses a prefix such as /api, without an asterisk. It matches /api
itself and descendants beginning /api/. It does not match /apiculture.

The longest matching prefix wins. If /api/admin is added later, that route
takes precedence over /api for /api/admin/users, regardless of config order.
Unmatched paths use default_pool.

Queries do not affect selection: /api/items?next=/static still uses the API
pool. Matching uses the literal, case-sensitive path; percent escapes and dot
segments are not normalized. Pools are not access-control rules.

## 5. Read the implementation in small steps

| Order | File / function | Question to answer |
| --- | --- | --- |
| 1 | config.json | Which servers belong to each pool? |
| 2 | http_protocol.py: parse_request | How do we extract the method/path and validate the headers? |
| 3 | scheduler.py: BackendScheduler.select | Which rule wins and how does the cursor wrap? |
| 4 | load_balancer.py: handle_client | Where does one request wait, select, and forward? |
| 5 | http_protocol.py: parse_response | How do we know how many response bytes to send? |

JSON needs no extra dependency, so the setup remains standard-library only.
Validation happens at startup, giving a useful error before listening if a
backend name is unknown, a pool is empty, or an algorithm is unsupported.
Configuration is loaded once; restart the balancer after edits.

## 6. Live demo checklist

First stop the Week 1 proxy and backend with Ctrl+C to free ports 8080 and 9001.
Use three terminals in the project directory:

~~~powershell
# Terminal 1: one launcher, three separate backend processes
python run_backends.py
# Terminal 2: HTTP load balancer
python load_balancer.py --config config.json
# Terminal 3: client
python demo_week2.py
~~~

The launcher stops its children on Ctrl+C. A child exiting also stops the group,
so use independent processes (below) if demonstrating a single backend failure.

| Step | Action | Explain / verify |
| --- | --- | --- |
| 1 | Run the checker | /hello uses all three backends in rotation |
| 2 | Observe /api/items requests | Only backends 1 and 2 are eligible |
| 3 | Observe /static/style.css | Only backend 3 is eligible; response is still dummy JSON |
| 4 | Observe /apiculture | It uses the default pool, proving segment-boundary matching |
| 5 | Observe POST check | The method, body bytes, and X-Demo header reach the backend |
| 6 | Look at balancer logs | Show method, path, pool, backend, status, outcome, and duration |
| 7 | Run the tests | Parser, routing, failure, and original TCP checks all pass |

Manual requests:

~~~powershell
curl.exe --max-time 5 -i "http://127.0.0.1:8080/api/items?course=CN"
curl.exe --max-time 5 -i "http://127.0.0.1:8080/static/style.css"
curl.exe --max-time 5 -i -H "Content-Type: text/plain" -H "X-Demo: week-2" --data-binary "hello" "http://127.0.0.1:8080/api/echo"
curl.exe --max-time 5 -i -X TRACE "http://127.0.0.1:8080/hello"
~~~

The final request should return 405 and an Allow header listing GET, HEAD, POST.
The returned Content-Length counts encoded bytes. Headers may be lowercased
because HTTP field names are case-insensitive.

To show a single backend failing, stop the launcher and start each backend in
its own terminal, keeping the balancer running:

~~~powershell
python demo_backend.py --port 9001 --name backend-1
python demo_backend.py --port 9002 --name backend-2
python demo_backend.py --port 9003 --name backend-3
~~~

Stop only backend-3. A request to /static/style.css now returns 502 for a refused
connection (or 504 if connection establishment times out). An /api request
still succeeds. Restart backend-3; /static works again without a balancer restart.
Avoid default-pool requests during this demonstration because that pool still
includes the stopped server.

## 7. What failure handling means at this stage

| Situation | Behavior |
| --- | --- |
| Bad headers / ambiguous body length | 400, before contacting a backend |
| Unsupported method | 405 with Allow |
| Header or body phase timeout | 408 |
| POST without Content-Length | 411 |
| Request body too large | 413 |
| Request target too long | 414 |
| Expect / 100-continue | 417 |
| Headers too large | 431 |
| Transfer-Encoding or protocol upgrade request | 501 |
| Invalid backend response or connection refusal before a response | 502 |
| Backend deadline before a response | 504 |
| Client HTTP version outside this subset | 505 |
| Backend body ends early after headers were sent | Close incomplete stream; log outcome=incomplete |

Once response bytes are sent, we cannot replace them with a new status line.
We close the connection so the client can detect incomplete framing. A log can
therefore show status=200 and outcome=incomplete: the status was sent, but the
body did not finish. Successful transfer of an origin error response can instead
show status=500 and outcome=complete.

No requests are retried or replayed. No health checker removes a failed backend
yet. A successful request after restart simply means the next connection worked.
These distinctions matter when explaining this milestone in a review.

## 8. Supported subset and tradeoffs

- One HTTP/1.1 request per client connection, with GET / HEAD / POST.
- A single valid Host header; duplicate or malformed Content-Length is rejected.
- Content-Length request bodies, at most 1 MiB with the supplied config.
- Response streaming uses the backend's Content-Length and handles HEAD / 204 /
  304 separately. It does not wait for EOF to finish a length-delimited body.
- Transfer-Encoding (including chunked), close-delimited backend bodies,
  informational backend responses, upgrades, and Expect are unsupported.
- Connection-specific headers are removed in both directions; Connection: close
  is explicit. The Host header and original request target are preserved.
- A second pipelined request is not forwarded; the connection closes after one
  response. Persistent connections and backend connection pooling are deferred.
- Default total phase deadlines are 5 / 5 / 3 / 10 seconds for request headers,
  request body, backend connection, and backend exchange respectively.
- The demo backend uses worker threads; the balancer uses asyncio. Responses
  demonstrate server selection, not a real API or static file application.
- This is an educational HTTP subset. There is no global connection cap or
  claim of full HTTP conformance or production security.

HTTP header semantics and connection-specific fields are described in
[RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html). Treat the list above as
our implementation contract, rather than a complete list of HTTP capabilities.

## 9. Review questions

**Why is this now Layer 7?** Selection depends on a parsed HTTP path.

**Why not use read(65536) for the whole request?** TCP read boundaries do not
identify HTTP message boundaries; a request can be fragmented or larger.

**Why keep separate pool counters?** Routing defines eligibility first.
Round-robin then distributes requests within that eligible set.

**Why is JSON useful?** Server addresses and route rules can change without
editing code, and invalid configuration is detected before serving clients.

**Why is equal request count not equal load?** Requests have different durations
and resource costs. This motivates next week's least-connections algorithm.

**Why not always return 502 after a failure?** If headers were already sent,
another status line would corrupt the response. Closing signals incompleteness.

**Why avoid retrying every failed POST?** The backend may have processed it
before the connection failed. Replaying it could duplicate a state change.

## 10. Evidence and next checkpoint

Run the entire local suite and unattended CLI demo:

~~~powershell
python -m unittest discover -s tests -v
python tests/smoke_week2.py
~~~

Actual demo output is in [evidence/week2/demo.txt](evidence/week2/demo.txt);
corresponding request logs are in
[evidence/week2/load-balancer.txt](evidence/week2/load-balancer.txt).
The smoke demo uses temporary ports so it does not disturb your running demo.

The captured run shows default-pool rotation, alternating API backends, a single
static backend, preserved POST body/header, a 400 rejection, and static backend
failure/recovery while the API pool remains usable. Its sequential/concurrent
delay measurements were 3.02 s and 1.01 s. These are one local observation.

Before Week 3, predict which pool /api, /api/users?next=/static, /apiculture,
and /static/logo.png will select, then verify with a request. Explain why a
pool with one member still works with the round-robin formula.

Next: least-connections, weighted round-robin, active health checks, JSON
request logs, and live statistics.
