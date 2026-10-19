"""Parser edge cases that otherwise cause incorrect routing or message framing."""

import unittest

from http_protocol import (HTTPError, encode_request, parse_request, parse_response)


def request(target="/api/items?x=1", method="GET", headers=b"", body=b""):
    return (f"{method} {target} HTTP/1.1\r\n".encode()
            + b"Host: demo.test\r\n" + headers + b"\r\n" + body)


class ProtocolTests(unittest.TestCase):
    def test_path_query_and_headers_are_parsed_separately(self):
        parsed = parse_request(request(headers=b"x-DEMO:  value \t\r\n"), 1024)
        self.assertEqual((parsed.method, parsed.target, parsed.path),
                         ("GET", "/api/items?x=1", "/api/items"))
        self.assertIn(("x-demo", "value"), parsed.headers)

    def test_unsafe_or_unsupported_requests_have_explicit_errors(self):
        cases = [
            (b"GET / HTTP/1.1\r\n\r\n", 400),
            (request(headers=b"Host: second\r\n"), 400),
            (request(headers=b"Bad Header: x\r\n"), 400),
            (request(headers=b"X: good\r\n folded\r\n"), 400),
            (request(headers=b"X: bad\x00value\r\n"), 400),
            (request(headers=b"Content-Length: -1\r\n"), 400),
            (request(headers=b"Content-Length: 0\r\nContent-Length: 0\r\n"), 400),
            (request(headers=b"Content-Length: 0, 0\r\n"), 400),
            (request(headers=b"Transfer-Encoding: chunked\r\nContent-Length: 0\r\n"), 400),
            (request(headers=b"Transfer-Encoding: chunked\r\n"), 501),
            (request(headers=b"Expect: 100-continue\r\n"), 417),
            (request(headers=b"Upgrade: websocket\r\n"), 501),
            (request(headers=b"Connection: Content-Length\r\n"), 400),
            (request(method="POST"), 411),
            (request(method="POST", headers=b"Content-Length: 1025\r\n"), 413),
            (request(method="CONNECT"), 405),
            (request(target="http://demo.test/"), 400),
            (request(target="//demo.test/"), 400),
            (request(target="/hello#fragment"), 400),
            (request(target="/bad%XX"), 400),
            (request(target="/" + "a" * 2048), 414),
            (b"GET / HTTP/1.0\r\nHost: demo\r\n\r\n", 505),
        ]
        for raw, status in cases:
            with self.subTest(raw=raw[:100]):
                with self.assertRaises(HTTPError) as raised:
                    parse_request(raw, 1024)
                self.assertEqual(raised.exception.status, status)

    def test_hop_headers_removed_and_end_to_end_duplicates_kept(self):
        parsed = parse_request(request(headers=(
            b"Connection: keep-alive, X-Private\r\nKeep-Alive: timeout=10\r\n"
            b"X-Private: remove-me\r\nX-Demo: first\r\nX-Demo: second\r\n"
        )), 1024)
        forwarded = encode_request(parsed)
        self.assertNotIn(b"x-private:", forwarded)
        self.assertNotIn(b"keep-alive:", forwarded)
        self.assertIn(b"connection: close\r\n", forwarded)
        self.assertEqual(forwarded.count(b"x-demo:"), 2)

    def test_post_body_length_is_bytes(self):
        parsed = parse_request(request(method="POST", headers=b"Content-Length: 6\r\n"),
                               1024)
        self.assertEqual(parsed.content_length, len("你好".encode("utf-8")))

    def test_head_and_bodyless_responses_do_not_read_a_body(self):
        cases = [(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n", "HEAD"),
                 (b"HTTP/1.1 204 No Content\r\n\r\n", "GET"),
                 (b"HTTP/1.1 304 Not Modified\r\nContent-Length: 100\r\n\r\n", "GET")]
        for raw, method in cases:
            with self.subTest(raw=raw):
                self.assertEqual(parse_response(raw, method).body_length, 0)

    def test_invalid_backend_framing_is_rejected(self):
        cases = [
            b"HTTP/1.1 200 OK\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\n",
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 101 Switching Protocols\r\n\r\n",
            b"HTTP/1.1 two OK\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nConnection: Content-Length\r\nContent-Length: 0\r\n\r\n",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(HTTPError) as raised:
                    parse_response(raw, "GET")
                self.assertEqual(raised.exception.status, 502)
