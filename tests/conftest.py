import json
import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture():
    def load(name):
        return json.loads((FIXTURES / f"{name}.json").read_text())

    return load


@pytest.fixture(autouse=True)
def isolate_network_and_state(monkeypatch, tmp_path, request):
    if "live" in request.keywords:
        return

    def blocked(*args, **kwargs):
        pytest.fail("Offline tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket.socket, "sendto", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
