"""F14 git-sync: implicit branch-on-run, provenance recording, --json."""
from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from reble.cli import cli
from reble.gitinfo import branchable_name, git_info


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True)


def _repo(path, branch="main"):
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")


@pytest.fixture()
def project(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli, ["init", "proj"]).exit_code == 0
    proj = tmp_path / "proj"
    monkeypatch.chdir(proj)
    return proj, runner


def test_git_info_reads_branch_sha_dirty(project):
    proj, _ = project
    assert git_info(proj) is None                      # not a repo yet
    _repo(proj)
    gi = git_info(proj)
    assert gi.branch == "main" and len(gi.sha) == 40 and not gi.dirty
    assert branchable_name(gi) is None                 # main is not followable
    _git(proj, "switch", "-qc", "fix-x")
    (proj / "models/demo/example.sql").write_text("SELECT 2 AS id, 'x' AS message")
    gi = git_info(proj)
    assert branchable_name(gi) == "fix-x" and gi.dirty


def test_run_follows_git_feature_branch(project):
    proj, runner = project
    _repo(proj)
    assert runner.invoke(cli, ["run"]).exit_code == 0  # baseline on main
    _git(proj, "switch", "-qc", "fix-msg")
    (proj / "models/demo/example.sql").write_text(
        "SELECT 1 AS id, 'edited' AS message")
    out = runner.invoke(cli, ["run"])
    assert out.exit_code == 0, out.output
    assert "fix-msg" in out.output
    assert "data branch created from your git branch" in out.output
    st = runner.invoke(cli, ["status", "--json"])
    payload = json.loads(st.output)
    assert payload["branch"] == "fix-msg"
    assert payload["git_branch"] == "fix-msg"
    assert payload["last_run_sha"] is not None
    # second run REUSES the branch, no re-create
    out2 = runner.invoke(cli, ["run"])
    assert out2.exit_code == 0
    assert "created from your git branch" not in out2.output


def test_run_on_git_main_stays_on_main(project):
    proj, runner = project
    _repo(proj)
    out = runner.invoke(cli, ["run"])
    assert out.exit_code == 0
    st = json.loads(runner.invoke(cli, ["status", "--json"]).output)
    assert st["branch"] == "main"


def test_no_git_repo_keeps_manual_flow(project):
    proj, runner = project
    out = runner.invoke(cli, ["run"])                  # no repo: plain main run
    assert out.exit_code == 0
    st = json.loads(runner.invoke(cli, ["status", "--json"]).output)
    assert st["branch"] == "main" and st["git_branch"] is None


def test_git_sync_off_disables_follow(project):
    proj, runner = project
    (proj / "reble.yml").write_text(
        (proj / "reble.yml").read_text() + "\ngit_sync: false\n")
    _repo(proj)
    _git(proj, "switch", "-qc", "fix-y")
    (proj / "models/demo/example.sql").write_text("SELECT 3 AS id, 'y' AS message")
    out = runner.invoke(cli, ["run"])
    assert out.exit_code == 0
    st = json.loads(runner.invoke(cli, ["status", "--json"]).output)
    assert st["branch"] == "main"


def test_branch_create_defaults_name_from_git(project):
    proj, runner = project
    _repo(proj)
    assert runner.invoke(cli, ["run"]).exit_code == 0
    _git(proj, "switch", "-qc", "fix-z")
    (proj / "models/demo/example.sql").write_text("SELECT 4 AS id, 'z' AS message")
    out = runner.invoke(cli, ["branch", "create"])     # no name argument
    assert out.exit_code == 0, out.output
    assert "fix-z" in out.output
    # and errors helpfully with no git feature branch to take a name from
    runner.invoke(cli, ["promote"])
    _git(proj, "switch", "-q", "main")
    out2 = runner.invoke(cli, ["branch", "create"])
    assert out2.exit_code != 0
    assert "no branch name" in out2.output


def test_json_outputs_parse(project):
    proj, runner = project
    _repo(proj)
    run_j = json.loads(runner.invoke(cli, ["run", "--json"]).output)
    assert run_j["environment"] == "main" and run_j["published"]
    _git(proj, "switch", "-qc", "fix-j")
    (proj / "models/demo/example.sql").write_text("SELECT 5 AS id, 'j' AS message")
    run_j2 = json.loads(runner.invoke(cli, ["run", "--json"]).output)
    assert run_j2["environment"] == "fix-j"
    diff_j = json.loads(runner.invoke(cli, ["diff", "--json"]).output)
    assert diff_j["branch"] == "fix-j" and diff_j["tables"]
    ls = json.loads(runner.invoke(cli, ["branch", "list", "--json"]).output)
    assert ls["current"] == "fix-j"
    assert ls["branches"][0]["git_branch"] == "fix-j"


def test_state_migration_adds_git_columns(tmp_path):
    import sqlite3

    from reble.state import StateStore
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)                          # pre-F14 schema
    con.executescript("""
      CREATE TABLE branches (name TEXT PRIMARY KEY, scope TEXT NOT NULL,
        pins TEXT NOT NULL, base TEXT NOT NULL, created_at REAL NOT NULL,
        ttl_days INTEGER NOT NULL);
      INSERT INTO branches VALUES ('old', '["a.b"]', '{}', '{}', 1.0, 14);
    """)
    con.commit(); con.close()
    st = StateStore(db)
    m = st.get("old")
    assert m.git_branch is None and m.last_run_at is None
    st.record_run("old", 2.0, "a" * 40, dirty=True)
    m = st.get("old")
    assert m.last_run_at == 2.0 and m.git_dirty and m.last_run_sha == "a" * 40
