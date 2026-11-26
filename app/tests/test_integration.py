"""Runs against a real PostgreSQL (PG* env vars). Skipped when PGHOST is unset."""

import os

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.getenv("PGHOST"), reason="needs PostgreSQL")]


@pytest.fixture(scope="module")
def client():
    from shortlink.app import create_app

    return create_app(fault_error_rate=0, fault_latency_ms=0, fault_after_s=0).test_client()


def test_ready_against_real_db(client):
    assert client.get("/readyz").status_code == 200


def test_round_trip_persists_hits(client):
    code = client.post("/api/links", json={"url": "https://www.postgresql.org/docs/"}).get_json()["code"]
    for _ in range(3):
        assert client.get(f"/{code}").status_code == 302
    stats = client.get(f"/api/links/{code}").get_json()
    assert stats["hits"] == 3 and stats["url"] == "https://www.postgresql.org/docs/"


def test_duplicate_code_raises_code_taken():
    from shortlink.store import CodeTaken, PgStore

    store = PgStore()
    store.ensure_schema()
    store.create("dupe001", "https://a.io")
    with pytest.raises(CodeTaken):
        store.create("dupe001", "https://b.io")


def test_delete_removes_row():
    from shortlink.store import PgStore

    store = PgStore()
    store.ensure_schema()
    store.create("del0001", "https://a.io")
    assert store.delete("del0001") is True
    assert store.get("del0001") is None and store.delete("del0001") is False
