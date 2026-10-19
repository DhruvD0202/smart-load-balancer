"""Small, explicit HTTP/1.1 subset for the Week 2 reverse proxy.

One request per connection; Content-Length bodies only. Unsupported framing
is rejected rather than guessed. This is not a complete HTTP implementation.
"""

import asyncio
from dataclasses import dataclass
from http import HTTPStatus
import re

TOKEN = re.compile(rb"[!#$%&'*+\-.^_\x60|~0-9A-Za-z]+")
HOP_HEADERS = {"connection", "keep-alive", "proxy-connection", "te", "trailer",
               "transfer-encoding", "upgrade", "proxy-authorization",
               "proxy-authenticate"}
METHODS = ("GET", "HEAD", "POST")


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


@dataclass
class Request:
    method: str
    target: str
    path: str
    headers: list
    content_length: int


@dataclass
class Response:
    status: int
    reason: str
    headers: list
    body_length: int


async def read_head(reader, maximum, error_status=431):
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        raise HTTPError(error_status, "Headers exceed the configured limit") from None
    except asyncio.IncompleteReadError:
        raise HTTPError(400 if error_status == 431 else 502,
                        "Connection ended before complete headers") from None
    if len(raw) > maximum:
        raise HTTPError(error_status, "Headers exceed the configured limit")
    return raw


def split_head(raw, status):
    if not raw.endswith(b"\r\n\r\n"):
        raise HTTPError(status, "Incomplete headers")
    lines = raw[:-4].split(b"\r\n")
    if not lines[0] or any(c < 32 or c == 127 for c in lines[0]):
        raise HTTPError(status, "Invalid start line")
    headers = []
    for line in lines[1:]:
        name, colon, value = line.partition(b":")
        if not colon or not TOKEN.fullmatch(name):
            raise HTTPError(status, "Invalid header name or folded header")
        if any((c < 32 and c != 9) or c == 127 for c in value):
            raise HTTPError(status, "Invalid control character in header")
        headers.append((name.decode("ascii").lower(),
                        value.strip(b" \t").decode("latin-1")))
    return lines[0], headers


def values(headers, name):
    return [value for key, value in headers if key == name]


def content_length(headers, status):
    lengths = values(headers, "content-length")
    if not lengths:
        return None
    # Reject even identical duplicate lengths: this demo uses a strict subset.
    if (len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0])
            or len(lengths[0]) > 18):
        raise HTTPError(status, "Invalid or duplicate Content-Length")
    return int(lengths[0])


def connection_tokens(headers, status):
    tokens = set()
    for value in values(headers, "connection"):
        for token in value.split(","):
            token = token.strip().lower()
            if not token or not TOKEN.fullmatch(token.encode("latin-1")):
                raise HTTPError(status, "Invalid Connection header")
            tokens.add(token)
    # These fields describe the message, not just this connection.
    if tokens & {"host", "content-length", "transfer-encoding"}:
        raise HTTPError(status, "Connection cannot remove host or framing headers")
    return tokens


def parse_request(raw, body_limit):
    start, headers = split_head(raw, 400)
    try:
        method, target, version = start.decode("ascii").split(" ")
    except (ValueError, UnicodeDecodeError):
        raise HTTPError(400, "Expected METHOD /path HTTP/1.1") from None
    if not TOKEN.fullmatch(method.encode("ascii")):
        raise HTTPError(400, "Invalid method")
    if version != "HTTP/1.1":
        raise HTTPError(505, "This milestone accepts HTTP/1.1 only")
    if method not in METHODS:
        raise HTTPError(405, "Supported methods: GET, HEAD, POST")
    if len(target) > 2048:
        raise HTTPError(414, "Request target is too long")
    if (not target.startswith("/") or target.startswith("//")
            or any(c in target for c in "#\\") or any(ord(c) <= 32 for c in target)
            or re.search(r"%(?![0-9A-Fa-f]{2})", target)):
        raise HTTPError(400, "Use an origin-form target such as /api/items?limit=2")
    hosts = values(headers, "host")
    if (len(hosts) != 1 or not hosts[0]
            or any(ord(c) <= 32 or ord(c) >= 127 or c in "/\\?#@," for c in hosts[0])):
        raise HTTPError(400, "Exactly one nonempty Host header is required")
    tokens = connection_tokens(headers, 400)
    length = content_length(headers, 400)
    if values(headers, "transfer-encoding"):
        if length is not None:
            raise HTTPError(400, "Transfer-Encoding and Content-Length cannot coexist")
        raise HTTPError(501, "Transfer-Encoding is not supported in this milestone")
    if values(headers, "expect"):
        raise HTTPError(417, "Expect / 100-continue is not supported")
    if values(headers, "upgrade") or "upgrade" in tokens:
        raise HTTPError(501, "Protocol upgrades are not supported")
    if method == "POST" and length is None:
        raise HTTPError(411, "POST requires Content-Length")
    length = length or 0
    if length > body_limit:
        raise HTTPError(413, "Request body exceeds the configured limit")
    return Request(method, target, target.partition("?")[0], headers, length)


async def read_body(reader, request):
    try:
        return await reader.readexactly(request.content_length)
    except asyncio.IncompleteReadError:
        raise HTTPError(400, "Connection ended before the declared body length") from None


def forward_headers(headers, status):
    removed = HOP_HEADERS | connection_tokens(headers, status)
    kept = [(name, value) for name, value in headers if name not in removed]
    kept.append(("connection", "close"))
    return kept


def encode_head(start, headers):
    return (start + "\r\n" + "".join(f"{name}: {value}\r\n" for name, value in headers)
            + "\r\n").encode("latin-1")


def encode_request(request):
    return encode_head(f"{request.method} {request.target} HTTP/1.1",
                       forward_headers(request.headers, 400))


def parse_response(raw, method):
    start, headers = split_head(raw, 502)
    parts = start.decode("latin-1").split(" ", 2)
    if (len(parts) != 3 or parts[0] not in ("HTTP/1.0", "HTTP/1.1")
            or not re.fullmatch(r"[0-9]{3}", parts[1])):
        raise HTTPError(502, "Invalid backend status line")
    status = int(parts[1])
    if not 200 <= status <= 599:
        raise HTTPError(502, "Informational responses / upgrades are unsupported")
    connection_tokens(headers, 502)
    length = content_length(headers, 502)
    if values(headers, "transfer-encoding"):
        raise HTTPError(502, "Backend Transfer-Encoding is unsupported")
    if status == 204 and length is not None:
        raise HTTPError(502, "A 204 response cannot include Content-Length")
    no_body = method == "HEAD" or status in (204, 304)
    if not no_body and length is None:
        raise HTTPError(502, "Backend responses require Content-Length")
    return Response(status, parts[2], headers, 0 if no_body else length)


def encode_response(response):
    return encode_head(f"HTTP/1.1 {response.status} {response.reason}",
                       forward_headers(response.headers, 502))


async def send_error(writer, status, message, head_only=False, allow=METHODS):
    body = (f"{status} {HTTPStatus(status).phrase}\n{message}\n").encode("utf-8")
    headers = [("content-type", "text/plain; charset=utf-8"),
               ("content-length", str(len(body))), ("connection", "close")]
    if status == 405:
        headers.append(("allow", ", ".join(allow)))
    data = encode_head(f"HTTP/1.1 {status} {HTTPStatus(status).phrase}", headers)
    try:
        writer.write(data + (b"" if head_only else body))
        async with asyncio.timeout(2):
            await writer.drain()
    except (OSError, TimeoutError):
        pass  # The client may have disconnected before our error could be sent.
