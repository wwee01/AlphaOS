"""Offline source-contract regressions; no installed provider or network needed."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from types import SimpleNamespace

import pytest

from alphaos.research import last30days_provider as provider_module
from alphaos.research.last30days_provider import CliLast30DaysProvider
from alphaos.research import last30days_source_policy as policy_module
from alphaos.research.last30days_source_policy import SourcePolicy, SourcePolicyError
from conftest import make_settings

REQUESTED = ["reddit", "hackernews", "polymarket", "github", "x", "youtube"]


def upstream_fixture():
    calls = []
    planner = SimpleNamespace()
    pipeline = SimpleNamespace(normalize_requested_sources=lambda sources: sources)

    def plan_query(*, available_sources, requested_sources, context="", **kwargs):
        calls.append(("planner", list(available_sources), context))
        sources = [s for s in available_sources if s in requested_sources]
        return SimpleNamespace(subqueries=[SimpleNamespace(sources=sources)],
                               source_weights=dict.fromkeys(sources, 1.0))

    def jobs(plan, available, *, explicit, topic):
        if "jobs" in available:
            for subquery in plan.subqueries:
                subquery.sources.append("jobs")
            plan.source_weights["jobs"] = 1.0

    def retrieve(*, source, **kwargs):
        calls.append(("retrieve", source))
        return [], {}

    def supplemental(*, depth, mock, **kwargs):
        if depth != "quick" and not mock:
            calls.append(("supplemental", "x"))

    def retry(*, plan, **kwargs):
        for source in plan.subqueries[0].sources:
            pipeline._retrieve_stream(source=source)

    def run(*, requested_sources, topic, depth="quick", external_plan=None, unavailable=()):
        available = [s for s in requested_sources if s not in unavailable]
        if "jobs" not in available:
            available.append("jobs")  # upstream bug after explicit filtering
        plan = external_plan or planner.plan_query(available_sources=available,
                                                  requested_sources=requested_sources,
                                                  context="preserved context")
        pipeline._ensure_jobs_in_plan(plan, available, explicit=False, topic=topic)
        for source in plan.subqueries[0].sources:
            if source in available:
                pipeline._retrieve_stream(source=source)
        pipeline._run_supplemental_searches(depth=depth, mock=False)
        if depth != "quick":
            pipeline._retry_thin_sources(plan=plan)
        calls.append(("rerank", "original model"))
        calls.append(("score_fun", "original model"))
        return plan

    planner.plan_query = plan_query
    pipeline.run = run
    pipeline._ensure_jobs_in_plan = jobs
    pipeline._retrieve_stream = retrieve
    pipeline._run_supplemental_searches = supplemental
    pipeline._retry_thin_sources = retry
    return pipeline, planner, calls


def test_reproduces_extra_jobs_then_removes_only_unrequested_source():
    pipeline, planner, calls = upstream_fixture()
    pipeline.run(requested_sources=REQUESTED, topic="AMD stock")
    assert ("retrieve", "jobs") in calls
    calls.clear()
    state = SourcePolicy(REQUESTED)
    state.install(pipeline, planner)
    plan = pipeline.run(requested_sources=REQUESTED, topic="AMD stock")
    assert sorted(s for kind, *rest in calls if kind == "retrieve" for s in rest) == sorted(REQUESTED)
    assert calls[0] == ("planner", REQUESTED, "preserved context")
    assert calls[-2:] == [("rerank", "original model"), ("score_fun", "original model")]
    assert plan.source_weights == dict.fromkeys(REQUESTED, 1.0)
    summary = state.summary("completed")
    assert summary["suppressed"] == ["jobs"] and summary["completed_runs"] == 1
    assert summary["dispatch_counts"] == dict.fromkeys(REQUESTED, 1)


def test_explicit_jobs_remains_opted_in():
    pipeline, planner, calls = upstream_fixture()
    state = SourcePolicy(["reddit", "jobs"])
    state.install(pipeline, planner)
    pipeline.run(requested_sources=["reddit", "jobs"], topic="AMD stock")
    assert ("retrieve", "jobs") in calls
    assert state.summary("completed")["suppressed"] == []


def test_does_not_invent_unavailable_configured_sources():
    pipeline, planner, calls = upstream_fixture()
    state = SourcePolicy(REQUESTED)
    state.install(pipeline, planner)
    pipeline.run(requested_sources=REQUESTED, topic="AMD stock", unavailable=["youtube"])
    assert ("retrieve", "youtube") not in calls
    assert "youtube" not in state.summary("completed")["eligible"]
    assert state.summary("completed")["requested"] == sorted(REQUESTED)


@pytest.mark.parametrize("sources", [["reddit"], ["reddit", "x"]])
def test_deep_supplemental_and_thin_retry_honor_sources(sources):
    pipeline, planner, calls = upstream_fixture()
    state = SourcePolicy(sources)
    state.install(pipeline, planner)
    pipeline.run(requested_sources=sources, topic="AMD stock", depth="deep")
    assert (("supplemental", "x") in calls) == ("x" in sources)
    assert state.summary("completed")["dispatch_counts"] == dict.fromkeys(sources, 2)
    assert ("retrieve", "jobs") not in calls


def test_external_plan_unrequested_sources_and_weights_removed():
    pipeline, planner, calls = upstream_fixture()
    state = SourcePolicy(["reddit"])
    state.install(pipeline, planner)
    plan = SimpleNamespace(subqueries=[SimpleNamespace(sources=["reddit", "jobs", "x"])],
                           source_weights={"reddit": .4, "jobs": .2, "x": .6})
    pipeline.run(requested_sources=["reddit"], topic="AMD stock", external_plan=plan)
    assert plan.source_weights == {"reddit": .4}
    assert plan.subqueries[0].sources == ["reddit"]
    assert ("retrieve", "jobs") not in calls


def test_changed_request_fails_before_planner_or_retrieval():
    pipeline, planner, calls = upstream_fixture()
    state = SourcePolicy(["reddit"])
    state.install(pipeline, planner)
    with pytest.raises(SourcePolicyError):
        pipeline.run(requested_sources=["reddit", "jobs"], topic="AMD stock")
    assert calls == [] and state.violations == 1


def test_guard_violation_caught_by_upstream_still_invalidates_result():
    pipeline, planner, calls = upstream_fixture()
    def catches(*, requested_sources, topic):
        try:
            pipeline._retrieve_stream(source="jobs")
        except Exception:
            return {"clusters": []}
    pipeline.run = catches
    state = SourcePolicy(["reddit"])
    state.install(pipeline, planner)
    with pytest.raises(SourcePolicyError):
        pipeline.run(requested_sources=["reddit"], topic="AMD stock")
    assert calls == [] and state.completed_runs == 0 and state.violations > 0


def test_missing_hook_fails_before_mutation():
    pipeline, planner, _ = upstream_fixture()
    original = pipeline.run
    del pipeline._retry_thin_sources
    with pytest.raises(SourcePolicyError, match="hooks unsupported"):
        SourcePolicy(["reddit"]).install(pipeline, planner)
    assert pipeline.run is original


@pytest.mark.parametrize("which", ["last30days.py", "lib/pipeline.py", "lib/planner.py"])
def test_changed_routing_file_fails_verification(tmp_path, monkeypatch, which):
    files = {name: "# valid routing\n" for name in policy_module.SUPPORTED_FILES}
    for name, body in files.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(body)
    monkeypatch.setattr(policy_module, "SUPPORTED_FILES", {
        name: hashlib.sha256(body.encode()).hexdigest() for name, body in files.items()})
    policy_module.verify_supported(tmp_path / "last30days.py")
    (tmp_path / which).write_text("# upgraded unreviewed routing\n")
    with pytest.raises(SourcePolicyError, match="contract unsupported"):
        policy_module.verify_supported(tmp_path / "last30days.py")


def test_default_worker_refuses_unsupported_provider_before_import_or_entry(tmp_path):
    marker = tmp_path / "should-not-run"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "last30days.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    settings = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli",
                             LAST30DAYS_PYTHON=sys.executable, LAST30DAYS_REPO_PATH=str(tmp_path))
    with pytest.raises(RuntimeError) as caught:
        CliLast30DaysProvider(settings).get_research_for_symbol("AMD", "AMD stock")
    assert not marker.exists()
    receipt = caught.value.last30days_diagnostics["source_policy"]
    assert receipt["status"] == "unsupported"


@pytest.mark.parametrize("status", [None, "installed", "active", "incomplete"])
def test_default_provider_requires_final_success_receipt(monkeypatch, status):
    settings = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli",
                             LAST30DAYS_REPO_PATH="/tmp")
    def fake_process(cmd, **kwargs):
        if status:
            row = {"event": "source_policy", "version": policy_module.POLICY_VERSION,
                   "status": status, "requested": settings.last30days_sources.split(","),
                   "completed_runs": 0, "violations": 0}
            os.write(kwargs["pass_fds"][0], (json.dumps(row) + "\n").encode())
        return SimpleNamespace(returncode=0, communicate=lambda timeout: ("{}", ""))
    monkeypatch.setattr(provider_module.subprocess, "Popen", fake_process)
    with pytest.raises(RuntimeError, match="source policy not verified"):
        CliLast30DaysProvider(settings).get_research_for_symbol("AMD", "AMD stock")


def test_custom_command_remains_unverified_and_inherited_policy_removed(monkeypatch):
    settings = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli", LAST30DAYS_CMD="custom-cli")
    monkeypatch.setenv(policy_module.POLICY_ENV, policy_module.POLICY_VERSION)
    def fake_process(cmd, **kwargs):
        assert cmd == ["custom-cli"]
        assert policy_module.POLICY_ENV not in kwargs["env"]
        return SimpleNamespace(returncode=0, communicate=lambda timeout: ("{}", ""))
    monkeypatch.setattr(provider_module.subprocess, "Popen", fake_process)
    result = CliLast30DaysProvider(settings).get_research_for_symbol("AMD", "AMD stock")
    assert result.raw_meta["diagnostics"]["source_policy"]["status"] == "unverified"


def test_source_aliases_normalize_and_empty_or_unknown_refuses():
    assert policy_module.requested_from_argv(["AMD stock", "--search", "hn,xquik,reddit"]) == ["hackernews", "reddit", "x"]
    for args in ([], ["--search"], ["--search", ""], ["--search", "unknown"], ["--search", "reddit", "--search", "x"]):
        with pytest.raises(SourcePolicyError):
            policy_module.requested_from_argv(args)


def test_loaded_module_from_different_root_refuses(tmp_path):
    pipeline = SimpleNamespace(__file__=str(tmp_path / "foreign" / "pipeline.py"))
    planner = SimpleNamespace(__file__=str(tmp_path / "lib" / "planner.py"))
    with pytest.raises(SourcePolicyError, match="import unsupported"):
        policy_module.verify_imports(tmp_path / "last30days.py", pipeline, planner)


def test_local_policy_import_cannot_be_shadowed_by_provider(tmp_path):
    marker = tmp_path / "wrong-policy-import"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "last30days_source_policy.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    (scripts / "last30days.py").write_text("print('{}')\n")
    settings = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli",
                             LAST30DAYS_PYTHON=sys.executable, LAST30DAYS_REPO_PATH=str(tmp_path))
    with pytest.raises(RuntimeError):
        CliLast30DaysProvider(settings).get_research_for_symbol("AMD", "AMD stock")
    assert not marker.exists()


@pytest.mark.parametrize("source", ["reddit", "jobs"])
def test_default_result_sources_must_match_verified_request(monkeypatch, source):
    settings = make_settings(LAST30DAYS_ENABLED="true", LAST30DAYS_PROVIDER="cli",
                             LAST30DAYS_REPO_PATH="/tmp", LAST30DAYS_SOURCES="reddit")
    def fake_process(cmd, **kwargs):
        row = {"event": "source_policy", "version": policy_module.POLICY_VERSION,
               "status": "completed", "requested": ["reddit"], "dispatch_counts": {"reddit": 1},
               "completed_runs": 1, "violations": 0}
        os.write(kwargs["pass_fds"][0], (json.dumps(row) + "\n").encode())
        result = {"clusters": [{"title": "theme", "sources": [source], "score": 2, "candidate_ids": ["a"]}]}
        return SimpleNamespace(returncode=0, communicate=lambda timeout: (json.dumps(result), ""))
    monkeypatch.setattr(provider_module.subprocess, "Popen", fake_process)
    if source == "jobs":
        with pytest.raises(RuntimeError, match="outside explicit source request"):
            CliLast30DaysProvider(settings).get_research_for_symbol("AMD", "AMD stock")
    else:
        result = CliLast30DaysProvider(settings).get_research_for_symbol("AMD", "AMD stock")
        assert result.sources_used == ["reddit"] and result.item_count == 1
