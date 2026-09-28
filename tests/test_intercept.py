"""M240 trigger (qa_intercept): resolved whatever happens, M400 on the SBC channel before the photo,
reconnects; and the conversation with DSF through the real dsf-python InterceptConnection."""

import json
import socket
import threading
import time

import pytest

import qa_intercept
import qa_patches

qa_patches.apply()

from dsf.commands.code_channel import CodeChannel  # noqa: E402
from dsf.connections import InterceptConnection, InterceptionMode  # noqa: E402


def wait_until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


class Lapse:
    def __init__(self, fail=None):
        self.photos = []
        self.fail = fail

    def photo(self, job_key, layer, ts_ms):
        self.photos.append((job_key, layer))
        if self.fail:
            raise RuntimeError(self.fail)
        return "taken"


class FakeConnection:
    """Hands out the codes it was given, then waits until closed."""

    def __init__(self, codes, log):
        self.codes = list(codes)
        self.log = log
        self.closed = threading.Event()
        self.socket = None

    def receive_code(self):
        if self.codes:
            return self.codes.pop(0)
        self.closed.wait(5)
        raise ConnectionError("closed")

    def perform_simple_code(self, code, channel):
        self.log.append(("code", code, channel))

    def resolve_code(self):
        self.log.append(("resolve",))

    def close(self):
        self.closed.set()


@pytest.fixture(autouse=True)
def quick_retry(monkeypatch):
    monkeypatch.setattr(qa_intercept, "RETRY_S", 0.05)


def run_trigger(connections, state, lapse):
    made = []

    def connect():
        if not connections:
            raise ConnectionRefusedError("no DSF")
        made.append(connections.pop(0))
        return made[-1]

    trigger = qa_intercept.CameraTrigger(lapse, lambda: state, connect, CodeChannel.SBC)
    trigger.start()
    return trigger, made


def test_photo_after_m400_and_always_resolved():
    log = []
    lapse = Lapse()
    trigger, _ = run_trigger([FakeConnection(["M240", "M240"], log)], ("job", 5, False), lapse)
    assert wait_until(lambda: log.count(("resolve",)) == 2)
    trigger.stop()
    assert log == [("code", "M400", CodeChannel.SBC), ("resolve",)] * 2
    assert lapse.photos == [("job", 5), ("job", 5)]


def test_a_failing_photo_still_resolves():
    log = []
    trigger, _ = run_trigger([FakeConnection(["M240"], log)], ("job", 5, False), Lapse(fail="camera gone"))
    assert wait_until(lambda: ("resolve",) in log)
    trigger.stop()


@pytest.mark.parametrize("state", [None, ("job", 5, True)], ids=["no job", "simulating"])
def test_no_job_or_simulation_resolves_without_photo(state):
    log = []
    lapse = Lapse()
    trigger, _ = run_trigger([FakeConnection(["M240"], log)], state, lapse)
    assert wait_until(lambda: ("resolve",) in log)
    trigger.stop()
    assert log == [("resolve",)] and lapse.photos == []


def test_reconnects_after_a_lost_connection():
    log = []
    first = FakeConnection([], log)
    first.closed.set()                          # DSF drops it at once
    trigger, made = run_trigger([first, FakeConnection(["M240"], log)], ("job", 2, False), Lapse())
    assert wait_until(lambda: ("resolve",) in log)
    trigger.stop()
    assert len(made) == 2


# --- the real InterceptConnection against a stand-in for DSF's socket ---------------------------

class FakeDsf:
    """Accepts one connection on a Unix socket and plays DSF's side of the conversation."""

    def __init__(self, path):
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(1)
        self.received = []
        self.buffer = ""
        self.conn = None

    def accept(self):
        self.conn, _ = self.server.accept()

    def send(self, obj):
        self.conn.sendall(json.dumps(obj).encode("utf-8"))

    def read(self):
        while True:
            end = qa_patches.json_object_end(self.buffer)
            if end > 0:
                text, self.buffer = self.buffer[:end], self.buffer[end:]
                self.received.append(json.loads(text))
                return self.received[-1]
            chunk = self.conn.recv(4096)
            if not chunk:
                raise ConnectionError("client closed")
            self.buffer += chunk.decode("utf-8")

    def close(self):
        if self.conn is not None:
            self.conn.close()
        self.server.close()


# A code as DSF 3.7 serialises it (DuetAPI Commands/Code/Code.cs properties, Parameter.cs:855-866;
# v3.7-dev @ cd3ae65f): M240 L12 from inside a macro on the File channel
M240_FROM_MACRO = {"command": "Code", "sourceConnection": 0, "result": None, "type": "M", "channel": "File",
                   "lineNumber": 9, "indent": 0, "keyword": "None", "keywordArgument": None, "majorNumber": 240,
                   "minorNumber": -1, "flags": "IsFromMacro", "comment": None, "filePosition": 211, "length": 9,
                   "parameters": [{"letter": "L", "value": "12", "isString": False}]}


def test_conversation_with_dsf(tmp_path):
    path = str(tmp_path / "dcs.sock")
    dsf = FakeDsf(path)
    lapse = Lapse()

    def connect():
        conn = InterceptConnection(InterceptionMode.PRE, channels=[CodeChannel.File, CodeChannel.File2],
                                   filters=[qa_intercept.FILTER], auto_flush=True, auto_evaluate_expression=True)
        conn.connect(socket_file=path)
        return conn

    def play():
        dsf.accept()
        dsf.send({"version": 13, "id": 42})           # DSF v3.7-dev: Defaults.ProtocolVersion = 13
        dsf.read()                                     # init message
        dsf.send({"success": True})
        dsf.send(M240_FROM_MACRO)
        dsf.read()                                     # M400
        dsf.send({"success": True, "result": ""})
        dsf.read()                                     # the resolution

    server = threading.Thread(target=play, daemon=True)
    server.start()
    trigger = qa_intercept.CameraTrigger(lapse, lambda: ("job", 12, False), connect, CodeChannel.SBC)
    trigger.start()
    server.join(5)
    trigger.stop()
    dsf.close()
    init, m400, resolve = dsf.received
    assert init["mode"] == "Intercept" and init["interceptionMode"] == "Pre"
    assert init["filters"] == ["M240"] and init["channels"] == ["File", "File2"]
    assert init["autoFlush"] is True and init["priorityCodes"] is False
    assert m400["command"] == "SimpleCode" and m400["code"] == "M400" and m400["channel"] == "SBC"
    assert resolve["command"] == "Resolve"
    assert lapse.photos == [("job", 12)]
