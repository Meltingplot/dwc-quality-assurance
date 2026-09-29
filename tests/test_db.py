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


def schema(con):
    return dict(con.execute("SELECT key, value FROM schema_meta").fetchall())


def test_schema_and_job_roundtrip(writer, readers):
    key = writer.call("job_insert", job_record())
    writer.submit("job_update", key, {"result": "completed", "summary": {"a": 1}}, urgent=True)
    writer.flush()
    row = readers.get().execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
    assert row["result"] == "completed"
    assert qa_db.loads(row["summary"]) == {"a": 1}
    assert qa_db.loads(row["context"])["file"]["fileName"] == "0:/gcodes/a.gcode"
    assert schema(readers.get()) == {"version": "1", "layout": "3"}


def test_migration_from_version_1(tmp_path):
    """Version 1 had no job_layers.filament_path; its layers stay."""
    con = sqlite3.connect(str(tmp_path / "v1.db"))
    con.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO schema_meta VALUES ('version', '1');
        CREATE TABLE job_layers (job_key INTEGER NOT NULL, layer INTEGER NOT NULL, started_at INTEGER,
            ended_at INTEGER, duration_s REAL, height REAL, z REAL, fraction_printed REAL, filament TEXT, flow TEXT,
            temps TEXT, fm_stats TEXT, pwm_stats TEXT, load_stats TEXT, PRIMARY KEY (job_key, layer)) WITHOUT ROWID;
        INSERT INTO job_layers (job_key, layer, duration_s) VALUES (1, 1, 12.5);
    """)
    qa_db.migrate(con)
    qa_db.migrate(con)   # again: nothing to do
    row = con.execute("SELECT layer, duration_s, filament_path, feed FROM job_layers").fetchone()
    assert row == (1, 12.5, None, None)
    assert schema(con) == {"version": "1", "layout": "3"}


def test_migration_from_version_2(tmp_path):
    """Version 2 had no job_layers.feed; its layers and their filament path stay."""
    con = sqlite3.connect(str(tmp_path / "v2.db"))
    con.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO schema_meta VALUES ('version', '2');
        CREATE TABLE job_layers (job_key INTEGER NOT NULL, layer INTEGER NOT NULL, started_at INTEGER,
            ended_at INTEGER, duration_s REAL, height REAL, z REAL, fraction_printed REAL, filament TEXT, flow TEXT,
            temps TEXT, fm_stats TEXT, pwm_stats TEXT, load_stats TEXT, filament_path TEXT,
            PRIMARY KEY (job_key, layer)) WITHOUT ROWID;
        INSERT INTO job_layers (job_key, layer, filament_path) VALUES (1, 1, '{"gearPasses": 2.2}');
    """)
    qa_db.migrate(con)
    row = con.execute("SELECT layer, filament_path, feed FROM job_layers").fetchone()
    assert row == (1, '{"gearPasses": 2.2}', None)
    assert schema(con) == {"version": "1", "layout": "3"}


def test_version_of_rc2_becomes_the_oldest_version_again(writer, data_dir):
    """0.1.0-rc.2 (and a sideload before it) kept the layout in `version`, which 0.1.0-rc.1 refuses
    (`version > 1`: the crash loop on the lab CHX 350, 2026-09-29); the next start makes it 1 again."""
    writer.stop()
    path = os.path.join(data_dir, qa_db.DB_FILE)
    con = sqlite3.connect(path)
    con.execute("DELETE FROM schema_meta WHERE key='layout'")
    con.execute("UPDATE schema_meta SET value='3' WHERE key='version'")
    con.commit()
    writer.start()
    assert schema(qa_db.connect(path, readonly=True)) == {"version": "1", "layout": "3"}


def set_schema(path, version, layout):
    con = sqlite3.connect(path)
    con.executemany("INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                    (("version", str(version)), ("layout", str(layout))))
    con.commit()
    con.close()


def job_ids(path):
    con = sqlite3.connect(path)
    try:
        return [row[0] for row in con.execute("SELECT id FROM jobs ORDER BY key")]
    finally:
        con.close()


def test_newer_layout_that_allows_this_plugin_is_used_as_it_is(writer, data_dir):
    """A newer QA that only added a column leaves `version` at 1: an older one writes its rows and
    leaves the newer layout alone."""
    writer.stop()
    path = os.path.join(data_dir, qa_db.DB_FILE)
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE job_layers ADD COLUMN future TEXT")
    con.commit()
    con.close()
    set_schema(path, 1, qa_db.SCHEMA_VERSION + 2)
    assert writer.start() == "ok"
    key = writer.call("job_insert", job_record())
    writer.call("layer_upsert", key, {"layer": 1, "duration_s": 3.0})
    writer.stop()
    con = sqlite3.connect(path)
    assert con.execute("SELECT duration_s, future FROM job_layers").fetchone() == (3.0, None)
    assert schema(con) == {"version": "1", "layout": str(qa_db.SCHEMA_VERSION + 2)}
    con.close()
    writer.start()


def test_database_too_new_is_set_aside_for_the_newest_usable_backup(data_dir):
    path = os.path.join(data_dir, qa_db.DB_FILE)
    w = qa_db.Writer(data_dir)
    w.start()
    w.call("job_insert", job_record())
    w.call("backup")
    w.call("job_insert", job_record("20260926-110000-00000002", 2_000_000))
    w.call("backup")
    w.call("job_insert", job_record("20260926-120000-00000003", 3_000_000))
    w.stop()
    # a newer QA migrated the database and made one backup; the older backup is still usable
    newer = qa_db.SCHEMA_VERSION + 1
    set_schema(path, newer, newer)
    set_schema(os.path.join(data_dir, "qa.backup.1.db"), newer, newer)
    w = qa_db.Writer(data_dir)
    try:
        assert w.start() == "downgraded:qa.backup.2.db"
        assert job_ids(path) == ["20260926-100000-00000001"]
        aside = [name for name in os.listdir(data_dir) if name.startswith("qa.db.newer.") and "-" not in name]
        assert len(aside) == 1   # with its -wal/-shm, if there were any
        assert len(job_ids(os.path.join(data_dir, aside[0]))) == 3
        assert os.path.exists(os.path.join(data_dir, "qa.backup.1.db"))  # kept for the newer QA
    finally:
        w.stop()


def test_database_too_new_without_usable_backup_starts_empty(data_dir):
    path = os.path.join(data_dir, qa_db.DB_FILE)
    w = qa_db.Writer(data_dir)
    w.start()
    w.call("job_insert", job_record())
    w.stop()
    set_schema(path, qa_db.SCHEMA_VERSION + 1, qa_db.SCHEMA_VERSION + 1)
    w = qa_db.Writer(data_dir)
    try:
        assert w.start() == "downgraded"
        assert job_ids(path) == []
        assert schema(sqlite3.connect(path)) == {"version": "1", "layout": str(qa_db.SCHEMA_VERSION)}
    finally:
        w.stop()


def test_copy_before_a_migration_older_versions_cannot_follow(data_dir, monkeypatch):
    """A later layout that an older QA would misread keeps the database as it was for that QA, and
    that QA falls back to the copy."""
    path = os.path.join(data_dir, qa_db.DB_FILE)
    old_version = qa_db.SCHEMA_VERSION
    w = qa_db.Writer(data_dir)
    w.start()
    w.call("job_insert", job_record())
    w.stop()

    monkeypatch.setattr(qa_db, "SCHEMA_VERSION", old_version + 1)
    monkeypatch.setattr(qa_db, "SCHEMA_COMPATIBLE", old_version + 1)
    w = qa_db.Writer(data_dir)
    assert w.start() == "ok"
    w.call("job_insert", job_record("20260926-110000-00000002", 2_000_000))
    w.stop()
    copy = os.path.join(data_dir, f"qa.backup.schema{old_version}.db")
    assert schema(sqlite3.connect(copy)) == {"version": "1", "layout": str(old_version)}
    assert schema(sqlite3.connect(path)) == {"version": str(old_version + 1), "layout": str(old_version + 1)}

    monkeypatch.setattr(qa_db, "SCHEMA_VERSION", old_version)
    monkeypatch.setattr(qa_db, "SCHEMA_COMPATIBLE", 1)
    w = qa_db.Writer(data_dir)
    try:
        assert w.start() == f"downgraded:qa.backup.schema{old_version}.db"
        assert job_ids(path) == ["20260926-100000-00000001"]
    finally:
        w.stop()


def test_migrate_refuses_a_database_too_new(tmp_path):
    con = sqlite3.connect(str(tmp_path / "new.db"))
    qa_db.migrate(con)
    con.execute("UPDATE schema_meta SET value='99' WHERE key IN ('version', 'layout')")
    with pytest.raises(qa_db.SchemaTooNew):
        qa_db.migrate(con)


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
