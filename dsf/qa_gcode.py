"""G-code layer index and toolpath (PLAN.md §5.7).

Layer numbers follow ``job.layer`` exactly: in SBC mode DSF forwards a comment line to RRF only
when it contains one of its ``FirmwareComments`` chunks (``Comment.Contains``, case-sensitive;
DuetControlServer ``Settings.cs``/``Commands/Generic/Code.cs``, DSF v3.7-dev @ cd3ae65f), and RRF
turns the ones that start with a known string into layer numbers
(``GCodes::ProcessWholeLineComment``, RepRapFirmware 3.7-dev @ 3638836, GCodes3.cpp;
``StringStartsWith`` is case-sensitive, RRFLibraries 3.7-dev). Both lists are copied below.
PrusaSlicer/OrcaSlicer/SuperSlicer ``;LAYER_CHANGE`` passes DSF because it contains "LAYER" and
counts +1 in RRF; Cura ``;LAYER:<n>`` counts from 0; Simplify3D ``; layer <n>, Z = ...`` from 1.
Without such comments RRF reports no layer; the index then falls back to Z changes of
extruding moves (the replay still works, but job.layer is null during such a job).

The index stores the byte range of every layer and the modal state at its start (position,
feed rate, G90/G91, M82/M83, current object and feature type), so ``toolpath`` reads only the
bytes of one layer.

G-code semantics from the Duet3D wiki (Gcodes.md, 2026-09-21): G90/G91 switch X/Y/Z only,
M82/M83 the extruder; G92 sets the user position; G2/G3 take I/J (relative centre) or R;
M486 S<n> [A"name"] marks the object being printed, S-1 a non-object feature.
"""

import json
import math
import os
import re
import threading
from collections import OrderedDict

# DuetControlServer Settings.FirmwareComments (v3.7-dev @ cd3ae65f)
FIRMWARE_COMMENTS = ("printing object", "MESH", "process", "stop printing object", "layer", "LAYER",
                     "BEGIN_LAYER_OBJECT z=", "HEIGHT", "PRINTING", "REMAINING_TIME")
# RepRapFirmware GCodes::ProcessWholeLineComment StartStrings (3.7-dev @ 3638836), same order
START_STRINGS = ("printing object", "MESH", "process", "stop printing object", "layer", "LAYER",
                 "; --- layer", "BEGIN_LAYER_OBJECT z=", "HEIGHT", "PRINTING", "REMAINING_TIME", "LAYER_CHANGE")

INDEX_VERSION = 1
_WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
_INT_RE = re.compile(r"[-+]?\d+")
_M486_NAME_RE = re.compile(r'A\s*"((?:[^"]|"")*)"')
_EXCLUDE_NAME_RE = re.compile(r"NAME=(\S+)")
ARC_SEGMENT_MM = 1.0
ARC_SEGMENT_DEG = 5.0


def forwarded_to_firmware(comment):
    """Whether DSF sends this comment (text after ';') to RRF."""
    return any(chunk in comment for chunk in FIRMWARE_COMMENTS)


def layer_comment(comment, current):
    """The layer RRF is on after this whole-line comment (``current`` if it changes nothing).

    Returns ``(layer, object_action)``; object_action is ``("start", name)``, ``("stop", None)``
    or None.
    """
    if not forwarded_to_firmware(comment):
        return current, None
    text = comment.lstrip(" ")
    for index, start in enumerate(START_STRINGS):
        if not text.startswith(start):
            continue
        rest = text[len(start):]
        if rest[:1].isalpha() or rest[:1] == "_":
            continue  # "processName" is not "process"
        rest = rest.lstrip(" :")
        if index == 1:  # MESH (Cura)
            return current, ("stop", None) if rest.startswith("NONMESH") else ("start", rest.strip())
        if index == 9:  # PRINTING (Ideamaker)
            return current, ("stop", None) if rest.startswith("NON-OBJECT") else ("start", rest.strip())
        if index in (0, 2):
            return current, ("start", rest.strip())
        if index == 3:
            return current, ("stop", None)
        if index in (4, 5, 6):
            match = _INT_RE.match(rest)
            if match:
                layer = int(match.group(0))
                if layer >= 0:
                    return (layer if index == 4 else layer + 1), None
            return current, None
        if index == 11:  # LAYER_CHANGE: no number, next layer
            return current + 1, None
        return current, None
    return current, None


def parse_words(code):
    """``{letter: float}`` of a G-code line without its comment."""
    return {m.group(1).upper(): float(m.group(2)) for m in _WORD_RE.finditer(code)}


class _State:
    __slots__ = ("x", "y", "z", "e", "f", "rel_xyz", "rel_e", "obj", "type")

    def __init__(self):
        self.x = self.y = self.z = 0.0
        self.e = 0.0
        self.f = 3000.0
        self.rel_xyz = False
        self.rel_e = False
        self.obj = None
        self.type = None

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, data):
        state = cls()
        for k in cls.__slots__:
            if k in data:
                setattr(state, k, data[k])
        return state


class _Objects:
    """Object numbering as RRF does it: M486 S<n>, else label comments numbered from 0 in order
    of first appearance."""

    def __init__(self):
        self.names = {}
        self._by_name = {}

    def by_label(self, name):
        if name not in self._by_name:
            index = len(self._by_name)
            self._by_name[name] = index
            self.names.setdefault(index, name)
        return self._by_name[name]


def _split(line):
    """``(code, comment)``: comment is the text after the first ';' (None if there is none)."""
    pos = line.find(";")
    if pos < 0:
        return line, None
    return line[:pos], line[pos + 1:]


def _apply_move(state, words, cmd):
    """Advance ``state`` by a G0/G1/G2/G3 line. Returns (x0, y0, z0, e_delta)."""
    x0, y0, z0, e0 = state.x, state.y, state.z, state.e
    if "X" in words:
        state.x = state.x + words["X"] if state.rel_xyz else words["X"]
    if "Y" in words:
        state.y = state.y + words["Y"] if state.rel_xyz else words["Y"]
    if "Z" in words:
        state.z = state.z + words["Z"] if state.rel_xyz else words["Z"]
    if "E" in words:
        state.e = state.e + words["E"] if state.rel_e else words["E"]
    if "F" in words and words["F"] > 0:
        state.f = words["F"]
    return x0, y0, z0, state.e - e0


def _process_code(state, objects, code):
    """Modal bookkeeping for one line of code. Returns (command, words) for moves, else None."""
    stripped = code.strip()
    if not stripped or stripped[0] not in "GMgmT" or "{" in stripped:
        return None
    words = parse_words(stripped)
    if stripped[0] in "Gg":
        g = words.get("G")
        if g is None:
            return None
        if g in (0, 1, 2, 3):
            return int(g), words
        if g == 90:
            state.rel_xyz = False
        elif g == 91:
            state.rel_xyz = True
        elif g == 92:
            for axis in ("X", "Y", "Z", "E"):
                if axis in words:
                    setattr(state, axis.lower(), words[axis])
        return None
    if stripped[0] in "Mm":
        m = words.get("M")
        if m == 82:
            state.rel_e = False
        elif m == 83:
            state.rel_e = True
        elif m == 486 and "S" in words:
            s = int(words["S"])
            state.obj = s if s >= 0 else None
            name = _M486_NAME_RE.search(stripped)
            if name and s >= 0:
                objects.names[s] = name.group(1).replace('""', '"')
    return None


def _comment_object(state, objects, comment):
    text = comment.strip()
    if text.startswith("TYPE:"):
        state.type = text[5:].strip() or None
    elif text.startswith("EXCLUDE_OBJECT_START"):
        match = _EXCLUDE_NAME_RE.search(text)
        if match:
            state.obj = objects.by_label(match.group(1))
    elif text.startswith("EXCLUDE_OBJECT_END"):
        state.obj = None


def build_index(path, crc32=None, progress=None):
    """Scan ``path`` once. Returns the index record (JSON-serialisable)."""
    state = _State()
    objects = _Objects()
    layer = 0
    ranges = {}            # layer -> [[start, end, state_at_start]]
    open_range = None
    offset = 0
    size = os.path.getsize(path)
    # Z fallback bookkeeping
    z_layers = []          # [[z, offset, state]]
    last_extrude_z = None
    with open(path, "rb") as handle:
        for raw in handle:
            line_offset = offset
            offset += len(raw)
            line = raw.decode("utf-8", errors="replace")
            code, comment = _split(line)
            if comment is not None and not code.strip():
                new_layer, obj_action = layer_comment(comment.rstrip("\r\n"), layer)
                if obj_action is not None:
                    state.obj = objects.by_label(obj_action[1]) if obj_action[0] == "start" else None
                _comment_object(state, objects, comment)
                if new_layer != layer:
                    if open_range is not None:
                        open_range[1] = line_offset
                    layer = new_layer
                    open_range = [line_offset, None, state.to_dict()]
                    ranges.setdefault(layer, []).append(open_range)
                continue
            if comment is not None:
                _comment_object(state, objects, comment)
            move = _process_code(state, objects, code)
            if move is not None:
                cmd, words = move
                before = state.to_dict()
                _, _, _, de = _apply_move(state, words, cmd)
                if de > 0 and ("X" in words or "Y" in words or cmd in (2, 3)):
                    if last_extrude_z is None or state.z > last_extrude_z + 1e-6:
                        z_layers.append([state.z, line_offset, before])
                    last_extrude_z = state.z if last_extrude_z is None else max(last_extrude_z, state.z)
            if progress is not None and (offset & 0xFFFFF) < len(raw):
                progress(offset / size if size else 1.0)
    if open_range is not None:
        open_range[1] = size
    source = "comments"
    if not ranges:
        source = "z"
        for i, (z, start, st) in enumerate(z_layers):
            end = z_layers[i + 1][1] if i + 1 < len(z_layers) else size
            ranges[i + 1] = [[start, end, {**st}]]
    layers = [{"layer": n, "ranges": [[r[0], r[1]] for r in rs], "state": rs[0][2]}
              for n, rs in sorted(ranges.items())]
    for entry, (n, rs) in zip(layers, sorted(ranges.items())):
        entry["states"] = [r[2] for r in rs]
    return {
        "version": INDEX_VERSION,
        "crc32": crc32,
        "size": size,
        "source": source,
        "numLayers": max(ranges) if ranges else 0,
        "objects": {str(k): v for k, v in sorted(objects.names.items())},
        "layers": layers,
    }


def _arc_points(x0, y0, words, clockwise, x1, y1):
    """Chord end points of a G2/G3 arc from (x0, y0) to (x1, y1)."""
    if "R" in words:
        r = words["R"]
        dx, dy = x1 - x0, y1 - y0
        d = math.hypot(dx, dy)
        if d == 0 or abs(r) < d / 2:
            return [(x1, y1)]
        h = math.sqrt(max(0.0, r * r - (d / 2) ** 2))
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        # centre on the side that gives the requested direction; a negative R takes the long way
        sign = -1 if clockwise else 1
        if r < 0:
            sign = -sign
        cx = mx - sign * h * dy / d
        cy = my + sign * h * dx / d
    else:
        cx, cy = x0 + words.get("I", 0.0), y0 + words.get("J", 0.0)
    radius = math.hypot(x0 - cx, y0 - cy)
    if radius == 0:
        return [(x1, y1)]
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    sweep = a1 - a0
    if clockwise and sweep >= 0:
        sweep -= 2 * math.pi
    elif not clockwise and sweep <= 0:
        sweep += 2 * math.pi
    length = abs(sweep) * radius
    steps = max(1, int(max(length / ARC_SEGMENT_MM, abs(math.degrees(sweep)) / ARC_SEGMENT_DEG)))
    points = [(cx + radius * math.cos(a0 + sweep * k / steps), cy + radius * math.sin(a0 + sweep * k / steps))
              for k in range(1, steps)]
    points.append((x1, y1))
    return points


def toolpath(path, index, layer, filament_diameter=1.75):
    """Segments of ``layer`` as columns: x0, y0, x1, y1, z, e, flow (mm³/s), object, type,
    travel (0/1). ``types`` maps the type ids to names."""
    entry = next((e for e in index.get("layers", []) if e["layer"] == layer), None)
    if entry is None:
        return None
    area = math.pi * ((filament_diameter if filament_diameter and filament_diameter > 0 else 1.75) / 2) ** 2
    cols = {k: [] for k in ("x0", "y0", "x1", "y1", "z", "e", "flow", "object", "type", "travel")}
    types = []
    type_ids = {}
    objects = _Objects()
    objects.names = {int(k): v for k, v in (index.get("objects") or {}).items()}
    for (start, end), st in zip(entry["ranges"], entry.get("states") or [entry["state"]] * len(entry["ranges"])):
        state = _State.from_dict(st)
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read(max(0, end - start))
        first = True
        for raw in data.splitlines():
            line = raw.decode("utf-8", errors="replace")
            code, comment = _split(line)
            if comment is not None and not code.strip():
                if first:
                    first = False
                    continue  # the layer comment itself
                _, obj_action = layer_comment(comment, 0)
                if obj_action is not None:
                    state.obj = objects.by_label(obj_action[1]) if obj_action[0] == "start" else None
                _comment_object(state, objects, comment)
                continue
            first = False
            if comment is not None:
                _comment_object(state, objects, comment)
            move = _process_code(state, objects, code)
            if move is None:
                continue
            cmd, words = move
            x0, y0, z0, de = _apply_move(state, words, cmd)
            if cmd in (2, 3):
                points = _arc_points(x0, y0, words, cmd == 2, state.x, state.y)
            else:
                points = [(state.x, state.y)]
            total = 0.0
            px, py = x0, y0
            for qx, qy in points:
                total += math.hypot(qx - px, qy - py)
                px, py = qx, qy
            if total <= 0:
                continue  # retract, unretract, Z hop, feed rate only
            feed = state.f / 60.0 if state.f > 0 else None
            px, py = x0, y0
            if state.type not in type_ids:
                type_ids[state.type] = len(types)
                types.append(state.type)
            for qx, qy in points:
                seg = math.hypot(qx - px, qy - py)
                share = seg / total
                e = de * share
                extruding = e > 0
                flow = (e * area) / (seg / feed) if extruding and feed and seg > 0 else 0.0
                cols["x0"].append(round(px, 3))
                cols["y0"].append(round(py, 3))
                cols["x1"].append(round(qx, 3))
                cols["y1"].append(round(qy, 3))
                cols["z"].append(round(state.z, 3))
                cols["e"].append(round(e, 5))
                cols["flow"].append(round(flow, 3))
                cols["object"].append(state.obj)
                cols["type"].append(type_ids[state.type])
                cols["travel"].append(0 if extruding else 1)
                px, py = qx, qy
    return {"layer": layer, "segments": cols, "types": types,
            "objects": {str(k): v for k, v in sorted(objects.names.items())}}


class IndexCache:
    """Layer indexes per file CRC: in RAM (LRU) and as JSON under ``<data>/index/<crc>.json``.
    ``ensure`` builds one in a background thread; ``get`` never blocks on a build."""

    def __init__(self, directory, capacity=8):
        self.directory = os.path.join(directory, "index")
        self._capacity = capacity
        self._items = OrderedDict()
        self._building = {}
        self._errors = {}
        self._lock = threading.Lock()

    def _path(self, crc):
        return os.path.join(self.directory, f"{crc}.json")

    def get(self, crc):
        """``(index | None, state)`` with state ``ready``, ``building``, ``missing`` or ``error: …``."""
        with self._lock:
            if crc in self._items:
                self._items.move_to_end(crc)
                return self._items[crc], "ready"
            if crc in self._building:
                return None, "building"
            if crc in self._errors:
                return None, f"error: {self._errors[crc]}"
        try:
            with open(self._path(crc), "r", encoding="utf-8") as handle:
                index = json.load(handle)
            if index.get("version") == INDEX_VERSION:
                self._remember(crc, index)
                return index, "ready"
        except (OSError, ValueError):
            pass
        return None, "missing"

    def _remember(self, crc, index):
        with self._lock:
            self._items[crc] = index
            self._items.move_to_end(crc)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def ensure(self, path, crc, run_async=True):
        """Build the index of ``path`` unless it exists or is being built."""
        index, state = self.get(crc)
        if index is not None or state == "building":
            return state
        with self._lock:
            self._building[crc] = True
            self._errors.pop(crc, None)

        def work():
            try:
                built = build_index(path, crc)
                os.makedirs(self.directory, exist_ok=True)
                tmp = self._path(crc) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(built, handle, separators=(",", ":"))
                os.replace(tmp, self._path(crc))
                self._remember(crc, built)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._errors[crc] = str(exc)
            finally:
                with self._lock:
                    self._building.pop(crc, None)

        if run_async:
            threading.Thread(target=work, name=f"qa-index-{crc}", daemon=True).start()
            return "building"
        work()
        return self.get(crc)[1]
