# 前沿检索、新鲜度与研究方案验证（2026-11-03）

两个原集合现已完成明示查询和日期窗口的分页检索，更新前沿参考空间，并由科研 agent 实际读取新增原件修订方案。原先的 never_completed 缺口已在这些匹配计划内解决；这不等于完成全领域系统综述。

## 实际完成的范围

日期窗口为 **2025-01-01 至 2026-11-03**，保留旧集合的基础文献。没有为获得新鲜度通过而改成任意很小的时间窗口。扩大到 2025 是因为 SciRerankBench、FRANQ 等工作在 2025 初投、2026 修订或正式发表；arXiv 的 submittedDate 不能代替修订日期。

| 项目 | RAG 评价 | 单细胞扰动预测 |
| --- | --- | --- |
| 完成来源 | arXiv；七个 ACL Anthology 集合 | Europe PMC；PubMed |
| 来源返回记录 | 636；1,248 | 685；353 |
| 合并后的库内论文 ID | 1,877 | 704 |
| 经主来源核验选定的近期工作 | 6 | 9 |
| 选定工作可用正文 | 6/6 | 2/9 |
| 另取得作者原代码/方法资料 | 本轮未额外取得 | 2 项工作、7 份材料 |
| 更新后关键入口 | 11 | 14 |
| 科研 agent 实际读取 | 7 次、30,370 字符 | 7 次、20,754 字符 |
| 原件使用与匹配检索新鲜度 | 通过 | 通过 |

来源记录有重叠，且保留正式论文与预印本的稳定 ID，所以表中的 ID 数不能直接解释为独立科学研究数。大部分新增检索候选仍待相关性筛选；本轮只纳入经独立主来源核验的重点工作。正式 DOI 与 arXiv 指向不同稳定记录时，选定正式 DOI，其他记录保留，不以标题合并。

完整查询、来源分页状态、完成时间和指纹见[检索计划](../examples/frontier-refresh/search-plans.json)；近期论文及原件状态见[参考文献](../examples/frontier-refresh/references.json)。RAG 的 ACL 集合为 2025/2026 ACL、EMNLP、Findings 及 2026 EACL。ACL 月份范围采用区间交集，不能冒充精确到日的发表日期。

OpenAlex 补充探测遇到限流，未被写成检索完成。旧的宽泛检索、初次误用 Findings 文件名的失败任务及暂停记录均保留，没有删除或改为成功。本轮新鲜度仅对应表中已完成的计划，其他渠道和未索引工作继续作为覆盖限制。

## 新鲜度机制补强

程序同时检查搜索完成时间和覆盖截止日期：今天重跑只截止很早年份的搜索仍返回 coverage_stale。prepare_context 返回实际查询、来源、原生查询和日期窗口；get_paper 与前沿入口可见带来源的 publication_history，区分初投、修订、正式发表及索引时间。

ACL Anthology 新增为第九个可选来源，需显式集合查询，避免普通主题默认调用不支持的查询格式。XML 快照校验续接；缺失、变更和格式异常不会伪造空结果完成。来源 request_count 与持久 job 的分页预算单位口径在[来源说明](sources.md)中区分。

## 科研 agent 实际用了什么

本轮先由在本机文献库上使用工具的 Codex 科研子 agent 执行真实会话：begin_research、原件读取、check_research、require_research_ready(require_current_frontier=True)。这里“本机”描述资料和工具所在位置，不宣称模型离线部署。随后用户明确允许选定必要片段传输，DeepSeek 完成了同样经过宿主检查的实际读取和方案修订；两种执行记录分开保留。

**RAG** 实读 FRESCO 第 3/4/21 页、EBG 第 3/4 页、RARE 第 6/7 页。方案保留原有公平比较与校准控制，新增：查询时点的语义充分和时间有效性、任一证据与全部必要证据的区别、答案/证据联合正确性、冗余等价证据 gold 和覆盖指标。实际方法页改变了实验设计；没有迁移作者的提升数值。见 [RAG 方案](../examples/frontier-refresh/rag-plan.md)与[使用记录](../examples/frontier-refresh/rag-review.json)。

**单细胞** 实读 SafeDelta 使用约定及适配/控制实现、PIE 两份划分配置。方案把零样本和 few-shot 分开，加入 calibration-only 的“适配或保留基线”门控，按独立实验单位拆控制、保留 NA 及完整配对评分，并审计 PIE 的真实训练证据与 split。所读代码固定到作者提交；未执行代码。见 [单细胞方案](../examples/frontier-refresh/bio-plan.md)与[使用记录](../examples/frontier-refresh/bio-review.json)。

SafeDelta、PIE 等预印本的 PDF 仍未取得；没有将站点 403、代码或摘要算作论文全文。PIE 的浏览器 PDF 页导航已执行，但页面检查超时，没有文件交回，任务保留重试状态。正文成功项是 scArchon、VCBench；新版 State 严格目标仍有身份/版本缺口。研究方案不声称读完尚未取得的论文。

## 获授权后的 DeepSeek 实读验证

| 项目 | RAG | 单细胞 |
| --- | --- | --- |
| 实际模型 | deepseek-flash | deepseek-flash |
| 读取范围 | FRESCO 3/4、EBG 3/4、RARE 6/7 物理页 | SafeDelta USAGE/adapter；PIE within/cross-dataset 配置 |
| 实际读取调用 | 6 | 5 |
| 返回字符 | 26,665 | 13,093 |
| 指定页/文件与续读 | 全部返回，续读无缺口 | 全部返回，adapter 续读完成 |
| 使用与匹配范围新鲜度检查 | 通过 | 通过 |

传输面由显式原件和物理页清单约束；导航不发送正文，其他原件和未许可页不能读取。模型原始输出和完整工具记录保留在本机；公开稿给出独立复核后的 [RAG 方案](../examples/frontier-refresh/rag-deepseek-plan.md)、[RAG 复核](../examples/frontier-refresh/rag-deepseek-review.json)、[单细胞方案](../examples/frontier-refresh/bio-deepseek-plan.md)和[单细胞复核](../examples/frontier-refresh/bio-deepseek-review.json)，与先前 Codex 方案分开。

DeepSeek 在指定范围内取得了真实方法信息，能据此提出时间诊断、联合答案/证据评价、冗余感知 gold、适配放行审计和划分审计。但科学复核仍发现可影响实现的错误：FRESCO 的 Obsolete Ratio 不是简单 top-1 过期比例；RARE 过滤把 Fail 作为正类，不能含糊地要求“高召回”；PCA 线性是正常预测基线；全部适配任务获放行并不单独证明准则无区分力；代码接收两组控制均值不证明采样具有生物独立性。数据库版本标签也不能替代论文接收状态核验。

这些错误已在独立稿中修订；额外复核页面单独标注，不归为模型亲读。代码及配置不是论文 PDF，也没有在本轮执行。此次成功证明外部模型确实使用组件与获准原件并完成范围检查，不能据此宣称无人复核的科学正确性或科研性能提升。

## 验证与研究收益

396 项离线测试和真实 MCP stdio 检查通过，包含覆盖截止日期、原始日期可见性、ACL 快照续接、原件读取和宿主约束。代码和可公开元数据通过独立文件清单检查后同步公开仓库；密钥、本机路径、论文原件、完整摘要、原作者代码副本及浏览器载荷不进入仓库。

本轮支持的结论是：更新后的检索状态可被程序核验，agent 确实使用了新原件，方案增加了能落实的比较条件、诊断和失败判据。没有运行训练或科研基准实验，因此不声称预测性能提升、完整复现或节时百分比。原件使用通过也不能代替科学理解与实验验证。
