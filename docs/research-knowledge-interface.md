# 面向研究决策的文献知识接口

Research Base 把论文、代码、数据和补充材料作为主要知识来源。集合承载研究方向，少量精选论文提供稳定的前沿入口，任务接口帮助 agent 选择要读的原文。组件不把长摘要或知识卡作为必须经过的中间层。

## 前沿参考空间

研究者或已获写入权限的 agent 通过 `configure_frontier(collection_id, paper_ids, profile)` 设置少量关键论文和默认学科。论文须已纳入对应集合；筛选候选、纳入集合和设置关键入口仍是不同操作。`get_frontier` 提供这些工作的版本、原文及附件入口、更新情况和缺口。

精选论文在任务导航中保持可见，避免只按搜索词或发布时间排序时漏掉关键基础工作。研究者可以替换精选集合；系统不把它们当作不可挑战的结论。被引用、被纳入或新近发表也不自动等于高质量或已达到研究前沿。

已有 DOI、arXiv 标识或 PMID 的关键工作仍应通过种子恢复核对身份，再按需做引用扩展。新鲜度来自实际检索记录，配置精选入口或创建阅读会话都不会刷新它。

## 研究任务接口

| 接口 | 用途 |
| --- | --- |
| `prepare_research` | 只读预览：任务相关材料、精选前沿、阶段与学科阅读问题、更新和获取缺口 |
| `begin_research` | 为当前任务和阶段建立使用会话，并返回相同的阅读入口 |
| `extend_research` | 将后续选中的已纳入论文加入当前任务范围，保留原有必读要求与读取记录 |
| `read_research_material` | 读取 agent 选择的原文范围，登记组件实际返回的内容范围 |
| `check_research` | 提交方案或结论前检查实际使用的论文与已观察的原文读取 |
| `end_research` | 外部宿主完成任务后释放会话；内置 agent 的当前宿主会话不能由模型提前关闭 |

`stage` 支持 `explore`、`design`、`implement`、`interpret`、`novelty`。`profile` 支持 `general`、`machine_learning`、`single_cell`，省略时使用集合的默认学科。调用方按当前问题选择阶段，无需每条对话都重新建会话；研究问题或决策阶段实质变化时建立新会话。

这些阶段影响“到原文中核对什么”，而不是要求 agent 填一套表格：

- 探索与创新性：研究范围、最接近的方法、关键基础工作、最新搜索范围及可验证的差异。
- 实验设计：假设、比较条件、基线、数据划分、测量对象与统计不确定性。
- 实现复现：代码、版本、预处理、参数选择、环境及与原论文设置的偏差。
- 结果解释：指标定义、适用条件、替代解释、失败情形与外推限制。

机器学习 profile 进一步提示训练测试隔离、数据泄漏、基线和调参预算、指标单位及重复实验。单细胞 profile 进一步提示生物学重复、供体或批次划分、扰动及细胞上下文、对照构建和评价单位。它们是可选的研究阅读提示；组件不会自动判定研究已符合某项专业标准。

## 命令行入口

使用实际的集合和论文 ID：

```text
research-base research configure --collection COLLECTION_ID --paper PAPER_ID --profile machine_learning
research-base research frontier --collection COLLECTION_ID
research-base research prepare --collection COLLECTION_ID --task "设计评价实验" --stage design --focus PAPER_ID
research-base agent run "设计评价实验" --project ./my-research-project --stage design --profile machine_learning --focus PAPER_ID
```

多个 `--paper` 或 `--focus` 可重复提供；configure 不传 `--paper` 可清空精选入口。`research prepare` 只准备导航，不创建跨命令的读取会话；会话使用 Python 注册表、MCP 连接或内置 agent 承载。

## 接入一个 agent

同一个 `ToolRegistry` 实例承载一轮研究会话。外部 agent 使用注册表或 MCP 暴露的结构化工具：

```python
from research_base.tools import ToolRegistry

tools = ToolRegistry(base, collection_id=collection_id)
started = tools.call("begin_research", {
    "task": "检查当前实验的划分和对照是否能支撑泛化结论",
    "stage": "design",
    "profile": "machine_learning",
    "focus_paper_ids": [paper_id],
})["data"]
session_id = started["session_id"]

# material_id 来自本次导航、get_paper 或 list_materials；由 agent 自选。
read = tools.call("read_research_material", {
    "session_id": session_id,
    "material_id": material_id,
    "pages": [3, 4],
    "max_chars": 8000,
})
checked = tools.call("check_research", {
    "session_id": session_id,
    "used_paper_ids": [paper_id],
})["data"]
```

宿主必须在发出关键研究方案或结论前检查当前会话结果：`ready` 为 true 且 status 为 `ready_to_synthesize` 表示可在列明限制的前提下合成；`needs_materials` 表示仍需处理。`current_frontier_ready` 另行表示读取步骤及匹配的检索新鲜度均已满足，不能用一般合成通过代替当前前沿或创新性核查。只浏览导航、获取路径、返回空文本、读取错误集合的文件或模型自报“已读”，均不能替代原文读取。`focus_paper_ids` 用于声明这次决策必须核对的论文，不需要强制阅读整个集合。`check_research` 中声明使用的论文 ID 也会累计保存，后续省略 used_paper_ids 不会消除尚未满足的阅读要求。

若需要更多材料，继续使用原有 `search_library`、`get_paper` 和 `list_materials` 自由选择原文，并通过 `extend_research(session_id, paper_ids)` 将新选中的已纳入论文加入当前任务范围。`read_research_material` 的页码为 PDF 物理页，从 1 开始；截断续读沿用返回的 `continuation`，其中包含 session_id。已选择范围的未完成续读会列在 pending_continuations；应从偏移 0 读取并补齐当前选择，不能跳过中间内容却称为已读完。路径列表与页码导航只说明有哪些资料，不能作为已读取内容。

每次检查重新读取当前集合的匹配检索状态，后续真实完成补充检索可改变结果，创建新会话不能把旧搜索变新。`novelty` 阶段在新鲜度未满足时不能 ready；其他阶段可以基于已读原文合成受限方案，但 `current_frontier_ready=false` 和相应限制必须保留。该标志仍不是全领域覆盖、论文质量或科学结论的认证。

外部 Python 宿主可把使用检查接到真正接收研究结果的代码位置：

```python
# session_id 由宿主保存本次 begin_research 的返回值，不能让模型替换。
usage = tools.require_research_ready(
    session_id,
    used_paper_ids=actual_used_paper_ids,
    require_current_frontier=True,
)
# 只有未抛出 RESEARCH_USAGE_REQUIRED，才接收本次研究结论。
```

`require_current_frontier=True` 适用于宿主要求核查当前前沿的输出；普通受限方案可采用默认 false，但必须保留 usage 中的更新和材料限制。该钩子是宿主 Python API，不是让模型自行声明通过的工具。宿主应保留当前会话 ID，并在需要固定任务范围时设置 `tools.required_session_id = session_id`，避免模型重建或替换会话。

Python 的 `prepare_research` 和 `ResearchUsage.begin` 还接受显式 `project_root`，使项目的 discovery 与 freshness 设置进入导航和后续动态检查。项目绑定必须与所选库和集合一致；内置 agent 在验证项目和实际规则入口后自动传递该项目目录。

## 保留直接访问原件的自由

同机宿主可以直接打开 `local_path`、`text_path`、`pages_path`，使用自己的 PDF 视觉阅读器、文件工具或代码浏览器。要让这些读取进入程序检查，宿主通过 `ResearchUsage.observe_read(session_id, actual_host_read_result, ...)` 将实际交给模型的读取结果登记到会话。该方法是仅供 Python 宿主使用的适配钩子，不暴露为模型工具；agent 自己填“已读”不作为可信回调。没有这种接入时，使用 `read_research_material`。

PDF 视觉读取由可信宿主在原始物理页已经通过视觉接口交给模型之后调用：

```python
tools.research.observe_visual_read(
    session_id,
    material_id,
    pages=[2, 3],
    image_sha256=delivered_page_image_sha256,
)
```

每个物理页对应一个已交付页图像的 SHA256，顺序与 pages 一致。宿主负责真正渲染并交付这些原件页面；只取得路径、渲染但未交付的图像、未给模型看的截图或模型自报不构成视觉阅读。这个回调不向模型暴露；哈希只标识已交付图像，本身不证明交付或理解。读取记录区分 `host_observed_visual` 与文本，视觉记录的 returned_chars 为 0，不伪造已读文字数量。已有截断文本范围仍保持 pending，不能用无关页图像清除续读要求。

远程模型只收到本机路径时无法读取其内容，应使用工具读取或导出的材料包。代码和数据链接保留原始入口；若组件尚不能读取该格式，使用检查不能替宿主证明内容已打开或代码已经运行。

## 内置 agent 的自动检查

内置 `ResearchAgent.run` 在绑定 collection_id 或 project_root 时默认采用 `research_mode="strict"`，先由宿主启动不可被模型替换的会话，再把清理私有路径后的任务导航和 session_id 交给模型。可设置 `research_stage="design"`、`profile` 与 `focus_paper_ids`；未指定 focus 时优先选任务检索命中的少量关键前沿入口，无直接命中时由可信规划决定相关性，不能要求模型随便读一篇就完成。绑定项目时仍真正读取所配置的 AGENTS.md 入口。普通 `read_material` 调用也会经过当前会话的读取观察，不要求模型只能使用一个读取工具。

strict 对所有阶段要求有效的匹配检索更新、必读原件、每篇 focus 的关键问题以及实际最终稿的独立审核。自动检索 focus 仅作建议，规划可在有限入口内重选最多三篇；显式宿主 scope 不可替换。默认每篇恰好一个单一决策细节问题，避免复合穷举或预设不存在的阈值/方法；宿主显式 critical_checks 仍可至多八个。默认规划和两类审核由已配置模型的隔离调用完成，外部宿主可替换可信回调，或 `automatic_review=False` 禁止默认额外调用。额外调用及 token 用量见 `auxiliary_model_calls`，上限十六次。`research_mode="explore"` 明确退回普通探索，结果为 `exploration_completed`；底层直接 `begin`/MCP 会话仍兼容普通读取，不自动获得 strict 资格。检查结果中的 `enforcement.strict_ready_for_final_review` 只表示可以审稿，`strict_research_completed` 仅在可信最终稿接收回执中为真，不能将中途普通 `ready` 当作研究完成。

模型准备输出时，宿主再次执行 `check_research`。尚缺材料或核验时，最多在原有预算内给模型两次补读/修订机会；仍未满足时返回 `needs_materials`、`needs_refresh` 或 `needs_verification`，不展示未经检查的科学草稿。默认 strict 的 `completed` 必须具有独立最终稿回执；显式 explore 的成功结果为 `exploration_completed`。调用结果中的 `research_usage`、`research_session_id` 和 `synthesis_review` 供工作流检查。

模型不能通过重建会话或使用另一个会话 ID 清掉宿主指定的必读要求；可以选择读取段落、补充材料和扩展本次任务的论文范围。未绑定任何文献集合的管理任务不自动启动研究检查，正常查询结果为 `unbound_completed`，并明确 `research_policy.mode="unbound"`、`strict_research_completed=false`，不混同严格研究完成。

同一已验证原件的实际页/节区间可跨选择合并：先完整读取 `[4,6,8]` 后只复查第 6 页尾部，不会重新制造该页开头的假缺口。真正没有返回的中间区间仍需续读；不同原件、版本或哈希不共享阅读信用。`read_research_context` 遇到宿主提供的 `permitted_pages` 时只读许可内的邻页/节，`omitted_neighbors` 明示未返回的邻单元；这不代表缺少的上下文已经解决，也不会扩大材料外发范围。

## 规则与程序检查的边界

局部 AGENTS.md v4 要求实际调用这些接口，默认建立任务相关前沿必读与关键问题，并在提交前严格检查。未改动的 v1/v2/v3 受管区块可通过重复 `attach` 安全升级；其他规则和 Vesti 区块保留。任务关键点、独立复核及最终稿检查见[关键细节核验](checked-reading.md)，后台原件获取见[浏览器适配器](background-browser.md)。

程序检查能证明某些原文内容已由接口返回、指定材料是否仍未读，以及哪些正文、更新和获取缺口尚存在。它不能证明 agent 理解正确、选择了充分的材料、识别了所有研究前沿或完成了复现实验。通过检查后仍应在结论中引用实际使用的论文，并说明适用条件。

检查不满足时继续读取或补资料；无法完成时可以输出受限的待验证方案，但不能标成已核对前沿细节的研究结论。工具不可用也应保留这一限制，不能仅凭 AGENTS.md 已存在就声称流程生效。

MCP 提供工具，不控制外部宿主的输出。外部工作流必须实际加载规则，并将 `check_research` 接到方案或结论的提交步骤；否则只能获得行为指引，不能获得程序拦截。会话记录仅在当前注册表或 MCP 连接中有效；重启后需重新建立会话。外部宿主完成任务后调用 `end_research(session_id)` 释放会话，避免长期连接积满会话；关闭后原记录不可再检查，文献和原件保留。内置 agent 的当前会话由宿主控制，模型不能提前关闭以绕过检查。它是本次工作流的轻量状态，不要求研究者维护额外阅读卡。

## 实践依据

多渠道检索、补充查找与明确搜索范围，参考 [Cochrane Handbook 第 4 章](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-04) 的检索原则。该手册主要针对干预研究系统综述；本组件采用相关检索原则，不把日常研究工作流标成完整系统综述。

核对原论文的假设、局限、数据划分、参数选择、代码数据和不确定性，参考 [NeurIPS Paper Checklist](https://neurips.cc/public/guides/PaperChecklist)。本接口将这些关注点转为按任务选择的阅读问题，不要求研究者额外撰写一套检查表，也不以接口通过代替学术评审。
