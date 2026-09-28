"""Timelapse (PLAN.md §5.11): a JPEG snapshot per finished layer while a job prints, one AV1 video
after it.

Capture. The collector thread only queues work; the capture thread fetches
``timelapse.snapshotUrl``. The snapshot taken when ``job.layer`` changes to n shows layer n-1
finished, so it is that layer's frame; the last layer's frame is taken at the job end. A job whose
G-code sends M240 ("trigger camera", usually from a slicer macro that parks the head first; Tim
2026-09-28) gets its frames from M240 instead, from the first one on: qa_intercept holds the code,
``photo`` waits ``settleMs`` for the camera and fetches while the machine stands still, the capture
thread stores the JPEG. The M240 at the change to layer n is layer n-1's frame as well. Only a
valid JPEG gets a frame number (``000000.jpg``, ``000001.jpg``, … without gaps: that is the input
ffmpeg reads); a failed or skipped snapshot is an index entry without a frame. Frames live in
``<data>/timelapse/<job id>/frames/`` until the video is verified.

Encoding. After the job, while no job prints, one ffmpeg at a time encodes with libsvtav1 into MP4
with a keyframe every ``keyframeInterval`` frames, so a single frame decodes quickly. The encoder
thread lowers its own priority to nice 19 and I/O class idle, and every ffmpeg it starts inherits
both: Linux keeps the nice value per thread (setpriority(2) BUGS) and ``ioprio_set`` with who 0
sets the calling thread (ioprio_set(2); man-pages 6.7); checked in a Debian trixie container on
2026-09-27 (child: nice 19, ``ionice -p`` idle; main thread unchanged). No ``nice``/``ionice``
binaries and no ``preexec_fn`` (unsafe with threads). A job that starts pauses ffmpeg with SIGSTOP,
its end resumes it with SIGCONT. ffprobe then counts the packets: equal to the frames → the JPEGs
go; otherwise they stay (``keepFramesOnFailure``) and a ``timelapse_failed`` event is written.

Checked against Debian trixie's ffmpeg 7.1.5 (libsvtav1enc2 2.3.0, libdav1d7 1.5.1, amd64
container, 2026-09-27): ``-preset`` −2..13 and ``-crf`` 0..63 with −2/0 meaning SVT's default;
``-g 30`` puts a keyframe every 30 frames; the MP4 reports nb_frames = packets = JPEGs;
``-ss (n − 0.5)/fps`` before ``-i`` extracts frame n (PSNR 41 dB against n, 24 dB against its
neighbours); odd sizes encode. Peak memory at 1984×1080 (the CHX 350 camera): 1.16 GB with SVT's
default threads on 12 cores, 0.95 GB ``lp=4``, 0.57 GB ``lp=2``, 0.46 GB ``lp=1``. A paused
encoder keeps its memory through the print, hence ``encoderThreads`` (default 2).
"""

import ctypes
import json
import os
import platform
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

from qa_log import logger

VIDEO_NAME = "timelapse.mp4"
FRAMES_DIR = "frames"
LOG_NAME = "encode.log"
SNAPSHOT_TIMEOUT_S = 5
SNAPSHOT_MAX_BYTES = 20 * 1024 * 1024
PROBE_TIMEOUT_S = 120
# a frame request must answer well inside the 10 s the HMI's haproxy gives a busy DSF (§5.6)
EXTRACT_TIMEOUT_S = 8
_JOB_ID_RE = re.compile(r"^[0-9A-Za-z-]+$")

# ioprio_set: syscall numbers from linux-libc-dev 6.8 (asm/unistd_64.h: 251,
# asm-generic/unistd.h as used by arm64: 30); class and "who" from linux/ioprio.h
_IOPRIO_SET = {"x86_64": 251, "aarch64": 30}
_IOPRIO_WHO_PROCESS = 1
_IOPRIO_CLASS_IDLE = 3
_IOPRIO_CLASS_SHIFT = 13


def frame_name(number):
    return f"{number:06d}.jpg"


def is_jpeg(data):
    """SOI at the start and EOI at the end (trailing padding allowed), so a truncated download is
    not taken as a frame."""
    return len(data) > 4 and data[:2] == b"\xff\xd8" and b"\xff\xd9" in data[-16:]


def encode_command(ffmpeg, frames_dir, out_path, fps, keyframe_interval, frames, crf=None, preset=None, threads=None):
    """ffmpeg arguments for the frames ``000000.jpg…`` in ``frames_dir`` → AV1 in MP4. Exactly
    ``frames`` frames: files a crash left beyond the committed index are not read."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-framerate", str(fps), "-start_number", "0", "-i", os.path.join(frames_dir, "%06d.jpg"),
           "-frames:v", str(int(frames)), "-c:v", "libsvtav1", "-g", str(int(keyframe_interval))]
    if crf is not None:
        cmd += ["-crf", str(int(crf))]
    if preset is not None:
        cmd += ["-preset", str(int(preset))]
    if threads:
        cmd += ["-svtav1-params", f"lp={int(threads)}"]
    # yuv420p: what libsvtav1 takes and every browser plays; the camera's JPEGs are yuvj420p
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", out_path]
    return cmd


def probe_command(ffprobe, path):
    """Codec and packet count of the first video stream as JSON."""
    return [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
            "-show_entries", "stream=codec_name,nb_read_packets", "-of", "json", path]


def extract_seek(frame, fps):
    """Seek position for frame ``frame``: ffmpeg's accurate seek outputs the first frame at or after
    it, so half a frame early hits exactly this one (a browser shows frame n at (n + 0.5)/fps)."""
    return max(0.0, (frame - 0.5) / fps)


def extract_command(ffmpeg, video, frame, fps, out_path):
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-ss", f"{extract_seek(frame, fps):.6f}", "-i", video, "-frames:v", "1",
            "-c:v", "mjpeg", "-q:v", "3", "-f", "image2", out_path]


class SnapshotError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a camera answers directly; following redirects would widen the URL setting


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def fetch_snapshot(url, timeout=SNAPSHOT_TIMEOUT_S):
    """The JPEG at ``url``. Raises SnapshotError with a short reason."""
    try:
        with _opener.open(url, timeout=timeout) as response:
            data = response.read(SNAPSHOT_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise SnapshotError(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SnapshotError(str(reason)[:200]) from None
    if len(data) > SNAPSHOT_MAX_BYTES:
        raise SnapshotError("snapshot larger than 20 MB")
    if not is_jpeg(data):
        raise SnapshotError("not a complete JPEG")
    return data


def lower_thread_priority():
    """nice 19 and I/O class idle for the calling thread (inherited by processes it starts)."""
    try:
        os.setpriority(os.PRIO_PROCESS, 0, 19)
    except OSError as exc:
        logger.debug("setpriority failed: %s", exc)
    number = _IOPRIO_SET.get(platform.machine())
    if number is None:
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.syscall(number, _IOPRIO_WHO_PROCESS, 0, _IOPRIO_CLASS_IDLE << _IOPRIO_CLASS_SHIFT) != 0:
            logger.debug("ioprio_set failed: errno %d", ctypes.get_errno())
    except (OSError, AttributeError) as exc:
        logger.debug("ioprio_set unavailable: %s", exc)


def _tree_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


class _Capture:
    """Capture state of one job (only the capture thread touches ``entries``/``next_frame``)."""

    def __init__(self, key, job_id, directory, entries, next_frame, last_layer):
        self.key = key
        self.id = job_id
        self.directory = directory
        self.frames_dir = os.path.join(directory, FRAMES_DIR)
        self.entries = entries
        self.next_frame = next_frame
        self.last_layer = last_layer      # collector thread
        self.last_queued = None           # collector thread, monotonic
        self.snapshot_failed = False
        self.marker = False               # frames come from M240 (set by the interceptor thread)


class Timelapse:
    """Capture thread + encoder thread. The collector calls ``job_started``, ``layer_changed`` and
    ``job_finished`` from its thread (``recover`` once after a daemon start); the API reads through
    ``status``, ``video_file``, ``frame_file`` and ``extract_frame``."""

    def __init__(self, writer, settings, data_dir, on_event=None, ffmpeg=None, ffprobe=None, fetch=None):
        self.writer = writer
        self.settings = settings
        self.root = os.path.join(data_dir, "timelapse")
        self.tmp_dir = os.path.join(data_dir, "tmp")
        # (job_key, ts_ms, type, subtype, payload), thread-safe; set by the daemon to the collector's
        self.on_event = on_event or (lambda *_args: None)
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self.ffprobe = ffprobe or shutil.which("ffprobe")
        self._fetch = fetch or fetch_snapshot
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._captures = {}               # job key -> _Capture
        self._capture_queue = queue.Queue()
        self._encode_queue = []           # job keys, under _lock
        self._printing = False
        self._proc = None
        self._paused = False
        self._encoding = None             # job id
        self._retention_due = True
        self._stopping = False
        self._threads = []
        self.last_error = None

    # --- lifecycle ---------------------------------------------------------------------------

    def start(self):
        for target, name in ((self._capture_loop, "qa-timelapse-capture"), (self._encode_loop, "qa-timelapse-encode")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout=10):
        with self._lock:
            self._stopping = True
            proc = self._proc
            self._wake.notify_all()
        self._capture_queue.put(None)
        if proc is not None:
            self._kill(proc)
        for thread in self._threads:
            thread.join(timeout)

    @staticmethod
    def _kill(proc):
        try:
            proc.send_signal(signal.SIGCONT)  # a stopped process does not act on SIGTERM
            proc.terminate()
            proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    def cfg(self):
        return self.settings.current()["timelapse"]

    def job_dir(self, job_id):
        if not job_id or not _JOB_ID_RE.match(job_id):
            raise ValueError(f"bad job id {job_id!r}")
        return os.path.join(self.root, job_id)

    # --- collector hooks (collector thread) ------------------------------------------------------

    def job_started(self, job):
        """A job prints: pause the encoder; start (or, after a daemon restart, continue) capturing."""
        with self._lock:
            self._printing = True
            if self._proc is not None and not self._paused:
                try:
                    self._proc.send_signal(signal.SIGSTOP)
                    self._paused = True
                except OSError:
                    pass
        cfg = self.cfg()
        if not cfg["enabled"] or not cfg["snapshotUrl"]:
            return
        row = self.writer.call("timelapse_get", job.key)
        directory = self.job_dir(job.id)
        entries = list((row or {}).get("layer_frames") or [])
        next_frame = (row or {}).get("frames") or 0
        capture = _Capture(job.key, job.id, directory, entries, next_frame, job.layer)
        self._drop_frames_from(capture.frames_dir, next_frame)
        with self._lock:
            self._captures[job.key] = capture
        if row is None:
            self.writer.submit("timelapse_upsert", job.key, {"status": "capturing", "codec": "av1", "fps": cfg["fps"],
                                                            "frames": 0, "layer_frames": []}, urgent=True)

    @staticmethod
    def _drop_frames_from(frames_dir, first):
        """Frames a crash wrote after the last committed index; new ones take their numbers."""
        try:
            names = os.listdir(frames_dir)
        except OSError:
            return
        for name in names:
            stem = name.split(".")[0]
            if stem.isdigit() and int(stem) >= first:
                try:
                    os.remove(os.path.join(frames_dir, name))
                except OSError:
                    pass

    def layer_changed(self, job, layer, now_ms):
        with self._lock:
            capture = self._captures.get(job.key)
        if capture is None:
            return
        finished, capture.last_layer = capture.last_layer, layer
        if finished is None or capture.marker:
            return
        mono = time.monotonic()
        if capture.last_queued is not None and mono - capture.last_queued < self.cfg()["minIntervalS"]:
            self._capture_queue.put(("skip", job.key, finished, now_ms, "interval"))
            return
        capture.last_queued = mono
        self._capture_queue.put(("frame", job.key, finished, now_ms, None))

    # --- M240 (qa_intercept thread; the print waits until this returns) --------------------------

    def photo(self, job_key, layer, ts_ms):
        """The frame of layer ``layer`` - 1, fetched now; the capture thread stores it. Returns what
        happened, for the log."""
        with self._lock:
            capture = self._captures.get(job_key)
        if capture is None:
            return "no capture (off, no snapshotUrl or no job)"
        capture.marker = True
        finished = (layer or 0) - 1
        if finished < 1:
            return "before the first layer"
        cfg = self.cfg()
        if cfg["settleMs"]:
            time.sleep(cfg["settleMs"] / 1000.0)   # the camera's picture lags the machine
        try:
            result = self._fetch(cfg["snapshotUrl"])
        except SnapshotError as exc:
            result = exc
        self._capture_queue.put(("photo", job_key, finished, ts_ms, result))
        return f"failed: {result}" if isinstance(result, Exception) else "taken"

    def job_finished(self, job, _result):
        """The last layer's frame, then the job goes to the encoder; the encoder may run again."""
        with self._lock:
            capture = self._captures.get(job.key)
            self._printing = False
            if self._proc is not None and self._paused:
                try:
                    self._proc.send_signal(signal.SIGCONT)
                except OSError:
                    pass
                self._paused = False
            self._wake.notify_all()
        if capture is not None:
            if capture.last_layer is not None:
                self._capture_queue.put(("frame", job.key, capture.last_layer, int(time.time() * 1000), None))
            self._capture_queue.put(("finish", job.key, None, None, None))

    def recover(self, current_key):
        """After a daemon start: finish what an earlier run left. Capturing rows of jobs that are not
        running any more go to the encoder, like queued and interrupted ones."""
        try:
            rows = self.writer.call("timelapse_rows")
        except Exception as exc:  # noqa: BLE001
            logger.error("timelapse: reading pending work failed: %s", exc)
            return
        with self._lock:
            for row in rows:
                if row["job_key"] == current_key:
                    continue
                if row["status"] in ("capturing", "queued", "encoding") and row["job_key"] not in self._encode_queue:
                    self._encode_queue.append(row["job_key"])
            self._wake.notify_all()

    def request_retention(self):
        with self._lock:
            self._retention_due = True
            self._wake.notify_all()

    # --- capture thread ------------------------------------------------------------------------

    def _capture_loop(self):
        while True:
            item = self._capture_queue.get()
            if item is None:
                return
            kind, key, layer, ts_ms, reason = item
            with self._lock:
                capture = self._captures.get(key)
            if capture is None:
                continue
            try:
                if kind == "frame":
                    self._take(capture, layer, ts_ms)
                elif kind == "photo":             # fetched by photo(): the JPEG or the SnapshotError
                    self._take(capture, layer, ts_ms, fetched=reason)
                elif kind == "skip":
                    capture.entries.append({"layer": layer, "frame": None, "ts": ts_ms, "reason": reason})
                    self._store(capture)
                elif kind == "finish":
                    self._finish(capture)
            except Exception as exc:  # noqa: BLE001
                logger.error("timelapse capture error: %s", exc)

    def _take(self, capture, layer, ts_ms, fetched=None):
        try:
            if isinstance(fetched, SnapshotError):
                raise fetched
            data = fetched
            if data is None:
                url = self.cfg()["snapshotUrl"]
                if not url:
                    raise SnapshotError("no snapshotUrl set")
                data = self._fetch(url)
            os.makedirs(capture.frames_dir, exist_ok=True)
            path = os.path.join(capture.frames_dir, frame_name(capture.next_frame))
            with open(path + ".tmp", "wb") as handle:
                handle.write(data)
            os.replace(path + ".tmp", path)
        except (SnapshotError, OSError) as exc:
            capture.entries.append({"layer": layer, "frame": None, "ts": ts_ms, "reason": f"snapshot: {exc}"})
            self._store(capture)
            if not capture.snapshot_failed:  # one event per job, not one per layer
                capture.snapshot_failed = True
                self.last_error = f"snapshot: {exc}"
                self.on_event(capture.key, ts_ms, "timelapse_failed", "snapshot", {"error": str(exc), "layer": layer})
            return
        capture.entries.append({"layer": layer, "frame": capture.next_frame, "ts": ts_ms})
        capture.next_frame += 1
        self._store(capture)

    def _store(self, capture):
        self.writer.submit("timelapse_upsert", capture.key, {"frames": capture.next_frame,
                                                             "layer_frames": capture.entries})

    def _finish(self, capture):
        with self._lock:
            self._captures.pop(capture.key, None)
        if capture.next_frame == 0:
            self.writer.submit("timelapse_upsert", capture.key, {"status": "failed", "error": "no frame captured"},
                               urgent=True)
            self.on_event(capture.key, int(time.time() * 1000), "timelapse_failed", "empty",
                          {"error": "no frame captured"})
            return
        self.writer.submit("timelapse_upsert", capture.key, {"status": "queued"}, urgent=True)
        with self._lock:
            if capture.key not in self._encode_queue:
                self._encode_queue.append(capture.key)
            self._wake.notify_all()

    # --- encoder thread ------------------------------------------------------------------------

    def _encode_loop(self):
        lower_thread_priority()
        while True:
            with self._lock:
                while not self._stopping and (self._printing or not (self._encode_queue or self._retention_due)):
                    self._wake.wait()
                if self._stopping:
                    return
                if self._retention_due:
                    self._retention_due = False
                    key = None
                else:
                    key = self._encode_queue.pop(0)
            try:
                if key is None:
                    self._retention()
                else:
                    self._encode(key)
                    self._retention()
            except Exception as exc:  # noqa: BLE001
                logger.error("timelapse encoder error: %s", exc)
                self.last_error = str(exc)

    def _encode(self, key):
        row = self.writer.call("timelapse_get", key)
        if row is None or row["status"] not in ("capturing", "queued", "encoding"):
            return
        cfg = self.cfg()
        directory = self.job_dir(row["job_id"])
        frames_dir = os.path.join(directory, FRAMES_DIR)
        count = row["frames"] or 0
        if count == 0:
            self._failed(row, "no frame captured", "empty", keep=False)
            return
        if not self.ffmpeg or not self.ffprobe:
            self._failed(row, "ffmpeg/ffprobe not found", "encode")
            return
        fps = row["fps"] or cfg["fps"]
        self.writer.submit("timelapse_upsert", key, {"status": "encoding", "fps": fps}, urgent=True)
        os.makedirs(directory, exist_ok=True)  # removed by hand: ffmpeg then fails and says so
        video = os.path.join(directory, VIDEO_NAME)
        tmp = video + ".tmp"
        cmd = encode_command(self.ffmpeg, frames_dir, tmp, fps, cfg["keyframeInterval"], count, cfg["crf"],
                             cfg["preset"], cfg["encoderThreads"])
        with open(os.path.join(directory, LOG_NAME), "wb") as log:
            with self._lock:
                if self._stopping:
                    return
                while self._printing and not self._stopping:
                    self._wake.wait()
                if self._stopping:
                    return
                self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log)
                self._paused = False
                self._encoding = row["job_id"]
                proc = self._proc
            try:
                code = proc.wait()
            finally:
                with self._lock:
                    self._proc = None
                    self._paused = False
                    self._encoding = None
        with self._lock:
            if self._stopping:
                return  # stays "encoding": the next start encodes again
        if code != 0:
            self._failed(row, f"ffmpeg exit {code}: {self._log_tail(directory)}", "encode", tmp=tmp)
            return
        packets, codec = self._probe(tmp)
        if packets != count or codec != "av1":
            self._failed(row, f"video has {packets} frames ({codec}), expected {count}", "encode", tmp=tmp)
            return
        os.replace(tmp, video)
        shutil.rmtree(frames_dir, ignore_errors=True)
        try:
            os.remove(os.path.join(directory, LOG_NAME))
        except OSError:
            pass
        self.writer.submit("timelapse_upsert", key, {"status": "done", "path": video, "codec": "av1",
                                                     "size_bytes": os.path.getsize(video), "error": None},
                           urgent=True)

    def _probe(self, path):
        try:
            result = subprocess.run(probe_command(self.ffprobe, path), capture_output=True, text=True,
                                    timeout=PROBE_TIMEOUT_S, check=False)
            stream = (json.loads(result.stdout or "{}").get("streams") or [{}])[0]
            return int(stream.get("nb_read_packets") or 0), stream.get("codec_name")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return 0, f"probe failed: {exc}"

    @staticmethod
    def _log_tail(directory):
        try:
            with open(os.path.join(directory, LOG_NAME), "rb") as handle:
                return handle.read()[-300:].decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def _failed(self, row, error, subtype, keep=None, tmp=None):
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        keep = self.cfg()["keepFramesOnFailure"] if keep is None else keep
        if not keep:
            shutil.rmtree(os.path.join(self.job_dir(row["job_id"]), FRAMES_DIR), ignore_errors=True)
        self.last_error = error
        logger.warning("timelapse of %s failed: %s", row["job_id"], error)
        self.writer.submit("timelapse_upsert", row["job_key"], {"status": "failed", "error": error[:500]}, urgent=True)
        self.on_event(row["job_key"], int(time.time() * 1000), "timelapse_failed", subtype, {"error": error[:500]})

    def _retention(self):
        """Keep the files of the newest ``retention.jobs`` timelapses, then drop the oldest while all
        of them together are larger than ``retention.maxBytes``. The index stays, status ``pruned``."""
        limits = self.cfg()["retention"]
        rows = [r for r in self.writer.call("timelapse_rows") if r["status"] in ("done", "failed")]
        rows.sort(key=lambda r: r["started_at"], reverse=True)
        keep = []
        for index, row in enumerate(rows):
            if index < limits["jobs"]:
                keep.append(row)
            else:
                self._prune(row)
        sizes = [(row, _tree_size(self.job_dir(row["job_id"]))) for row in keep]
        total = sum(size for _row, size in sizes)
        while sizes and total > limits["maxBytes"]:
            row, size = sizes.pop()
            self._prune(row)
            total -= size

    def _prune(self, row):
        shutil.rmtree(self.job_dir(row["job_id"]), ignore_errors=True)
        self.writer.submit("timelapse_upsert", row["job_key"], {"status": "pruned", "path": None})

    # --- API (endpoint threads) ----------------------------------------------------------------

    def status(self):
        cfg = self.cfg()
        reason = None
        if not cfg["enabled"]:
            reason = "disabled in the settings"
        elif not cfg["snapshotUrl"]:
            reason = "no snapshotUrl set"
        elif not self.ffmpeg or not self.ffprobe:
            reason = "ffmpeg not found"
        with self._lock:
            state = "idle" if self._proc is None else ("paused" if self._paused else "encoding")
            return {
                "enabled": reason is None,
                "reason": reason,
                "capturing": [c.id for c in self._captures.values()],
                "encoder": {"state": state, "jobId": self._encoding, "queued": len(self._encode_queue)},
                "lastError": self.last_error,
            }

    def frame_file(self, job_id, frame):
        """Path of a captured JPEG that still exists, else None."""
        path = os.path.join(self.job_dir(job_id), FRAMES_DIR, frame_name(frame))
        return path if os.path.isfile(path) else None

    def video_file(self, job_id):
        path = os.path.join(self.job_dir(job_id), VIDEO_NAME)
        return path if os.path.isfile(path) else None

    def extract_frame(self, job_id, frame, fps):
        """Frame ``frame`` of the video as a JPEG under ``<data>/tmp`` (kept for reuse, cleaned
        with the other temporary files). None when the video is missing or ffmpeg fails."""
        video = self.video_file(job_id)
        if video is None or not self.ffmpeg:
            return None
        os.makedirs(self.tmp_dir, exist_ok=True)
        out = os.path.join(self.tmp_dir, f"frame-{job_id}-{frame}.jpg")
        if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(video):
            os.utime(out)  # in use again: not old enough for the tmp cleanup
            return out
        try:
            subprocess.run(extract_command(self.ffmpeg, video, frame, fps, out + ".tmp.jpg"), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=EXTRACT_TIMEOUT_S, check=True)
            os.replace(out + ".tmp.jpg", out)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("frame %d of %s: %s", frame, job_id, exc)
            return None
        return out
