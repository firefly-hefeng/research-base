"""Inspect provider counts before committing a bounded frontier refresh plan."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.models import ResearchError
from research_base.sources import SourceRegistry

QUERIES = {
    "rag": {
        "arxiv": '(ti:"retrieval augmented generation" OR abs:"retrieval augmented generation" OR ti:RAG) AND (ti:evaluation OR ti:benchmark OR ti:factuality OR ti:calibration OR ti:faithfulness OR ti:reranking)',
        "openalex": '"retrieval augmented generation" AND (evaluation OR benchmark OR factuality OR calibration OR faithfulness)',
    },
    "bio": {
        "europe_pmc": '(TITLE_ABS:"single-cell" OR TITLE_ABS:"single cell") AND (TITLE_ABS:perturbation OR TITLE_ABS:perturbations) AND (TITLE_ABS:prediction OR TITLE_ABS:predicting OR TITLE_ABS:benchmark OR TITLE_ABS:generalization)',
        "pubmed": '("single-cell"[Title/Abstract] OR "single cell"[Title/Abstract]) AND perturbation[Title/Abstract] AND (prediction[Title/Abstract] OR predicting[Title/Abstract] OR benchmark[Title/Abstract] OR generalization[Title/Abstract])',
        "openalex": '("single-cell" OR "single cell") AND perturbation AND (prediction OR benchmark OR generalization)',
    },
}

def main():
    root = Path(__file__).resolve().parents[1] / ".research-base/frontier-refresh"
    root.mkdir(parents=True, exist_ok=True)
    registry = SourceRegistry()
    records = []
    try:
        for case, queries in QUERIES.items():
            for source, query in queries.items():
                row = {"case": case, "source": source, "query": query,
                       "date_from": "2025-01-01", "date_to": "2026-11-03"}
                try:
                    page = registry.search_page(source, query, page_size=50,
                        date_from=row["date_from"], date_to=row["date_to"])
                    row.update(total=page.total, next_cursor=page.next_cursor,
                               items=[item.model_dump() for item in page.items])
                    print(json.dumps({"case": case, "source": source, "total": page.total,
                                      "first_page": len(page.items)}, ensure_ascii=True), flush=True)
                except ResearchError as error:
                    row["error"] = error.to_dict()
                    print(json.dumps({"case": case, "source": source, "error": error.code}), flush=True)
                records.append(row)
                (root / "query-probes.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        registry.close()

if __name__ == "__main__":
    main()
