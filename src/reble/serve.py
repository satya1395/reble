"""reble serve: the network skin of the branch resolver.

A local, read-only Iceberg REST catalog proxy. Any Iceberg-speaking client
(pyiceberg, DuckDB's iceberg extension, Spark, Trino) connects to it and
sees the warehouse exactly as the CURRENT reble branch sees it: scoped
tables at the branch ref, everything else at its pinned/epoch snapshot.

The one trick (docs/design/serve.md): the LoadTable response carries
synthesized metadata whose current-snapshot-id is whatever
`BranchEngine.resolve_read()` answers, with reble's private branch refs
stripped. Clients then read manifests and Parquet DIRECTLY from the
warehouse with their own credentials — no data ever flows through this
process, only kilobytes of metadata.

Design constraints:
- Read-only by construction: any write verb answers 405. External writes
  would bypass the branch guard.
- Resolution is computed per request: a `reble run` mid-session is visible
  on the client's next (re)load of the table.
- Single-threaded on purpose: the engine's SQLite handles live in the
  thread that built the server. Build the server in the thread that runs it
  (`make_server` + `serve_forever` in the same thread).
- Synthesized metadata is also written to `.reble/serve/` and advertised as
  the metadata-location, so clients that re-read the pointed-at file instead
  of trusting the inline document still see the branch view.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlparse

from reble.branches import EPOCH_EMPTY, BranchEngine
from reble.config import RebleConfig

_WRITE_ERROR = ("reble serve is read-only: writes go through `reble run` "
                "and the branch guard, never through the catalog proxy")


def _resolved_metadata(engine: BranchEngine, ident: str) -> dict | None:
    """The LoadTable payload for `ident`, as the current branch sees it."""
    from pyiceberg.exceptions import NoSuchTableError
    try:
        tbl = engine.catalog.load_table(ident)
    except NoSuchTableError:
        return None
    md = tbl.metadata.model_dump(mode="json", by_alias=True, exclude_none=True)

    reble_branches = {m.name for m in engine.state.list()}
    refs = {k: v for k, v in md.get("refs", {}).items()
            if k not in reble_branches}

    snap = engine.resolve_read(ident)
    if snap == EPOCH_EMPTY:
        # born after the branch epoch: clients must see an empty table
        md.pop("current-snapshot-id", None)
        refs.pop("main", None)
    elif snap is not None:
        md["current-snapshot-id"] = snap
        refs["main"] = {"snapshot-id": snap, "type": "branch"}
        # some clients (DuckDB's REST attach) resolve "current" from the
        # snapshot-log's last entry, not current-snapshot-id — rewrite it
        ts = next((sn["timestamp-ms"] for sn in md.get("snapshots", [])
                   if sn["snapshot-id"] == snap), None)
        if ts is not None:
            md["snapshot-log"] = [{"snapshot-id": snap, "timestamp-ms": ts}]
            md["last-updated-ms"] = ts
    # snap is None -> current main state; pass through untouched
    md["refs"] = refs
    if snap == EPOCH_EMPTY:
        md["snapshot-log"] = []

    # persist the synthesized document so metadata-location agrees with the
    # inline metadata (some clients re-read the location)
    serve_dir = engine.cfg.project_dir / ".reble" / "serve"
    serve_dir.mkdir(parents=True, exist_ok=True)
    tag = "empty" if snap == EPOCH_EMPTY else (snap or "main")
    path = serve_dir / f"{ident}.{tag}.metadata.json"
    path.write_text(json.dumps(md))

    return {
        "metadata-location": f"file://{path}",
        "metadata": md,
        "config": {},
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "reble-serve"
    # HTTP/1.0 + Connection: close, deliberately: this server is
    # single-threaded (SQLite thread affinity), and a kept-alive idle client
    # would serialize everyone else behind it. Metadata responses are a few
    # KB — per-request connections cost nothing measurable.
    protocol_version = "HTTP/1.0"
    timeout = 10

    # -- plumbing --------------------------------------------------------------
    def log_message(self, fmt, *args):        # quiet by default
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, kind: str) -> None:
        self._json(status, {"error": {"message": message, "type": kind,
                                      "code": status}})

    def _route(self) -> list[str]:
        """Path segments after /v1, with any catalog prefix stripped."""
        parts = [unquote(p) for p in
                 urlparse(self.path).path.split("/") if p]
        if "v1" not in parts:
            return []
        parts = parts[parts.index("v1") + 1:]
        # accept both /v1/namespaces/... and /v1/{prefix}/namespaces/...
        if len(parts) >= 2 and parts[0] != "config" \
                and parts[0] != "namespaces" and parts[1] == "namespaces":
            parts = parts[1:]
        return parts

    # -- read endpoints --------------------------------------------------------
    def do_GET(self):                                       # noqa: N802
        eng: BranchEngine = self.server.engine
        r = self._route()
        if r == ["config"]:
            self._json(200, {"defaults": {}, "overrides": {}})
        elif r == ["namespaces"]:
            self._json(200, {"namespaces": [list(ns) for ns
                                            in eng.catalog.list_namespaces()]})
        elif len(r) == 2 and r[0] == "namespaces":
            if any(".".join(ns) == r[1] for ns in eng.catalog.list_namespaces()):
                self._json(200, {"namespace": r[1].split("."), "properties": {}})
            else:
                self._error(404, f"namespace {r[1]!r} not found",
                            "NoSuchNamespaceException")
        elif len(r) == 3 and r[0] == "namespaces" and r[2] == "tables":
            idents = [{"namespace": r[1].split("."), "name": t[-1]}
                      for t in eng.catalog.list_tables(r[1])]
            self._json(200, {"identifiers": idents})
        elif len(r) == 4 and r[0] == "namespaces" and r[2] == "tables":
            payload = _resolved_metadata(eng, f"{r[1]}.{r[3]}")
            if payload is None:
                self._error(404, f"table {r[1]}.{r[3]} not found",
                            "NoSuchTableException")
            else:
                self._json(200, payload)
        else:
            self._error(404, f"no such route: {self.path}", "NotFound")

    def do_HEAD(self):                                      # noqa: N802
        eng: BranchEngine = self.server.engine
        r = self._route()
        ok = (len(r) == 4 and r[0] == "namespaces" and r[2] == "tables"
              and _resolved_metadata(eng, f"{r[1]}.{r[3]}") is not None)
        self.send_response(200 if ok else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- everything that could write ------------------------------------------
    def _refuse(self):
        self._error(405, _WRITE_ERROR, "NotAuthorizedException")

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse


class RebleRestServer(HTTPServer):
    def __init__(self, cfg: RebleConfig, host: str, port: int,
                 verbose: bool = False):
        super().__init__((host, port), _Handler)
        # built here so the engine's SQLite handles live in the serving thread
        self.engine = BranchEngine(cfg)
        self.verbose = verbose


def make_server(cfg: RebleConfig, host: str = "127.0.0.1",
                port: int = 8181, verbose: bool = False) -> RebleRestServer:
    return RebleRestServer(cfg, host, port, verbose=verbose)
