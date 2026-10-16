# loki-mcp

Read-only Loki (log) MCP server. Each deployment points at one Loki endpoint,
is limited to an allow-listed set of namespaces, and exposes only the tools its
configuration enables. Every `loki_query` is pinned to an allow-listed
`namespace` label matcher.

Loki keeps logs after a pod is gone, so this reads logs of pods that have
already terminated, which `kubectl logs` cannot.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LOKI_URL` | _(required)_ | Loki base URL, e.g. `http://loki.monitoring:3100` |
| `LOKI_ALLOWED_NAMESPACES` | - | Comma-separated namespace allow-list (the tier boundary). When set, `loki_query` requires a namespace and rejects any not on the list. |
| `LOKI_NAMESPACE_LABEL` | `namespace` | The Loki label that carries the k8s namespace |
| `LOKI_TENANT` | - | `X-Scope-OrgID` for multi-tenant Loki |
| `LOKI_TOKEN_FILE` | `/run/secrets/loki-token` | Path to a bearer token (mounted secret); optional |
| `LOKI_TOKEN` | - | Bearer token fallback for local dev |
| `LOKI_MAX_LINES` | `200` | Line cap per result |
| `LOKI_DEFAULT_SINCE` | `1h` | Default time window (`30m`, `1h`, `2d`, `1w`) |
| `LOKI_HTTP_TIMEOUT` | `30` | HTTP timeout (seconds) |
| `EXPOSE_QUERY` | `true` | Enable `loki_query` |
| `EXPOSE_METADATA` | `false` | Enable `loki_labels`, `loki_label_values` |
| `MCP_TRANSPORT` | `stdio` | Set to `sse` for in-cluster HTTP/SSE |
| `MCP_HOST` | `0.0.0.0` | SSE bind host |
| `MCP_PORT` | `3000` | SSE bind port |

Truthy values for boolean flags: `1`, `true`, `yes`, `on` (case-insensitive).

## Tools

- **`loki_query`** (`EXPOSE_QUERY`) - logs for a `namespace` over a `since`
  window, with optional label `selector` and line `filter`. The namespace is
  pinned to the allow-list; results are capped at `LOKI_MAX_LINES`.
- **`loki_labels`** (`EXPOSE_METADATA`) - list known log label names.
- **`loki_label_values`** (`EXPOSE_METADATA`) - values for a label (namespace
  values are filtered to the allow-list).

## Scoping model

The tier boundary is enforced in the
server: `loki_query` always composes `{namespace="<allow-listed>", ...}` itself,
and the optional `selector` is validated as label matchers only, so a caller
cannot break out of the pinned stream selector. Combined with a network policy
that only lets this pod reach Loki, that is the read boundary.

## Run locally

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen
LOKI_URL=http://localhost:3100 LOKI_ALLOWED_NAMESPACES=monitoring \
  EXPOSE_METADATA=true .venv/bin/python server.py     # stdio
```

## Test

```bash
uv sync --frozen --extra dev
LOKI_URL=http://loki.invalid:3100 LOKI_ALLOWED_NAMESPACES=monitoring,default \
  .venv/bin/python -m pytest tests/ -v
```

## Deploy

Build the image from the included `Dockerfile` (`docker build .` or
`podman build .`) and push it to your registry. Run it with
`MCP_TRANSPORT=sse`; the Deployment, Service, Loki credentials secret and
network policy are not part of this repository.

The server reports its release in the MCP `initialize` result
(`serverInfo.version`). To stamp it, build from a checkout with tags:

```
docker build --build-arg LOKI_MCP_VERSION=$(git describe --tags --match 'loki-mcp-v*' | sed 's/^loki-mcp-v//') .
```

A build without it reports `dev`.
