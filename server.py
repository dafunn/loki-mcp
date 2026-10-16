"""loki-mcp: read-only Loki (log) MCP server.

Each deployment points at one Loki endpoint, is limited to an allow-listed set
of namespaces, and exposes only the tools its configuration enables. Every
query is pinned to an allow-listed namespace label matcher.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

# -- Config -----------------------------------------------------------

LOKI_URL = os.getenv("LOKI_URL", "").rstrip("/")
LOKI_TENANT = os.getenv("LOKI_TENANT", "").strip()  # X-Scope-OrgID (multi-tenant Loki)
NAMESPACE_LABEL = os.getenv("LOKI_NAMESPACE_LABEL", "namespace").strip()
MAX_LINES = int(os.getenv("LOKI_MAX_LINES", "200"))
DEFAULT_SINCE = os.getenv("LOKI_DEFAULT_SINCE", "1h").strip()
HTTP_TIMEOUT = float(os.getenv("LOKI_HTTP_TIMEOUT", "30"))

EXPOSE_QUERY = os.getenv("EXPOSE_QUERY", "true").lower() in {"1", "true", "yes", "on"}
EXPOSE_METADATA = os.getenv("EXPOSE_METADATA", "false").lower() in {"1", "true", "yes", "on"}

_allowlist_raw = os.getenv("LOKI_ALLOWED_NAMESPACES", "").strip()
ALLOWED_NAMESPACES: frozenset[str] | None = (
    frozenset(ns.strip() for ns in _allowlist_raw.split(",") if ns.strip())
    if _allowlist_raw
    else None
)

# -- Error results ----------------------------------------------------


class ErrorText(str):
    """A tool result that reports a failure. call_tool sends it with isError set,
    so a client can tell it from output (a log line or label value can itself
    start with "Error:")."""


# -- Token ------------------------------------------------------------


def _get_token() -> str | None:
    """Bearer token for Loki, if the endpoint requires one. Optional."""
    tok_file = os.getenv("LOKI_TOKEN_FILE", "/run/secrets/loki-token")
    p = Path(tok_file)
    if p.exists():
        return p.read_text().strip()
    return os.getenv("LOKI_TOKEN") or None


def _headers() -> dict[str, str]:
    h: dict[str, str] = {}
    if LOKI_TENANT:
        h["X-Scope-OrgID"] = LOKI_TENANT
    tok = _get_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


# -- Scoping + LogQL construction -------------------------------------

# A single label matcher: name (op) "value". Used to validate the optional
# extra `selector` so the model cannot break out of the namespace-pinned {}.
_MATCHER = r'[a-zA-Z_][a-zA-Z0-9_]*\s*(?:=~|!~|=|!=)\s*"[^"]*"'
_SELECTOR_RE = re.compile(rf"^\s*{_MATCHER}(?:\s*,\s*{_MATCHER})*\s*$")
_DURATION_RE = re.compile(r"^\d+[smhdw]$")


def _check_namespace(namespace: str) -> ErrorText | None:
    """Return an error string if namespace is not allowed, else None."""
    if ALLOWED_NAMESPACES is not None and namespace not in ALLOWED_NAMESPACES:
        allowed = ", ".join(sorted(ALLOWED_NAMESPACES))
        return ErrorText(f"Error: namespace '{namespace}' is not in the allowed set ({allowed})")
    if not namespace and ALLOWED_NAMESPACES is not None:
        return ErrorText("Error: a namespace is required (this tier is namespace-scoped)")
    return None


def _build_logql(namespace: str, selector: str, line_filter: str) -> str | None:
    """Compose a namespace-pinned LogQL query."""
    matchers = [f'{NAMESPACE_LABEL}="{namespace}"'] if namespace else []
    if selector:
        matchers.append(selector.strip())
    stream = "{" + ", ".join(matchers) + "}"
    if line_filter:
        escaped = line_filter.replace("\\", "\\\\").replace('"', '\\"')
        return f'{stream} |= "{escaped}"'
    return stream


def _since_to_ns(since: str) -> tuple[int, int]:
    """Return (start_ns, end_ns) for a duration like '1h', '30m', '2d'."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    seconds = int(since[:-1]) * units[since[-1]]
    end_ns = time.time_ns()
    start_ns = end_ns - seconds * 1_000_000_000
    return start_ns, end_ns


def _parse_time(spec: str) -> int:
    """Parse a time spec to nanosecond epoch: relative (1h/30m/2d/1w back from
    now), ISO 8601, epoch seconds/millis, or 'now'."""
    s = spec.strip()
    if s == "" or s.lower() == "now":
        return time.time_ns()
    if _DURATION_RE.match(s):  # relative window back from now
        units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
        return time.time_ns() - int(s[:-1]) * units[s[-1]] * 1_000_000_000
    try:  # ISO 8601 (tolerate a trailing Z)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000_000)
    except ValueError:
        pass
    try:  # epoch seconds or milliseconds
        v = float(s)
        if v > 1e12:
            v /= 1000
        return int(v * 1_000_000_000)
    except ValueError as e:
        raise ValueError(f"cannot parse time '{spec}' (use 1h / ISO / epoch / now)") from e


# -- Loki calls -------------------------------------------------------


async def _loki_get(path: str, params: dict[str, Any]) -> dict:
    if not LOKI_URL:
        raise RuntimeError("LOKI_URL environment variable is required")
    url = f"{LOKI_URL}{path}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=_headers())
        resp.raise_for_status()
        return resp.json()


def _format_streams(data: dict) -> str:
    """Render a Loki query_range 'streams' result as timestamped log lines."""
    result = data.get("data", {}).get("result", [])
    entries: list[tuple[int, str, str]] = []
    for stream in result:
        labels = stream.get("stream", {})
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        for ts_ns, line in stream.get("values", []):
            entries.append((int(ts_ns), label_str, line))

    if not entries:
        return "(no log lines matched)"

    entries.sort(key=lambda e: e[0])  # oldest -> newest
    truncated = len(entries) > MAX_LINES
    if truncated:
        entries = entries[-MAX_LINES:]  # keep the most recent when capping

    lines: list[str] = []
    for ts_ns, label_str, line in entries:
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_ns / 1e9))
        lines.append(f"{iso}Z  {line.rstrip()}")

    out = "\n".join(lines)
    out += f"\n({len(lines)} line{'s' if len(lines) != 1 else ''})"
    if truncated:
        out += (
            f"\n[TRUNCATED - showing the most recent {MAX_LINES} lines. "
            "Narrow with a filter, a tighter selector, or a shorter time range.]"
        )
    return out


# -- Tool handlers ----------------------------------------------------


async def _handle_loki_query(args: dict) -> str:
    namespace = str(args.get("namespace", "")).strip()
    selector = str(args.get("selector", "")).strip()
    line_filter = str(args.get("filter", ""))
    since = str(args.get("since", "") or DEFAULT_SINCE).strip()
    start = str(args.get("start", "")).strip()
    end = str(args.get("end", "") or "now").strip()
    limit = int(args.get("limit", MAX_LINES))

    err = _check_namespace(namespace)
    if err:
        return err
    if selector and not _SELECTOR_RE.match(selector):
        return ErrorText(
            "Error: `selector` must be comma-separated label matchers, e.g. "
            'app="foo", pod=~"bar.*" (no namespace - that is applied for you)'
        )
    # Explicit start/end (relative, ISO, or epoch) override the since window.
    if start:
        try:
            start_ns, end_ns = _parse_time(start), _parse_time(end)
        except ValueError as e:
            return ErrorText(f"Error: {e}")
    else:
        if not _DURATION_RE.match(since):
            return ErrorText("Error: `since` must look like 30m, 1h, 2d, or 1w")
        start_ns, end_ns = _since_to_ns(since)

    query = _build_logql(namespace, selector, line_filter)
    try:
        data = await _loki_get(
            "/loki/api/v1/query_range",
            {
                "query": query,
                "start": start_ns,
                "end": end_ns,
                "limit": min(limit, MAX_LINES),
                "direction": "backward",
            },
        )
        return f"query: {query}\n\n{_format_streams(data)}"
    except httpx.HTTPStatusError as e:
        return ErrorText(f"Error: Loki returned {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        return ErrorText(f"Error: {e}")


async def _handle_loki_labels(args: dict) -> str:
    since = str(args.get("since", "") or DEFAULT_SINCE).strip()
    if not _DURATION_RE.match(since):
        return ErrorText("Error: `since` must look like 30m, 1h, 2d, or 1w")
    start_ns, end_ns = _since_to_ns(since)
    try:
        data = await _loki_get("/loki/api/v1/labels", {"start": start_ns, "end": end_ns})
        names = data.get("data", [])
        return "\n".join(names) if names else "(no labels)"
    except Exception as e:
        return ErrorText(f"Error: {e}")


async def _handle_loki_label_values(args: dict) -> str:
    label = str(args.get("label", "")).strip()
    if not label:
        return ErrorText("Error: 'label' parameter is required")
    since = str(args.get("since", "") or DEFAULT_SINCE).strip()
    if not _DURATION_RE.match(since):
        return ErrorText("Error: `since` must look like 30m, 1h, 2d, or 1w")
    start_ns, end_ns = _since_to_ns(since)
    try:
        data = await _loki_get(
            f"/loki/api/v1/label/{label}/values",
            {"start": start_ns, "end": end_ns},
        )
        values = data.get("data", [])
        # If this tier is namespace-scoped and the label is itself the
        # namespace label, only surface the allow-listed namespaces.
        if (
            ALLOWED_NAMESPACES is not None
            and label == NAMESPACE_LABEL
        ):
            values = [v for v in values if v in ALLOWED_NAMESPACES]
        return "\n".join(values) if values else "(no values)"
    except Exception as e:
        return ErrorText(f"Error: {e}")


# -- Tool definitions -------------------------------------------------

_LOKI_QUERY_TOOL = Tool(
    name="loki_query",
    description=(
        "Query logs from Loki for a namespace over a time window. Loki retains "
        "logs after a pod is gone, so this is how you investigate a pod that has "
        "already terminated (kubectl logs cannot). The namespace is pinned to this "
        "tier's allow-list; results are capped at LOKI_MAX_LINES."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "namespace": {
                "type": "string",
                "description": "Namespace to read logs from (must be allow-listed for this tier)",
            },
            "selector": {
                "type": "string",
                "description": (
                    'Optional extra label matchers, comma-separated, e.g. '
                    'app="api", pod=~"web-.*". The namespace is applied for '
                    'you, so leave it out.'
                ),
                "default": "",
            },
            "filter": {
                "type": "string",
                "description": 'Optional case-sensitive substring to grep for, e.g. "error"',
                "default": "",
            },
            "since": {
                "type": "string",
                "description": "Time window back from now: 30m, 1h, 2d, 1w (ignored if start is set)",
                "default": DEFAULT_SINCE,
            },
            "start": {
                "type": "string",
                "description": (
                    "Optional explicit window start; overrides since. Relative (2h), "
                    "ISO 8601 with timezone (YYYY-MM-DDTHH:MM:SSZ), or epoch "
                    "seconds/millis."
                ),
                "default": "",
            },
            "end": {
                "type": "string",
                "description": "Optional window end when start is set; same formats. Defaults to now.",
                "default": "now",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to return (capped at LOKI_MAX_LINES)",
                "default": MAX_LINES,
            },
        },
        "required": ["namespace"],
    },
)

_METADATA_TOOLS: list[Tool] = [
    Tool(
        name="loki_labels",
        description="List the log label names Loki knows about in the time window.",
        inputSchema={
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "Time window back from now: 30m, 1h, 2d, 1w",
                    "default": DEFAULT_SINCE,
                },
            },
        },
    ),
    Tool(
        name="loki_label_values",
        description=(
            "List the values a given log label takes in the time window (e.g. "
            "label='app' to discover app names). Namespace values are filtered to "
            "this tier's allow-list."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Label name to enumerate"},
                "since": {
                    "type": "string",
                    "description": "Time window back from now: 30m, 1h, 2d, 1w",
                    "default": DEFAULT_SINCE,
                },
            },
            "required": ["label"],
        },
    ),
]

# -- MCP server -------------------------------------------------------

def release_version() -> str:
    """The release this build reports to clients: LOKI_MCP_VERSION,
    stamped into the image from the loki-mcp-v* tag at build time, else `dev`."""
    return os.environ.get("LOKI_MCP_VERSION", "").strip() or "dev"


_server = Server("loki-mcp", version=release_version())


async def list_tools(_ctx: Any, _params: PaginatedRequestParams) -> ListToolsResult:
    tools: list[Tool] = []
    if EXPOSE_QUERY:
        tools.append(_LOKI_QUERY_TOOL)
    if EXPOSE_METADATA:
        tools.extend(_METADATA_TOOLS)
    return ListToolsResult(tools=tools)


async def call_tool(_ctx: Any, params: CallToolRequestParams) -> CallToolResult:
    text = await _dispatch(params.name, params.arguments or {})
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          is_error=isinstance(text, ErrorText))

# Each handler is registered with its wire method and params model; the runner
# validates a request against the model before calling the handler.
_server.add_request_handler("tools/list", PaginatedRequestParams, list_tools)
_server.add_request_handler("tools/call", CallToolRequestParams, call_tool)


async def _dispatch(name: str, args: dict) -> str:
    if name == "loki_query":
        if not EXPOSE_QUERY:
            return ErrorText(f"Error: tool '{name}' is not enabled on this instance")
        return await _handle_loki_query(args)
    if name == "loki_labels":
        if not EXPOSE_METADATA:
            return ErrorText(f"Error: tool '{name}' is not enabled on this instance")
        return await _handle_loki_labels(args)
    if name == "loki_label_values":
        if not EXPOSE_METADATA:
            return ErrorText(f"Error: tool '{name}' is not enabled on this instance")
        return await _handle_loki_label_values(args)
    return ErrorText(f"Error: unknown tool '{name}'")


# -- Entry point ------------------------------------------------------

if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Mount, Route
        import uvicorn

        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "3000"))

        sse = SseServerTransport("/messages/")

        async def handle_sse(request: Request) -> Response:
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as (read_stream, write_stream):
                await _server.run(
                    read_stream,
                    write_stream,
                    _server.create_initialization_options(),
                )
            # Starlette >= 1.0 calls the endpoint return value as an
            # ASGI app; the SSE handler hijacks the connection, so return an
            # empty Response to avoid "'NoneType' object is not callable".
            return Response()

        web_app = Starlette(
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ]
        )
        uvicorn.run(web_app, host=host, port=port)
    else:
        import asyncio
        from mcp.server.stdio import stdio_server

        async def _run_stdio() -> None:
            async with stdio_server() as (read_stream, write_stream):
                await _server.run(
                    read_stream,
                    write_stream,
                    _server.create_initialization_options(),
                )

        asyncio.run(_run_stdio())
