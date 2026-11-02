"""Apply independently reviewed identities to existing case collections.

Only identifiers already returned by real sources are selected. Missing leads
remain missing; supplied titles are never fabricated into provider hits.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.core import ResearchBase
from research_base.storage import now

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["rag", "bio"], required=True)
    parser.add_argument("--acquire", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = root / ".research-base/frontier-refresh" / args.case
    folder.mkdir(parents=True, exist_ok=True)
    leads = json.loads((folder.parent / f"{args.case}-independent-discovery.json").read_text(encoding="utf-8"))["papers"]
    leads = leads[:6] if args.case == "rag" else leads
    cid = "col_2e45965895b9424fbe84" if args.case == "rag" else "col_482a3da9f9a4472cb6ae"
    with ResearchBase(root / ".research-base/library") as base:
        records, selected = [], []
        for lead in leads:
            identities = []
            if lead.get("doi"):
                identities.append(("doi", lead["doi"].lower()))
            if lead.get("arxiv"):
                value = re.sub(r"v\d+$", "", lead["arxiv"])
                identities.extend([("arxiv_work", value), ("arxiv", value), ("arxiv", lead["arxiv"]), ("doi", "10.48550/arxiv." + value)])
            with base.store.connect() as db:
                found = {row[0] for namespace, value in identities for row in db.execute(
                    "SELECT paper_id FROM identifiers WHERE namespace=? AND value=?", (namespace, value))}
            record = {key: lead[key] for key in ("title", "doi", "arxiv", "primary_url", "task_relevance", "date", "first_submitted", "latest_arxiv_revision", "publication_month", "reading_caution", "scope_limit") if key in lead}
            if not found:
                # A lead is only a lookup instruction. Bibliographic values come
                # from the live archive response, not from the supplied title.
                candidate = None
                if lead.get("arxiv"):
                    response = base.sources.search_page("arxiv", "id:" + lead["arxiv"], page_size=5)
                    candidate = next((item for item in response.items if re.sub(r"v\d+$", "", item.identifiers.get("arxiv", "")) == re.sub(r"v\d+$", "", lead["arxiv"])), None)
                elif (lead.get("doi") or "").startswith(("10.64898/", "10.1101/")):
                    endpoint = "https://api.biorxiv.org/details/biorxiv/" + lead["doi"]
                    response = base.client.get(endpoint)
                    response.raise_for_status()
                    actual = [item for item in response.json().get("collection", []) if item.get("doi", "").lower() == lead["doi"].lower()]
                    if actual:
                        item = max(actual, key=lambda value: int(value.get("version", 0)))
                        version = str(item["version"])
                        landing = "https://www.biorxiv.org/content/" + item["doi"] + "v" + version
                        candidate = {"source": "biorxiv_details", "source_id": item["doi"] + "v" + version,
                            "title": item["title"], "authors": [author.strip() for author in item.get("authors", "").split(";")],
                            "doi": item["doi"], "year": int(item["date"][:4]), "abstract": item.get("abstract", ""),
                            "landing_url": landing, "fulltext_urls": [landing + ".full.pdf"], "version_kind": "preprint",
                            "extra": {"archive_posted_date": item["date"], "archive_version": version, "metadata_url": endpoint}}
                if candidate is None:
                    record["decision"] = "missing_provider_identity"
                    records.append(record)
                    continue
                found = {base.add_paper(candidate)["paper_id"]}
            if len(found) != 1:
                # A formal DOI and an archive version may be separate stable
                # records. Select the exact formal DOI without title merging.
                with base.store.connect() as db:
                    exact = db.execute("SELECT paper_id FROM identifiers WHERE namespace='doi' AND value=?", (lead.get("doi", ""),)).fetchone()
                if exact and exact[0] in found:
                    record["alternate_archive_records_retained"] = sorted(found - {exact[0]})
                    record["identity_selection"] = "exact_formal_doi; alternative archive identities are not merged"
                    found = {exact[0]}
                else:
                    record.update(decision="identity_review_required", paper_ids=sorted(found))
                    records.append(record)
                    continue
            pid = next(iter(found))
            paper = base.get_paper(pid)
            membership = next((m["status"] for m in paper["memberships"] if m["collection_id"] == cid), "absent")
            if membership == "excluded":
                record.update(decision="preserved_prior_exclusion", paper_id=pid)
                records.append(record)
                continue
            base.set_membership(cid, [pid], "included")
            base.set_tags(pid, ["frontier-refresh-20261103", "fulltext-review-pending"])
            selected.append(pid)
            record.update(decision="included_for_original_review", paper_id=pid, previous_membership=membership,
                          fulltext_available=paper["fulltext_available"], text_searchable=paper["text_searchable"])
            records.append(record)
        anchors = base.get_frontier(cid)["paper_ids"]
        frontier = base.configure_frontier(cid, list(dict.fromkeys([*anchors, *selected]))[:20],
            profile="machine_learning" if args.case == "rag" else "single_cell")
        report = {"case": args.case, "checked_at": now(), "collection_id": cid, "records": records,
                  "selected_paper_ids": selected, "frontier": frontier}
        path = folder / "selection.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"case": args.case, "selected": len(selected), "missing": sum(r["decision"] == "missing_provider_identity" for r in records),
                          "freshness": frontier["freshness"]}, ensure_ascii=True), flush=True)
        if args.acquire:
            old_path = folder / "acquisition.json"
            if old_path.exists():
                job = base.get_job(json.loads(old_path.read_text(encoding="utf-8"))["job_id"])
            else:
                job = base.acquire_papers(cid, selected, request_id="frontier-refresh-20261103")
            job = base.run_worker(job["job_id"])
            old_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"case": args.case, "acquisition": job["task_count"]}, ensure_ascii=True), flush=True)

if __name__ == "__main__":
    main()
