# 研究知识接口验证

本目录提供本轮验证的[精简结果](summary.json)、[单细胞独立复核记录](single-cell-review.json)和[修订方案](single-cell-reviewed-design.md)。完整过程和限制见[验证说明](../../docs/research-usage-validation.md)。

RAG 已获片段传输授权并完成在线验证：[独立复核记录](rag-review.json)和[修订方案](rag-reviewed-design.md)保留实际读取与科学修订的区别。

这里的读取次数区分宿主流程验证与科研模型实际调用。材料可访问、所选范围读取完整和科学理解正确是不同问题，结果不能互相替代。

运行 `scripts/validate_research_usage.py` 需要执行机器上已有两个真实集合和上一轮的原件定位文件。公开仓库不包含这些原件；新用户可先按快速开始建立自己的文献库，再通过研究知识接口使用自己的集合。`--live-model` 会调用配置的外部模型，须确保选定材料的传输范围已获授权；默认模式仅做本地流程验证。
