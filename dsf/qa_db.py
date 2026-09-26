"""SQLite storage (PLAN.md §5.5).

One writer thread owns the only write connection. The collector hands it operations through
a queue; samples are committed in a transaction every ``commitIntervalS`` (≤ 30 s loss window
on power failure), anything marked urgent (events, job records) is committed at once.
Endpoint threads read through their own read-only connections (WAL lets them run beside the
writer). ``journal_mode=WAL`` + ``synchronous=FULL``: a commit is on disk when it returns.

Start-up: ``PRAGMA quick_check``; a damaged file is moved aside (``qa.db.corrupt.<epoch>``, with
its -wal/-shm) and the newest backup that passes ``quick_check`` is copied back, else a new
database is created. After every job: ``integrity_check`` then ``VACUUM INTO`` a new backup,
two generations (``qa.backup.1.db`` newest).

Jobs have a text id (``YYYYMMDD-HHMMSS-<crc>``, what the API uses) and an integer ``key`` that
the high-volume tables reference, which keeps sample rows small.
"""

import json
import logging
import os
import queue
import shutil
import sqlite3
import threading
import time

logger = logging.getLogger("qa.db")

DB_FILE = "qa.db"
BACKUP_FILES = ("qa.backup.1.db", "qa.backup.2.db")

SCHEMA_VERSION = 1

RESOLUTION_COARSE = 0
RESOLUTION_FINE = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    key INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    file_name TEXT,
    file_crc32 TEXT,
    started_at INTEGER NOT NULL,          -- epoch ms
    ended_at INTEGER,                     -- epoch ms
    result TEXT NOT NULL,                 -- running|completed|cancelled|aborted|unknown
    partial INTEGER NOT NULL DEFAULT 0,
    start_layer INTEGER,
    num_layers INTEGER,
    duration_s REAL,
    warmup_s REAL,
    pause_s REAL,
    material TEXT,
    context TEXT,                         -- JSON
    summary TEXT,                         -- JSON
    raw_pruned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_started ON jobs (started_at);
CREATE TABLE IF NOT EXISTS job_layers (
    job_key INTEGER NOT NULL REFERENCES jobs (key) ON DELETE CASCADE,
    layer INTEGER NOT NULL,
    started_at INTEGER,
    ended_at INTEGER,
    duration_s REAL,
    height REAL,
    z REAL,
    fraction_printed REAL,
    filament TEXT,
    flow TEXT,
    temps TEXT,
    fm_stats TEXT,
    pwm_stats TEXT,
    load_stats TEXT,
    PRIMARY KEY (job_key, layer)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS samples (
    job_key INTEGER NOT NULL,
    channel INTEGER NOT NULL,
    ts_ms INTEGER NOT NULL,
    value REAL,
    resolution INTEGER NOT NULL,
    block_id INTEGER,
    PRIMARY KEY (job_key, channel, ts_ms)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY,
    job_key INTEGER NOT NULL REFERENCES jobs (key) ON DELETE CASCADE,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER,
    triggers TEXT
);
CREATE INDEX IF NOT EXISTS blocks_job ON blocks (job_key, start_ms);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    job_key INTEGER NOT NULL REFERENCES jobs (key) ON DELETE CASCADE,
    ts_ms INTEGER NOT NULL,
    end_ms INTEGER,
    type TEXT NOT NULL,
    subtype TEXT,
    layer INTEGER,
    x REAL,
    y REAL,
    z REAL,
    positions TEXT,
    workplace INTEGER,
    offsets TEXT,
    tool INTEGER,
    object_id INTEGER,
    device INTEGER,
    payload TEXT,
    block_id INTEGER
);
CREATE INDEX IF NOT EXISTS events_job ON events (job_key, ts_ms);
CREATE INDEX IF NOT EXISTS events_type ON events (type, ts_ms);
CREATE TABLE IF NOT EXISTS timelapse (
    job_key INTEGER PRIMARY KEY REFERENCES jobs (key) ON DELETE CASCADE,
    status TEXT NOT NULL,                 -- queued|capturing|encoding|done|failed
    codec TEXT,
    path TEXT,
    frames INTEGER,
    fps REAL,
    size_bytes INTEGER,
    layer_frames TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS spectra (
    id INTEGER PRIMARY KEY,
    job_key INTEGER NOT NULL REFERENCES jobs (key) ON DELETE CASCADE,
    ts_ms INTEGER NOT NULL,
    layer INTEGER,
    board INTEGER,
    axis TEXT,
    sampling_rate REAL,
    n_samples INTEGER,
    freqs TEXT,
    amplitudes TEXT,
    peak_hz REAL,
    rms REAL,
    source TEXT
);
CREATE TABLE IF NOT EXISTS reference_spectra (
    axis TEXT PRIMARY KEY,
    spectrum_id INTEGER,
    mode TEXT NOT NULL,                   -- auto|manual
    set_at INTEGER
);
"""


def dumps(value):
    return None if value is None else json.dumps(value, separators=(",", ":"))


def loads(value):
    return None if value is None else json.loads(value)


def now_ms():
    return int(time.time() * 1000)


# --- Opening, quarantine, restore ---------------------------------------------------------------

def _quick_check(path):
    """True if the file is a readable SQLite database that passes quick_check."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
            return row is not None and row[0] == "ok"
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False


def _move_aside(path, suffix):
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(path + ext):
            os.replace(path + ext, f"{path}.corrupt.{suffix}{ext}")


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def prepare(directory):
    """Make sure ``directory/qa.db`` is healthy before it is opened for writing.

    Returns a short description of what happened (``ok``, ``created``, ``restored:<backup>``,
    ``replaced``) for the status endpoint and the console.
    """
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, DB_FILE)
    if not os.path.exists(path):
        return "created"
    if _quick_check(path):
        return "ok"
    stamp = int(time.time())
    logger.error("database %s failed quick_check, moved aside as %s.corrupt.%d", path, path, stamp)
    _move_aside(path, stamp)
    for name in BACKUP_FILES:
        backup = os.path.join(directory, name)
        if os.path.exists(backup) and _quick_check(backup):
            tmp = path + ".restore"
            shutil.copyfile(backup, tmp)
            with open(tmp, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            _fsync_dir(directory)
            logger.warning("database restored from %s", name)
            return f"restored:{name}"
    logger.warning("no usable backup, starting with an empty database")
    return "replaced"


def connect(path, readonly=False):
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10, check_same_thread=False)
    else:
        con = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=10000")
    if not readonly:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA foreign_keys=ON")
    return con


def migrate(con):
    con.executescript(SCHEMA)
    row = con.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    version = int(row[0]) if row else 0
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"database schema {version} is newer than this plugin ({SCHEMA_VERSION})")
    # Migrations from older versions go here, one step at a time
    if version < SCHEMA_VERSION:
        con.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)", (str(SCHEMA_VERSION),))


# --- Writer -------------------------------------------------------------------------------------

class _Barrier:
    def __init__(self):
        self.done = threading.Event()
        self.result = None


class Writer:
    """Owns the write connection; runs operations from a queue in its own thread.

    Operations are ``(name, args, urgent)``; ``name`` is a method ``op_<name>`` of this class.
    Urgent operations commit the open transaction right after they ran.
    """

    def __init__(self, directory, commit_interval_s=30):
        self.directory = directory
        self.path = os.path.join(directory, DB_FILE)
        self.commit_interval_s = commit_interval_s
        self._queue = queue.Queue()
        self._thread = None
        self._con = None
        self._in_tx = False
        self._tx_started = 0.0
        self._channel_ids = {}
        self.prepare_result = None
        self.last_error = None
        self.last_backup = None
        self.last_retention = None

    # public API, callable from any thread

    def start(self):
        self.prepare_result = prepare(self.directory)
        self._con = connect(self.path)
        migrate(self._con)
        self._load_channels()
        self._thread = threading.Thread(target=self._run, name="qa-db-writer", daemon=True)
        self._thread.start()
        return self.prepare_result

    def submit(self, name, *args, urgent=False):
        self._queue.put((name, args, urgent))

    def call(self, name, *args, timeout=60):
        """Run an operation and wait for its result (commits afterwards)."""
        barrier = _Barrier()
        self._queue.put((name, args, True, barrier))
        if not barrier.done.wait(timeout):
            raise TimeoutError(f"database operation {name} timed out")
        if isinstance(barrier.result, Exception):
            raise barrier.result
        return barrier.result

    def flush(self, timeout=60):
        """Commit everything queued so far."""
        return self.call("noop", timeout=timeout)

    def stop(self, timeout=60):
        if self._thread is None:
            return
        barrier = _Barrier()
        self._queue.put(("stop", (), True, barrier))
        barrier.done.wait(timeout)
        self._thread.join(timeout)
        self._thread = None

    def queue_size(self):
        return self._queue.qsize()

    # thread

    def _run(self):
        running = True
        while running:
            timeout = None
            if self._in_tx:
                timeout = max(0.0, self.commit_interval_s - (time.monotonic() - self._tx_started))
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                self._commit()
                continue
            name, args, urgent = item[0], item[1], item[2]
            barrier = item[3] if len(item) > 3 else None
            if name == "stop":
                self._commit()
                try:
                    self._con.close()
                finally:
                    running = False
                if barrier:
                    barrier.done.set()
                continue
            try:
                if name in ("backup", "integrity"):
                    self._commit()  # these run outside a transaction
                    result = getattr(self, f"op_{name}")(*args)
                else:
                    self._begin()
                    result = getattr(self, f"op_{name}")(*args)
                if barrier:
                    barrier.result = result
            except Exception as exc:  # noqa: BLE001 - one bad operation must not stop recording
                self.last_error = f"{name}: {exc}"
                logger.error("database operation %s failed: %s", name, exc)
                if barrier:
                    barrier.result = exc
            if urgent or barrier:
                self._commit()
            elif self._in_tx and time.monotonic() - self._tx_started >= self.commit_interval_s:
                self._commit()
            if barrier:
                barrier.done.set()

    def _begin(self):
        if not self._in_tx:
            self._con.execute("BEGIN IMMEDIATE")
            self._in_tx = True
            self._tx_started = time.monotonic()

    def _commit(self):
        if self._in_tx:
            try:
                self._con.execute("COMMIT")
            except sqlite3.Error as exc:
                self.last_error = f"commit: {exc}"
                logger.error("commit failed: %s", exc)
                try:
                    self._con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            self._in_tx = False

    def _load_channels(self):
        self._channel_ids = {row["name"]: row["id"] for row in self._con.execute("SELECT id, name FROM channels")}

    def _channel_id(self, name):
        cid = self._channel_ids.get(name)
        if cid is None:
            cur = self._con.execute("INSERT OR IGNORE INTO channels (name) VALUES (?)", (name,))
            if cur.lastrowid and cur.rowcount:
                cid = cur.lastrowid
            else:
                cid = self._con.execute("SELECT id FROM channels WHERE name=?", (name,)).fetchone()[0]
            self._channel_ids[name] = cid
        return cid

    # operations (writer thread only)

    def op_noop(self):
        return True

    def op_job_insert(self, job):
        """``job``: dict with the jobs columns (context as object). Returns the job key."""
        cur = self._con.execute(
            "INSERT INTO jobs (id, file_name, file_crc32, started_at, result, partial, start_layer, num_layers, "
            "material, context) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job["id"], job.get("file_name"), job.get("file_crc32"), job["started_at"], job.get("result", "running"),
             1 if job.get("partial") else 0, job.get("start_layer"), job.get("num_layers"), job.get("material"),
             dumps(job.get("context"))))
        return cur.lastrowid

    def op_job_update(self, job_key, fields):
        """Update columns of a job; JSON columns take objects."""
        if not fields:
            return
        columns = []
        values = []
        for column, value in fields.items():
            if column not in ("file_name", "file_crc32", "ended_at", "result", "partial", "start_layer", "num_layers",
                              "duration_s", "warmup_s", "pause_s", "material", "context", "summary", "raw_pruned"):
                raise ValueError(f"unknown job column {column}")
            columns.append(f"{column}=?")
            values.append(dumps(value) if column in ("context", "summary") else value)
        values.append(job_key)
        self._con.execute(f"UPDATE jobs SET {', '.join(columns)} WHERE key=?", values)

    def op_layer_upsert(self, job_key, layer):
        json_cols = ("filament", "flow", "temps", "fm_stats", "pwm_stats", "load_stats")
        self._con.execute(
            "INSERT OR REPLACE INTO job_layers (job_key, layer, started_at, ended_at, duration_s, height, z, "
            "fraction_printed, filament, flow, temps, fm_stats, pwm_stats, load_stats) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_key, layer["layer"], layer.get("started_at"), layer.get("ended_at"), layer.get("duration_s"),
             layer.get("height"), layer.get("z"), layer.get("fraction_printed"),
             *(dumps(layer.get(c)) for c in json_cols)))

    def op_samples(self, job_key, rows, resolution, block_id=None):
        """``rows``: iterable of ``(ts_ms, channel_name, value)``. A coarse sample never
        replaces a fine one at the same time stamp."""
        data = [(job_key, self._channel_id(name), ts, value, resolution, block_id)
                for ts, name, value in rows if value is not None]
        verb = "INSERT OR REPLACE" if resolution == RESOLUTION_FINE else "INSERT OR IGNORE"
        self._con.executemany(
            f"{verb} INTO samples (job_key, channel, ts_ms, value, resolution, block_id) VALUES (?,?,?,?,?,?)", data)
        return len(data)

    def op_block_insert(self, block_id, job_key, start_ms, end_ms, triggers):
        self._con.execute("INSERT INTO blocks (id, job_key, start_ms, end_ms, triggers) VALUES (?,?,?,?,?)",
                          (block_id, job_key, start_ms, end_ms, dumps(triggers)))

    def op_block_update(self, block_id, end_ms, triggers):
        self._con.execute("UPDATE blocks SET end_ms=?, triggers=? WHERE id=?", (end_ms, dumps(triggers), block_id))

    def op_event_insert(self, event):
        self._con.execute(
            "INSERT INTO events (id, job_key, ts_ms, end_ms, type, subtype, layer, x, y, z, positions, workplace, "
            "offsets, tool, object_id, device, payload, block_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event["id"], event["job_key"], event["ts_ms"], event.get("end_ms"), event["type"], event.get("subtype"),
             event.get("layer"), event.get("x"), event.get("y"), event.get("z"), dumps(event.get("positions")),
             event.get("workplace"), dumps(event.get("offsets")), event.get("tool"), event.get("object_id"),
             event.get("device"), dumps(event.get("payload")), event.get("block_id")))

    def op_event_update(self, event_id, fields):
        columns = []
        values = []
        for column, value in fields.items():
            if column not in ("end_ms", "subtype", "payload", "block_id"):
                raise ValueError(f"unknown event column {column}")
            columns.append(f"{column}=?")
            values.append(dumps(value) if column == "payload" else value)
        values.append(event_id)
        self._con.execute(f"UPDATE events SET {', '.join(columns)} WHERE id=?", values)

    def op_timelapse_upsert(self, job_key, fields):
        row = self._con.execute("SELECT job_key FROM timelapse WHERE job_key=?", (job_key,)).fetchone()
        allowed = ("status", "codec", "path", "frames", "fps", "size_bytes", "layer_frames", "error")
        for column in fields:
            if column not in allowed:
                raise ValueError(f"unknown timelapse column {column}")
        values = {k: (dumps(v) if k == "layer_frames" else v) for k, v in fields.items()}
        if row is None:
            values.setdefault("status", "queued")
            cols = ", ".join(["job_key", *values])
            marks = ", ".join("?" * (len(values) + 1))
            self._con.execute(f"INSERT INTO timelapse ({cols}) VALUES ({marks})", (job_key, *values.values()))
        elif values:
            sets = ", ".join(f"{k}=?" for k in values)
            self._con.execute(f"UPDATE timelapse SET {sets} WHERE job_key=?", (*values.values(), job_key))

    def op_next_ids(self):
        """Highest event and block ids, so the collector can hand out new ones."""
        event = self._con.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        block = self._con.execute("SELECT COALESCE(MAX(id), 0) FROM blocks").fetchone()[0]
        return event, block

    def op_running_jobs(self):
        return [dict(row) for row in self._con.execute(
            "SELECT key, id, file_name, file_crc32, started_at, start_layer, partial FROM jobs WHERE result='running'")]

    def op_job_context(self, job_key):
        row = self._con.execute("SELECT context FROM jobs WHERE key=?", (job_key,)).fetchone()
        return loads(row[0]) if row else None

    def op_last_sample_ms(self, job_key):
        row = self._con.execute("SELECT MAX(ts_ms) FROM samples WHERE job_key=?", (job_key,)).fetchone()
        event = self._con.execute("SELECT MAX(COALESCE(end_ms, ts_ms)) FROM events WHERE job_key=?",
                                  (job_key,)).fetchone()
        values = [v for v in (row[0], event[0]) if v is not None]
        return max(values) if values else None

    def op_integrity(self):
        rows = self._con.execute("PRAGMA integrity_check").fetchall()
        return [r[0] for r in rows]

    def op_backup(self):
        """integrity_check, then VACUUM INTO a new backup and rotate. Returns the backup path or None."""
        problems = self.op_integrity()
        if problems != ["ok"]:
            self.last_error = f"integrity_check: {problems[:3]}"
            logger.error("integrity_check failed, no backup made: %s", problems[:3])
            return None
        newest = os.path.join(self.directory, BACKUP_FILES[0])
        tmp = os.path.join(self.directory, "qa.backup.new.db")
        if os.path.exists(tmp):
            os.remove(tmp)
        self._con.execute("VACUUM INTO ?", (tmp,))
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        for index in range(len(BACKUP_FILES) - 1, 0, -1):
            older = os.path.join(self.directory, BACKUP_FILES[index - 1])
            if os.path.exists(older):
                os.replace(older, os.path.join(self.directory, BACKUP_FILES[index]))
        os.replace(tmp, newest)
        _fsync_dir(self.directory)
        self.last_backup = now_ms()
        return newest

    def op_retention(self, keep_jobs, keep_days, max_bytes, now=None):
        """Drop raw data (samples, blocks) of old jobs. A job keeps its raw data while it is one of
        the ``keep_jobs`` newest **or** younger than ``keep_days``; then, while the database is
        larger than ``max_bytes``, the oldest remaining raw data goes. Summaries, layers, events,
        spectra stay. Returns the ids of the jobs pruned."""
        now = now if now is not None else now_ms()
        cutoff = now - int(keep_days * 86400 * 1000)
        rows = self._con.execute(
            "SELECT key, id, started_at, result FROM jobs WHERE raw_pruned=0 ORDER BY started_at DESC").fetchall()
        newest = {row["key"] for row in self._con.execute(
            "SELECT key FROM jobs ORDER BY started_at DESC LIMIT ?", (keep_jobs,))}
        pruned = []
        for row in rows:
            if row["result"] == "running":
                continue
            if row["key"] in newest or row["started_at"] >= cutoff:
                continue
            self._prune_job(row["key"])
            pruned.append(row["id"])
        # size cap, oldest first
        remaining = [row for row in reversed(rows) if row["id"] not in pruned and row["result"] != "running"]
        while remaining and self.used_bytes() > max_bytes:
            row = remaining.pop(0)
            self._prune_job(row["key"])
            pruned.append(row["id"])
        self.last_retention = {"at": now, "pruned": pruned}
        return pruned

    def _prune_job(self, job_key):
        self._con.execute("DELETE FROM samples WHERE job_key=?", (job_key,))
        self._con.execute("DELETE FROM blocks WHERE job_key=?", (job_key,))
        self._con.execute("UPDATE events SET block_id=NULL WHERE job_key=?", (job_key,))
        self._con.execute("UPDATE jobs SET raw_pruned=1 WHERE key=?", (job_key,))

    def op_checkpoint(self):
        self._commit()
        return self._con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]

    def used_bytes(self):
        page_size = self._con.execute("PRAGMA page_size").fetchone()[0]
        pages = self._con.execute("PRAGMA page_count").fetchone()[0]
        free = self._con.execute("PRAGMA freelist_count").fetchone()[0]
        return (pages - free) * page_size

    def op_used_bytes(self):
        return self.used_bytes()


def file_size(directory):
    """Size of the database and its WAL on disk."""
    path = os.path.join(directory, DB_FILE)
    total = 0
    for ext in ("", "-wal"):
        try:
            total += os.path.getsize(path + ext)
        except OSError:
            pass
    return total


class Readers:
    """One read-only connection per thread (endpoint threads, tests)."""

    def __init__(self, directory):
        self.path = os.path.join(directory, DB_FILE)
        self._local = threading.local()

    def get(self):
        con = getattr(self._local, "con", None)
        if con is None:
            con = connect(self.path, readonly=True)
            self._local.con = con
        return con
