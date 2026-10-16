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

from mcp.types import CallToolRequestParams  # noqa: E402

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


# -- error results carry isError ---------------------------------------


def _call(name: str, args: dict):
    return asyncio.run(server.call_tool(None, CallToolRequestParams(name=name, arguments=args)))


def _text(result) -> str:
    return result.content[0].text


def test_query_refusal_is_an_error_result():
    result = _call("loki_query", {"namespace": "kube-system"})
    assert result.is_error is True
    assert _text(result).startswith("Error: namespace 'kube-system' is not in the allowed set")


def test_query_bad_selector_is_an_error_result():
    result = _call("loki_query", {"namespace": "monitoring", "selector": '} or {x="y"'})
    assert result.is_error is True
    assert "`selector` must be comma-separated label matchers" in _text(result)


def test_labels_bad_since_is_an_error_result():
    result = _call("loki_labels", {"since": "yesterday"})
    assert result.is_error is True
    assert _text(result) == "Error: `since` must look like 30m, 1h, 2d, or 1w"


def test_label_values_missing_label_is_an_error_result():
    result = _call("loki_label_values", {})
    assert result.is_error is True
    assert _text(result) == "Error: 'label' parameter is required"


def test_unknown_tool_is_an_error_result():
    result = _call("loki_nope", {})
    assert result.is_error is True
    assert _text(result) == "Error: unknown tool 'loki_nope'"


def test_loki_failure_is_an_error_result(monkeypatch):
    async def fail(path, params):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(server, "_loki_get", fail)
    result = _call("loki_labels", {})
    assert result.is_error is True
    assert _text(result) == "Error: connection refused"


def test_normal_result_is_not_an_error_even_if_it_reads_like_one(monkeypatch):
    async def values(path, params):
        return {"data": ["Error: disk full", "ok"]}
    monkeypatch.setattr(server, "_loki_get", values)
    result = _call("loki_label_values", {"label": "message"})
    assert not result.is_error
    assert _text(result) == "Error: disk full\nok"


# -- Release version ------------------------------------------------


def test_release_version_is_dev_when_unset(monkeypatch):
    monkeypatch.delenv("LOKI_MCP_VERSION", raising=False)
    assert server.release_version() == "dev"


def test_release_version_is_dev_when_blank(monkeypatch):
    monkeypatch.setenv("LOKI_MCP_VERSION", "  ")
    assert server.release_version() == "dev"


def test_release_version_reports_the_build_value(monkeypatch):
    monkeypatch.setenv("LOKI_MCP_VERSION", "0.2.0-3-gabc1234")
    assert server.release_version() == "0.2.0-3-gabc1234"


def test_server_reports_the_release_version():
    assert server._server.version == server.release_version()
