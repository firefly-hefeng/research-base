import json

import httpx
import pytest

from research_base.agent import ResearchAgent
from research_base.models import ResearchError


class Core:
    def __init__(self):
        self.calls = []

    def list_collections(self):
        self.calls.append("list_collections")
        return [{"collection_id": "c1"}]


def completion(message, usage=None):
    return {"choices": [{"message": message}], "usage": usage or {"prompt_tokens": 5, "completion_tokens": 2}}


def test_tool_loop_roundtrips_original_message_and_hides_reasoning():
    requests = []
    replies = [
        completion({"role": "assistant", "content": None, "reasoning_content": "internal reasoning", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "list_collections", "arguments": "{}"}}]}),
        completion({"role": "assistant", "content": "Collection c1 is available."}),
    ]
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=replies.pop(0))
    core = Core()
    agent = ResearchAgent(core, api_key="fake-test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = agent.run("Find the relevant collection")
    assert result["status"] == "unbound_completed"
    assert result["research_policy"] == {"mode": "unbound", "strict_research_completed": False}
    assert result["answer"] == "Collection c1 is available."
    assert core.calls == ["list_collections"]
    assert requests[1]["messages"][2]["reasoning_content"] == "internal reasoning"
    assert requests[1]["messages"][3]["tool_call_id"] == "call_1"
    assert "internal reasoning" not in json.dumps(result)
    assert result["usage"]["prompt_tokens"] == 10


def test_read_only_agent_cannot_execute_write_tool_hallucination():
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, json=completion({"role": "assistant", "content": None, "tool_calls": [
                {"id": "x", "type": "function", "function": {"name": "discover_papers", "arguments": '{"collection_id":"c","query":"q"}'}}]}))
        assert "TOOL_NOT_ALLOWED" in body["messages"][-1]["content"]
        return httpx.Response(200, json=completion({"role": "assistant", "content": "Write tools are unavailable."}))
    result = ResearchAgent(Core(), api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).run("Search")
    assert result["tool_calls"][0]["error_code"] == "TOOL_NOT_ALLOWED"
    names = {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert "discover_papers" not in names
    assert "export_collection" not in names


def test_invalid_json_arguments_return_tool_error_and_continue():
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json=completion({"role": "assistant", "content": None, "tool_calls": [
                {"id": "x", "type": "function", "function": {"name": "list_collections", "arguments": "invalid json"}}]}))
        assert "INVALID_ARGUMENT" in json.loads(request.content)["messages"][-1]["content"]
        return httpx.Response(200, json=completion({"role": "assistant", "content": "Corrected."}))
    result = ResearchAgent(Core(), api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).run("Read")
    assert result["status"] == "unbound_completed"
    assert result["tool_calls"][0]["status"] == "error"


def test_agent_stops_at_step_budget():
    def handler(request):
        if "tools" not in json.loads(request.content):
            return httpx.Response(200, json=completion({"role": "assistant", "content": "Limited answer from the available collection information."}))
        return httpx.Response(200, json=completion({"role": "assistant", "content": None, "tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "list_collections", "arguments": "{}"}}]}))
    core = Core()
    result = ResearchAgent(core, api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).run("Read", max_steps=2)
    assert result["status"] == "unbound_completed"
    assert result["limit_reason"] == "step_limit"
    assert len(core.calls) == 2


def test_agent_stops_at_tool_budget_without_executing_extra_call():
    def handler(request):
        calls = [{"id": f"x{i}", "type": "function", "function": {"name": "list_collections", "arguments": "{}"}} for i in range(3)]
        return httpx.Response(200, json=completion({"role": "assistant", "content": None, "tool_calls": calls}))
    core = Core()
    result = ResearchAgent(core, api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).run("Read", max_tool_calls=1)
    assert result["status"] == "tool_limit"
    assert len(core.calls) == 1


def test_http_errors_do_not_expose_provider_body_or_key():
    def handler(request):
        return httpx.Response(401, text="sensitive-provider-text")
    agent = ResearchAgent(Core(), api_key="fake-secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ResearchError) as error:
        agent.run("Read")
    assert error.value.code == "MODEL_AUTH_FAILED"
    assert "sensitive-provider-text" not in str(error.value)
    assert "fake-secret" not in str(error.value)


def test_missing_key_makes_no_request(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    def handler(request):
        raise AssertionError("Network should not be used")
    agent = ResearchAgent(Core(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ResearchError, match="DEEPSEEK_API_KEY"):
        agent.run("Read")


def test_plan_queries_does_not_call_library_tools():
    core = Core()
    plan = {"query": "evaluation", "synonyms": ["benchmark"], "sources": ["openalex", "europe_pmc"], "source_queries": {"openalex": "evaluation"}}
    def handler(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert "tools" not in payload
        return httpx.Response(200, json=completion({"role": "assistant", "content": json.dumps(plan)}))
    result = ResearchAgent(core, api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).plan_queries("evaluation methods")
    assert result["plan"] == plan
    assert core.calls == []


def test_invalid_plan_and_remote_http_url_are_rejected():
    def handler(request):
        return httpx.Response(200, json=completion({"role": "assistant", "content": '{"query":"q","sources":["invented"]}'}))
    agent = ResearchAgent(Core(), api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ResearchError) as error:
        agent.plan_queries("q")
    assert error.value.code == "MODEL_INVALID_RESPONSE"
    with pytest.raises(ResearchError):
        ResearchAgent(Core(), base_url="http://example.com")


def test_query_plans_accept_new_registry_sources_without_hardcoded_names():
    class DynamicSourcesCore(Core):
        def list_sources(self):
            return [{"name": "doaj"}, {"name": "datacite"}]
    plan = {"query": "evaluation", "synonyms": [], "sources": ["doaj", "datacite"], "source_queries": {"doaj": "evaluation"}}
    def handler(request):
        assert "datacite" in json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(200, json=completion({"role": "assistant", "content": json.dumps(plan)}))
    agent = ResearchAgent(DynamicSourcesCore(), api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert agent.plan_queries("evaluation")["plan"]["sources"] == ["doaj", "datacite"]


def test_select_papers_uses_metadata_and_never_changes_membership():
    core = Core()
    candidates = [{"paper_id": "p1", "title": "Evaluation methods", "authors": ["A"], "year": 2026, "abstract": "Methods", "local_path": "C:/private.pdf"},
                  {"paper_id": "p2", "title": "Unrelated", "abstract": "Another topic"}]
    def handler(request):
        payload = json.loads(request.content)
        submitted = json.loads(payload["messages"][1]["content"])
        assert "local_path" not in submitted["candidates"][0]
        assert payload["response_format"] == {"type": "json_object"}
        assert "tools" not in payload
        return httpx.Response(200, json=completion({"role": "assistant", "content": json.dumps({"selected_paper_ids": ["p1", "p1"], "reasons": {"p1": "Relevant experimental method"}})}))
    result = ResearchAgent(core, api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).select_papers("Design evaluation", candidates, max_papers=1)
    assert result["selected_paper_ids"] == ["p1"]
    assert result["reasons"]["p1"] == "Relevant experimental method"
    assert result["considered_count"] == 2
    assert core.calls == []


@pytest.mark.parametrize("selection", [
    {"selected_paper_ids": ["invented"], "reasons": {"invented": "Not actually a candidate"}},
    {"selected_paper_ids": ["p1", "p2"], "reasons": {"p1": "One", "p2": "Two"}},
    {"selected_paper_ids": ["p1"], "reasons": {}},
])
def test_select_papers_rejects_unknown_ids_excess_count_and_missing_reason(selection):
    def handler(request):
        return httpx.Response(200, json=completion({"role": "assistant", "content": json.dumps(selection)}))
    agent = ResearchAgent(Core(), api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ResearchError) as error:
        agent.select_papers("Select", [{"paper_id": "p1"}, {"paper_id": "p2"}], max_papers=1)
    assert error.value.code == "MODEL_INVALID_RESPONSE"


def test_select_papers_empty_candidates_do_not_require_model_or_key():
    def handler(request):
        raise AssertionError("Empty input must not request a completion")
    result = ResearchAgent(Core(), api_key="", client=httpx.Client(transport=httpx.MockTransport(handler))).select_papers("Select", [])
    assert result["selected_paper_ids"] == []
    assert result["usage"] == {}


def test_select_papers_bounds_metadata_and_reports_candidate_truncation():
    candidates = [{"paper_id": f"p{i}", "title": "t" * 1000, "abstract": "a" * 3000, "authors": ["x"] * 20} for i in range(101)]
    def handler(request):
        submitted = json.loads(json.loads(request.content)["messages"][1]["content"])["candidates"]
        assert len(submitted) == 100
        assert len(submitted[0]["title"]) == 500
        assert len(submitted[0]["abstract"]) == 1500
        assert len(submitted[0]["authors"]) == 8
        return httpx.Response(200, json=completion({"role": "assistant", "content": '{"selected_paper_ids":[],"reasons":{}}'}))
    result = ResearchAgent(Core(), api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).select_papers("Select", candidates)
    assert result["truncated"] is True
    assert result["considered_count"] == 100


class ProjectCore(Core):
    library_id = "lib_project"

    def __init__(self):
        super().__init__()
        self.prepared = []
        self.material_queries = []

    def prepare_context(self, task, **kwargs):
        self.prepared.append((task, kwargs))
        return {"collection_id": "c_project", "freshness": {"needs_refresh": True, "reason": "stale"},
                "papers": [{"paper_id": "p1", "title": "Relevant Original Material"}], "gaps": []}

    def prepare_research(self, task, **kwargs):
        context = self.prepare_context(task, collection_id=kwargs["collection_id"],
                                       max_papers=kwargs["max_papers"], max_chars=kwargs["max_chars"],
                                       project_root=kwargs.get("project_root"))
        return {"task": task, "stage": kwargs["stage"], "context": context,
                "frontier": {}, "focus_paper_ids": [], "warnings": [], "project_root": kwargs.get("project_root")}

    def list_materials(self, **kwargs):
        self.material_queries.append(kwargs)
        return []

    def get_paper(self, paper_id):
        raise ResearchError("NOT_FOUND", "Navigation-only test fixture has no originals")


def project_files(tmp_path, *, entry="AGENTS.md", rules="PROJECT RULE: compare current frontier before experiment design.", library="lib_project"):
    import yaml
    root = tmp_path / "project"
    (root / ".research").mkdir(parents=True)
    config = {"schema_version": 1, "library_id": library, "collection_id": "c_project", "integration": {"mode": "agents", "entry_file": entry}}
    (root / ".research" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    if rules is not None and not entry.startswith(".."):
        target = root / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rules, encoding="utf-8")
    return root


def test_project_run_loads_actual_rules_prepares_context_once_and_scopes_tools(tmp_path):
    root = project_files(tmp_path)
    original = (root / "AGENTS.md").read_bytes()
    core = ProjectCore()
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "PROJECT RULE: compare current frontier" in payload["messages"][0]["content"]
        assert '"needs_refresh": true' in payload["messages"][1]["content"]
        if len(requests) == 1:
            return httpx.Response(200, json=completion({"role": "assistant", "content": None, "tool_calls": [
                {"id": "materials", "type": "function", "function": {"name": "list_materials", "arguments": "{}"}}]}))
        return httpx.Response(200, json=completion({"role": "assistant", "content": "The collection is stale; verify newer work before finalizing."}))
    result = ResearchAgent(core, api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).run("Design an experiment", project_root=root, research_mode="explore")
    assert core.prepared[0][1]["collection_id"] == "c_project"
    assert core.prepared[0][1]["project_root"] == str(root.resolve())
    assert sum(1 for item in core.prepared if item[1].get("max_chars") == 10000) == 1
    assert core.material_queries == [{"collection_id": "c_project"}]
    assert result["project_integration"]["rules_loaded"] is True
    assert result["project_integration"]["context_prepared"] is True
    assert result["project_integration"]["project_root"] == str(root.resolve())
    assert (root / "AGENTS.md").read_bytes() == original


def test_project_run_respects_configured_rule_entry(tmp_path):
    root = project_files(tmp_path, entry="rules/research.md", rules="CUSTOM ENTRY RULE")
    def handler(request):
        payload = json.loads(request.content)
        assert "CUSTOM ENTRY RULE" in payload["messages"][0]["content"]
        assert str(root.resolve()) not in payload["messages"][1]["content"]
        assert "project_root" not in payload["messages"][1]["content"]
        return httpx.Response(200, json=completion({"role": "assistant", "content": "Ready."}))
    result = ResearchAgent(ProjectCore(), api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).run("Analyze", project_root=root, research_mode="explore")
    assert result["project_integration"]["entry_file"] == str((root / "rules/research.md").resolve())


@pytest.mark.parametrize("kind,code", [("wrong_library", "LIBRARY_ID_MISMATCH"), ("outside", "PATH_OUTSIDE_PROJECT"), ("wrong_collection", "INVALID_ARGUMENT")])
def test_invalid_project_binding_is_rejected_before_model_call(tmp_path, kind, code):
    root = project_files(tmp_path, library="wrong" if kind == "wrong_library" else "lib_project", entry="../outside.md" if kind == "outside" else "AGENTS.md")
    core = ProjectCore()
    def handler(request):
        raise AssertionError("Invalid project must not be sent to the model")
    agent = ResearchAgent(core, api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ResearchError) as error:
        agent.run("Analyze", project_root=root, collection_id="another" if kind == "wrong_collection" else None)
    assert error.value.code == code
    assert core.prepared == []


def test_missing_project_rules_are_reported_without_claiming_load(tmp_path):
    root = project_files(tmp_path, rules=None)
    def handler(request):
        assert "rule entry status: missing" in json.loads(request.content)["messages"][0]["content"]
        return httpx.Response(200, json=completion({"role": "assistant", "content": "Using available library context only."}))
    result = ResearchAgent(ProjectCore(), api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).run("Analyze", project_root=root, research_mode="explore")
    assert result["project_integration"]["rules_loaded"] is False
    assert result["project_integration"]["rules_status"] == "missing"
    assert result["project_integration"]["context_prepared"] is True


def test_project_navigation_is_bounded_and_preserves_freshness(tmp_path):
    root = project_files(tmp_path)
    class LargeNavigation(ProjectCore):
        def prepare_context(self, *args, **kwargs):
            data = super().prepare_context(*args, **kwargs)
            data["papers"][0]["large_metadata"] = "X" * 60000
            return data
    def handler(request):
        content = json.loads(request.content)["messages"][1]["content"]
        assert len(content) < 25000
        assert '"needs_refresh": true' in content
        assert '"truncated": true' in content
        return httpx.Response(200, json=completion({"role": "assistant", "content": "Limited navigation."}))
    ResearchAgent(LargeNavigation(), api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).run("Analyze", project_root=root, research_mode="explore")


def test_context_limit_synthesizes_once_without_more_tools():
    class LargeCore(Core):
        def list_materials(self, **kwargs):
            self.calls.append("list_materials")
            return [{"material_id": "m", "description": "X" * 40000}]
    core, requests = LargeCore(), []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            calls = [{"id": f"c{i}", "type": "function", "function": {"name": "list_materials", "arguments": "{}"}} for i in range(7)]
            return httpx.Response(200, json=completion({"role": "assistant", "content": None, "tool_calls": calls}))
        assert "tools" not in payload
        assert "Do not claim exhaustive literature coverage" in payload["messages"][-1]["content"]
        return httpx.Response(200, json=completion({"role": "assistant", "content": "A limited plan; full texts remain to be read."}))
    result = ResearchAgent(core, api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).run("Plan research", max_tool_calls=7)
    assert result["status"] == "unbound_completed"
    assert result["limit_reason"] == "context_limit"
    assert len(core.calls) == 7
    assert len(requests) == 2


def test_final_synthesis_preserves_read_original_text_and_tool_budget():
    class ReadCore(Core):
        def read_material(self, **kwargs):
            self.calls.append("read_material")
            return {"material": {"material_id": "m1", "role": "main_text"},
                    "pages": [{"page": 3, "text": "ACTUAL_METHOD: use held-out evaluation."}]}
    core, requests = ReadCore(), []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json=completion({"role": "assistant", "content": None, "tool_calls": [
                {"id": "r1", "type": "function", "function": {"name": "read_material", "arguments": '{"material_id":"m1"}'}}]}))
        assert "tools" not in payload
        assert "ACTUAL_METHOD: use held-out evaluation." in payload["messages"][-2]["content"]
        return httpx.Response(200, json=completion({"role": "assistant", "content": "Use held-out evaluation [m1], with remaining coverage limitations."}))
    result = ResearchAgent(core, api_key="fake", client=httpx.Client(transport=httpx.MockTransport(handler))).run("Design experiment", max_steps=1, max_tool_calls=1)
    assert result["status"] == "unbound_completed"
    assert core.calls == ["read_material"]
    assert len(result["tool_calls"]) == 1
