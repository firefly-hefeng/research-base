# 前沿更新后的真实用例

- [结果摘要](summary.json)与[完整检索计划](search-plans.json)：来源、时间窗口、分页状态和新鲜度范围。
- [近期参考文献与原件状态](references.json)：选定工作、发表/修订日期、原始材料可用情况。
- [RAG 研究方案](rag-plan.md)与[实际使用记录](rag-review.json)。
- [单细胞研究方案](bio-plan.md)与[实际使用记录](bio-review.json)。

上述方案来自实际工具读取，不是按论文题名生成。完整过程与限制见[本轮验证](../../docs/frontier-refresh-validation.md)。程序已核验声明范围的搜索新鲜度；候选未全部筛完，未声称全领域无遗漏，也未运行科研实验。

论文原件、完整摘要及固定版本的作者代码保留在执行机器，不随公开仓库分发。参考来源和代码链接指向原作者。scripts/refresh_frontier_cases.py 等脚本复用执行机器上的已有集合和定位清单；新克隆项目应先建立自己的库和研究集合。
