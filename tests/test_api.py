"""Lane B API tests against the FakeStore contract (runs standalone)."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest
from fastapi.testclient import TestClient

from api import create_app
from fake_store import FakeStore


@pytest.fixture()
def client():
    store = FakeStore()
    app = create_app(store=store)
    return TestClient(app)


def _create(client, title, **kw):
    resp = client.post("/tickets", json={"title": title, **kw})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---- tickets ---------------------------------------------------------------

def test_create_ticket(client):
    t = _create(client, "Parent ticket", owner={"name": "ray", "family": "ray",
                                                "kind": "human"}, tags=["core"])
    assert t["id"].startswith("TK-")
    assert t["status"] == "draft"
    fetched = client.get(f"/tickets/{t['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Parent ticket"


def test_get_missing_404_json_error(client):
    resp = client.get("/tickets/TK-NOPE")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_create_child_via_parent_field(client):
    parent = _create(client, "Parent")
    child = _create(client, "Child", parent=parent["id"])
    lineage = client.get(f"/tickets/{child['id']}/lineage").json()
    assert [a["id"] for a in lineage["ancestry"]] == [parent["id"]]
    lineage_p = client.get(f"/tickets/{parent['id']}/lineage").json()
    assert [d["id"] for d in lineage_p["descendants"]] == [child["id"]]


def test_list_filters(client):
    a = _create(client, "Alpha thing", tags=["ui"],
                owner={"name": "hermes-1", "family": "hermes"})
    b = _create(client, "Beta thing")
    client.patch(f"/tickets/{a['id']}", json={"status": "open"})
    assert {t["id"] for t in client.get("/tickets?status=Open").json()} == {a["id"]}
    assert {t["id"] for t in client.get("/tickets?family=hermes").json()} == {a["id"]}
    assert {t["id"] for t in client.get("/tickets?tag=ui").json()} == {a["id"]}
    assert {t["id"] for t in client.get("/tickets?q=Beta").json()} == {b["id"]}
    bad = client.get("/tickets?status=Bogus")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "bad_request"


# ---- patch / events ----------------------------------------------------------

def test_patch_status_emits_event(client):
    t = _create(client, "Patchable")
    resp = client.patch(f"/tickets/{t['id']}",
                        json={"status": "review", "actor": {"name": "glm"}})
    assert resp.status_code == 200
    assert resp.json()["status"] == "review"
    events = client.get(f"/tickets/{t['id']}/lineage").json()["events"]
    sc = [e for e in events if e["kind"] == "status_changed"]
    assert len(sc) == 1
    assert sc[0]["payload"] == {"from": "draft", "to": "review"}


def test_patch_invalid_status_400(client):
    t = _create(client, "X")
    resp = client.patch(f"/tickets/{t['id']}", json={"status": "Frozen"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_append_event(client):
    t = _create(client, "Evented")
    resp = client.post(f"/tickets/{t['id']}/events",
                       json={"kind": "artifact",
                             "actor": {"name": "forge", "family": "forge"},
                             "payload": {"name": "round-13 package"}})
    assert resp.status_code == 201
    ev = resp.json()
    assert ev["kind"] == "artifact" and ev["ticket_id"] == t["id"]
    bad = client.post(f"/tickets/{t['id']}/events", json={"kind": "yell"})
    assert bad.status_code == 400


# ---- edges -------------------------------------------------------------------

def test_edge_created(client):
    a = _create(client, "A")
    b = _create(client, "B")
    resp = client.post(f"/tickets/{a['id']}/edges",
                       json={"type": "blocks", "dst": b["id"]})
    assert resp.status_code == 201
    edge = resp.json()
    assert edge["src"] == a["id"] and edge["dst"] == b["id"]
    assert b["id"] in [t["id"] for t in client.get(f"/tickets/{a['id']}/lineage").json()["related"]]


def test_edge_cycle_rejected_400(client):
    parent = _create(client, "P")
    child = _create(client, "C", parent=parent["id"])
    grand = _create(client, "G", parent=child["id"])
    # Closing the loop: grand -> parent as parent_of must be rejected.
    resp = client.post(f"/tickets/{grand['id']}/edges",
                       json={"type": "parent_of", "dst": parent["id"]})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "cycle_rejected"
    # Self-edge too.
    resp2 = client.post(f"/tickets/{parent['id']}/edges",
                        json={"type": "parent_of", "dst": parent["id"]})
    assert resp2.status_code == 400
    # Unknown edge type rejected.
    resp3 = client.post(f"/tickets/{parent['id']}/edges",
                        json={"type": "friend_of", "dst": child["id"]})
    assert resp3.status_code == 400


def test_edge_missing_dst_404(client):
    a = _create(client, "A")
    resp = client.post(f"/tickets/{a['id']}/edges",
                       json={"type": "relates_to", "dst": "TK-GHOST"})
    assert resp.status_code == 404


# ---- digest ------------------------------------------------------------------

def test_digest_structured_bullets_cite_event_ids(client):
    parent = _create(client, "Launch hub")
    child = _create(client, "Subtask", parent=parent["id"])
    client.patch(f"/tickets/{child['id']}", json={"status": "review",
                                                  "actor": {"name": "hermes"}})
    ev_art = client.post(f"/tickets/{child['id']}/events", json={
        "kind": "artifact", "actor": {"name": "forge"},
        "payload": {"name": "snapshot tarball"}}).json()
    ev_appr = client.post(f"/tickets/{child['id']}/events", json={
        "kind": "approval_receipt", "actor": {"name": "ray", "family": "ray"},
        "payload": {"approver": "Ray"}}).json()
    ev_dec = client.post(f"/tickets/{child['id']}/events", json={
        "kind": "decision", "actor": {"name": "ray"},
        "payload": {"text": "ship v0"}}).json()
    ev_known = client.post(f"/tickets/{parent['id']}/events", json={
        "kind": "comment", "actor": {"name": "forge"},
        "payload": {"text": "QUEUED FINDING: flaky replay"}}).json()

    resp = client.get(f"/tickets/{parent['id']}/digest?format=json")
    assert resp.status_code == 200
    digest = resp.json()
    assert digest["project"] == parent["id"]
    texts = [b["text"] for b in digest["bullets"]]
    assert "advanced to Review" in texts
    assert "produced snapshot tarball" in texts
    assert "approved by Ray" in texts
    assert "decision recorded" in texts
    assert "known issue logged" in texts
    # Every bullet cites at least one real event id, and cited ids cover the
    # translated events.
    for b in digest["bullets"]:
        assert len(b["event_ids"]) >= 1
    all_cited = {eid for b in digest["bullets"] for eid in b["event_ids"]}
    assert {ev_art["id"], ev_appr["id"], ev_dec["id"], ev_known["id"]} <= all_cited


def test_digest_from_child_scopes_to_project(client):
    parent = _create(client, "Project root")
    child = _create(client, "Child", parent=parent["id"])
    client.patch(f"/tickets/{child['id']}", json={"status": "running"})
    digest = client.get(f"/tickets/{child['id']}/digest").json()
    assert digest["project"] == parent["id"]
    assert any("advanced to Running" == b["text"] for b in digest["bullets"])


def test_digest_markdown_format(client):
    t = _create(client, "MD project")
    client.patch(f"/tickets/{t['id']}", json={"status": "done"})
    resp = client.get(f"/tickets/{t['id']}/digest?format=md")
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers["content-type"]
    assert "advanced to Done" in resp.text


def test_digest_bad_format_400(client):
    t = _create(client, "X")
    resp = client.get(f"/tickets/{t['id']}/digest?format=xml")
    assert resp.status_code == 400


# ---- board -------------------------------------------------------------------

def test_board_columns(client):
    a = _create(client, "A")
    b = _create(client, "B")
    client.patch(f"/tickets/{b['id']}", json={"status": "open"})
    board = client.get("/board").json()
    assert set(board["columns"]) >= {"draft", "open", "running", "blocked",
                                     "review", "done", "cancelled"}
    assert [t["id"] for t in board["columns"]["draft"]] == [a["id"]]
    assert [t["id"] for t in board["columns"]["open"]] == [b["id"]]


# ---- thread binding / inbound --------------------------------------------------

def test_bind_thread(client):
    t = _create(client, "Chatty")
    resp = client.post(f"/tickets/{t['id']}/bind", json={
        "platform": "matrix", "room_id": "!room:example.org",
        "thread_id": "$root", "root_msg_id": "$root"})
    assert resp.status_code == 201
    assert resp.json()["ticket_id"] == t["id"]
    bindings = client.get(f"/tickets/{t['id']}/bindings").json()
    assert bindings[0]["platform"] == "matrix"
    bad = client.post(f"/tickets/{t['id']}/bind",
                      json={"platform": "myspace", "room_id": "x"})
    assert bad.status_code == 400


def test_inbound_creates_thread_message_event(client):
    t = _create(client, "Chatty")
    client.post(f"/tickets/{t['id']}/bind",
                json={"platform": "discord", "room_id": "chan-1",
                      "thread_id": "thr-1"})
    resp = client.post("/adapters/discord/inbound", json={
        "room_id": "chan-1", "thread_id": "thr-1",
        "actor": {"name": "ray", "family": "ray", "kind": "human"},
        "text": "looks good, ship it"})
    assert resp.status_code == 200
    event = resp.json()["event"]
    assert event["kind"] == "thread_message"
    assert event["ticket_id"] == t["id"]
    assert event["payload"]["text"] == "looks good, ship it"
    # Unbound room -> 404 JSON error.
    miss = client.post("/adapters/discord/inbound",
                       json={"room_id": "nope", "text": "hi"})
    assert miss.status_code == 404
    assert miss.json()["error"]["code"] == "not_found"


# ---- UI ------------------------------------------------------------------------

def test_ui_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "ticket-hub" in resp.text.lower()
    for asset in ("/ui/app.js", "/ui/style.css"):
        r = client.get(asset)
        assert r.status_code == 200, asset


def test_validation_error_is_json(client):
    resp = client.post("/tickets", json={"body": "no title"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation"
