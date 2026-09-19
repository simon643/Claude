"""End-to-end through the command line, against the bundled demo org."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apflow.cli import main
from tests.conftest import DEMO_DIR


@pytest.fixture
def run(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Invoke the CLI against the demo snapshot and a throwaway database."""
    database = tmp_path / "apflow.sqlite3"

    def invoke(*args: str) -> tuple[int, str, str]:
        code = main([args[0], "--snapshot", str(DEMO_DIR), "--db", str(database), *args[1:]])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke


def _json(output: str) -> dict:
    return json.loads(output)


def test_the_shipped_matrix_validates(run) -> None:
    code, output, _ = run("matrix")
    assert code == 0
    assert "No problems found" in output


def test_pull_routes_every_document(run) -> None:
    code, output, _ = run("pull", "--requested-by", "priya", "--json")
    assert code == 0
    results = _json(output)["results"]
    assert len(results) == 12
    assert not [r for r in results if r["outcome"] == "unroutable"]
    assert sum(1 for r in results if r["outcome"] == "auto_approved") == 2


def test_pull_is_idempotent(run) -> None:
    run("pull", "--requested-by", "priya")
    code, output, _ = run("pull", "--requested-by", "priya", "--json")
    outcomes = {r["outcome"] for r in _json(output)["results"]}
    assert outcomes <= {"unchanged", "already_approved"}
    assert code == 0


def test_dry_run_creates_nothing(run) -> None:
    run("pull", "--dry-run")
    _, output, _ = run("queue", "--json")
    assert _json(output)["count"] == 0


def test_the_whole_cycle(run) -> None:
    run("pull", "--requested-by", "priya")

    code, output, _ = run("inbox", "--as", "dana", "--json")
    assert code == 0
    references = {item["reference"] for item in _json(output)["items"]}
    assert "AR-0004" in references

    code, _, _ = run("approve", "AR-0004", "--as", "dana", "--comment", "hosting, as agreed")
    assert code == 0

    _, output, _ = run("show", "AR-0004", "--json")
    view = _json(output)
    assert view["state"] == "approved"
    assert view["steps"][0]["decisions"][0]["comment"] == "hosting, as agreed"

    code, output, _ = run("push", "--dry-run", "--json")
    assert code == 0
    assert any(o["reference"] == "AR-0004" for o in _json(output)["outcomes"])


def test_an_over_limit_approval_is_refused_with_a_way_forward(run) -> None:
    run("pull", "--requested-by", "priya")
    code, _, error = run("approve", "AR-0003", "--as", "dana")
    assert code == 1
    assert "limit is" in error
    assert "Who can" in error


def test_a_rejection_without_a_reason_is_refused_by_the_parser(run) -> None:
    with pytest.raises(SystemExit):
        main(["reject", "AR-0001", "--as", "dana"])


def test_reject_closes_the_request(run) -> None:
    run("pull", "--requested-by", "priya")
    code, _, _ = run("reject", "AR-0005", "--as", "dana", "--comment", "book economy")
    assert code == 0
    code, output, _ = run("show", "AR-0005", "--json")
    assert _json(output)["state"] == "rejected"


def test_status_reports_the_queue(run) -> None:
    run("pull", "--requested-by", "priya")
    code, output, _ = run("status", "--json")
    assert code == 0
    summary = _json(output)
    assert summary["pending"]["count"] == 10
    assert summary["ready_to_post"]["count"] == 2


def test_budget_shows_commitments_against_headroom(run) -> None:
    run("pull", "--requested-by", "priya")
    code, output, _ = run("budget", "--period", "2026-09", "--json")
    assert code == 0
    travel = next(line for line in _json(output)["lines"] if line["account"] == "429")
    assert travel["remaining"].startswith("-")


def test_pushing_read_only_data_is_refused(run) -> None:
    run("pull", "--requested-by", "priya")
    run("approve", "AR-0004", "--as", "dana", "--comment", "ok")
    code, _, error = run("push")
    assert code == 1
    assert "read-only" in error


def test_users_lists_the_directory_with_delegations(run) -> None:
    code, output, _ = run("users")
    assert code == 0
    assert "away until 2026-09-26 -> jo" in output


def test_audit_shows_the_trail(run) -> None:
    run("pull", "--requested-by", "priya")
    run("approve", "AR-0004", "--as", "dana", "--comment", "ok")
    _, output, _ = run("audit", "--json")
    kinds = {event["kind"] for event in _json(output)["events"]}
    assert {"submitted", "decision_approve", "approved"} <= kinds


def test_reports_run(run) -> None:
    run("pull", "--requested-by", "priya")
    run("approve", "AR-0004", "--as", "dana", "--comment", "ok")
    assert run("report", "turnaround", "--json")[0] == 0
    assert run("report", "spend", "--json")[0] == 0


def test_an_unknown_reference_fails_cleanly(run) -> None:
    code, _, error = run("show", "AR-9999")
    assert code == 1
    assert "no approval request matches" in error


def test_matrix_init_writes_a_starter(tmp_path, capsys) -> None:
    target = tmp_path / "matrix.json"
    assert main(["matrix", "--init", "--matrix", str(target)]) == 0
    assert target.exists()
    # A second init must not silently overwrite a live matrix.
    assert main(["matrix", "--init", "--matrix", str(target)]) == 2
