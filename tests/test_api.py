"""HTTP API handlers against a database filled by a real collector run, and the live WebSocket
endpoint over real asyncio streams. The contract tests pin what the CHX UI and a later Quality
Control plugin rely on (PLAN.md §5.6, §5.10); docs/api.md describes the same shapes."""

import asyncio
import json
import os
import threading

import pytest

import qa_api
import qa_db
import qa_gcode


class Req:
    """A request as dsf-python hands it over; ``session_id`` -1 is an anonymous one."""

    def __init__(self, body="", session_id=7, **queries):
        self.queries = {k: str(v) for k, v in queries.items()}
        self.body = body
        self.session_id = session_id


@pytest.fixture
def ctx(rig, data_dir, settings, writer):
    context = qa_api.ApiContext(version="test", settings=settings, writer=writer, readers=qa_db.Readers(data_dir),
                                data_dir=data_dir, index_cache=rig.index, prepare_result="created",
                                resolve_path=lambda v: str(rig.gcode) if v == "0:/gcodes/a.gcode" else None,
                                collector=rig.collector)
    return context


def get(ctx, path, **queries):
    response = qa_api.call(ctx, qa_api.ENDPOINTS[("GET", path)], Req(**queries))
    return response.status, response.body


def run_job(rig, cancel=False):
    rig.start_job()
    for layer in (1, 2, 3):
        rig.patch({"job": {"layer": layer, "duration": layer * 20},
                   "heat": {"heaters": [{"current": 60}, {"current": 220, "avgPwm": 0.6}]},
                   "move": {"currentMove": {"extrusionRate": 1.0}, "axes": [{}, {}, {"machinePosition": 0.2 * layer}]},
                   "sensors": {"filamentMonitors": [{"lastPercentage": 99, "avgPercentage": 98,
                                                     "totalExtrusion": 10.0 * layer,
                                                     "calibrated": {"mmPerRev": 25.0, "totalDistance": 10.0 * layer}}]}})
        for _ in range(12):
            rig.patch({"heat": {"heaters": [{}, {"avgPwm": 0.6}]}}, dt_ms=1000)
    rig.patch({"heat": {"heaters": [{}, {"current": 213}]}})  # a jump: one fine block
    rig.patch({"state": {"status": "idle"}, "job": {"duration": None, "layer": None}})
    rig.patch({"job": {"lastFileCancelled": cancel}} if cancel else {"job": {"lastFileAborted": False}})
    if not cancel:
        rig.collector.resolve_pending_end(rig.t, force=True)
    rig.writer.flush()
    return rig.rows("SELECT id FROM jobs")[0]["id"]


def test_status(ctx):
    status, body = get(ctx, "status")
    assert status == 200
    assert body["version"] == "test"
    assert body["collector"]["state"] == "idle"
    assert body["database"]["startup"] == "created"
    assert body["timelapse"]["enabled"] is False
    assert body["accelerometer"]["enabled"] is False


def test_jobs_contract_for_the_chx_ui(ctx, rig):
    job_id = run_job(rig)
    status, body = get(ctx, "jobs")
    assert status == 200 and body["total"] == 1
    entry = body["jobs"][0]
    # useJobHistory.ts HistoryEntry + analysable
    assert entry["id"] == job_id
    assert entry["file"] == "0:/gcodes/a.gcode"
    assert entry["result"] == "finished"
    assert isinstance(entry["printTimeS"], (int, float))
    assert entry["timestamp"].endswith("Z")
    assert entry["analysable"] is True
    assert entry["summary"]["heaterLoadMean"]["1"] == pytest.approx(0.6)
    assert entry["summary"]["filamentRatio"]["0"] == pytest.approx(0.98)


def test_jobs_filter_and_paging(ctx, rig):
    run_job(rig, cancel=True)
    assert get(ctx, "jobs", result="cancelled")[1]["total"] == 1
    assert get(ctx, "jobs", result="finished")[1]["total"] == 0
    assert get(ctx, "jobs", material="PLA")[1]["total"] == 1
    assert get(ctx, "jobs", offset=1)[1]["jobs"] == []


def test_job_detail_and_missing(ctx, rig):
    job_id = run_job(rig)
    status, body = get(ctx, "job", id=job_id)
    assert status == 200
    assert body["context"]["file"]["crc32"] == body["fileCrc32"]
    assert body["summary"]["result"] == "completed"
    assert get(ctx, "job", id="nope")[0] == 404
    assert get(ctx, "job")[0] == 400


def wait_for_index(rig, job_id):
    crc = rig.rows("SELECT file_crc32 FROM jobs WHERE id=?", job_id)[0]["file_crc32"]
    for _ in range(200):
        if rig.index.get(crc)[1] == "ready":
            return
        threading.Event().wait(0.01)
    raise AssertionError("the layer index was not built")


def test_layers_contract_for_the_chx_analysis(ctx, rig):
    job_id = run_job(rig)
    wait_for_index(rig, job_id)
    status, body = get(ctx, "job/layers", id=job_id)
    assert status == 200
    layers = body["layers"]
    assert [layer["layer"] for layer in layers] == [1, 2, 3]
    first = layers[0]
    # §5.10: duration, height, filament per layer, temperatures per sensor with index and name,
    # heater load per nozzle heater, MFM percentage, volumetric flow
    assert first["durationS"] > 0
    assert "height" in first and first["z"] == pytest.approx(0.2)
    assert first["filament"]["0"]["commandedMm"] == pytest.approx(10.0)
    assert first["temps"]["sensors"]["2"]["name"] == "SZP coil"
    assert first["loadStats"]["1"]["mean"] == pytest.approx(0.6)
    assert "shareHigh" in first["loadStats"]["1"]
    assert first["fmStats"]["0"]["mean"] == pytest.approx(99)
    assert first["flow"]["0"] > 0
    # the G-code index per layer: the file has two layers, the job reported three
    assert first["gcode"] == {"extrudeMm": 1.0, "types": {}, "retracts": 0, "fwRetracts": 0, "retractMm": 0.0,
                              "macros": {}}
    assert layers[2]["gcode"] is None
    meta = body["meta"]
    assert meta["gcode"] == "ready"
    assert {"index": 2, "name": "SZP coil", "type": "unknown"} in meta["sensors"]
    assert [h["role"] for h in meta["heaters"]] == ["bed", "nozzle"]
    assert meta["chamber"] == ["sensor", 2]
    assert meta["heaterLoad"] == {"high": 0.8, "limit": 0.9}


def test_layers_without_a_layer_index(ctx, rig, tmp_path):
    job_id = run_job(rig)
    ctx.index_cache = qa_gcode.IndexCache(str(tmp_path / "other"))   # nothing built yet
    rig.gcode.write_text(rig.gcode.read_text() + "G1 X0 Y0\n")
    _, body = get(ctx, "job/layers", id=job_id)
    assert body["meta"]["gcode"] == "changed" and "gcode" not in body["layers"][0]
    rig.gcode.unlink()
    assert get(ctx, "job/layers", id=job_id)[1]["meta"]["gcode"] == "gone"


def test_events_and_type_filter(ctx, rig):
    job_id = run_job(rig)
    _, body = get(ctx, "job/events", id=job_id)
    types = [e["type"] for e in body["events"]]
    assert types[0] == "job_start" and types[-1] == "job_end"
    _, body = get(ctx, "job/events", id=job_id, type="job_end,job_start")
    assert [e["type"] for e in body["events"]] == ["job_start", "job_end"]
    assert body["events"][0]["positions"] == {"X": 0.0, "Y": 0.0, "Z": 0.0}


def test_samples_with_derived_load(ctx, rig):
    job_id = run_job(rig)
    status, body = get(ctx, "job/samples", id=job_id, channels="heater.1.current,heater.1.load", resolution="coarse")
    assert status == 200
    loads = [v for _, v in body["channels"]["heater.1.load"]]
    assert loads and max(loads) == pytest.approx(0.6)
    assert all(isinstance(ts, int) for ts, _ in body["channels"]["heater.1.current"])
    assert get(ctx, "job/samples", id=job_id)[0] == 400
    assert get(ctx, "job/samples", id=job_id, channels="a", resolution="x")[0] == 400


def test_load_follows_a_max_pwm_change(ctx, rig):
    rig.start_job()
    rig.patch({"heat": {"heaters": [{}, {"current": 220, "avgPwm": 0.45}]}}, dt_ms=6000)
    rig.patch({"heat": {"heaters": [{}, {"model": {"maxPwm": 0.9}}]}}, dt_ms=6000)
    rig.patch({"heat": {"heaters": [{}, {"avgPwm": 0.45}]}}, dt_ms=6000)
    rig.writer.flush()
    job_id = rig.rows("SELECT id FROM jobs")[0]["id"]
    _, body = get(ctx, "job/samples", id=job_id, channels="heater.1.load", resolution="coarse")
    values = [v for _, v in body["channels"]["heater.1.load"]]
    assert values[0] == 0.0  # job start, heater off
    assert values[1] == pytest.approx(0.45) and values[-1] == pytest.approx(0.5)


def test_blocks_and_fine_samples(ctx, rig):
    job_id = run_job(rig)
    _, body = get(ctx, "job/blocks", id=job_id)
    assert len(body["blocks"]) >= 1
    _, fine = get(ctx, "job/samples", id=job_id, channels="heater.1.current", resolution="fine")
    assert fine["channels"]["heater.1.current"]


def test_trends(ctx, rig):
    run_job(rig)
    _, body = get(ctx, "trends", metric="heater_load_mean")
    point = body["points"][0]
    assert point["heater"] == 1 and point["setpoint"] == 220 and point["value"] == pytest.approx(0.6)
    assert point["nozzleDiameter"] == 0.8
    _, body = get(ctx, "trends", metric="fm_avg_percentage")
    assert body["points"][0]["value"] == 98
    assert get(ctx, "trends", metric="nonsense")[0] == 400
    assert get(ctx, "trends")[0] == 400


def test_channels_catalog(ctx, rig):
    run_job(rig)
    _, body = get(ctx, "channels")
    assert "heater.1.avgPwm" in body["channels"]
    assert "heater.1.load" in body["derived"]


def test_settings_roundtrip(ctx):
    status, body = get(ctx, "settings")
    assert body["settings"]["sampleIntervalS"] == 5
    response = qa_api.call(ctx, qa_api.ENDPOINTS[("POST", "settings")], Req(body=json.dumps({"sampleIntervalS": 0})))
    assert response.status == 200 and response.body["saved"] is False
    assert response.body["errors"][0].startswith("sampleIntervalS")
    assert response.body["settings"]["sampleIntervalS"] == 5
    response = qa_api.call(ctx, qa_api.ENDPOINTS[("POST", "settings")],
                           Req(body=json.dumps({"timelapse": {"snapshotUrl": "http://10.42.0.1/snapshot"}})))
    assert response.status == 200 and response.body["saved"] is True
    assert ctx.settings.current()["timelapse"]["snapshotUrl"] == "http://10.42.0.1/snapshot"
    response = qa_api.call(ctx, qa_api.ENDPOINTS[("POST", "settings")], Req(body="{nope"))
    assert response.status == 400


def test_toolpath(ctx, rig):
    job_id = run_job(rig)
    crc = rig.rows("SELECT file_crc32 FROM jobs")[0]["file_crc32"]
    rig.index.ensure(str(rig.gcode), crc, run_async=False)
    status, body = get(ctx, "job/toolpath", id=job_id, layer=2)
    assert status == 200
    assert body["meta"]["numLayers"] == 2
    assert body["segments"]["x1"][-1] == 20
    assert get(ctx, "job/toolpath", id=job_id, layer=9)[0] == 404
    # the file changed after the job
    rig.gcode.write_text(";LAYER_CHANGE\nG1 X1 Y1 E1\n")
    assert get(ctx, "job/toolpath", id=job_id, layer=1)[0] == 409
    os.remove(rig.gcode)
    assert get(ctx, "job/toolpath", id=job_id, layer=1)[0] == 409


def test_toolpath_while_the_index_builds(ctx, rig, monkeypatch):
    job_id = run_job(rig)
    fresh = qa_gcode.IndexCache(os.path.join(ctx.data_dir, "other"))
    monkeypatch.setattr(fresh, "ensure", lambda path, crc: "building")
    ctx.index_cache = fresh
    assert get(ctx, "job/toolpath", id=job_id, layer=1)[0] == 202


def test_export_is_a_file(ctx, rig):
    job_id = run_job(rig)
    response = qa_api.call(ctx, qa_api.ENDPOINTS[("GET", "job/export")], Req(id=job_id))
    assert response.kind == "file"
    with open(response.body) as handle:
        data = json.load(handle)
    assert data["job"]["id"] == job_id
    assert len(data["layers"]) == 3 and data["events"]


def test_spectrum_reference(ctx, rig, writer):
    run_job(rig)
    key = rig.rows("SELECT key FROM jobs")[0]["key"]
    writer._con.execute("INSERT INTO spectra (id, job_key, ts_ms, axis) VALUES (5, ?, 1, 'X')", (key,))
    post = qa_api.ENDPOINTS[("POST", "spectra/reference")]
    assert qa_api.call(ctx, post, Req(body=json.dumps({"axis": "X", "spectrumId": 5}))).status == 200
    assert qa_api.call(ctx, post, Req(body=json.dumps({"axis": "X", "spectrumId": 99}))).status == 404
    assert qa_api.call(ctx, post, Req(body=json.dumps({"axis": "Q"}))).status == 400
    _, body = get(ctx, "spectra/reference")
    assert body["references"][0]["mode"] == "manual"


# --- live WebSocket -----------------------------------------------------------------------------

class FakeWriter:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def frames(self):
        decoder = json.JSONDecoder()
        text = self.data.decode()
        out = []
        index = 0
        while index < len(text):
            obj, index = decoder.raw_decode(text, index)
            out.append(json.loads(obj["response"]))
        return out


def ws_message(session_id, body="hello"):
    """A client text frame as DSF forwards it (ReceivedHttpRequest, camelCase JSON)."""
    return json.dumps({"sessionId": session_id, "remoteIPAddress": None, "remotePort": 0, "queries": {},
                       "headers": {}, "contentType": "", "body": body}).encode()


def test_live_websocket(ctx, monkeypatch):
    from dsf.http import HttpEndpointConnection

    monkeypatch.setattr(qa_api, "LIVE_PING_S", 0.1)

    async def scenario():
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        conn = HttpEndpointConnection(reader, writer, True)
        task = asyncio.ensure_future(qa_api.make_live_handler(ctx)(conn))
        await asyncio.sleep(0.05)
        assert ctx.live.count() == 0  # nothing before the client's first frame shows its session
        assert writer.data == b""
        reader.feed_data(ws_message(3))
        await asyncio.sleep(0.05)
        assert ctx.live.count() == 1
        # published from another thread, as the collector does
        threading.Thread(target=ctx.live.publish, args=({"type": "sample", "values": {"a": 1}},)).start()
        await asyncio.sleep(0.25)
        reader.feed_data(b'{"body":"hi"}')  # client text is ignored
        await asyncio.sleep(0.05)
        reader.feed_eof()  # the browser closed the socket
        await asyncio.wait_for(task, 2)
        return writer

    writer = asyncio.run(scenario())
    frames = writer.frames()
    assert frames[0]["type"] == "hello"
    assert {"type": "sample", "values": {"a": 1}} in frames
    assert any(f["type"] == "ping" for f in frames)
    assert writer.closed and ctx.live.count() == 0


@pytest.mark.parametrize("first", [ws_message(-1), None])
def test_live_websocket_without_session_is_closed(ctx, monkeypatch, first):
    """An anonymous client, or one that sends nothing, gets close code 1008 and no frames."""
    from dsf.http import HttpEndpointConnection

    monkeypatch.setattr(qa_api, "LIVE_AUTH_TIMEOUT_S", 0.1)

    async def scenario():
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        conn = HttpEndpointConnection(reader, writer, True)
        task = asyncio.ensure_future(qa_api.make_live_handler(ctx)(conn))
        if first is not None:
            reader.feed_data(first)
        ctx.live.publish({"type": "sample"})
        await asyncio.wait_for(task, 2)
        return writer

    writer = asyncio.run(scenario())
    sent = json.loads(writer.data.decode())
    assert sent["statusCode"] == 1008
    assert writer.closed and ctx.live.count() == 0


def test_every_endpoint_refuses_anonymous_requests(ctx):
    for (method, path), func in qa_api.ENDPOINTS.items():
        response = qa_api.call(ctx, func, Req(session_id=-1, id="x", layer=1, metric="duration_s", channels="a"))
        assert response.status == 401, (method, path)
        assert response.body == {"error": "a DWC session is required"}
    # a request object without the attribute counts as anonymous too
    assert qa_api.call(ctx, qa_api.ENDPOINTS[("GET", "status")], object()).status == 401


def test_live_hub_drops_oldest_when_a_client_lags():
    hub = qa_api.LiveHub()

    async def scenario():
        cid, queue = hub.register(asyncio.get_running_loop())
        for i in range(qa_api.LiveHub.MAX_QUEUE + 10):
            hub.publish({"i": i})
        await asyncio.sleep(0.05)
        first = json.loads(queue.get_nowait())
        hub.unregister(cid)
        return first, queue.qsize()

    first, remaining = asyncio.run(scenario())
    assert first == {"i": 10}
    assert remaining == qa_api.LiveHub.MAX_QUEUE - 1


def test_register_endpoints_includes_the_websocket(ctx):
    from dsf.object_model import HttpEndpointType

    registered = []

    class Cmd:
        def add_http_endpoint(self, endpoint_type, namespace, path):
            registered.append((endpoint_type, namespace, path))

            class Endpoint:
                def set_endpoint_handler(self, handler):
                    pass
            return Endpoint()

    endpoints = qa_api.register_endpoints(Cmd(), ctx)
    assert len(endpoints) == len(qa_api.ENDPOINTS) + 1
    assert (HttpEndpointType.WebSocket, "QualityAssurance", "live") in registered
    assert (HttpEndpointType.POST, "QualityAssurance", "settings") in registered
