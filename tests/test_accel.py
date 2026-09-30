"""Accelerometer: CSV as RRF 3.7 writes it, the spectrum against a direct DFT and against DWC's own
@duet3d/motionanalysis (when the DuetWebControl checkout is next to this repo), the recorder with the
real collector and a stand-in for RRF (M956 writes the CSV, ``runs`` advances with a patch), the API."""

import cmath
import json
import math
import os
import shutil
import subprocess
import threading
import time

import pytest

import qa_accel
import qa_api
import qa_db
from conftest import make_model

RATE = 800
SZP = {"orientation": 25, "points": 0, "port": "60.i2c.lis", "resolution": 14, "runs": 0, "samplingRate": RATE}


def csv_text(n=200, freq=48.0, amplitude=0.2, overflows=0, axes="XYZ", rate=RATE):
    """A file as RRF writes it (Accelerometers.cpp, RepRapFirmware 3.7-dev @ 3638836)."""
    lines = ["Sample," + ",".join(axes)]
    for i in range(n):
        values = {"X": amplitude * math.sin(2 * math.pi * freq * i / rate),
                  "Y": 0.05 * math.sin(2 * math.pi * 120 * i / rate), "Z": 1.0 + 0.01 * math.sin(2 * math.pi * 30 * i / rate)}
        lines.append(f"{i}," + ",".join(f"{values[a]:.4f}" for a in axes))
    lines.append(f"Rate {rate}, overflows {overflows}")
    return "\n".join(lines) + "\n"


# --- parsing and maths --------------------------------------------------------------------------

def test_parse_csv():
    parsed = qa_accel.parse_csv(csv_text(n=5))
    assert parsed["axes"] == ["X", "Y", "Z"] and parsed["rate"] == 800 and parsed["overflows"] == 0
    assert len(parsed["samples"]["X"]) == 5 and parsed["samples"]["Z"][0] == 1.0
    assert qa_accel.parse_csv("Sample,X\n0,0.1\n1,0.2\nRate 798 overflows 2\n")["overflows"] == 2  # comma optional
    with pytest.raises(qa_accel.CsvIncomplete):
        qa_accel.parse_csv("Sample,X,Y,Z\n0,0.1,0.2,1.0\n1,0.1,0.2,1.0\n")  # DSF not done yet
    with pytest.raises(qa_accel.CsvIncomplete):
        qa_accel.parse_csv("")
    with pytest.raises(qa_accel.CsvError, match="Received bad data"):
        qa_accel.parse_csv("Sample,X,Y,Z\n0,0.1,0.2,1.0\nReceived bad data\n")
    with pytest.raises(qa_accel.CsvError, match="line 3"):
        qa_accel.parse_csv("Sample,X,Y\n0,0.1,0.2\n1,0.1\nRate 800, overflows 0\n")
    with pytest.raises(qa_accel.CsvError, match="no accelerometer CSV"):
        qa_accel.parse_csv("<html>\n")


@pytest.mark.parametrize("n", [1, 2, 8, 12, 17, 64, 100])
def test_dft_matches_the_definition(n):
    values = [math.sin(0.7 * i) + 0.3 * math.cos(2.1 * i) + (i % 3) for i in range(n)]
    expected = [sum(v * cmath.exp(-2j * math.pi * k * i / n) for i, v in enumerate(values)) for k in range(n)]
    got = qa_accel.dft(values)
    assert max(abs(a - b) for a, b in zip(got, expected)) < 1e-9


def test_spectrum_of_a_sine_on_a_bin():
    samples = [0.2 * math.sin(2 * math.pi * 48 * i / RATE) for i in range(1000)]
    freqs, amplitudes = qa_accel.spectrum(samples, RATE)
    assert len(freqs) == 500 and freqs[0] == pytest.approx(0.8) and freqs[-1] == pytest.approx(400)
    peak = max(range(len(amplitudes)), key=amplitudes.__getitem__)
    assert freqs[peak] == pytest.approx(48)
    assert amplitudes[peak] == pytest.approx(0.2, rel=1e-6)  # Hann + 4/N: exact for a sine on a bin


def test_analyse_peak_and_rms():
    rows = qa_accel.analyse(qa_accel.parse_csv(csv_text(n=1000)))
    by_axis = {r["axis"]: r for r in rows}
    assert by_axis["X"]["peak_hz"] == pytest.approx(48, abs=0.8)
    assert by_axis["Y"]["peak_hz"] == pytest.approx(120, abs=0.8)
    assert by_axis["X"]["rms"] == pytest.approx(0.2 / math.sqrt(2), rel=0.01)
    assert by_axis["Z"]["rms"] == pytest.approx(0.01 / math.sqrt(2), rel=0.05)  # 1 g of gravity is the mean
    assert by_axis["X"]["n_samples"] == 1000 and by_axis["X"]["sampling_rate"] == 800


MOTIONANALYSIS = os.path.join(os.path.dirname(__file__), "..", "..", "DuetWebControl", "node_modules", "@duet3d",
                              "motionanalysis")


@pytest.mark.skipif(not (shutil.which("node") and os.path.isdir(MOTIONANALYSIS)),
                    reason="needs node and the DuetWebControl checkout next to this repo")
@pytest.mark.parametrize("n", [1000, 1024, 777])
def test_spectrum_equals_dwc_motionanalysis(n):
    """QA's spectra are the ones DWC's input-shaping plugin shows (@duet3d/motionanalysis)."""
    script = (f"const ma = require({json.dumps(os.path.abspath(MOTIONANALYSIS))});"
              f"const x = Array.from({{length: {n}}}, (_, i) => 0.2 * Math.sin(2 * Math.PI * 47.3 * i / 800)"
              f" + 0.05 * Math.sin(2 * Math.PI * 131 * i / 800 + 1) + 0.01 * ((i * 7919) % 13 - 6));"
              "const r = ma.analyzeAccelerometerData([x], 800, true, true);"
              "console.log(JSON.stringify({f: r.frequencies, a: r.amplitudes[0]}));")
    expected = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    x = [0.2 * math.sin(2 * math.pi * 47.3 * i / 800) + 0.05 * math.sin(2 * math.pi * 131 * i / 800 + 1)
         + 0.01 * ((i * 7919) % 13 - 6) for i in range(n)]
    freqs, amplitudes = qa_accel.spectrum(x, 800)
    assert freqs == pytest.approx(expected["f"], abs=1e-9)
    assert amplitudes == pytest.approx(expected["a"], abs=1e-12)


def test_board_and_choice():
    assert qa_accel.board_of("60.i2c.lis") == 60
    assert qa_accel.board_of("spi.cs1+spi.cs0") == 0
    accels = [None, {"board": 60}, {"board": 121}]
    assert qa_accel.pick(accels, None) is None  # nothing selected: no recordings
    assert qa_accel.pick(accels, 60) == (1, {"board": 60})
    assert qa_accel.pick(accels, 121) == (2, {"board": 121})
    assert qa_accel.pick(accels, 5) is None
    assert qa_accel.pick([], None) is None


def test_median_spectrum_on_the_first_grid():
    a = {"freqs": [1.0, 2.0, 3.0], "amplitudes": [1.0, 2.0, 3.0]}
    b = {"freqs": [1.1, 2.1, 3.1], "amplitudes": [1.0, 4.0, 3.0]}   # another measured rate
    c = {"freqs": [1.0, 2.0, 3.0], "amplitudes": [5.0, 0.0, 3.0]}
    freqs, amplitudes = qa_accel.median_spectrum([a, b, c])
    assert freqs == [1.0, 2.0, 3.0]
    assert amplitudes == pytest.approx([1.0, 2.0, 3.0])


# --- fans ---------------------------------------------------------------------------------------

# the CHX 350's part-cooling blowers at full PWM (fans[0], fans[1]) and its water pump, 2026-09-30
FANS = [{"actualValue": 1.0, "requestedValue": 1.0, "rpm": 9210, "name": "part cooling"},
        {"actualValue": 1.0, "requestedValue": 1.0, "rpm": 9234, "name": "part cooling #2"},
        {"actualValue": 0.0, "requestedValue": 0.0, "rpm": -1, "name": "aux"},
        {"actualValue": 0.0, "requestedValue": 0.0, "rpm": 0, "name": "radiator"},
        {"actualValue": 0.5, "requestedValue": 0.5, "rpm": 0, "name": "stuck"}]


def test_fans_from_the_model():
    """Fans with a tachometer only (rpm −1 = none), read from the real dsf-python model."""
    fans = qa_accel.fans_from_model(make_model({"fans": FANS}))
    assert [f["fan"] for f in fans] == [0, 1, 3, 4]
    assert fans[0] == {"fan": 0, "name": "part cooling", "pwm": 1.0, "rpm": 9210}
    assert qa_accel.fans_from_model(make_model()) == []   # the test model's fan has no tachometer
    assert qa_accel.fans_from_model(make_model({"fans": [{"actualValue": -1, "rpm": 500}]}))[0]["pwm"] is None


def test_fan_lines_and_their_amplitude():
    """The line at rpm / 60 and each axis' amplitude there; a driven fan at 0 rpm stands; an undriven one
    at 0 rpm is left out; lines closer than two half widths share one peak."""
    model = make_model({"fans": FANS})
    observations = [qa_accel.fans_from_model(model)]
    model.update_from_json({"fans": [{"rpm": 9222}, {"rpm": 9246}, {}, {}, {}]})
    observations.append(qa_accel.fans_from_model(model))
    lines = qa_accel.fan_lines(observations)
    assert lines == [
        {"fan": 0, "name": "part cooling", "pwm": 1.0, "rpm": 9216, "hz": 153.6},
        {"fan": 1, "name": "part cooling #2", "pwm": 1.0, "rpm": 9240, "hz": 154.0},
        {"fan": 4, "name": "stuck", "pwm": 0.5, "rpm": 0, "hz": None}]
    assert qa_accel.fan_lines([[], []]) is None   # no fan with a tachometer: nothing known

    x = [0.2 * math.sin(2 * math.pi * 153.6 * i / RATE) for i in range(1000)]
    freqs, amplitudes = qa_accel.spectrum(x, RATE)
    far = {"fan": 5, "name": None, "pwm": 1.0, "rpm": 30000, "hz": 500.0}   # above Nyquist (400 Hz)
    marked = qa_accel.fan_amplitudes(lines + [far], freqs, amplitudes)
    assert marked[0]["amplitude"] == pytest.approx(0.2, rel=0.03) and marked[0]["sharedWith"] == [1]
    assert marked[1]["amplitude"] == marked[0]["amplitude"] and marked[1]["sharedWith"] == [0]
    assert marked[2]["amplitude"] is None and marked[2]["sharedWith"] == []
    assert marked[3]["amplitude"] is None
    alone = qa_accel.fan_amplitudes([{**lines[0], "hz": 120.0}], freqs, amplitudes)[0]
    assert alone["amplitude"] < 0.001 and alone["sharedWith"] == []   # no tone there
    assert qa_accel.fan_amplitudes(None, freqs, amplitudes) is None


# --- recorder with the collector ----------------------------------------------------------------

class FakeRrf:
    """M956 on the SBC channel: writes the CSV like RRF; the test advances ``runs`` with a patch."""

    def __init__(self, directory):
        self.directory = directory
        self.codes = []
        self.reply = ""
        self.text = csv_text()
        self.write = True
        self.sent = threading.Event()

    def send_code(self, code):
        self.codes.append(code)
        if self.write and not self.reply:
            name = code.split('F"', 1)[1].rstrip('"')
            with open(os.path.join(self.directory, name), "w") as handle:
                handle.write(self.text)
        self.sent.set()
        return self.reply

    def resolve(self, virtual):
        assert virtual.startswith("0:/sys/accelerometer/")
        return os.path.join(self.directory, virtual.rsplit("/", 1)[1])


def wait_until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.fixture
def accel(rig, settings, writer, tmp_path):
    directory = tmp_path / "accelerometer"
    directory.mkdir()
    rrf = FakeRrf(str(directory))
    settings.update({"accelerometer": {"board": 60, "intervalMin": 1, "samples": 200}})
    recorder = qa_accel.Recorder(writer, settings, rrf.send_code, rrf.resolve, on_event=rig.collector.external_event,
                                 sleep=lambda _s: time.sleep(0.01))
    recorder.rrf = rrf
    rig.collector.accel = recorder
    recorder.start()
    rig.patch({"sensors": {"accelerometers": [dict(SZP)]}})
    yield recorder
    recorder.stop()


def spectra(rig):
    return rig.rows("SELECT * FROM spectra ORDER BY id")


def finish_run(rig, runs, points=200):
    rig.patch({"sensors": {"accelerometers": [{"runs": runs, "points": points}]}}, dt_ms=10)


def test_spectrum_every_interval_from_layer_two(rig, accel):
    rig.start_job()
    rig.patch({"job": {"layer": 1, "duration": 5}})
    assert not accel.rrf.sent.wait(0.2)  # the first layer is not the steady state
    rig.patch({"job": {"layer": 2, "duration": 30}})
    assert accel.rrf.sent.wait(2)
    assert accel.rrf.codes[0].startswith('M956 P0 S200 A0 F"qa-')
    name = accel.rrf.codes[0].split('F"', 1)[1].rstrip('"')
    assert len(name) <= 50  # RRF reads F into a String<StringLength50>
    assert accel.busy()
    finish_run(rig, 1)
    assert wait_until(lambda: len(spectra(rig)) == 3)
    rows = spectra(rig)
    assert [r["axis"] for r in rows] == ["X", "Y", "Z"]
    assert rows[0]["layer"] == 2 and rows[0]["board"] == 60 and rows[0]["source"] == "60.i2c.lis"
    assert rows[0]["peak_hz"] == pytest.approx(48, abs=4) and rows[0]["n_samples"] == 200
    assert not os.path.exists(os.path.join(accel.rrf.directory, name))  # the CSV goes
    assert wait_until(lambda: not accel.busy())

    accel.rrf.sent.clear()
    rig.patch({"job": {"duration": 40}}, dt_ms=30_000)
    assert not accel.rrf.sent.wait(0.2)  # intervalMin not over
    rig.patch({"state": {"status": "paused"}}, dt_ms=40_000)
    assert not accel.rrf.sent.wait(0.2)  # not while paused
    rig.patch({"state": {"status": "processing"}}, dt_ms=1000)
    assert accel.rrf.sent.wait(2)
    finish_run(rig, 2)
    assert wait_until(lambda: len(spectra(rig)) == 6)

    rig.patch({"state": {"status": "idle"}, "job": {"duration": None, "layer": None}})
    rig.collector.resolve_pending_end(rig.t, force=True)
    summary = qa_db.loads(rig.rows("SELECT summary FROM jobs")[0]["summary"])
    assert summary["mechanics"]["X"]["spectra"] == 2
    assert summary["mechanics"]["X"]["peakHzMean"] == pytest.approx(48, abs=4)
    assert set(summary["mechanics"]) == {"X", "Y", "Z"}
    assert rig.events("accelerometer_failed") == []


def test_each_recording_keeps_the_fans(rig, accel, settings, writer, data_dir):
    """The fans that turn or are driven while M956 runs, with their line in each axis; the job summary and the
    trends follow them over the jobs (a fan that slows down or runs rougher shows there)."""
    ctx = qa_api.ApiContext(version="test", settings=settings, writer=writer, readers=qa_db.Readers(data_dir),
                            data_dir=data_dir, collector=rig.collector, accel=accel)
    settings.update({"accelerometer": {"board": 60, "intervalMin": 1, "samples": 1000}})
    accel.rrf.text = csv_text(n=1000, freq=153.5)   # X carries the blowers' line
    rig.patch({"fans": FANS})
    rig.start_job()
    rig.patch({"job": {"layer": 2, "duration": 30}})
    assert accel.rrf.sent.wait(2)
    rig.patch({"fans": [{"rpm": 9222}, {"rpm": 9246}, {}, {}, {}]}, dt_ms=250)   # a model update while it runs
    finish_run(rig, 1)
    assert wait_until(lambda: len(spectra(rig)) == 3)
    fans = {r["axis"]: qa_db.loads(r["fans"]) for r in spectra(rig)}
    x = fans["X"]
    assert [f["fan"] for f in x] == [0, 1, 4]   # the aux fan has no tachometer, the radiator stands undriven
    assert x[0]["name"] == "part cooling" and x[0]["pwm"] == 1.0 and x[0]["rpm"] == pytest.approx(9217, abs=2)
    assert x[0]["hz"] == pytest.approx(153.6, abs=0.05) and x[0]["sharedWith"] == [1]
    assert x[0]["amplitude"] == pytest.approx(0.2, rel=0.05)
    assert fans["Y"][0]["amplitude"] < 0.01    # Y's tone is at 120 Hz
    assert x[2] == {"fan": 4, "name": "stuck", "pwm": 0.5, "rpm": 0, "hz": None, "amplitude": None, "sharedWith": []}
    assert wait_until(lambda: not accel.busy())

    rig.patch({"state": {"status": "idle"}, "job": {"duration": None, "layer": None}})
    rig.collector.resolve_pending_end(rig.t, force=True)
    summary = qa_db.loads(rig.rows("SELECT summary FROM jobs")[0]["summary"])["fans"]
    assert set(summary) == {"0", "1", "4"}
    assert summary["0"]["recordings"] == 1 and summary["0"]["stalled"] == 0 and summary["0"]["pwmMean"] == 1.0
    assert summary["0"]["amplitudeMean"] == pytest.approx(0.2, rel=0.05)   # root sum square over X, Y, Z
    assert summary["4"] == {"name": "stuck", "recordings": 1, "pwmMean": 0.5, "rpmMean": 0, "stalled": 1,
                            "amplitudeMean": None, "amplitudeMax": None}

    def trend(metric):
        response = qa_api.call(ctx, qa_api.ENDPOINTS[("GET", "trends")], Req(metric=metric))
        assert response.status == 200
        return {p["fan"]: p for p in response.body["points"]}

    rpm = trend("fan_rpm")
    assert set(rpm) == {0, 1, 4} and rpm[0]["value"] == pytest.approx(9217, abs=2) and rpm[4]["stalled"] == 1
    amplitude = trend("fan_amplitude")
    assert set(amplitude) == {0, 1} and amplitude[0]["name"] == "part cooling"   # a standing fan has no line
    assert amplitude[0]["value"] == pytest.approx(0.2, rel=0.05)


@pytest.mark.parametrize("case, subtype, error", [
    ("reply", "start", "Error: M956: an accelerometer is already collecting data"),
    ("bad", "run", "Received bad data"),
    ("overflow", "overflow", "3 overflows, samples lost"),
])
def test_failed_recordings_are_one_event_each(rig, accel, case, subtype, error):
    if case == "reply":
        accel.rrf.reply = "Error: M956: an accelerometer is already collecting data"
    elif case == "bad":
        accel.rrf.text = "Sample,X,Y,Z\n0,0.1,0.2,1.0\nReceived bad data\n"
    else:
        accel.rrf.text = csv_text(overflows=3)
    rig.start_job()
    rig.patch({"job": {"layer": 2, "duration": 30}})
    assert accel.rrf.sent.wait(2)
    if case != "reply":
        finish_run(rig, 1, points=0 if case == "bad" else 200)
    assert wait_until(lambda: rig.events("accelerometer_failed"))
    event = rig.events("accelerometer_failed")[0]
    assert (event["subtype"], event["payload"]["error"]) == (subtype, error)
    assert spectra(rig) == []
    assert os.listdir(accel.rrf.directory) == []
    assert accel.status()["lastError"].startswith(subtype)


def test_a_run_that_never_finishes_times_out(rig, accel, monkeypatch):
    monkeypatch.setattr(qa_accel, "RUN_MARGIN_S", 0.2)
    rig.start_job()
    rig.patch({"job": {"layer": 2, "duration": 30}})
    assert wait_until(lambda: rig.events("accelerometer_failed"))
    assert rig.events("accelerometer_failed")[0]["subtype"] == "timeout"
    assert wait_until(lambda: not accel.busy())


def test_the_trailer_may_come_late(rig, accel):
    """SBC mode: runs advances before DSF has written the last line (DWC reads again every 250 ms)."""
    full = csv_text()
    accel.rrf.text = full.rsplit("Rate", 1)[0]
    rig.start_job()
    rig.patch({"job": {"layer": 2, "duration": 30}})
    assert accel.rrf.sent.wait(2)
    finish_run(rig, 1)
    name = accel.rrf.codes[0].split('F"', 1)[1].rstrip('"')
    with open(os.path.join(accel.rrf.directory, name), "w") as handle:
        handle.write(full)
    assert wait_until(lambda: len(spectra(rig)) == 3)


def test_only_the_board_in_the_settings_records(rig, accel, settings):
    settings.update({"accelerometer": {"intervalMin": 1, "samples": 200}})  # board not set
    status = accel.status()
    assert (status["enabled"], status["reason"]) == (False, "no accelerometer selected (accelerometer.board)")
    assert status["available"] == [{"index": 0, "port": "60.i2c.lis", "board": 60}]
    rig.start_job()
    rig.patch({"job": {"layer": 2, "duration": 30}})
    assert not accel.rrf.sent.wait(0.2)
    settings.update({"accelerometer": {"intervalMin": 1, "samples": 200, "board": 121}})
    assert accel.status()["reason"] == "no accelerometer on board 121 (M955)"
    rig.patch({"job": {"duration": 31}})
    assert not accel.rrf.sent.wait(0.2)
    settings.update({"accelerometer": {"intervalMin": 1, "samples": 200, "board": 60}})
    status = accel.status()
    assert status["enabled"] and status["accelerometer"] == {"index": 0, "port": "60.i2c.lis", "board": 60,
                                                            "samplingRate": 800, "resolution": 14}
    rig.patch({"job": {"duration": 31}})
    assert accel.rrf.sent.wait(2)
    assert accel.rrf.codes[0].startswith("M956 P0 ")


def test_axes_setting_names_the_axes(rig, accel, settings):
    settings.update({"accelerometer": {"board": 60, "intervalMin": 1, "samples": 200, "axes": "XY"}})
    accel.rrf.text = csv_text(axes="XY")
    rig.start_job()
    rig.patch({"job": {"layer": 2, "duration": 30}})
    assert accel.rrf.sent.wait(2)
    assert " A0 X Y F\"" in accel.rrf.codes[0]
    finish_run(rig, 1)
    assert wait_until(lambda: [r["axis"] for r in spectra(rig)] == ["X", "Y"])
    assert settings.update({"accelerometer": {"axes": "XQ"}})[1] == ["accelerometer.axes: must be letters of XYZ"]


# --- API ----------------------------------------------------------------------------------------

class Req:
    def __init__(self, body="", **queries):
        self.queries = {k: str(v) for k, v in queries.items()}
        self.body = body
        self.session_id = 7


def test_api_references_latest_trends_status(rig, accel, settings, writer, data_dir):
    ctx = qa_api.ApiContext(version="test", settings=settings, writer=writer, readers=qa_db.Readers(data_dir),
                            data_dir=data_dir, collector=rig.collector, accel=accel)

    def get(path, **q):
        response = qa_api.call(ctx, qa_api.ENDPOINTS[("GET", path)], Req(**q))
        return response.status, response.body

    settings.update({"accelerometer": {"board": 60, "intervalMin": 1, "samples": 200, "referenceAutoCount": 2}})
    for job, amplitude in enumerate((0.2, 0.4)):
        accel.rrf.text = csv_text(amplitude=amplitude)
        accel.rrf.sent.clear()
        rig.start_job()
        rig.patch({"job": {"layer": 2, "duration": 30}})
        assert accel.rrf.sent.wait(2)
        finish_run(rig, job + 1)
        assert wait_until(lambda: len(spectra(rig)) == 3 * (job + 1))
        assert wait_until(lambda: not accel.busy())
        rig.patch({"state": {"status": "idle"}, "job": {"duration": None, "layer": None}})
        rig.collector.resolve_pending_end(rig.t, force=True)
        rig.patch({}, dt_ms=60_000)

    status, body = get("spectra/reference")
    x = body["references"][0]
    assert status == 200 and body["autoCount"] == 2
    assert x["mode"] == "auto" and x["complete"] and len(x["spectrumIds"]) == 2
    peak = max(x["amplitudes"])
    assert peak == pytest.approx(0.3, rel=0.05)  # median of the two runs, 0.2 and 0.4 g
    assert x["peakHz"] == pytest.approx(48, abs=4)

    status, latest = get("spectra/latest", axis="X", limit=5)
    assert status == 200 and [max(s["amplitudes"]) for s in latest["spectra"]] == pytest.approx([0.4, 0.2], rel=0.05)
    assert get("spectra/latest", axis="Q")[0] == 400

    post = qa_api.ENDPOINTS[("POST", "spectra/reference")]
    first = latest["spectra"][1]["id"]
    assert qa_api.call(ctx, post, Req(body=json.dumps({"axis": "X", "spectrumId": first}))).status == 200
    x = get("spectra/reference")[1]["references"][0]
    assert x["mode"] == "manual" and x["spectrumIds"] == [first] and max(x["amplitudes"]) == pytest.approx(0.2, rel=0.05)

    status, trend = get("trends", metric="spectrum_peak_hz")
    assert status == 200 and sorted(p["axis"] for p in trend["points"]) == ["X", "X", "Y", "Y", "Z", "Z"]
    rms = [p["value"] for p in get("trends", metric="spectrum_rms")[1]["points"] if p["axis"] == "X"]
    assert rms == pytest.approx([0.4 / math.sqrt(2), 0.2 / math.sqrt(2)], rel=0.05)

    status = get("status")[1]["accelerometer"]
    assert status["enabled"] and status["accelerometer"]["port"] == "60.i2c.lis" and status["lastError"] is None
