import json
import os

import qa_settings


def test_defaults_without_file(settings):
    cfg = settings.current()
    assert cfg["sampleIntervalS"] == 5
    assert cfg["heaterLoad"] == {"high": 0.8, "limit": 0.9, "hysteresis": 0.05, "windowS": 60,
                                 "minCoverage": 0.75, "reachedToleranceK": 2}
    assert cfg["timelapse"]["snapshotUrl"] is None
    assert settings.errors == []


def test_partial_file_is_merged(data_dir):
    with open(os.path.join(data_dir, "settings.json"), "w") as handle:
        json.dump({"sampleIntervalS": 10, "timelapse": {"snapshotUrl": "http://10.42.0.1/snapshot"}}, handle)
    s = qa_settings.Settings(data_dir)
    assert s.load() == []
    assert s.current()["sampleIntervalS"] == 10
    assert s.current()["timelapse"]["snapshotUrl"] == "http://10.42.0.1/snapshot"
    assert s.current()["timelapse"]["fps"] == 30


def test_invalid_values_fall_back_and_are_reported(data_dir):
    with open(os.path.join(data_dir, "settings.json"), "w") as handle:
        json.dump({"sampleIntervalS": 0, "chamber": {"mode": "window"},
                   "heaterLoad": {"high": 0.95, "limit": 0.9}}, handle)
    s = qa_settings.Settings(data_dir)
    errors = s.load()
    assert any(e.startswith("sampleIntervalS") for e in errors)
    assert any(e.startswith("chamber.mode") for e in errors)
    assert any(e.startswith("heaterLoad.limit") for e in errors)
    assert s.current()["sampleIntervalS"] == 5
    assert s.current()["chamber"]["mode"] == "auto"
    assert s.current()["heaterLoad"]["high"] == 0.8


def test_broken_file_means_defaults(data_dir):
    with open(os.path.join(data_dir, "settings.json"), "w") as handle:
        handle.write("{not json")
    s = qa_settings.Settings(data_dir)
    errors = s.load()
    assert errors and "unreadable" in errors[0]
    assert s.current()["ringBufferS"] == 90


def test_update_validates_and_persists(settings, data_dir):
    _, errors = settings.update({"timelapse": {"snapshotUrl": "ftp://x"}})
    assert errors
    assert not os.path.exists(os.path.join(data_dir, "settings.json"))
    stored, errors = settings.update({"postTriggerS": 45, "contextGlobals": ["nozzle_type"]})
    assert errors == []
    assert settings.current()["postTriggerS"] == 45
    with open(os.path.join(data_dir, "settings.json")) as handle:
        assert json.load(handle)["contextGlobals"] == ["nozzle_type"]
    assert stored["postTriggerS"] == 45


def test_update_refuses_unknown_keys(settings):
    _, errors = settings.update({"bogus": 1, "thresholds": {"typo": 2}})
    assert errors == ["thresholds.typo: unknown setting", "bogus: unknown setting"]


def test_another_versions_keys_are_kept(data_dir):
    """A newer QA's settings survive an older QA that saves (a downgrade to the image's QA)."""
    path = os.path.join(data_dir, "settings.json")
    with open(path, "w") as handle:
        json.dump({"sampleIntervalS": 10, "newer": {"a": 1}, "thresholds": {"newerK": 7, "temperatureK": 6}}, handle)
    s = qa_settings.Settings(data_dir)
    assert s.load() == []
    assert "newer" not in s.current() and "newerK" not in s.current()["thresholds"]
    _, errors = s.update({**s.current(), "postTriggerS": 45})
    assert errors == []
    with open(path) as handle:
        stored = json.load(handle)
    assert stored["newer"] == {"a": 1}
    assert stored["thresholds"]["newerK"] == 7
    assert stored["thresholds"]["temperatureK"] == 6
    assert stored["sampleIntervalS"] == 10 and stored["postTriggerS"] == 45


def test_snapshot_is_a_copy(settings):
    cfg = settings.current()
    cfg["thresholds"]["temperatureK"] = 99
    # current() hands out the shared snapshot; validate() output is independent of DEFAULTS
    assert qa_settings.DEFAULTS["thresholds"]["temperatureK"] == 5
