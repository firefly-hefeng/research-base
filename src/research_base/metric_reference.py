"""Small mathematical references; input labels are not inferred paper facts."""
from .models import ResearchError


def metric_reference(kind="binary_classification", *, positive_label="positive", negative_label="negative",
                     predicted_positive_action="predict positive", predicted_negative_action="predict negative", trials=None):
    notice = "Mathematical reference only. Verify the paper's actual labels, actions, unit and aggregation from its original; this is not reading credit."
    if kind == "binary_classification":
        values=(positive_label,negative_label,predicted_positive_action,predicted_negative_action)
        if any(not isinstance(v,str) or not 1 <= len(v.strip()) <= 200 for v in values) or positive_label == negative_label:
            raise ResearchError("INVALID_ARGUMENT","Use distinct, bounded positive and negative labels")
        return {"kind":kind,"categories":{
            "TP":{"actual":positive_label,"predicted":positive_label,"action":predicted_positive_action},
            "FP":{"actual":negative_label,"predicted":positive_label,"action":predicted_positive_action},
            "FN":{"actual":positive_label,"predicted":negative_label,"action":predicted_negative_action},
            "TN":{"actual":negative_label,"predicted":negative_label,"action":predicted_negative_action}},
            "precision":"TP / (TP + FP)","recall":"TP / (TP + FN)",
            "zero_denominator":"undefined; choose and declare an explicit evaluation convention", "notice":notice}
    if kind == "joint_binary":
        if (not isinstance(trials,list) or not 1 <= len(trials) <= 1000
                or any(not isinstance(t,dict) or set(t) != {"answer_correct","evidence_correct"}
                       or any(not isinstance(v,bool) for v in t.values()) for t in trials)):
            raise ResearchError("INVALID_ARGUMENT","Joint metrics require 1..1000 actual boolean pairs")
        n=len(trials)
        answer=sum(t["answer_correct"] for t in trials)/n
        evidence=sum(t["evidence_correct"] for t in trials)/n
        joint=sum(t["answer_correct"] and t["evidence_correct"] for t in trials)/n
        return {"kind":kind,"instances":n,"answer_rate":answer,"evidence_rate":evidence,"joint_rate":joint,
                "product_of_marginal_rates":answer*evidence,
                "per_instance_identity":"I(answer AND evidence) = I(answer) * I(evidence)",
                "aggregate_notice":"Mean of products generally differs from product of means; no independence is assumed.","notice":notice}
    raise ResearchError("INVALID_ARGUMENT","Unknown metric reference kind")
