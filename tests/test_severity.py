"""Event levels (qa_severity): the rules Tim chose on 2026-10-01 and the catalog they cover."""

import pathlib
import re

import pytest

import qa_settings
import qa_severity
from qa_severity import ERROR, INFO, WARNING

DSF = pathlib.Path(__file__).resolve().parent.parent / "dsf"
EVENT_CALL = re.compile(r"""(?:_event|on_event)\(\s*[^,()]+,\s*[^,()]+(?:\[[^\]]*\])?,\s*"([a-z_]+)"|"""
                        r"""events\.append\(\("([a-z_]+)\"""")


def emitted_types():
    found = set()
    for path in DSF.glob("qa_*.py"):
        for match in EVENT_CALL.finditer(path.read_text(encoding="utf-8")):
            found.add(match.group(1) or match.group(2))
    return found


def test_every_event_type_has_a_rule():
    """A new event type without a rule would count as a warning; the catalog of PLAN.md §5.4 has 28."""
    types = emitted_types()
    assert len(types) >= 28
    assert types == set(qa_severity.RULES)


def level(type_, subtype=None, payload=None, ranges=None, ended_ms=None, ts_ms=0):
    return qa_severity.level({"type": type_, "subtype": subtype, "ts_ms": ts_ms, "payload": payload}, ranges, ended_ms)


@pytest.mark.parametrize("type_, subtype, expected", [
    ("job_start", None, INFO),
    ("job_end", "completed", INFO),
    ("job_end", "aborted", INFO),          # the result is its own criterion
    ("pause", "filament_monitor", INFO),   # the cause counts, not the pause
    ("mfm_error_tolerated", None, INFO),   # the filament_status it tolerated counts
    ("mfm_recovery", "requested", INFO),
    ("mfm_recovery", "false_positive", INFO),
    ("mfm_recovery", "real_issue", ERROR),
    ("gear_passes_forecast", "high", INFO),
    ("gear_passes", "high", WARNING),
    ("firmware_restart", "before_job", INFO),
    ("firmware_restart", "during_job", ERROR),
    ("heater_load", "high", WARNING),
    ("heater_load", "limit", ERROR),
    ("heater_monitor", "tooHigh", ERROR),
    ("heater_fault", None, ERROR),
    ("driver_error", "error", ERROR),
    ("driver_error", "stall", WARNING),
    ("filament_status", "tooLittleMovement", WARNING),
    ("machine_mode", "automatic", INFO),
    ("machine_mode", "default", ERROR),
    ("timelapse_failed", "snapshot", INFO),
    ("calibration", "mesh", INFO),
    ("something_new", None, WARNING),
])
def test_rules(type_, subtype, expected):
    assert level(type_, subtype) == expected


def test_an_open_load_that_cleared_within_500_ms_is_info():
    assert level("driver_error", "warning", {"confirmed": False}) == INFO
    assert level("driver_error", "warning", {"confirmed": True}) == WARNING
    assert level("driver_error", "warning", {"source": "message"}) == WARNING


def test_the_default_mode_of_the_end_sequence_is_info():
    """stop.g's default mode came 1.3 s before job_end (20260929-125727) and 5 ms after it (20260930-075116)"""
    ended = 10_000_000
    assert level("machine_mode", "default", ts_ms=ended - 1288, ended_ms=ended) == INFO
    assert level("machine_mode", "default", ts_ms=ended + 5, ended_ms=ended) == INFO
    assert level("machine_mode", "default", ts_ms=ended - 600_000, ended_ms=ended) == ERROR   # doors opened
    assert level("machine_mode", "default", ts_ms=ended - 1288) == ERROR                       # job still runs


def test_a_setpoint_counts_outside_its_expected_range():
    ranges = qa_settings.DEFAULTS["expectedRanges"]
    pa = {"setpoint": "pressAdv.k0", "index": 0, "from": 0.035, "to": 0.11}
    assert level("setpoint_change", "pressAdv.k0", pa, ranges) == INFO
    assert level("setpoint_change", "pressAdv.k0", {**pa, "to": 0.4}, ranges) == WARNING
    assert level("setpoint_change", "pressAdv.k0", {**pa, "to": 0.4}, {}) == INFO               # no range
    assert level("setpoint_change", "heater.active", {"to": 0}, ranges) == INFO
    assert level("babystep", None, {"axis": "Z", "from": 0, "to": 0.3}, {"babystep": [-0.1, 0.1]}) == WARNING
    assert level("setpoint_change", "pressAdv.k0", {**pa, "to": -0.01}, {"pressAdv.k0": [0, None]}) == WARNING
    # the M92 of a flow-bias correction follows from mfm_flow_bias, which counts
    steps = {"setpoint": "stepsPerMm", "from": 690, "to": 720, "cause": "mfm_flow_bias"}
    assert level("setpoint_change", "stepsPerMm", steps, {"stepsPerMm": [680, 700]}) == INFO
    # objects (M207, M593) have no range
    assert level("setpoint_change", "tool.retraction", {"to": {"length": 9}}, {"tool.retraction": [0, 1]}) == INFO


def test_counts_and_counted():
    events = [{"type": "job_start"}, {"type": "heater_load", "subtype": "high"},
              {"type": "heater_load", "subtype": "limit"}, {"type": "job_end", "subtype": "completed"}]
    counts = qa_severity.counts(events)
    assert counts == {"info": 2, "warning": 1, "error": 1}
    assert qa_severity.counted(counts) == 2


@pytest.mark.parametrize("ranges, problem", [
    ({"pressAdv.k0": [0, 0.3], "babystep": [None, 0.1]}, None),
    ({}, None),
    ({"pressAdv.k0": [0.3, 0]}, "expectedRanges: pressAdv.k0: low must be <= high"),
    ({"pressAdv.k0": [0]}, "expectedRanges: pressAdv.k0: must be [low, high], numbers or null"),
    ({"pressAdv.k0": ["a", 1]}, "expectedRanges: pressAdv.k0: must be [low, high], numbers or null"),
    ({"pressAdv.k0": [True, 1]}, "expectedRanges: pressAdv.k0: must be [low, high], numbers or null"),
])
def test_expected_ranges_setting(ranges, problem):
    settings, errors = qa_settings.validate({"expectedRanges": ranges})
    if problem is None:
        assert errors == [] and settings["expectedRanges"] == ranges
    else:
        assert errors == [problem] and settings["expectedRanges"] == qa_settings.DEFAULTS["expectedRanges"]
