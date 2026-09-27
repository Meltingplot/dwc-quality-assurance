"""Collector: turns the object-model stream into jobs, samples, blocks, events and layers
(PLAN.md §5.4). Runs in the daemon's main thread; everything that touches the disk goes through
the database writer's queue.

Feed it ``update(model, patch, now_ms)`` for every patch (``patch`` = the raw patch dict, None for
the full model) and ``tick(now_ms)`` when the subscription was idle (3 s heartbeat).

Job lifecycle (dwc-vigil ``VigilTracker``, verified against DSF v3.7-dev and RRF 3.7-dev on
2026-09-26): a job runs while ``job.duration is not None``; ``job.file.fileName`` keeps the last
name on DSF 3.7. DSF sets ``lastFileCancelled/Aborted`` after the job-end patch, so the outcome
waits until they change or ``JOB_OUTCOME_GRACE_S`` passed. Simulations are ignored.
"""

import collections
import logging
import re
import statistics
import threading
import time
import zlib

import qa_accel
import qa_channels
import qa_context
import qa_db
import qa_heaterload
import qa_machine
import qa_summary

logger = logging.getLogger("qa.collector")

JOB_OUTCOME_GRACE_S = 10.0
PAUSE_CAUSE_WINDOW_MS = 10_000
ABORT_CAUSE_WINDOW_MS = 60_000
LIVE_SAMPLE_INTERVAL_MS = 1000
LIVE_HEARTBEAT_MS = 25_000
VIN_MEDIAN_WINDOW_MS = 90_000

PAUSED = ("pausing", "paused")
RESUMING = ("resuming",)

# Events that may explain a pause (§5.4 "Pause-Ursache")
PAUSE_CAUSE_TYPES = ("filament_status", "filament_percent_window", "mfm_error_tolerated", "mfm_recovery",
                     "mfm_flow_bias", "heater_fault", "heater_monitor", "heater_load", "driver_error")

# RRF event texts (Event::GetTextDescription, RepRapFirmware 3.7-dev @ 3638836, Platform/Event.cpp):
# "Driver <board>.<driver> error: ...", "... warning: ...", "... stall". RRF prints them only when
# no handler macro (driver-error.g etc.) exists (GCodes::ProcessEvent).
DRIVER_MESSAGE_RE = re.compile(r"Driver (\d+)(?:\.(\d+))? (error|warning|stall)\b(?::\s*(.*))?")

# StandardDriverStatus (Duet3D CANlib 3.7-dev, RRF3Common.h): bit masks and meanings
DRIVER_ERROR_MASK = 0b00_0100_1010_0011_1110
DRIVER_WARNING_MASK = 0b00_0010_0100_1100_0001
DRIVER_STALL_BIT = 1 << 8
DRIVER_BITS = ("over temperature warning", "over temperature shutdown", "phase A short to ground",
               "phase B short to ground", "phase A short to Vin", "phase B short to Vin",
               "phase A may be disconnected", "phase B may be disconnected", "motor stall",
               "external driver error", "closed loop position warning", "closed loop position not maintained",
               "closed loop not tuned", "closed loop tuning error", "closed loop illegal move", "reserved",
               "standstill", "driver not present")


def decode_driver_status(value):
    """Meanings of the set bits 0-17 (from CANlib's StandardDriverStatus, not the wiki)."""
    return [text for bit, text in enumerate(DRIVER_BITS) if value & (1 << bit)]


def job_id(started_ms, file_name, file_crc):
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(started_ms / 1000))
    digest = zlib.crc32(f"{file_name or ''}{file_crc or ''}".encode("utf-8")) & 0xFFFFFFFF
    return f"{stamp}-{digest:08x}"


class _Job:
    def __init__(self, key, id_, file_name, file_crc, started_ms, partial, start_layer, context):
        self.key = key
        self.id = id_
        self.file_name = file_name
        self.file_crc = file_crc
        self.started_ms = started_ms
        self.partial = partial
        self.start_layer = start_layer
        self.context = context or {}
        self.layer = None
        self.layer_acc = None
        self.job_acc = None
        self.events = []          # light copies for the summary: type, subtype, layer, ts_ms, payload
        self.pause = None         # {"event": id, "since": ms}
        self.pause_s = 0.0
        self.last_pause_cause = None
        self.last_warmup = None
        self.last_pause = None
        self.last_duration = None
        self.layers_written = 0
        self.last_accel_ms = None   # last M956 request


class Collector:
    def __init__(self, writer, settings, resolve_path=None, broadcast=None, plugin_version="unknown",
                 index_cache=None, timelapse=None, accel=None, on_status=None):
        self.writer = writer
        self.settings = settings
        self.resolve_path = resolve_path or (lambda _virtual: None)
        self.broadcast = broadcast or (lambda _frame: None)
        self.plugin_version = plugin_version
        self.index_cache = index_cache
        self.timelapse = timelapse
        self.accel = accel
        self.on_status = on_status or (lambda _status: None)

        cfg = settings.current()
        self.load = qa_heaterload.HeaterLoadTracker(cfg)
        self.mfm = qa_machine.MfmWatcher()
        self.job = None
        self.last_job_id = None
        self._first = True
        self._prev_active = None
        self._prev_status = None
        self._job_flags = None
        self._last_flags = (False, False)
        self._pending_end = None
        self._simulating = False

        self._ring = collections.deque()
        self._current = None        # snapshot of the patch being processed
        self._prev_snapshot = None
        self._prev_ms = None
        self._last_coarse_ms = None
        self._last_live_ms = 0
        self._fine_until = None
        self._block = None          # {"id", "start", "triggers"}
        self._last_fine = {}        # channel -> last value written fine

        self._next_event_id = 1
        self._next_block_id = 1
        self._id_lock = threading.Lock()
        self._ongoing = {}          # key -> event dict (ongoing events that get an end)
        self._setpoints = None
        self._heater_states = {}
        self._monitor_violations = set()
        self._driver_status = {}
        self._phantom = {}          # channel -> {"ts", "before", "peak"}
        self._vin = {}              # board -> deque[(ts, value)]
        self._vin_low = set()
        self._fm_status = {}
        self._fm_window = {}        # monitor -> {"since": ms, "event": id or None}
        self.model = None

    # --- helpers ---------------------------------------------------------------------------

    def cfg(self):
        return self.settings.current()

    def _alloc_event_id(self):
        with self._id_lock:  # the timelapse threads write events too (external_event)
            eid = self._next_event_id
            self._next_event_id += 1
            return eid

    def _alloc_block_id(self):
        bid = self._next_block_id
        self._next_block_id += 1
        return bid

    def init_ids(self):
        event, block = self.writer.call("next_ids")
        self._next_event_id = event + 1
        self._next_block_id = block + 1

    def status(self):
        return {
            "state": "recording" if self.job else "idle",
            "currentJobId": self.job.id if self.job else None,
            "lastJobId": self.last_job_id,
            "layer": self.job.layer if self.job else None,
            "fineUntil": self._fine_until,
            "pendingEnd": self._pending_end is not None,
        }

    # --- entry points ----------------------------------------------------------------------

    def update(self, model, patch, now_ms):
        """Process the model after a patch (``patch``: the raw patch dict, or None)."""
        self.model = model
        cfg = self.cfg()
        snapshot = qa_channels.extract(model)
        state = getattr(model, "state", None)
        status = qa_channels.enum_value(getattr(state, "status", None))

        self._advance(now_ms)
        self._current = snapshot
        self._ring.append((now_ms, snapshot))
        horizon = now_ms - cfg["ringBufferS"] * 1000
        while self._ring and self._ring[0][0] < horizon:
            self._ring.popleft()

        self._lifecycle(model, status, now_ms)
        if self.accel is not None:
            self.accel.observe(qa_accel.from_model(model))
        if self.job is not None and not self._simulating:
            self._track_layer(model, snapshot, now_ms)
            self._accelerometer(status, now_ms)
            if self.job.job_acc is not None:
                self.job.job_acc.heater_setpoints(snapshot, now_ms)
            self._detect(model, patch, snapshot, status, now_ms)
            self._jump_triggers(snapshot, now_ms)
            self._write_fine(snapshot, now_ms)
            self._write_coarse(snapshot, now_ms)
            self._live(snapshot, now_ms)
        self._prev_status = status
        self._prev_snapshot = snapshot
        self._prev_ms = now_ms

    def tick(self, now_ms):
        """No patch for a while: time still passes."""
        self.resolve_pending_end(now_ms)
        if self.model is None or self._prev_snapshot is None:
            return
        self._advance(now_ms)
        self._prev_ms = now_ms
        if self.job is not None and not self._simulating:
            status = qa_channels.enum_value(getattr(getattr(self.model, "state", None), "status", None))
            self._heater_load(self.model, status, now_ms)
            self._fm_window_check(self.model, now_ms)
            self._close_block_if_due(now_ms)
            self._accelerometer(status, now_ms)
            self._write_coarse(self._prev_snapshot, now_ms)
            self._live(self._prev_snapshot, now_ms)
        elif now_ms - self._last_live_ms >= LIVE_HEARTBEAT_MS:
            self._last_live_ms = now_ms
            self.broadcast({"type": "status", "ts": now_ms, **self.status()})

    def shutdown(self, now_ms):
        """Daemon stops: keep the running job 'running' (it is resumed on the next start), flush."""
        self.resolve_pending_end(now_ms, force=True)
        if self.job is not None:
            self._flush_layer_partial(now_ms)
            self._close_block(now_ms)

    # --- time-weighted accumulation ----------------------------------------------------------

    def _advance(self, now_ms):
        if self.job is None or self._prev_snapshot is None or self._prev_ms is None:
            return
        dt = (now_ms - self._prev_ms) / 1000.0
        if dt <= 0:
            return
        if self.job.layer_acc is not None:
            self.job.layer_acc.advance(self._prev_snapshot, dt)
        if self.job.job_acc is not None:
            self.job.job_acc.advance(self._prev_snapshot, dt, now_ms)

    # --- lifecycle ---------------------------------------------------------------------------

    def _lifecycle(self, model, status, now_ms):
        job = getattr(model, "job", None)
        active = job is not None and getattr(job, "duration", None) is not None
        flags = (bool(getattr(job, "last_file_cancelled", False)), bool(getattr(job, "last_file_aborted", False)))
        self._last_flags = flags

        if self._first:
            self._first = False
            self._resume(model, active, status, now_ms)
        elif active and not self._prev_active:
            self.resolve_pending_end(now_ms, force=True)
            if status == "simulating":
                self._simulating = True
            else:
                self._simulating = False
                self._start_job(model, now_ms, partial=False)
        if active:
            self._job_flags = flags
            if self.job is not None:
                self.job.last_warmup = getattr(job, "warm_up_duration", None)
                self.job.last_pause = getattr(job, "pause_duration", None)
                self.job.last_duration = getattr(job, "duration", None)
        if self._prev_active and not active:
            if self._simulating:
                self._simulating = False
            elif self.job is not None:
                self._pending_end = {"since": time.monotonic(), "flags": self._job_flags, "ms": now_ms}
                self._finish_layer(model, now_ms)
            self._job_flags = None
        if self.job is not None and not self._simulating:
            self._pause_resume(model, status, now_ms)
        self.resolve_pending_end(now_ms)
        self._prev_active = active

    def _resume(self, model, active, status, now_ms):
        """First model after the daemon started."""
        try:
            running = self.writer.call("running_jobs")
        except Exception as exc:  # noqa: BLE001
            logger.error("reading running jobs failed: %s", exc)
            running = []
        current = None
        if active and status != "simulating":
            file_name = getattr(getattr(model.job, "file", None), "file_name", None)
            path = self._real_path(file_name)
            crc = self._crc(path)
            for row in running:
                if row["file_name"] == file_name and (row["file_crc32"] == crc or crc is None):
                    current = row
                    break
            if current is not None:
                gap = self.writer.call("last_sample_ms", current["key"])
                self.job = _Job(current["key"], current["id"], file_name, current["file_crc32"],
                                current["started_at"], bool(current["partial"]), current["start_layer"], {})
                self.job.context = self._load_context(current["key"])
                self._begin_accumulators(model, now_ms)
                self.last_job_id = self.job.id
                self._event(model, now_ms, "daemon_started_mid_job", "resumed",
                            payload={"resumed": True, "lastRecordMs": gap})
                if self.timelapse is not None:
                    self.timelapse.job_started(self.job)
                self.on_status(self.status())
            else:
                self._start_job(model, now_ms, partial=True)
        elif active:
            self._simulating = True
        for row in running:
            if current is None or row["key"] != current["key"]:
                last = self.writer.call("last_sample_ms", row["key"])
                self.writer.submit("job_update", row["key"], {"result": "unknown", "ended_at": last}, urgent=True)
        if self.timelapse is not None:
            self.timelapse.recover(self.job.key if self.job is not None else None)

    def _load_context(self, job_key):
        try:
            return self.writer.call("job_context", job_key) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _real_path(self, virtual):
        if not virtual:
            return None
        try:
            return self.resolve_path(virtual)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cannot resolve %s: %s", virtual, exc)
            return None

    @staticmethod
    def _crc(path):
        if not path:
            return None
        try:
            return qa_context.file_crc32(path)
        except OSError:
            return None

    def _start_job(self, model, now_ms, partial):
        cfg = self.cfg()
        job_model = getattr(model, "job", None)
        file_name = getattr(getattr(job_model, "file", None), "file_name", None)
        path = self._real_path(file_name)
        crc = self._crc(path)
        context = qa_context.snapshot(model, cfg, self.plugin_version, path, crc)
        start_layer = getattr(job_model, "layer", None) if partial else None
        jid = job_id(now_ms, file_name, crc)
        record = {"id": jid, "file_name": file_name, "file_crc32": crc, "started_at": now_ms, "result": "running",
                  "partial": partial, "start_layer": start_layer,
                  "num_layers": context.get("file", {}).get("numLayers"), "material": context.get("material"),
                  "context": context}
        key = self.writer.call("job_insert", record)
        self.job = _Job(key, jid, file_name, crc, now_ms, partial, start_layer, context)
        self.last_job_id = jid
        self.load.reset_job()
        self.load.configure(cfg)
        self.mfm.reset()
        self.mfm.update(getattr(model, "globals", None))
        self._reset_detectors(model)
        self._begin_accumulators(model, now_ms)
        if partial:
            self._event(model, now_ms, "daemon_started_mid_job", "new", payload={"resumed": False, "layer": start_layer})
        self._event(model, now_ms, "job_start", None, payload={"file": file_name, "crc32": crc, "partial": partial})
        if self.index_cache is not None and path and crc:
            self.index_cache.ensure(path, crc)
        if self.timelapse is not None:
            self.timelapse.job_started(self.job)
        self.on_status(self.status())

    def _begin_accumulators(self, model, now_ms):
        cfg = self.cfg()
        snapshot = qa_channels.extract(model)
        diameters = {e["index"]: e.get("filamentDiameter") for e in self.job.context.get("extruders", [])} or {
            i: qa_channels.number(getattr(e, "filament_diameter", None))
            for i, e in qa_channels.items(getattr(getattr(model, "move", None), "extruders", None))}
        self._diameters = diameters
        self._chamber = qa_channels.chamber_channel(model, cfg)
        self._sensor_names = {i: getattr(s, "name", None)
                              for i, s in qa_channels.items(getattr(getattr(model, "sensors", None), "analog", None))}
        self.job.job_acc = qa_summary.JobAccumulator(snapshot, diameters, self._chamber)
        layer = getattr(getattr(model, "job", None), "layer", None)
        self.job.layer = layer
        self.job.layer_acc = qa_summary.LayerAccumulator(layer, now_ms, snapshot, diameters, self._chamber,
                                                         self._sensor_names) if layer else None
        self._last_coarse_ms = None

    def resolve_pending_end(self, now_ms, force=False):
        pending = self._pending_end
        if pending is None:
            return
        changed = pending["flags"] is None or self._last_flags != pending["flags"]
        expired = time.monotonic() - pending["since"] >= JOB_OUTCOME_GRACE_S
        if not (force or changed or expired):
            return
        self._pending_end = None
        cancelled, aborted = self._last_flags
        result = "aborted" if aborted else ("cancelled" if cancelled else "completed")
        self._end_job(result, pending["ms"])

    def _end_job(self, result, ended_ms):
        job = self.job
        if job is None:
            return
        model = self.model
        cfg = self.cfg()
        self._close_ongoing(ended_ms)
        reason = None
        if result != "completed":
            reason = self._abort_reason(ended_ms)
        self._event(model, ended_ms, "job_end", result, payload={"result": result, "abortReason": reason})
        self._close_block(ended_ms)
        globals_end = qa_machine.context_globals(getattr(model, "globals", None), cfg.get("contextGlobals", []))
        summary = self._summary(result, reason, globals_end)
        context = dict(job.context)
        context["globalsEnd"] = globals_end
        self.writer.submit("job_update", job.key, {
            "ended_at": ended_ms, "result": result,
            "duration_s": job.last_duration if job.last_duration is not None else round((ended_ms - job.started_ms) / 1000, 1),
            "warmup_s": job.last_warmup, "pause_s": job.last_pause if job.last_pause is not None else round(job.pause_s, 1),
            "summary": summary, "context": context,
        }, urgent=True)
        self.writer.submit("backup", urgent=True)
        if self.timelapse is not None:
            self.timelapse.job_finished(job, result)
        self.broadcast({"type": "job", "ts": ended_ms, "event": "end", "jobId": job.id, "result": result})
        self.job = None
        self.load.reset_job()
        self.on_status(self.status())

    def _abort_reason(self, ended_ms):
        job = self.job
        if job.last_pause_cause and job.last_pause_cause != "user":
            return job.last_pause_cause
        for event in reversed(job.events):
            if ended_ms - event["ts_ms"] > ABORT_CAUSE_WINDOW_MS:
                break
            if event["type"] in PAUSE_CAUSE_TYPES:
                return event["type"]
        return job.last_pause_cause or "user"

    def _summary(self, result, reason, globals_end):
        job = self.job
        acc = job.job_acc
        nozzles = qa_channels.nozzle_heaters(self.model) if self.model is not None else {}
        return {
            "result": result,
            "abortReason": reason,
            "layers": job.layers_written,
            "filament": acc.filament_totals() if acc else {},
            "thermal": {**(acc.thermal() if acc else {}), "heaterLoad": self.load.job_stats(nozzles)},
            "mfm": qa_summary.mfm_summary(job.events, globals_end),
            "events": qa_summary.event_summary(job.events),
            "spoolUsageG": qa_summary.spool_usage(job.context.get("globalsStart"), globals_end),
            "mechanics": self.accel.job_summary(job.key) if self.accel is not None else None,
        }

    def _accelerometer(self, status, now_ms):
        """A spectrum every ``accelerometer.intervalMin`` while the job prints (not paused), from
        layer 2 on, one recording at a time (qa_accel)."""
        job = self.job
        if self.accel is None or status != "processing" or (job.layer or 0) < 2:
            return
        interval_ms = self.cfg()["accelerometer"]["intervalMin"] * 60_000
        if job.last_accel_ms is not None and now_ms - job.last_accel_ms < interval_ms:
            return
        choice = self.accel.choice()
        if choice is None or self.accel.busy():
            return
        job.last_accel_ms = now_ms
        index, entry = choice
        self.accel.request(job.key, job.id, job.layer, index, entry["runs"], now_ms)

    def _pause_resume(self, model, status, now_ms):
        prev = self._prev_status
        job = self.job
        if status in PAUSED and prev not in PAUSED and job.pause is None:
            cause = self._pause_cause(model, now_ms)
            job.last_pause_cause = cause
            eid = self._event(model, now_ms, "pause", cause, payload={"cause": cause})
            # a pause starts the heater load over: _heater_load sees status != processing
            job.pause = {"event": eid, "since": now_ms}
        elif job.pause is not None and status not in PAUSED and status is not None:
            paused_s = round((now_ms - job.pause["since"]) / 1000.0, 1)
            job.pause_s += paused_s
            self._event(model, now_ms, "resume", None, payload={"pausedS": paused_s, "pauseEvent": job.pause["event"]})
            self.writer.submit("event_update", job.pause["event"], {"end_ms": now_ms}, urgent=True)
            job.pause = None

    def _pause_cause(self, model, now_ms):
        if self.mfm.recovery_requested():
            return "filament_monitor"
        for event in reversed(self.job.events):
            if now_ms - event["ts_ms"] > PAUSE_CAUSE_WINDOW_MS:
                break
            if event["type"] in PAUSE_CAUSE_TYPES:
                return event["type"]
        box = getattr(getattr(model, "state", None), "message_box", None)
        title = getattr(box, "title", None) if box is not None else None
        if title:
            return f"message: {title}"
        return "user"

    # --- layers ----------------------------------------------------------------------------

    def _track_layer(self, model, snapshot, now_ms):
        layer = getattr(getattr(model, "job", None), "layer", None)
        if layer == self.job.layer:
            return
        if self.job.layer is not None and self.job.layer_acc is not None:
            self._finish_layer(model, now_ms)
        self.job.layer = layer
        self.load.reset_layer()
        if layer is not None:
            self.job.layer_acc = qa_summary.LayerAccumulator(layer, now_ms, snapshot, self._diameters,
                                                             self._chamber, self._sensor_names)
            self.broadcast({"type": "layer", "ts": now_ms, "jobId": self.job.id, "layer": layer})
            if self.timelapse is not None:
                self.timelapse.layer_changed(self.job, layer, now_ms)
        else:
            self.job.layer_acc = None

    def _finish_layer(self, model, now_ms):
        acc = self.job.layer_acc if self.job is not None else None
        if acc is None:
            return
        job_model = getattr(model, "job", None)
        height = None
        fraction = None
        layers = getattr(job_model, "layers", None) or []
        # job.layers[] holds the finished layers (DSF UpdateService.cs); index layer-1 once DSF added it
        if acc.layer and 0 < acc.layer <= len(layers) and layers[acc.layer - 1] is not None:
            height = qa_channels.number(getattr(layers[acc.layer - 1], "height", None))
            fraction = qa_channels.number(getattr(layers[acc.layer - 1], "fraction_printed", None))
        # the values at the moment of the change end the layer (the next one starts with them)
        last = self._current if self._current is not None else self._prev_snapshot
        z = None if acc.print_z is not None else (
            self._prev_snapshot.get("axis.Z.machinePosition") if self._prev_snapshot else None)
        if fraction is None:
            size = getattr(getattr(job_model, "file", None), "size", None)
            pos = getattr(job_model, "file_position", None)
            if size and pos is not None:
                fraction = round(pos / size, 4)
        record = acc.finish(now_ms, last, height=height, z=z, fraction_printed=fraction,
                            load_stats=self.load.layer_stats(acc.span_s))
        self.writer.submit("layer_upsert", self.job.key, record)
        self.job.layers_written += 1
        self.job.layer_acc = None
        self.broadcast({"type": "layer", "ts": now_ms, "jobId": self.job.id, "layer": acc.layer, "finished": True,
                        "durationS": record["duration_s"]})

    def _flush_layer_partial(self, now_ms):
        """Store the layer in progress on shutdown; it is overwritten when the job resumes."""
        acc = self.job.layer_acc
        if acc is None:
            return
        record = acc.finish(now_ms, self._prev_snapshot, load_stats=self.load.layer_stats(acc.span_s))
        record["partial"] = True
        self.writer.submit("layer_upsert", self.job.key, record)

    # --- events ----------------------------------------------------------------------------

    def _event(self, model, ts_ms, type_, subtype=None, payload=None, device=None, trigger_block=True):
        job = self.job
        if job is None:
            return None
        eid = self._alloc_event_id()
        pos, workplace, offsets = qa_channels.positions(model) if model is not None else ({}, None, {})
        block_id = self._trigger(ts_ms, type_) if trigger_block else None
        event = {
            "id": eid, "job_key": job.key, "ts_ms": ts_ms, "type": type_, "subtype": subtype,
            "layer": job.layer, "x": pos.get("X"), "y": pos.get("Y"), "z": pos.get("Z"), "positions": pos,
            "workplace": workplace, "offsets": offsets,
            "tool": qa_channels.current_tool(model) if model is not None else None,
            "object_id": qa_channels.current_object(model) if model is not None else None,
            "device": device, "payload": payload, "block_id": block_id,
        }
        self.writer.submit("event_insert", event, urgent=True)
        job.events.append({"id": eid, "type": type_, "subtype": subtype, "layer": job.layer, "ts_ms": ts_ms,
                           "payload": payload})
        self.broadcast({"type": "event", "ts": ts_ms, "jobId": job.id, "event": {
            k: event[k] for k in ("id", "type", "subtype", "layer", "x", "y", "z", "tool", "object_id", "device", "payload")}})
        return eid

    def external_event(self, job_key, ts_ms, type_, subtype=None, payload=None):
        """An event another thread reports (timelapse), possibly after its job ended: no model
        access, so no position, tool or object."""
        eid = self._alloc_event_id()
        job = self.job if self.job is not None and self.job.key == job_key else None
        layer = job.layer if job is not None else None
        event = {"id": eid, "job_key": job_key, "ts_ms": ts_ms, "type": type_, "subtype": subtype, "layer": layer,
                 "x": None, "y": None, "z": None, "positions": None, "workplace": None, "offsets": None,
                 "tool": None, "object_id": None, "device": None, "payload": payload, "block_id": None}
        self.writer.submit("event_insert", event, urgent=True)
        self.broadcast({"type": "event", "ts": ts_ms, "jobId": job.id if job is not None else None, "event": {
            k: event[k] for k in ("id", "type", "subtype", "layer", "x", "y", "z", "tool", "object_id", "device", "payload")}})
        return eid

    def _end_event(self, key, ts_ms, extra=None):
        ongoing = self._ongoing.pop(key, None)
        if ongoing is None:
            return
        payload = dict(ongoing.get("payload") or {})
        payload.update(extra or {})
        payload["durationS"] = round((ts_ms - ongoing["ts_ms"]) / 1000.0, 1)
        self.writer.submit("event_update", ongoing["id"], {"end_ms": ts_ms, "payload": payload}, urgent=True)

    def _close_ongoing(self, ts_ms):
        for key in list(self._ongoing):
            self._end_event(key, ts_ms, {"closedBy": "job_end"})

    def _reset_detectors(self, model):
        self._ongoing = {}
        self._setpoints = self._read_setpoints(model)
        self._heater_states = {}
        self._monitor_violations = set()
        self._driver_status = self._read_driver_status(model)
        self._phantom = {}
        self._fm_status = {}
        self._fm_window = {}
        self._vin_low = set()

    def _detect(self, model, patch, snapshot, status, now_ms):
        self._heater_events(model, now_ms)
        self._heater_load(model, status, now_ms)
        self._filament_events(model, now_ms)
        self._fm_window_check(model, now_ms)
        self._mfm_events(model, now_ms)
        self._setpoint_events(model, now_ms)
        self._driver_events(model, patch, now_ms)
        self._voltage_events(model, snapshot, now_ms)
        self._phantom_events(snapshot, now_ms)

    def _heater_events(self, model, now_ms):
        heat = getattr(model, "heat", None)
        analog = getattr(getattr(model, "sensors", None), "analog", None) or []
        for i, heater in qa_channels.items(getattr(heat, "heaters", None)):
            state = qa_channels.enum_value(getattr(heater, "state", None))
            prev = self._heater_states.get(i)
            self._heater_states[i] = state
            if state == "fault" and prev is not None and prev != "fault":
                self._event(model, now_ms, "heater_fault", None, device=i,
                            payload={"heater": i, "previousState": prev, "current": getattr(heater, "current", None)})
            for m, monitor in enumerate(getattr(heater, "monitors", None) or []):
                if monitor is None:
                    continue
                condition = qa_channels.enum_value(getattr(monitor, "condition", None))
                limit = getattr(monitor, "limit", None)
                sensor = getattr(monitor, "sensor", None)
                reading = None
                if isinstance(sensor, int) and 0 <= sensor < len(analog) and analog[sensor] is not None:
                    reading = getattr(analog[sensor], "last_reading", None)
                elif sensor in (None, -1):
                    reading = getattr(heater, "current", None)
                violated = (limit is not None and reading is not None and
                            ((condition == "tooHigh" and reading > limit) or (condition == "tooLow" and reading < limit)))
                key = (i, m)
                if violated and key not in self._monitor_violations:
                    self._monitor_violations.add(key)
                    self._event(model, now_ms, "heater_monitor", condition, device=i,
                                payload={"heater": i, "monitor": m, "limit": limit, "reading": reading,
                                         "sensor": sensor, "action": qa_channels.enum_value(getattr(monitor, "action", None))})
                elif not violated:
                    self._monitor_violations.discard(key)

    def _heater_load(self, model, status, now_ms):
        heaters = {}
        for i, heater in qa_channels.items(getattr(getattr(model, "heat", None), "heaters", None)):
            heaters[i] = (qa_channels.enum_value(getattr(heater, "state", None)), getattr(heater, "active", None),
                          getattr(heater, "current", None), getattr(heater, "avg_pwm", None),
                          getattr(getattr(heater, "model", None), "max_pwm", None))
        nozzles = qa_channels.nozzle_heaters(model)
        transitions = self.load.update(now_ms / 1000.0, heaters, status == "processing", nozzles,
                                       self.job.layer if self.job else None)
        for kind, heater, info in transitions:
            key = ("heater_load", heater)
            if kind == "start":
                payload = self._load_payload(heater, info)
                eid = self._event(model, now_ms, "heater_load", info["level"], device=heater, payload=payload)
                self._ongoing[key] = {"id": eid, "ts_ms": now_ms, "payload": payload}
            elif kind == "change" and key in self._ongoing:
                ongoing = self._ongoing[key]
                payload = ongoing["payload"]
                payload.setdefault("levels", []).append({"ts": now_ms, "level": info["level"]})
                if info["level"] == "limit":
                    payload["level"] = "limit"
                payload["peakMean"] = max(payload.get("peakMean") or 0, info.get("mean") or 0)
                self.writer.submit("event_update", ongoing["id"], {"subtype": payload["level"], "payload": payload},
                                   urgent=True)
            elif kind == "end":
                self._end_event(key, now_ms)
        # the peak of an ongoing load event follows the mean
        for heater, mean in self.load.means().items():
            ongoing = self._ongoing.get(("heater_load", heater))
            if ongoing is not None and mean > (ongoing["payload"].get("peakMean") or 0):
                ongoing["payload"]["peakMean"] = round(mean, 4)

    def _load_payload(self, heater, info):
        snap = self._prev_snapshot or {}
        rate = snap.get("move.currentMove.extrusionRate")
        diameter = self._diameters.get(0) if hasattr(self, "_diameters") else None
        return {"heater": heater, "tool": info.get("tool"), "setpoint": info.get("setpoint"),
                "level": info["level"], "peakMean": round(info.get("mean") or 0, 4),
                "levels": [{"ts": None, "level": info["level"]}],
                "volumetricFlow": None if rate is None else round(rate * qa_summary.cross_section(diameter), 3),
                "speedFactor": snap.get("move.speedFactor")}

    def _filament_events(self, model, now_ms):
        for i, fm in qa_channels.items(getattr(getattr(model, "sensors", None), "filament_monitors", None)):
            status = qa_channels.enum_value(getattr(fm, "status", None))
            prev = self._fm_status.get(i)
            self._fm_status[i] = status
            if prev is None or status == prev:
                continue
            key = ("filament_status", i)
            if prev == "ok" and status != "ok":
                payload = {"monitor": i, "status": status, "from": prev,
                           "lastPercentage": getattr(fm, "last_percentage", None)}
                eid = self._event(model, now_ms, "filament_status", status, device=i, payload=payload)
                self._ongoing[key] = {"id": eid, "ts_ms": now_ms, "payload": payload}
            elif status == "ok":
                self._end_event(key, now_ms, {"returnedTo": "ok"})

    def _fm_window_check(self, model, now_ms):
        min_s = self.cfg()["filamentPercentWindowMinS"]
        for i, fm in qa_channels.items(getattr(getattr(model, "sensors", None), "filament_monitors", None)):
            configured = getattr(fm, "configured", None)
            low = getattr(configured, "percent_min", None) if configured is not None else None
            high = getattr(configured, "percent_max", None) if configured is not None else None
            pct = getattr(fm, "last_percentage", None)
            status = qa_channels.enum_value(getattr(fm, "status", None))
            outside = (pct is not None and low is not None and high is not None and status == "ok"
                       and (pct < low or pct > high))
            window = self._fm_window.get(i)
            key = ("filament_percent_window", i)
            if outside:
                if window is None:
                    window = self._fm_window[i] = {"since": now_ms, "event": None, "extreme": pct}
                window["extreme"] = pct if abs(pct - (low + high) / 2) > abs(window["extreme"] - (low + high) / 2) else window["extreme"]
                if window["event"] is None and now_ms - window["since"] >= min_s * 1000:
                    payload = {"monitor": i, "since": window["since"], "percentMin": low, "percentMax": high,
                               "lastPercentage": pct, "side": "low" if pct < low else "high"}
                    eid = self._event(model, now_ms, "filament_percent_window", payload["side"], device=i, payload=payload)
                    window["event"] = eid
                    self._ongoing[key] = {"id": eid, "ts_ms": window["since"], "payload": payload}
            elif window is not None:
                if window["event"] is not None:
                    self._end_event(key, now_ms, {"extreme": window["extreme"]})
                del self._fm_window[i]

    def _mfm_events(self, model, now_ms):
        if not self.cfg().get("machineSignals", {}).get("mfm", True):
            return
        for type_, subtype, payload in self.mfm.update(getattr(model, "globals", None)):
            self._event(model, now_ms, type_, subtype, payload=payload)

    @staticmethod
    def _read_setpoints(model):
        values = {}
        for i, heater in qa_channels.items(getattr(getattr(model, "heat", None), "heaters", None)):
            values[("heater.active", i)] = qa_channels.number(getattr(heater, "active", None))
            values[("heater.standby", i)] = qa_channels.number(getattr(heater, "standby", None))
            values[("heater.maxPwm", i)] = qa_channels.number(getattr(getattr(heater, "model", None), "max_pwm", None))
        for i, ext in qa_channels.items(getattr(getattr(model, "move", None), "extruders", None)):
            values[("stepsPerMm", i)] = qa_channels.number(getattr(ext, "steps_per_mm", None))
            pa = getattr(ext, "press_adv", None)
            for name in ("k0", "k1", "d"):
                values[(f"pressAdv.{name}", i)] = qa_channels.number(getattr(pa, name, None)) if pa is not None else None
            nl = getattr(ext, "nonlinear", None)
            for prop, name in (("a", "a"), ("b", "b"), ("upper_limit", "upperLimit")):
                values[(f"nonlinear.{name}", i)] = qa_channels.number(getattr(nl, prop, None)) if nl is not None else None
        for i, fm in qa_channels.items(getattr(getattr(model, "sensors", None), "filament_monitors", None)):
            configured, calibrated = qa_context.filament_config(fm)
            values[("fm.configured", i)] = configured
            values[("fm.calibrated", i)] = calibrated is not None
        for axis in getattr(getattr(model, "move", None), "axes", None) or []:
            if qa_channels.axis_letter(axis) == "Z":
                values[("babystep", "Z")] = qa_channels.number(getattr(axis, "babystep", None))
        return values

    def _setpoint_events(self, model, now_ms):
        new = self._read_setpoints(model)
        old = self._setpoints or {}
        self._setpoints = new
        for key, value in new.items():
            if key not in old or old[key] == value:
                continue
            name, index = key
            before = old[key]
            if name == "babystep":
                self._event(model, now_ms, "babystep", None, payload={"axis": "Z", "from": before, "to": value})
                continue
            payload = {"setpoint": name, "index": index, "from": before, "to": value}
            if name == "stepsPerMm":
                payload["cause"] = qa_machine.steps_cause(value, getattr(model, "globals", None))
            self._event(model, now_ms, "setpoint_change", name, device=index if isinstance(index, int) else None,
                        payload=payload)

    @staticmethod
    def _read_driver_status(model):
        values = {}
        for b, board in qa_channels.items(getattr(model, "boards", None)):
            for d, driver in enumerate(getattr(board, "drivers", None) or []):
                if driver is not None:
                    values[(b, d)] = getattr(driver, "status", None)
        return values

    def _driver_events(self, model, patch, now_ms):
        # Main board and any board that reports its status: new error/warning/stall bits
        new = self._read_driver_status(model)
        boards = getattr(model, "boards", None) or []
        for key, value in new.items():
            prev = self._driver_status.get(key)
            if value is None or prev is None or value == prev:
                continue
            added = value & ~prev
            kind = "error" if added & DRIVER_ERROR_MASK else ("warning" if added & DRIVER_WARNING_MASK else (
                "stall" if added & DRIVER_STALL_BIT else None))
            if kind is None:
                continue
            b, d = key
            board = boards[b] if b < len(boards) else None
            self._event(model, now_ms, "driver_error", kind, device=d, payload={
                "source": "status", "board": b, "canAddress": getattr(board, "can_address", None),
                "driver": d, "status": value, "previous": prev, "bits": decode_driver_status(added)})
        self._driver_status = new
        # Expansion boards do not report status changes by themselves (wiki CAN_limitations.md);
        # RRF's event text reaches messages[] when no handler macro exists
        for message in (patch or {}).get("messages") or []:
            content = message.get("content") if isinstance(message, dict) else None
            match = DRIVER_MESSAGE_RE.search(content or "")
            if not match:
                continue
            board_address = int(match.group(1)) if match.group(2) is not None else None
            driver = int(match.group(2) if match.group(2) is not None else match.group(1))
            self._event(model, now_ms, "driver_error", match.group(3), device=driver, payload={
                "source": "message", "canAddress": board_address, "driver": driver,
                "text": content.strip()[:200]})

    def _voltage_events(self, model, snapshot, now_ms):
        threshold = self.cfg()["thresholds"]["vInPercent"] / 100.0
        for name, value in snapshot.items():
            if not qa_channels.is_vin(name):
                continue
            board = int(name.split(".")[1])
            history = self._vin.setdefault(board, collections.deque())
            history.append((now_ms, value))
            while history and history[0][0] < now_ms - VIN_MEDIAN_WINDOW_MS:
                history.popleft()
            if len(history) < 5:
                continue
            median = statistics.median(v for _, v in history)
            low = value < median * (1 - threshold)
            key = ("voltage_dip", board)
            if low and board not in self._vin_low:
                self._vin_low.add(board)
                payload = {"board": board, "vIn": value, "median90s": round(median, 3)}
                eid = self._event(model, now_ms, "voltage_dip", None, device=board, payload=payload)
                self._ongoing[key] = {"id": eid, "ts_ms": now_ms, "payload": payload}
            elif not low and board in self._vin_low:
                self._vin_low.discard(board)
                self._end_event(key, now_ms, {"recoveredTo": value})

    def _phantom_events(self, snapshot, now_ms):
        cfg = self.cfg()["thresholds"]
        jump = cfg["phantomJumpK"]
        back = cfg["temperatureK"]
        window_ms = cfg["phantomReturnS"] * 1000
        prev = self._prev_snapshot or {}
        for name, value in snapshot.items():
            if not qa_channels.is_temperature(name):
                continue
            candidate = self._phantom.get(name)
            if candidate is not None:
                if abs(value - candidate["before"]) <= back:
                    del self._phantom[name]
                    kind, index = name.split(".")[0], int(name.split(".")[1])
                    self._event(self.model, now_ms, "phantom_reading", kind, device=index, payload={
                        "channel": name, "before": candidate["before"], "peak": candidate["peak"],
                        "durationMs": now_ms - candidate["ts"]})
                    continue
                if now_ms - candidate["ts"] > window_ms:
                    del self._phantom[name]  # a real change, not a phantom
                else:
                    if abs(value - candidate["before"]) > abs(candidate["peak"] - candidate["before"]):
                        candidate["peak"] = value
                    continue
            before = prev.get(name)
            if before is not None and abs(value - before) >= jump:
                self._phantom[name] = {"ts": now_ms, "before": before, "peak": value}

    # --- samples and blocks ------------------------------------------------------------------

    def _trigger(self, ts_ms, reason):
        """Start or extend the fine-resolution block. Returns its id."""
        if self.job is None:
            return None
        post = self.cfg()["postTriggerS"] * 1000
        if self._block is None:
            start = self._ring[0][0] if self._ring else ts_ms
            bid = self._alloc_block_id()
            self._block = {"id": bid, "start": start, "triggers": [{"ts": ts_ms, "reason": reason}]}
            self.writer.submit("block_insert", bid, self.job.key, start, None, self._block["triggers"])
            # the ring buffer up to now, in full resolution
            rows = []
            last = {}
            for ts, snap in list(self._ring):
                for name, value in snap.items():
                    if last.get(name) != value:
                        rows.append((ts, name, value))
                        last[name] = value
            self._last_fine = last
            self.writer.submit("samples", self.job.key, rows, qa_db.RESOLUTION_FINE, bid)
        else:
            self._block["triggers"].append({"ts": ts_ms, "reason": reason})
            if len(self._block["triggers"]) > 200:
                self._block["triggers"] = self._block["triggers"][-200:]
        self._fine_until = max(self._fine_until or 0, ts_ms + post)
        return self._block["id"]

    def _jump_triggers(self, snapshot, now_ms):
        prev = self._prev_snapshot
        if not prev:
            return
        th = self.cfg()["thresholds"]
        for name, value in snapshot.items():
            before = prev.get(name)
            if before is None or before == value:
                continue
            reason = None
            if qa_channels.is_temperature(name) and abs(value - before) >= th["temperatureK"]:
                reason = f"jump:{name}"
            elif qa_channels.is_fm_percentage(name) and abs(value - before) >= th["filamentPercentPoints"]:
                reason = f"jump:{name}"
            elif qa_channels.is_vin(name) and before > 0 and abs(value - before) / before >= th["vInPercent"] / 100:
                reason = f"jump:{name}"
            if reason:
                self._trigger(now_ms, reason)

    def _write_fine(self, snapshot, now_ms):
        if self._block is None:
            return
        rows = []
        for name, value in snapshot.items():
            if self._last_fine.get(name) != value:
                rows.append((now_ms, name, value))
                self._last_fine[name] = value
        if rows:
            self.writer.submit("samples", self.job.key, rows, qa_db.RESOLUTION_FINE, self._block["id"])
        self._close_block_if_due(now_ms)

    def _close_block_if_due(self, now_ms):
        if self._block is not None and self._fine_until is not None and now_ms >= self._fine_until:
            self._close_block(now_ms)

    def _close_block(self, now_ms):
        if self._block is None:
            return
        if self.job is not None:
            self.writer.submit("block_update", self._block["id"], min(now_ms, self._fine_until or now_ms),
                               self._block["triggers"])
        self._block = None
        self._fine_until = None
        self._last_fine = {}

    def _write_coarse(self, snapshot, now_ms):
        interval = self.cfg()["sampleIntervalS"] * 1000
        if self._last_coarse_ms is not None and now_ms - self._last_coarse_ms < interval:
            return
        self._last_coarse_ms = now_ms
        self.writer.submit("samples", self.job.key, [(now_ms, n, v) for n, v in snapshot.items()], qa_db.RESOLUTION_COARSE)

    def _live(self, snapshot, now_ms):
        if now_ms - self._last_live_ms < LIVE_SAMPLE_INTERVAL_MS:
            return
        self._last_live_ms = now_ms
        loads = {f"heater.{h}.load": round(qa_heaterload.heater_load(snapshot.get(f"heater.{h}.avgPwm"),
                                                                      self._max_pwm(h)) or 0, 4)
                 for h in qa_channels.nozzle_heaters(self.model)} if self.model is not None else {}
        self.broadcast({"type": "sample", "ts": now_ms, "jobId": self.job.id if self.job else None,
                        "layer": self.job.layer if self.job else None, "values": {**snapshot, **loads},
                        "heaterLoad": {str(h): {"mean": round(m, 4), "level": self.load.levels().get(h)}
                                       for h, m in self.load.means().items()}})

    def _max_pwm(self, heater):
        heaters = getattr(getattr(self.model, "heat", None), "heaters", None) or []
        if heater < len(heaters) and heaters[heater] is not None:
            return getattr(getattr(heaters[heater], "model", None), "max_pwm", None)
        return None
