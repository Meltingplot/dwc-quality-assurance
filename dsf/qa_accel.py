"""Accelerometer (PLAN.md §3 "Accelerometer", phase 6): a vibration spectrum every ``intervalMin``
minutes while a job prints, from the accelerometer (configured with M955) on the board whose CAN
address ``accelerometer.board`` names; without that setting QA records none. On the CHX 350 that is
the SZP at CAN address 60: ``sensors.accelerometers[0]``, port ``60.i2c.lis``, 800 Hz, 14 bit
(object model, Tim 2026-09-27).

Recording. The collector decides when (status ``processing``, from layer 2 on, every
``intervalMin``); the recorder thread sends ``M956 P<n> S<samples> A0 F"qa-<job>-<epoch s>.csv"`` on the
SBC code channel, which runs alongside the print's file channel. RRF starts the collection at once
and takes no movement lock (``Accelerometers::StartAccelerometer``), and counts every finished run
in ``runs``, a failed one with ``points`` 0 (``AddLocalAccelerometerRun``/``AddRemoteAccelerometerRun``;
RepRapFirmware 3.7-dev @ 3638836, 2026-09-27). ``P`` is the index in ``sensors.accelerometers``
(DuetAPI ``Sensors.Accelerometers``, DSF v3.7-dev @ cd3ae65f). The recorder waits for ``runs`` to
advance, reads the CSV from ``0:/sys/accelerometer/``, stores one spectrum per axis and deletes the
file. In SBC mode ``runs`` advances as soon as RRF asked DSF to close the file, so the last line can
be missing for a moment: read again every 250 ms (DWC v3.7-dev @ 1fb6a51,
``InputShaping/useAccelerometer.ts`` ``loadAccelerometerFile``). No retries of a failed recording,
one ``accelerometer_failed`` event instead (PLAN.md §8).

CSV (RRF ``Accelerometers.cpp``, same commit; parsed like @duet3d/motionanalysis 3.7.0-rc.1
``parseAccelerometerCsv``): ``Sample[,X][,Y][,Z]``, one row per sample in g, last line
``Rate <Hz>, overflows <n>`` with the measured rate. A failed run ends with a reason instead
(``Received bad data``, ``Received mismatched data``, ``Board restarted before the collection was
complete``). Overflows > 0 means lost samples: discard (wiki Sensors_Accelerometer.md).

Spectrum as DWC's input-shaping plugin computes it (motionanalysis ``analyzeAccelerometerData`` with
wide band and window): mean removed, Hann window, |DFT| × 4/N at k × rate/N for k = 1 … N/2, so QA's
spectra and the plugin's agree. Pure Python (radix-2 FFT, Bluestein for other lengths) instead of
numpy: three axes of about 1000 samples every 15 minutes take milliseconds, and a C extension would
have to be installed into the plugin venv by the image build (docs/image.md). The peak is the
largest amplitude at or above ``PEAK_MIN_HZ``; RMS is that of the samples without their mean, in g.
"""

import cmath
import math
import os
import queue
import re
import threading
import time

from qa_log import logger

ACCEL_DIR = "0:/sys/accelerometer"
# DWC reads the CSV again every 250 ms; 8 tries cover 2 s of DSF closing the file
CSV_RETRIES = 8
CSV_RETRY_S = 0.25
# waiting for runs: the collection itself plus CAN transfer and file writing
RUN_MARGIN_S = 20
# below this a spectrum is drift and window leakage, not a resonance of the machine
PEAK_MIN_HZ = 5.0
_TRAILER_RE = re.compile(r"^Rate (\d+(?:\.\d+)?),? overflows (\d+)")
_BOARD_RE = re.compile(r"^[!^*]*(\d+)\.")


class CsvIncomplete(Exception):
    """The file has no last line yet (DSF still writing it)."""


class CsvError(Exception):
    """The run failed or the file is not an accelerometer CSV; the message says why."""


def parse_csv(text):
    """``{"axes": [...], "samples": {axis: [g, ...]}, "rate": Hz, "overflows": n}``."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise CsvIncomplete("empty")
    if not lines[0].startswith("Sample"):
        raise CsvError(f"no accelerometer CSV: {lines[0][:80]}")
    axes = lines[0].strip().split(",")[1:]
    if not axes:
        raise CsvError("no axes in the header")
    trailer = _TRAILER_RE.match(lines[-1])
    if trailer is None:
        last = lines[-1].split(",")
        if len(lines) > 1 and last[0].strip().isdigit():
            raise CsvIncomplete("no rate line yet")
        raise CsvError(lines[-1].strip()[:200])  # RRF's reason for a failed run
    samples = {axis: [] for axis in axes}
    for number, line in enumerate(lines[1:-1], start=2):
        values = line.split(",")
        if len(values) != len(axes) + 1:
            raise CsvError(f"line {number} has {len(values)} values")
        try:
            for axis, value in zip(axes, values[1:]):
                samples[axis].append(float(value))
        except ValueError:
            raise CsvError(f"line {number} is not numeric") from None
    if len(lines) < 3:
        raise CsvError("no samples")
    return {"axes": axes, "samples": samples, "rate": float(trailer.group(1)), "overflows": int(trailer.group(2))}


# --- DFT ------------------------------------------------------------------------------------------

def _fft_radix2(values):
    """In place, len(values) a power of two (iterative Cooley-Tukey, decimation in time)."""
    n = len(values)
    levels = n.bit_length() - 1
    for i in range(n):
        j = int(format(i, f"0{levels}b")[::-1], 2) if levels else 0
        if j > i:
            values[i], values[j] = values[j], values[i]
    size = 2
    while size <= n:
        half = size // 2
        step = cmath.exp(-2j * math.pi / size)
        twiddles = [step ** k for k in range(half)]
        for start in range(0, n, size):
            for k in range(half):
                a = values[start + k]
                b = values[start + k + half] * twiddles[k]
                values[start + k] = a + b
                values[start + k + half] = a - b
        size *= 2
    return values


def _fft_bluestein(values):
    """Any length, as a convolution of power-of-two length (Bluestein's chirp z-transform)."""
    n = len(values)
    m = 1
    while m < 2 * n - 1:
        m *= 2
    chirp = [cmath.exp(-1j * math.pi * ((k * k) % (2 * n)) / n) for k in range(n)]
    a = [values[k] * chirp[k] for k in range(n)] + [0j] * (m - n)
    b = [0j] * m
    b[0] = chirp[0].conjugate()
    for k in range(1, n):
        b[k] = b[m - k] = chirp[k].conjugate()
    _fft_radix2(a)
    _fft_radix2(b)
    c = [x * y for x, y in zip(a, b)]
    # inverse via conjugation
    c = [x.conjugate() for x in c]
    _fft_radix2(c)
    return [(c[k].conjugate() / m) * chirp[k] for k in range(n)]


def dft(values):
    """Discrete Fourier transform of a list of numbers, any length."""
    values = [complex(v) for v in values]
    n = len(values)
    if n <= 1:
        return values
    if n & (n - 1) == 0:
        return _fft_radix2(values)
    return _fft_bluestein(values)


def spectrum(samples, rate):
    """Frequencies (Hz) and amplitudes (g) as motionanalysis ``analyzeAccelerometerData(samples,
    rate, wideBand=true, applyWindow=true)``: k × rate/N for k = 1 … floor(N/2)."""
    n = len(samples)
    if n < 2 or rate <= 0:
        raise ValueError("too few samples for a spectrum")
    mean = sum(samples) / n
    windowed = [(v - mean) * (0.5 - 0.5 * math.cos(2 * math.pi * i / n)) for i, v in enumerate(samples)]
    transformed = dft(windowed)
    resolution = rate / n
    count = int(min(n / 2, (rate / 2) / resolution))
    scale = 4 / n
    freqs = [(k + 1) * resolution for k in range(count)]
    amplitudes = [scale * abs(transformed[k + 1]) for k in range(count)]
    return freqs, amplitudes


def analyse(parsed):
    """One spectrum per axis: ``{axis, sampling_rate, n_samples, freqs, amplitudes, peak_hz, rms}``."""
    out = []
    rate = parsed["rate"]
    for axis in parsed["axes"]:
        samples = parsed["samples"][axis]
        freqs, amplitudes = spectrum(samples, rate)
        candidates = [(a, f) for f, a in zip(freqs, amplitudes) if f >= PEAK_MIN_HZ]
        peak_hz = max(candidates)[1] if candidates else None
        mean = sum(samples) / len(samples)
        rms = math.sqrt(sum((v - mean) ** 2 for v in samples) / len(samples))
        out.append({"axis": axis, "sampling_rate": rate, "n_samples": len(samples),
                    "freqs": [round(f, 3) for f in freqs], "amplitudes": [round(a, 7) for a in amplitudes],
                    "peak_hz": round(peak_hz, 3) if peak_hz is not None else None, "rms": round(rms, 6)})
    return out


def board_of(port):
    """CAN address from a port as M955 C takes it (``60.i2c.lis`` → 60); 0 for the main board."""
    match = _BOARD_RE.match(port or "")
    return int(match.group(1)) if match else 0


def from_model(model):
    """``sensors.accelerometers`` as plain dicts (list index = M955/M956 P), None for an empty slot."""
    out = []
    for acc in getattr(getattr(model, "sensors", None), "accelerometers", None) or []:
        if acc is None:
            out.append(None)
            continue
        port = getattr(acc, "port", "") or ""
        out.append({"port": port, "board": board_of(port), "runs": getattr(acc, "runs", 0) or 0,
                    "points": getattr(acc, "points", 0) or 0, "samplingRate": getattr(acc, "sampling_rate", 0) or 0,
                    "resolution": getattr(acc, "resolution", 0) or 0})
    return out


def pick(accelerometers, board):
    """The accelerometer on CAN address ``board`` as ``(index, entry)``; None when ``board`` is None
    (no recordings) or that board has none."""
    if board is None:
        return None
    for index, entry in enumerate(accelerometers or []):
        if entry is not None and entry["board"] == board:
            return index, entry
    return None


def interpolate(freqs, amplitudes, grid):
    """``amplitudes`` over ``freqs`` linearly onto ``grid`` (both ascending), clamped at the ends."""
    out = []
    j = 0
    for f in grid:
        while j + 1 < len(freqs) and freqs[j + 1] < f:
            j += 1
        if f <= freqs[0]:
            out.append(amplitudes[0])
        elif j + 1 >= len(freqs):
            out.append(amplitudes[-1])
        else:
            f0, f1 = freqs[j], freqs[j + 1]
            t = (f - f0) / (f1 - f0) if f1 > f0 else 0.0
            out.append(amplitudes[j] * (1 - t) + amplitudes[j + 1] * t)
    return out


def median_spectrum(spectra):
    """Element-wise median of spectra on the grid of the first one (the measured rate differs a
    little from run to run, so the others are interpolated onto it)."""
    grid = spectra[0]["freqs"]
    columns = [spectra[0]["amplitudes"]] + [interpolate(s["freqs"], s["amplitudes"], grid) for s in spectra[1:]]
    amplitudes = []
    for values in zip(*columns):
        ordered = sorted(values)
        middle = len(ordered) // 2
        amplitudes.append(ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2)
    return grid, [round(a, 7) for a in amplitudes]


class Recorder:
    """The M956 worker. The collector calls ``observe`` with every model update, ``request`` when a
    recording is due; everything else happens in the recorder thread."""

    def __init__(self, writer, settings, send_code, resolve_path, on_event=None, sleep=time.sleep):
        self.writer = writer
        self.settings = settings
        self.send_code = send_code          # code -> reply text (the daemon's CommandConnection, locked)
        self.resolve_path = resolve_path    # virtual -> real path
        self.on_event = on_event or (lambda *_args: None)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._accelerometers = []
        self._queue = queue.Queue()
        self._pending = False
        self._jobs = {}                     # job key -> {axis: {"n", "peakSum", "peakN", "rmsSum", "peakMax"}}
        self._thread = None
        self._stopping = False
        self.last_recording = None
        self.last_error = None

    def cfg(self):
        return self.settings.current()["accelerometer"]

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="qa-accelerometer", daemon=True)
        self._thread.start()

    def stop(self, timeout=10):
        with self._lock:
            self._stopping = True
            self._changed.notify_all()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout)

    # --- collector thread ----------------------------------------------------------------------

    def observe(self, accelerometers):
        with self._lock:
            if accelerometers != self._accelerometers:
                self._accelerometers = accelerometers
                self._changed.notify_all()

    def choice(self):
        """The accelerometer the settings select, or None."""
        board = self.cfg()["board"]
        with self._lock:
            return pick(self._accelerometers, board)

    def busy(self):
        with self._lock:
            return self._pending

    def request(self, job_key, job_id, layer, index, runs_before, ts_ms):
        with self._lock:
            self._pending = True
        self._queue.put((job_key, job_id, layer, index, runs_before, ts_ms))

    def job_summary(self, job_key):
        """Per axis: number of spectra, mean and highest peak frequency, mean RMS (job summary
        ``mechanics``); None without spectra."""
        with self._lock:
            stats = self._jobs.pop(job_key, None)
        if not stats:
            return None
        return {axis: {"spectra": s["n"], "peakHzMean": round(s["peakSum"] / s["peakN"], 2) if s["peakN"] else None,
                       "peakHzMax": s["peakMax"], "rmsMean": round(s["rmsSum"] / s["n"], 6)}
                for axis, s in sorted(stats.items())}

    # --- recorder thread -----------------------------------------------------------------------

    def _loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._record(*item)
            except Exception as exc:  # noqa: BLE001
                logger.error("accelerometer recording failed: %s", exc)
                self._fail(item[0], item[5], "error", str(exc))
            finally:
                with self._lock:
                    self._pending = False

    def _fail(self, job_key, ts_ms, subtype, error, extra=None):
        self.last_error = f"{subtype}: {error}"
        logger.warning("accelerometer %s: %s", subtype, error)
        self.on_event(job_key, ts_ms, "accelerometer_failed", subtype, {"error": error, **(extra or {})})

    def _record(self, job_key, job_id, layer, index, runs_before, ts_ms):
        cfg = self.cfg()
        # M956 reads F into a String<StringLength50> (RRF Accelerometers.cpp): 42 characters here
        name = f"qa-{job_id}-{ts_ms // 1000}.csv"
        axes = "".join(a for a in "XYZ" if a in cfg["axes"])
        words = "" if axes in ("", "XYZ") else " " + " ".join(axes)  # no letters: all three (M956)
        code = f'M956 P{index} S{int(cfg["samples"])} A0{words} F"{name}"'
        reply = (self.send_code(code) or "").strip()
        if reply.startswith("Error"):
            self._fail(job_key, ts_ms, "start", reply, {"code": code})
            return
        with self._lock:
            entry = self._accelerometers[index] if index < len(self._accelerometers) else None
            rate = (entry or {}).get("samplingRate") or 400
        deadline = time.monotonic() + cfg["samples"] / rate + RUN_MARGIN_S
        with self._lock:
            while not self._stopping:
                entry = self._accelerometers[index] if index < len(self._accelerometers) else None
                if entry is not None and entry["runs"] > runs_before:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
            if self._stopping:
                return
        path = self.resolve_path(f"{ACCEL_DIR}/{name}")
        try:
            if entry is None or entry["runs"] <= runs_before:
                self._fail(job_key, ts_ms, "timeout", "the accelerometer did not finish the run", {"code": code})
                return
            parsed = self._read(path, entry["points"] == 0)
            if isinstance(parsed, str):
                self._fail(job_key, ts_ms, "run", parsed, {"points": entry["points"]})
                return
            if parsed["overflows"] > 0:
                self._fail(job_key, ts_ms, "overflow", f"{parsed['overflows']} overflows, samples lost",
                           {"overflows": parsed["overflows"]})
                return
            rows = []
            for spec in analyse(parsed):
                rows.append({"job_key": job_key, "ts_ms": ts_ms, "layer": layer, "board": entry["board"],
                             "source": entry["port"], **spec})
            self.writer.submit("spectra_insert", rows, urgent=True)
            self._account(job_key, rows)
            self.last_recording = ts_ms
            self.last_error = None
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _read(self, path, failed):
        """The parsed CSV, or the reason the run failed as a string."""
        reason = "no data file"
        for attempt in range(CSV_RETRIES):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    return parse_csv(handle.read())
            except CsvError as exc:
                return str(exc)
            except (CsvIncomplete, OSError, TypeError) as exc:
                reason = f"incomplete data file ({exc})" if isinstance(exc, CsvIncomplete) else "no data file"
                if failed and attempt >= 1:
                    break  # points 0: RRF gave up, do not wait for a file that stays incomplete
            self._sleep(CSV_RETRY_S)
        return "the run failed (points 0)" if failed and reason == "no data file" else reason

    def _account(self, job_key, rows):
        with self._lock:
            stats = self._jobs.setdefault(job_key, {})
            for row in rows:
                s = stats.setdefault(row["axis"], {"n": 0, "peakSum": 0.0, "peakN": 0, "rmsSum": 0.0, "peakMax": None})
                s["n"] += 1
                s["rmsSum"] += row["rms"]
                if row["peak_hz"] is not None:
                    s["peakSum"] += row["peak_hz"]
                    s["peakN"] += 1
                    s["peakMax"] = row["peak_hz"] if s["peakMax"] is None else max(s["peakMax"], row["peak_hz"])

    # --- API -----------------------------------------------------------------------------------

    def status(self):
        cfg = self.cfg()
        with self._lock:
            available = [{"index": i, "port": a["port"], "board": a["board"]}
                         for i, a in enumerate(self._accelerometers) if a is not None]
            chosen = pick(self._accelerometers, cfg["board"])
            pending = self._pending
        reason = None
        if cfg["board"] is None:
            reason = "no accelerometer selected (accelerometer.board)"
        elif chosen is None:
            reason = f"no accelerometer on board {cfg['board']} (M955)"
        return {
            "enabled": reason is None,
            "reason": reason,
            "accelerometer": {"index": chosen[0], "port": chosen[1]["port"], "board": chosen[1]["board"],
                              "samplingRate": chosen[1]["samplingRate"], "resolution": chosen[1]["resolution"]}
            if chosen else None,
            "available": available,
            "intervalMin": cfg["intervalMin"],
            "pending": pending,
            "lastRecording": self.last_recording,
            "lastError": self.last_error,
        }
