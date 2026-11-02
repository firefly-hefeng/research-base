"""Critical interpretations require observed originals and independent review."""
import json
from pathlib import Path

import httpx
import pytest

from research_base.core import ResearchBase
from research_base.models import ResearchError
from research_base.usage import ResearchUsage


class OfflineSources:
    def list_sources(self):
        return []


@pytest.fixture
def library(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("No network"))) as client:
        with ResearchBase(tmp_path / "lib", source_registry=OfflineSources(), client=client) as base:
            cid = base.create_collection("Evaluation")["collection_id"]
            papers, materials = [], []
            for index in range(2):
                paper = base.add_paper({"source": "offline", "source_id": str(index),
                    "title": f"Evaluation metric definitions {index}"}, cid)
                path = tmp_path / f"original-{index}.txt"
                path.write_text("Fail is the positive class.\nThe denominator is negatives ranked above the positive.\n"
                                "Coverage is evaluated per independent query.", encoding="utf-8")
                material = base.import_material(paper["paper_id"], path=path, role="main_text",
                    version_id=paper["preferred_version_id"], confirmed=True)
                papers.append(paper["paper_id"])
                materials.append(material["material_id"])
            code_path = tmp_path / "metric.py"
            code_path.write_text("# Fail is the positive class.\nPOSITIVE_CLASS = 'Fail'\n", encoding="utf-8")
            code = base.import_material(papers[0], path=code_path, role="code", confirmed=True)
            yield base, cid, papers, materials, code["material_id"]


def begin(library, **kwargs):
    base, cid, papers, materials, code = library
    usage = ResearchUsage(base, **kwargs)
    checks = [{"check_id": "positive-class", "paper_id": papers[0], "kind": "metric",
               "question": "Which class is positive in the filtering audit?", "required_role": "main_text"}]
    sid = usage.begin("evaluation", cid, critical_checks=checks)["session_id"]
    return usage, sid, checks


def anchor(read, quote="Fail is the positive class."):
    return [{"read_id": read["read_id"], "quote": quote}]


def accept(usage, sid, check_id="positive-class"):
    packet = usage.detail_review_packet(sid, check_id)
    return usage.review_detail(sid, check_id, verdict="supported", reviewer="independent-local-reviewer",
        reason="The interpretation matches the quoted definition and the audited context.", expected_revision=packet["revision"])


def test_ordinary_sessions_get_read_ids_without_retaining_original_text(library):
    base, cid, papers, materials, code = library
    usage = ResearchUsage(base)
    sid = usage.begin("evaluation", cid)["session_id"]
    read = usage.read(sid, materials[0])
    checked = usage.check(sid)
    assert read["read_id"] == checked["reads"][0]["read_id"]
    assert checked["ready"] and checked["reading_ready"]
    assert checked["detail_status"] == "not_required" and not checked["detail_ready"]
    assert not checked["ready_for_checked_synthesis"]
    assert usage._get(sid).details.fragments == {}


def test_supported_submission_is_pending_not_model_self_certification(library):
    usage, sid, checks = begin(library)
    read = usage.read(sid, library[3][0])
    submitted = usage.submit_detail(sid, "positive-class", "Pass is the positive class.", anchor(read))
    assert submitted["status"] == "pending_review"
    checked = usage.check(sid)
    assert checked["reading_ready"] and not checked["ready"] and not checked["detail_ready"]
    assert checked["status"] == "needs_details"
    assert checked["detail_gaps"] == [{"check_id": "positive-class", "reason": "pending_review"}]
    packet = usage.detail_review_packet(sid, "positive-class")
    usage.review_detail(sid, "positive-class", verdict="contradicted", reviewer="independent-reader",
                        reason="The quote says Fail, while the submitted interpretation says Pass.", expected_revision=packet["revision"])
    assert usage.check(sid)["detail_gaps"][0]["reason"] == "contradicted"


def test_revision_invalidates_old_review_and_stale_async_verdict(library):
    usage, sid, _ = begin(library)
    read = usage.read(sid, library[3][0])
    usage.submit_detail(sid, "positive-class", "Fail is positive.", anchor(read))
    old_packet = usage.detail_review_packet(sid, "positive-class")
    accept(usage, sid)
    assert usage.check(sid)["ready_for_checked_synthesis"]
    revised = usage.submit_detail(sid, "positive-class", {"summary": "Fail is positive.", "positive_class": "Fail"}, anchor(read))
    assert revised["revision"] == 2 and revised["review"] is None
    assert not usage.check(sid)["ready"]
    with pytest.raises(ResearchError) as error:
        usage.review_detail(sid, "positive-class", verdict="supported", reviewer="independent-reader",
                            reason="Stale asynchronous response", expected_revision=old_packet["revision"])
    assert error.value.code == "DETAIL_REVISION_CHANGED"
    accept(usage, sid)
    assert usage.check(sid)["detail_ready"] and usage.check(sid)["ready"]


def test_quotes_must_have_been_returned_not_merely_exist_in_current_paper(library):
    usage, sid, _ = begin(library)
    read = usage.read(sid, library[3][0], max_chars=12)
    assert "denominator" in library[0].read_material(library[3][0])["pages"][0]["text"]
    with pytest.raises(ResearchError) as error:
        usage.submit_detail(sid, "positive-class", "The denominator is defined.",
                            anchor(read, "The denominator is negatives ranked above the positive."))
    assert error.value.code == "DETAIL_QUOTE_NOT_READ"
    with pytest.raises(ResearchError):
        usage.submit_detail(sid, "positive-class", "Fabricated evidence", [{"read_id": "invented", "quote": "Fail"}])


@pytest.mark.parametrize("source,expected", [("other-paper", "DETAIL_WRONG_PAPER"), ("code", "DETAIL_WRONG_ROLE")])
def test_quote_cannot_change_paper_or_pass_code_as_required_manuscript(library, source, expected):
    usage, sid, _ = begin(library)
    material = library[3][1] if source == "other-paper" else library[4]
    read = usage.read(sid, material)
    with pytest.raises(ResearchError) as error:
        usage.submit_detail(sid, "positive-class", "Fail is positive.", anchor(read))
    assert error.value.code == expected


def test_normalized_quote_packet_contains_exact_location_and_private_text_is_not_in_check(library):
    usage, sid, checks = begin(library)
    checks[0]["question"] = "An external mutation cannot change the host requirement"
    read = usage.read(sid, library[3][0])
    usage.submit_detail(sid, "positive-class", "Fail is positive.", anchor(read, "Fail   is\nthe positive class."))
    packet = usage.detail_review_packet(sid, "positive-class")
    evidence = packet["anchors"][0]
    assert packet["question"].startswith("Which class")
    assert evidence["quote"] == "Fail is the positive class."
    assert evidence["page"] == 1 and evidence["source_range"]["page_char_offset"] == 0
    assert len(evidence["sha256"]) == 64 and evidence["version_id"]
    checked = usage.check(sid)
    assert "Fail is the positive class." not in json.dumps(checked)
    assert "anchors" not in checked["critical_checks"][0] and "answer" not in checked["critical_checks"][0]
    checked["critical_checks"][0]["question"] = "Another mutation"
    assert usage.detail_review_packet(sid, "positive-class")["question"].startswith("Which class")


@pytest.mark.parametrize("field", ["sha256", "version_id", "paper_id", "role"])
def test_source_hash_version_identity_and_role_changes_invalidate_anchors_and_review(library, field):
    usage, sid, _ = begin(library)
    read = usage.read(sid, library[3][0])
    usage.submit_detail(sid, "positive-class", "Fail is positive.", anchor(read))
    accept(usage, sid)
    replacement = {"sha256": "0" * 64, "version_id": library[0].get_paper(library[2][1])["preferred_version_id"],
                   "paper_id": library[2][1], "role": "code"}[field]
    with library[0].store.connect() as db:
        db.execute(f"UPDATE materials SET {field}=? WHERE id=?", (replacement, library[3][0]))
    checked = usage.check(sid)
    assert not checked["ready"] and not checked["ready_for_checked_synthesis"]
    assert checked["critical_checks"][0]["status"] == "stale_anchor"
    assert checked["critical_checks"][0]["review"] is None
    with pytest.raises(ResearchError) as error:
        usage.detail_review_packet(sid, "positive-class")
    assert error.value.code == "DETAIL_ANCHOR_STALE"


def test_incomplete_continuation_still_blocks_after_independent_detail_acceptance(library):
    usage, sid, _ = begin(library)
    read = usage.read(sid, library[3][0], max_chars=40)
    assert read["truncated"]
    usage.submit_detail(sid, "positive-class", "Fail is positive.", anchor(read))
    accept(usage, sid)
    checked = usage.check(sid)
    assert checked["detail_ready"] and not checked["reading_ready"] and not checked["ready"]
    while read["continuation"]:
        read = usage.read(**read["continuation"])
    assert usage.check(sid)["ready_for_checked_synthesis"]


def test_physical_original_tampering_invalidates_review_even_if_catalog_hash_is_unchanged(library):
    usage, sid, _ = begin(library)
    read = usage.read(sid, library[3][0])
    usage.submit_detail(sid, "positive-class", "Fail is positive.", anchor(read))
    accept(usage, sid)
    material = library[0].list_materials(paper_id=library[2][0])[0]
    path = Path(material["local_path"])
    original = path.read_bytes()
    path.write_bytes(original.replace(b"Fail", b"Pass"))
    assert library[0].list_materials(paper_id=library[2][0])[0]["sha256"] == material["sha256"]
    checked = usage.check(sid)
    assert not checked["ready"] and checked["critical_checks"][0]["status"] == "stale_anchor"
    assert checked["critical_checks"][0]["review"] is None
    with pytest.raises(ResearchError) as error:
        usage.detail_review_packet(sid, "positive-class")
    assert error.value.code == "DETAIL_ANCHOR_STALE"


def test_unresolved_submission_cannot_be_supported_by_host_or_model(library):
    usage, sid, _ = begin(library)
    usage.read(sid, library[3][0])
    usage.submit_detail(sid, "positive-class", "The denominator still needs adjacent context.", [], status="unresolved")
    packet = usage.detail_review_packet(sid, "positive-class")
    with pytest.raises(ResearchError) as error:
        usage.review_detail(sid, "positive-class", verdict="supported", reviewer="independent-reader",
                            reason="Cannot promote an unresolved answer", expected_revision=packet["revision"])
    assert error.value.code == "DETAIL_UNRESOLVED"
    assert not usage.check(sid)["ready"]


def test_visual_usage_does_not_invent_text_quote_anchors(library):
    usage, sid, _ = begin(library)
    usage.observe_visual_read(sid, library[3][0], pages=[1], image_sha256=["a" * 64])
    receipt = usage.check(sid)["reads"][0]
    with pytest.raises(ResearchError) as error:
        usage.submit_detail(sid, "positive-class", "Fail is positive.", [{"read_id": receipt["read_id"], "quote": "Fail"}])
    assert error.value.code == "DETAIL_QUOTE_NOT_READ"
    assert usage.check(sid)["reading_ready"] and not usage.check(sid)["ready"]


def test_private_detail_fragments_are_bounded_and_end_destroys_session(library):
    usage, sid, _ = begin(library, max_detail_chars=35)
    read = usage.read(sid, library[3][0], max_chars=30)
    with pytest.raises(ResearchError) as error:
        usage.read(sid, library[3][0], max_chars=30)
    assert error.value.code == "RESEARCH_DETAIL_CONTEXT_LIMIT"
    assert len(usage.check(sid)["reads"]) == 1
    usage.end(sid)
    with pytest.raises(ResearchError):
        usage.detail_review_packet(sid, "positive-class")


@pytest.mark.parametrize("checks", [[{"check_id": "missing-fields"}], [{}] * 9,
    [{"check_id": "x", "paper_id": "p", "kind": "metric", "question": "q", "required_role": []}]])
def test_host_critical_requirements_are_bounded_and_validated(library, checks):
    usage = ResearchUsage(library[0])
    with pytest.raises(ResearchError) as error:
        usage.begin("evaluation", library[1], critical_checks=checks)
    assert error.value.code == "INVALID_ARGUMENT"
