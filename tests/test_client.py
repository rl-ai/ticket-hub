"""HubClient tests against the real app in-process (see conftest.py)."""
import pytest

from hub_client import HubClient, HubError


@pytest.fixture()
def hub(hub_base_url):
    return HubClient(base_url=hub_base_url)


def _create(hub, title, **kw):
    return hub.create_ticket(title, **kw)


# -- create / get / list ------------------------------------------------------

def test_create_and_get(hub):
    t = _create(hub, "CLI client: alpha", owner={"name": "hermes",
                                                 "family": "hermes"},
                tags=["qa"])
    assert t["id"].startswith("TK-")
    assert t["status"] == "draft"
    got = hub.get(t["id"])
    assert got["title"] == "CLI client: alpha"
    assert got["owner"]["family"] == "hermes"


def test_get_missing_raises_hub_error(hub):
    with pytest.raises(HubError) as ei:
        hub.get("TK-NOPE")
    assert ei.value.code == "not_found"
    assert "TK-NOPE" in ei.value.message


def test_list_filters(hub):
    a = _create(hub, "CLI filter me", tags=["zeta"],
                owner={"name": "f-1", "family": "forge"})
    b = _create(hub, "CLI other thing")
    hub.patch(a["id"], status="open")
    assert a["id"] in {t["id"] for t in hub.list(status="open")}
    assert a["id"] in {t["id"] for t in hub.list(tag="zeta")}
    assert a["id"] in {t["id"] for t in hub.list(family="forge")}
    assert {t["id"] for t in hub.list(q="CLI other")} == {b["id"]}
    with pytest.raises(HubError) as ei:
        hub.list(status="bogus")
    assert ei.value.code == "bad_request"


# -- patch / events / edges -----------------------------------------------------

def test_patch_status_transition(hub):
    t = _create(hub, "CLI transition")
    patched = hub.patch(t["id"], status="open", actor="owner")
    assert patched["status"] == "open"
    with pytest.raises(HubError) as ei:
        hub.patch(t["id"], status="draft")  # open -> draft illegal
    assert ei.value.code == "bad_request"


def test_event_and_comment(hub):
    t = _create(hub, "CLI evented")
    ev = hub.event(t["id"], "checkpoint", {"n": 1}, actor="forge")
    assert ev["kind"] == "checkpoint" and ev["ticket_id"] == t["id"]
    cm = hub.comment(t["id"], "hello from client")
    assert cm["kind"] == "comment"
    with pytest.raises(HubError):
        hub.event(t["id"], "yell")


def test_edge_and_cycle_rejection(hub):
    a = _create(hub, "CLI edge A")
    b = _create(hub, "CLI edge B")
    edge = hub.edge(a["id"], b["id"], "blocks")
    assert edge["src"] == a["id"] and edge["dst"] == b["id"]
    # parent_of cycle: child -> ancestor must be rejected
    child = _create(hub, "CLI edge child", parent=a["id"])
    with pytest.raises(HubError) as ei:
        hub.edge(child["id"], a["id"], "parent_of")
    assert ei.value.code == "cycle_rejected"
    # self-edge rejected
    with pytest.raises(HubError):
        hub.edge(a["id"], a["id"], "parent_of")


# -- projections ----------------------------------------------------------------

def test_lineage(hub):
    parent = _create(hub, "CLI lineage root")
    child = _create(hub, "CLI lineage child", parent=parent["id"])
    lin = hub.lineage(parent["id"])
    assert [d["id"] for d in lin["descendants"]] == [child["id"]]
    assert any(e["kind"] == "created" for e in lin["events"])


def test_digest_json_and_md(hub):
    t = _create(hub, "CLI digest proj")
    hub.patch(t["id"], status="open")
    hub.patch(t["id"], status="running")
    d = hub.digest(t["id"])
    assert d["project"] == t["id"]
    assert any("advanced to Running" == b["text"] for b in d["bullets"])
    md = hub.digest(t["id"], fmt="md")
    assert isinstance(md, str) and "advanced to Running" in md


def test_digest_full_flag(hub):
    t = _create(hub, "CLI digest full")
    hub.comment(t["id"], "a note")
    d = hub.digest(t["id"], full=True)
    assert d["project"] == t["id"]


def test_board(hub):
    t = _create(hub, "CLI board item")
    board = hub.board()
    assert set(board["columns"]) >= {"draft", "open", "running", "blocked",
                                     "review", "done", "cancelled"}
    assert t["id"] in {x["id"] for x in board["columns"]["draft"]}


# -- bindings / inbound -------------------------------------------------------------

def test_bind_bindings_inbound(hub):
    t = _create(hub, "CLI chatty")
    b = hub.bind(t["id"], "matrix", "!cli:example.org",
                 thread_id="$thr", root_msg_id="$root")
    assert b["ticket_id"] == t["id"]
    bindings = hub.bindings(t["id"])
    assert bindings[0]["platform"] == "matrix"
    res = hub.inbound("matrix", "!cli:example.org", "hi from client",
                      thread_id="$thr")
    assert res["event"]["kind"] == "thread_message"
    assert res["event"]["ticket_id"] == t["id"]
    with pytest.raises(HubError) as ei:
        hub.inbound("matrix", "!unbound:example.org", "hi")
    assert ei.value.code == "not_found"


def test_connection_error_code():
    dead = HubClient(base_url="http://127.0.0.1:1", timeout=1.0)
    with pytest.raises(HubError) as ei:
        dead.board()
    assert ei.value.code in ("connection_error", "http_error")
