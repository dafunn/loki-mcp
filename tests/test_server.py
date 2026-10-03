"""Unit tests for loki-mcp's pure logic: scoping, LogQL construction, validation.

No network: only the branches that resolve before a Loki call are exercised.
Env is set before importing `server` because config is read at import time.
"""

import asyncio
import os
import sys

os.environ.setdefault("LOKI_URL", "http://loki.invalid:3100")
os.environ.setdefault("LOKI_ALLOWED_NAMESPACES", "monitoring,default")
os.environ.setdefault("EXPOSE_METADATA", "true")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import server  # noqa: E402


# -- namespace allow-list (the tier boundary) -------------------------


def test_allowed_namespace_passes():
    assert server._check_namespace("monitoring") is None


def test_disallowed_namespace_rejected():
    err = server._check_namespace("kube-system")
    assert err is not None and "not in the allowed set" in err


def test_query_rejects_offlist_namespace_without_network():
    out = asyncio.run(server._handle_loki_query({"namespace": "kube-system"}))
    assert "not in the allowed set" in out


# -- selector validation (no breaking out of the pinned {}) -----------


def test_valid_selector_accepted():
    assert server._SELECTOR_RE.match('app="api", pod=~"web-.*"')


def test_injection_selector_rejected():
    # A selector trying to close the brace and inject a pipeline must fail.
    assert not server._SELECTOR_RE.match('app="x"} |= "boom')
    out = asyncio.run(
        server._handle_loki_query(
            {"namespace": "monitoring", "selector": 'app="x"} |= "boom'}
        )
    )
    assert "must be comma-separated label matchers" in out


# -- LogQL construction + escaping ------------------------------------


def test_build_logql_pins_namespace():
    q = server._build_logql("monitoring", 'app="api"', "")
    assert q == '{namespace="monitoring", app="api"}'


def test_build_logql_escapes_filter():
    q = server._build_logql("monitoring", "", 'say "hi"')
    assert q == '{namespace="monitoring"} |= "say \\"hi\\""'


# -- duration parsing -------------------------------------------------


def test_since_to_ns_window():
    start, end = server._since_to_ns("1h")
    assert end - start == 3600 * 1_000_000_000


def test_query_rejects_bad_since():
    out = asyncio.run(
        server._handle_loki_query({"namespace": "monitoring", "since": "yesterday"})
    )
    assert "must look like" in out
