# 两个真实科研用例的公开验证摘要

本目录仅提供简短书目、相关性筛选决定、实际阅读范围和经独立复核的研究方案。PDF、完整摘要、作者代码说明副本、模型密钥、浏览器响应、本机路径与原始库不在公开仓库中。

- [汇总结果](summary.json)：区分书目记录、经明确关系去重的工作组、可读全文与严格获取任务状态。
- [RAG 复核方案](rag-evaluation/reviewed-plan.md)、[具体帮助和边界](rag-evaluation/benefits.md)、[实际读取](rag-evaluation/reading-log.json)、[筛选](rag-evaluation/screening.json)。
- [单细胞复核方案](single-cell-perturbation/reviewed-plan.md)、[具体帮助和边界](single-cell-perturbation/benefits.md)、[实际读取](single-cell-perturbation/reading-log.json)、[筛选](single-cell-perturbation/screening.json)。

论文仍需从 DOI、仓储或机构会话取得。书目 paper_id/material_id 是这次本地运行的稳定编号，新库会生成自己的编号，应按 DOI/仓储标识关联。筛选是元数据相关性判断，纳入不代表质量认证或全文已经读完。

方案由实际 DeepSeek 工具读取生成，再由独立 agent 对照原件复核修订。读取日志仅说明工具实际返回的文字；不等于整页或整篇完整阅读。原始模型回答和私有运行日志保留在执行机器，用于与复核稿区分。

复跑可先用 `scripts/run_use_cases.py` 生成本机初始集合，再运行 `scripts/validate_round2.py` 和 `scripts/continue_bio_citations.py`。获取、元数据筛选、阅读定位和 browser handoff 仍需要相应前序结果与宿主能力。`screen_rag_candidates.py` 是这次冻结人工筛选的重建脚本，不是通用自动相关性分类器。真实来源的排序、版本、访问权和当前引用索引会改变，公开计数不是固定测试断言。

这些用例验证资料准备和研究设计纠错，没有执行预测或 RAG 性能基准，没有证明节时百分比或完整学科覆盖。
