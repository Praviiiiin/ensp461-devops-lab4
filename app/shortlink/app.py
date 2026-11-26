"""ShortLink: a small URL-shortener REST API backed by PostgreSQL."""

import hmac
import json
import logging
import os
import random
import re
import secrets
import string
import sys
import time
from urllib.parse import urlparse

from flask import Flask, Response, g, jsonify, redirect, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .store import CodeTaken

VERSION = os.getenv("APP_VERSION", "dev")
COLOUR = os.getenv("APP_COLOUR", "none")
ALPHABET = string.ascii_letters + string.digits
CODE_LEN = 7
MAX_URL_LEN = 2048
PROBE_PATHS = {"/healthz", "/readyz", "/metrics"}
ALIAS_RE = re.compile(r"[A-Za-z0-9_-]{3,16}")
RESERVED = {"api", "healthz", "readyz", "metrics"}
# The admin token is mounted as a file from a Kubernetes Secret and re-read on every request,
# so a rotated secret takes effect without a restart and never appears in the pod spec or env.
ADMIN_TOKEN_FILE = os.getenv("ADMIN_TOKEN_FILE", "/etc/shortlink/admin-token")

REQUESTS = Counter(
    "shortlink_http_requests_total",
    "HTTP requests handled",
    ["method", "route", "status", "version", "colour"],
)
LATENCY = Histogram(
    "shortlink_http_request_duration_seconds",
    "HTTP request latency",
    ["route", "version", "colour"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
LINKS_CREATED = Counter("shortlink_links_created_total", "Short links created", ["version", "colour"])

log = logging.getLogger("shortlink")


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def valid_url(url) -> bool:
    if not isinstance(url, str) or len(url) > MAX_URL_LEN:
        return False
    parts = urlparse(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def create_app(store=None, fault_error_rate=None, fault_latency_ms=None, fault_after_s=None):
    if store is None:
        from .store import PgStore

        store = PgStore()
    error_rate = float(os.getenv("FAULT_ERROR_RATE", "0")) if fault_error_rate is None else fault_error_rate
    latency_ms = int(os.getenv("FAULT_LATENCY_MS", "0")) if fault_latency_ms is None else fault_latency_ms
    after_s = float(os.getenv("FAULT_AFTER_SECONDS", "0")) if fault_after_s is None else fault_after_s
    started = time.monotonic()

    app = Flask(__name__)
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False

    @app.before_request
    def start_timer():
        g.t0 = time.perf_counter()
        # Fault injection for the blue-green drills. Probe endpoints stay healthy on purpose,
        # so the fault is only visible in user traffic, like a real application bug.
        if request.path in PROBE_PATHS or time.monotonic() - started < after_s:
            return None
        if latency_ms:
            time.sleep(latency_ms / 1000)
        if error_rate and random.random() < error_rate:  # noqa: S311  # nosec B311
            return jsonify(error="injected fault"), 500
        return None

    @app.after_request
    def observe(resp):
        route = request.url_rule.rule if request.url_rule else "unmatched"
        elapsed = time.perf_counter() - g.get("t0", time.perf_counter())
        REQUESTS.labels(request.method, route, str(resp.status_code), VERSION, COLOUR).inc()
        LATENCY.labels(route, VERSION, COLOUR).observe(elapsed)
        if request.path not in PROBE_PATHS:
            log.info(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                        "level": "error" if resp.status_code >= 500 else "info",
                        "method": request.method,
                        "path": request.path,
                        "route": route,
                        "status": resp.status_code,
                        "duration_ms": round(elapsed * 1000, 2),
                        "version": VERSION,
                        "colour": COLOUR,
                    }
                )
            )
        resp.headers["X-ShortLink-Version"] = VERSION
        return resp

    def admin_ok() -> bool:
        try:
            with open(ADMIN_TOKEN_FILE) as f:
                expected = f.read().strip()
        except OSError:
            return False  # no token configured: admin API is closed
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())

    @app.delete("/api/links/<code>")
    def delete_link(code):
        if not admin_ok():
            return jsonify(error="admin token required"), 401
        if not store.delete(code):
            return jsonify(error="not found"), 404
        return "", 204

    @app.post("/api/links")
    def create_link():
        body = request.get_json(silent=True) or {}
        url = body.get("url")
        if not valid_url(url):
            return jsonify(error="url must be an absolute http(s) URL up to 2048 characters"), 400
        alias = body.get("alias")
        if alias is not None:
            if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias) or alias in RESERVED:
                return jsonify(error="alias must be 3-16 characters of A-Z a-z 0-9 _ -"), 400
            try:
                store.create(alias, url)
            except CodeTaken:
                return jsonify(error="alias already taken"), 409
            LINKS_CREATED.labels(VERSION, COLOUR).inc()
            return jsonify(code=alias, url=url, short_url=f"{request.host_url}{alias}"), 201
        for _ in range(5):
            code = new_code()
            try:
                store.create(code, url)
                break
            except CodeTaken:
                continue
        else:
            return jsonify(error="could not allocate a code"), 503
        LINKS_CREATED.labels(VERSION, COLOUR).inc()
        return jsonify(code=code, url=url, short_url=f"{request.host_url}{code}"), 201

    @app.get("/<code>")
    def follow(code):
        url = store.hit(code)
        if url is None:
            return jsonify(error="not found"), 404
        return redirect(url, code=302)

    @app.get("/api/links/<code>")
    def stats(code):
        link = store.get(code)
        if link is None:
            return jsonify(error="not found"), 404
        return jsonify(link)

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok", version=VERSION, colour=COLOUR)

    @app.get("/readyz")
    def readyz():
        try:
            store.ping()
        except Exception as exc:  # any DB failure means not ready
            return jsonify(status="unavailable", reason=type(exc).__name__), 503
        return jsonify(status="ready", version=VERSION, colour=COLOUR)

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

    return app
