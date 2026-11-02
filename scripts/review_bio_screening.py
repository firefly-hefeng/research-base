"""Apply explicit host review to model metadata suggestions; no library writes."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


OVERRIDES = {
    "paper_98a604b776eb41a6ab5a": ("context", "Prospective perspective, not an implemented model or new performance result.", "Optional falsification-protocol reference; do not use as empirical model-ranking evidence."),
    "paper_e0d70dd8366b45f38dc3": ("context", "Abstract studies perturbation-identity representations/retrieval rather than forward transcriptional response generalization.", "Representation background; verify a response-prediction adapter before treating it as a comparator."),
    "paper_9c555da20620442880ae": ("context", "Review summarizes model boundaries and evidence, without a directly comparable new perturbation predictor.", "Background evaluation checklist; prioritize primary benchmarks for conclusions."),
    "paper_67d3136437c048299ac3": ("context", "Generic scVI representation method; supplied metadata does not establish the requested forward perturbation task.", "Optional latent embedding implementation background, not a perturbation-prediction ranking."),
    "paper_24976218e5ea4115b6d4": ("context", "Measurement-scope review; current task compares predictors on existing RNA-count data rather than new assays.", "Record assay limitations as background without extending the evaluation to new measurement technologies."),
    "paper_8e9d6656c67e43cbb32f": ("context", "Broad pharmacology review; direct benchmark and method papers are available for this task.", "Background on translational evidence requirements, not empirical predictive performance."),
    "paper_d1315b765e1f4e35beed": ("context", "Systematic review is useful for search coverage but is secondary evidence for the planned model comparisons.", "Use to check coverage and bias; read primary task-specific benchmarks before changing the experiment."),
    "paper_591c90cce93a45d887e4": ("context", "Title identifies a broad review; no abstract supplied to establish a directly comparable experiment.", "Historical field overview; primary benchmark methods take priority."),
    "paper_b788ca9378bb4284a3f7": ("context", "UCE abstract reports representations, annotation and atlas transfer, not forward perturbation-response prediction.", "Optional cell representation for an explicitly defined downstream adapter; do not equate embeddings with response prediction."),
    "paper_3778b7a31a824228bd82": ("context", "GenePT abstract evaluates gene properties/cell types rather than the target perturbation-response outputs.", "Possible inexpensive embedding baseline only after a prediction adapter and leakage boundary are specified."),
    "paper_5dbad392b56a4e52bfda": ("context", "Encoding-strategy benchmark metadata does not specify the target held-out perturbation/context task.", "Model-design background; confirm downstream task correspondence before comparison."),
    "paper_038e51be952946bd90e3": ("context", "MrVI analyzes sample heterogeneity and perturbation grouping, not the specified unseen-response forecasting task.", "Background for sample stratification and unit-of-analysis choices."),
    "paper_5ec89b1ba56b456688c8": ("include", "Abstract explicitly predicts dose- and cell-type-dependent chemical perturbation responses.", "Compare scVIDR on held-out cell contexts with dose held fixed or separately varied."),
    "paper_3302d60fd8204af3b447": ("include", "INVAE explicitly addresses cross-cell-type perturbation response mapping in the abstract.", "Verify control-to-perturbed input contract and compare to other cross-context methods on matched splits."),
    "paper_524e7c8cc9d244e599a1": ("include", "PRnet abstract explicitly evaluates novel compounds, pathways and cell lines at single-cell and bulk levels.", "Separate single-cell transcriptome prediction from bulk drug screening, and audit compound/context extrapolation."),
    "paper_5c551f1e2f4c4e17a6ef": ("context", "Likely earlier TFActProfiler report; supplied authors and framework match the included NAR article.", "Retain as possible version fallback; verify official relation before merging or counting independent evidence."),
    "paper_c3d93441e6ea481795f3": ("uncertain", "Two SSRN records have identical title and need identity/version verification before a comparative interpretation.", "Resolve duplicate/version identity and verify full-text evaluation before inclusion."),
    "paper_518c387f9cae4bdb9cf7": ("uncertain", "Two SSRN records have identical title and need identity/version verification before a comparative interpretation.", "Resolve duplicate/version identity and verify full-text evaluation before inclusion."),
}

PRIORITY = [
    "paper_7691f9d9842745e4ad64", "paper_38df55b917474dc18ce2", "paper_3ac2aab34bb9494695a4",
    "paper_dc1edad4cb404d5d9a01", "paper_98bd177324774f90ba1c", "paper_9303a1714f814a71b8dc",
    "paper_d3ad241fc4ca44588465", "paper_8c4faf4a13d44ed0a826", "paper_b7ab84fca5f6485aa5a1",
    "paper_cab0753a25ce48e6a16e", "paper_bc5dfcb929cf4f3eb84d", "paper_a9bbc21992ef48739720",
    "paper_d01c146df3d640e484cb", "paper_b58f1cc7607840f78559", "paper_8163e363cd944e7685a3",
    "paper_dca0caee31d0434f95bd", "paper_01cfe56b04114d0d9fda", "paper_108ac7c23e8d41e481ea",
    "paper_8ce04bf0dea14f4fb905", "paper_c3613a0289a74bb18f23",
]

POSSIBLE_VERSIONS = [
    {"paper_ids": ["paper_7691f9d9842745e4ad64", "paper_e8f4a74130dd4e2f94b5"], "basis": "same normalized title; formal Science Advances record and original bioRxiv record", "verified_relation": False},
    {"paper_ids": ["paper_3ac2aab34bb9494695a4", "paper_dc1edad4cb404d5d9a01"], "basis": "similar calibrated-metrics response title; formal article/preprint requires provider verification", "verified_relation": False},
    {"paper_ids": ["paper_c61998a0b3444436909e", "paper_3320e66abbc34d649547"], "basis": "same MAP framework in abstract; formal article/preprint requires provider verification", "verified_relation": False},
    {"paper_ids": ["paper_e48d455199c24435928c", "paper_5c551f1e2f4c4e17a6ef"], "basis": "same TFActProfiler framework and authors; formal article/preprint requires provider verification", "verified_relation": False},
    {"paper_ids": ["paper_c3d93441e6ea481795f3", "paper_518c387f9cae4bdb9cf7"], "basis": "identical title, different SSRN DOI; requires identity/version resolution", "verified_relation": False},
]

EVALUATION_IDS = {
    "paper_7691f9d9842745e4ad64", "paper_d3ad241fc4ca44588465", "paper_b58f1cc7607840f78559",
    "paper_b81f2d4d6f2243dbace2", "paper_9303a1714f814a71b8dc", "paper_8c4faf4a13d44ed0a826",
    "paper_037837ec4e7c4412a035", "paper_3ac2aab34bb9494695a4", "paper_2d8fbbb47c4c4a73aaf8",
    "paper_108ac7c23e8d41e481ea", "paper_b7ab84fca5f6485aa5a1", "paper_5523731991d94331b76b",
    "paper_050b421949a84d7b9c8a", "paper_cbdf339e89d34974bc65", "paper_a721bda4672c45f0bd93",
    "paper_bb9a67b70619488eb8fc", "paper_98bd177324774f90ba1c", "paper_38df55b917474dc18ce2",
    "paper_33eee60d907e4e6e9617", "paper_b7c4fc1577664e669d36", "paper_a9bbc21992ef48739720",
    "paper_09c50f729aa5415f81f8", "paper_dc1edad4cb404d5d9a01", "paper_e8f4a74130dd4e2f94b5",
    "paper_7be67d5261ea438298aa", "paper_9a4f0cdb28bd4ff986e5",
}
DATA_RESOURCE_IDS = {
    "paper_bd70b175453d402fbca7", "paper_8ce5f51a8fdc4cf3ac96", "paper_7ef5ad54b83d4e78bab4",
    "paper_455e102d3ce045bda964", "paper_d01c146df3d640e484cb", "paper_6ccd088c60454cec85c7",
    "paper_8e6f48e91b2940e7a11b", "paper_d942a65358514fa280e6",
}
ESTABLISHED_COMPARATOR_IDS = {
    "paper_f8407e20f47342a39c62", "paper_6e2584694f944fdb8ed9", "paper_944d408cc59d40018845",
    "paper_420f3408dd554abbadfb", "paper_8163e363cd944e7685a3", "paper_dca0caee31d0434f95bd",
    "paper_b7272cc04a7e4d90ac9f", "paper_c34f067d283349d6b5e1", "paper_01cfe56b04114d0d9fda",
    "paper_c9bd138d4864463f96e3", "paper_be3ea497d9cc4129953b", "paper_df6d1a2f2d0940478cbc",
    "paper_993875049011432f9f3a", "paper_3a27799e15a541388cd4", "paper_531f52acf990427799e8",
}


def main():
    project = Path(__file__).resolve().parents[1]
    source_path = project / ".research-base/round3/bio-expansion.json"
    output = project / ".research-base/round3/bio-screening.json"
    report = json.loads(output.read_text(encoding="utf-8"))
    for paper in report["items"]:
        pid = paper["paper_id"]
        paper["host_review"] = "Metadata screening reviewed against task scope; no full-text claim."
        if pid in OVERRIDES:
            decision, reason, use = OVERRIDES[pid]
            paper.update(decision=decision, reason=reason, research_use=use, host_override=True)
        paper["recommend_include"] = paper["decision"] == "include" and not paper["already_originally_included"]
        paper["priority_read_rank"] = PRIORITY.index(pid) + 1 if pid in PRIORITY else None
        if paper["decision"] == "include":
            paper["fulltext_checks"] = ["Confirm input/output task and held-out unit", "Verify metrics, baselines, preprocessing and test-data boundaries", "Treat abstract claims as provisional; do not report an unrun ranking"]
            paper["category"] = "benchmark_or_evaluation" if pid in EVALUATION_IDS else "data_resource_for_evaluation" if pid in DATA_RESOURCE_IDS else "established_comparator" if pid in ESTABLISHED_COMPARATOR_IDS else "direct_prediction_method"
        else:
            paper["category"] = "context_only" if paper["decision"] == "context" else "identity_or_relevance_unresolved" if paper["decision"] == "uncertain" else "outside_task_scope"
    report["decision_counts"] = dict(Counter(p["decision"] for p in report["items"]))
    report["recommended_include_ids"] = [p["paper_id"] for p in report["items"] if p["recommend_include"]]
    report["recommended_new_citation_include_ids"] = [p["paper_id"] for p in report["items"] if p["recommend_include"] and not p["already_in_broad_or_seed"]]
    report["priority_read_ids"] = [pid for pid in PRIORITY if any(p["paper_id"] == pid and p["decision"] == "include" for p in report["items"])]
    report["possible_version_groups"] = POSSIBLE_VERSIONS
    report["stage"] = "host_reviewed_metadata_screening_pending_fulltext_confirmation"
    report["host_reviewed_at"] = datetime.now(timezone.utc).isoformat()
    report["host_review_summary"] = "All 321 titles/decisions reviewed; borderline scope/representation/review/method records checked against available abstracts. Eighteen explicit overrides distinguish forward prediction from annotation/retrieval, prioritize primary evidence, restore three missed direct predictors, and defer unresolved duplicate SSRN records. This is not a quality certification or full-text review."
    report["source_snapshot_sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    temp = output.with_suffix(".review.tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(output)
    public = {k: report[k] for k in ("case_id", "host_reviewed_at", "screened_count", "new_to_round2_collection_count", "decision_counts", "model", "model_batches", "stage", "host_review_summary", "possible_version_groups", "source_snapshot_sha256")}
    public["notice"] = "Metadata-only relevance screening, not full-text reading or quality certification. This summary omits full abstracts, local paths, material originals and credentials."
    public["items"] = [{k: paper.get(k) for k in ("paper_id", "title", "year", "doi", "decision", "category", "reason", "research_use", "already_in_broad_or_seed", "already_originally_included", "priority_read_rank")} for paper in report["items"]]
    (output.parent / "bio-screening-public.json").write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"screened": report["screened_count"], "decisions": report["decision_counts"], "recommended": len(report["recommended_include_ids"]), "recommended_new_citation": len(report["recommended_new_citation_include_ids"]), "priority_read": len(report["priority_read_ids"])}))


if __name__ == "__main__":
    main()
