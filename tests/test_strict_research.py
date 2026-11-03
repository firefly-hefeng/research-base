"""Strict task policy is host-owned; actual reads alone cannot complete it."""
import hashlib
import json
import re

import httpx
import pytest

from research_base.agent import ResearchAgent
from research_base.core import ResearchBase
from research_base.models import ResearchError
from research_base.tools import ToolRegistry
from research_base.usage import ResearchUsage


QUOTE = "Fail is the positive class."


@pytest.fixture
def library(tmp_path):
    with ResearchBase(tmp_path / "lib") as core:
        cid = core.create_collection("Evaluation")['collection_id']
        papers, materials = [], []
        for index, title in enumerate(("Filtering evaluation metric", "Protein structure analysis")):
            paper = core.add_paper({"source": "manual", "source_id": str(index), "title": title}, cid)
            path = tmp_path / f"paper-{index}.txt"
            path.write_text(QUOTE if index == 0 else "A protein folding method.", encoding="utf-8")
            material = core.import_material(paper["paper_id"], path=path, confirmed=True)
            papers.append(paper["paper_id"])
            materials.append(material["material_id"])
        core.configure_frontier(cid, papers)
        yield core, cid, papers, materials


def check(paper):
    return {"check_id": "class", "paper_id": paper, "kind": "metric",
            "question": "Which class is positive, and what does that imply?", "required_role": "main_text"}


def verdict(value="supported"):
    return {"verdict": value, "reviewer": "independent-test-reviewer", "reason": "Inspected actual source context and interpretation."}


def fresh(core, monkeypatch):
    original = core.prepare_context
    def simulated_freshness(*args, **kwargs):
        result = original(*args, **kwargs)
        result["freshness"] = {**result["freshness"], "needs_refresh": False, "reason": None}
        return result
    monkeypatch.setattr(core, "prepare_context", simulated_freshness)


def test_strict_automatically_selects_relevant_frontier_not_entire_space(library):
    core, cid, papers, materials = library
    packets = []
    def planner(packet):
        packets.append(packet)
        assert packet["selected_focus_paper_ids"] == []
        assert packet["suggested_focus_paper_ids"] == papers[:1]
        assert "local_path" not in json.dumps(packet) and QUOTE not in json.dumps(packet)
        return {"focus_paper_ids": papers[:1], "critical_checks": [check(papers[0])], "reason": "Filtering metric is relevant to evaluation."}
    usage = ResearchUsage(core)
    started = usage.begin("Filtering evaluation", cid, mode="strict", critical_planner=planner)
    assert started["preparation"]["focus_paper_ids"] == papers[:1]
    assert len(packets) == 1
    assert not started["usage"]["enforcement"]["strict_research_completed"]
    assert [c["paper_id"] for c in started["usage"]["critical_checks"]] == papers[:1]


def test_automatic_search_suggestions_can_be_replaced_but_explicit_scope_is_fixed(library):
    core, cid, papers, _ = library
    usage = ResearchUsage(core)
    def alternate(packet):
        assert packet['suggested_focus_paper_ids'] == papers[:1]
        return {'focus_paper_ids': papers[1:], 'critical_checks': [check(papers[1])],
                'reason': 'Trusted host identifies the protein methods as the relevant comparison for this task.'}
    started = usage.begin('Filtering evaluation', cid, mode='strict', critical_planner=alternate)
    assert started['preparation']['focus_paper_ids'] == papers[1:]
    with pytest.raises(ResearchError, match='host-selected reading scope'):
        usage.begin('Filtering evaluation', cid, mode='strict', focus_paper_ids=papers[:1], critical_planner=alternate)


def test_generated_plan_is_one_question_per_paper_but_host_can_supply_more(library):
    core, cid, papers, _ = library
    many = [check(papers[0]), {**check(papers[0]), 'check_id': 'second', 'question': 'A second decision detail?'}]
    with pytest.raises(ResearchError, match='exactly one'):
        ResearchUsage(core).begin('Filtering evaluation', cid, mode='strict', critical_planner=lambda packet: {
            'focus_paper_ids': papers[:1], 'critical_checks': many, 'reason': 'Two generated questions are excessive.'})
    started = ResearchUsage(core).begin('Filtering evaluation', cid, mode='strict', critical_checks=many)
    assert len(started['usage']['critical_checks']) == 2
    long_check = {**check(papers[0]), 'question': 'Q' * 401}
    with pytest.raises(ResearchError, match='400 characters'):
        ResearchUsage(core).begin('Filtering evaluation', cid, mode='strict', critical_planner=lambda packet: {
            'focus_paper_ids': papers[:1], 'critical_checks': [long_check], 'reason': 'An excessive generated question.'})
    assert ResearchUsage(core).begin('Filtering evaluation', cid, mode='strict', critical_checks=[long_check])['usage']['critical_checks']


def test_strict_fallback_requires_trusted_relevance_plan_and_rejects_fabricated_id(library):
    core, cid, papers, _ = library
    usage = ResearchUsage(core)
    def planner(packet):
        assert packet["selected_focus_paper_ids"] == []
        return {"focus_paper_ids": papers[:1], "critical_checks": [check(papers[0])], "reason": "Host connects the terminology to filtering evaluation."}
    assert usage.begin("Unmatched terminology", cid, mode="strict", critical_planner=planner)["preparation"]["focus_paper_ids"] == papers[:1]
    with pytest.raises(ResearchError, match="bounded included frontier"):
        usage.begin("Unmatched terminology", cid, mode="strict", critical_planner=lambda p: {
            "focus_paper_ids": ["fabricated"], "critical_checks": [check("fabricated")], "reason": "Invented."})


def test_strict_requires_configured_frontier_and_task_checks(library):
    core, cid, papers, _ = library
    with pytest.raises(ResearchError) as error:
        ResearchUsage(core).begin("Filtering evaluation", cid, mode="strict")
    assert error.value.code == "RESEARCH_POLICY_REQUIRED"
    core.configure_frontier(cid, [])
    with pytest.raises(ResearchError) as error:
        ResearchUsage(core).begin("Filtering evaluation", cid, mode="strict", critical_checks=[check(papers[0])])
    assert error.value.code == "RESEARCH_FRONTIER_REQUIRED"


def test_strict_reading_and_detail_review_cannot_bypass_current_search_or_final_draft(library, monkeypatch):
    core, cid, papers, materials = library
    tools = ToolRegistry(core, collection_id=cid)
    opened = tools.begin_bound_research("Filtering evaluation", critical_checks=[check(papers[0])])
    sid = opened["session_id"]
    read = tools.call("read_research_material", {"session_id": sid, "material_id": materials[0]})["data"]
    tools.research.submit_detail(sid, "class", "Fail is positive.", [{"read_id": read["read_id"], "quote": QUOTE}])
    tools.review_research_detail(sid, "class", expected_revision=1, **verdict())
    result = tools.research.check(sid)
    assert result["reading_ready"] and result["detail_ready"] and not result["ready"]
    with pytest.raises(ResearchError):
        tools.require_checked_synthesis(sid, "Fail is positive.", lambda p: verdict())
    fresh(core, monkeypatch)
    ready = tools.research.check(sid)
    assert ready["enforcement"]["strict_ready_for_final_review"]
    assert not ready["enforcement"]["strict_research_completed"]
    assert tools.call("begin_research", {"task": "Reset", "collection_id": cid})["error"]["code"] == "RESEARCH_SESSION_BOUND"
    final = tools.require_checked_synthesis(sid, "Fail is positive.", lambda p: verdict(), require_current_frontier=True)
    assert final["research_usage"]["enforcement"]["strict_research_completed"]
    # A later procedural check does not manufacture an acceptance of a new draft.
    assert not tools.research.check(sid)["enforcement"]["strict_research_completed"]


def test_ordinary_reading_ready_is_explicitly_not_strict_completion(library):
    core, cid, _, materials = library
    usage = ResearchUsage(core)
    sid = usage.begin("Filtering evaluation", cid)["session_id"]
    usage.read(sid, materials[0])
    result = usage.check(sid)
    assert result["ready"] and result["policy"]["mode"] == "explore"
    assert not result["enforcement"]["strict_research_completed"]


def reply(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"id": str(i), "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments)}} for i, (name, arguments) in enumerate(calls)]
    return httpx.Response(200, json={"choices": [{"message": message}], "usage": {"total_tokens": 17}})


def run_default(library, monkeypatch, *, final_verdict="supported", detail_response="supported", current=True,
                automatic=True):
    core, cid, papers, materials = library
    if current:
        fresh(core, monkeypatch)
    draft_steps, auxiliary_payloads = 0, []
    last_read = None
    def handler(request):
        nonlocal draft_steps, last_read
        payload = json.loads(request.content)
        system = payload["messages"][0]["content"]
        if "response_format" in payload:
            auxiliary_payloads.append(payload)
            assert "tools" not in payload
            assert str(core.store.root) not in json.dumps(payload)
            if system.startswith("Plan a small"):
                packet = json.loads(payload["messages"][1]["content"])
                assert packet["selected_focus_paper_ids"] == []
                assert packet["suggested_focus_paper_ids"] == papers[:1]
                assert QUOTE not in json.dumps(packet)
                return reply(json.dumps({"focus_paper_ids": papers[:1], "critical_checks": [check(papers[0])], "reason": "Relevant evaluation metric."}))
            if system.startswith("Independently check"):
                if detail_response == "invalid":
                    return reply("not JSON")
                if detail_response == "timeout":
                    raise httpx.ReadTimeout("Offline simulated review timeout", request=request)
                return reply(json.dumps(verdict(detail_response)))
            assert system.startswith("Independently review")
            packet = json.loads(payload["messages"][1]["content"])
            assert QUOTE in json.dumps(packet["observed_context"])
            return reply(json.dumps(verdict(final_verdict)))
        sid = re.search(r'"session_id": "(research_\w+)"', payload["messages"][1]["content"])[1]
        draft_steps += 1
        if draft_steps == 1:
            return reply(calls=[("read_research_material", {"session_id": sid, "material_id": materials[0]})])
        if draft_steps == 2:
            last_read = json.loads(payload["messages"][-1]["content"])["data"]
            return reply(calls=[("submit_research_detail", {"session_id": sid, "check_id": "class",
                "answer": "Fail is positive.", "anchors": [{"read_id": last_read["read_id"], "quote": QUOTE}]})])
        if draft_steps > 3 and final_verdict == "contradicted" and detail_response == "supported" and current and automatic:
            feedback = payload["messages"][-1]["content"]
            assert "final_draft_review" in feedback and "Inspected actual source context and interpretation." in feedback
            assert "without resubmitting unchanged supported details" in feedback
        return reply("Fail is positive. RESEARCH_DRAFT")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = ResearchAgent(core, api_key="offline-test", client=client).run("Filtering evaluation", collection_id=cid,
            automatic_review=automatic, critical_checks=None if automatic else [check(papers[0])])
    return result, auxiliary_payloads


def test_bound_agent_default_plans_reads_checks_and_reviews_actual_final(library, monkeypatch):
    result, payloads = run_default(library, monkeypatch)
    assert result["status"] == "completed"
    assert result["research_usage"]["enforcement"]["strict_research_completed"]
    assert [item["kind"] for item in result["auxiliary_model_calls"]] == ["planning", "detail", "synthesis"]
    assert sum(item["usage"]["total_tokens"] for item in result["auxiliary_model_calls"]) == 51
    assert result["synthesis_review"]["draft_sha256"] == hashlib.sha256(result["answer"].encode()).hexdigest()
    assert len(payloads) == 3


@pytest.mark.parametrize("bad_review", ["contradicted", "needs_context", "invalid", "timeout"])
def test_default_detail_rejection_or_invalid_json_fails_closed(library, monkeypatch, bad_review):
    result, _ = run_default(library, monkeypatch, detail_response=bad_review)
    assert result["status"] == "needs_verification"
    assert "RESEARCH_DRAFT" not in result["answer"]
    assert not result["research_usage"]["enforcement"]["strict_research_completed"]


def test_default_final_rejection_and_missing_trusted_review_fail_closed(library, monkeypatch):
    result, _ = run_default(library, monkeypatch, final_verdict="contradicted")
    assert result["status"] == "needs_verification" and "RESEARCH_DRAFT" not in result["answer"]
    result, payloads = run_default(library, monkeypatch, automatic=False)
    assert result["status"] == "needs_verification" and not payloads


def test_default_design_mode_cannot_complete_without_current_search(library, monkeypatch):
    result, _ = run_default(library, monkeypatch, current=False)
    assert result["status"] == "needs_refresh"
    assert "RESEARCH_DRAFT" not in result["answer"]
    assert not result["research_usage"]["ready"]


def test_review_redaction_drops_host_paths_without_mutating_source_packet():
    packet = {"local_path": "C:\\fixture\\project.py", "project_root": "/workspace/fixture/project",
              "answer": "Use C:\\fixture\\notes.txt and /workspace/fixture/local.txt.", "text": QUOTE}
    safe = ResearchAgent._review_payload(packet)
    assert "local_path" not in safe and "project_root" not in safe
    assert "fixture" not in safe["answer"] and "/workspace/fixture" not in safe["answer"]
    assert safe["text"] == QUOTE and "local_path" in packet
