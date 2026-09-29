"""Object model journal (qa_journal): DSF's patches and snapshots per job in one gzip file, replayed to the
model at any time and to the history of one value (Tim 2026-09-29: so problems nobody thought of can
still be investigated)."""

import gzip
import json
import os

import pytest

import qa_api
import qa_collector
import qa_db
import qa_journal

from conftest import BASE_MODEL

T0 = 1_700_000_000_000
JOB = "20260929-085035-adb6b4f6"


def envelope(model):
    """DSF's answer to GetObjectModel (IPC/Connection.cs:698-703)"""
    return '{"success":true,"result":' + json.dumps(model, separators=(",", ":")) + "}"


class Model:
    """What DSF would answer: the model the test has set up"""

    def __init__(self, model):
        self.model = model
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return envelope(self.model)


@pytest.fixture
def journal(data_dir, settings):
    snapshot = Model({"state": {"status": "processing"}, "global": {"machine_mode": "automatic"},
                      "move": {"axes": [{"letter": "X", "machinePosition": 1.25}, {"letter": "Y", "machinePosition": 2}]},
                      "messages": []})
    writer = qa_journal.Writer(data_dir, settings, snapshot)
    writer.model = snapshot
    return writer


def directory(data_dir, job_id=JOB):
    return qa_journal.job_dir(data_dir, job_id)


def lines(data_dir, job_id=JOB):
    with gzip.open(os.path.join(directory(data_dir, job_id), qa_journal.DATA_FILE), "rt") as handle:
        return [json.loads(line) for line in handle]


def test_merge_follows_dsfs_patch_rules():
    """ModelSubscription.cs: only what changed; a list has the new length and {} for an unchanged object"""
    state = {"move": {"axes": [{"letter": "X", "machinePosition": 1.0}, {"letter": "Y", "machinePosition": 2.0},
                               {"letter": "Z", "machinePosition": 3.0}],
                      "kinematics": {"tiltCorrection": {"lastCorrections": [0.1, 0.2, 0.3, 0.4]}}},
             "tools": [{"number": 0}], "job": {"file": {"fileName": "a.gcode"}}}
    qa_journal.merge(state, {"move": {"axes": [{}, {"machinePosition": 2.5}],
                                      "kinematics": {"tiltCorrection": {"lastCorrections": [-0.1, 0.0, 0.1, 0.2]}}},
                             "tools": [{}, {"number": 1}], "job": {"file": None}})
    assert state == {"move": {"axes": [{"letter": "X", "machinePosition": 1.0}, {"letter": "Y", "machinePosition": 2.5}],
                              "kinematics": {"tiltCorrection": {"lastCorrections": [-0.1, 0.0, 0.1, 0.2]}}},
                     "tools": [{"number": 0}, {"number": 1}], "job": {"file": None}}


def test_parse_and_get_path():
    assert qa_journal.parse_path("move.axes[2].machinePosition") == ["move", "axes", 2, "machinePosition"]
    assert qa_journal.parse_path("") == []
    assert qa_journal.get_path({"a": [{"b": 1}]}, ["a", 0, "b"]) == 1
    assert qa_journal.get_path({"a": []}, ["a", 3]) is None


def test_a_job_journal_is_one_gzip_file_with_snapshots_and_patches(journal, data_dir):
    journal.job_started(JOB, T0)
    journal.patch('{"move":{"axes":[{"machinePosition":1.2345678901}]}}', T0 + 1000)
    journal.patch('{"global":{"machine_mode":"default"}}\n', T0 + 2000)
    journal.tick(T0 + 30_000)
    assert len(qa_journal.read_index(directory(data_dir))) == 1        # patches wait for their minute
    journal.patch('{"state":{"status":"paused"}}', T0 + 61_000)          # the minute is full: written
    journal.tick(T0 + 3_600_000)                                          # the interval passed: a snapshot
    journal.job_finished(T0 + 3_700_000)
    assert journal.job_id is None and journal.model.calls == 3
    entries = lines(data_dir)
    assert [(e["t"], next(k for k in e if k != "t")) for e in entries] == [
        (T0, "snapshot"), (T0 + 1000, "patch"), (T0 + 2000, "patch"), (T0 + 61_000, "patch"),
        (T0 + 3_600_000, "snapshot"), (T0 + 3_700_000, "snapshot")]
    # DSF's text as it came: no float loses digits (dsf-python's json.dumps would write %g)
    assert entries[1]["patch"]["move"]["axes"][0]["machinePosition"] == 1.2345678901
    index = qa_journal.read_index(directory(data_dir))
    assert [(e["start"], e["end"], e["snapshot"]) for e in index] == [
        (T0, T0, True), (T0 + 1000, T0 + 61_000, False), (T0 + 3_600_000, T0 + 3_600_000, True),
        (T0 + 3_700_000, T0 + 3_700_000, True)]
    assert qa_journal.info(directory(data_dir))["snapshots"] == 3


def test_state_at_a_time_and_history_of_a_value(journal, data_dir):
    journal.job_started(JOB, T0)
    journal.patch('{"global":{"machine_mode":"default"},"messages":[{"content":"door open","type":1}]}', T0 + 1000)
    journal.patch('{"move":{"axes":[{},{"machinePosition":5}]}}', T0 + 2000)
    journal.patch('{"move":{"axes":[{"machinePosition":7},{}]}}', T0 + 3000)   # DSF sends the list's full length
    journal.patch('{"global":{"machine_mode":"automatic"}}', T0 + 4000)
    journal.shutdown()
    folder = directory(data_dir)

    at = qa_journal.state_at(folder, T0 + 2500)
    assert (at["at"], at["snapshotAt"]) == (T0 + 2500, T0)
    assert at["value"]["global"]["machine_mode"] == "default"
    assert at["value"]["move"]["axes"] == [{"letter": "X", "machinePosition": 1.25}, {"letter": "Y", "machinePosition": 5}]
    assert at["value"]["messages"] == [{"content": "door open", "type": 1}]
    assert qa_journal.state_at(folder, None, "move.axes[0].machinePosition")["value"] == 7
    assert qa_journal.state_at(folder, T0 - 1) is None

    mode = qa_journal.history(folder, "global.machine_mode")
    assert mode == {"path": "global.machine_mode", "truncated": False, "points": [
        {"t": T0, "value": "automatic"}, {"t": T0 + 1000, "value": "default"}, {"t": T0 + 4000, "value": "automatic"}]}
    axes = qa_journal.history(folder, "move.axes[1]", start=T0 + 2500)
    assert axes["points"] == [{"t": T0 + 2000, "value": {"letter": "Y", "machinePosition": 5}}]   # its value then
    assert qa_journal.history(folder, "move.axes", limit=2)["truncated"] is True
    assert qa_journal.history(folder, "messages")["points"] == [{"t": T0 + 1000, "value": {"content": "door open", "type": 1}}]
    with pytest.raises(ValueError):
        qa_journal.history(folder, "")


def test_a_shorter_list_ends_the_value(journal, data_dir):
    journal.job_started(JOB, T0)
    journal.patch('{"move":{"axes":[{}]}}', T0 + 1000)
    journal.shutdown()
    points = qa_journal.history(directory(data_dir), "move.axes[1].machinePosition")["points"]
    assert points == [{"t": T0, "value": 2}, {"t": T0 + 1000, "value": None}]


def test_resume_cuts_a_half_written_member_off(journal, data_dir):
    journal.job_started(JOB, T0)
    journal.patch('{"state":{"status":"paused"}}', T0 + 1000)
    journal.shutdown()
    with open(os.path.join(directory(data_dir), qa_journal.DATA_FILE), "ab") as handle:
        handle.write(gzip.compress(b'{"t":1,"patch":{}}\n')[:10])       # the daemon died while writing
    with open(os.path.join(directory(data_dir), qa_journal.INDEX_FILE), "a") as handle:
        handle.write('{"start":1,"end"')
    journal.job_started(JOB, T0 + 10_000)                                  # the collector resumes the job
    journal.patch('{"state":{"status":"processing"}}', T0 + 11_000)
    journal.job_finished(T0 + 12_000)
    assert [e["t"] for e in lines(data_dir)] == [T0, T0 + 1000, T0 + 10_000, T0 + 11_000, T0 + 12_000]
    assert qa_journal.state_at(directory(data_dir))["value"]["state"]["status"] == "processing"


def test_without_a_snapshot_the_patches_still_count(data_dir, settings, caplog):
    def broken():
        raise ConnectionError("DSF gone")

    journal = qa_journal.Writer(data_dir, settings, broken)
    journal.job_started(JOB, T0)
    journal.patch('{"global":{"machine_mode":"default"}}', T0 + 1000)
    journal.job_finished(T0 + 2000)
    state = qa_journal.state_at(directory(data_dir))
    assert state["snapshotAt"] is None and state["value"]["global"] == {"machine_mode": "default"}
    assert sum("snapshot failed" in r.getMessage() for r in caplog.records) == 1


def test_disabled_writes_nothing(journal, data_dir, settings):
    settings.update({"journal": {"enabled": False}})
    journal.job_started(JOB, T0)
    journal.patch('{"state":{}}', T0 + 1000)
    journal.job_finished(T0 + 2000)
    assert not os.path.exists(os.path.join(data_dir, "journal"))


def test_retention_follows_the_raw_data_and_a_size_cap(journal, data_dir):
    a, b, c = "20260901-000000-00000001", "20260902-000000-00000002", "20260903-000000-00000003"
    for i, job_id in enumerate((a, b, c)):
        journal.job_started(job_id, T0 + i)
        journal.job_finished(T0 + i + 1)
    journal.job_started(JOB, T0 + 10)                                       # the running job
    root = os.path.join(data_dir, "journal")

    def size(job_id):
        return sum(os.path.getsize(os.path.join(root, job_id, f)) for f in os.listdir(os.path.join(root, job_id)))

    assert journal.retention({b, c}, max_bytes=10 ** 9) == [a]              # its raw data is gone
    assert journal.retention({b, c}, max_bytes=size(c) + size(JOB)) == [b]  # too large: the oldest goes
    assert journal.retention(set(), max_bytes=0) == [c]                    # the running job's always stays
    assert os.listdir(root) == [JOB]


def test_the_collector_starts_and_ends_the_journal(rig, data_dir, settings):
    journal = qa_journal.Writer(data_dir, settings, Model(BASE_MODEL))
    rig.collector.journal = journal
    rig.start_job()
    job_id = rig.collector.job.id
    assert journal.job_id == job_id
    rig.patch({"state": {"status": "idle"}, "job": {"duration": None}})
    rig.collector.resolve_pending_end(rig.t, force=True)
    assert journal.job_id is None
    kinds = [next(k for k in e if k != "t") for e in lines(data_dir, job_id)]
    assert kinds == ["snapshot", "snapshot"]


def test_api_serves_the_journal(rig, data_dir, settings, writer, journal):
    ctx = qa_api.ApiContext(version="test", settings=settings, writer=writer, readers=qa_db.Readers(data_dir),
                            data_dir=data_dir, collector=rig.collector)
    rig.collector.journal = journal
    rig.start_job()
    job_id = rig.collector.job.id
    journal.patch('{"global":{"machine_mode":"default"}}', rig.t + 500)
    journal.shutdown()

    from test_api import Req

    def get(endpoint, **queries):
        response = qa_api.call(ctx, qa_api.ENDPOINTS[("GET", endpoint)], Req(**queries))
        return response.status, response.body

    status, job = get("job", id=job_id)
    assert status == 200 and job["journal"]["snapshots"] == 1 and job["journal"]["start"] == rig.t
    status, body = get("job/om", id=job_id, path="global.machine_mode")
    assert (status, body["value"]) == (200, "default")
    status, body = get("job/om", id=job_id, at=rig.t, path="global")
    assert body["value"] == {"machine_mode": "automatic"}
    status, body = get("job/om/history", id=job_id, path="global.machine_mode")
    assert [p["value"] for p in body["points"]] == ["automatic", "default"]
    response = qa_api.call(ctx, qa_api.ENDPOINTS[("GET", "job/om/journal")], Req(id=job_id))
    assert response.kind == "file" and response.body.endswith(".jsonl.gz")
    assert get("job/om/history", id=job_id)[0] == 400
    assert get("job/om", id=job_id, at="soon")[0] == 400
    assert get("job/om", id="../../etc")[0] == 400
    assert get("job/om", id="20200101-000000-00000000")[0] == 404
    assert qa_api.call(ctx, qa_api.ENDPOINTS[("GET", "job/om")], Req(id=job_id, session_id=-1)).status == 401


def test_collector_without_journal_still_works(writer, settings):
    collector = qa_collector.Collector(writer, settings)
    assert collector.journal is None
