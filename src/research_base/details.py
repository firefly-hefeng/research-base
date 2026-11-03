"""Ephemeral task-critical interpretations anchored to actually returned text.

Quote matching establishes source access, never scientific entailment. Only a
trusted independent host reviewer can accept an interpretation. Nothing here is
a persistent paper card or an agent-callable self-certification method.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import re
import unicodedata

from .models import ResearchError


ORIGINAL_ROLES = frozenset(("main_text", "supplement", "code", "data", "model"))
ANSWER_FIELDS = frozenset(("summary", "numerator", "denominator", "positive_class", "evaluation_unit", "conditions", "aggregation"))


def normalized_quote(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def critical_requirements(checks) -> tuple[dict, ...]:
    if checks is None:
        return ()
    if not isinstance(checks, list) or len(checks) > 8:
        raise ResearchError("INVALID_ARGUMENT", "Choose at most eight task-critical detail checks")
    result, seen = [], set()
    for item in checks:
        if (not isinstance(item, dict) or set(item) - {"check_id", "paper_id", "kind", "question", "required_role"}
                or any(not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > limit
                       for key, limit in (("check_id", 100), ("paper_id", 300), ("kind", 100), ("question", 2000)))
                or (item.get("required_role") is not None and (not isinstance(item["required_role"], str)
                    or item["required_role"] not in ORIGINAL_ROLES))):
            raise ResearchError("INVALID_ARGUMENT", "Critical checks require check_id, paper_id, kind, question and an optional original-material role")
        if item["check_id"] in seen:
            raise ResearchError("INVALID_ARGUMENT", "Critical check identifiers must be unique")
        seen.add(item["check_id"])
        result.append(deepcopy(item))
    return tuple(result)


def same_source(receipt: dict, material: dict, *, check_bytes: bool = True) -> bool:
    metadata_matches = (material.get("status") == "available" and material.get("exists")
            and material.get("role") in ORIGINAL_ROLES and material.get("details", {}).get("identity_confirmed")
            and all(receipt.get(key) == material.get(key) for key in ("paper_id", "version_id", "sha256", "role")))
    digest = receipt.get("sha256")
    if not metadata_matches or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        return False
    if not check_bytes:
        return True
    try:
        original = Path(material.get("local_path") or "")
        if not original.is_file() or original.stat().st_size > 256 * 1024 * 1024:
            return False
        observed = hashlib.sha256()
        consumed = 0
        with original.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                consumed += len(chunk)
                if consumed > 256 * 1024 * 1024:
                    return False
                observed.update(chunk)
        return observed.hexdigest() == digest.lower()
    except (OSError, ValueError, TypeError):
        return False


class DetailChecks:
    def __init__(self, requirements: tuple[dict, ...], *, max_chars: int = 500000):
        self.requirements = {item["check_id"]: deepcopy(item) for item in requirements}
        self.max_chars = max_chars
        self.retained_chars = 0
        self.fragments: dict[str, dict] = {}
        self.passages: dict[str, dict] = {}
        self.submissions: dict[str, dict] = {}

    def capture(self, receipt: dict, fragments: list[dict]) -> None:
        if not self.requirements:
            return
        pages = [{"page": item.get("page"), "page_char_offset": item.get("page_char_offset", 0),
                  "returned_chars": len(item["text"]), "text": normalized_quote(item["text"])} for item in fragments]
        chars = sum(len(page["text"]) for page in pages)
        if self.retained_chars + chars > self.max_chars:
            raise ResearchError("RESEARCH_DETAIL_CONTEXT_LIMIT", "Critical-detail text budget reached; use a new host task or smaller readings")
        self.fragments[receipt["read_id"]] = {"receipt": deepcopy(receipt), "pages": pages}
        for page_index, page in enumerate(pages):
            for index, start in enumerate(range(0,len(page["text"]),512)):
                passage_id = f"{receipt['read_id']}.{page_index}.{index}"
                self.passages[passage_id] = {"read_id":receipt["read_id"],"page":page["page"],
                    "normalized_start":start,"normalized_end":min(start+512,len(page["text"])),
                    "quote":page["text"][start:start+512]}
        self.retained_chars += chars

    def passage_navigation(self, read_id):
        return [{"passage_id":key,**{k:v for k,v in item.items() if k != "quote"},"preview":item["quote"][:96]}
                for key,item in self.passages.items() if item["read_id"] == read_id]

    def _requirement(self, check_id: str) -> dict:
        try:
            return self.requirements[check_id]
        except (KeyError, TypeError):
            raise ResearchError("DETAIL_CHECK_NOT_FOUND", "Use a host-selected critical check from this research session") from None

    @staticmethod
    def _answer(answer):
        if isinstance(answer, str):
            valid = bool(answer.strip()) and len(answer) <= 6000
        elif isinstance(answer, dict):
            valid = (bool(answer) and not set(answer) - ANSWER_FIELDS and "summary" in answer
                     and all(isinstance(value, str) and value.strip() and len(value) <= 2000 for value in answer.values())
                     and sum(len(value) for value in answer.values()) <= 6000)
        else:
            valid = False
        if not valid:
            raise ResearchError("INVALID_ARGUMENT", "A detail answer must be nonempty text or a bounded interpretation object with summary")
        return deepcopy(answer)

    def _anchor(self, requirement: dict, anchor: dict, catalog: dict) -> dict:
        if isinstance(anchor,dict) and set(anchor) == {"passage_id"}:
            passage = self.passages.get(anchor["passage_id"]) if isinstance(anchor["passage_id"],str) else None
            if passage is None:
                raise ResearchError("DETAIL_QUOTE_NOT_READ", "Choose a passage_id from this session's actual read response")
            return {**self._anchor(requirement,{"read_id":passage["read_id"],"quote":passage["quote"]},catalog),
                    "passage_id":anchor["passage_id"]}
        if (not isinstance(anchor, dict) or set(anchor) != {"read_id", "quote"}
                or not isinstance(anchor["read_id"], str) or not isinstance(anchor["quote"], str)
                or not anchor["quote"].strip() or len(anchor["quote"]) > 2000):
            raise ResearchError("INVALID_ARGUMENT", "Anchors require a read_id and a nonempty exact quote up to 2000 characters")
        fragment = self.fragments.get(anchor["read_id"])
        if fragment is None:
            raise ResearchError("DETAIL_QUOTE_NOT_READ", "Anchor text must come from an actual text response in this session")
        receipt = fragment["receipt"]
        match = catalog.get(receipt["material_id"])
        if match is None or not same_source(receipt, match[0]):
            raise ResearchError("DETAIL_ANCHOR_STALE", "The anchored material identity, version, hash, role or collection scope changed")
        material, aliases = match
        if requirement["paper_id"] not in [*aliases, material.get("paper_id")]:
            raise ResearchError("DETAIL_WRONG_PAPER", "Anchor is not from the paper selected for this critical check")
        if requirement.get("required_role") and material.get("role") != requirement["required_role"]:
            raise ResearchError("DETAIL_WRONG_ROLE", "Anchor material role does not match the critical check")
        if not isinstance(receipt.get("sha256"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", receipt["sha256"]):
            raise ResearchError("DETAIL_UNVERIFIED_SOURCE", "Quote anchors require a content-hashed original material")
        quote = normalized_quote(anchor["quote"])
        for page in fragment["pages"]:
            offset = page["text"].find(quote)
            if offset >= 0:
                return {"read_id": receipt["read_id"], "quote": quote, "material_id": receipt["material_id"],
                        "paper_id": receipt.get("paper_id"), "role": receipt.get("role"),
                        "version_id": receipt.get("version_id"), "sha256": receipt["sha256"], "page": page["page"],
                        "source_range": {"page_char_offset": page["page_char_offset"], "returned_chars": page["returned_chars"]},
                        "normalized_quote_offset": offset, "offset_basis": "NFKC_and_whitespace_normalized_returned_page_fragment"}
        raise ResearchError("DETAIL_QUOTE_NOT_READ", "Exact normalized quote is absent from the text actually returned by this read_id")

    def submit(self, check_id: str, answer, anchors, status: str, catalog: dict) -> dict:
        requirement = self._requirement(check_id)
        answer = self._answer(answer)
        if (status not in ("supported", "unresolved") or not isinstance(anchors, list) or len(anchors) > 8
                or (status == "supported" and not anchors) or (status == "unresolved" and anchors)):
            raise ResearchError("INVALID_ARGUMENT", "Supported submissions need 1..8 observed quote anchors; unresolved submissions use an empty anchor list")
        verified = [self._anchor(requirement, anchor, catalog) for anchor in anchors]
        old = self.submissions.get(check_id)
        revision = old["revision"] + 1 if old else 1
        self.submissions[check_id] = {"answer": answer, "anchors": verified, "status": status,
                                      "revision": revision, "review": None}
        return {"check_id": check_id, "revision": revision, "submission_status": status,
                "status": "pending_review" if status == "supported" else "unresolved", "review": None}

    def _validated_submission(self, check_id: str, catalog: dict) -> tuple[dict, dict]:
        requirement = self._requirement(check_id)
        submission = self.submissions.get(check_id)
        if submission is None:
            raise ResearchError("DETAIL_NOT_SUBMITTED", "Submit a critical-detail interpretation before requesting independent review")
        try:
            for anchor in submission["anchors"]:
                # Recheck both the observed text and the live source at review/check time.
                self._anchor(requirement, {key: anchor[key] for key in ("read_id", "quote")}, catalog)
        except ResearchError as error:
            if error.code in ("DETAIL_ANCHOR_STALE", "DETAIL_WRONG_PAPER", "DETAIL_WRONG_ROLE"):
                submission["review"] = None
            raise
        return requirement, submission

    def packet(self, session_id: str, check_id: str, catalog: dict) -> dict:
        requirement, submission = self._validated_submission(check_id, catalog)
        anchors = deepcopy(submission["anchors"])
        for anchor in anchors:
            fragment = self.fragments[anchor["read_id"]]
            # Context is deliberately confined to the already delivered page
            # fragment. A nearby passage in the current PDF is not evidence
            # that the agent (or this reviewer) actually received that text.
            page = next(item for item in fragment["pages"]
                        if item["page"] == anchor["page"]
                        and item["text"].find(anchor["quote"]) == anchor["normalized_quote_offset"])
            text, offset = page["text"], anchor["normalized_quote_offset"]
            start = max(0, min(offset - 400, len(text) - 1200))
            end = min(len(text), start + 1200)
            material = catalog[anchor["material_id"]][0]
            count = material.get("details", {}).get("parsed_pages")
            pagination = material.get("details", {}).get("pagination", {})
            logical = pagination.get("kind") == "logical_sections" or pagination.get("physical_pages") is False
            physical_page = anchor["page"]
            navigation = {"previous_page": None, "next_page": None,
                          "notice": "Navigation only: neighboring logical XML sections require a separate text read, not physical-page evidence." if logical else "Navigation only: neighboring pages require a separate original-material read."}
            if isinstance(physical_page, int) and not isinstance(physical_page, bool):
                navigation["previous_page"] = physical_page - 1 if physical_page > 1 else None
                if isinstance(count, int) and not isinstance(count, bool) and physical_page < count:
                    navigation["next_page"] = physical_page + 1
            anchor["packet_context"] = {
                "pagination": {"kind": "logical_sections" if logical else "physical_pages", "physical_pages": not logical},
                "text": text[start:end], "normalized_start": start, "normalized_end": end,
                "returned_fragment_chars": len(text),
                "quote_complete": offset >= start and offset + len(anchor["quote"]) <= end,
                "basis": "NFKC_and_whitespace_normalized_actually_returned_page_fragment",
                "neighboring_pages": navigation}
        return {"session_id": session_id, **deepcopy(requirement), "answer": deepcopy(submission["answer"]),
                "status": submission["status"], "revision": submission["revision"],
                "anchors": anchors,
                "notice": "Trusted reviewer packet includes original quotes. Exact matching proves access, not scientific support; inspect conditions, units and omitted context."}

    def review(self, check_id: str, *, verdict: str, reviewer: str, reason: str,
               expected_revision: int, catalog: dict) -> dict:
        _, submission = self._validated_submission(check_id, catalog)
        if (verdict not in ("supported", "contradicted", "needs_context")
                or not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 200
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000
                or not isinstance(expected_revision, int) or isinstance(expected_revision, bool)):
            raise ResearchError("INVALID_ARGUMENT", "Independent review requires a verdict, reviewer, reason and exact integer revision")
        if expected_revision != submission["revision"]:
            raise ResearchError("DETAIL_REVISION_CHANGED", "Interpretation changed after this review packet; review the current revision")
        if verdict == "supported" and submission["status"] != "supported":
            raise ResearchError("DETAIL_UNRESOLVED", "An unresolved interpretation cannot be accepted as supported")
        submission["review"] = {"verdict": verdict, "reviewer": reviewer, "reason": reason,
                                 "revision": expected_revision}
        return {"check_id": check_id, "revision": expected_revision, "review": deepcopy(submission["review"])}

    def summary(self, catalog: dict) -> dict:
        checks, gaps = [], []
        for check_id, requirement in self.requirements.items():
            submission = self.submissions.get(check_id)
            status = "missing"
            if submission is not None:
                try:
                    self._validated_submission(check_id, catalog)
                    status = ("unresolved" if submission["status"] == "unresolved" else
                              submission["review"]["verdict"] if submission["review"] else "pending_review")
                except ResearchError as error:
                    if error.code not in ("DETAIL_ANCHOR_STALE", "DETAIL_WRONG_PAPER", "DETAIL_WRONG_ROLE"):
                        raise
                    status = "stale_anchor"
            checks.append({**deepcopy(requirement), "status": status,
                           "revision": submission["revision"] if submission else 0,
                           "submission_status": submission["status"] if submission else None,
                           "review": deepcopy(submission["review"]) if submission else None})
            if status != "supported":
                gaps.append({"check_id": check_id, "reason": status})
        return {"detail_status": "not_required" if not checks else "needs_review" if gaps else "supported",
                "critical_checks": checks, "detail_gaps": gaps,
                "all_details_supported": bool(checks) and not gaps}
