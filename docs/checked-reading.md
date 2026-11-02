# 关键细节阅读与独立核验

本轮真实复核显示：完成选定页面的读取，并不能防止分母、正类、基线角色或代码含义被解释错误。组件因此在既有读取会话上加入少量任务关键点，并独立检查最终稿；无需为每篇论文维护知识卡。

## 宿主选择问题，agent 自主查原件

`begin_research(..., critical_checks=[...])` 最多接受 8 个宿主指定问题，每项为 check_id、paper_id、kind、question 及可选 required_role。只接受已纳入当前集合的论文；这些要求不能由模型重建会话、替换 ID 或省略 used_paper_ids 清除。无关键点的旧会话仍可进行普通原文访问检查，但不会获得 ready_for_checked_synthesis。

agent 使用原接口查找材料和直接路径，也可以用 `read_research_context(page=4, adjacent_pages=1)` 查看定义跨页的上下文。返回的 continuation 继续通过 `read_research_material` 原样补读。“所选范围返回完整”和“概念已定义完整”是不同判断。

`read_id` 标识本会话实际返回的文本。读取结果同时提供轻量 `passages` 导航：片段地址、规范化范围与短预览。推荐通过 `submit_research_detail` 提交解释和 `{passage_id}`，由宿主解析实际返回的原文，避免模型重抄排版空格、数学字符或错配 read_id。也可使用 `{read_id, quote}` 精确引文。它们都是原件地址，不是新增知识卡；不能从未返回的全文、其他会话或其他版本找相似文字冒充。NFKC 与空白规范化后的偏移明确标注，不伪装成原 PDF 的字节位置。

解释可为短文本，或带 summary、numerator、denominator、positive_class、evaluation_unit、conditions、aggregation 的对象。指标需要检查相应定义；不适用项明确说明。代码与正文角色分别指定，配置声明不能冒充已执行的划分，库内 accepted 标签不能冒充同行评审证明。

`metric_reference` 提供可计算的通用逻辑参考。例如指定正类 Fail、预测 Fail 时剔除，则 FP 对应有效 Pass 被误删，FN 对应无效 Fail 被保留；二值联合指标能直接比较“逐实例乘积的均值”和“边际均值相乘”。这些数学参考不证明论文采用了哪些标签，也不能作为原件阅读信用。最终复核包将数学参考与原文上下文分别标注。

## 独立审阅与最终稿

提交 supported 仍只得到 pending_review。模型工具中没有审核通过接口。可信 Python 宿主取得 `detail_review_packet`，让独立复核者查看解释、引文、同论文实际已返回的有界上下文和相邻页导航，再调用 `review_research_detail`；verdict 为 supported、contradicted 或 needs_context。同论文上下文上限 20,000 字符并明确截断；未读的相邻页不会被暗中放入审阅包。

```python
packet = tools.research.detail_review_packet(session_id, "metric-definition")
verdict = independent_reviewer(packet)  # 宿主选择复核方式，不会默认外发原文
tools.review_research_detail(
    session_id, "metric-definition", expected_revision=packet["revision"],
    verdict=verdict["verdict"], reviewer=verdict["reviewer"], reason=verdict["reason"],
)

# 普通检查与严格检查区分；不要只看“已读取”就接收科学结论。
tools.require_research_ready(session_id, require_current_frontier=True,
                              require_checked_details=True)

# 对真正要发出的最终文本再独立复核，防止审核后改口。
accepted = tools.require_checked_synthesis(
    session_id, final_draft, independent_final_reviewer,
    require_current_frontier=True,
)
```

最终复核包包含实际 draft、已核验关键点和本会话实际返回的有界原文上下文，避免只凭模型选出的短引文复核其他原文细节。默认上下文上限为 60,000 字符，截断明确标注，不暗读相邻未返回页。通过后回执绑定该文本的 SHA256；审核期间材料或提交版本变化，旧结果作废。缺少复核者、回调异常、矛盾或上下文不足均不发布成已核验结论。宿主应当发布受检的同一字符串；修改内容后必须重检。

内置 `ResearchAgent.run` 接受 critical_checks、detail_reviewer、synthesis_reviewer。配置关键点后，缺少任一必要复核就返回 needs_verification 或原件缺口，不展示未通过的科学草稿。回调由可信宿主提供，不是模型可调用的工具；组件不默认选择外部供应商或取得新增材料外发授权。

## 状态与边界

- reading_ready：当前任务所需原件已实际返回，指定论文与续读范围满足。
- detail_ready：存在任务关键点，且当前版本均获独立 supported；字节、身份、角色与版本仍匹配。
- ready_for_checked_synthesis：本会话读取、阶段要求及关键点要求满足，仍须对最终稿调用 require_checked_synthesis。
- synthesis_review：独立复核通过的最终文本哈希和关键点版本。它只覆盖本次所选问题和该稿件。

真实文件字节会重新计算哈希，不能只相信目录中的旧 SHA256。修改原件、版本、角色、集合归属或解释版本都会使旧锚点/审核失效。核验会话的必要片段只保留在有上限的内存中，结束会话即释放；普通会话不保留正文。检查结果默认不导出原文或解释全文。

首版严格引文锚支持可提取文本。可信视觉读取仍可登记原图交付，但尚不能直接变成文本锚；公式、图像、解析遗漏应查看原件并保持相应未解决状态。独立复核也可能犯错，尤其同一模型的独立请求会有相关偏差。字段齐全、精确引文匹配和程序通过均不构成科学正确、全领域覆盖或性能复现的认证。

## 局部规则升级

AGENTS.md v3 要求处理跨页定义、指标语义、基线角色、代码/配置与执行证据，并区分普通原文读取与独立细节复核。重复 attach 可升级未改动的 v1/v2 受管区块，保留 BOM、换行、Vesti 和研究者添加的内容。被人工改动的受管规则保持冲突，不覆盖。

规则负责要求 agent 使用接口；外部宿主还必须实际配置任务关键点与独立复核，并把严格检查接到方案提交位置。仅安装 AGENTS.md 不会自动使任意 agent 的输出受到程序拦截。
