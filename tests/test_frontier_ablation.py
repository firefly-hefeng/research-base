"""Evaluation must count actual accepted drafts, including budget-limited ones."""
import importlib.util
from pathlib import Path
import sys

scripts = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location("frontier_ablation", scripts / "compare_frontier_injection.py")
ablation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ablation)


def test_strict_delivery_uses_trusted_final_acceptance_instead_of_status_label():
    limited = {"arm": "frontier_strict", "result": {"status": "completed_with_limits",
        "research_usage": {"enforcement": {"strict_research_completed": True}}}}
    assert ablation.delivered_plan(limited)
    limited["result"]["research_usage"]["enforcement"]["strict_research_completed"] = False
    assert not ablation.delivered_plan(limited)
    assert not ablation.delivered_plan({"arm": "frontier_strict", "result": {"status": "completed"}})
    assert ablation.delivered_plan({"arm": "frontier_reading", "result": {"status": "completed"}})
