# Week 1 — understand and demonstrate the TCP foundation

**Status: technically review-ready.** On Windows / Python 3.13.0, all ten local
socket integration tests passed. The actual CLI smoke demo also passed.
The saved run completed three sequential delayed requests in **3.01 s** and
three concurrent requests in **1.01 s**, all with the expected response paths.
These are observations from one local run, not promised timings.

Your learning checkpoint is to reproduce the demo and explain the flow below
before moving to HTTP parsing and load balancing.

## 1. Understand the network path

~~~text
Client                 Proxy                   Backend
                       listens on 8080         listens on 9001
  |--- TCP connect ------>|                         |
  |                       |--- TCP connect ------->|
  |--- HTTP request ----->|--- same bytes -------->|
  |<-- same bytes --------|<-- HTTP response ------|
~~~

An IP address identifies a network endpoint's host address. A port identifies
which listening service to contact at that address. 127.0.0.1 is the loopback
address: the traffic stays on this computer. Ports 8080 and 9001 distinguish
the two services even though their IP addresses are identical.

A TCP connection is a reliable, ordered **byte stream**. It has two directions:
each endpoint can send and receive. The proxy terminates the client's TCP
connection and creates a separate backend TCP connection. The two connections
have separate TCP state, sequence numbers, and transport flow control.

HTTP is the application protocol carried by those bytes. A request might start
with GET /hello HTTP/1.1. The backend interprets that request and produces a
status, headers, and body. Our Week 1 proxy simply copies their bytes.

This is a TCP-level forwarding foundation, not yet the promised Layer 7 load
balancer. Once the proxy parses an HTTP path and uses it to choose a backend
pool, it makes application-layer decisions.

## 2. Learn the asyncio building blocks

Network code often waits for a connection, incoming bytes, or room in an output
buffer. We want another client to progress while one client is waiting.

| Pattern | Why we need it here |
| --- | --- |
| async def | Defines a coroutine function whose work can pause at await points |
| await | Waits for an asynchronous result; if it must suspend, the event loop can run other ready tasks |
| Event loop | Coordinates ready work and waiting network operations, using one proxy event-loop thread |
| asyncio.run | Starts the event loop, runs our top-level coroutine, and manages loop shutdown |
| asyncio.start_server | Listens for clients and schedules our async handler for each accepted connection |
| asyncio.open_connection | Opens the separate connection to the backend |
| StreamReader / StreamWriter | Give us convenient byte-reading and byte-writing interfaces to a connection |
| asyncio.create_task | Schedules each forwarding coroutine so both directions can progress |
| asyncio.gather | Waits for the forwarding tasks together |
| asyncio.timeout | Sets a deadline so a stalled operation cannot wait indefinitely |
| finally | Cleans up tasks and sockets on success, error, or cancellation |

Calling an async function creates a coroutine object; it does not by itself
run that work. Use await, create_task, or the top-level asyncio.run as appropriate.
An await is not a command to create a new thread, and a ready operation can
complete without suspending. Asyncio overlaps I/O waits; it does not make
CPU-heavy Python calculations automatically run in parallel.

Read [Python's coroutine and task guide](https://docs.python.org/3.13/library/asyncio-task.html)
for the exact API behavior.

## 3. Read the proxy in three small passes

### Pass A: relay — move bytes in one direction

The core loop is:

~~~python
data = await reader.read(BUFFER_SIZE)
writer.write(data)
await writer.drain()
~~~

Read means "give me up to this many currently available bytes." It does not
mean "give me one complete HTTP request." The actual function repeats this,
handles EOF, and adds deadlines.

write queues bytes for sending. drain applies **backpressure**: if the outgoing
buffer is full enough, pause until it has room again. Without flow control,
a fast sender could keep filling buffers while a slow receiver falls behind.
drain does not mean the remote application has processed the data.

We relay bytes directly rather than decoding/re-encoding them, which preserves
binary bodies and arbitrary TCP data. The 64 KiB read size is a chunk limit,
not a maximum request size or a TCP packet size.

See [Python's streams documentation](https://docs.python.org/3.13/library/asyncio-stream.html)
for read, write, drain, and write_eof.

### Pass B: handle_client — connect the two directions

The handler opens the backend connection, schedules client-to-backend and
backend-to-client relay tasks, and waits for both.

If we awaited the entire upload relay before starting the download relay, an
HTTP client waiting for a response could get stuck: it may keep its sending
side open while waiting. Both directions need to be active together.

An empty read means **EOF**: that peer has finished sending. TCP supports a
half-close, where one direction ends while the opposite direction remains
usable. We forward EOF with write_eof and let the other relay finish.
Closing the entire connection immediately could discard a delayed response.

If a relay raises, gather does not automatically cancel its sibling. Our finally
block cancels any remaining relay tasks, waits for their cleanup, and closes
both streams. This prevents a task from continuing to use an abandoned socket.
CancelledError is re-raised so application shutdown can continue normally.

### Pass C: serve — accept many clients

start_server invokes handle_client for each connection. serve_forever keeps the
listener active. Each handler can suspend on I/O, leaving the event loop free
to advance other handlers. The backend is fixed for this milestone.

## 4. Run the demonstration

Allow five to seven minutes. The Week 1 program remains available as proxy.py;
the main README now starts the Week 2 HTTP balancer. Stop the Week 2 servers
before using the same ports for this older demonstration.

Run these in separate backend, proxy, and client terminals in the project folder:

~~~powershell
# Terminal 1
python demo_backend.py --port 9001 --name backend-1
# Terminal 2
python proxy.py --port 8080 --backend-port 9001
# Terminal 3
curl.exe --max-time 5 -i "http://127.0.0.1:8080/hello"
python demo_check.py
~~~

Keep the backend, proxy, and client terminals visible.

| Step | Action | What you should see / say |
| --- | --- | --- |
| 1 | Start backend on 9001 and proxy on 8080 | "These are separate listening services connected by TCP." |
| 2 | Request http://127.0.0.1:9001/hello directly | HTTP 200 and backend-1 JSON; confirms the backend works |
| 3 | Request http://127.0.0.1:8080/hello through the proxy | Same backend identity and path; show the proxy connection log |
| 4 | Run python demo_check.py | Three sequential waits take about 3 s; three concurrent waits take about 1 s |
| 5 | Stop ONLY the backend with Ctrl+C | Proxy stays running |
| 6 | Send another request through the proxy | curl reports an empty reply/reset; proxy logs connection_failed |
| 7 | Restart backend with the original command and repeat the request | HTTP 200 returns without restarting the proxy |
| 8 | Run the ten socket tests | All pass; explain the tests cover behavior beyond one browser request |

Client commands for the comparison and failure steps:

~~~powershell
curl.exe --max-time 5 -i "http://127.0.0.1:9001/hello"
curl.exe --max-time 5 -i "http://127.0.0.1:8080/hello"
python demo_check.py
python -m unittest discover -s tests -p test_proxy.py -v
~~~

The failed request itself is not recovered, retried, or turned into a friendly
HTTP error at this stage. Only later requests work after the backend returns.
That is connection failure isolation, not health checking or failover.

For an unattended repeatable check, run:

~~~powershell
python tests/smoke_demo.py
~~~

It uses temporary local ports, verifies GET and HEAD, runs the concurrency
comparison, stops/restarts its backend, and stops all processes it started.
It writes real output to [week1-demo.txt](evidence/week1-demo.txt),
[proxy.txt](evidence/proxy.txt), and backend logs beside them.

## 5. Read your logs correctly

connection_open records the client IP/port and the chosen backend address.
connection_failed records a connection/relay failure and its exception type.
connection_end records application byte counts and total connection lifetime.

These are basic connection logs. HTTP paths and per-request response latency
cannot be reported accurately until the proxy understands request boundaries.
A normal TCP EOF can still contain a truncated HTTP response; only an
HTTP-aware layer can compare the body to its declared length.

For final review evidence, capture:

- A client response through port 8080 alongside the corresponding proxy log.
- The sequential/concurrent demo output.
- Backend failure followed by successful recovery without a proxy restart.
- The passing tests.

The automated evidence is saved now. Take screenshots of your own rehearsal
when preparing the final report and presentation.

## 6. Practice explaining the design

**Give a 30-second explanation.**

"My proxy accepts a client TCP connection and opens a separate TCP connection
to a backend. It uses two asyncio tasks to relay bytes in opposite directions.
While one task waits for network I/O, the event loop can serve other clients.
It handles timeouts and cleans up failed connections. This milestone establishes
the transport foundation; next I will parse HTTP and select among backends."

**Why use asyncio?** Network waits can overlap without a proxy worker thread
for every client. It suits this I/O-bound workload and keeps task coordination
explicit. It is not a claim that asyncio is always faster than threads.

**Why does the test backend use threads?** It is a separate test fixture using
Python's built-in HTTP handler. Threading lets its artificial delays overlap.
Our learning and implementation focus is the asyncio proxy. See the
[standard-library HTTP server reference](https://docs.python.org/3/library/http.server.html).

**Why does TCP need two connections here?** The proxy is a TCP endpoint for the
client and a separate TCP client of the backend. It receives and sends bytes;
it does not rewrite and forward the original IP packets like a router.

**Does the proxy implement a TCP sliding window?** No. The operating system
handles TCP sequencing, acknowledgments, retransmission, and window-based
transport flow control. Our application uses streams and buffer backpressure.

**Can it choose /api versus /static yet?** No. That requires parsing the HTTP
request and applying routing rules, which is the next milestone.

**What happens if a backend fails halfway through a response?** The client
gets an incomplete response or connection failure. The proxy handles the socket
closure/error and stays available. It cannot undo already forwarded bytes.
Future HTTP handling will detect framing errors and define safe error behavior.

**How do you know concurrent handling really works?** A socket test makes six
backend handlers wait until all six clients arrive before releasing any response.
A proxy that serves clients sequentially cannot pass that test. The /slow timing
demo makes the same idea visible without claiming production throughput.

## 7. Common beginner mistakes

- Forgetting await on a coroutine: work may never run.
- Writing await writer.write(...): write is synchronous; await drain instead.
- Using time.sleep inside proxy code: it blocks the event-loop thread.
  The demo backend's sleep runs in its own worker thread.
- Treating one read as one HTTP message: TCP has no message boundaries.
- Waiting for upload EOF before starting the response relay: clients may be
  waiting for the response while keeping the connection open.
- Closing both directions at the first EOF: delayed responses can be lost.
- Swallowing every exception: expected socket errors should be handled, while
  unexpected programming errors should remain visible.
- Calling connection duration "HTTP latency," or reconnecting "health checks":
  these are different measurements and mechanisms.

## 8. Small exercises before Week 2

1. Trace a request from port 8080 through each function in proxy.py.
2. Predict what will happen if --backend-port points to an unused port.
3. Restart the backend with --delay 2 and predict the two demo timings, then run it.
4. Explain why a 2 MiB body works even though each read is limited to 64 KiB.
5. Explain why all requests still go to backend-1 and what must change to balance them.

If a port is occupied, stop your previous demo instance or choose a different
port using --port; update --backend-port and the client URL accordingly.
If the client fails immediately, first confirm the backend works directly,
then check the proxy's backend host/port and the proxy terminal's error message.
