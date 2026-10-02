"""Default worker boundary with an explicitly pinned, entirely offline fixture.

Only temporary test copies of the policy hashes are replaced. The installed
external provider is neither imported nor edited by this repository test.
"""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from alphaos.constants import CONTEXT_UNAVAILABLE_V1
from alphaos.research import last30days_provider as module
from alphaos.research.last30days_enricher import Last30DaysEnricher
from test_last30days_supervision import Journal
from conftest import make_settings


def default_fixture(
    tmp_path, monkeypatch, *, planner_delay=0.01, items=True, hang=True
):
    worker_dir = tmp_path / "adapter"
    worker_dir.mkdir()
    source = Path(module._WORKER).parent
    for name in [
        "last30days_worker.py",
        "last30days_source_policy.py",
        "last30days_partial.py",
        "last30days_wait.py",
    ]:
        if (source / name).exists():
            shutil.copyfile(source / name, worker_dir / name)
    root = tmp_path / "upstream"
    scripts = root / "scripts"
    lib = scripts / "lib"
    lib.mkdir(parents=True)
    (lib / "__init__.py").write_text("")
    (lib / "planner.py").write_text(f"""import time
from types import SimpleNamespace
def plan_query(*,available_sources,requested_sources):
 time.sleep({planner_delay!r})
 return SimpleNamespace(subqueries=[SimpleNamespace(sources=list(available_sources))],source_weights=dict.fromkeys(available_sources,1))
""")
    (lib / "pipeline.py").write_text(f"""import time
from concurrent.futures import ThreadPoolExecutor,as_completed
from types import SimpleNamespace
from lib import planner
def normalize_requested_sources(s): return s
def _ensure_jobs_in_plan(plan,available,*,explicit,topic): pass
def _retrieve_stream(*,source):
 if source=='reddit': time.sleep({5 if hang else 0!r})
 return ([{{'id':'one'}},{{'id':'two'}}] if source=='hackernews' and {items!r} else []),{{}}
def _normalize_score_dedupe(source,raw_items,*args,**kwargs):
 return [SimpleNamespace(item_id=r['id'],source=source,published_at='2026-01-01',date_confidence='high',engagement={{'score':3}},title='SECRET',body='PRIVATE',url='https://SECRET') for r in raw_items]
def _run_supplemental_searches(*,depth,mock): pass
def _retry_thin_sources(*,plan): pass
def _finalize_items_by_source(**kwargs): pass
def cluster_candidates(**kwargs): pass
def run(*,requested_sources,topic):
 available=list(requested_sources)
 plan=planner.plan_query(available_sources=available,requested_sources=requested_sources)
 _ensure_jobs_in_plan(plan,available,explicit=True,topic=topic)
 with ThreadPoolExecutor(max_workers=2) as pool:
  futures={{pool.submit(_retrieve_stream,source=s):s for s in plan.subqueries[0].sources}}
  for future in as_completed(futures):
   raw,_=future.result()
   _normalize_score_dedupe(futures[future],raw)
 return {{'clusters':[]}}
""")
    (lib / "rerank.py").write_text(
        "def rerank_candidates(**kwargs):pass\ndef score_fun(**kwargs):pass\n"
    )
    (lib / "github.py").write_text("def enrich_candidates_with_stars(**kwargs):pass\n")
    (scripts / "last30days.py").write_text(
        "import json,sys\nfrom lib import pipeline\nprint(json.dumps(pipeline.run(requested_sources=sys.argv[sys.argv.index('--search')+1].split(','),topic=sys.argv[1])))\n"
    )
    hashes = {
        n: hashlib.sha256((scripts / n).read_bytes()).hexdigest()
        for n in ["last30days.py", "lib/pipeline.py", "lib/planner.py"]
    }
    with (worker_dir / "last30days_source_policy.py").open("a") as f:
        f.write(
            "\n# Test-only compatible fixture, never installed upstream.\nSUPPORTED_FILES="
            + repr(hashes)
            + "\n"
        )
    monkeypatch.setattr(module, "_WORKER", str(worker_dir / "last30days_worker.py"))
    settings = replace(
        make_settings(
            LAST30DAYS_ENABLED="true",
            LAST30DAYS_PROVIDER="cli",
            LAST30DAYS_PYTHON=sys.executable,
            LAST30DAYS_REPO_PATH=str(root),
            LAST30DAYS_SOURCES="reddit,hackernews",
        ),
        last30days_timeout_seconds=0.8,
    )
    return module.CliLast30DaysProvider(settings), settings


@pytest.mark.parametrize("planner_delay", [0.01, 0.55])
def test_default_timeout_journals_item_evidence_without_changing_packet(
    tmp_path, monkeypatch, planner_delay
):
    provider, settings = default_fixture(
        tmp_path, monkeypatch, planner_delay=planner_delay
    )
    journal = Journal()
    ctx = Last30DaysEnricher(settings, journal, provider).enrich(
        SimpleNamespace(symbol="TEST", candidate_id="cand1", packet_id="packet1")
    )
    assert ctx.to_packet_dict() == {
        "last30days_context": CONTEXT_UNAVAILABLE_V1,
        "sentiment_context": CONTEXT_UNAVAILABLE_V1,
    }
    assert (
        ctx.enrichment_status == "error"
        and ctx.item_count == ctx.cluster_count == 0
        and not ctx.label_review_required
    )
    start, finish = journal.events
    assert (
        finish["attempt_id"] == start["attempt_id"]
        and finish["candidate_id"] == "cand1"
    )
    diag = finish["diagnostics"]
    assert diag["outcome"] == "timeout" and diag["deadline_seconds"] == 0.8
    partial = diag["partial_evidence"]
    assert partial["status"] == "incomplete_source_evidence"
    assert (
        partial["retained_unique_item_count"] == 2
        and len(partial["unfinished_calls"]) == 1
    )
    assert (
        partial["source_error_coverage"]
        == "not_observed_internal_errors_may_be_suppressed"
    )
    assert diag["source_policy"]["dispatch_counts"] == {"hackernews": 1, "reddit": 1}
    assert not partial["research_complete"] and not partial["model_context_eligible"]
    assert diag["adapter_contract"] == "structural_partial_owned_groups_v1"
    assert len(diag["adapter_file_sha256"]) == 5
    assert diag["cleanup_elapsed_seconds"] >= 0
    assert "SECRET" not in json.dumps(finish) and "PRIVATE" not in json.dumps(finish)


def test_timeout_with_no_returned_items_does_not_invent_partial_success(
    tmp_path, monkeypatch
):
    provider, _ = default_fixture(tmp_path, monkeypatch, items=False)
    with pytest.raises(TimeoutError) as exc:
        provider.get_research_for_symbol("TEST", "TEST stock")
    partial = exc.value.last30days_diagnostics["partial_evidence"]
    assert partial["status"] == "no_retained_source_items" and partial["items"] == []
    assert not partial["research_complete"] and len(partial["unfinished_calls"]) == 1


def test_fully_finished_result_remains_normal_result(tmp_path, monkeypatch):
    provider, _ = default_fixture(tmp_path, monkeypatch, hang=False)
    result = provider.get_research_for_symbol("TEST", "TEST stock")
    assert result.clusters == [] and result.item_count == 0
    assert result.raw_meta["diagnostics"]["outcome"] == "returned"
    assert result.raw_meta["diagnostics"]["source_policy"]["status"] == "completed"
    assert "partial_evidence" not in result.raw_meta["diagnostics"]


def test_configured_external_python_worker_retains_partial(tmp_path, monkeypatch):
    external = Path("~/.local/bin/python3.12").expanduser()
    if not external.is_file():
        pytest.skip("configured optional external interpreter is absent")
    provider, _ = default_fixture(tmp_path, monkeypatch)
    provider.s = replace(provider.s, last30days_python=str(external))
    with pytest.raises(TimeoutError) as exc:
        provider.get_research_for_symbol("TEST", "TEST stock")
    assert (
        exc.value.last30days_diagnostics["partial_evidence"][
            "retained_unique_item_count"
        ]
        == 2
    )
