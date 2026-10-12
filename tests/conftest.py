"""Shared fixture: run the REAL app in-process (uvicorn on a random port)
against a tmp SQLite DB via TICKET_HUB_DB, so the stdlib-urllib client can
talk to the actual HTTP contract end-to-end."""
import os
import socket
import sys
import threading
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def hub_base_url(tmp_path_factory):
    db_path = str(tmp_path_factory.mktemp("hub-cli-db") / "hub.db")
    os.environ["TICKET_HUB_DB"] = db_path
    import uvicorn
    from api import create_app

    app = create_app()  # store is resolved lazily from TICKET_HUB_DB
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/board", timeout=0.5):
                break
        except Exception:
            time.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError("test hub server did not come up")
    yield base
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive()
