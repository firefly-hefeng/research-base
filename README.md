# Research Base 科研文献基础组件

Research Base 面向实验室成员和科研 agent，负责多源发现文献、尽量获取全文、组织论文及代码材料，并为当前研究任务提供检索和读取入口。论文和材料原件是知识主体，目录、解析文本和索引用于帮助使用。

当前已有可运行的 Python 内核、CLI、可选 MCP 和 DeepSeek 工具调用 agent。默认采用本地文献库，多项目可以复用原件和索引。

本轮[两项核心能力升级](docs/core-capability-upgrade.md)扩展检索建议与 bioRxiv 来源，找回四篇已知漏检，补齐单细胞原集合全文，并将原文阅读与关键细节核验接入默认严格研究流程。两真实用例的[前沿注入对照](examples/frontier-ablation/README.md)分别检验方案质量、交付率和额外调用成本。

此前[前沿更新验证](docs/frontier-refresh-validation.md)已完成两个方向 2025—2026 明示查询范围的分页与新鲜度核验，加入 ACL Anthology 来源，并用新增论文及作者原代码修订[真实研究方案](examples/frontier-refresh/README.md)。已读原件、缺失全文和范围限制分别保留。

[研究知识接口](docs/research-knowledge-interface.md)提供精选前沿参考空间和按任务、阶段、学科准备的原文入口。[关键细节核验](docs/checked-reading.md)增加任务关键点、实际原文锚、独立复核和最终稿约束；局部 AGENTS.md 已升级至 v4。绑定项目的内置 agent 默认执行严格研究：任务规划、实际阅读、关键细节复核、当前前沿检查和最终稿验收。原始路径仍可直接使用，程序与独立复核均不替代科研实验。

[后台浏览器获取](docs/background-browser.md)使用任务标签页与浏览器内输入，不控制系统鼠标键盘；大 PDF 分块传回并核对整体哈希。单细胞本轮选定的 9 篇近期论文已取得匹配全文，历史获取缺口记录保留。

[全文与细节核验验收](docs/checked-reading-validation.md)保留两例的实际运行、未通过记录、独立校正和最终研究方案，不将程序或同模型复核通过当作科学正确性保证。

[研究工作流验证](docs/research-validation-round3.md)完成单细胞六条引用流、两个用例的逐条候选筛选与公开全文获取，并保留实际原件读取和独立复核后的研究方案。[公开验证摘要](examples/validated-cases/README.md)区分候选、可读全文、严格版本目标及模型阅读范围；原始论文和密钥保留在执行机器。

## 开始使用

需要 Python 3.11 或更高版本。安装后执行 CLI；Windows 的完整步骤见[快速开始](docs/quickstart.md)。

```text
python -m pip install -e ".[mcp,dev]"
research-base --help
research-base sources list
```

本机验证生成了示例集合与材料入口 `.research-base/demo.json` 和 MCP 配置 `.research-base/mcp-config.json`。这些文件位于忽略目录，不随仓库提供；新克隆项目应按快速开始创建自己的库、集合和路径配置。

## 当前能力

- 多源发现与来源插件：综合来源、生物医学来源、预印本及开放期刊等按需选择，支持分页、来源专用查询、失败状态和请求预算；实际清单使用 `sources list` 查看，[来源说明](docs/sources.md)列出覆盖和限制。
- 种子引导与引用扩展：`frontier` 将主题广检索与真实来源种子恢复分开计量，`citations expand` 可有界展开参考文献和后续引用，保留起点、方向和续接状态。
- 文献与材料组织：稳定标识、版本关联、集合纳入/排除、标签，以及 BibTeX、RIS、CSL JSON、Zotero API JSON 书目导入。
- 全文获取：依次尝试已有位置、开放元数据和后续备用解析，支持经身份核对的论文页 PDF 元数据、PMC 原始 JATS 全文及提供方明确的版本关联；`job retry-access` 可重查未完成的公开路径，未取得项再由宿主后台浏览器接续。XML 按逻辑章节导航，不冒充 PDF 物理页码。
- 原件自主使用：agent 可无检索词分页浏览论文，查看原始 PDF、补充材料、代码/数据链接及可用的本地绝对路径；支持分页文本读取、字符偏移续读和可移交的 bundle 导出。
- 工作流接入：Python、CLI、MCP 复用同一内核；DeepSeek agent 默认只读，显式开放写操作后可检索和获取；局部 AGENTS.md 通过受管区块安装、更新和卸载。

## 验证状态和边界

已进行公开来源真实请求、公开 PDF 下载与提取、DeepSeek 调用材料列表和读取工具、MCP stdio 连接，以及真实浏览器下载后交回和校验的基础检查。离线测试覆盖身份、任务、路径、规则冲突和接口等行为。

[两个科研用例](docs/use-cases.md)已完成有边界的真实流程验证，并经[多 Agent 议会](docs/council-review.md)独立评审和修订。检索仍有预算暂停，单细胞集合也保留未获取全文，尚未完成全领域覆盖或实际科研基准实验。首版浏览器衔接由具备 computer-use 和本地文件访问能力的宿主执行，尚不提供通用无人值守浏览器驱动。

同机路径可直接交给具有本地文件能力的科研 agent；远程 DeepSeek API 无法只凭路径读取本机文件，应使用 `read_material` 工具取得内容。跨机器应移交 bundle 或提供可访问的文件服务。

## 设计和开发文档

| 文档 | 内容 |
| --- | --- |
| [快速开始](docs/quickstart.md) | 安装、检索获取、原件访问、书目导入、模型及 MCP 配置 |
| [实现状态](docs/implementation.md) | 已实现能力、验证边界和与设计基线的差异 |
| [开源工具参考](docs/open-source-review.md) | Zotero、PaperQA2、ASReview 等的可借鉴做法与取舍 |
| [真实科研用例](docs/use-cases.md) | RAG 评价与单细胞扰动预测的执行结果和材料入口 |
| [两项核心能力升级](docs/core-capability-upgrade.md) | 已知漏检回归、全文缺口、默认严格研究与注入对照 |
| [本轮交叉议会](docs/council-core-upgrade.md) | 来源、身份、研究门禁和实际对照的交叉检查与修复 |
| [议会评审](docs/council-review.md) | 独立评审发现、修复和下一轮优先项 |
| [第二轮议会](docs/council-round2.md) | 种子、引用、全文稳定性及科研接口交叉复评 |
| [第二轮优化实现](docs/optimization-round2.md) | 种子恢复、独立引用图、分阶段获取和本轮验证口径 |
| [科研 Agent 接口](docs/agent-api.md) | 工具接口、原始资料路径和 DeepSeek 接入 |
| [研究知识接口](docs/research-knowledge-interface.md) | 前沿参考空间、阶段注入、原文使用检查与宿主接入 |
| [本轮注入验证](docs/research-usage-validation.md) | 两个真实资料集、实际读取、科学复核及议会修复 |
| [前沿更新验证](docs/frontier-refresh-validation.md) | 最新检索、新鲜度截止、近期原件与研究方案变化 |
| [文献来源](docs/sources.md) | 来源覆盖、查询和分页限制、适配器扩展 |
| [总体设计](docs/design.md) | 原则、模块边界、数据模型、存储组织、技术选型与首版范围 |
| [接口和浏览器衔接](docs/interfaces.md) | 核心接口、任务状态、浏览器领取和交回协议、错误与幂等语义 |
| [Agent 接入和规则管理](docs/agent-integration.md) | 项目绑定、局部 AGENTS.md 写入、更新、卸载及上下文使用 |
| [首版开发任务](docs/mvp-plan.md) | 开发顺序、依赖、交付物、测试与发布验收 |

## 示例和模板

- [项目配置](examples/project.research.yaml)：可提交的项目绑定与研究范围配置。
- [浏览器任务](examples/browser-task.json) 与 [文件交回](examples/browser-result.json)：同机获取协议示例。
- [任务上下文](examples/context-result.json)：给研究 agent 的轻量文献入口示例。
- [局部规则模板](templates/AGENTS.research.md)：接入时写入目标研究项目的受管区块。

examples 中的 ID、标题、网址、文件位置及时间是协议演示数据。使用实际调用返回的 ID 和路径；规则模板通过 attach 安装到明确选择的研究项目。

## 首版使用闭环

输入研究方向 → 开放接口检索 → 合并候选并确定纳入范围 → 尽量获取全文 → 未获取项交给宿主浏览器 → 建立本地可检索材料集合 → 将集合接入研究项目 → agent 在研究关键节点检索并读取相关原件。

首版按单机部署，同机可有多个研究项目共享一个本地库。实验室多成员服务化、远程文件上传与多用户权限管理在后续阶段实现。
