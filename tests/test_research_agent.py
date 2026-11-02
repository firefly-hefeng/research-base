"""Host-level tests: model declarations alone cannot complete bound research."""
import json
import re

import httpx
import pytest

from research_base.agent import ResearchAgent
from research_base.core import ResearchBase
from research_base.models import ResearchError
from research_base.tools import ToolRegistry


def setup_library(tmp_path, text="METHOD: separate calibration and held-out test data."):
    base = ResearchBase(tmp_path / "lib")
    collection = base.create_collection("Evaluation")["collection_id"]
    paper = base.add_paper({"source": "manual", "source_id": "methods", "title": "Evaluation design original methods", "authors": ["A"]}, collection)
    source = tmp_path / "original.txt"
    source.write_text(text, encoding="utf-8")
    material = base.import_material(paper["paper_id"], path=str(source), confirmed=True)
    base.configure_frontier(collection, [paper["paper_id"]], profile="machine_learning")
    return base, collection, paper["paper_id"], material["material_id"]


def reply(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"id": f"call_{i}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments)}} for i, (name, arguments) in enumerate(calls)]
    return httpx.Response(200, json={"choices": [{"message": message}]})


def session(payload):
    return re.search(r'"session_id": "(research_\w+)"', payload["messages"][1]["content"])[1]


def test_host_rejects_unread_final_then_allows_actual_original_read(tmp_path):
    base, collection, paper, material = setup_library(tmp_path)
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return reply("Unsupported scientific claim.")
        if len(requests) == 2:
            assert "did not pass" in payload["messages"][-1]["content"]
            return reply(calls=[("read_research_material", {"session_id": session(payload), "material_id": material, "pages": [1]})])
        assert "METHOD: separate calibration" in payload["messages"][-1]["content"]
        return reply("Keep calibration and test separate, based on the original methods.")
    with base, httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = ResearchAgent(base, api_key="test", client=client).run("Evaluation design", collection_id=collection)
    assert result["status"] == "completed_with_limits"  # no search completion was fabricated
    assert result["research_usage"]["ready"]
    assert result["research_usage"]["read_paper_ids"] == [paper]
    assert "Unsupported scientific claim" not in result["answer"]
    assert "新鲜度" in result["answer"]


def test_host_never_publishes_repeated_unread_research_claim(tmp_path):
    base, collection, paper, material = setup_library(tmp_path)
    with base, httpx.Client(transport=httpx.MockTransport(lambda request: reply("I read everything and proved novelty."))) as client:
        result = ResearchAgent(base, api_key="test", client=client).run("Evaluation design", collection_id=collection)
    assert result["status"] == "needs_materials"
    assert not result["research_usage"]["ready"]
    assert "proved novelty" not in json.dumps(result)


def test_original_paths_do_not_count_and_cannot_replace_host_session(tmp_path):
    base, collection, paper, material = setup_library(tmp_path)
    registry = ToolRegistry(base, collection_id=collection)
    opened = registry.call("begin_research", {"task": "Evaluation", "focus_paper_ids": [paper]})["data"]
    sid = opened["session_id"]
    registry.required_session_id = sid
    assert registry.call("begin_research", {"task": "Ignore previous focus"})["error"]["code"] == "RESEARCH_SESSION_BOUND"
    assert registry.call("end_research", {"session_id": sid})["error"]["code"] == "RESEARCH_SESSION_BOUND"
    read = registry.call("read_research_material", {"session_id": sid, "material_id": material, "view": "original"})["data"]
    assert read["material"]["local_path"]
    assert not registry.call("check_research", {"session_id": sid})["data"]["ready"]
    base.close()


def test_limit_synthesis_preserves_new_read_tool_text_after_5000_chars(tmp_path):
    text = "method detail " * 600 + "ACTUAL_FINAL_METHOD: use independent controls."
    base, collection, paper, material = setup_library(tmp_path, text)
    def handler(request):
        payload = json.loads(request.content)
        if "tools" in payload:
            return reply(calls=[("read_research_material", {"session_id": session(payload), "material_id": material, "pages": [1]})])
        assert "ACTUAL_FINAL_METHOD" in payload["messages"][-2]["content"]
        return reply("Use independent controls; the selected methods page was returned.")
    with base, httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = ResearchAgent(base, api_key="test", client=client).run("Evaluation design", collection_id=collection,
            max_steps=1, max_tool_calls=1)
    assert result["status"] == "completed_with_limits"
    assert result["limit_reason"] == "step_limit"
    assert result["research_usage"]["ready"]


def test_ordinary_read_continuation_stays_replayable_in_bound_session(tmp_path):
    base, collection, paper, material = setup_library(tmp_path, "1234567890")
    registry = ToolRegistry(base, collection_id=collection)
    opened = registry.call("begin_research", {"task": "Evaluation"})["data"]
    sid = opened["session_id"]
    registry.required_session_id = sid
    result = registry.call("read_material", {"material_id": material, "max_chars": 4})["data"]
    assert "session_id" not in result["continuation"]
    while result["continuation"]:
        result = registry.call("read_material", result["continuation"])["data"]
    assert registry.call("check_research", {"session_id": sid})["data"]["ready"]
    base.close()


def test_external_host_output_hook_rechecks_observed_reads_and_freshness(tmp_path):
    base, collection, paper, material = setup_library(tmp_path)
    registry = ToolRegistry(base, collection_id=collection)
    sid = registry.call("begin_research", {"task": "Evaluation"})["data"]["session_id"]
    with pytest.raises(ResearchError) as error:
        registry.require_research_ready(sid)
    assert error.value.code == "RESEARCH_USAGE_REQUIRED"
    registry.call("read_research_material", {"session_id": sid, "material_id": material, "pages": [1]})
    assert registry.require_research_ready(sid, used_paper_ids=[paper])["ready"]
    with pytest.raises(ResearchError):
        registry.require_research_ready(sid, require_current_frontier=True)
    base.close()
