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
