"""qa-daemon.py main loop with fake DSF connections (the model itself is real dsf-python)."""

import importlib.util
import json
import os

import pytest

from conftest import BASE_MODEL, make_model

DAEMON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dsf", "qa-daemon.py")


@pytest.fixture
def daemon(data_dir):
    spec = importlib.util.spec_from_file_location("qa_daemon_under_test", DAEMON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._shutdown.clear()
    return module


class FakeEndpoint:
    def __init__(self, path):
        self.path = path
        self.closed = False
        self.handler = None

    def set_endpoint_handler(self, handler):
        self.handler = handler

    def close(self):
        self.closed = True


class FakeCommand:
    instances = []

    def __init__(self, *args, **kwargs):
        self.plugin_data = {}
        self.endpoints = []
        self.messages = []
        FakeCommand.instances.append(self)

    def connect(self):
        pass

    def close(self):
        pass

    def add_http_endpoint(self, endpoint_type, namespace, path):
        endpoint = FakeEndpoint(path)
        self.endpoints.append(endpoint)
        return endpoint

    def set_plugin_data(self, plugin, key, value):
        self.plugin_data[key] = value

    def write_message(self, *args):
        self.messages.append(args)

    def resolve_path(self, path):
        class R:
            result = "/nonexistent" + path[2:]
        return R()


def fake_subscribe(patches, daemon, error=None):
    class FakeSubscribe:
        def __init__(self, *args, **kwargs):
            self.left = list(patches)

        def connect(self):
            pass

        def close(self):
            pass

        def get_object_model(self):
            return make_model()

        def get_object_model_patch(self):
            if error is not None:
                raise error
            if self.left:
                return json.dumps(self.left.pop(0))
            daemon._shutdown.set()
            raise TimeoutError()

    return FakeSubscribe


def test_main_records_a_job_and_publishes_status(daemon, data_dir, monkeypatch):
    FakeCommand.instances.clear()
    patches = [
        {"state": {"status": "processing"}, "job": {"duration": 0, "file": {"fileName": "0:/gcodes/a.gcode"}}},
        {"job": {"duration": 3, "layer": 1}},
    ]
    monkeypatch.setattr(daemon, "CommandConnection", FakeCommand)
    monkeypatch.setattr(daemon, "SubscribeConnection", fake_subscribe(patches, daemon))
    monkeypatch.setattr(daemon, "PLUGIN_DATA_INTERVAL_S", 0)
    daemon.main()
    cmd = FakeCommand.instances[0]
    assert "status" in [e.path for e in cmd.endpoints]
    assert all(e.closed for e in cmd.endpoints)
    assert cmd.plugin_data["status"] == "recording"
    assert cmd.plugin_data["currentJobId"]
    import qa_db
    con = qa_db.Readers(data_dir).get()
    assert con.execute("SELECT result FROM jobs").fetchone()[0] == "running"  # resumed on the next start
    assert con.execute("SELECT COUNT(*) FROM events WHERE type='job_start'").fetchone()[0] == 1
    assert set(cmd.plugin_data) <= set(daemon.PLUGIN_DATA_KEYS)


def test_main_gives_up_after_repeated_errors(daemon, monkeypatch):
    monkeypatch.setattr(daemon, "CommandConnection", FakeCommand)
    monkeypatch.setattr(daemon, "SubscribeConnection", fake_subscribe([], daemon, error=ConnectionResetError("gone")))
    monkeypatch.setattr(daemon._shutdown, "wait", lambda _t=None: False)
    with pytest.raises(ConnectionResetError):
        daemon.main()


def test_sigterm_wakes_the_main_loop(daemon, monkeypatch):
    """The handler shuts the subscription socket down: the blocked read ends at once (patched receive_json
    raises ConnectionError on end of stream) instead of after the 3 s subscription timeout"""
    import socket

    ours, theirs = socket.socketpair()

    class BlockingSubscribe:
        def __init__(self, *args, **kwargs):
            self.socket = ours

        def connect(self):
            pass

        def close(self):
            pass

        def get_object_model(self):
            return make_model()

        def get_object_model_patch(self):
            daemon._signal_handler(15, None)          # SIGTERM arrives while QA waits for DSF
            if not self.socket.recv(4096):
                raise ConnectionError("DSF closed the connection")
            raise AssertionError("the socket should be shut down")

    monkeypatch.setattr(daemon, "CommandConnection", FakeCommand)
    monkeypatch.setattr(daemon, "SubscribeConnection", BlockingSubscribe)
    daemon.main()
    assert daemon._shutdown.is_set()
    theirs.close()


def test_connect_with_retry(daemon, monkeypatch):
    attempts = []

    class Flaky:
        def connect(self):
            attempts.append(1)
            if len(attempts) < 3:
                raise FileNotFoundError("no socket")

    monkeypatch.setattr(daemon._shutdown, "wait", lambda _t=None: False)
    assert daemon.connect_with_retry(Flaky(), "test", attempts=5, delay=0) is True
    assert len(attempts) == 3
    with pytest.raises(RuntimeError):
        daemon.connect_with_retry(type("Never", (), {"connect": lambda self: (_ for _ in ()).throw(OSError())})(),
                                  "test", attempts=2, delay=0)


def test_base_model_is_json_serialisable():
    json.dumps(BASE_MODEL)
