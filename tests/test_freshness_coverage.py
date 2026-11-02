from datetime import datetime, timedelta, timezone

import pytest

from research_base.core import ResearchBase
from research_base.models import ResearchError
from research_base.storage import now


@pytest.fixture
def library(tmp_path):
    with ResearchBase(tmp_path / "library") as base:
        plan = {"query": "evaluation", "sources": ["arxiv"], "date_from": "2025-01-01",
                "date_to": now()[:10], "source_queries": {"arxiv": "ti:evaluation"}}
        cid = base.create_collection("Evaluation", config={"discovery": plan})["collection_id"]
        yield base, cid, plan


def test_newly_executed_historical_search_does_not_establish_current_freshness(library):
    base, cid, plan = library
    old_cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
    with base.store.connect() as db:
        db.execute("INSERT INTO search_runs VALUES(?,?,?,?)", (cid, base._plan_fingerprint(plan), now(), old_cutoff))
    fresh = base.prepare_context("evaluation", collection_id=cid)["freshness"]
    assert fresh["reason"] == "coverage_stale" and fresh["needs_refresh"]
    assert fresh["checked_age_days"] < 1 and fresh["coverage_age_days"] >= 89
    assert fresh["search_scope"]["source_queries"] == {"arxiv": "ti:evaluation"}


def test_recent_matching_completed_scope_has_both_time_and_coverage_checks(library):
    base, cid, plan = library
    with base.store.connect() as db:
        db.execute("INSERT INTO search_runs VALUES(?,?,?,?)", (cid, base._plan_fingerprint(plan), now(), now()[:10]))
    fresh = base.prepare_context("evaluation", collection_id=cid)["freshness"]
    assert not fresh["needs_refresh"] and fresh["coverage_age_days"] == 0
    assert fresh["search_scope"]["sources"] == ["arxiv"]


@pytest.mark.parametrize("value", [True, -1, 36501, "30"])
def test_invalid_freshness_policy_cannot_silently_pass(library, value):
    base, cid, plan = library
    with pytest.raises(ResearchError):
        base.prepare_context("evaluation", collection_id=cid, freshness_days=value)


def test_original_date_observations_remain_visible_without_raw_abstract_payload(library):
    base, cid, plan = library
    paper = base.add_paper({"source": "arxiv", "source_id": "2501.00001v2", "title": "Evaluation",
        "identifiers": {"arxiv": "2501.00001v2"}, "version_kind": "preprint",
        "extra": {"first_submitted_at": "2025-01-01T00:00:00Z", "version_updated_at": "2026-10-12T00:00:00Z"}}, cid)
    current = base.get_paper(paper["paper_id"])
    assert "extra" not in current
    dates = current["publication_history"][0]
    assert dates["source"] == "arxiv" and dates["dates"]["first_submitted_at"].startswith("2025")
    assert dates["dates"]["version_updated_at"].startswith("2026")
    base.configure_frontier(cid, [paper["paper_id"]])
    assert base.get_frontier(cid)["papers"][0]["publication_history"] == current["publication_history"]
