import pytest

from shortlink.app import create_app, valid_url
from shortlink.store import CodeTaken


class FakeStore:
    def __init__(self, up=True):
        self.links, self.up = {}, up

    def ping(self):
        if not self.up:
            raise ConnectionError("db down")

    def create(self, code, url):
        if code in self.links:
            raise CodeTaken(code)
        self.links[code] = {"code": code, "url": url, "hits": 0, "created_at": "2026-01-01T00:00:00"}

    def hit(self, code):
        link = self.links.get(code)
        if link:
            link["hits"] += 1
            return link["url"]
        return None

    def get(self, code):
        return self.links.get(code)

    def delete(self, code):
        return self.links.pop(code, None) is not None


@pytest.fixture
def client():
    return create_app(FakeStore(), 0, 0, 0).test_client()


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://example.com/a?b=c", True),
        ("http://krmu.edu.in", True),
        ("ftp://example.com", False),
        ("example.com", False),
        ("javascript:alert(1)", False),
        (None, False),
        pytest.param("https://x.io/" + "a" * 2100, False, id="longer-than-2048"),
        ("https://user:pass@example.com/", False),
        ("https://:token@example.com/", False),
    ],
)
def test_valid_url(url, ok):
    assert valid_url(url) is ok


def test_create_redirect_and_stats(client):
    r = client.post("/api/links", json={"url": "https://example.com/docs"})
    assert r.status_code == 201
    code = r.get_json()["code"]
    assert len(code) == 7
    r = client.get(f"/{code}")
    assert r.status_code == 302 and r.headers["Location"] == "https://example.com/docs"
    assert client.get(f"/api/links/{code}").get_json()["hits"] == 1


def test_surrounding_whitespace_is_stripped(client):
    r = client.post("/api/links", json={"url": "  https://krmu.edu.in/admissions \n"})
    assert r.status_code == 201 and r.get_json()["url"] == "https://krmu.edu.in/admissions"


def test_rejects_bad_url(client):
    assert client.post("/api/links", json={"url": "nope"}).status_code == 400
    assert client.post("/api/links", data="not json").status_code == 400


def test_unknown_code_is_404(client):
    assert client.get("/zzzzzzz").status_code == 404
    assert client.get("/api/links/zzzzzzz").status_code == 404


def test_code_collision_retries(monkeypatch):
    store = FakeStore()
    store.links["AAAAAAA"] = {"code": "AAAAAAA"}
    codes = iter(["AAAAAAA", "BBBBBBB"])
    monkeypatch.setattr("shortlink.app.new_code", lambda: next(codes))
    r = create_app(store, 0, 0, 0).test_client().post("/api/links", json={"url": "https://a.io"})
    assert r.get_json()["code"] == "BBBBBBB"


def test_probes(client):
    assert client.get("/healthz").get_json()["status"] == "ok"
    assert client.get("/readyz").status_code == 200
    down = create_app(FakeStore(up=False), 0, 0, 0).test_client()
    assert down.get("/readyz").status_code == 503


def test_metrics_exposed(client):
    client.get("/healthz")
    body = client.get("/metrics").data.decode()
    assert "shortlink_http_requests_total" in body
    assert "shortlink_http_request_duration_seconds_bucket" in body


def test_fault_injection_spares_probes():
    c = create_app(FakeStore(), fault_error_rate=1.0, fault_latency_ms=0, fault_after_s=0).test_client()
    assert c.post("/api/links", json={"url": "https://a.io"}).status_code == 500
    assert c.get("/healthz").status_code == 200


def test_fault_is_dormant_before_delay():
    c = create_app(FakeStore(), fault_error_rate=1.0, fault_latency_ms=0, fault_after_s=3600).test_client()
    assert c.post("/api/links", json={"url": "https://a.io"}).status_code == 201


def test_custom_alias(client):
    r = client.post("/api/links", json={"url": "https://krmu.edu.in", "alias": "krmu-home"})
    assert r.status_code == 201 and r.get_json()["code"] == "krmu-home"
    assert client.get("/krmu-home").headers["Location"] == "https://krmu.edu.in"
    assert client.post("/api/links", json={"url": "https://a.io", "alias": "krmu-home"}).status_code == 409


@pytest.mark.parametrize("alias", ["ab", "x" * 17, "has space", "metrics", 42, "../etc"])
def test_bad_alias_rejected(client, alias):
    assert client.post("/api/links", json={"url": "https://a.io", "alias": alias}).status_code == 400


def test_version_header(client):
    r = client.get("/healthz")
    assert r.headers["X-ShortLink-Version"] == "dev" and r.headers["X-Served-By"]


def test_delete_needs_admin_token(client, tmp_path, monkeypatch):
    token = tmp_path / "admin-token"
    monkeypatch.setattr("shortlink.app.ADMIN_TOKEN_FILE", str(token))
    client.post("/api/links", json={"url": "https://a.io", "alias": "gone1"})
    assert client.delete("/api/links/gone1").status_code == 401  # no token file: admin API closed
    token.write_text("s3cret-1\n")
    assert client.delete("/api/links/gone1", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.delete("/api/links/gone1", headers={"Authorization": "Bearer s3cret-1"}).status_code == 204
    assert client.delete("/api/links/gone1", headers={"Authorization": "Bearer s3cret-1"}).status_code == 404
    token.write_text("s3cret-2")  # rotation is picked up without a restart
    client.post("/api/links", json={"url": "https://a.io", "alias": "gone2"})
    assert client.delete("/api/links/gone2", headers={"Authorization": "Bearer s3cret-1"}).status_code == 401
    assert client.delete("/api/links/gone2", headers={"Authorization": "Bearer s3cret-2"}).status_code == 204
