"""Object model journal (PLAN.md §5.13, Tim 2026-09-29): every change of the object model during a job,
so that problems nobody thought of can still be investigated afterwards.

Recording: DSF's patches as they arrive (the raw JSON text, so no float loses digits; dsf-python makes
``json.dumps`` write ``%g``, object_model/model_object.py:9-17) and full snapshots from DSF at the job start,
every ``journal.snapshotIntervalMin`` and at the job end, into ``journal/<job id>/journal.jsonl.gz``. One
line per entry, ``{"t":<epoch ms>,"snapshot":{…}}`` or ``{"t":<epoch ms>,"patch":{…}}``; one gzip member
per ``CHUNK_MS`` (concatenated members are one gzip file, so the file downloads and unpacks as a whole).
Every snapshot starts a member of its own and ``index.jsonl`` lists the members (``start``, ``end``,
``offset``, ``length``, ``snapshot``), so a reader seeks to the last snapshot before a time. A member is
indexed only once it is written, so after a crash the file is cut back to the last indexed one.

Measured on the CHX 350 on 2026-09-29 (layers 13-27 of a Benchy): 12.5 patches/s, 11 KB/s of JSON, about
5 MB/h compressed; a full model 57 KB (21 KB compressed, 16 KB of it ``job.file.thumbnails``). In the
database the journal would have made every backup (``VACUUM INTO`` after each job) that much larger, so it
lives in files next to it, like the timelapse.

Replay follows DSF's patch rules (DuetSoftwareFramework v3.7-dev @ cd3ae65f
IPC/Processors/ModelSubscription.cs:359-452, 461-600): an object carries only what changed, a list its new
length with ``{}`` for an unchanged object, ``messages`` only the new messages. Replaying 734 patches of the
CHX 350 onto its model gave the model DSF reported afterwards in all 2071 values (2026-09-29).
"""

import copy
import gzip
import json
import logging
import os
import re
import shutil
import zlib

logger = logging.getLogger("qa.journal")

CHUNK_MS = 60_000
DATA_FILE = "journal.jsonl.gz"
INDEX_FILE = "index.jsonl"
# DSF's answer to GetObjectModel, the model written raw into it (Command.cs:106-113, IPC/Connection.cs:698-703)
ENVELOPE_START = '{"success":true,"result":'
MESSAGES_KEPT = 100   # messages a replayed model keeps (the model only carries new ones per patch)

_JOB_ID_RE = re.compile(r"^[0-9A-Za-z-]+$")
_PATH_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
MISSING = object()


def job_dir(data_dir, job_id):
    if not job_id or not _JOB_ID_RE.match(job_id):
        raise ValueError(f"bad job id {job_id!r}")
    return os.path.join(data_dir, "journal", job_id)


def unwrap(text):
    """The model out of DSF's ``{"success":true,"result":…}``."""
    text = text.strip()
    if text.startswith(ENVELOPE_START) and text.endswith("}"):
        return text[len(ENVELOPE_START):-1]
    if text.startswith('{"success":false'):
        raise RuntimeError(text[:300])
    return text


def _line(t, kind, raw):
    # a JSON text has no raw line break inside a string, only between tokens
    return f'{{"t":{int(t)},"{kind}":{raw.strip().replace(chr(10), " ").replace(chr(13), " ")}}}'


class Writer:
    """Records the job's journal; called from the collector's thread (``job_started``, ``job_finished``,
    ``shutdown``) and the daemon's loop (``patch``, ``tick``). ``snapshot``: () -> DSF's model as JSON text."""

    def __init__(self, data_dir, settings, snapshot=None):
        self.data_dir = data_dir
        self.settings = settings
        self._snapshot = snapshot
        self.job_id = None
        self._lines = []           # (t, line)
        self._chunk_start = None
        self._last_snapshot = None
        self._warned = set()

    def cfg(self):
        return self.settings.current()["journal"]

    # --- collector hooks -------------------------------------------------------------------

    def job_started(self, job_id, now_ms):
        """A job starts, or goes on after a daemon restart: its journal continues."""
        self._flush()
        self.job_id = None
        if not self.cfg()["enabled"]:
            return
        directory = job_dir(self.data_dir, job_id)
        try:
            os.makedirs(directory, exist_ok=True)
            _repair(directory)
        except OSError as exc:
            self._warn("dir", "object model journal of %s not writable: %s", job_id, exc)
            return
        self.job_id = job_id
        self._take_snapshot(now_ms)

    def job_finished(self, now_ms):
        if self.job_id is not None:
            self._take_snapshot(now_ms)
        self.job_id = None

    def shutdown(self, _now_ms=None):
        """The daemon stops: what is buffered goes to the file, the job's journal goes on after a restart."""
        self._flush()
        self.job_id = None

    # --- daemon loop -----------------------------------------------------------------------

    def patch(self, raw, now_ms):
        if self.job_id is None:
            return
        if self._chunk_start is None:
            self._chunk_start = now_ms
        self._lines.append((now_ms, _line(now_ms, "patch", raw)))
        self.tick(now_ms)

    def tick(self, now_ms):
        if self.job_id is None:
            return
        if self._chunk_start is not None and now_ms - self._chunk_start >= CHUNK_MS:
            self._flush()
        interval = self.cfg()["snapshotIntervalMin"] * 60_000
        if self._last_snapshot is None or now_ms - self._last_snapshot >= interval:
            self._take_snapshot(now_ms)

    # --- writing ---------------------------------------------------------------------------

    def _take_snapshot(self, now_ms):
        self._flush()
        self._last_snapshot = now_ms   # a failed one is tried again after the interval, not on every patch
        if self._snapshot is None:
            return
        try:
            raw = unwrap(self._snapshot())
        except Exception as exc:  # noqa: BLE001
            self._warn("snapshot", "object model snapshot failed: %s", exc)
            return
        self._write([(now_ms, _line(now_ms, "snapshot", raw))], snapshot=True)

    def _flush(self):
        if not self._lines:
            return
        lines, self._lines = self._lines, []
        self._chunk_start = None
        self._write(lines, snapshot=False)

    def _write(self, lines, snapshot):
        if self.job_id is None:
            return
        directory = job_dir(self.data_dir, self.job_id)
        data = gzip.compress(("\n".join(line for _t, line in lines) + "\n").encode("utf-8"), compresslevel=6)
        try:
            with open(os.path.join(directory, DATA_FILE), "ab") as handle:
                offset = handle.tell()
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            entry = {"start": lines[0][0], "end": lines[-1][0], "offset": offset, "length": len(data),
                     "snapshot": snapshot}
            with open(os.path.join(directory, INDEX_FILE), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            self._warn("write", "object model journal of %s: %s", self.job_id, exc)

    def _warn(self, key, message, *args):
        if key not in self._warned:   # once per daemon run and kind
            self._warned.add(key)
            logger.warning(message, *args)

    # --- retention -------------------------------------------------------------------------

    def retention(self, keep_ids, max_bytes):
        """Journals of jobs that still have their raw data (``keep_ids``, the database's retention) stay,
        the running job's always; then the oldest go while all together are larger than ``max_bytes``."""
        root = os.path.join(self.data_dir, "journal")
        try:
            names = sorted(os.listdir(root))   # job ids start with their UTC start time
        except OSError:
            return []
        removed = []
        kept = []
        for name in names:
            if name == self.job_id or name in keep_ids:
                kept.append(name)
            else:
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                removed.append(name)
        sizes = [(name, _tree_size(os.path.join(root, name))) for name in kept]
        total = sum(size for _name, size in sizes)
        for name, size in sizes:
            if total <= max_bytes:
                break
            if name == self.job_id:
                continue
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
            removed.append(name)
            total -= size
        return removed


def _tree_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def _repair(directory):
    """Cuts the data file back to its last indexed member (a crash may have left half of one)."""
    index = read_index(directory)
    end = index[-1]["offset"] + index[-1]["length"] if index else 0
    path = os.path.join(directory, DATA_FILE)
    if os.path.exists(path) and os.path.getsize(path) > end:
        with open(path, "r+b") as handle:
            handle.truncate(end)
    with open(os.path.join(directory, INDEX_FILE), "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(entry, separators=(",", ":")) + "\n" for entry in index)


# --- reading (API threads) -------------------------------------------------------------------

def read_index(directory):
    """The members that were written completely (a torn last line is left out)."""
    entries = []
    try:
        with open(os.path.join(directory, INDEX_FILE), encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    break
                if isinstance(entry, dict) and {"start", "end", "offset", "length"} <= entry.keys():
                    entries.append(entry)
    except OSError:
        pass
    return entries


def info(directory):
    """What the API tells about a job's journal, None without one."""
    index = read_index(directory)
    if not index:
        return None
    try:
        size = os.path.getsize(os.path.join(directory, DATA_FILE))
    except OSError:
        return None
    return {"bytes": size, "start": index[0]["start"], "end": index[-1]["end"],
            "snapshots": sum(1 for e in index if e.get("snapshot"))}


def entries(directory, start=None):
    """``(t, kind, line)`` from the last snapshot at or before ``start`` (the first one without)."""
    index = read_index(directory)
    snapshots = [i for i, entry in enumerate(index) if entry.get("snapshot")]
    first = snapshots[0] if snapshots else 0
    for i in snapshots:
        if start is not None and index[i]["start"] <= start:
            first = i
    with open(os.path.join(directory, DATA_FILE), "rb") as handle:
        for entry in index[first:]:
            handle.seek(entry["offset"])
            try:
                text = zlib.decompress(handle.read(entry["length"]), 31).decode("utf-8")
            except (zlib.error, UnicodeDecodeError) as exc:
                logger.warning("object model journal %s: bad member at %d: %s", directory, entry["offset"], exc)
                continue
            for line in text.splitlines():
                comma = line.find(",", 5)
                if not line.startswith('{"t":') or comma < 0:
                    continue
                kind = "snapshot" if line.startswith('"snapshot"', comma + 1) else "patch"
                yield int(line[5:comma]), kind, line


def merge(state, patch):
    """DSF's patch onto a model (in place where possible; returns the result)."""
    if isinstance(patch, dict):
        state = state if isinstance(state, dict) else {}
        for key, value in patch.items():
            state[key] = merge(state.get(key), value)
        return state
    if isinstance(patch, list):
        state = state if isinstance(state, list) else []
        del state[len(patch):]
        for i, value in enumerate(patch):
            if i < len(state):
                state[i] = merge(state[i], value)
            else:
                state.append(merge(None, value))
        return state
    return patch


def parse_path(path):
    """``move.axes[2].machinePosition`` -> ``["move", "axes", 2, "machinePosition"]``."""
    keys = []
    for name, index in _PATH_RE.findall(path or ""):
        keys.append(int(index) if index else name)
    return keys


def get_path(value, keys):
    for key in keys:
        if isinstance(key, int):
            if not isinstance(value, list) or key >= len(value):
                return None
        elif not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _sub_patch(patch, keys):
    """The part of a patch at ``keys``: MISSING when it leaves the path alone, None when a list on the way
    became shorter than the index."""
    node = patch
    for key in keys:
        if isinstance(key, int):
            if not isinstance(node, list):
                return MISSING
            if key >= len(node):
                return None
        elif not isinstance(node, dict) or key not in node:
            return MISSING
        node = node[key]
    return node


def _needles(keys):
    """Texts a patch line must contain to be able to change the value at ``keys``: the last key's name, or a
    key on the way set to null, or a list on the way (a shorter one drops the index)."""
    names = [k for k in keys if isinstance(k, str)]
    needles = {f'"{names[-1]}"'} | {f'"{name}":null' for name in names}
    needles |= {f'"{keys[i]}":[' for i in range(len(keys) - 1) if isinstance(keys[i], str) and isinstance(keys[i + 1], int)}
    return tuple(needles)


def _values(directory, keys, start=None):
    """``(t, value)`` of the value at ``keys`` after each snapshot and each patch that touches it."""
    needles = _needles(keys)
    value = MISSING
    for t, kind, line in entries(directory, start):
        if kind == "patch" and not any(needle in line for needle in needles):
            continue
        body = json.loads(line)
        if kind == "snapshot":
            value = get_path(body["snapshot"], keys)
        else:
            sub = _sub_patch(body["patch"], keys)
            if sub is MISSING:
                continue
            value = None if sub is None else merge(copy.deepcopy(value) if value is not MISSING else None, sub)
        yield t, value


def state_at(directory, at_ms=None, path=""):
    """The model (or the value at ``path``) as it was at ``at_ms`` (default: the journal's end), replayed
    from the last snapshot before it; ``snapshotAt`` null: no snapshot, only what the patches carried.
    The whole model's ``messages`` holds the messages since that snapshot."""
    keys = parse_path(path)
    if keys and keys != ["messages"]:
        found = None
        for t, value in _values(directory, keys, at_ms):
            if at_ms is not None and t > at_ms:
                break
            found = (t, value)
        snapshots = [e["start"] for e in read_index(directory)
                     if e.get("snapshot") and (at_ms is None or e["start"] <= at_ms)]
        if found is None:
            return None
        return {"at": at_ms if at_ms is not None else _end(directory), "snapshotAt": snapshots[-1] if snapshots else None,
                "path": path, "value": found[1]}
    state = {"messages": []}
    snapshot_t = None
    last_t = None
    for t, kind, line in entries(directory, at_ms):
        if at_ms is not None and t > at_ms:
            break
        body = json.loads(line)
        if kind == "snapshot":
            state = body["snapshot"]
            state["messages"] = list(state.get("messages") or [])
            snapshot_t = t
        else:
            patch = body["patch"]
            messages = patch.pop("messages", None) or []
            state = merge(state, patch)
            state["messages"] = (state["messages"] + messages)[-MESSAGES_KEPT:]
        last_t = t
    if last_t is None:
        return None
    return {"at": at_ms if at_ms is not None else last_t, "snapshotAt": snapshot_t, "path": path or "",
            "value": get_path(state, keys)}


def _end(directory):
    index = read_index(directory)
    return index[-1]["end"] if index else None


def history(directory, path, start=None, end=None, limit=1000):
    """Every change of the value at ``path`` between ``start`` and ``end`` (epoch ms), starting with the value
    it had at ``start``. ``messages`` gives each new message instead."""
    keys = parse_path(path)
    if not keys:
        raise ValueError("a path is needed")
    points = []
    before = None
    truncated = False
    if keys == ["messages"]:
        changes = ((t, m) for t, kind, line in entries(directory, start) if kind == "patch" and '"messages"' in line
                   for m in (json.loads(line)["patch"].get("messages") or []))
    else:
        changes = _changes(_values(directory, keys, start))
    for t, value in changes:
        if end is not None and t > end:
            break
        if start is not None and t < start:
            before = {"t": t, "value": value}
            continue
        if len(points) >= limit:
            truncated = True
            break
        points.append({"t": t, "value": value})
    if before is not None:
        points.insert(0, before)
    return {"path": path, "points": points[:limit], "truncated": truncated}


def _changes(values):
    last = MISSING
    for t, value in values:
        if last is MISSING or value != last:
            last = value
            yield t, value
