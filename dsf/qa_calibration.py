"""Calibration of the bed and the Z probe per job (PLAN.md §5.4 "Kalibrierung", Tim 2026-09-29): the
mesh (G29), the levelling (G32), the scanning probe (M558.1) and its drive level (M558.2).

Whatever the object model holds comes from it (Tim 2026-09-29: never parse a message for a value the
model has). Read against RRF 3.7-dev @ 32a84d2 and dsf-python 3.7.0b1 on 2026-09-29:
- mesh: ``move.compensation`` ``type``, ``file``, ``fadeHeight``, ``liveGrid``, ``meshDeviation``
  {mean, deviation}, the last three only while a mesh is in use (Move.cpp:236-248). G29 sets them when it
  has probed (GCodes4.cpp:1127) and when it loads a file (Move3.cpp:248); ``G29 S2`` clears them, which the
  CHX 350's bed.g does before it probes, so a new mesh with the old numbers still counts as new.
- levelling: ``move.calibration`` {initial, final {mean, deviation}, numFactors} (Move.cpp:177, 223-233),
  set by G32's leadscrew adjustment (ZLeadscrewKinematics.cpp:385-393) or a delta's auto-calibration, and
  ``move.kinematics.tiltCorrection.lastCorrections`` (ZLeadscrewKinematics.cpp:59).
- probe: ``sensors.probes[]`` ``scanCoefficients`` (offset mm, A, B, C) and ``threshold`` (the reading at the
  trigger height) after M558.1, ``isCalibrated``, ``triggerHeight``, ``calibrationTemperature``,
  ``temperatureCoefficients`` (ZProbe.cpp:86-119).
Not in the model, so from elsewhere:
- the mesh's min/max error, probed points and heights: the height map file the model names, as
  HeightMap::SaveToFile writes it (Grid.cpp:367-420; a "0" without a decimal point was not probed). G29
  writes it after the model has the new mesh, so the file is taken once it matches (``MeshFile``).
- the rms error of the M558.1 fit: its reply "…, reading at trigger height N, rms error X mm"
  (ZProbe.cpp:798), matched to the probe by N = ``threshold``.
- M558.2's drive current and offset: the sensor board's reply "Calibration successful, sensor drive current
  is N, offset is M" (Duet3Expansion 3.7-dev @ 806ef34 ScanningSensorHandler.cpp:391; RRF relays it,
  RemoteZProbe.cpp:286-310). Setting them with S/R replies nothing, so only calibrations are seen.
DSF puts the replies of the job's codes into ``messages[]`` (DuetSoftwareFramework v3.7-dev @ cd3ae65f
Codes/Pipelines/Executed.cs:211-213, Utility/Logger.cs:370-376).

Stored as JSON, the scan coefficients keep 6 significant digits: dsf-python 3.7.0b1 makes every
``json.dumps`` of the process write floats with ``%g`` (object_model/model_object.py:9-17, 2026-09-29).
"""

import collections
import copy
import logging
import os
import re

import qa_channels

logger = logging.getLogger("qa.calibration")

MESH_FILE_WAIT_MS = 30_000   # G29 writes the height map after the model has the new mesh: wait this long
MESH_LOAD_WAIT_MS = 5_000    # a loaded mesh (G29 S1) has an older file: take a matching one after this
FILE_FRESH_MS = 5_000        # a file written this long before the model changed still counts as the new one
MATCH_TOLERANCE = 0.0015     # the model and the file both give the deviation to 3 decimals
RMS_WINDOW_MS = 60_000       # an M558.1 reply belongs to a probe change within this time

HEIGHT_MAP_COMMENT = "RepRapFirmware height map file v2"
HEIGHT_MAP_LABELS = "axis0,axis1,min0,max0,min1,max1,radius,spacing0,spacing1,num0,num1"
HEIGHT_MAP_STATS_RE = re.compile(r"min error (-?[\d.]+), max error (-?[\d.]+), mean (-?[\d.]+), deviation (-?[\d.]+)")
HEIGHT_MAP_DATE_RE = re.compile(r"generated at (\d{4}-\d\d-\d\d \d\d:\d\d)")
SCAN_FIT_RE = re.compile(r"reading at trigger height (-?\d+), rms error (-?[\d.]+)\s*mm")
DRIVE_RE = re.compile(r"Calibration successful, sensor drive current is (\d+), offset is (\d+)")

# kind: mesh / levelling / probe / probeDrive; index: the probe number, else None; entry: as in the context
# (None: gone); new: True for a calibration that just happened, False when only values from the file or a
# reply arrived, or the mesh was cleared
Change = collections.namedtuple("Change", "kind index entry new")


def _num(value):
    return qa_channels.number(value)


def _nums(values):
    return [_num(v) for v in values] if values is not None else None


def read_mesh(model):
    """The mesh in use as the model reports it, None without one."""
    comp = getattr(getattr(model, "move", None), "compensation", None)
    if comp is None or qa_channels.enum_value(getattr(comp, "type", None)) != "mesh":
        return None
    dev = getattr(comp, "mesh_deviation", None)
    grid = getattr(comp, "live_grid", None)
    return {
        "file": getattr(comp, "file", None),
        "fadeHeight": _num(getattr(comp, "fade_height", None)),
        "mean": _num(getattr(dev, "mean", None)) if dev is not None else None,
        "deviation": _num(getattr(dev, "deviation", None)) if dev is not None else None,
        "grid": None if grid is None else {
            "axes": list(getattr(grid, "axes", None) or []), "mins": _nums(getattr(grid, "mins", None)),
            "maxs": _nums(getattr(grid, "maxs", None)), "spacings": _nums(getattr(grid, "spacings", None)),
            "radius": _num(getattr(grid, "radius", None))},
    }


def read_levelling(model):
    """The last G32 (leadscrews) or delta auto-calibration, None before the first one since RRF started."""
    move = getattr(model, "move", None)
    cal = getattr(move, "calibration", None)
    if cal is None or not getattr(cal, "num_factors", 0):
        return None

    def deviations(dev):
        return {"mean": _num(getattr(dev, "mean", None)), "deviation": _num(getattr(dev, "deviation", None))}

    tilt = getattr(getattr(move, "kinematics", None), "tilt_correction", None)
    return {
        "numFactors": getattr(cal, "num_factors", None),
        "before": deviations(getattr(cal, "initial", None)),
        "after": deviations(getattr(cal, "final", None)),
        "corrections": _nums(getattr(tilt, "last_corrections", None)) if tilt is not None else None,
    }


def read_probes(model):
    """Per probe number what its calibration consists of (not its live readings or touch mode)."""
    probes = {}
    for i, probe in qa_channels.items(getattr(getattr(model, "sensors", None), "probes", None)):
        probes[i] = {
            "index": i,
            "type": qa_channels.enum_value(getattr(probe, "type", None)),
            "isCalibrated": getattr(probe, "is_calibrated", None),
            "scanCoefficients": _nums(getattr(probe, "scan_coefficients", None)) or None,
            "threshold": getattr(probe, "threshold", None),
            "triggerHeight": _num(getattr(probe, "trigger_height", None)),
            "calibrationTemperature": _num(getattr(probe, "calibration_temperature", None)),
            "temperatureCoefficients": _nums(getattr(probe, "temperature_coefficients", None)),
        }
    return probes


def read_height_map(path):
    """RRF's height map file: its statistics, grid and heights (rows along axis 1, None = not probed).
    None when it is not a version-2 height map."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        lines = handle.read().splitlines()
    if len(lines) < 3 or not lines[0].startswith(HEIGHT_MAP_COMMENT) or lines[1].strip() != HEIGHT_MAP_LABELS:
        return None
    stats = HEIGHT_MAP_STATS_RE.search(lines[0])
    date = HEIGHT_MAP_DATE_RE.search(lines[0])
    params = [p.strip() for p in lines[2].split(",")]
    if stats is None or len(params) != 11:
        return None
    nums = [int(params[9]), int(params[10])]
    heights = []
    for line in lines[3:3 + nums[1]]:
        heights.append([float(cell) if "." in cell else None for cell in (c.strip() for c in line.split(","))])
    return {
        "generatedAt": date.group(1) if date else None,
        "minError": float(stats.group(1)), "maxError": float(stats.group(2)),
        "mean": float(stats.group(3)), "deviation": float(stats.group(4)),
        "grid": {"axes": params[0:2], "mins": [float(params[2]), float(params[4])],
                 "maxs": [float(params[3]), float(params[5])], "spacings": [float(params[7]), float(params[8])],
                 "radius": float(params[6]), "nums": nums},
        "points": sum(v is not None for row in heights for v in row),
        "heights": heights,
    }


def event_payload(entry):
    """An entry for an event: without the mesh's heights (the context has them)."""
    return None if entry is None else copy.deepcopy({k: v for k, v in entry.items() if k != "heights"})


class Tracker:
    """The calibration state of the machine, kept whether a job runs or not, so a job's context starts
    with what was calibrated before it. ``update`` per patch and ``tick`` return the changes."""

    def __init__(self, resolve_path):
        self.resolve_path = resolve_path
        self._first = True
        self._om = {"mesh": None, "levelling": None, "probes": {}}   # the model's part, to compare
        self.mesh = None
        self.levelling = None
        self.probes = {}
        self.drive = None
        self._pending = None        # mesh waiting for its file: {"file", "since", "mtime"}
        self._paths = {}            # virtual -> real path of height map files
        self._fits = collections.deque(maxlen=8)   # (ts, reading at trigger height, rms error) of M558.1 replies

    def snapshot(self):
        """The context entry ``calibration``."""
        return copy.deepcopy({"mesh": self.mesh, "levelling": self.levelling,
                              "probes": [self.probes[i] for i in sorted(self.probes)], "probeDrive": self.drive})

    def update(self, model, patch, now_ms):
        first = self._first
        self._first = False
        ts = None if first else now_ms   # what the model had when QA started has no known time
        changes = []

        mesh = read_mesh(model)
        if mesh != self._om["mesh"]:
            self._om["mesh"] = mesh
            self.mesh = None if mesh is None else {**mesh, "ts": ts}
            # at the start any matching file will do; later G29 writes the new one after the model changed
            self._pending = None if mesh is None else {
                "file": mesh["file"], "since": now_ms - (MESH_LOAD_WAIT_MS if first else 0), "mtime": None}
            if not first:
                changes.append(Change("mesh", None, self.mesh, mesh is not None))
        changes += self._poll_mesh_file(now_ms)

        levelling = read_levelling(model)
        if levelling != self._om["levelling"]:
            self._om["levelling"] = levelling
            self.levelling = None if levelling is None else {**levelling, "ts": ts}
            if not first:
                changes.append(Change("levelling", None, self.levelling, levelling is not None))

        probes = read_probes(model)
        for i in sorted(set(probes) | set(self._om["probes"])):
            new, old = probes.get(i), self._om["probes"].get(i)
            if new == old:
                continue
            entry = None if new is None else {**new, "ts": ts}
            if entry is not None and entry["scanCoefficients"] is not None:
                rms = self._fit_rms(entry["threshold"], now_ms)
                if rms is not None:
                    entry["rmsError"] = rms
            if entry is None:
                self.probes.pop(i, None)
            else:
                self.probes[i] = entry
            if not first:
                # a probe that appears or goes (M558) is configuration, not a calibration
                changes.append(Change("probe", i, entry, new is not None and old is not None))
        self._om["probes"] = probes

        changes += self._messages(patch, now_ms)
        return changes

    def tick(self, now_ms):
        return self._poll_mesh_file(now_ms)

    # --- replies --------------------------------------------------------------------------

    def _messages(self, patch, now_ms):
        changes = []
        for message in (patch or {}).get("messages") or []:
            content = message.get("content") if isinstance(message, dict) else None
            if not content:
                continue
            fit = SCAN_FIT_RE.search(content)
            if fit:
                reading, rms = int(fit.group(1)), float(fit.group(2))
                self._fits.append((now_ms, reading, rms))
                for i, entry in self.probes.items():
                    if (entry.get("threshold") == reading and entry.get("scanCoefficients") is not None
                            and entry.get("rmsError") != rms):
                        entry["rmsError"] = rms
                        changes.append(Change("probe", i, entry, False))
            drive = DRIVE_RE.search(content)
            if drive:
                self.drive = {"current": int(drive.group(1)), "offset": int(drive.group(2)), "ts": now_ms}
                changes.append(Change("probeDrive", None, self.drive, True))
        return changes

    def _fit_rms(self, reading, now_ms):
        for ts, fit_reading, rms in reversed(self._fits):
            if fit_reading == reading and now_ms - ts <= RMS_WINDOW_MS:
                return rms
        return None

    # --- the mesh's file ------------------------------------------------------------------

    def _real_path(self, virtual):
        if virtual not in self._paths:
            real = self.resolve_path(virtual)
            if not real:
                return None
            self._paths[virtual] = real
        return self._paths[virtual]

    def _poll_mesh_file(self, now_ms):
        """Adds the height map's statistics and heights to the mesh once the file matches it: the same
        deviation and grid origin, written after the change (G29) or, after MESH_LOAD_WAIT_MS, any time
        (G29 S1 loads an older file). Gives up after MESH_FILE_WAIT_MS."""
        pending = self._pending
        if pending is None or self.mesh is None:
            return []
        waited = now_ms - pending["since"]
        path = self._real_path(pending["file"]) if pending["file"] else None
        info = None
        try:
            mtime = os.stat(path).st_mtime * 1000 if path else None
            if mtime is not None and mtime != pending["mtime"]:
                pending["mtime"] = mtime
                info = read_height_map(path)
                pending["info"] = info
            else:
                info = pending.get("info")
        except (OSError, ValueError) as exc:
            pending["error"] = str(exc)
        mesh = self.mesh
        matches = (info is not None and mesh.get("deviation") is not None
                   and abs(info["deviation"] - mesh["deviation"]) <= MATCH_TOLERANCE
                   and _same_origin(info["grid"], mesh.get("grid")))
        fresh = pending["mtime"] is not None and pending["mtime"] >= pending["since"] - FILE_FRESH_MS
        if matches and (fresh or waited >= MESH_LOAD_WAIT_MS):
            self._pending = None
            mesh.update({k: info[k] for k in ("minError", "maxError", "points", "generatedAt", "grid", "heights")})
            return [Change("mesh", None, mesh, False)]
        if waited >= MESH_FILE_WAIT_MS:
            self._pending = None
            logger.warning("height map %s does not match the mesh in use (%s)", pending["file"],
                           pending.get("error") or "deviation or grid differ")
        return []


def _same_origin(file_grid, model_grid):
    if not model_grid or not model_grid.get("mins"):
        return True
    return all(a is not None and abs(a - b) <= 0.01 for a, b in zip(model_grid["mins"], file_grid["mins"]))
