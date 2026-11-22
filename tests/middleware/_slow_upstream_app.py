"""Minimal upstream app used by the proxy backpressure tests.

Run as a standalone process so its response buffers do not count toward the
proxy process memory measurements.
"""

from __future__ import annotations

import sys

from werkzeug.serving import make_server
from werkzeug.wrappers import Request
from werkzeug.wrappers import Response

body_size = int(sys.argv[1])
chunked = sys.argv[2] == "chunked"
port = int(sys.argv[3])


@Request.application
def app(request: Request) -> Response:
    if not chunked:
        return Response(b"x" * body_size, content_type="application/octet-stream")

    def generate():
        sent = 0
        block = b"x" * 16384
        while sent < body_size:
            yield block
            sent += len(block)

    return Response(generate(), content_type="application/octet-stream")


make_server("127.0.0.1", port, app, threaded=True).serve_forever()
