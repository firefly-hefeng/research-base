"""Actual stdio protocol smoke test, skipped when the optional SDK is absent."""
import asyncio
import importlib.util
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def _stdio_smoke(library):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    source_root = Path(__file__).resolve().parents[1] / "src"
    command = "import sys; from research_base.mcp_server import serve; serve(sys.argv[1])"
    env = {"PYTHONPATH": str(source_root), "PATH": os.getenv("PATH", ""), "SYSTEMROOT": os.getenv("SYSTEMROOT", "")}
    parameters = StdioServerParameters(command=sys.executable, args=["-c", command, str(library)], env=env)

    async def run():
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert {"search_library", "read_material", "list_materials", "prepare_context", "export_collection", "claim_browser_task", "import_material", "confirm_material_identity", "discover_frontier", "expand_citations", "retry_acquisition", "resume_job", "cancel_job"} <= names
                assert {"get_frontier", "configure_frontier", "prepare_research", "begin_research",
                        "extend_research", "read_research_material", "check_research", "end_research"} <= names
                created = await session.call_tool("create_collection", {"name": "MCP collection"})
                assert not created.isError
                payload = json.loads(created.content[0].text)
                collection_id = payload["data"]["collection_id"]
                begun = await session.call_tool("begin_research", {"task": "Design an evaluation", "collection_id": collection_id})
                assert not begun.isError
                research_id = json.loads(begun.content[0].text)["data"]["session_id"]
                checked = await session.call_tool("check_research", {"session_id": research_id})
                assert not json.loads(checked.content[0].text)["data"]["ready"]
                ended = await session.call_tool("end_research", {"session_id": research_id})
                assert json.loads(ended.content[0].text)["data"]["status"] == "ended"
                stale_session = await session.call_tool("check_research", {"session_id": research_id})
                assert stale_session.isError
                listed_collections = await session.call_tool("list_collections", {})
                assert json.loads(listed_collections.content[0].text)["data"][0]["collection_id"] == collection_id
                frontier = await session.call_tool("discover_frontier", {
                    "collection_id": collection_id, "query": "RAG evaluation",
                    "seeds": [{"doi": "10.48550/arxiv.2408.08067"}], "budget": {"max_requests": 1}})
                assert not frontier.isError
                queued = json.loads(frontier.content[0].text)["data"]
                assert queued["type"] == "frontier" and queued["state"] == "queued"
                persisted = await session.call_tool("get_job", {"job_id": queued["job_id"]})
                assert not persisted.isError
                assert json.loads(persisted.content[0].text)["data"]["payload"]["seeds"] == [{"doi": "10.48550/arxiv.2408.08067"}]
                resumed = await session.call_tool("resume_job", {"job_id": queued["job_id"]})
                assert not resumed.isError
                cancelled = await session.call_tool("cancel_job", {"job_id": queued["job_id"]})
                assert not cancelled.isError
                assert json.loads(cancelled.content[0].text)["data"]["state"] == "cancelled"
                invalid = await session.call_tool("read_material", {"material_id": "missing", "pages": [0]})
                assert invalid.isError
                # The official SDK may reject schema violations before registry dispatch.
                assert "error" in invalid.content[0].text.lower()

    asyncio.run(asyncio.wait_for(run(), timeout=20))


@pytest.mark.live
@pytest.mark.skipif(os.getenv("RB_RUN_MCP_TESTS") != "1", reason="Set RB_RUN_MCP_TESTS=1 for the actual local stdio check")
@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="MCP optional dependency is not installed")
def test_stdio_server_discovers_tools_and_calls_shared_core(tmp_path):
    # The separate process bounds even Windows event-loop initialization, which can
    # block before an async timeout starts in environments without loopback access.
    project = Path(__file__).resolve().parents[1]
    env = {"PYTHONPATH": os.pathsep.join((str(project / "src"), str(project / "tests"))),
           "PATH": os.getenv("PATH", ""), "SYSTEMROOT": os.getenv("SYSTEMROOT", "")}
    command = ("import asyncio, json, os, sys\nfrom pathlib import Path\n"
               + "__file__ = " + repr(str(Path(__file__).resolve())) + "\n"
               + inspect.getsource(_stdio_smoke) + "\n_stdio_smoke(sys.argv[1])\n")
    try:
        result = subprocess.run([sys.executable, "-c", command, str(tmp_path / "mcp-library")],
                                env=env, capture_output=True, text=True, timeout=35)
    except subprocess.TimeoutExpired:
        pytest.fail("MCP local stdio check timed out; this environment may block Windows loopback initialization")
    assert result.returncode == 0, result.stderr[-6000:]
