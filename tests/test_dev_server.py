import os
import subprocess

import scripts.dev_server as dev_server


def test_dev_server_can_disable_reload_explicitly(monkeypatch):
    executed = {}

    monkeypatch.setenv("WATCH_DEV_RELOAD", "0")
    monkeypatch.setattr(dev_server, "_terminate_existing_project_servers", lambda host, port: [])
    monkeypatch.setattr(dev_server, "_wait_for_port_available", lambda host, port: True)
    monkeypatch.setattr(
        "subprocess.run",
        lambda args, **kwargs: executed.update(
            {
                "args": list(args),
                "env": dict(kwargs["env"]),
            }
        )
        or subprocess.CompletedProcess(args, 0),
    )

    try:
        dev_server.main()
    except SystemExit as exc:
        assert exc.code == 0

    assert executed["args"][:4] == [
        dev_server._python_executable(),
        "-m",
        "uvicorn",
        "app.main:app",
    ]
    assert "--reload" not in executed["args"]


def test_dev_server_can_enable_reload_explicitly(monkeypatch):
    executed = {}

    monkeypatch.setenv("WATCH_DEV_RELOAD", "1")
    monkeypatch.setattr(dev_server, "_terminate_existing_project_servers", lambda host, port: [])
    monkeypatch.setattr(dev_server, "_wait_for_port_available", lambda host, port: True)
    monkeypatch.setattr(
        "subprocess.run",
        lambda args, **kwargs: executed.update(
            {
                "args": list(args),
                "env": dict(kwargs["env"]),
            }
        )
        or subprocess.CompletedProcess(args, 0),
    )

    try:
        dev_server.main()
    except SystemExit as exc:
        assert exc.code == 0

    assert "--reload" in executed["args"]
    assert "--reload-dir" in executed["args"]


def test_wait_for_port_available_polls_until_free(monkeypatch):
    calls = []

    def available(host, port):
        calls.append((host, port))
        return len(calls) >= 3

    monkeypatch.setattr(dev_server, "_port_available", available)
    monkeypatch.setattr(dev_server.time, "sleep", lambda seconds: None)

    assert dev_server._wait_for_port_available("127.0.0.1", 8788, timeout_seconds=1) is True
    assert len(calls) == 3


def test_terminate_existing_project_servers_matches_project_venv(monkeypatch):
    captured = {}

    class Result:
        stdout = "123\r\n"

    def run(command, **kwargs):
        captured["command"] = command
        captured["script"] = kwargs["input"] if "input" in kwargs else command[-1]
        return Result()

    monkeypatch.setattr(dev_server.os, "name", "nt")
    monkeypatch.setattr(dev_server, "_port_available", lambda host, port: True)
    monkeypatch.setattr(dev_server.time, "sleep", lambda seconds: None)
    monkeypatch.setattr("subprocess.run", run)

    stopped = dev_server._terminate_existing_project_servers("127.0.0.1", 8788)

    assert stopped == [123]
    assert str(dev_server.VENV_DIR) in captured["script"]
    assert "uvicorn app.main:app" in captured["script"]
