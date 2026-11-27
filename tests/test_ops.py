import asyncio
import json

import pytest

from app.ops.jobs import KINDS, InvalidParams, build_steps, describe, restarts_runner
from app.web.main import Hardening


def test_every_kind_builds_with_defaults():
    for key, k in KINDS.items():
        p = k.clean({})
        steps = build_steps(key, p, "/opt/telec-signal")
        assert steps and all(isinstance(a, str) for s in steps for a in s.argv)
    assert len(describe()) == len(KINDS)


@pytest.mark.parametrize("kind,params", [
    ("extend_history", {"days": 99999}),
    ("extend_history", {"tf": "1d; rm -rf /"}),
    ("extend_history", {"symbols": ["BTCUSDT", "$(reboot)"]}),
    ("bt_crowding", {"since": "2022-13-40"}),
    ("bt_crowding", {"since": "2022-01-01 && reboot"}),
    ("logs", {"service": "ssh"}),
    ("restart", {"service": "nginx"}),
    ("bt_signals", {"days": "12abc"}),
    ("status", {"extra": 1}),
])
def test_rejects_bad_params(kind, params):
    with pytest.raises(InvalidParams):
        KINDS[kind].clean(params)


def test_values_reach_argv_as_single_arguments():
    p = KINDS["bt_crowding"].clean({"since": "2022-01-01", "universe": "today", "top_n": 30})
    argv = build_steps("bt_crowding", p, "/x")[0].argv
    assert argv[-2:] == ("--since", "2022-01-01") and "--top-n" in argv
    p = KINDS["extend_history"].clean({"symbols": "btcusdt, ethusdt"})
    assert build_steps("extend_history", p, "/x")[0].argv[-3:] == ("--symbols", "BTCUSDT", "ETHUSDT")
    assert not build_steps("bt_signals", {"days": 120}, "/x")[0].as_root
    assert restarts_runner("update", {}) and restarts_runner("restart", {"service": "all"})
    assert not restarts_runner("restart", {"service": "telec-web"})


def _call(method, path, headers):
    sent = []

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "method": method, "path": path, "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
             "query_string": b"", "server": ("x", 443), "scheme": "https", "root_path": ""}
    asyncio.run(Hardening(inner)(scope, receive, send))
    start = sent[0]
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}


def test_csrf_and_headers():
    host = {"host": "signal.telec.shop"}
    ok = {**host, "origin": "https://signal.telec.shop", "content-type": "application/json"}
    assert _call("POST", "/api/ops/jobs", ok)[0] == 200
    assert _call("POST", "/api/ops/jobs", {**ok, "origin": "https://evil.example"})[0] == 403
    assert _call("POST", "/api/ops/jobs", {**host, "content-type": "application/json"})[0] == 403          # no origin/referer
    assert _call("POST", "/api/ops/jobs", {**ok, "content-type": "text/plain"})[0] == 403                 # simple form post
    assert _call("POST", "/api/login", {**host, "referer": "https://signal.telec.shop/login", "content-type": "application/json"})[0] == 200
    status, hs = _call("GET", "/api/me", host)
    assert status == 200 and "frame-ancestors 'none'" in hs["content-security-policy"] and hs["cache-control"] == "no-store"
