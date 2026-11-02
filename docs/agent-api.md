# 科研 Agent 接口和 DeepSeek 接入

Research Base 通过 Python、CLI 和可选 MCP 提供同一份文献库能力。外部研究 agent 可以自行选择论文、原始材料和读取范围；不必接受固定摘要或预先整理的知识卡。

## 原始资料路径

同机 agent 可以使用 `get_paper` 和 `list_materials` 获得材料 ID、用途、版本、原始资料路径、可用的提取文本路径及外部代码和数据入口，再通过宿主已有的文件读取、PDF 视觉阅读或代码浏览能力访问原件。

提供方明确连接了已分别入库的预印本和正式论文时，`get_paper.related_paper_ids` 给出关联 ID，版本和材料列表可包含这些关联记录。已有 paper_id 和原件归属保留；原件仍通过 local_path、text_path、pages_path 读取。发现可读预印本不表示已经取得正式版，严格版本目标继续按实际版本检查。

这类路径只对能够访问库所在文件系统的宿主有效。DeepSeek API 收到一个本地绝对路径，并不能直接访问本机文件。内置 DeepSeek agent 因此使用 `read_material` 取得需要的文本；支持同机文件工具的其他宿主可以直接使用路径。远程 agent 应通过工具读取或显式导出的材料包取得内容，不能把服务器绝对路径当作跨机器下载地址。

建议的调用顺序是：

1. 使用 `list_papers` 直接浏览集合并自行选文献，或用 `prepare_context`、`search_library` 找到与任务相关的论文。
2. `get_paper` 查看论文与版本，`list_materials` 列出主文、附件、代码及数据入口。
3. 按需要直接读取原件，或调用 `read_material(material_id, pages=[...])`。
4. 需要移交其他工作环境时，由可信宿主显式调用 `export_collection` 导出集合与材料。

路径是读取入口，不代表内容已经被模型读过。扫描 PDF、提取失败和仅有外部链接的材料要保留状态，使用原件或宿主能力继续处理。

`read_material` 截断时返回可直接重放的 `continuation` 参数对象，读完为 `null`。续读应原样传回该对象；其中 `pages` 保持原选择，`char_offset` 是这些页按物理页序拼接后的字符偏移。换成新的 `pages` 选择时必须把 `char_offset` 重置为 0，不能把上一选择的偏移带过去。`offset_scope` 说明这个范围；`next_page` 仅供导航，不能替代 `continuation`，也不能证明该页内容已经被读到。

```python
result = base.read_material(material_id, pages=[2, 3], max_chars=6000)
while result["continuation"] is not None:
    result = base.read_material(**result["continuation"])
```

## 通用工具注册表

`ToolRegistry` 定义函数名称、JSON Schema 和统一调用入口。CLI、MCP 和内置 DeepSeek agent 复用内核，返回 `{schema_version: 1, data: ...}` 或错误 envelope。

```python
from research_base.core import ResearchBase
from research_base.tools import ToolRegistry

base = ResearchBase("./.research-base/library")
registry = ToolRegistry(base)
tool_schemas = registry.specs()
result = registry.call("list_materials", {
    "collection_id": "实际集合ID",
    "include_paths": True,
})
```

默认工具包括 `list_sources`、`list_collections`、`list_papers`、`search_library`、`get_paper`、`list_materials`、`read_material`、`prepare_context` 和 `get_job`。`list_papers` 不要求检索词，支持 collection_id、成员状态、limit 和 offset，返回简要元数据、总数及 next_offset，便于 agent 分页浏览后自主选择材料。

内置 agent 绑定 collection_id 后，省略集合参数的 list_papers、list_materials、search_library 和 prepare_context 默认使用该集合，避免一次列出整个库的材料。未绑定的通用注册表仍可全库浏览；调用方也可以显式指定其他集合。

设置 `allow_writes=True` 才会提供创建集合、发现文献、改变纳入状态、批量获取和运行 worker 的工具。内置模型 agent 不获得任意文件路径导出、任意文件导入、shell 或代码执行工具。

可信同机 MCP 服务使用 `allow_writes=True, local_admin=True`，额外提供材料导出和浏览器任务领取、续租、提交、恢复操作。它面向已授权使用本机库的宿主，不应未经鉴权改造成公共网络服务。

## 种子引导发现和引用扩展

已知一组重要论文时，可以使用 `discover_frontier` 建立一次有边界的资料准备任务。它先按主题与来源检索，再以提供方真实返回的标识或经过身份核对的结果恢复漏掉的种子。seeds 只描述待查身份，不会被直接当作检索结果写入；本地已有条目也不能冒充本轮来源命中。

```python
job = base.discover_frontier(
    collection_id="实际集合ID",
    query="retrieval augmented generation evaluation",
    seeds=[
        {"title": "RAGChecker: A Fine-grained Framework for Diagnosing Retrieval-Augmented Generation", "arxiv": "2408.08067"},
        {"doi": "10.52202/085713-2087"},
    ],
    sources=["openalex", "crossref", "arxiv"],
    date_from="2024-01-01",
    budget={"max_requests": 12, "broad_requests": 4},
)
result = base.run_worker(job["job_id"])
```

报告应分别读取广检索召回、种子恢复结果和仍未找回的条目。种子通过定向查询找回时，不记作广检索成功。短预算中未执行、来源错误和确定未命中也要区分；暂停后通过任务续接继续。已知基础论文超出广检索时间范围时，应保留其实际日期和恢复来源，不能由此刷新另一个时间范围的覆盖状态。

progress.broad_recall 是广检索命中的 found/total，seed_coverage 是包括来源查回后的 found/total；progress.seeds 逐条给出 status、paper_ids、broad_paper_ids 与提供方尝试。broad_job_id 指向对应普通检索任务。budget.broad_requests 可固定广检索的请求份额，例如总计 12、广检索 4，余下用于种子查回；必须在 1..max_requests 内。省略时由内核分配，原 discover_papers 的行为保持不变。

取得真实论文后，`expand_citations` 从实际 paper_id 启动一跳扩展：citations 找引用该工作的后续研究，references 找它引用的基础工作，both 同时处理两类关系。输入论文与扩展方向、提供方及游标保存在任务中；扩展产生 candidate，不自动改成 included，也不改变研究者先前的排除决定。

```python
job = base.expand_citations(
    collection_id="实际集合ID",
    paper_ids=["已取得的paper_id"],
    direction="both",
    date_from="2024-01-01",
    budget={"max_requests": 6},
)
result = base.run_worker(job["job_id"])
```

一跳引用扩展用于补充主题检索，完成一跳不表示已遍历学科引用图。引用关系表示文献连接，不能独立用作质量或支持关系判定。扩展 job 的完成也不会刷新主题广检索的新鲜度；需按当前研究问题阅读原件、判断可比条件。

引用任务的 progress.streams 分别保存每个起点和方向的 cursor、provider、complete；paper_ids 和 edges_added 展示本轮加入的候选及关系数量，scope 声明扩展范围。用例报告应保留未完成的流，不能把已返回的一页称为整张引用图。

模型和 MCP 对预算暂停的任务先调用 `resume_job(job_id)`，再调用 `run_worker(job_id)`，从保存的来源或引用游标继续；只重复 run_worker 不会恢复 paused 或 partial。工具层的 resume_job 只接受 discovery、frontier 和 citations。`cancel_job(job_id)` 可以取消未完成工作，保留已经取得的文献和原件；取消 frontier 时同时取消其广检索子任务。

这两个操作只在 allow_writes=True 时暴露给模型，MCP 使用相同的结构化参数。模型工具接收 seeds 数组，不获得 --seeds 文件路径读取能力。每条 seed 至少提供 title、doi、arxiv 或 pmid，允许补充 authors、year 和 seed_id 以便身份核对。

## 重新尝试公开全文路径

`retry_acquisition(job_id, refresh_locations=True)` 用于已有获取任务。它重新安排允许重试且没有有效浏览器租约的公开获取目标，可以刷新备用位置；不是新建一份重复的资料集合，也不删除已有成功原件。

正常获取按需推进：先尝试已知 PDF，再查开放元数据位置，前面路径未满足目标才执行较重的备用阶段。备用阶段可核对论文页的 citation_pdf_url 或 PDF alternate 元数据、PMC 公开元数据，以及提供方明确的正式版与预印本关系。网页需通过标识核对，不能把登录 HTML 当作 PDF，也不按相似标题直接合并文献。

任务结果中的 route_attempts 标记实际尝试的路径阶段和结果；resolver_diagnostics 或 open_access_errors 保留解析器的限制和失败。位置失效、文件身份不明、版本不满足和缺少访问权是不同问题。具体阶段诊断与本轮实现说明见[第二轮优化](optimization-round2.md)。

需要机构登录、验证码或身份确认的任务保留人工障碍；有效浏览器租约不能被后台重试抢占。研究者或宿主明确解决障碍后，再使用对应恢复或确认入口。新恢复的公开副本仍按原版本策略判断是否满足目标，unknown 不能自动变成 published。

模型与 MCP 的 resume_job 不接受 acquisition，避免以通用续接清除 needs_user。公开路径使用 retry_acquisition；人工障碍解决后，由有浏览器管理权限的宿主调用 resume_browser_task，再领取和交回文件。Python 与 CLI 的通用恢复入口仍由明确操作它们的宿主负责。

评估改进时分别记录广检索、种子恢复、引用扩展与获取的实际请求数和状态。同预算对比须说明各阶段怎样分配预算；增加定向查询后找回种子，证明该恢复路径可用，不能单独推断整个领域召回率。

## DeepSeek 配置

可选配置通过进程环境变量提供，或由启动入口从明确指定的本机忽略文件加载：

| 变量 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | API 密钥，必须由运行环境提供 |
| `DEEPSEEK_BASE_URL` | 默认 `https://api.deepseek.com`，可配置兼容服务或本地代理 |
| `DEEPSEEK_MODEL` | 默认 `deepseek-flash`，可按账户可用模型覆盖 |

库和检索功能不需要 DeepSeek 密钥。构造 agent 不会联网，只有 `run` 或 `plan_queries` 才发起模型请求。远程 API 使用 HTTPS；本机 HTTP 代理仅允许 localhost/回环地址。程序不把密钥写入对话、工具载荷、普通异常或日志。

```python
from research_base.agent import ResearchAgent
from research_base.core import ResearchBase

base = ResearchBase("./.research-base/library")
with ResearchAgent(base) as agent:
    result = agent.run(
        "查找与当前实验设计相关的论文，并列出可以直接读取的原始材料",
        collection_id="实际集合ID",
        max_steps=8,
        max_tool_calls=24,
    )
    print(result["answer"])
```

`run` 默认只读。显式 `allow_writes=True` 允许模型为当前请求建立集合、检索、纳入和获取文献；浏览器操作仍由宿主完成。返回值包含最终答案、模型、用量汇总、调用工具名称和完成状态，不返回内部推理文本。

模型正常完成时状态为 completed。达到轮次或上下文预算且此前工具调用已闭合时，程序压缩冗余导航元数据，优先保留实际读取的原文，再做一次不提供工具的最终合成；返回 completed_with_limits 和 limit_reason，要求答案说明全文、覆盖及更新方面的限制。该收尾不会增加工具调用次数。若工具调用预算在一个尚未闭合的批次中耗尽，则返回 tool_limit，不发送协议不完整的后续请求。达到预算和生成有限答案均不等于已完成全面研究。

## 显式项目模式

内置 agent 可以真正加载某个研究项目的局部规则，而不只依赖规则文件已经写入磁盘：

```python
with ResearchAgent(base) as agent:
    result = agent.run("设计下一步实验", project_root="./my-research-project")
    print(result["project_integration"])
```

CLI 对应 `research-base agent run "设计下一步实验" --project ./my-research-project`；通过全局 --library 选择与该项目绑定身份一致的库。

项目模式读取所选目录内的 `.research/config.yaml`，校验 library_id 和 collection_id，再按 integration.entry_file 读取规则；未配置时使用 AGENTS.md。文件必须位于明确选定的项目边界内，不向祖先或用户全局目录查找。规则内容加入 system 消息；模型请求前仅自动调用一次 prepare_context，将有界文献导航和 freshness 注入任务上下文，之后由模型选择具体原文读取。

返回的 project_integration 包含 rules_loaded、rules_status、context_prepared、project_root 和实际入口文件。规则缺失或为空会明确报告，不能称为已加载；配置、库身份或路径边界错误则在模型请求前停止。规则过大或无法读取时也明确报错。运行过程只读，不安装、修改或恢复项目规则。

project_integration 表明本次确实加载了哪些入口、是否准备了文献导航，不证明模型已理解或遵循全部研究要求。外部 MCP 宿主的规则加载仍由该宿主负责；这里的自动加载只适用于显式选择 project_root 的内置 agent。

`plan_queries(task)` 可单独生成查询、同义词、来源选择和各来源检索式的 JSON 计划，不检索外部文献，也不修改集合。调用方可以审阅或调整后交给 `discover_papers`，便于接入现有科研工作流。

`select_papers(task, candidates, max_papers=6)` 从已有候选中提出筛选建议，仅发送有界的 ID、标题、作者、年份和摘要。返回的 selected_paper_ids 必须属于输入集合，附简短理由；该方法不修改纳入状态。最多向模型提交 100 条候选，每条摘要最多 1500 字符，输入超过范围时明确标记截断。调用方可审阅建议后显式执行 set_membership，不能把元数据筛选当作已经读过全文。

模型循环使用有界次数的 function tool calls，校验工具参数并限制可调用名称。默认使用非思考模式；若兼容服务仍返回 `reasoning_content`，后续请求保留原始 assistant 消息以维持协议兼容，但不将该字段作为用户报告输出。工具结果过长时明确截断，提示缩小读取范围。

## MCP

安装可选依赖后通过 CLI 的 MCP 入口启动，或调用：

```python
from research_base.mcp_server import serve

serve("./.research-base/library")
```

服务使用官方 Python SDK 和 stdio，不把诊断日志写入协议输出。未安装 SDK 时返回 `MCP_NOT_INSTALLED` 和安装方式。网络检索和下载通过 `run_worker` 显式执行；不要只因创建了 job 就声称已取得全文。

真实 stdio 检查单独标记为 live；设置 `RB_RUN_MCP_TESTS=1` 后运行 `tests/test_mcp.py`。检查在带超时的独立进程中执行，不让普通离线测试因本机通信权限而挂起。Windows 的事件循环初始化需要可用的本机回环通信。

## 接口依据

- [DeepSeek 首次调用说明](https://api-docs.deepseek.com/) 定义 API 地址和当前模型名称；模型可用性以实际账户和配置为准。
- [DeepSeek 工具调用说明](https://api-docs.deepseek.com/guides/tool_calls/) 给出工具循环和非思考模式调用方式。
- [DeepSeek 思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/) 说明携带工具的多轮请求如何传回 reasoning_content。
