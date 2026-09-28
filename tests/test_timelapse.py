"""Timelapse: capture per layer through the real collector, encoding with stand-ins for ffmpeg and
ffprobe (scripts that behave like them on the arguments QA passes), pause/resume of a running
encoder, retention, the API. ``test_real_ffmpeg`` runs the exact commands against a real ffmpeg
with libsvtav1 when one is installed (Debian trixie: apt install ffmpeg)."""

import http.server
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time

import pytest

import qa_api
import qa_db
import qa_timelapse

JPEG = b"\xff\xd8\xff\xe0" + b"frame" * 20 + b"\xff\xd9"

# Behaves like ffmpeg for QA's two commands: encoding (writes "<frames>" into the output, reading
# -frames:v) and frame extraction (-ss, writes a JPEG). FAKE_FFMPEG_SLEEP makes it run a while.
FAKE_FFMPEG = r'''#!{python}
import os, sys, time
args = sys.argv[1:]
out = args[-1]
if "-ss" in args:
    open(out, "wb").write(bytes.fromhex("ffd8ffe0") + b"extracted" + bytes.fromhex("ffd9"))
    sys.exit(0)
time.sleep(float(os.environ.get("FAKE_FFMPEG_SLEEP", "0")))
frames = args[args.index("-frames:v") + 1]
open(out, "w").write(frames)
sys.exit(int(os.environ.get("FAKE_FFMPEG_EXIT", "0")))
'''

FAKE_FFPROBE = r'''#!{python}
import json, os, sys
count = int(open(sys.argv[-1]).read()) - int(os.environ.get("FAKE_FFPROBE_SHORT", "0"))
print(json.dumps({"streams": [{"codec_name": "av1", "nb_read_packets": str(count)}]}))
'''


def wait_until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.fixture
def tools(tmp_path):
    paths = {}
    for name, text in (("ffmpeg", FAKE_FFMPEG), ("ffprobe", FAKE_FFPROBE)):
        path = tmp_path / f"fake-{name}"
        path.write_text(text.replace("{python}", sys.executable))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        paths[name] = str(path)
    return paths


class Camera:
    """Snapshot stand-in: answers JPEG until told to fail."""

    def __init__(self):
        self.calls = 0
        self.fail = None

    def __call__(self, url):
        self.calls += 1
        if self.fail:
            raise qa_timelapse.SnapshotError(self.fail)
        assert url == "http://camera/snapshot"
        return JPEG


@pytest.fixture
def lapse(rig, settings, writer, data_dir, tools):
    settings.update({"timelapse": {"snapshotUrl": "http://camera/snapshot", "minIntervalS": 0}})
    camera = Camera()
    # no thumbnails: the fake ffmpeg only knows encoding and extraction
    tl = qa_timelapse.Timelapse(writer, settings, data_dir, on_event=rig.collector.external_event,
                                ffmpeg=tools["ffmpeg"], ffprobe=tools["ffprobe"], fetch=camera,
                                thumbnail=lambda _data: None)
    tl.camera = camera
    rig.collector.timelapse = tl
    tl.start()
    yield tl
    tl.stop()


def row(rig, job_key=None):
    rows = rig.rows("SELECT t.*, j.id AS job_id FROM timelapse t JOIN jobs j ON j.key = t.job_key "
                    + ("WHERE t.job_key=?" if job_key else "ORDER BY j.started_at DESC LIMIT 1"),
                    *([job_key] if job_key else []))
    if not rows:
        return None
    rows[0]["layer_frames"] = qa_db.loads(rows[0]["layer_frames"])
    return rows[0]


def print_job(rig, layers=(1, 2, 3), end=True):
    rig.start_job()
    for layer in layers:
        rig.patch({"job": {"layer": layer, "duration": layer * 20}})
    if end:
        rig.patch({"state": {"status": "idle"}, "job": {"duration": None, "layer": None}})
        rig.collector.resolve_pending_end(rig.t, force=True)


# --- pure parts ---------------------------------------------------------------------------------

def test_commands():
    cmd = qa_timelapse.encode_command("ffmpeg", "/f", "/o.mp4.tmp", 30, 30, 12)
    assert cmd == ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-framerate", "30",
                   "-start_number", "0", "-i", "/f/%06d.jpg", "-frames:v", "12", "-c:v", "libsvtav1", "-g", "30",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", "/o.mp4.tmp"]
    cmd = qa_timelapse.encode_command("ffmpeg", "/f", "/o", 25, 10, 5, crf=35, preset=8, threads=2)
    assert cmd[cmd.index("-crf") + 1] == "35" and cmd[cmd.index("-preset") + 1] == "8"
    assert cmd[cmd.index("-svtav1-params") + 1] == "lp=2"
    assert qa_timelapse.extract_seek(0, 30) == 0.0
    assert qa_timelapse.extract_seek(31, 30) == pytest.approx(30.5 / 30)
    extract = qa_timelapse.extract_command("ffmpeg", "/v.mp4", 31, 30, "/x.jpg")
    assert extract[extract.index("-ss") + 1] == f"{30.5 / 30:.6f}"
    assert extract.index("-ss") < extract.index("-i")  # input seek: fast, and accurate in ffmpeg


def test_is_jpeg():
    assert qa_timelapse.is_jpeg(JPEG)
    assert qa_timelapse.is_jpeg(JPEG + b"\x00\x00")
    assert not qa_timelapse.is_jpeg(JPEG[:-2])      # truncated
    assert not qa_timelapse.is_jpeg(b"<html>error</html>")


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/snapshot":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(JPEG)
        elif self.path == "/html":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html></html>")
        elif self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/snapshot")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args):
        pass


def test_fetch_snapshot():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert qa_timelapse.fetch_snapshot(base + "/snapshot") == JPEG
        for path, reason in (("/missing", "HTTP 404"), ("/html", "not a complete JPEG"), ("/moved", "HTTP 302")):
            with pytest.raises(qa_timelapse.SnapshotError, match=reason):
                qa_timelapse.fetch_snapshot(base + path)
    finally:
        server.shutdown()
    with pytest.raises(qa_timelapse.SnapshotError):
        qa_timelapse.fetch_snapshot(base + "/snapshot", timeout=0.5)  # nobody listens any more


# --- capture and encoding -----------------------------------------------------------------------

def test_frame_per_finished_layer_then_video(rig, lapse, data_dir):
    print_job(rig)
    assert wait_until(lambda: (row(rig) or {}).get("status") == "done")
    r = row(rig)
    # the change to layer n shows layer n-1 finished; the last layer's frame comes at the job end
    assert [(e["layer"], e["frame"]) for e in r["layer_frames"]] == [(1, 0), (2, 1), (3, 2)]
    assert r["frames"] == 3 and r["codec"] == "av1" and r["fps"] == 30
    directory = os.path.join(data_dir, "timelapse", r["job_id"])
    assert open(os.path.join(directory, "timelapse.mp4")).read() == "3"
    assert not os.path.exists(os.path.join(directory, "frames"))  # verified: the JPEGs go
    assert r["size_bytes"] == 1 and r["path"].endswith("timelapse.mp4")
    assert lapse.status()["encoder"] == {"state": "idle", "jobId": None, "queued": 0}


def test_min_interval_skips_a_layer(rig, lapse, settings):
    settings.update({"timelapse": {"snapshotUrl": "http://camera/snapshot", "minIntervalS": 3600}})
    print_job(rig)
    assert wait_until(lambda: (row(rig) or {}).get("status") == "done")
    entries = row(rig)["layer_frames"]
    assert [(e["layer"], e["frame"], e.get("reason")) for e in entries] == [(1, 0, None), (2, None, "interval"),
                                                                            (3, 1, None)]


def test_snapshot_failure_is_one_event_and_entries_without_frame(rig, lapse):
    lapse.camera.fail = "HTTP 503"
    print_job(rig)
    assert wait_until(lambda: (row(rig) or {}).get("status") == "failed")
    r = row(rig)
    assert r["error"] == "no frame captured"
    assert [e["frame"] for e in r["layer_frames"]] == [None, None, None]
    assert r["layer_frames"][0]["reason"] == "snapshot: HTTP 503"
    failed = rig.events("timelapse_failed")
    assert [(e["subtype"], e["payload"].get("error")) for e in failed] == [("snapshot", "HTTP 503"),
                                                                          ("empty", "no frame captured")]


def test_m240_frames_replace_the_layer_changes(rig, lapse, settings):
    """A slicer macro sends M240 at each layer change (after ;LAYER_CHANGE, so job.layer is already n)"""
    settings.update({"timelapse": {"snapshotUrl": "http://camera/snapshot", "minIntervalS": 0, "settleMs": 0,
                                   "stillMs": 0}})
    rig.start_job()
    key = rig.collector.job.key
    rig.patch({"job": {"layer": 1, "duration": 20}})
    assert lapse.photo(key, 1, rig.t) == "before the first layer"     # the empty bed: no frame
    for layer in (2, 3):
        rig.patch({"job": {"layer": layer, "duration": layer * 20}})  # no snapshot of its own any more
        assert lapse.photo(key, layer, rig.t) == "taken"
    rig.patch({"state": {"status": "idle"}, "job": {"duration": None, "layer": None}})
    rig.collector.resolve_pending_end(rig.t, force=True)
    assert wait_until(lambda: (row(rig) or {}).get("status") == "done")
    assert [(e["layer"], e["frame"]) for e in row(rig)["layer_frames"]] == [(1, 0), (2, 1), (3, 2)]
    assert lapse.camera.calls == 3                                     # two M240, the job end


def test_m240_photo_failure_and_no_capture(rig, lapse, settings):
    settings.update({"timelapse": {"snapshotUrl": "http://camera/snapshot", "minIntervalS": 0, "settleMs": 0,
                                   "stillMs": 0}})
    assert lapse.photo(12345, 3, 0).startswith("no capture")
    rig.start_job()
    rig.patch({"job": {"layer": 2, "duration": 20}})
    lapse.camera.fail = "HTTP 503"
    assert lapse.photo(rig.collector.job.key, 2, rig.t) == "failed: HTTP 503"
    assert wait_until(lambda: len((row(rig) or {}).get("layer_frames") or []) == 1)
    assert row(rig)["layer_frames"][0] == {"layer": 1, "frame": None, "ts": rig.t, "reason": "snapshot: HTTP 503"}
    assert [e["subtype"] for e in rig.events("timelapse_failed")] == ["snapshot"]


# --- M240: a still picture ----------------------------------------------------------------------

class Film:
    """A camera on a fake clock: shows ``scenes[i]`` (a grey value for the whole thumbnail) from
    i × 0.1 s on, a new JPEG every 0.1 s like the CHX 350 camera, the last scene after the end."""

    def __init__(self, *scenes):
        self.t = 0.0
        self.scenes = scenes
        self.fail_after = None

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def fetch(self):
        if self.fail_after is not None and self.t >= self.fail_after:
            raise qa_timelapse.SnapshotError("timed out")
        return b"\xff\xd8frame %d\xff\xd9" % min(int(self.t * 10 + 1e-6), len(self.scenes) - 1)

    def thumbnail(self, data):
        return bytes([self.scenes[int(data[8:-2])]]) * 20

    def run(self, still_s=0.5, until=5.0):
        data, still = qa_timelapse.still_snapshot(self.fetch, self.thumbnail, still_s, until, self.clock, self.sleep)
        return int(data[8:-2]), still


def test_still_picture_helpers():
    assert qa_timelapse.changed_pixels(bytes([10, 10, 10]), bytes([10, 34, 35])) == 1   # 24 levels are not more
    cmd = qa_timelapse.thumbnail_command("ffmpeg")
    assert cmd[cmd.index("-i") + 1] == "pipe:0" and cmd[-1] == "pipe:1"
    assert "scale=160:90:flags=area,format=gray" in cmd


def test_still_snapshot_waits_until_the_picture_stood_still(monkeypatch):
    monkeypatch.setattr(qa_timelapse, "STILL_POLL_S", 0.05)  # every frame twice: a repeat does not count
    # printing, the retract pause before the park move (0.2 s), the move, parked from frame 7 on
    film = Film(0, 30, 60, 60, 60, 90, 120, 150, 150, 150, 150, 150, 150, 150, 150, 150, 150)
    frame, still = film.run(still_s=0.45)
    assert still is True
    assert frame == 12 and abs(film.t - 1.2) < 0.01   # the first frame 0.45 s after frame 7


def test_still_snapshot_gives_up(monkeypatch):
    monkeypatch.setattr(qa_timelapse, "STILL_POLL_S", 0.1)
    moving = Film(*range(0, 250, 30))
    assert moving.run(until=0.55) == (6, False)              # the last snapshot at the deadline
    parked = Film(150)
    parked.fail_after = 0.15                                 # the camera stops answering
    assert parked.run() == (0, False)
    dead = Film(150)
    dead.fail_after = 0
    with pytest.raises(qa_timelapse.SnapshotError):
        dead.run()
    blind = Film(0, 30, 60)
    data, still = qa_timelapse.still_snapshot(blind.fetch, lambda _data: None, 0.5, 5.0, blind.clock, blind.sleep)
    assert (data, still, blind.t) == (b"\xff\xd8frame 0\xff\xd9", None, 0)   # cannot judge: no waiting


def test_m240_photo_keeps_the_wait_in_the_index(rig, lapse, settings, monkeypatch):
    monkeypatch.setattr(qa_timelapse, "STILL_POLL_S", 0.01)
    settings.update({"timelapse": {"snapshotUrl": "http://camera/snapshot", "minIntervalS": 0, "settleMs": 0,
                                   "stillMs": 30}})
    shots = iter(range(1000))
    lapse._fetch = lambda _url: b"\xff\xd8%d\xff\xd9" % next(shots)
    lapse._thumbnail = lambda data: bytes([min(int(data[2:-2]) * 30, 240)]) * 20   # still from the 9th on
    rig.start_job()
    rig.patch({"job": {"layer": 2, "duration": 20}})
    assert lapse.photo(rig.collector.job.key, 2, rig.t).startswith("taken after ")
    assert wait_until(lambda: len((row(rig) or {}).get("layer_frames") or []) == 1)
    entry = row(rig)["layer_frames"][0]
    assert entry["still"] is True and entry["frame"] == 0 and entry["waitMs"] >= 30
    stored = os.path.join(lapse.job_dir(rig.collector.job.id), "frames", qa_timelapse.frame_name(0))
    with open(stored, "rb") as handle:
        assert int(handle.read()[2:-2]) >= 10   # the picture of the parked machine


def test_encoder_failure_keeps_frames(rig, lapse, data_dir, monkeypatch):
    monkeypatch.setenv("FAKE_FFPROBE_SHORT", "1")  # the video has a frame less than the index
    print_job(rig)
    assert wait_until(lambda: (row(rig) or {}).get("status") == "failed")
    r = row(rig)
    assert r["error"] == "video has 2 frames (av1), expected 3"
    frames = os.path.join(data_dir, "timelapse", r["job_id"], "frames")
    assert sorted(os.listdir(frames)) == ["000000.jpg", "000001.jpg", "000002.jpg"]
    assert not os.path.exists(os.path.join(data_dir, "timelapse", r["job_id"], "timelapse.mp4.tmp"))
    assert [e["subtype"] for e in rig.events("timelapse_failed")] == ["encode"]


def proc_state(pid):
    with open(f"/proc/{pid}/stat") as handle:
        return handle.read().rsplit(")", 1)[1].split()[0]


def test_a_new_job_pauses_the_encoder(rig, lapse, monkeypatch):
    monkeypatch.setenv("FAKE_FFMPEG_SLEEP", "1.5")
    print_job(rig)
    assert wait_until(lambda: lapse._proc is not None)
    first = row(rig)
    pid = lapse._proc.pid
    rig.start_job()  # the next job starts while the first one encodes
    assert lapse.status()["encoder"]["state"] == "paused"
    assert wait_until(lambda: proc_state(pid) == "T")  # SIGSTOP
    time.sleep(0.3)
    assert row(rig, first["job_key"])["status"] == "encoding"
    rig.patch({"state": {"status": "idle"}, "job": {"duration": None, "layer": None}})
    rig.collector.resolve_pending_end(rig.t, force=True)
    assert wait_until(lambda: row(rig, first["job_key"])["status"] == "done", timeout=10)


def test_encoder_priority_is_lowered_for_ffmpeg(rig, lapse, monkeypatch):
    monkeypatch.setenv("FAKE_FFMPEG_SLEEP", "1")
    print_job(rig)
    assert wait_until(lambda: lapse._proc is not None)
    nice = int(open(f"/proc/{lapse._proc.pid}/stat").read().rsplit(")", 1)[1].split()[16])
    assert nice == 19
    assert os.getpriority(os.PRIO_PROCESS, 0) == 0  # only the encoder thread and its children


def test_restart_recovers_an_unfinished_timelapse(rig, settings, writer, data_dir, tools):
    """A daemon that stopped while a job's timelapse was captured encodes it on the next start."""
    settings.update({"timelapse": {"snapshotUrl": "http://camera/snapshot", "minIntervalS": 0}})
    first = qa_timelapse.Timelapse(writer, settings, data_dir, fetch=Camera(), ffmpeg=tools["ffmpeg"],
                                   ffprobe=tools["ffprobe"])
    rig.collector.timelapse = first
    first.start()
    print_job(rig, end=False)
    assert wait_until(lambda: (row(rig) or {}).get("frames") == 2)
    first.stop()
    rig.collector.timelapse = None
    rig.patch({"state": {"status": "idle"}, "job": {"duration": None, "layer": None}})
    rig.collector.resolve_pending_end(rig.t, force=True)
    assert row(rig)["status"] == "capturing"

    second = qa_timelapse.Timelapse(writer, settings, data_dir, ffmpeg=tools["ffmpeg"], ffprobe=tools["ffprobe"])
    second.start()
    second.recover(None)
    try:
        assert wait_until(lambda: row(rig)["status"] == "done")
        assert open(os.path.join(data_dir, "timelapse", row(rig)["job_id"], "timelapse.mp4")).read() == "2"
    finally:
        second.stop()


def test_resume_drops_frames_the_index_does_not_know(tmp_path):
    frames = tmp_path / "frames"
    frames.mkdir()
    for n in range(4):
        (frames / qa_timelapse.frame_name(n)).write_bytes(JPEG)
    qa_timelapse.Timelapse._drop_frames_from(str(frames), 2)
    assert sorted(os.listdir(frames)) == ["000000.jpg", "000001.jpg"]


def test_retention_by_count_and_size(rig, lapse, settings, data_dir):
    ids = []
    for _ in range(3):
        print_job(rig, layers=(1, 2))
        assert wait_until(lambda: (row(rig) or {}).get("status") == "done")
        ids.append(row(rig)["job_id"])
        rig.patch({}, dt_ms=60_000)
    settings.update({"timelapse": {"snapshotUrl": "http://camera/snapshot", "retention": {"jobs": 2}}})
    lapse.request_retention()
    statuses = lambda: [r["status"] for r in rig.rows(  # noqa: E731
        "SELECT t.status FROM timelapse t JOIN jobs j ON j.key = t.job_key ORDER BY j.started_at")]
    assert wait_until(lambda: statuses() == ["pruned", "done", "done"])
    assert not os.path.exists(os.path.join(data_dir, "timelapse", ids[0]))
    settings.update({"timelapse": {"snapshotUrl": "http://camera/snapshot", "retention": {"jobs": 2, "maxBytes": 1}}})
    lapse.request_retention()
    assert wait_until(lambda: statuses() == ["pruned", "pruned", "done"])
    assert os.path.exists(os.path.join(data_dir, "timelapse", ids[2], "timelapse.mp4"))


def test_disabled_or_without_url_records_nothing(rig, settings, writer, data_dir):
    tl = qa_timelapse.Timelapse(writer, settings, data_dir, fetch=Camera())
    rig.collector.timelapse = tl
    tl.start()
    try:
        print_job(rig)
        time.sleep(0.1)
        assert row(rig) is None
        assert tl.status()["reason"] == "no snapshotUrl set"
    finally:
        tl.stop()


# --- API ----------------------------------------------------------------------------------------

class Req:
    def __init__(self, **queries):
        self.queries = {k: str(v) for k, v in queries.items()}
        self.body = ""
        self.session_id = 7


@pytest.fixture
def ctx(rig, lapse, data_dir, settings, writer):
    return qa_api.ApiContext(version="test", settings=settings, writer=writer, readers=qa_db.Readers(data_dir),
                             data_dir=data_dir, collector=rig.collector, timelapse=lapse)


def api(ctx, path, **queries):
    response = qa_api.call(ctx, qa_api.ENDPOINTS[("GET", path)], Req(**queries))
    return response.status, response.body, response.kind


def test_api_during_capture_and_after_encoding(rig, lapse, ctx, monkeypatch):
    monkeypatch.setenv("FAKE_FFMPEG_SLEEP", "0.5")
    print_job(rig, end=False)
    assert wait_until(lambda: (row(rig) or {}).get("frames") == 2)
    job_id = row(rig)["job_id"]
    status, meta, _ = api(ctx, "job/timelapse/meta", id=job_id)
    assert status == 200 and meta["status"] == "capturing" and meta["video"] is False
    assert [(e["layer"], e["frame"]) for e in meta["layers"]] == [(1, 0), (2, 1)]
    # during the print: the captured JPEG itself
    status, path, kind = api(ctx, "job/timelapse/frame", id=job_id, layer=2)
    assert (status, kind) == (200, "file") and path.endswith("frames/000001.jpg")
    assert api(ctx, "job/timelapse/frame", id=job_id, layer=3)[0] == 404  # not finished yet
    assert api(ctx, "job/timelapse", id=job_id)[0] == 404

    rig.patch({"state": {"status": "idle"}, "job": {"duration": None, "layer": None}})
    rig.collector.resolve_pending_end(rig.t, force=True)
    assert wait_until(lambda: row(rig)["status"] == "done")
    status, meta, _ = api(ctx, "job/timelapse/meta", id=job_id)
    assert meta["status"] == "done" and meta["video"] is True and meta["frames"] == 3
    status, path, kind = api(ctx, "job/timelapse", id=job_id)
    assert (status, kind) == (200, "file") and path.endswith("timelapse.mp4")
    # after the video: extracted, and reused
    status, path, kind = api(ctx, "job/timelapse/frame", id=job_id, layer=2)
    assert (status, kind) == (200, "file") and os.path.basename(path) == f"frame-{job_id}-1.jpg"
    assert qa_timelapse.is_jpeg(open(path, "rb").read())
    assert api(ctx, "job/timelapse/frame", id=job_id, layer=2)[1] == path


def test_api_without_timelapse(rig, lapse, ctx, settings):
    settings.update({"timelapse": {"snapshotUrl": None}})
    print_job(rig)
    job_id = rig.rows("SELECT id FROM jobs")[0]["id"]
    status, meta, _ = api(ctx, "job/timelapse/meta", id=job_id)
    assert status == 200 and meta == {"jobId": job_id, "status": "none", "reason": "no snapshotUrl set",
                                      "video": False, "layers": []}
    assert api(ctx, "job/timelapse", id=job_id)[0] == 404
    assert api(ctx, "job/timelapse/frame", id=job_id, layer=1)[0] == 404
    assert api(ctx, "job/timelapse/meta", id="nope")[0] == 404
    assert api(ctx, "job/timelapse/frame", id=job_id)[0] == 400


# --- the real thing -----------------------------------------------------------------------------

def _has_svtav1():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        return False
    encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libsvtav1" in encoders


@pytest.mark.skipif(not _has_svtav1(), reason="needs ffmpeg with libsvtav1 (Debian trixie: apt install ffmpeg)")
def test_real_ffmpeg(tmp_path):
    frames = tmp_path / "frames"
    frames.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10",
                    "-frames:v", "12", "-q:v", "3", "-start_number", "0", str(frames / "%06d.jpg")], check=True)
    (frames / qa_timelapse.frame_name(12)).write_bytes(JPEG)  # a stale frame beyond the index is not read
    video = str(tmp_path / "t.mp4")
    subprocess.run(qa_timelapse.encode_command("ffmpeg", str(frames), video + ".tmp", 10, 5, 12, crf=40, preset=12,
                                               threads=1), check=True)
    probe = json.loads(subprocess.run(qa_timelapse.probe_command("ffprobe", video + ".tmp"), capture_output=True,
                                      text=True, check=True).stdout)
    assert probe["streams"][0] == {"codec_name": "av1", "nb_read_packets": "12"}
    keyframes = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "packet=flags", "-of", "csv=p=0", video + ".tmp"], capture_output=True, text=True,
                               check=True).stdout.split()
    assert [i for i, flags in enumerate(keyframes) if flags.startswith("K")] == [0, 5, 10]
    out = str(tmp_path / "x.jpg")
    subprocess.run(qa_timelapse.extract_command("ffmpeg", video + ".tmp", 7, 10, out), check=True)

    def psnr(a, b):
        text = subprocess.run(["ffmpeg", "-nostdin", "-i", a, "-i", b, "-lavfi", "psnr", "-f", "null", "-"],
                              capture_output=True, text=True).stderr
        value = text.rsplit("average:", 1)[1].split()[0]
        return float("inf") if value == "inf" else float(value)

    source = lambda n: str(frames / qa_timelapse.frame_name(n))  # noqa: E731
    assert psnr(out, source(7)) > psnr(out, source(6)) + 5
    assert psnr(out, source(7)) > psnr(out, source(8)) + 5


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg (Debian trixie: apt install ffmpeg)")
def test_real_ffmpeg_thumbnail(tmp_path, writer, settings, data_dir):
    shots = []
    for second in (0, 3):  # the CHX 350 camera's size
        out = tmp_path / f"{second}.jpg"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=1",
                        "-ss", str(second), "-frames:v", "1", str(out)], check=True)
        shots.append(out.read_bytes())
    tl = qa_timelapse.Timelapse(writer, settings, data_dir)
    first, again, later = (tl._ffmpeg_thumbnail(shot) for shot in (shots[0], shots[0], shots[1]))
    assert len(first) == qa_timelapse.THUMB_W * qa_timelapse.THUMB_H
    assert qa_timelapse.changed_pixels(first, again) == 0
    assert qa_timelapse.changed_pixels(first, later) > qa_timelapse.STILL_PIXELS
    assert tl._ffmpeg_thumbnail(b"\xff\xd8 not a JPEG \xff\xd9") is None
