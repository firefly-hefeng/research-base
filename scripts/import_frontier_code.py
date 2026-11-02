"""Import pinned author-released code as code/supplement, never as paper text."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_base.core import ResearchBase
from research_base.storage import now

def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".research-base/frontier-refresh/bio-code-originals.json").read_text(encoding="utf-8"))
    result = {"imported_at": now(), "code_executed": False, "manuscript_fulltext_claimed": False, "works": []}
    with ResearchBase(root / ".research-base/library") as base:
        for work in manifest["works"]:
            directory = (root / work["local_directory"]).resolve()
            if not directory.is_relative_to(root):
                raise ValueError("Repository is outside this workspace")
            actual = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            if actual != work["commit"]:
                raise ValueError("Repository commit differs from the verified manifest")
            with base.store.connect() as db:
                row = db.execute("SELECT paper_id FROM identifiers WHERE namespace='doi' AND value=?", (work["doi"],)).fetchone()
            if not row:
                raise ValueError("Verified paper must already exist")
            paper = base.get_paper(row[0])
            saved = {"paper_id": row[0], "title": paper["title"], "doi": work["doi"],
                     "commit": actual, "repository": work["repository"], "materials": []}
            for item in work["files"]:
                source = (directory / item["path"]).resolve()
                if not source.is_relative_to(directory) or not source.is_file():
                    raise ValueError("Code file is missing or outside the verified checkout")
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                existing = next((m for m in base.list_materials(paper_id=row[0]) if m.get("sha256") == digest and m["role"] == item["role"]), None)
                material = existing or base.import_material(row[0], path=source, role=item["role"],
                    version_id=paper["preferred_version_id"], confirmed=True)
                link = base.import_material(row[0], url=item["url"], role=item["role"], version_id=paper["preferred_version_id"])
                saved["materials"].append({"material_id": material["material_id"], "external_entry_id": link["material_id"],
                    "source_url": item["url"], "repository_path": item["path"], "role": item["role"],
                    "sha256": digest, "format": material["format"], "status": material["status"], "purpose": item["purpose"]})
            result["works"].append(saved)
    output = root / ".research-base/frontier-refresh/bio/code-materials.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"works": len(result["works"]), "materials": sum(len(w["materials"]) for w in result["works"]),
                      "code_executed": False, "manuscript_fulltext_claimed": False}))

if __name__ == "__main__":
    main()
