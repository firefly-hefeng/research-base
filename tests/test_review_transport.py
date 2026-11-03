"""Public redirect boundaries and lossless independent-review formatting."""
import json

import httpx
import pytest

from research_base.agent import ResearchAgent
from research_base.models import ResearchError
from research_base.resolvers import _request


@pytest.mark.parametrize("destination", ["https://127.0.0.1/record", "https://[::1]/record", "https://10.1.2.3/record",
                                         "https://localhost/record", "http://public.example.invalid/record"])
def test_public_metadata_redirect_is_validated_before_destination_request(destination):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": destination})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            _request(client, "https://public.example.invalid/record")
    assert len(requests) == 1


def test_public_metadata_redirect_retains_limits_and_does_not_forward_credentials():
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            assert request.url.params["token"] == "synthetic-test-value"
            return httpx.Response(302, headers={"Location": "https://alternate.example.invalid/record"})
        assert "Authorization" not in request.headers
        assert "token" not in request.url.params
        return httpx.Response(200, json={"article": "metadata"})
    with httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer synthetic-test-value"}) as client:
        status, final, _, raw = _request(client, "https://public.example.invalid/record", params={"token": "synthetic-test-value"})
    assert status == 200 and final.startswith("https://alternate.example.invalid/")
    assert json.loads(raw)["article"] == "metadata" and len(requests) == 2


def test_public_metadata_redirects_have_a_finite_hop_budget():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "/record/" + str(len(requests))})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="limit"):
            _request(client, "https://public.example.invalid/start")
    assert len(requests) == 6


def completion(value):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": json.dumps(value)}}],
                                   "usage": {"total_tokens": 11}})


def test_review_extra_note_is_preserved_losslessly_without_strengthening_verdict():
    calls, ledger = [], []
    note = "The proposed experiment is unrun and must not be called a measured improvement."
    def handler(request):
        calls.append(json.loads(request.content))
        return completion({"verdict": "needs_context", "reviewer": "independent", "reason": "Missing an essential factual definition.",
                           "verdict_note": note})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        answer = ResearchAgent(object(), api_key="offline", client=client)._independent_json("synthesis", {"answer": "An unrun proposal."}, ledger)
    assert answer["verdict"] == "needs_context" and note in answer["reason"]
    assert set(answer) == {"verdict", "reviewer", "reason"}
    assert ledger[0]["status"] == "format_repaired" and len(calls) == 1
    diagnostic = ledger[0]["schema_diagnostic"]
    assert diagnostic["extra_fields"] == ["verdict_note"] and note not in json.dumps(diagnostic)


@pytest.mark.parametrize("repair_verdict", ["needs_context", "supported"])
def test_one_format_retry_preserves_notes_and_cannot_change_original_verdict(repair_verdict):
    ledger, calls = [], []
    note = "Keep this substantive caveat verbatim."
    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return completion({"verdict": "needs_context", "reviewer": "independent", "reason": "R" * 1700, "scope_note": note})
        assert "Do not re-evaluate science" in calls[-1]["messages"][0]["content"]
        return completion({"verdict": repair_verdict, "reviewer": "independent", "reason": "The essential definition remains unavailable."})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        agent = ResearchAgent(object(), api_key="offline", client=client)
        if repair_verdict == "supported":
            with pytest.raises(ResearchError, match="without changing its verdict"):
                agent._independent_json("detail", {"answer": "A source interpretation."}, ledger)
        else:
            result = agent._independent_json("detail", {"answer": "A source interpretation."}, ledger)
            assert result["verdict"] == "needs_context" and note in result["reason"]
    assert len(calls) == 2 and [row["kind"] for row in ledger] == ["detail", "format_repair"]
    assert sum(row["usage"]["total_tokens"] for row in ledger) == 22


def test_meaningful_note_too_large_to_preserve_is_not_silently_dropped():
    ledger = []
    def handler(request):
        return completion({"verdict": "supported", "reviewer": "independent", "reason": "A bounded source claim.",
                           "important_note": "N" * 1700})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ResearchError):
            ResearchAgent(object(), api_key="offline", client=client)._independent_json("detail", {}, ledger)
    assert len(ledger) == 1 and ledger[0]["status"] == "invalid_response"
