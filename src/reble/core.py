"""Headless core: every verb as a library call — one core, every frontend.

The CLI and the MCP server are both thin adapters over this module. Each
verb returns the full JSON envelope dict (spec §6) — identical output for
every frontend. Failures that still produce output (run with model errors,
status drift) raise RebleError with ``payload`` set to the envelope; adapters
emit/return the payload, then translate ``exit_code`` into a process exit
(CLI) or a structured error object (MCP).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import envelope
from .catalog import (
    drop_ref,
    ensure_branch,
    get_head,
    get_ref_snapshot,
    load_catalog,
    snapshot_id_of,
)
from .config import ConfigLoader
from .diff import diff_arrow, diff_snapshots, resolve_keys
from .duckio import DuckIo
from .engine import DuckDbEngine, SparkEngine
from .errors import DriftError, EmptyScope, RebleError
from .events import EventCallback
from .gitinfo import base_commit, current_branch, file_at, head_commit, repo_root, uncommitted_files
from .lineage import Graph, ast_hash, build_graph
from .naming import disambiguate, sanitize_branch_name
from .promote import Promoter, orphan_pin_tags
from .relations import data_stale_models, relation_id, table_for_model, tag_name
from .runner import Runner
from .scope import compute_scope
from .state import BranchState, Pin, StateStore


def branch_env(git_branch: str | None, data_branch: str, changeset_id: str | None) -> dict:
    """Envelope `branch` object — additive field: changeset (SPEC v0.2)."""
    return {"git": git_branch, "data": data_branch, "changeset": changeset_id}


def _manifest_hashes(manifest_path: Path) -> dict[str, str]:
    """Per-model AST hashes from a run manifest (hash-baseline fallback)."""
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {r["model"]: r["ast_hash"] for r in manifest.get("results", []) if r.get("ast_hash")}


def _relpath(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root))
    except ValueError:
        return path


def list_tables(catalog) -> list[str]:
    out: list[str] = []
    try:
        for ns in catalog.list_namespaces():
            out.extend(catalog.list_tables(ns))
    except Exception:  # noqa: BLE001
        try:
            out.extend(catalog.list_tables())
        except Exception:  # noqa: BLE001
            pass
    return out


def _empty_like(table):
    import pyarrow as pa

    return pa.table({f.name: [] for f in table.schema}, schema=table.schema)


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str))


def _snapshot_summary(table, snapshot_id: int) -> dict:
    """{records, bytes} from the snapshot summary; zeros if absent."""
    try:
        snapshot = table.snapshot_by_id(snapshot_id)
        props = dict(snapshot.summary.additional_properties or {}) if snapshot else {}
    except Exception:  # noqa: BLE001
        props = {}
    return {
        "records": int(props.get("total-records", 0)),
        "bytes": int(props.get("total-files-size", 0)),
    }


class Reble:
    """One instance per project (+ optional profile). Verbs return envelopes."""

    def __init__(self, project_root: Path | None = None, profile: str | None = None):
        self.project_root = (project_root or Path.cwd()).resolve()
        self.loader = ConfigLoader(self.project_root)
        self.cfg = self.loader.load(profile=profile)
        self.reble_dir = self.loader.reble_dir
        sc = self.cfg.state
        self.store = StateStore(
            store=sc.store, uri=sc.uri, reble_dir=self.reble_dir
        )
        self.store.validate()  # exit 2 before any verb if backend is broken
        self.state = self.store.load()
        self.catalog = load_catalog(self.cfg.warehouse.catalog)
        engine_cls = (
            SparkEngine if self.cfg.compute_policy.prefer == "spark" else DuckDbEngine
        )
        self.engine = engine_cls(self.cfg, self.catalog)

    # ------------------------------------------------------------- resolution

    @property
    def git_branch(self) -> str | None:
        """Invariant 1: reble reads git, never runs it — and git_sync: false
        makes reble 100% git-ignorant."""
        if not self.cfg.branching.git_sync:
            return None
        root = repo_root(self.project_root)
        return current_branch(root) if root else None

    def changeset(self, explicit: str | None = None) -> tuple[str, str]:
        """Change-set id and its source. Precedence: --change-set flag →
        REBLE_CHANGE_SET env → git branch (when git_sync). The change-set id
        is the primary state key; git is one derivation adapter."""
        if explicit:
            return explicit, "explicit"
        env = os.environ.get("REBLE_CHANGE_SET")
        if env:
            return env, "env"
        git = self.git_branch
        if git is not None:
            return git, "git"
        if not self.cfg.branching.git_sync:
            # git-less local projects get a stable single-user change-set
            return "local", "default"
        raise RebleError(
            "No change-set: pass --change-set NAME, set REBLE_CHANGE_SET, or "
            "run inside a git branch (or set git_sync: false for the 'local' default)",
            exit_code=2,
        )

    def data_branch_for(
        self,
        changeset_id: str | None = None,
        explicit_branch: str | None = None,
    ) -> str:
        """Data branch for a change-set. The recorded mapping in state wins;
        disambiguation is only for a genuinely new change-set whose sanitized
        name is taken (e.g. another engineer's branch)."""
        if explicit_branch:
            return explicit_branch
        if changeset_id is None:
            raise RebleError(
                "No change-set id available. Create an explicit data branch: "
                "reble branch create <name>",
                exit_code=2,
            )
        st = self.state.branches.get(changeset_id)
        if st is not None:
            return st.data_branch
        sanitized = sanitize_branch_name(changeset_id, self.cfg.branching.name_sanitization)
        # The default base is not a name collision — it IS the base. Running
        # on main (the nightly-refresh case) must target main itself, not a
        # suffixed branch.
        if sanitized == sanitize_branch_name(
            self.cfg.warehouse.default_base, self.cfg.branching.name_sanitization
        ):
            return sanitized
        return disambiguate(sanitized, self._existing_refs())

    def _existing_refs(self) -> set[str]:
        refs: set[str] = set()
        for table_id in list_tables(self.catalog):
            try:
                refs.update(self.catalog.load_table(table_id).refs().keys())
            except Exception:  # noqa: BLE001
                continue
        return refs

    def branch_state(self, changeset_id: str, data_branch: str) -> BranchState:
        st = self.state.branches.get(changeset_id) or self._state_by_branch(data_branch)
        if st is None:
            raise RebleError(
                f"No data branch for change-set '{changeset_id}'. Run `reble run` "
                "or `reble branch create` first.",
                exit_code=2,
            )
        return st

    def _state_by_branch(self, data_branch: str) -> BranchState | None:
        for st in self.state.branches.values():
            if st.data_branch == data_branch:
                return st
        return None

    def graph(self) -> Graph:
        return build_graph(
            self.project_root / self.cfg.lineage.models_path, self.cfg.lineage.dialect
        )

    def hash_baseline(self, st: BranchState | None, data_branch: str) -> dict[str, str]:
        """AST-hash baseline for edited detection.

        Primary: the change-set's stored hashes. Fallback: the data branch's
        last run manifest — so a new change-set resuming an existing data
        branch stays incremental (the agent case). Returns {} for a genuinely
        fresh branch (bootstrap rule: declare --models or get empty scope).
        """
        if st is not None and st.model_hashes:
            return dict(st.model_hashes)
        if st is not None and st.last_run_id:
            hashes = self.store.load_run_hashes(st.last_run_id)
            if hashes:
                return hashes
        prior = self._state_by_branch(data_branch)
        if prior is not None and prior.last_run_id:
            return self.store.load_run_hashes(prior.last_run_id)
        return {}

    def edited_models(
        self, graph: Graph, st: BranchState | None, data_branch: str
    ) -> tuple[list[str], dict[str, str]]:
        """Edited = git-diff vs base commit (git_sync) ∪ AST-changed vs hash baseline."""
        edited: set[str] = set()
        dialect = self.cfg.lineage.dialect
        hashes = {n: ast_hash(m.sql, dialect) for n, m in graph.models.items() if m.sql.strip()}
        baseline = self.hash_baseline(st, data_branch)

        if self.cfg.branching.git_sync:
            root = repo_root(self.project_root)
            base = base_commit(root) if root else None
            if root and base:
                for name, model in graph.models.items():
                    old_sql = file_at(root, base, _relpath(model.path, root))
                    if old_sql is None or ast_hash(old_sql, dialect) != hashes[name]:
                        edited.add(name)

        for name, h in hashes.items():
            if baseline.get(name) not in (None, h):
                edited.add(name)

        return sorted(edited), hashes

    def keys_for(self, graph: Graph, model_name: str) -> list[str]:
        model = graph.models.get(model_name)
        inferred = model.diff_keys if model else []
        explicit = self.cfg.diff.keys.get(model_name)
        return explicit or inferred

    # ------------------------------------------------------------------ verbs

    def run(
        self,
        models: list[str] | str | None = None,
        depth: int | None = None,
        dry_run: bool = False,
        engine: str | None = None,
        change_set: str | None = None,
        branch: str | None = None,
        refresh: bool = False,
        force: bool = False,
        on_event: EventCallback | None = None,
    ) -> dict:
        """Resolve scope, create/update the data branch, pin inputs, execute."""
        if engine == "spark":
            self.engine = SparkEngine(self.cfg, self.catalog)
        changeset_id, key_source = self.changeset(change_set)
        git_branch = self.git_branch
        data_branch = self.data_branch_for(changeset_id, explicit_branch=branch)
        graph = self.graph()

        st = self.state.branches.get(changeset_id) or self._state_by_branch(data_branch)
        if refresh and models:
            raise RebleError("--refresh and --models are mutually exclusive", exit_code=2)
        if force and dry_run:
            raise RebleError("--force with --dry-run has no effect", exit_code=2)
        edited, _ = self.edited_models(graph, st, data_branch)
        if models:
            if isinstance(models, str):
                models = [m.strip() for m in models.split(",") if m.strip()]
            edited = list(models)
        elif refresh:
            # data-driven scope: models whose upstream snapshots moved,
            # closed downward exactly like an edit-driven scope
            edited = data_stale_models(self.cfg, self.catalog, graph)
        elif force:
            # --force means full rebuild: every model is in scope, even with
            # nothing edited (engine switch, rebuilding on a fresh branch).
            edited = [name for name, m in graph.models.items() if m.sql.strip()]

        scope = compute_scope(graph, edited, depth=depth)

        # Branch-first with an empty scope is legal (invariant 5): register state.
        if st is None:
            root = repo_root(self.project_root)
            st = BranchState(
                git_branch=git_branch or "",
                data_branch=data_branch,
                base_ref=self.cfg.warehouse.default_base,
                base_commit=head_commit(root) if root else None,
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                key_source=key_source,
            )
            self.state.branches[changeset_id] = st
        st.scope = scope.scope
        # Persist before execution: a crashed run must not lose the change-set↔
        # data-branch mapping or the branch epoch (invariant 5).
        self.store.save(self.state)

        runner = Runner(
            self.cfg, self.catalog, graph, self.engine, self.reble_dir,
            save_manifest=self.store.save_run_manifest,
        )

        if scope.is_empty:
            return envelope.envelope(
                "run",
                ok=True,
                data={"status": "empty scope — branch registered (invariant 6)"},
                branch=branch_env(git_branch, data_branch, changeset_id),
            )

        preflight = runner.preflight(data_branch, scope)
        warnings = [
            f"{m}: no diff key (on_missing_key: {self.cfg.diff.on_missing_key})"
            for m in scope.scope
            if not self.keys_for(graph, m)
        ]

        if dry_run:
            return envelope.envelope(
                "run",
                ok=True,
                data={"dry_run": True, "preflight": _jsonable(preflight)},
                branch=branch_env(git_branch, data_branch, changeset_id),
                warnings=warnings,
            )

        def record_model(model_name: str, ast_hash: str) -> None:
            # Persist per-model progress as it commits: a crashed run leaves
            # completed models recorded, so a retry resumes instead of
            # re-running them (replace-never-append keeps even a full retry
            # correct — this makes it cheap too).
            st.model_hashes[model_name] = ast_hash
            self.store.save(self.state)

        manifest = runner.run(
            data_branch,
            scope,
            st.base_ref,
            {} if force else st.model_hashes,  # --force: rebuild the scope
            changeset_id=changeset_id,
            on_event=on_event,
            on_model_complete=record_model,
        )

        # Record execution hashes, input pins, and scope-table base heads.
        st.model_hashes.update({r.model: r.ast_hash for r in manifest.results if r.ast_hash})
        for table in scope.pinned_inputs:
            table_id = relation_id(self.cfg, table)
            snapshot = get_head(self.catalog, table_id, st.base_ref)
            if snapshot is not None:
                st.pins[table] = Pin(
                    table=table_id,
                    tag=tag_name(self.cfg, data_branch, table),
                    snapshot_id=snapshot,
                    base_snapshot_id=snapshot,
                )
        for model in scope.scope:
            table_id = table_for_model(self.cfg, model)
            head = get_head(self.catalog, table_id, st.base_ref)
            branch_head = get_head(self.catalog, table_id, data_branch)
            if head is not None or branch_head is not None:
                st.base_heads[table_id] = head if head is not None else -1
        st.last_run_id = manifest.run_id
        self.store.save(self.state)

        data = manifest.to_dict()
        data["preflight"] = _jsonable(preflight)
        env = envelope.envelope(
            "run",
            ok=all(r.status != "error" for r in manifest.results),
            data=data,
            branch=branch_env(git_branch, data_branch, changeset_id),
            warnings=warnings + getattr(runner, "io_warnings", []),
        )
        failed = [r for r in manifest.results if r.status == "error"]
        if failed:
            raise RebleError(
                f"run failed for {len(failed)} model(s): "
                + ", ".join(f"{r.model} ({r.error})" for r in failed),
                payload=env,
            )
        return env

    def diff(
        self,
        tables: list[str] | str | None = None,
        against: str = "base",
        schema_only: bool = False,
        rows: int | None = None,
        full: bool = False,
        change_set: str | None = None,
        branch: str | None = None,
        on_event: EventCallback | None = None,
    ) -> dict:
        """Row-level + schema diff of scope tables (exit 7 if key required and missing)."""
        changeset_id, _ = self.changeset(change_set)
        git_branch = self.git_branch
        data_branch = self.data_branch_for(changeset_id, explicit_branch=branch)
        st = self.branch_state(changeset_id, data_branch)
        graph = self.graph()

        if isinstance(tables, str):
            targets = [t.strip() for t in tables.split(",") if t.strip()]
        else:
            targets = list(tables) if tables else st.scope
        if not targets:
            raise EmptyScope("no scope to diff — run `reble run` first")

        base_ref = st.base_ref if against == "base" else against
        # None = unlimited (--full); 0 = counts only; otherwise the cap
        max_rows = (
            None if full else (rows if rows is not None else self.cfg.diff.max_rows_dumped)
        )
        io = DuckIo(self.cfg.engines.duckdb, self.reble_dir)
        warnings: list[str] = []

        out = []
        for name in targets:
            if on_event:
                on_event("diff.table.begin", table=name)
            table_id = relation_id(self.cfg, name)
            table = self.catalog.load_table(table_id)
            branch_snap = get_ref_snapshot(table, data_branch)
            base_snap = get_ref_snapshot(table, base_ref)
            if branch_snap is None:
                raise RebleError(f"{table_id}: no branch ref '{data_branch}' to diff against")

            con = io.connect()
            try:
                io.register_snapshot(con, "branch_tbl", table, branch_snap, warnings)
                if base_snap is not None:
                    io.register_snapshot(con, "base_tbl", table, base_snap, warnings)
                else:
                    # new-model branch: main holds only the zero-row seed
                    con.register(
                        "base_tbl",
                        _empty_like(table.scan(snapshot_id=branch_snap).to_arrow()),
                    )

                model = graph.models.get(name)
                inferred = model.diff_keys if model else []
                keys = resolve_keys(
                    name, self.cfg.diff.keys, inferred, self.cfg.diff.on_missing_key
                )
                if schema_only:
                    d = diff_snapshots(con, table_id, keys=None, max_rows_dumped=0)
                    d.added_count = d.removed_count = d.changed_count = 0
                    d.added_samples = d.removed_samples = d.changed_samples = []
                    d.warning = None
                else:
                    d = diff_snapshots(con, table_id, keys, max_rows_dumped=max_rows)
            finally:
                con.close()
            out.append(d.to_dict())
            if on_event:
                on_event(
                    "diff.table.end",
                    table=table_id,
                    added=d.added_count,
                    removed=d.removed_count,
                    changed=d.changed_count,
                )

        diff_dir = self._save_diff(changeset_id, against, out)

        return envelope.envelope(
            "diff",
            ok=True,
            data={"tables": out, "diff_dir": str(diff_dir)},
            branch=branch_env(git_branch, data_branch, changeset_id),
            warnings=warnings,
        )

    def _save_diff(self, changeset_id: str, against: str, tables: list[dict]) -> Path:
        """Persist per-table diff JSON under .reble/diffs/<change-set>/.

        Refreshed in place per change-set (and per --against), so a change-set
        has exactly one current diff on disk — detail lives here, not in the
        terminal. One file per table keeps folders navigable at 300 tables.
        """
        import json as _json
        import re as _re

        safe_cs = _re.sub(r"[^A-Za-z0-9._-]", "_", changeset_id)
        folder = safe_cs if against == "base" else f"{safe_cs}__vs_{against}"
        diff_dir = self.reble_dir / "diffs" / folder
        diff_dir.mkdir(parents=True, exist_ok=True)
        summary = {}
        for t in tables:
            name = _re.sub(r"[^A-Za-z0-9._-]", "_", t["table"])
            path = diff_dir / f"{name}.json"
            path.write_text(_json.dumps(t, indent=2, default=str))
            summary[t["table"]] = {
                "added": t["added"],
                "removed": t["removed"],
                "changed": t["changed"],
                "file": path.name,
            }
        (diff_dir / "summary.json").write_text(
            _json.dumps(summary, indent=2, default=str)
        )
        return diff_dir

    def estimate(
        self,
        models: list[str] | str | None = None,
        depth: int | None = None,
        change_set: str | None = None,
        branch: str | None = None,
        refresh: bool = False,
    ) -> dict:
        """Rough, local cost estimate from Iceberg metadata only.

        Honest about being rough (spec: accurate estimation is a non-goal):
        per scope table and pinned input, snapshot summaries give row counts
        and file bytes — nothing is scanned. Bytes estimate what a run+diff
        will read: pinned inputs (base) plus branch and base of each scope
        table.
        """
        changeset_id, _ = self.changeset(change_set)
        git_branch = self.git_branch
        data_branch = self.data_branch_for(changeset_id, explicit_branch=branch)
        graph = self.graph()
        st = self.state.branches.get(changeset_id) or self._state_by_branch(data_branch)

        edited, _ = self.edited_models(graph, st, data_branch)
        if models:
            if isinstance(models, str):
                models = [m.strip() for m in models.split(",") if m.strip()]
            edited = list(models)
        elif refresh:
            edited = data_stale_models(self.cfg, self.catalog, graph)
        scope = compute_scope(graph, edited, depth=depth)

        if scope.is_empty:
            return envelope.envelope(
                "estimate",
                ok=True,
                data={"models": 0, "status": "empty scope"},
                branch=branch_env(git_branch, data_branch, changeset_id),
            )

        tables = []
        est_bytes = 0
        est_input_rows = 0

        def measure(table_id: str, role: str) -> None:
            nonlocal est_bytes, est_input_rows
            try:
                table = self.catalog.load_table(table_id)
            except Exception:  # noqa: BLE001 — not materialized yet
                tables.append({"table": table_id, "role": role, "records": 0, "bytes": 0,
                               "note": "not materialized yet"})
                return
            snapshot_ids = []
            if role == "scope":
                snapshot_ids = [
                    sid for sid in (
                        get_ref_snapshot(table, data_branch),
                        get_ref_snapshot(table, st.base_ref if st else self.cfg.warehouse.default_base),
                    ) if sid is not None
                ]
            else:
                sid = get_ref_snapshot(
                    table, st.base_ref if st else self.cfg.warehouse.default_base
                )
                snapshot_ids = [sid] if sid is not None else []
            records = 0
            size = 0
            seen: set[int] = set()
            for sid in snapshot_ids:
                if sid in seen:
                    continue
                seen.add(sid)
                summary = _snapshot_summary(table, sid)
                records += summary.get("records", 0)
                size += summary.get("bytes", 0)
            est_bytes += size
            if role == "input":
                est_input_rows += records
            tables.append({"table": table_id, "role": role, "records": records, "bytes": size})

        for model in scope.scope:
            measure(table_for_model(self.cfg, model), "scope")
        for rel in sorted(scope.pinned_inputs):
            measure(relation_id(self.cfg, rel), "input")

        return envelope.envelope(
            "estimate",
            ok=True,
            data={
                "models": len(scope.scope),
                "tables": tables,
                "est_bytes_read": est_bytes,
                "est_input_rows": est_input_rows,
            },
            branch=branch_env(git_branch, data_branch, changeset_id),
            warnings=[
                (
                    "rough estimate from snapshot summaries; file-level stats and "
                    "predicate pushdown not accounted for"
                )
            ],
        )

    def status(self, change_set: str | None = None, branch: str | None = None) -> dict:
        """The 'where was I?' answer. Read-only, CI-safe. Raises DriftError (3) on drift."""
        changeset_id, _ = self.changeset(change_set)
        git_branch = self.git_branch
        data_branch = self.data_branch_for(changeset_id, explicit_branch=branch)
        st = self.state.branches.get(changeset_id) or self._state_by_branch(data_branch)

        code_section: dict = {}
        data_section: dict = {}
        warnings: list[str] = []
        drift = False

        if self.cfg.branching.git_sync:
            root = repo_root(self.project_root)
            code_section["uncommitted"] = uncommitted_files(root) if root else []

        if st is None:
            data_section["branch"] = None
            warnings.append("no data branch yet — run `reble run`")
        else:
            data_section["branch"] = st.data_branch
            data_section["last_run_id"] = st.last_run_id
            data_section["scope"] = st.scope
            graph = self.graph()
            edited, hashes = self.edited_models(graph, st, data_branch)
            data_section["edited_since_branch_point"] = edited
            # Un-run = edited models whose current SQL does not match the last run
            data_section["un_run_changes"] = [
                m for m in edited if st.model_hashes.get(m) != hashes.get(m)
            ]

            promoter = Promoter(
                self.cfg, self.catalog, self.reble_dir,
                load_record=self.store.load_promote_record,
            )
            drifted = [r for r in promoter.preflight(st) if r.drifted]
            if drifted:
                drift = True
                data_section["drifted"] = [
                    {
                        "table": r.table,
                        "kind": r.kind,
                        "expected": r.pinned_base,
                        "current_main": r.current_main,
                    }
                    for r in drifted
                ]
                warnings += [
                    f"{r.table}: {r.kind} base {r.pinned_base} != current main {r.current_main}"
                    for r in drifted
                ]
            data_section["age_days"] = round((time.time() - st.epoch) / 86400, 1)
            data_section["expires_in_days"] = round(
                max(0, self.cfg.branching.ttl_days - data_section["age_days"]), 1
            )

        env = envelope.envelope(
            "status",
            ok=not drift,
            data={"code": code_section, "data": data_section},
            branch=branch_env(git_branch, data_branch, changeset_id),
            warnings=warnings,
        )
        if drift:
            raise DriftError("drift detected", payload=env)
        return env

    def promote(
        self,
        ff_only: bool = False,
        dry_run: bool = False,
        change_set: str | None = None,
        branch: str | None = None,
    ) -> dict:
        """Fast-forward promote — only when pinned bases still equal current main."""
        changeset_id, _ = self.changeset(change_set)
        git_branch = self.git_branch
        data_branch = self.data_branch_for(changeset_id, explicit_branch=branch)
        st = self.branch_state(changeset_id, data_branch)

        promoter = Promoter(
            self.cfg, self.catalog, self.reble_dir, persist=lambda: self.store.save(self.state),
            load_record=self.store.load_promote_record,
            save_record=self.store.save_promote_record,
            delete_record=self.store.delete_promote_record,
        )
        reports = promoter.preflight(st)
        drifted = [r for r in reports if r.drifted]

        if dry_run:
            return envelope.envelope(
                "promote",
                ok=True,
                data={
                    "dry_run": True,
                    "preflight": [
                        {
                            "table": r.table,
                            "kind": r.kind,
                            "pinned_base": r.pinned_base,
                            "current_main": r.current_main,
                        }
                        for r in reports
                    ],
                    "would": "fast-forward" if not drifted else "re-run + fresh diff + fast-forward",
                    "atomicity": "per-table fast-forwards (atomic on a reble catalog)",
                },
                branch=branch_env(git_branch, data_branch, changeset_id),
            )

        warnings: list[str] = []
        promote_diff: dict[str, dict] = {}
        if drifted:
            warnings.append(
                f"drift on {len(drifted)} tables — re-pinning, re-running scope, "
                "emitting a fresh promote-time diff"
            )
            if ff_only:
                raise RebleError(
                    "promote blocked: drift detected and --ff-only given (exit 4)",
                    exit_code=4,
                )
            graph = self.graph()
            scope = compute_scope(graph, st.scope)
            runner = Runner(
            self.cfg, self.catalog, graph, self.engine, self.reble_dir,
            save_manifest=self.store.save_run_manifest,
        )
            manifest = runner.run(data_branch, scope, st.base_ref, {})
            for table in scope.pinned_inputs:
                table_id = relation_id(self.cfg, table)
                snapshot = get_head(self.catalog, table_id, st.base_ref)
                if snapshot is not None:
                    st.pins[table] = Pin(
                        table=table_id,
                        tag=tag_name(self.cfg, data_branch, table),
                        snapshot_id=snapshot,
                        base_snapshot_id=snapshot,
                    )
            for model in scope.scope:
                table_id = table_for_model(self.cfg, model)
                head = get_head(self.catalog, table_id, st.base_ref)
                if head is not None:
                    st.base_heads[table_id] = head
            st.model_hashes.update({r.model: r.ast_hash for r in manifest.results if r.ast_hash})
            self.store.save(self.state)

            # Authoritative promote-time diff (PR diffs are advisory; this is not).
            for model in scope.scope:
                table_id = table_for_model(self.cfg, model)
                try:
                    table = self.catalog.load_table(table_id)
                    b = get_ref_snapshot(table, data_branch)
                    m = get_ref_snapshot(table, st.base_ref)
                    if b is not None and m is not None:
                        branch_data = table.scan(snapshot_id=b).to_arrow()
                        base_data = table.scan(snapshot_id=m).to_arrow()
                        model_node = graph.models.get(model)
                        keys = resolve_keys(
                            model,
                            self.cfg.diff.keys,
                            model_node.diff_keys if model_node else [],
                            self.cfg.diff.on_missing_key,
                        )
                        d = diff_arrow(
                            table_id, base_data, branch_data, keys,
                            max_rows_dumped=self.cfg.diff.max_rows_dumped,
                        )
                        promote_diff[table_id] = d.to_dict()
                except RebleError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    promote_diff[table_id] = {"error": str(exc)}

        results = promoter.promote(st, ff_only=ff_only)
        # The promoter advanced base heads per table (and persisted via callback);
        # persist once more so any non-table state is durable even on partial failure.
        self.store.save(self.state)
        if all(r.get("status") != "failed" for r in results.values()):
            # Fully promoted: the branch's pins advance to main too, so
            # `reble status` is clean (the branch epoch advances to the promote
            # point). Base heads were already advanced per table.
            for pin in st.pins.values():
                head = get_head(self.catalog, pin.table, st.base_ref)
                if head is not None:
                    pin.snapshot_id = head
                    pin.base_snapshot_id = head
            self.store.save(self.state)
        return envelope.envelope(
            "promote",
            ok=all(r.get("status") != "failed" for r in results.values()),
            data={
                "results": results,
                "promote_diff": promote_diff,
                "atomicity": "per-table fast-forwards (atomic on a reble catalog)",
            },
            branch=branch_env(git_branch, data_branch, changeset_id),
            warnings=warnings,
        )

    def branch_create(
        self, name: str, from_ref: str | None = None, change_set: str | None = None
    ) -> dict:
        """Explicit branch (required when git_sync: false). Zero-copy refs."""
        base_ref = from_ref or self.cfg.warehouse.default_base
        _ = self.graph()  # validates the model registry before touching the catalog
        final_name = disambiguate(name, self._existing_refs() - {name})
        created = 0
        warnings: list[str] = []
        for table_id in list_tables(self.catalog):
            try:
                ensure_branch(self.catalog, table_id, final_name, base_ref)
                created += 1
            except Exception as exc:  # noqa: BLE001 — tables without snapshots are skipped
                warnings.append(f"skip {table_id}: {exc}")
        if change_set:
            changeset_id, key_source = change_set, "explicit"
        else:
            try:
                changeset_id, key_source = self.changeset()
            except RebleError:
                # git-less bootstrap: the branch itself is the change-set key
                changeset_id, key_source = final_name, "explicit"
        self.state.branches[changeset_id] = BranchState(
            git_branch=self.git_branch or "",
            data_branch=final_name,
            base_ref=base_ref,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            key_source=key_source,
        )
        self.store.save(self.state)
        return envelope.envelope(
            "branch create",
            ok=True,
            data={"branch": final_name, "base_ref": base_ref, "tables": created},
            branch=branch_env(self.git_branch, final_name, changeset_id),
            warnings=warnings,
        )

    def branch_list(self) -> dict:
        branches = [
            {
                "changeset": k,
                "key_source": b.key_source,
                "data_branch": b.data_branch,
                "base_ref": b.base_ref,
                "scope": b.scope,
                "age_days": round((time.time() - b.epoch) / 86400, 1),
            }
            for k, b in self.state.branches.items()
        ]
        return envelope.envelope("branch list", ok=True, data={"branches": branches})

    def branch_show(self, name: str) -> dict:
        """Catalog refs + pin tags + local state for one branch."""
        refs: list[dict] = []
        for table_id in list_tables(self.catalog):
            try:
                table = self.catalog.load_table(table_id)
            except Exception:  # noqa: BLE001
                continue
            for ref_name, ref in table.refs().items():
                if name in ref_name:
                    refs.append(
                        {
                            "table": table_id,
                            "ref": ref_name,
                            "snapshot": snapshot_id_of(ref),
                        }
                    )
        return envelope.envelope("branch show", ok=True, data={"refs": refs})

    def branch_discard(self, name: str) -> dict:
        """Drop branch refs and pin tags; refuses if promote was in progress."""
        if self.store.load_promote_record(name) is not None:
            raise RebleError(
                "promote in progress — resume it or let it finish first"
            )
        dropped = 0
        for table_id in list_tables(self.catalog):
            try:
                table = self.catalog.load_table(table_id)
                refs = dict(table.refs())
            except Exception:  # noqa: BLE001
                continue
            for ref_name in refs:
                if ref_name == name:
                    drop_ref(self.catalog, table_id, name, "branch")
                    dropped += 1
                elif ref_name.startswith(self.cfg.branching.tag_prefix + name + "__"):
                    drop_ref(self.catalog, table_id, ref_name, "tag")
                    dropped += 1
        for key, st in list(self.state.branches.items()):
            if st.data_branch == name:
                del self.state.branches[key]
        self.store.save(self.state)
        return envelope.envelope(
            "branch discard", ok=True, data={"dropped_refs": dropped}
        )

    def gc(self, before_days: int | None = None, dry_run: bool = False) -> dict:
        """Expire TTL'd branches, drop orphan pin tags. GC is a correctness command."""
        ttl = before_days if before_days is not None else self.cfg.branching.ttl_days
        active_tags = set()
        expired: list[str] = []
        for key, st in self.state.branches.items():
            active_tags.update(p.tag for p in st.pins.values())
            if (time.time() - st.epoch) / 86400 > ttl:
                expired.append(key)

        orphans = orphan_pin_tags(self.catalog, self.cfg, active_tags)
        if not dry_run:
            for key in expired:
                st = self.state.branches[key]
                for table_id in list_tables(self.catalog):
                    try:
                        if st.data_branch in self.catalog.load_table(table_id).refs():
                            drop_ref(self.catalog, table_id, st.data_branch, "branch")
                    except Exception:  # noqa: BLE001
                        continue
                del self.state.branches[key]
            self.store.save(self.state)
            for table_id, tag in orphans:
                try:
                    drop_ref(self.catalog, table_id, tag, "tag")
                except Exception:  # noqa: BLE001
                    pass
        return envelope.envelope(
            "gc",
            ok=True,
            data={
                "expired_branches": expired,
                "orphan_tags": [f"{t}:{g}" for t, g in orphans],
                "dry_run": dry_run,
            },
        )
