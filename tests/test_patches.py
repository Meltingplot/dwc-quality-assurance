"""The dsf-python patches, run against the real library (dsf-python 3.7.0b1, as on the image)."""

import json

import pytest

import qa_patches

qa_patches.apply()

from dsf.object_model import ObjectModel  # noqa: E402


def model_from(data):
    om = ObjectModel()
    om.update_from_json(json.loads(json.dumps(data)))
    return om


def test_all_patches_apply():
    assert set(qa_patches.apply()) == {name for name, _ in qa_patches.PATCHES}


def test_rotating_magnet_keeps_agc_and_mm_per_rev():
    om = model_from({"sensors": {"filamentMonitors": [{
        "type": "rotatingMagnet", "status": "ok", "agc": 117, "avgPercentage": 97,
        "calibrated": {"mmPerRev": 27.8, "percentMin": 90, "percentMax": 105, "totalDistance": 800.2},
        "configured": {"mmPerRev": 28.8, "percentMin": 70, "percentMax": 150, "sampleDistance": 3},
    }]}})
    fm = om.sensors.filament_monitors[0]
    assert fm.agc == 117
    assert fm.calibrated.mm_per_rev == pytest.approx(27.8)
    assert fm.calibrated.total_distance == pytest.approx(800.2)
    assert fm.configured.mm_per_rev == pytest.approx(28.8)
    assert fm.avg_percentage == 97


def test_patch_updates_agc_and_clears_calibration():
    om = model_from({"sensors": {"filamentMonitors": [{
        "type": "rotatingMagnet", "agc": 100, "calibrated": {"mmPerRev": 27.0}}]}})
    om.update_from_json({"sensors": {"filamentMonitors": [{"agc": 120, "calibrated": None}]}})
    fm = om.sensors.filament_monitors[0]
    assert fm.agc == 120
    assert fm.calibrated is None


def test_sensor_accelerometers():
    """As on the CHX 350 (2026-09-27): the SZP's accelerometer at CAN address 60."""
    om = model_from({"sensors": {"accelerometers": [{"orientation": 25, "points": 0, "port": "60.i2c.lis",
                                                     "resolution": 14, "runs": 0, "samplingRate": 800}]}})
    acc = om.sensors.accelerometers[0]
    assert (acc.orientation, acc.points, acc.port, acc.resolution, acc.runs, acc.sampling_rate) == \
        (25, 0, "60.i2c.lis", 14, 0, 800)
    om.update_from_json({"sensors": {"accelerometers": [{"runs": 1, "points": 1000, "samplingRate": 798}]}})
    acc = om.sensors.accelerometers[0]
    assert (acc.runs, acc.points, acc.sampling_rate, acc.port) == (1, 1000, 798, "60.i2c.lis")
    om.update_from_json({"sensors": {"accelerometers": [None, {"port": "121.i2c.lis"}]}})
    assert om.sensors.accelerometers[0] is None
    assert om.sensors.accelerometers[1].port == "121.i2c.lis"
    assert len(model_from({}).sensors.accelerometers) == 0  # not shared between models


def test_pressure_advance_k0_k1_survive():
    om = model_from({"move": {"extruders": [{"pressAdv": {"k0": 0.05, "k1": 0.01, "d": 0.02}}]}})
    pa = om.move.extruders[0].press_adv
    assert (pa.k0, pa.k1, pa.d) == pytest.approx((0.05, 0.01, 0.02))


def test_build_object_cancelled():
    om = model_from({"job": {"build": {"currentObject": 1, "m486Names": True,
                                       "objects": [{"name": "a", "cancelled": True}]}}})
    assert om.job.build.objects[0].canceled is True
    assert om.job.build.m486_names is True


def test_null_custom_info_does_not_drop_the_patch():
    om = model_from({"job": {"file": {"fileName": "0:/gcodes/a.gcode", "customInfo": {"x": "1"}}}})
    om.update_from_json({"job": {"file": {"customInfo": None, "numLayers": 5}}})
    assert om.job.file.num_layers == 5
    assert dict(om.job.file.custom_info) == {}


def test_unknown_enum_value_is_kept():
    om = model_from({"sensors": {"endstops": [{"type": "somethingNew", "triggered": False}],
                                 "analog": [{"name": "x", "lastReading": 20}]},
                     "boards": [{"state": "timedOut"}]})
    assert om.sensors.endstops[0].type == "somethingNew"
    assert om.sensors.analog[0].last_reading == 20
    assert om.boards[0].state == "timedOut"


def test_motor_stall_encoder_is_a_real_member():
    from dsf.object_model.sensors.endstop_type import EndstopType

    assert EndstopType("motorStallEncoder").name == "MotorStallEncoder"


def test_axis_letter_null_byte():
    om = model_from({"move": {"axes": [{"letter": "\x00", "machinePosition": 1.0}]}})
    assert om.move.axes[0].machine_position == 1.0


@pytest.mark.parametrize("text,end", [
    ('{"a":1}', 7),
    ('{"a":"}"}rest', 9),
    ('{"a":"\\"}"}x', 11),
    ('{"a":{', -1),
    ('', -1),
])
def test_json_object_end(text, end):
    assert qa_patches.json_object_end(text) == end


def test_read_json_object_keeps_leftover():
    chunks = [b'{"id":"abc', b'def","version":12}{"next"', b""]
    json_string, leftover = qa_patches.read_json_object(lambda _n: chunks.pop(0))
    assert json.loads(json_string) == {"id": "abcdef", "version": 12}
    assert leftover == '{"next"'


def test_read_json_object_closed_connection():
    with pytest.raises(ConnectionError):
        qa_patches.read_json_object(lambda _n: b"")


def _connection(timeout=1):
    import socket

    from dsf.connections.base_connection import BaseConnection
    ours, theirs = socket.socketpair()
    conn = BaseConnection(timeout=timeout)
    conn.socket = ours
    ours.settimeout(timeout)
    return conn, theirs


def test_receive_json_braces_in_strings_and_split_characters():
    conn, dsf = _connection()
    message = '{"content":"Error: expected \'}\' in G1 {E","temp":"25 °C"}{"next":1}'.encode("utf-8")
    cut = message.index("°".encode("utf-8")) + 1          # inside the two bytes of °
    dsf.sendall(message[:cut])
    dsf.sendall(message[cut:])
    assert json.loads(conn.receive_json()) == {"content": "Error: expected '}' in G1 {E", "temp": "25 °C"}
    assert json.loads(conn.receive_json()) == {"next": 1}   # from the leftover, no further recv
    assert conn.has_data_available() is False


def test_receive_json_end_of_stream_and_timeout():
    import time

    conn, dsf = _connection(timeout=0.3)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        conn.receive_json()
    assert time.monotonic() - start < 1.5
    dsf.sendall(b'{"a":')
    dsf.close()
    with pytest.raises(ConnectionError):   # not an endless loop on b""
        conn.receive_json()


def test_endpoint_close_ends_its_thread(tmp_path):
    """With a client connected, the unpatched close() left the loop in epoll and the process hung at exit"""
    import socket
    import time

    from dsf.http import HttpEndpointUnixSocket
    from dsf.object_model import HttpEndpointType
    endpoints, clients = [], []
    for n in range(3):
        endpoints.append(HttpEndpointUnixSocket(HttpEndpointType.GET, "qa", f"t{n}", str(tmp_path / f"ep{n}.sock")))
    time.sleep(0.3)
    for n in range(3):
        client = socket.socket(socket.AF_UNIX)
        client.connect(str(tmp_path / f"ep{n}.sock"))
        clients.append(client)
    time.sleep(0.2)
    for endpoint in endpoints:
        endpoint.close()
    threads = [t for endpoint in endpoints for t in endpoint.executor._threads]
    deadline = time.monotonic() + 3
    while any(t.is_alive() for t in threads) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(t.is_alive() for t in threads)
    for client in clients:
        client.close()
