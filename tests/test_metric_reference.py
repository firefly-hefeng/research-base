import pytest
from research_base.metric_reference import metric_reference
from research_base.models import ResearchError
from research_base.tools import ToolRegistry


def test_fail_positive_with_exclusion_action_keeps_false_positive_and_negative_distinct():
    result=metric_reference(positive_label="Fail",negative_label="Pass",predicted_positive_action="exclude",predicted_negative_action="retain")
    assert result["categories"]["FP"] == {"actual":"Pass","predicted":"Fail","action":"exclude"}
    assert result["categories"]["FN"] == {"actual":"Fail","predicted":"Pass","action":"retain"}
    assert result["recall"] == "TP / (TP + FN)"


def test_instance_conjunction_product_does_not_license_product_of_aggregate_accuracies():
    result=ToolRegistry(None).call("metric_reference",{"kind":"joint_binary","trials":[
        {"answer_correct":True,"evidence_correct":True},{"answer_correct":False,"evidence_correct":False}]})["data"]
    assert result["joint_rate"] == .5 and result["product_of_marginal_rates"] == .25


def test_metric_reference_does_not_accept_soft_labels_as_boolean_observations():
    with pytest.raises(ResearchError):
        metric_reference("joint_binary",trials=[{"answer_correct":1,"evidence_correct":0}])
