# 核心接口和浏览器获取协议

状态：v0.1 设计契约。当前已提供可执行接口，并增加原件路径、书目导入和自主浏览能力；实际工具参数以[Agent 接口](agent-api.md)及 ToolRegistry 的 JSON Schema 为准，实现差异见[实现状态](implementation.md)。

## 接口层和公共约定

应用服务是业务能力的唯一实现。CLI、Python 调用和 MCP 工具转换到同一服务，不通过启动另一个 CLI 进程实现服务内部调用。

首版 MCP 使用本地 stdio；JSON 输入输出为领域契约，不重新实现 MCP 的协议包。CLI 的 `--json` 输出相同领域结果，常规输出只展示用户需要的信息；诊断日志写 stderr。

时间使用带 `Z` 的 UTC ISO 8601，展示时可按用户时区转换。PDF 页码统一为物理页码，从 1 开始，不把论文印刷页码当作 PDF 偏移。ID 使用不含标题的稳定不透明字符串。

成功响应为 `{schema_version: 1, data: ...}`。错误响应为 `{schema_version: 1, error: {code, message, retryable, details}}`；MCP 适配器同时标记工具调用错误。此处的 schema_version 用于领域载荷，与 MCP 协议版本无关。

写入接口支持 `request_id`。同一作用域内，相同 request_id 和规范化载荷返回原结果；载荷不同时返回 `IDEMPOTENCY_CONFLICT`。作用域至少包含库、操作和调用方。未提供时可发起新请求，但获取任务仍须通过自身目标幂等键去重。

首版单次检索结果读取和批量 ID 输入均设置可配置分页上限；超限返回游标或明确错误，不静默丢弃。返回文献路径前先校验当前宿主与库的同机绑定。

## 对研究 agent 暴露的六个核心工具

| 工具 | 必需输入 | 可选输入 | 输出和副作用 |
| --- | --- | --- | --- |
| `discover_papers` | `collection_id`, `query` | `source_queries`, `date_from`, `date_to`, `sources`, `budget`, `request_id` | 创建发现 job；候选记录写入集合，不自动纳入和获取 |
| `acquire_papers` | `collection_id` | `paper_ids`, `roles`, `version_policy`, `requested_versions`, `request_id` | 对指定的已纳入项创建获取 job；省略 paper_ids 时使用全部 included 的快照 |
| `search_library` | `query` | `collection_id`, `content_scope`, `membership`, `sort`, `cursor`, `limit` | 查询本地库；返回匹配片段、文献状态、material_id 与页码 |
| `read_material` | `material_id` | `view`, `pages`, `max_chars` | 返回指定材料的文本或原件入口，不执行代码或自动下载外部链接 |
| `prepare_context` | `task` 和 `collection_id / project_root` 二选一 | `max_papers`, `max_chars`, `freshness_days` | 返回当前任务的轻量文献入口；不启动隐式网络任务 |
| `get_job` | `job_id` | `task_state`, `cursor`, `limit` | 返回汇总和分页任务详情，不改变任务状态 |

`roles` 首版自动获取仅支持 `main_text`；其他角色可通过材料导入和链接登记提供。`version_policy` 默认允许正式版、接受稿、预印本依次尝试，保留实际取得的版本；可显式要求只接受某种版本。requested_versions 是 paper_id 到 version_id 的映射，会生成对应目标的 requested_version_id，必须精确匹配。unknown 文件可以保存，但只有调用方显式允许 unknown，或人工确认实际版本后，才能满足获取目标；工作者自报版本不足以满足严格策略。

`search_library` 带 collection_id 时默认检索 included；全库检索必须明确其范围，结果仍标明集合成员状态。`prepare_context` 只以绑定集合的 included 作为主要上下文，可另列相关 candidate 供补充，不把 excluded 项注入。

`read_material(view=text)` 支持有限页集合，例如 `[3,4,5]`，并返回 `truncated` 及继续读取定位。`view=original` 返回原件描述和同机可访问路径；文本无法提取时保留原件入口并返回 `TEXT_UNAVAILABLE` 提示。PDF 图像阅读由宿主自己的文件或视觉能力完成，首版不承诺图片渲染工具。外部材料尚未下载时仅返回链接和 `external` 状态。

### 发现任务示例

```json
{
  "collection_id": "col_demo",
  "query": "retrieval augmented generation evaluation",
  "sources": ["openalex", "crossref"],
  "date_from": "2024-01-01",
  "date_to": "2026-11-02",
  "budget": {"max_requests": 100},
  "request_id": "discovery-demo-001"
}
```

接受请求后立即返回 job ID，不在 MCP 调用中等待所有分页完成。`budget` 限制请求规模而非宣称覆盖程度。暂停返回保存的来源游标及 `stop_reason=budget_reached`，之后用 `resume_job` 显式续接。

检索式按提供方能力转换；复杂语法通过 `source_queries` 单独给出。不能将一个数据库的专用语法原样用于所有来源；不支持的过滤条件必须返回提示。来源失败时保留其他来源结果，并报告逐来源状态。

### 上下文返回

[完整示例](../examples/context-result.json) 包含以下字段：

- `collection_id`、集合修订号、`task`、生成时间。
- `freshness`：当前有效计划指纹、对应的完整检索时间与 coverage_through、最近一次尝试是否覆盖全部来源、是否需要更新及原因；计划不匹配或从未完成时日期为 null。
- `papers`：相关论文的标识、标题、版本、全文和可检索状态、摘要或匹配片段、material_id 与页码。
- `gaps`：只有摘要、解析失败、获取中、未命中等影响使用的问题。
- `truncated`、`limits`、`next_actions`：预算和后续读取方法。

正文预算采用字符数，输出标明这是字符预算而非 token 精确计数。查询未命中时返回空 papers 和缺口，不强行塞入无关文献。prepare_context 本身不生成研究结论，也不要求保存持久的“任务证据包”。

## 集合和运行管理接口

这些是配套操作，CLI 必须提供，MCP 可按宿主需要注册。它们同样调用应用服务。

| 应用操作 | CLI 形态 | 语义 |
| --- | --- | --- |
| `init_library` | `library init --ref NAME --path PATH` | 初始化库并注册本机逻辑名称，不把磁盘路径写入研究项目 |
| `create_collection` | `collection create` | 保存名称和研究范围 |
| `update_collection` | `collection update` | 显式更新共享集合默认配置，项目配置不隐式回写 |
| `set_membership` | `collection include / exclude` | 按 ID 或发现 job 的结果显式改变纳入状态，返回数量 |
| `import_material` | `material import` | 将用户指定文件复制到暂存区后走统一校验，登记材料链接或文件 |
| `confirm_material_identity` | `material confirm-identity` | 人工确认暂存文件的论文、角色或版本；格式检查仍必须通过 |
| `link_versions` | `paper link-version` | 确认两个记录的研究工作关联；保留材料和原标识符，不自动删除文件 |
| `run_worker` | `worker run --job JOB` | 运行可续接的发现、下载、解析工作；不持有长数据库事务 |
| `resume_job / cancel_job` | `job resume / cancel` | 续接暂停任务，或取消未完成工作；不删除已入库资料 |
| `export_collection` | `collection export --format bibtex/jsonl` | 导出集合，输出稳定标识和相对对象位置 |
| 项目绑定操作 | `attach / detach / doctor` | 见[接入机制](agent-integration.md) |

如果未运行 worker，异步 job 保持 queued；CLI、MCP 均应明确返回下一步运行入口，避免造成已开始下载的假象。MCP 服务可配置内嵌单 worker，默认配置和进程退出后的续接行为须在安装说明中说明。stdio 日志不得混入协议输出。

CLI 使用流程按以下顺序演示，不预设静态 job ID：初始化库 → 创建集合 → 发起发现 → 运行 worker → 查看结果并纳入 → 创建获取 job → 运行 worker → 宿主处理浏览器任务 → 查询集合 → 绑定项目。

## Job 和 Task 生命周期

### Job

| 状态 | 含义 |
| --- | --- |
| `queued` | 已持久化，尚未执行 |
| `running` | 存在可推进或正在执行的任务 |
| `paused` | 预算或显式暂停，保留检查点，等待 resume |
| `waiting_external` | 等待浏览器工作者领取或完成 |
| `needs_user` | 剩余任务需要登录、身份确认等外部处理 |
| `succeeded` | 目标已全部满足 |
| `partial` | 已结束，部分成功、部分失败；没有仍等待执行的任务 |
| `failed` | 已结束且目标未满足，或发生不可恢复的 job 级错误 |
| `cancelled` | 已取消，不再分发剩余任务 |

Job 计数按独立目标汇总，同时提供 `paper_count` 与各状态 `task_count`。同一目标的重试不增加完成数量。存在等待项时不能提前标成 partial 或 succeeded；只要还有可执行工作，优先保持 running，并另报等待计数。

### 获取 Task

```text
queued -> resolving -> downloading -> validating -> succeeded
                    -> waiting_browser -> leased -> validating
任何执行阶段 -> retry_wait -> queued 或 waiting_browser
任何执行阶段 -> needs_user -> queued 或 waiting_browser（显式 resume）
任何非终态 -> failed 或 cancelled
```

浏览器轮次从 `waiting_browser` 领取。解析是独立的派生处理：主文成功入库但提取文本失败，获取任务仍可成功，材料另报解析状态。不能反复下载同一个扫描 PDF 试图解决 OCR 缺失。

获取目标幂等键为 `paper_id + role + requested_version_id/version_policy` 的规范化值，对活跃目标设置唯一约束。已有满足目标的材料则直接复用；已有未完成目标则通过 JobTask 关联该 task，不重复创建浏览器工作。终态失败目标在新的显式获取请求中可重新创建任务，旧 request_id 重放仍返回原结果。新 job 汇总关联任务的最终结果。

取消 job 只取消自己的 JobTask 关联。存在其他活跃关联时 task 继续；最后一个活跃关联取消后停止分发并使租约失效。取消和最终入库通过数据库事务顺序裁决：先提交成功的原件保留，先取消则不得把该目标继续标成功，未登记文件按暂存或孤立对象处理。已取消 job 不随后续共享任务成功改成 succeeded。

## 浏览器衔接

### 首版部署前提

内核和宿主在同一机器上，能够访问约定的暂存目录。核心组件不依赖某款浏览器、不保管登录 cookie，也不通过 MCP 假定宿主具有 computer-use。研究者或宿主可执行 CLI 交接；带适配器的宿主也可调用同名 MCP 工具。

下载失败的开放接口项进入 waiting_browser，任务携带原始论文身份和落地页。每个浏览器会话同时处理一个任务；不同会话可并行。没有可用工作者时保留任务，报告浏览器能力缺失，不假报失败或自动完成。

### 操作契约

| 操作 | 输入 | 输出 |
| --- | --- | --- |
| `claim_browser_task` | `worker_id`, `session_id`, `capabilities`, 可选 `job_id` | 原子领取一个任务，返回期望身份、落地页、租约与暂存位置；没有工作返回 task=null |
| `renew_browser_lease` | `task_id`, `lease_token` | 延长当前租约，返回新到期时间 |
| `submit_browser_result` | `task_id`, `lease_token`, `submission_id`, `result` | 接受文件或障碍；文件经核心验证后才改变入库状态 |
| `resume_browser_task` | `task_id`, 可选说明 | 用户或宿主解决障碍后显式重新排队 |

CLI 对应 `browser claim / renew / submit / resume`。首版至少支持 `capabilities=["local_download"]`。租约默认 10 分钟，可配置；较长阅读或下载期间由工作者续租。租约过期重新排队时生成新 lease_id 和新暂存目录，避免旧工作者覆盖新工作者的文件。

### 领取载荷

[JSON 示例](../examples/browser-task.json) 展示完整响应。关键内容：

| 字段 | 约定 |
| --- | --- |
| `task_id / job_id / paper_id` | task 关联研究工作；job_id 是本次领取使用的活跃关联 job，供追踪而非 task 的唯一所有者 |
| `target.role` | 首版为 main_text，补充材料不能完成主文任务 |
| `target.requested_version_id` | 可空；为空按 version_policy 接受真实版本 |
| `expected_identity` | 标题、作者、年份、已知 DOI 等，用于浏览器查找及入库校验 |
| `landing_urls` | 按优先顺序尝试的实际页面，不要求工作者从零搜索 |
| `lease` | lease_id、短期 token、到期时间；只在领取响应中给 token，普通 job 查询不泄露 |
| `handoff.mode` | 首版只支持 shared_local_filesystem |
| `handoff.download_dir` | 本轮租约独占、两侧可见的规范绝对目录，由核心创建 |

宿主先查看论文落地页，用已有内网会话下载。如果浏览器只能存到默认下载目录，由宿主在有文件访问能力时复制到任务暂存目录。没有移动或读取文件能力的宿主不满足 local_download，返回能力缺口或请求人工导入，不宣称仅靠点击就完成衔接。

### 文件交回

[JSON 示例](../examples/browser-result.json) 的 result.outcome 可为：

- `downloaded`：提供暂存文件路径、去除凭据的来源 URL、可选版本线索。
- `retryable_error`：下载超时或暂时网络错误。
- `needs_user`：登录、验证码、权限问题或无法确认身份；提供简短原因。
- `not_found`：经当前落地页尝试仍未找到，记录具体原因并结束本轮。

`downloaded` 只表示工作者交回文件。核心在返回 `accepted` 前将文件复制到自身持有的校验暂存对象，登记持久 submission 和 staged Material，再进入 validating；复制或登记失败不得返回 accepted。响应包含 submission_id、task_id、material_id 和当前状态，get_job 也可查到暂存材料，供人工确认。此后由内部 worker 恢复校验，不再因浏览器租约到期重新下载。只有 leased 状态会因浏览器租约到期重新排队。`reported_version` 是线索，不覆盖实际版本识别。

同 task、相同 submission_id 和载荷的重复提交返回原响应；载荷变化返回冲突。已被接受的提交即使稍后租约到期也能查询原结果。新提交必须具有当前有效租约；过期、取消或被重新领取后的提交返回 STALE_LEASE。token 不写入文献正文或普通日志。

| 交回或校验结果 | 下一状态 |
| --- | --- |
| downloaded 且已持久接收 | validating，后续确认目标满足才 succeeded |
| retryable_error，或格式损坏 | retry_wait；超过浏览器通道次数后 failed |
| needs_user，或身份/角色/版本无法确认 | needs_user；明确身份冲突不得自动入库成功 |
| 已尝试所给落地页仍 not_found | failed，错误 FULLTEXT_NOT_FOUND；以后可显式重试或人工导入 |

### 校验和入库

1. 路径必须经过规范化与链接解析，位于该租约暂存目录内；拒绝目录穿越、边界外符号链接和 Windows junction。先确认下载完成，拒绝 .part/.crdownload 等临时文件。
2. 在接收时创建的核心独占副本上检查和计算 SHA256，避免校验与入库之间文件被替换。检查实际 PDF 格式、可解析性和合理的非空页内容，排除 HTML 错误页。
3. 结合正文标题、作者和已知标识确认论文身份。标识需来自论文身份区域，不能把参考文献中的 DOI 当作本篇 DOI。存在冲突拒绝自动入库；不足以判断时进入 needs_user，保留暂存文件。
4. 确认目标角色是主文。页数和文件大小只能作线索，不能独立证明全文完整。无法排除摘要页或补充材料时要求确认。
5. 原件写入内容对象库，再以短数据库事务登记 Material 和目标完成状态。崩溃后允许留下可回收的孤立对象，但不得把不存在的对象标为 available。数据库提交失败可用同一哈希重试。
6. 创建派生提取任务，保留原件及版本关系。文本提取异常不删除已校验原件。

这些检查只验证下载对象可用及身份匹配，不产生额外的学术证据卡。

### 重试和人工接管

瞬时网络错误和服务限流采用有限次数退避，尊重 Retry-After。默认每通道最多 3 次，可配置；开放通道耗尽后转浏览器，不对同一 URL 无限重试。登录等障碍直接进入 needs_user，待显式 resume。浏览器缺席不消耗网络重试次数。

人工接管通过现有浏览器会话解决问题，或用 material import 导入文件。导入也需经过校验；身份自动判断不足时通过 confirm_material_identity 确认归属，记录操作者与时间即可。文件成功登记或确认后重新匹配活跃获取目标，满足目标的 task 成功并撤销其浏览器租约，所有关联 job 更新。恢复任务也先检查已有材料，再决定是否再次下载。

首版不接受远端绝对文件路径。未来远程服务增加 upload 模式，以文件上传句柄交回内容；这属于新能力，不将 shared_local_filesystem 伪装成跨机器协议。

## 错误代码

| 代码 | 调用方处理 |
| --- | --- |
| `INVALID_ARGUMENT / NOT_FOUND` | 修正输入或目标 ID |
| `LIBRARY_UNAVAILABLE / LIBRARY_ID_MISMATCH` | 检查本机库映射，不自动绑定另一个同名库 |
| `SOURCE_RATE_LIMITED / SOURCE_UNAVAILABLE` | 按来源状态退避或稍后续接 |
| `BROWSER_WORKER_UNAVAILABLE` | 保留等待项，接入宿主或人工导入 |
| `STALE_LEASE / IDEMPOTENCY_CONFLICT` | 读取当前状态，禁止覆盖新任务结果 |
| `FILE_NOT_READY / INVALID_FILE / IDENTITY_MISMATCH / FULLTEXT_NOT_FOUND` | 修正下载、显式重试或转人工确认 |
| `TEXT_UNAVAILABLE` | 使用原件入口，或后续增加 OCR/解析器 |
| `PATH_OUTSIDE_STAGING` | 将文件复制到本轮指定目录后提交 |
| `RULES_CONFLICT / PROJECT_CHANGED / RECOVERY_REQUIRED` | 保留用户内容，处理规则冲突或中断恢复 |

## 实现前需要锁定的契约

先依据本文件建立请求响应类型和离线示例，再开始适配器实现。新增字段优先向后兼容；改动状态含义、身份规则或任务幂等行为时同步修改本文件、示例和对应验收。浏览器和宿主的真实下载路径必须在首个实验室环境演示中验证。
