import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import qa_db

DSF_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dsf")


def job_record(job_id="20260926-100000-00000001", started=1_000_000):
    return {"id": job_id, "file_name": "0:/gcodes/a.gcode", "file_crc32": "deadbeef", "started_at": started,
            "context": {"file": {"fileName": "0:/gcodes/a.gcode"}}}


def test_schema_and_job_roundtrip(writer, readers):
    key = writer.call("job_insert", job_record())
    writer.submit("job_update", key, {"result": "completed", "summary": {"a": 1}}, urgent=True)
    writer.flush()
    row = readers.get().execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
    assert row["result"] == "completed"
    assert qa_db.loads(row["summary"]) == {"a": 1}
    assert qa_db.loads(row["context"])["file"]["fileName"] == "0:/gcodes/a.gcode"
    assert readers.get().execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == "1"


def test_samples_wait_for_the_commit_interval(data_dir):
    w = qa_db.Writer(data_dir, commit_interval_s=30)
    w.start()
    try:
        key = w.call("job_insert", job_record())
        w.submit("samples", key, [(1, "heater.1.current", 200.0)], qa_db.RESOLUTION_COARSE)
        time.sleep(0.2)
        reader = qa_db.Readers(data_dir).get()
        assert reader.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
        # an urgent operation (an event) commits everything queued before it
        w.submit("event_insert", {"id": 1, "job_key": key, "ts_ms": 2, "type": "pause"}, urgent=True)
        w.flush()
        assert reader.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
        assert reader.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        w.stop()


def test_commit_after_interval(data_dir):
    w = qa_db.Writer(data_dir, commit_interval_s=0.2)
    w.start()
    try:
        key = w.call("job_insert", job_record())
        w.submit("samples", key, [(1, "a", 1.0), (2, "a", 2.0)], qa_db.RESOLUTION_COARSE)
        time.sleep(0.6)
        assert qa_db.Readers(data_dir).get().execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 2
    finally:
        w.stop()


@pytest.mark.parametrize("fine_first", [True, False])
def test_coincident_samples_share_a_row(writer, readers, fine_first):
    """Same snapshot, same time stamp: the row stays on the coarse grid and joins the block."""
    key = writer.call("job_insert", job_record())
    fine = ("samples", key, [(10, "c", 1.0)], qa_db.RESOLUTION_FINE, 7)
    coarse = ("samples", key, [(10, "c", 1.0)], qa_db.RESOLUTION_COARSE)
    for op in ((fine, coarse) if fine_first else (coarse, fine)):
        writer.submit(*op)
    writer.submit("samples", key, [(11, "c", 1.5)], qa_db.RESOLUTION_FINE, 7)
    writer.flush()
    rows = [tuple(r) for r in readers.get().execute("SELECT ts_ms, value, resolution, block_id FROM samples ORDER BY ts_ms")]
    assert rows == [(10, 1.0, qa_db.RESOLUTION_COARSE, 7), (11, 1.5, qa_db.RESOLUTION_FINE, 7)]


def test_channel_ids_survive_restart(data_dir):
    w = qa_db.Writer(data_dir)
    w.start()
    key = w.call("job_insert", job_record())
    w.submit("samples", key, [(1, "x", 1.0), (1, "y", 2.0)], qa_db.RESOLUTION_COARSE)
    w.stop()
    w = qa_db.Writer(data_dir)
    w.start()
    try:
        w.submit("samples", key, [(2, "y", 3.0)], qa_db.RESOLUTION_COARSE)
        w.flush()
        con = qa_db.Readers(data_dir).get()
        assert con.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM samples WHERE channel=(SELECT id FROM channels WHERE name='y')").fetchone()[0] == 2
    finally:
        w.stop()


def test_corrupt_database_is_quarantined_and_restored(data_dir):
    w = qa_db.Writer(data_dir)
    w.start()
    w.call("job_insert", job_record())
    assert w.call("backup").endswith("qa.backup.1.db")
    w.call("job_insert", job_record("20260926-110000-00000002", 2_000_000))
    w.stop()
    path = os.path.join(data_dir, qa_db.DB_FILE)
    with open(path, "r+b") as handle:  # destroy the header
        handle.write(b"garbage" * 20)
    w = qa_db.Writer(data_dir)
    try:
        assert w.start() == "restored:qa.backup.1.db"
        ids = [r[0] for r in qa_db.Readers(data_dir).get().execute("SELECT id FROM jobs")]
        assert ids == ["20260926-100000-00000001"]  # the state of the backup
        assert any(name.startswith("qa.db.corrupt.") for name in os.listdir(data_dir))
    finally:
        w.stop()


def test_corrupt_without_backup_starts_empty(data_dir):
    with open(os.path.join(data_dir, qa_db.DB_FILE), "wb") as handle:
        handle.write(b"not a database at all" * 100)
    w = qa_db.Writer(data_dir)
    try:
        assert w.start() == "replaced"
        assert qa_db.Readers(data_dir).get().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        w.stop()


def test_backup_rotation(writer, data_dir):
    writer.call("job_insert", job_record())
    writer.call("backup")
    writer.call("job_insert", job_record("20260926-110000-00000002", 2_000_000))
    writer.call("backup")
    one = os.path.join(data_dir, "qa.backup.1.db")
    two = os.path.join(data_dir, "qa.backup.2.db")
    count = lambda p: sqlite3.connect(p).execute("SELECT COUNT(*) FROM jobs").fetchone()[0]  # noqa: E731
    assert count(one) == 2
    assert count(two) == 1


def test_retention_keeps_newest_or_young_jobs(writer, readers):
    day = 86400 * 1000
    now = 400 * day
    keys = {}
    # five jobs: three old (200, 250, 300 days back), two young
    for i, age in enumerate((300, 250, 200, 10, 1)):
        record = job_record(f"job{i}", now - age * day)
        keys[i] = writer.call("job_insert", record)
        writer.submit("job_update", keys[i], {"result": "completed"})
        writer.submit("samples", keys[i], [(now - age * day, "a", 1.0)], qa_db.RESOLUTION_COARSE)
    writer.flush()
    pruned = writer.call("retention", 3, 90, 10 ** 12, now)
    # the three newest (job2..job4) and everything younger than 90 days stay
    assert sorted(pruned) == ["job0", "job1"]
    con = readers.get()
    assert con.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 3
    assert {r[0] for r in con.execute("SELECT id FROM jobs WHERE raw_pruned=1")} == {"job0", "job1"}


def test_retention_size_cap_prunes_oldest_first(writer, readers):
    keys = []
    for i in range(3):
        key = writer.call("job_insert", job_record(f"job{i}", 1000 + i))
        writer.submit("job_update", key, {"result": "completed"})
        writer.submit("samples", key, [(t, "a", float(t)) for t in range(3000)], qa_db.RESOLUTION_COARSE)
        keys.append(key)
    writer.flush()
    used = writer.call("used_bytes")
    pruned = writer.call("retention", 100, 100000, used - 1, 2000)
    assert pruned[0] == "job0"


def test_running_job_is_never_pruned(writer):
    key = writer.call("job_insert", job_record("running", 1))
    writer.submit("samples", key, [(1, "a", 1.0)], qa_db.RESOLUTION_COARSE)
    writer.flush()
    assert writer.call("retention", 0, 0, 1, 10 ** 12) == []


def test_parallel_readers_while_writing(writer, data_dir):
    key = writer.call("job_insert", job_record())
    errors = []
    readers = qa_db.Readers(data_dir)

    def read():
        try:
            for _ in range(50):
                readers.get().execute("SELECT COUNT(*) FROM samples").fetchone()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(4)]
    for t in threads:
        t.start()
    for i in range(50):
        writer.submit("samples", key, [(i, "a", 1.0)], qa_db.RESOLUTION_COARSE, urgent=True)
    for t in threads:
        t.join()
    writer.flush()
    assert errors == []


def test_killed_mid_transaction_keeps_the_last_commit(data_dir):
    """A power cut while samples are being written: the file opens, quick_check passes, and
    everything up to the last commit (events commit at once) is there."""
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {DSF_DIR!r})
        import qa_db
        w = qa_db.Writer({data_dir!r}, commit_interval_s=3600)
        w.start()
        key = w.call("job_insert", {{"id": "j", "started_at": 1}})
        w.submit("event_insert", {{"id": 1, "job_key": key, "ts_ms": 5, "type": "job_start"}}, urgent=True)
        w.flush()
        print("ready", flush=True)
        i = 0
        while True:
            w.submit("samples", key, [(10 + i, "a", float(i))], qa_db.RESOLUTION_COARSE)
            i += 1
            if i % 1000 == 0:
                time.sleep(0.01)
    """)
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        time.sleep(0.5)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert qa_db.prepare(data_dir) == "ok"
    con = qa_db.connect(os.path.join(data_dir, qa_db.DB_FILE))
    assert con.execute("SELECT type FROM events").fetchall()[0][0] == "job_start"
    assert con.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0  # never committed
    con.close()
