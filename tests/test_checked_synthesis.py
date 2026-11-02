"""Independent detail approval never substitutes for reviewing the final draft."""
import hashlib
import json
from pathlib import Path
import re

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from research_base.agent import ResearchAgent
from research_base.core import ResearchBase
from research_base.models import ResearchError
from research_base.tools import ToolRegistry


QUOTE = "Fail is the positive class."
CORRECT = "Audit recall measures how many failing items are detected; Fail is positive."
WRONG = "Audit recall measures how many passing items are detected; Pass is positive."


class OfflineSources:
    def list_sources(self):
        return []


@pytest.fixture
def library(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("No source network"))) as client:
        with ResearchBase(tmp_path / "lib", source_registry=OfflineSources(), client=client) as base:
            cid = base.create_collection("Metric audit")["collection_id"]
            paper = base.add_paper({"source": "offline", "source_id": "metric-audit",
                                   "title": "Filtering audit metric definition"}, cid)
            path = tmp_path / "original.txt"
            path.write_text(QUOTE + " Recall counts detected failing items among all failing items.", encoding="utf-8")
            material = base.import_material(paper["paper_id"], path=path, confirmed=True,
                                           version_id=paper["preferred_version_id"])
            checks = [{"check_id": "positive-class", "paper_id": paper["paper_id"], "kind": "metric",
                       "question": "Which class is positive, and what does recall measure?", "required_role": "main_text"}]
            yield base, cid, paper, material, checks


def verdict(value="supported", reason="The final draft preserves the independently checked class definition."):
    return {"verdict": value, "reviewer": "independent-host-reviewer", "reason": reason}


def approved_session(library):
    base, cid, paper, material, checks = library
    registry = ToolRegistry(base, collection_id=cid)
    sid = registry.research.begin("Design the audit", cid, critical_checks=checks)["session_id"]
    read = registry.research.read(sid, material["material_id"])
    anchors = [{"read_id": read["read_id"], "quote": QUOTE}]
    registry.research.submit_detail(sid, "positive-class", {"summary": "Fail is positive.", "positive_class": "Fail"}, anchors)
    packet = registry.research.detail_review_packet(sid, "positive-class")
    registry.review_research_detail(sid, "positive-class", expected_revision=packet["revision"], **verdict())
    assert registry.research.check(sid)["ready_for_checked_synthesis"]
    return registry, sid, anchors


def test_final_review_is_trusted_only_and_cannot_be_self_certified(library):
    registry, sid, _ = approved_session(library)
    names = {spec["function"]["name"] for spec in registry.specs()}
    assert "review_research_detail" not in names and "require_checked_synthesis" not in names
    assert registry.call("require_checked_synthesis", {"session_id": sid, "answer": CORRECT})["error"]["code"] == "TOOL_NOT_ALLOWED"
    with pytest.raises(ResearchError) as error:
        registry.require_checked_synthesis(sid, CORRECT, None)
    assert error.value.code == "RESEARCH_SYNTHESIS_REVIEW_REQUIRED"


def test_supported_final_binds_exact_draft_and_current_detail_revision(library):
    registry, sid, _ = approved_session(library)
    observed = []

    def independent_review(packet):
        observed.append(packet)
        assert packet["answer"] == CORRECT
        assert packet["verified_details"][0]["answer"]["positive_class"] == "Fail"
        assert QUOTE in packet["verified_details"][0]["anchors"][0]["packet_context"]["text"]
        assert "Recall counts detected failing items" in packet["observed_context"]["fragments"][0]["text"]
        return verdict()

    result = registry.require_checked_synthesis(sid, CORRECT, independent_review)
    assert result["synthesis_review"]["draft_sha256"] == hashlib.sha256(CORRECT.encode()).hexdigest()
    assert result["synthesis_review"]["detail_revisions"] == {"positive-class": 1}
    assert observed[0]["session_id"] == sid
    assert result["research_usage"]["ready_for_checked_synthesis"]
    # Approval of an earlier draft cannot authorize a later contradictory one.
    with pytest.raises(ResearchError):
        registry.require_checked_synthesis(sid, WRONG, lambda packet: verdict("contradicted"))


def test_reviewer_context_is_bounded_and_does_not_turn_unreturned_text_into_evidence(library):
    base, cid, paper, material, checks = library
    registry = ToolRegistry(base, collection_id=cid)
    sid = registry.research.begin("Inspect definition",cid,critical_checks=checks)["session_id"]
    registry.research.read(sid,material["material_id"],max_chars=10)
    context = registry.research.review_context(sid,max_chars=5)
    assert context["returned_chars"] == 5 and context["truncated"]
    assert context["fragments"][0]["text"] == QUOTE[:5]
    assert "Recall counts" not in json.dumps(context)
    assert "text" not in registry.research.check(sid)["reads"][0]
    assert registry.call("review_context",{"session_id":sid})["error"]["code"] == "TOOL_NOT_ALLOWED"


def test_source_passage_ids_resolve_actual_text_without_retyping_and_cannot_cross_sessions(library):
    base,cid,paper,material,checks=library
    tools=ToolRegistry(base,collection_id=cid)
    sid=tools.research.begin("Inspect metric",cid,critical_checks=checks)["session_id"]
    read=tools.call("read_research_material",{"session_id":sid,"material_id":material["material_id"]})["data"]
    anchor={"passage_id":read["passages"][0]["passage_id"]}
    result=tools.call("submit_research_detail",{"session_id":sid,"check_id":"positive-class","answer":"Fail is positive.","anchors":[anchor]})
    assert result["data"]["status"] == "pending_review"
    packet=tools.research.detail_review_packet(sid,"positive-class")
    assert QUOTE in packet["anchors"][0]["quote"]
    other=tools.research.begin("Separate task",cid,critical_checks=checks)["session_id"]
    rejected=tools.call("submit_research_detail",{"session_id":other,"check_id":"positive-class","answer":"Fail is positive.","anchors":[anchor]})
    assert rejected["error"]["code"] == "DETAIL_QUOTE_NOT_READ"


@pytest.mark.parametrize("review", [lambda packet: verdict("contradicted"),
    lambda packet: verdict("needs_context"), lambda packet: {**verdict(), "approved": True},
    lambda packet: {"verdict": "supported", "reviewer": "", "reason": "Not independent"},
    lambda packet: (_ for _ in ()).throw(RuntimeError("Private reviewer diagnostics"))])
def test_rejected_missing_or_broken_final_review_fails_closed(library, review):
    registry, sid, _ = approved_session(library)
    with pytest.raises(ResearchError) as error:
        registry.require_checked_synthesis(sid, CORRECT, review)
    assert error.value.code == "RESEARCH_SYNTHESIS_REVIEW_REQUIRED"
    assert "Private reviewer diagnostics" not in str(error.value)


@pytest.mark.parametrize("change", ["source-bytes", "submission", "reviewed-submission"])
def test_approval_cannot_survive_source_or_submission_changes_during_callback(library, change):
    registry, sid, anchors = approved_session(library)

    def changing_reviewer(packet):
        if change == "source-bytes":
            path = Path(library[3]["local_path"])
            path.write_bytes(path.read_bytes().replace(b"Fail", b"Pass"))
        else:
            registry.research.submit_detail(sid, "positive-class", "A revised interpretation.", anchors)
            if change == "reviewed-submission":
                current = registry.research.detail_review_packet(sid, "positive-class")
                registry.review_research_detail(sid, "positive-class", expected_revision=current["revision"], **verdict())
        return verdict()

    with pytest.raises(ResearchError) as error:
        registry.require_checked_synthesis(sid, CORRECT, changing_reviewer)
    assert error.value.code in {"RESEARCH_USAGE_REQUIRED", "RESEARCH_SYNTHESIS_REVIEW_REQUIRED"}
    if change == "reviewed-submission":
        # Even a freshly accepted revision differs from the review packet.
        assert error.value.code == "RESEARCH_SYNTHESIS_REVIEW_REQUIRED"
    else:
        assert not registry.research.check(sid)["ready_for_checked_synthesis"]


def reply(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"id": f"call_{i}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments)}} for i, (name, arguments) in enumerate(calls)]
    return httpx.Response(200, json={"choices": [{"message": message}]})


def run_model(library, final, synthesis_reviewer):
    base, cid, paper, material, checks = library
    observed_details = []
    step = 0

    def handler(request):
        nonlocal step
        payload = json.loads(request.content)
        sid = re.search(r'"session_id": "(research_\w+)"', payload["messages"][1]["content"])[1]
        step += 1
        if step == 1:
            return reply(calls=[("read_research_material", {"session_id": sid,
                "material_id": material["material_id"], "pages": [1]})])
        if step == 2:
            data = json.loads(payload["messages"][-1]["content"])["data"]
            assert QUOTE in data["pages"][0]["text"]
            return reply(calls=[("submit_research_detail", {"session_id": sid, "check_id": "positive-class",
                "answer": {"summary": "Fail is positive.", "positive_class": "Fail"},
                "anchors": [{"read_id": data["read_id"], "quote": QUOTE}]})])
        return reply(final)

    def detail_reviewer(packet):
        observed_details.append(packet)
        assert packet["answer"]["positive_class"] == "Fail"
        assert packet["anchors"][0]["quote"] == QUOTE
        return verdict()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = ResearchAgent(base, api_key="test", client=client).run("Design a filtering audit", collection_id=cid,
            critical_checks=checks, detail_reviewer=detail_reviewer, synthesis_reviewer=synthesis_reviewer)
    assert len(observed_details) == 1
    return result


def test_actual_model_correct_detail_then_contradictory_final_is_not_published(library):
    final_packets = []

    def independent_final(packet):
        final_packets.append(packet)
        assert packet["verified_details"][0]["answer"]["positive_class"] == "Fail"
        assert WRONG in packet["answer"]
        return verdict("contradicted", "The actual final draft switches the positive class from Fail to Pass.")

    result = run_model(library, WRONG, independent_final)
    assert final_packets
    assert result["research_usage"]["reading_ready"] and result["research_usage"]["detail_ready"]
    assert result["status"] == "needs_verification"
    assert WRONG not in result["answer"]
    assert result["synthesis_review"]["status"] == "required"


def test_actual_model_final_without_independent_callback_is_not_published(library):
    result = run_model(library, CORRECT, None)
    assert result["status"] == "needs_verification"
    assert result["research_usage"]["ready_for_checked_synthesis"]
    assert CORRECT not in result["answer"]
    assert result["synthesis_review"]["error_code"] == "RESEARCH_SYNTHESIS_REVIEW_REQUIRED"


def test_actual_model_supported_final_reports_hash_of_the_published_draft(library):
    final_packets = []

    def independent_final(packet):
        final_packets.append(packet)
        assert CORRECT in packet["answer"]
        assert WRONG not in packet["answer"]
        return verdict()

    result = run_model(library, CORRECT, independent_final)
    assert result["status"] == "completed_with_limits"  # offline fixture made no current-frontier claim
    assert final_packets[-1]["answer"] == result["answer"]
    assert result["synthesis_review"]["draft_sha256"] == hashlib.sha256(result["answer"].encode()).hexdigest()
    assert result["synthesis_review"]["detail_revisions"] == {"positive-class": 1}


def test_review_context_contains_only_the_actual_returned_fragment(library):
    base, cid, paper, material, checks = library
    registry = ToolRegistry(base, collection_id=cid)
    sid = registry.research.begin("Metric", cid, critical_checks=checks)["session_id"]
    returned = registry.research.read(sid, material["material_id"], max_chars=len(QUOTE))
    assert returned["continuation"]
    registry.research.submit_detail(sid, "positive-class", "Fail is positive.", [{"read_id": returned["read_id"], "quote": QUOTE}])
    packet = registry.research.detail_review_packet(sid, "positive-class")
    context = packet["anchors"][0]["packet_context"]
    assert context["text"] == QUOTE
    assert "Recall counts" not in json.dumps(packet)
    assert context["normalized_start"] == 0 and context["normalized_end"] == len(QUOTE)
    assert context["quote_complete"] and context["returned_fragment_chars"] == len(QUOTE)
    # Mutating a trusted packet cannot change the stored receipt or later packets.
    context["text"] = "Injected context"
    assert registry.research.detail_review_packet(sid, "positive-class")["anchors"][0]["packet_context"]["text"] == QUOTE


def make_pdf(path, texts):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    font_ref = writer._add_object(font)
    for text in texts:
        page = writer.add_blank_page(width=595, height=842)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(("BT /F1 12 Tf 50 800 Td (" + escaped + ") Tj ET").encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)


def test_neighbor_context_read_preserves_continuation_and_anchor_read_id(library, tmp_path):
    base, cid, paper, material, checks = library
    source = tmp_path / "context.pdf"
    make_pdf(source, ["Scope applies to filtering audits.", QUOTE + " " + "metric context " * 150,
                      "Recall uses all independently labelled failing items."])
    pdf = base.import_material(paper["paper_id"], path=source, confirmed=True, version_id=paper["preferred_version_id"])
    registry = ToolRegistry(base, collection_id=cid)
    sid = registry.research.begin("Metric", cid, critical_checks=checks)["session_id"]
    first = registry.call("read_research_context", {"session_id": sid, "material_id": pdf["material_id"],
        "page": 2, "adjacent_pages": 1, "max_chars": 90})["data"]
    assert first["context_selection"]["selected_pages"] == [1, 2, 3]
    assert first["continuation"] and first["read_id"]
    registry.research.submit_detail(sid, "positive-class", "Fail is positive.", [{"read_id": first["read_id"], "quote": QUOTE}])
    packet = registry.research.detail_review_packet(sid, "positive-class")
    evidence = packet["anchors"][0]
    assert evidence["read_id"] == first["read_id"] and evidence["page"] == 2
    assert evidence["packet_context"]["neighboring_pages"]["previous_page"] == 1
    assert evidence["packet_context"]["neighboring_pages"]["next_page"] == 3
    assert "independently labelled" not in evidence["packet_context"]["text"]
    registry.review_research_detail(sid, "positive-class", expected_revision=packet["revision"], **verdict())
    assert not registry.research.check(sid)["reading_ready"]
    latest = first
    while latest["continuation"]:
        latest = registry.call("read_research_material", latest["continuation"])["data"]
    assert registry.research.check(sid)["ready_for_checked_synthesis"]
    # The first anchor's context stays receipt-bound after later text has been delivered.
    assert registry.research.detail_review_packet(sid, "positive-class")["anchors"][0]["packet_context"] == evidence["packet_context"]


def test_review_context_is_bounded_and_uses_normalized_fragment_offsets(library, tmp_path):
    base, cid, paper, material, checks = library
    source = tmp_path / "long-context.txt"
    text = "Earlier context " * 90 + "Fail   is\nthe positive class." + " Later context" * 110
    source.write_text(text, encoding="utf-8")
    material = base.import_material(paper["paper_id"], path=source, confirmed=True, version_id=paper["preferred_version_id"])
    registry = ToolRegistry(base, collection_id=cid)
    sid = registry.research.begin("Metric", cid, critical_checks=checks)["session_id"]
    read = registry.research.read(sid, material["material_id"])
    registry.research.submit_detail(sid, "positive-class", "Fail is positive.", [{"read_id": read["read_id"], "quote": QUOTE}])
    evidence = registry.research.detail_review_packet(sid, "positive-class")["anchors"][0]
    context = evidence["packet_context"]
    assert len(context["text"]) == 1200 and context["normalized_start"] > 0
    assert context["normalized_end"] - context["normalized_start"] == 1200
    quote_offset = evidence["normalized_quote_offset"] - context["normalized_start"]
    assert context["text"][quote_offset:quote_offset + len(QUOTE)] == QUOTE
    assert context["quote_complete"]
