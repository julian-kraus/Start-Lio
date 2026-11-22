"""ops/check.py must not report a database ready while only the image's socket-only init server answers."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

CHECK = Path(__file__).resolve().parent.parent / "ops" / "check.py"


@pytest.fixture
def check():
    spec = importlib.util.spec_from_file_location("ops_check", CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_docker(check, monkeypatch, state, tcp_codes):
    """The socket always answers. TCP answers with tcp_codes in turn, then keeps the last one."""
    tcp_polls = []

    def docker(cmd, **kwargs):
        code = 0
        if "pg_isready" in cmd and "-h" in cmd:
            assert cmd[cmd.index("-h") + 1] == "127.0.0.1"
            code = tcp_codes[min(len(tcp_polls), len(tcp_codes) - 1)]
            tcp_polls.append(code)
        return SimpleNamespace(returncode=code, stdout="", stderr="")

    clock = [0.0]
    monkeypatch.setattr(check, "docker_state", lambda: state)
    monkeypatch.setattr(check, "subprocess", SimpleNamespace(run=docker))
    monkeypatch.setattr(check, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    return tcp_polls


@pytest.mark.parametrize("state", ["absent", "stopped", "running"])
def test_waits_until_tcp_answers(check, monkeypatch, state):
    polls = fake_docker(check, monkeypatch, state, [2, 2, 0])
    ok, _ = check.ensure_db()
    assert ok
    assert polls == [2, 2, 0]


def test_socket_alone_is_never_ready(check, monkeypatch):
    fake_docker(check, monkeypatch, "absent", [2])
    ok, message = check.ensure_db()
    assert not ok
    assert "not ready" in message
