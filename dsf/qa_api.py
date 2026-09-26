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

import asyncio
import json
import os
import threading
import time
import traceback
from dataclasses import dataclass, field

import qa_db
import qa_gcode
import qa_queries
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


class LiveHub:
    """Fan-out of live frames to the WebSocket clients of the ``live`` endpoint.

    ``publish`` is called from the collector thread; each client lives in the endpoint's own
    asyncio loop, so frames are handed over with ``loop.call_soon_threadsafe``. A client that
    falls behind by ``MAX_QUEUE`` frames loses the oldest ones.
    """

    MAX_QUEUE = 500

    def __init__(self):
        self._clients = {}
        self._lock = threading.Lock()
        self._next = 1
        self.closed = False

    def register(self, loop):
        queue = asyncio.Queue()
        with self._lock:
            cid = self._next
            self._next += 1
            self._clients[cid] = (loop, queue)
        return cid, queue

    def unregister(self, cid):
        with self._lock:
            self._clients.pop(cid, None)

    def count(self):
        with self._lock:
            return len(self._clients)

    @staticmethod
    def _put(queue, text):
        while queue.qsize() >= LiveHub.MAX_QUEUE:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        queue.put_nowait(text)

    def publish(self, frame):
        with self._lock:
            clients = list(self._clients.values())
        if not clients:
            return
        text = json.dumps(frame, separators=(",", ":"))
        for loop, queue in clients:
            try:
                loop.call_soon_threadsafe(self._put, queue, text)
            except RuntimeError:
                pass  # loop closed

    def close(self):
        self.closed = True
        with self._lock:
            clients = list(self._clients.values())
        for loop, queue in clients:
            try:
                loop.call_soon_threadsafe(self._put, queue, None)
            except RuntimeError:
                pass


@dataclass
class ApiContext:
    version: str = "unknown"
    started: float = field(default_factory=time.monotonic)
    # The command connection is shared by all endpoint threads and is not thread-safe
    cmd_lock: threading.Lock = field(default_factory=threading.Lock)
    settings: object = None
    writer: object = None
    readers: object = None
    data_dir: str = ""
    index_cache: object = None
    prepare_result: str = ""
    resolve_path: object = None
    collector: object = None
    timelapse: object = None
    live: LiveHub = field(default_factory=LiveHub)


def query(request, key, default=None):
    """A query parameter as string (DSF passes them decoded, one value per key)."""
    queries = getattr(request, "queries", None) or {}
    value = queries.get(key, default)
    if isinstance(value, list):
        value = value[0] if value else default
    return value


def _int(request, key, default, low=None, high=None):
    try:
        value = int(query(request, key, default))
    except (TypeError, ValueError):
        return default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _job_id(request):
    job_id = query(request, "id")
    if not job_id:
        raise ApiError(400, "missing 'id'")
    return job_id


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _con(ctx):
    return ctx.readers.get()


def _found(value, what="job"):
    if value is None:
        raise ApiError(404, f"{what} not found")
    return json_response(value)


# --- handlers -----------------------------------------------------------------------------------

def handle_status(ctx, _request):
    writer = ctx.writer
    collector = ctx.collector
    settings = ctx.settings.current() if ctx.settings is not None else {}
    timelapse = ctx.timelapse
    return json_response({
        "version": ctx.version,
        "uptimeS": round(time.monotonic() - ctx.started, 1),
        "collector": collector.status() if collector is not None else None,
        "database": {
            "sizeBytes": qa_db.file_size(ctx.data_dir) if ctx.data_dir else None,
            "startup": ctx.prepare_result,
            "queue": writer.queue_size() if writer is not None else None,
            "lastError": writer.last_error if writer is not None else None,
            "lastBackup": qa_queries.iso(writer.last_backup) if writer is not None else None,
            "lastRetention": writer.last_retention if writer is not None else None,
            "retention": settings.get("retention"),
        },
        "settingsErrors": ctx.settings.errors if ctx.settings is not None else [],
        "liveClients": ctx.live.count(),
        "timelapse": timelapse.status() if timelapse is not None else {
            "enabled": False, "reason": "not available in this version"},
        "accelerometer": {"enabled": False, "reason": "postponed (no board reports an accelerometer)"},
    })


def handle_settings_get(ctx, _request):
    return json_response({"settings": ctx.settings.current(), "errors": ctx.settings.errors})


def handle_settings_post(ctx, request):
    try:
        given = json.loads(getattr(request, "body", "") or "{}")
    except ValueError:
        raise ApiError(400, "body is not JSON")
    settings, errors = ctx.settings.update(given)
    # Always 200: DWC's REST connector drops the body of a 4xx answer ("bad status code 422"),
    # so the errors would never reach the settings form (@duet3d/connectors RestConnector.request)
    if errors:
        return json_response({"saved": False, "settings": ctx.settings.current(), "errors": errors})
    return json_response({"saved": True, "settings": settings, "errors": []})


def handle_jobs(ctx, request):
    return json_response(qa_queries.jobs(_con(ctx), limit=_int(request, "limit", 50, 1, 1000),
                                         offset=_int(request, "offset", 0, 0), result=query(request, "result"),
                                         material=query(request, "material")))


def handle_job(ctx, request):
    return _found(qa_queries.job(_con(ctx), _job_id(request)))


def handle_layers(ctx, request):
    load = ctx.settings.current()["heaterLoad"] if ctx.settings is not None else None
    thresholds = {"high": load["high"], "limit": load["limit"]} if load else None
    return _found(qa_queries.layers(_con(ctx), _job_id(request), thresholds))


def handle_events(ctx, request):
    return _found(qa_queries.events(_con(ctx), _job_id(request), query(request, "type")))


def handle_blocks(ctx, request):
    return _found(qa_queries.blocks(_con(ctx), _job_id(request)))


def handle_spectra(ctx, request):
    return _found(qa_queries.spectra(_con(ctx), _job_id(request)))


def handle_samples(ctx, request):
    names = [n for n in (query(request, "channels") or "").split(",") if n]
    if not names:
        raise ApiError(400, "missing 'channels'")
    if len(names) > 50:
        raise ApiError(400, "at most 50 channels per request")
    resolution = query(request, "resolution", "auto")
    if resolution not in ("coarse", "fine", "auto"):
        raise ApiError(400, "resolution must be coarse, fine or auto")
    start = query(request, "from")
    end = query(request, "to")
    try:
        start = int(start) if start not in (None, "") else None
        end = int(end) if end not in (None, "") else None
    except ValueError:
        raise ApiError(400, "'from' and 'to' are epoch milliseconds")
    return _found(qa_queries.samples(_con(ctx), _job_id(request), names, start, end, resolution))


def handle_channels(ctx, _request):
    return json_response(qa_queries.channels(_con(ctx)))


def handle_trends(ctx, request):
    metric = query(request, "metric")
    if not metric:
        raise ApiError(400, f"missing 'metric' ({', '.join(qa_queries.TREND_METRICS)})")
    try:
        return json_response(qa_queries.trends(_con(ctx), metric, _int(request, "limit", 100, 1, 1000),
                                               query(request, "material")))
    except ValueError as exc:
        raise ApiError(400, str(exc))


def handle_reference_get(ctx, _request):
    return json_response(qa_queries.references(_con(ctx)))


def handle_reference_post(ctx, request):
    """``{"axis": "X", "spectrumId": 12}`` sets a manual reference, ``{"axis": "X", "mode": "auto"}``
    returns to the automatic one."""
    try:
        body = json.loads(getattr(request, "body", "") or "{}")
    except ValueError:
        raise ApiError(400, "body is not JSON")
    axis = body.get("axis")
    if axis not in ("X", "Y", "Z"):
        raise ApiError(400, "axis must be X, Y or Z")
    if body.get("mode") == "auto":
        ctx.writer.call("reference_set", axis, None, "auto")
    elif isinstance(body.get("spectrumId"), int):
        if not ctx.writer.call("reference_set", axis, body["spectrumId"], "manual"):
            raise ApiError(404, "spectrum not found")
    else:
        raise ApiError(400, "give spectrumId or mode 'auto'")
    return json_response(qa_queries.references(_con(ctx)))


class _CrcCache:
    """CRC32 per (path, size, mtime), so the toolpath check does not reread the file every time."""

    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()

    def get(self, path):
        import qa_context

        stat = os.stat(path)
        key = (path, stat.st_size, stat.st_mtime_ns)
        with self._lock:
            if key in self._items:
                return self._items[key]
        crc = qa_context.file_crc32(path)
        with self._lock:
            if len(self._items) > 64:
                self._items.clear()
            self._items[key] = crc
        return crc


_crc_cache = _CrcCache()


def handle_toolpath(ctx, request):
    job = qa_queries.job(_con(ctx), _job_id(request))
    if job is None:
        raise ApiError(404, "job not found")
    layer = _int(request, "layer", None, 0)
    if layer is None:
        raise ApiError(400, "missing 'layer'")
    path = ctx.resolve_path(job["file"]) if ctx.resolve_path and job["file"] else None
    if not path or not os.path.isfile(path):
        return json_response({"error": "the job file is gone", "file": job["file"]}, 409)
    crc = _crc_cache.get(path)
    if crc != job["fileCrc32"]:
        return json_response({"error": "the job file has changed since the job", "file": job["file"]}, 409)
    index, state = ctx.index_cache.get(crc)
    if index is None:
        if state == "missing":
            state = ctx.index_cache.ensure(path, crc)
        if state.startswith("error"):
            return json_response({"error": f"layer index failed: {state}"}, 500)
        return json_response({"state": "building"}, 202)
    extruders = (job.get("context") or {}).get("extruders") or [{}]
    diameter = extruders[0].get("filamentDiameter") or 1.75
    result = qa_gcode.toolpath(path, index, layer, diameter)
    if result is None:
        raise ApiError(404, "layer not in the file")
    result["meta"] = {"numLayers": index["numLayers"], "source": index["source"], "objects": index["objects"],
                      "filamentDiameter": diameter}
    return json_response(result)


EXPORT_MAX_AGE_S = 3600


def handle_export(ctx, request):
    """The job as one JSON file, sent by DSF from disk (large answers do not go through the socket)."""
    data = qa_queries.export(_con(ctx), _job_id(request))
    if data is None:
        raise ApiError(404, "job not found")
    directory = os.path.join(ctx.data_dir, "tmp")
    os.makedirs(directory, exist_ok=True)
    now = time.time()
    for name in os.listdir(directory):
        full = os.path.join(directory, name)
        try:
            if name.startswith("export-") and now - os.path.getmtime(full) > EXPORT_MAX_AGE_S:
                os.remove(full)
        except OSError:
            pass
    path = os.path.join(directory, f"export-{data['job']['id']}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))
    return Response(200, path, "file")


# (method, path) -> handler
ENDPOINTS = {
    ("GET", "status"): handle_status,
    ("GET", "settings"): handle_settings_get,
    ("POST", "settings"): handle_settings_post,
    ("GET", "jobs"): handle_jobs,
    ("GET", "job"): handle_job,
    ("GET", "job/layers"): handle_layers,
    ("GET", "job/events"): handle_events,
    ("GET", "job/samples"): handle_samples,
    ("GET", "job/blocks"): handle_blocks,
    ("GET", "job/spectra"): handle_spectra,
    ("GET", "job/toolpath"): handle_toolpath,
    ("GET", "job/export"): handle_export,
    ("GET", "trends"): handle_trends,
    ("GET", "spectra/reference"): handle_reference_get,
    ("POST", "spectra/reference"): handle_reference_post,
    ("GET", "channels"): handle_channels,
}


def call(ctx, func, request):
    """Run a handler; API errors become their status, anything else 500."""
    try:
        return func(ctx, request)
    except ApiError as exc:
        return error_response(exc.message, exc.status)


def _make_handler(ctx, func):
    from dsf.http import HttpResponseType

    kinds = {"json": HttpResponseType.JSON, "text": HttpResponseType.PlainText, "file": HttpResponseType.File}

    async def _handler(http_conn):
        try:
            request = await http_conn.read_request()
            response = call(ctx, func, request)
            body = response.body
            if response.kind == "json":
                body = json.dumps(body, separators=(",", ":"))
            await http_conn.send_response(response.status, body if body is not None else "", kinds[response.kind])
        except Exception:  # noqa: BLE001
            logger.error("handler error: %s", traceback.format_exc())
            try:
                await http_conn.send_response(500, json.dumps({"error": "internal error"}), HttpResponseType.JSON)
            except Exception:  # noqa: BLE001
                pass

    return _handler


LIVE_PING_S = 25  # below the 30 s the plan asks for; the HMI's haproxy tunnel times out after 1 h


def make_live_handler(ctx):
    """WebSocket ``live``: DSF opens one connection per browser client and forwards every
    ``send_response`` as a text frame; nothing is read on connect (CustomEndpointMiddleware,
    DSF v3.7-dev @ cd3ae65f). Frames: hello, sample, event, layer, job, status, ping."""
    from dsf.http import HttpResponseType

    async def _handler(http_conn):
        loop = asyncio.get_running_loop()
        cid, queue = ctx.live.register(loop)
        reader = asyncio.ensure_future(http_conn.reader.read(65536))
        getter = None
        try:
            hello = {"type": "hello", "ts": qa_db.now_ms(), "version": ctx.version,
                     "status": ctx.collector.status() if ctx.collector is not None else None}
            await http_conn.send_response(200, json.dumps(hello), HttpResponseType.JSON)
            while not ctx.live.closed:
                if getter is None:
                    getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, reader}, timeout=LIVE_PING_S,
                                             return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    if not reader.result():
                        break  # the browser went away
                    reader = asyncio.ensure_future(http_conn.reader.read(65536))  # client text is ignored
                if getter in done:
                    text = getter.result()
                    getter = None
                    if text is None:
                        break
                    await http_conn.send_response(200, text, HttpResponseType.JSON)
                elif not done:
                    await http_conn.send_response(200, json.dumps({"type": "ping", "ts": qa_db.now_ms()}),
                                                  HttpResponseType.JSON)
        except (ConnectionError, OSError):
            pass
        except Exception:  # noqa: BLE001
            logger.error("live endpoint error: %s", traceback.format_exc())
        finally:
            ctx.live.unregister(cid)
            for task in (reader, getter):
                if task is not None:
                    task.cancel()
            try:
                http_conn.close()
            except Exception:  # noqa: BLE001
                pass

    return _handler


def register_endpoints(cmd, ctx, endpoints=None, live=True):
    """Register every endpoint with DSF. Returns the endpoint sockets (close them on shutdown)."""
    from dsf.object_model import HttpEndpointType

    registered = []
    items = [(method, path, _make_handler(ctx, func)) for (method, path), func in (endpoints or ENDPOINTS).items()]
    if live:
        items.append(("WebSocket", "live", make_live_handler(ctx)))
    for method, path, handler in items:
        endpoint_type = {"GET": HttpEndpointType.GET, "POST": HttpEndpointType.POST,
                         "WebSocket": HttpEndpointType.WebSocket}[method]
        try:
            with ctx.cmd_lock:
                endpoint = cmd.add_http_endpoint(endpoint_type, API_NAMESPACE, path)
            endpoint.set_endpoint_handler(handler)
            registered.append(endpoint)
        except Exception as exc:  # noqa: BLE001
            logger.error("failed to register %s %s: %s", method, path, exc)
    return registered
