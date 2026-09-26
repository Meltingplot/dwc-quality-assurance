"""HTTP endpoints under /machine/QualityAssurance/.

Handlers are plain functions ``handler(ctx, request) -> Response`` so they can be tested
without DSF. ``register_endpoints`` wraps each in the async callback dsf-python expects.

dsf-python 3.7.0b1 serves every endpoint from its own thread with its own asyncio loop
(``HttpEndpointUnixSocket``: ``ThreadPoolExecutor(max_workers=1)``), so handlers run
concurrently with each other and with the collector. Anything they share needs a lock:
the command connection is guarded by ``ApiContext.cmd_lock`` (lesson from the CHX350
backend, 2026-09-26), SQLite reads use one connection per thread.
``read_request`` reads at most 32 KiB, so POST bodies stay small.
"""

import json
import threading
import time
import traceback
from dataclasses import dataclass, field

from qa_log import PLUGIN_ID, logger

API_NAMESPACE = PLUGIN_ID


@dataclass
class Response:
    status: int = 200
    body: object = None
    # "json" | "text" | "file" (body is a path DSF sends as application/octet-stream)
    kind: str = "json"


def json_response(data, status=200):
    return Response(status, data, "json")


def error_response(message, status=400):
    return Response(status, {"error": message}, "json")


@dataclass
class ApiContext:
    version: str = "unknown"
    started: float = field(default_factory=time.monotonic)
    # The command connection is shared by all endpoint threads and is not thread-safe
    cmd_lock: threading.Lock = field(default_factory=threading.Lock)


def query(request, key, default=None):
    """A query parameter as string (DSF passes them decoded, one value per key)."""
    queries = getattr(request, "queries", None) or {}
    value = queries.get(key, default)
    if isinstance(value, list):
        value = value[0] if value else default
    return value


def handle_status(ctx, _request):
    return json_response({
        "version": ctx.version,
        "uptimeS": round(time.monotonic() - ctx.started, 1),
    })


# (method, path) -> handler
ENDPOINTS = {
    ("GET", "status"): handle_status,
}


def _make_handler(ctx, func):
    from dsf.http import HttpResponseType

    kinds = {"json": HttpResponseType.JSON, "text": HttpResponseType.PlainText, "file": HttpResponseType.File}

    async def _handler(http_conn):
        try:
            request = await http_conn.read_request()
            response = func(ctx, request)
            body = response.body
            if response.kind == "json":
                body = json.dumps(body)
            await http_conn.send_response(response.status, body if body is not None else "", kinds[response.kind])
        except Exception:  # noqa: BLE001
            logger.error("handler error: %s", traceback.format_exc())
            try:
                await http_conn.send_response(500, json.dumps({"error": "internal error"}), HttpResponseType.JSON)
            except Exception:  # noqa: BLE001
                pass

    return _handler


def register_endpoints(cmd, ctx, endpoints=None):
    """Register every endpoint with DSF. Returns the endpoint sockets (close them on shutdown)."""
    from dsf.object_model import HttpEndpointType

    registered = []
    for (method, path), func in (endpoints or ENDPOINTS).items():
        endpoint_type = {"GET": HttpEndpointType.GET, "POST": HttpEndpointType.POST}[method]
        try:
            with ctx.cmd_lock:
                endpoint = cmd.add_http_endpoint(endpoint_type, API_NAMESPACE, path)
            endpoint.set_endpoint_handler(_make_handler(ctx, func))
            registered.append(endpoint)
        except Exception as exc:  # noqa: BLE001
            logger.error("failed to register %s %s: %s", method, path, exc)
    return registered
