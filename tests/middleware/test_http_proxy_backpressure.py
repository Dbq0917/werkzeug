from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import ephemeral_port_reserve
import pytest

from werkzeug.middleware.http_proxy import ProxyMiddleware
from werkzeug.serving import make_server
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="/proc based memory measurement is POSIX only"
)

BODY_SIZE = 8 * 1024 * 1024
CHUNK_SIZE = 16 * 1024
UPSTREAM_APP = Path(__file__).with_name("_slow_upstream_app.py")


def _rss_mb() -> float:
    with open(f"/proc/{os.getpid()}/status", encoding="ascii") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    raise RuntimeError("VmRSS not found")


@Request.application
def _root_app(request: Request) -> Response:
    return Response("ROOT")


@pytest.fixture()
def proxy_server():
    processes: list[subprocess.Popen[bytes]] = []
    servers: list = []

    def start(chunked: bool):
        upstream_port = ephemeral_port_reserve.reserve("127.0.0.1")
        process = subprocess.Popen(
            [
                sys.executable,
                os.fspath(UPSTREAM_APP),
                str(BODY_SIZE),
                "chunked" if chunked else "content-length",
                str(upstream_port),
            ]
        )
        processes.append(process)

        proxy = make_server(
            "127.0.0.1",
            0,
            ProxyMiddleware(
                _root_app,
                {"/up": {"target": f"http://127.0.0.1:{upstream_port}/"}},
            ),
            threaded=True,
        )
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        servers.append(proxy)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", upstream_port), 0.5):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("upstream test server did not start")

        return proxy

    yield start

    for server in servers:
        server.shutdown()
    for process in processes:
        process.terminate()
        process.wait()


def _open_proxy_response(port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(b"GET /up/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    sock.settimeout(30)
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)
    header_end = buf.index(b"\r\n\r\n") + 4
    return sock, len(buf) - header_end


@pytest.mark.parametrize("chunked", [False, True])
def test_upstream_reads_are_chunk_sized(proxy_server, chunked, monkeypatch):
    """The proxy never asks the target for more than ``chunk_size`` at once.

    The old implementation iterated the raw ``HTTPResponse``, whose
    ``readline`` pulls an unbounded amount of a fixed-length body into memory
    before the client has consumed anything.
    """
    read_sizes: list[int] = []
    original_read = http.client.HTTPResponse.read

    def tracking_read(self, amt=-1):
        data = original_read(self, amt)
        if data:
            read_sizes.append(len(data))
        return data

    monkeypatch.setattr(http.client.HTTPResponse, "read", tracking_read)

    proxy = proxy_server(chunked)
    sock, received = _open_proxy_response(proxy.port)

    while True:
        data = sock.recv(CHUNK_SIZE)
        if not data:
            break
        received += len(data)

    sock.close()
    assert received >= BODY_SIZE
    assert read_sizes
    assert max(read_sizes) <= CHUNK_SIZE


@pytest.mark.parametrize("chunked", [False, True])
def test_response_drains_without_buffering(proxy_server, chunked):
    """The full body is delivered while the proxy never holds more than a
    small fraction of it in Python memory at any point.
    """
    proxy = proxy_server(chunked)
    tracemalloc.start()
    base_current, _ = tracemalloc.get_traced_memory()
    sock, received = _open_proxy_response(proxy.port)

    try:
        while True:
            data = sock.recv(CHUNK_SIZE)
            if not data:
                break
            received += len(data)
        current, _ = tracemalloc.get_traced_memory()
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        sock.close()

    # Framing bytes are included in the wire count; only check the payload
    # all made it through.
    assert received >= BODY_SIZE
    assert peak - base_current < BODY_SIZE // 4
    assert current - base_current < BODY_SIZE // 4


@pytest.mark.parametrize("chunked", [False, True])
def test_slow_client_applies_backpressure(proxy_server, chunked):
    """A client reading only a few KB/s must not make the proxy buffer the
    upstream response. RSS stays flat while most of the body is unconsumed.
    """
    proxy = proxy_server(chunked)
    baseline = _rss_mb()
    sock, received = _open_proxy_response(proxy.port)

    try:
        for _ in range(40):
            data = sock.recv(8 * 1024)
            if not data:
                break
            received += len(data)
            time.sleep(0.05)
        growth = _rss_mb() - baseline
    finally:
        sock.close()

    # Only a small fraction of the body was consumed. The proxy must not
    # accumulate the remainder; leave slack for interpreter allocations.
    assert received < BODY_SIZE // 4
    assert growth < 4
