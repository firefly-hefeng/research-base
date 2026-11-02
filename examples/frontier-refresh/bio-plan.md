# 单细胞扰动预测：前沿更新后的研究方案

这是本地科研 agent 经实际研究接口读取原始资料后的方案，未调用 DeepSeek，也未执行作者代码或科研实验。本次新增依据是 2026 年 10 月的 SafeDelta 与 PIE 作者代码及方法说明，共五份材料、七次文本读取、20,754 个返回字符；不是两篇论文的 PDF 全文。旧版方案中的基础指标、简单基线、组合任务和对照校准继续保留。

## 本轮具体修改

1. **把泛化与适配拆成不同问题。** 分别建立未见上下文、未见扰动、二者同时未见，以及跨数据集测试；few-shot 上下文适配单列，不能称作零样本。PIE 的 within-dataset 配置针对四条细胞系逐折训练和验证，并指向三套已命名测试集。先取得实际 split JSON，检查每条扰动、上下文、剂量和实验批次归属，再冻结划分；仅看配置名不能证明无泄漏。组合预测仍用旧版独立协议，不从这两份代码推断组合泛化能力。[PIE within-dataset 原始配置](https://github.com/ArcInstitute/pie/blob/038940057545ae686db946ae247aeb0ff0101897/src/pie/configs/experiment/replogle_wdataset.yaml)

2. **新增可执行的“适配或保留基线”分支。** 对有少量已测目标上下文响应的任务，比较 pooled-source 基线、固定 offset 适配、校准门控 offset；mixture 作为另一个预先声明的扩展。SafeDelta 的 `fit` 仅接收其他上下文响应、校准扰动索引、尚未减控制的校准表达，以及两组控制均值；最终测试扰动的响应不得用于这些输入。基因轴、目标轴须显式对齐，校准目标至少两个且互不重复。[SafeDelta 使用约定](https://github.com/yyb2020/safedelta/blob/6789706cc63dfe0a60e7a717fb51c63590452422/docs/USAGE.md)

3. **把控制独立性落实到释放决策，而非仅改最终评分。** 一组控制参与拟合，另一组评价 leave-one-calibration-target-out 的 PCC 增益；预先固定 one-way 或 symmetric 约定。只有校准统计量为有限数且大于零才释放适配，否则原样返回 pooled-source 预测。不能在最终测试结果上重选释放阈值、适配类型或控制拆分；这个经验规则也不保证每个新目标受益。[SafeDelta 原始实现](https://github.com/yyb2020/safedelta/blob/6789706cc63dfe0a60e7a717fb51c63590452422/src/safedelta/adapter.py)

4. **按实验单位拆控制，保留无定义结果。** 有供体、培养孔或独立实验单位标签时，优先整单位分组，再记录样本量匹配及重复拆分的不确定性。SafeDelta 的函数支持按细胞或整单位拆分，但“不相交”不等于“与处理独立”，后者必须由采样设计成立。常数预测或常数真值的 PCC 保留为 NA，并报告覆盖比例；不能补零或悄悄丢弃不利案例。共享与独立控制诊断必须保留完整配对记录。[SafeDelta 数值与控制实现](https://github.com/yyb2020/safedelta/blob/6789706cc63dfe0a60e7a717fb51c63590452422/src/safedelta/core.py)

5. **独立运行跨数据集协议并审计额外信息。** PIE 的跨数据集配置让 Replogle 保持在目录中但训练权重为零，注释说明其用于固定基因顺序和采样器标识；目录存在本身不等于训练污染。仍须核对实际训练行、验证及 checkpoint 选择、响应证据生成过程。该配置与 within-dataset 配置的训练混合、外部来源、训练步数和资源不同，不能把两者差异直接归为模型架构效果。按“共同可用输入”和“额外知识来源”分层比较，逐一排查外部表达、形态和药物资料与测试条件的重合。[PIE cross-dataset 原始配置](https://github.com/ArcInstitute/pie/blob/038940057545ae686db946ae247aeb0ff0101897/src/pie/configs/experiment/replogle_xdataset.yaml)

## 运行顺序与失败判据

先生成不可变的划分清单、输入权限表和基因轴映射；随后做不训练大模型的合成/小样本检查，再跑简单基线与门控适配，最后才加入 PIE 等资源较重方法。正式实验分别报告误差、相关性、扰动区分、DE 识别及适配净增益，不用单个综合排名掩盖退化。控制不独立、测试响应进入拟合或选型、轴未对齐、NA 覆盖不足时暂停对应比较并排查；运行成本、硬件、重复数和停止预算在正式执行前固定，本轮没有给出尚未测得的性能或耗时数字。

## 读取与前沿核验范围

使用会话的 `check_research` 与宿主输出钩子均要求实际原件使用。对应检索计划覆盖 2025-01-01 至 2026-11-03 的 Europe PMC、PubMed 明示查询；`current_frontier_ready=true` 仅表示本次已完成查询及所读资料满足程序检查，不代表全学科无遗漏。SafeDelta/PIE 的论文 PDF、其余新候选全文、实际 split JSON、数据与训练实现仍有未读项；不能据此复述论文数值结论或宣称已完成独立复现。
