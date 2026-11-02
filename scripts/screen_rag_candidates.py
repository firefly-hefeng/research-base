"""Rebuild the round-3 RAG metadata screening artifact from its frozen pool.

Decisions are an agent's qualitative title/abstract review, not a relevance
classifier or an assertion that the papers were read in full. No library writes.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


DECISIONS = {
    "paper_daac795e6be349e39b88": ("relevant-context", "摘要描述合成persona应用，虽有BM25/稠密/混合对照和多种证据指标，但主要任务并非通用RAG评估。", "作为跨任务外部有效性和多指标不等价的例子；不直接迁移其效果数值。"),
    "paper_b17eaa4df813410c9a41": ("relevant-context", "与实验室规程/FAQ场景接近，但摘要明确是叙述综述，其提出的机构评估协议尚未验证。", "补充时效、拒答和授权边界检查，作为背景而非已验证评估器。"),
    "paper_b6446c8dad5e4273897a": ("included", "摘要含lookup与synthesis任务分开对照，并区分检索发现、选择与证据准入，直接关系评估分层。", "核对任务条件交叉及实体、时间、来源角色、矛盾的准入控制，避免把检索相似度当答案证据。"),
    "paper_dedee06200094e9ca84b": ("relevant-context", "摘要为大范围RAG聊天系统综述，含评估分类，但未提供可直接核对的特定评测方法。", "查漏评估维度与相关工作，不作为新性能结论来源。"),
    "paper_af1c6836c98a43b69840": ("uncertain", "只有题名，未有摘要；不能判断其超越文本准确性的度量能否迁移到本任务。", "补摘要或全文后核对是否有可复用的人类任务成效评估。"),
    "paper_0930940fdd964508b9b2": ("exclude", "摘要主要研究双语立法矛盾检测和法律NLP综述，未给出本用例RAG检索/生成评价方法。", "本轮不纳入实验室RAG评估集合。"),
    "paper_f68d561eeb894faaacb1": ("uncertain", "只有MOOC视频QA实现题名，没有摘要，不能仅因routing一词判断评估贡献。", "若研究范围扩展到视频或时间戳来源追踪，再补全文判断。"),
    "paper_d50b21da59774cd39e69": ("uncertain", "只有工程变更多模态检索题名，缺少摘要和可比较任务细节。", "先补摘要；不据题名直接当作多跳评价基线。"),
    "paper_31a486335e3443489ac5": ("relevant-context", "摘要明确为denoising观点/分类文章，直接讨论注意力预算和噪声，但不提供本用例的独立实证评估协议。", "检查噪声和上下文证据密度的假设，作为综述背景。"),
    "paper_a8ac73e3f93e48979bc4": ("exclude", "摘要为数数任务的心理测量与Poisson/Rasch再分析，与RAG评价任务不相符。", "本轮不纳入；引用连接不代表主题相关。"),
    "paper_c85b2d597e454aac8642": ("uncertain", "只有策略约束RAG题名，没有摘要，无法判断其评估是否可复用。", "补全文后看逐字引用与受控综合的测试定义及拒答边界。"),
    "paper_9e5597c3ba2040c08d73": ("included", "摘要明确比较词法、语义、混合、oracle检索，以相同question-report对拆分检索质量与模型能力，直接匹配研究问题。", "核对配对设计、oracle条件、模型×检索交互与专家复核，补到实验设计中。"),
    "paper_c45fb150537a41f49222": ("exclude", "主要贡献为临床术语替换错误检测中的多agent辩论系统，任务与通用RAG忠实性/检索组件对照不同。", "本轮不引入临床检测排名或专用训练条件。"),
    "paper_a8516e5c1109424e8cfb": ("relevant-context", "摘要关注金融情感分析解释的持久化与多方法XAI，RAG评测只是应用中的解释检查。", "可借鉴方法归属/引用错误类型；不当作本用例主基线。"),
    "paper_c09e0e2ba29143b695fd": ("uncertain", "题名直接涉及RAG多数据库公平评价，但摘要缺失，尚不清楚控制变量和数据构造。", "高优先补摘要或全文，核对跨数据库可比性后再决定方法纳入。"),
    "paper_154c78388a324a349c3a": ("uncertain", "只有SOC/LLM数据验证题名，未提供摘要，相关性不足以判定。", "补元数据；本轮不从SOC缩写推断具体任务或方法。"),
    "paper_4285243a1053420bacd4": ("included", "虽为安全运营应用，摘要有component ablation、检索覆盖、逐claim验证与300条人工注释可靠性研究，直接补充诊断和评估器校准。", "核对检索改进与claim repair的消融隔离、检查器人工可靠性和actionability定义；不直接迁移SOC数值。"),
    "paper_953895039f044dfaa87d": ("uncertain", "仅一般RAG QA实现题名，缺摘要，不能确认有超出应用实现的评价贡献。", "按需补摘要，未获得证据前不作为评估方法来源。"),
    "paper_a6c856f65a604feb9827": ("included", "摘要以领域QA常见错误类型设计多agent评估，并有公开benchmark，直接补充通用judge与领域错误的适配问题。", "核对错误类型、点对点对齐、标注与跨领域可迁移条件，设计领域judge抽查。"),
    "paper_ffc10e3089704908a727": ("uncertain", "只有数字图书馆语义搜索框架题名，缺少摘要和评价设计信息。", "若补全文证明检索诊断或可比评测贡献，再纳入。"),
    "paper_e64ba4654d8542bf8bae": ("included", "摘要明确研究top-k中的干扰内容、上下文排列和LLM注意力，直接对应原计划噪声及排序扰动检查。", "核对位置、噪声混入和信息增强是否各自独立消融，防止把预处理变化归因为检索器变化。"),
    "paper_18095a425b484a1c9080": ("uncertain", "只有反谣言事实修正系统题名，未有摘要，不足以判断与RAG诊断协议的关系。", "先补摘要；不把事实核验应用直接并入通用评测对照。"),
    "paper_31d07c2854f043cfbef8": ("uncertain", "题名直接为组织RAG综合评价，但没有摘要，不能推断其维度及验证强度。", "高优先补元数据/全文，看是否补充机构文档场景的任务成效和成本定义。"),
    "paper_e03f4b7618164490b748": ("uncertain", "题名涉及检索器升级的query-level兼容性，直接相关，但缺摘要不能据题名采用新指标。", "高优先补全文，核对升级后回归/翻转定义及对照是否适用。"),
    "paper_97ed9695cf374d168918": ("included", "摘要直接用科学文献QA、固定最终证据预算、段落身份和检索/生成分开指标，任务接近实验室论文库。", "核对QASPER/PeerQA划分、paragraph Evidence F1、证据身份保持与bootstrap，补充相同预算下证据恢复检查。"),
    "paper_26400c6be13946858cf3": ("relevant-context", "摘要提出多轮对话RAG图和历史上下文建模，重点任务超出当前单轮检索器对照。", "若扩展多轮，使用主题漂移和长历史分层；当前不混入单轮主比较。"),
    "paper_3ae43ae55d714215a319": ("exclude", "摘要主要是无人机边缘计算网络低碳优化和强化学习，RAG为优化建模应用，不是本任务评估方法。", "本轮不纳入。"),
    "paper_8a3e2bfc77fb4f0b84a7": ("included", "摘要直接联合调度与每query RAG配置适配来平衡质量和时延，补充原计划单一固定k/预算的成本视角。", "核对调度与配置各自消融、质量保持标准和latency定义，补充质量-时延曲线而非单总分排名。"),
    "paper_bc2d459249ed46a292ff": ("included", "摘要提出定制评价维度及基线/上界参考集合，直接相关于领域指标可比性与judge校准。", "核对bounds和reference sets构造、人工标签依赖与稳定性评估，避免把LLM打分等同人类真值。"),
    "paper_0e34278bc2fc4aaa9d56": ("relevant-context", "摘要为通用RAG原则和前沿综述，无本任务可执行评价细节，不能单凭标题的breakthroughs提高优先级。", "背景查漏，不作实证或性能结论来源。"),
    "paper_827c04bd4a3b4ad6b17c": ("included", "摘要同时比较Dense/MMR/Hybrid、chunking、生成模型，区分LLM生成和人工QA集，并分段测延迟，直接涉及控制变量和评估数据偏差。", "核对因子设计是否解耦、两种QA集的独立性和分段延迟，作为实验室FAQ外部场景。"),
    "paper_b07902b1b4674d76abff": ("included", "摘要明确以条件共形框架校准RAG subclaims及跨子域覆盖，直接补充平均faithfulness之外的可靠性界限。", "全文核对exchangeability/分组校准、无真值说法的实际标注依赖及claim保留率-覆盖率权衡；未核对前不宣称统计保证适用本实验。"),
    "paper_f27c881cf88146d0ac75": ("exclude", "摘要主要是MR视觉/语音交互与车载手册多模态检索，当前文本实验不具备其输入模态和任务条件。", "本轮不纳入主评估；将来另设多模态任务才重审。"),
    "paper_35ef2d0312dd4bfd873f": ("included", "摘要系统回顾63篇RAG评价工作，明确自动judge与人类评价关系、各组件与数据集设计，直接回应单一LLM总分是否可信。", "对照原文的评价分类、人工复核局限和数据构造建议，用于确认原计划覆盖及剩余缺口。"),
    "paper_bd58f85310354ff7a7cd": ("relevant-context", "摘要为XR工业知识助手，含chunking/embedding/vector store比较但主要是早期应用演示。", "可作部署和人类任务成效背景，不能把工业效果迁移到实验室文本指标。"),
    "paper_456db2ca24cb4a08aa09": ("included", "摘要含SQuAD/HotpotQA及制造语料、检索与生成分开度量，并指出多跳问题受益，直接支持任务分层对照。", "核对图构建、chunk/top-k消融及多跳数据的证据标签，决定是否增加结构检索扩展组。"),
    "paper_6c92ea20841148f083ad": ("relevant-context", "摘要为公共交通专用混合树图RAG，评价以领域精度和满意度为主，未给出通用诊断贡献。", "仅作为领域外部有效性候选，不把其报告的百分比当作本任务预期提升。"),
    "paper_c84503e8f3f246bfaaf1": ("included", "摘要使用相同语料的德英双语QA对照，直接比较混合与纯向量检索，适合检验语言和专业术语对方法结论的影响。", "核对平行文本/QA构造和语言控制，补充实验室中英术语分层，而不是假设英语结论通用。"),
    "paper_40dfc6bb9eac464aa98f": ("uncertain", "题名直接为ensemble LLM judges对齐评价，缺摘要；作者仓储入口存在，但未读原件。", "高优先补全文，核对ensemble与人工对齐证据，未读前不采用其方法细节。"),
}


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / ".research-base/round3"
    pool = json.loads((folder / "rag-pool.json").read_text(encoding="utf-8"))
    assert {p["paper_id"] for p in pool["papers"]} == set(DECISIONS)
    rows = []
    for paper in pool["papers"]:
        decision, reason, use = DECISIONS[paper["paper_id"]]
        rows.append({k: paper.get(k) for k in ("paper_id", "title", "doi", "year", "abstract", "sources", "graph_hits", "in_original_included", "in_current_broad", "new_to_round2_collection")} | {
            "decision": decision, "reason": reason, "research_use": use,
            "screening_basis": "provider_title_and_abstract" if paper.get("abstract") else "provider_title_only",
            "fulltext_reviewed_for_this_screen": False,
            "quality_status": "not_independently_assessed; bibliographic metadata is not a quality guarantee",
        })
    priority_uncertain = {"paper_c09e0e2ba29143b695fd", "paper_e03f4b7618164490b748", "paper_31d07c2854f043cfbef8", "paper_40dfc6bb9eac464aa98f"}
    report = {
        "case_id": "rag-evaluation", "screened_at": "2026-11-02", "stage": "metadata_relevance_screen; fulltext_review_pending",
        "research_task": "为实验室RAG系统设计可诊断评估，分开衡量检索、生成忠实性、覆盖、信息需求和成本。",
        "original_collection_id": pool["original_collection_id"], "citation_job_id": pool["graph_job_id"],
        "round2_discovery_collection_id": pool["round2_collection_id"],
        "method": "Agent逐条读取库内真实标题和可用摘要，按研究用途定性筛选；未使用关键词自动分数或可用PDF优先替代相关性。",
        "decision_meanings": {"included": "建议纳入用于获取和全文核对；不表示已读或结论获验证", "relevant-context": "保留背景用途，不纳入本轮主方法阅读集", "exclude": "与当前文本RAG诊断任务偏离", "uncertain": "信息不足，暂不纳入；保留补查入口"},
        "scope_counts": {"all_unique_graph_candidates": len(rows), "new_to_round2_collection": sum(p["new_to_round2_collection"] for p in rows), "already_in_current_broad": sum(p["in_current_broad"] for p in rows), "already_in_original_included": sum(p["in_original_included"] for p in rows)},
        "counts": dict(Counter(p["decision"] for p in rows)),
        "new_only_counts": dict(Counter(p["decision"] for p in rows if p["new_to_round2_collection"])),
        "recommended_include_ids": [p["paper_id"] for p in rows if p["decision"] == "included"],
        "recommended_new_only_include_ids": [p["paper_id"] for p in rows if p["decision"] == "included" and p["new_to_round2_collection"]],
        "high_priority_metadata_or_fulltext_review_ids": [p["paper_id"] for p in rows if p["paper_id"] in priority_uncertain],
        "library_modified": False,
        "papers": rows,
    }
    path = folder / "rag-screening.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"path": str(path), "scope_counts": report["scope_counts"], "counts": report["counts"], "new_only_counts": report["new_only_counts"], "recommended_include_ids": report["recommended_include_ids"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
