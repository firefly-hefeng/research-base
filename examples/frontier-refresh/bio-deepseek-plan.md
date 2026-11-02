# 单细胞研究方案：DeepSeek 原始读取后的独立复核版

本稿与原始模型输出、先前本地 agent 稿分开保存。本次 DeepSeek 实际读取 SafeDelta 的 `docs/USAGE.md`、`src/safedelta/adapter.py`，以及 PIE 的 `replogle_wdataset.yaml`、`replogle_xdataset.yaml`，共四份材料、五次调用、13,093 个返回字符；adapter 的截断范围已续读完成。所读是作者代码、文档和配置，不是论文 PDF，也没有运行其中代码。

SafeDelta 固定提交为 `6789706cc63dfe0a60e7a717fb51c63590452422`；PIE 为 `038940057545ae686db946ae247aeb0ff0101897`。库内出现的 `accepted` 标签不足以证明论文获期刊接收，本稿不据此宣称其接收或同行评议状态。两篇预印本与代码版本应分别核验。

## 研究步骤

1. **保留旧方案的基线与任务边界。** 冻结研究问题、划分、输入权限和互补指标，检查供体、培养孔、批次及控制的关系。PCA 线性、mean/control 及适用任务的 additive 仍是可参与预测比较的基线；使用 held-out 响应的 interpolated duplicate 仅作评价校准或上界参照。未见组合任务继续采用独立协议，不因两份新配置未列出它而取消。

2. **把 few-shot 校准门控作为新实验分支。** 比较 pooled-source、固定 offset 与门控 offset，另将 mixture 作为预先声明的扩展。SafeDelta 接口要求源上下文响应、校准目标索引、未减控制的校准表达及两个控制均值，基因和目标轴必须对齐；最终测试目标响应不得进入拟合或选型。按留一校准目标计算 PCC 增益，有限且大于零才放行，否则回退到 pooled-source。预先固定 one-way 或 symmetric 约定、适配类型和最终输出 fold；不能查看测试结果后再选。[使用约定](https://github.com/yyb2020/safedelta/blob/6789706cc63dfe0a60e7a717fb51c63590452422/docs/USAGE.md)、[原始实现](https://github.com/yyb2020/safedelta/blob/6789706cc63dfe0a60e7a717fb51c63590452422/src/safedelta/adapter.py)

3. **独立设计控制与决策检验。** 两个均值数组不能证明控制组生物独立。优先按真正独立的实验单位拆分，明确哪些供体、孔或实验批次可用于拟合、校准及最终评分；仅在同一供体内随机拆细胞不能替代这一设计。重复拆分和控制深度作为预先声明的敏感性分析，报告放行比例、证据分布、适配净增益及其不确定性。全放行不自动说明准则无区分力；应在独立测试中检验它是否带来收益及风险。常数向量 PCC 按 NA/拒绝规则处理，不补成零。`predict(fold=0/1)` 是同一放行决定下的指定控制参考分支，拒绝时返回相应 pooled-source 基线，不是两个独立候选模型。

4. **分开单数据集泛化与跨数据集迁移。** PIE within-dataset 配置声明逐细胞系折，并列出未见上下文、未见扰动、二者同时未见三套测试。先读取实际 split JSON，核对训练、验证、测试行及其交集，再运行。cross-dataset 配置让目标 Replogle 的训练权重为零，并说明保留目录用于基因轴和采样标识；目录存在本身不是训练泄漏证据，仍须审计实际采样和响应证据生成。两份配置的来源集合、训练混合及预算不同，分别报告结果和成本，不能把差异直接归为架构优劣。[Within-dataset 配置](https://github.com/ArcInstitute/pie/blob/038940057545ae686db946ae247aeb0ff0101897/src/pie/configs/experiment/replogle_wdataset.yaml)、[Cross-dataset 配置](https://github.com/ArcInstitute/pie/blob/038940057545ae686db946ae247aeb0ff0101897/src/pie/configs/experiment/replogle_xdataset.yaml)

先完成划分和输入审计，再做小规模检查、简单基线及门控适配，最后扩展计算成本较高的方法。出现测试响应用于拟合或选型、控制设计不独立、轴无法对齐时，暂停对应比较；全部科学性能和资源收益均待实际实验验证。

## 本次核验的范围

使用检查及 `current_frontier_ready` 均通过，对应完成的是 Europe PMC、PubMed 在 2025-01-01 至 2026-11-03 日期窗的明示查询，不代表所有预印本、引用图或全领域已经覆盖。历史引用扩展应另查日期和范围，不能凭本次搜索记录断言此前未做，或已经刷新到最新。

尚未读取两篇论文 PDF、实际 split 文件、训练与评价完整实现、数据卡和其他近期候选原件。SafeDelta 的 PCC 数值实现也未交给本次 DeepSeek；不能把先前本地 agent 读取该文件计入本次模型记录。本稿是基于真实原件的待验证实验设计，不是论文结论复现或新模型性能证明。
