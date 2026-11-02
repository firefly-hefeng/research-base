# Research Base 安装和使用

Research Base 使用本地文献库保存原件、目录和任务。研究 agent 可以先浏览论文和材料，再自由选择原始文件、分页文本或外部代码入口；无需先制作逐篇摘要。下面以 Windows PowerShell 为例，所有 ID 均从实际调用结果取得。

## 安装

在项目根目录运行，使用独立环境无需修改 PowerShell 的脚本执行策略：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mcp,dev]"
$rb = (Resolve-Path .\.venv\Scripts\research-base.exe).Path
$library = Join-Path (Get-Location) '.research-base/library'
& $rb --library $library library init
& $rb --library $library sources list
```

也可以使用 `python -m research_base`，或在已激活环境中直接运行 research-base。`--library` 是全局参数，放在子命令前。省略时使用 RESEARCH_BASE_LIBRARY，空值或未设置时使用当前目录下 `.research-base/library`。

CLI 的正常结果包含 schema_version 和 data；业务错误包含 error 并返回非零退出码。`--json` 可显式选择结构化输出，目前默认也是 JSON。交给脚本时检查退出码和 error，不能只判断有无输出。

## 当前机器的示例

本机验证生成的示例入口为项目根下的 `.research-base/demo.json`，其中记录实际 library_root、collection_id、paper_id、material_id、PDF/文本路径及 bundle_dir。该文件不随仓库提供；已有本机产物时可在项目根运行下面命令，没有时跳过并按后续步骤建立集合：

```powershell
$demo = Get-Content -LiteralPath .research-base/demo.json -Raw | ConvertFrom-Json
& $rb --library $demo.library_root paper show $demo.paper_id
& $rb --library $demo.library_root material list --paper $demo.paper_id
& $rb --library $demo.library_root material read $demo.material_id --page 1 --max-chars 6000
```

这些 ID 和绝对路径只属于当前机器。`.research-base/` 被 Git 忽略，新克隆项目需要执行后续步骤生成自己的资料和配置。

## 建立集合并发现文献

```powershell
$created = & $rb --library $library collection create --name 'RAG evaluation' --description '检索与生成的评价方法'
$collectionId = ($created | ConvertFrom-Json).data.collection_id

$discovery = & $rb --library $library discover --collection $collectionId --query 'retrieval augmented generation evaluation' --source arxiv --source crossref --max-requests 6
$discoveryJob = ($discovery | ConvertFrom-Json).data.job_id
& $rb --library $library worker run --job $discoveryJob
& $rb --library $library job show $discoveryJob
& $rb --library $library paper list --collection $collectionId --membership candidate --limit 20 --offset 0
```

`discover` 创建持久任务，`worker run` 才执行网络请求。来源、分页进度、预算暂停或错误可在 job 结果中查看。来源适合的领域与查询语法见[来源说明](sources.md)；并非每个来源都适合每个研究方向。

预算暂停后可执行 `job resume JOB_ID` 再运行 worker；预算按本轮请求计算。限流或临时故障也可在处理原因后续接。已经完成的发现 job 不用来刷新前沿，应发起新的发现请求，保留新的时间和查询范围。

候选由研究者或 agent 判断相关性后纳入。将下列变量换成实际选定的 paper_id，可重复 --paper 纳入多篇：

```powershell
$paperId = '实际选定的 paper_id'
& $rb --library $library paper show $paperId
& $rb --library $library collection include --collection $collectionId --paper $paperId
& $rb --library $library paper tag $paperId --tag 'evaluation' --tag 'baseline'
& $rb --library $library paper list --collection $collectionId --tag 'baseline'
```

`paper tag` 默认追加；加 `--replace` 替换标签。`collection exclude` 与 include 使用同样的 --collection 和 --paper 参数。标签用于轻量整理，不替代纳入状态。

## 用已知重要论文检查漏检

如果已经知道相关种子论文，准备一个 JSON 数组文件，例如 seeds.json：

```json
[
  {
    "title": "RAGChecker: A Fine-grained Framework for Diagnosing Retrieval-Augmented Generation",
    "arxiv": "2408.08067"
  },
  {"doi": "10.52202/085713-2087"}
]
```

通过 frontier 创建“主题检索加种子恢复”任务：

```powershell
$prepared = & $rb --library $library frontier --collection $collectionId --query 'retrieval augmented generation evaluation' --seeds .\seeds.json --source openalex --source crossref --source arxiv --date-from 2024-01-01 --max-requests 12 --broad-requests 4
$frontierJob = ($prepared | ConvertFrom-Json).data.job_id
& $rb --library $library worker run --job $frontierJob
& $rb --library $library job show $frontierJob
```

seeds.json 只是已知身份输入，程序必须从真实来源取得匹配记录，才算本轮找回。报告区分广检索直接命中、定向恢复命中与未执行或未找到；不能把定向补找计入广检索召回。--source-queries 可额外接收 JSON 对象文件，分别提供来源专属检索式。

--max-requests 是该轮总请求预算，--broad-requests 可固定其中用于广检索的份额；上例广检索使用 4 次请求，其余留给种子恢复。省略 --broad-requests 时由内核分配。进度中的 broad_recall 和 seed_coverage 分别给出两种 found/total，broad_job_id 可继续检查普通检索来源状态。

预算暂停时按正常 job resume 和 worker run 续接。仅恢复已知种子仍不足以证明找全了前沿；研究者应结合宽窄检索、时间范围和后续引用扩展检查空缺。

## 扩展基础文献和后续引用

已取得 paper_id 后，可以补查它引用的论文，或引用它的后续工作：

```powershell
$expanded = & $rb --library $library citations expand --collection $collectionId --paper $paperId --direction both --date-from 2024-01-01 --max-requests 6
$citationJob = ($expanded | ConvertFrom-Json).data.job_id
& $rb --library $library worker run --job $citationJob
& $rb --library $library job show $citationJob
& $rb --library $library paper list --collection $collectionId --membership candidate --limit 20
```

--direction citations 表示后续引用，references 表示参考文献，both 同时展开。当前是有请求预算和检查点的一跳扩展；不会自动把新候选纳入，也不会把图检索完成视作主题检索已经更新。按需阅读新项并显式 include 后，再获取全文。

## 批量获取全文

```powershell
$acquisition = & $rb --library $library acquire --collection $collectionId
$acquisitionJob = ($acquisition | ConvertFrom-Json).data.job_id
& $rb --library $library worker run --job $acquisitionJob
& $rb --library $library job show $acquisitionJob
```

省略 --paper 时为集合全部 included 项创建获取目标。组件优先尝试已有全文位置和已知 DOI、PubMed/PMC 等标识的公开全文解析。可选来源凭据或邮箱见 `.env.example`。不能直接获取时保留浏览器等待任务；扫描 PDF、版本不确定、损坏文件等有独立状态。

已有获取任务需要重新检查公开备用位置时，可显式执行：

```powershell
& $rb --library $library job retry-access $acquisitionJob
& $rb --library $library worker run --job $acquisitionJob
& $rb --library $library job show $acquisitionJob
```

该操作保留已成功原件，不抢占有效浏览器租约，也不绕过 needs_user 的登录或身份障碍。--keep-locations 可请求保留现有位置；刷新或重试并不自动放宽论文版本要求。

获取任务会记录 route_attempts，区分 known_location、metadata 和 fallback。程序先使用已有位置，未满足时才继续公开元数据和备用阶段；提供方论文页中的 PDF 元数据也要核对 DOI。需要登录的 HTML 页不作为全文入库，不能在一个位置失效后就把其他公开路径当作不可用。

若提供方明确给出正式版和预印本的关系，原有条目 ID 保留并关联。可用 `paper show PAPER_ID` 查看 related_paper_ids、实际版本和材料；可读预印本仍按预印本保存，不自动满足只允许正式版的目标。

### 宿主浏览器接续

1. 宿主执行 `browser claim --worker lab-browser --session browser-one --job JOB_ID`。有任务时返回论文身份、落地页、租约和本轮 download_dir；无任务返回 task=null。
2. 宿主使用自己的 computer-use 在已有内网会话中打开落地页并下载主文。把完成的下载复制到返回的目录；浏览器和内核必须能访问同一文件系统。
3. 按[交回示例](../examples/browser-result.json)填写实际 task_id、lease_token、submission_id、文件路径和来源。将 JSON 保存在本机忽略目录，用 `browser submit --file RESULT.json` 提交。
4. 再执行 `worker run --job JOB_ID` 完成或续接校验，并用 job show 检查材料是否入库。交回 accepted 表示核心已接收文件，不等于所有校验完成。

每个会话同时处理一项。时间较长时用 `browser renew --task TASK_ID`，租约 token 可通过 RESEARCH_BASE_LEASE_TOKEN 环境变量提供。登录、验证码或身份不确定时报告 needs_user，处理后执行 browser resume；不要无限重试。首版不自动操作浏览器，宿主步骤与协议见[接口文档](interfaces.md)。

## 浏览和读取原始材料

```powershell
& $rb --library $library paper list --collection $collectionId --membership included --limit 100 --offset 0
& $rb --library $library material list --paper $paperId
& $rb --library $library material list --collection $collectionId
& $rb --library $library search 'evaluation metrics' --collection $collectionId
```

论文列表不要求检索词，返回 total 和 next_offset。材料列表包含用途、版本、获取和解析状态、源 URL，以及可用的 local_path、text_path、pages_path。同机 agent 可用自己的文件或 PDF 工具直接读取这些路径。

材料可来自当前论文及其已确认的版本关联。根据返回的 paper_id、version_id 和状态选择原件，不仅根据首选论文条目的显示年份判断文件版本。显式关联不会使此前可用的原始路径失效。

按实际 material_id 读取 PDF 物理页码，或取得原件入口：

```powershell
$materialId = '实际 material_id'
& $rb --library $library material read $materialId --view original
& $rb --library $library material read $materialId --page 3 --page 4 --max-chars 8000
& $rb --library $library material read $materialId --page 3 --page 4 --max-chars 8000 --char-offset 8000
```

结果 truncated 为 true 时，使用返回的 next_char_offset 继续读取，并保持相同页范围；偏移是在这次选定范围的文本中计算的。不要假定每个 PDF 都有高质量提取文本。扫描、表格和复杂公式可以交给宿主读取原始 PDF。

已知论文和本地材料可以显式导入：

```powershell
& $rb --library $library paper add --metadata .\paper-metadata.json --collection $collectionId
& $rb --library $library material import --paper $paperId --file .\article.pdf
& $rb --library $library material import --paper $paperId --url 'https://github.com/author/project' --role code
```

paper-metadata.json 使用 Candidate 格式，至少含 source、source_id、title；可包含 authors、year、doi、abstract、version_kind、fulltext_urls 和材料 links。需人工确认的文件先查看实际原件，再用 `material confirm-identity MATERIAL_ID`，必要时指定 --role 和 --version。导入是复制，不执行材料中的代码。

## 导入已有书目

支持 BibTeX、RIS、CSL JSON 和 Zotero API JSON。默认作为 candidate，原有 excluded 状态不会因重复导入而自动改为 included：

```powershell
& $rb --library $library collection import --collection $collectionId --file .\references.bib
& $rb --library $library collection import --collection $collectionId --file .\references.ris --format ris --membership candidate
& $rb --library $library collection import --collection $collectionId --file .\zotero-items.json --format zotero_json
```

书目中的标签、标识和公开材料链接会被整理。书目文件指向的本地附件不会自动读取；确认要使用的 PDF 或代码材料需再显式 material import。Zotero API JSON 是文件导入，不是在线同步服务。

共享集合默认配置通过 JSON 文件显式更新：

```powershell
& $rb --library $library collection update --collection $collectionId --config .\collection-defaults.json
```

配置可包含 discovery、acquisition、freshness、context 等字段。项目局部配置不会自动回写共享集合，完整检索时间按有效查询范围匹配。

## 导出给其他 agent 或机器

```powershell
& $rb --library $library collection export --collection $collectionId --output .\.research-base\exports\rag --format bundle
```

bundle 将可用原件复制到按论文组织的目录，写入 manifest.json、papers.jsonl 和 references.bib。其他机器用 bundle_path 相对导出目录定位文件；原库的 local_path 仍只是原机器的历史位置。仅导出书目时可选 --format jsonl 或 bibtex。

## 接入研究项目规则

目标目录必须明确指定。可先预览，再安装：

```powershell
$project = '实际研究项目目录'
& $rb --library $library attach --project $project --collection $collectionId --dry-run
& $rb --library $library attach --project $project --collection $collectionId
& $rb --library $library doctor --project $project
& $rb --library $library context --project $project --task '设计与近期相关工作可比较的评估实验'
```

attach 只管理根 AGENTS.md 中自己的标记区块，保留其他规则。`--mode manual` 仅建立配置并提供说明；`detach --project PATH` 移除受管规则并保留集合与资料。用户改过受管区块时返回冲突，需先解决差异。

规则是否被加载取决于宿主。doctor 的 host_rule_loading=unverified 不能解释成已生效。绑定配置保存逻辑库身份，不保存个人磁盘路径；运行 CLI 时继续指定同一 --library，或用 RESEARCH_BASE_LIBRARY 指向它。

## 配置 DeepSeek

文献库和普通检索不需要模型密钥。需要内置 agent 时，把 `.env.example` 复制成项目根 `.env.local`，在本机填写 DEEPSEEK_API_KEY；该文件被忽略，不提交版本库。也可设置同名进程环境变量。

DEEPSEEK_BASE_URL 默认为 `https://api.deepseek.com`，DEEPSEEK_MODEL 默认为 `deepseek-flash`；可按账户可用情况覆盖。CLI 只加载当前工作目录的 .env.local，Python SDK 不隐式搜索磁盘上的密钥文件。

```powershell
& $rb --library $library agent plan '设计RAG检索与生成分别评价的文献检索计划'
& $rb --library $library agent run '查看本集合原始材料，读取与评价实验相关的章节后提出建议' --collection $collectionId --max-steps 8
```

plan 只返回建议查询，不自动执行。run 默认只读；显式 `--allow-writes` 才允许模型创建集合、发现、纳入和获取。返回 completed、step_limit 等状态应据实判断；达到步数上限不代表科研任务完成。

DeepSeek API 不能直接打开本地路径，内置 agent 使用 list_materials 和 read_material 取得内容；具有本地文件能力的宿主可直接访问原件。CLI 退出时关闭相关客户端，密钥不写入库、导出包或普通日志。

## 接入其他 agent 和 MCP

Python 接入示例：

```python
from research_base.core import ResearchBase

with ResearchBase("./.research-base/library") as base:
    papers = base.list_papers(collection_id="实际集合ID", limit=20)
    for paper in papers["papers"]:
        materials = base.list_materials(paper_id=paper["paper_id"])
        print(paper["title"], materials)
```

需要自动工具选择时，可用 ToolRegistry 取得 JSON Schema 和统一 dispatch；见[科研 Agent 接口](agent-api.md)。

MCP 使用本地 stdio，安装 mcp extra 后由宿主启动：

```json
{
  "mcpServers": {
    "research-base": {
      "command": "C:/your-project/.venv/Scripts/python.exe",
      "args": ["-m", "research_base", "--library", "C:/your-library", "mcp", "serve"]
    }
  }
}
```

路径必须换成宿主可执行的 Python 和可访问的库。本机验证生成的 `.research-base/mcp-config.json` 可作为本机宿主的参考；该文件不随仓库提供，其他机器应自行配置，不能照搬其中路径。

MCP 提供可信本机宿主需要的检索、材料、任务和浏览器交接工具。安装连接不自动安装研究规则，MCP 也不自带 computer-use。source 凭据需在宿主传给服务进程的环境中设置；仅在终端配置、未传入 MCP 进程时，服务不会自动获得它们。

MCP 或允许写入的模型 agent 遇到 paused、partial 检索任务时，调用 resume_job，再调用 run_worker，即可从已有游标续接；支持 discovery、frontier 和 citations。cancel_job 可取消未完成任务并保留已有资料。全文 acquisition 的模型工具续接使用 retry_acquisition；needs_user 由宿主解决登录或验证后调用 resume_browser_task，不能用通用模型 resume_job 绕过。

## 验证和限制

开发检查可运行 `python -m pytest -q`。本机 Windows 默认临时目录的文件替换可能受环境权限影响，可预先建立 `.research-base/`，并为每次检查指定独立的 `--basetemp=.research-base/check-N`。真实网络、模型和 MCP 检查按测试标记单独执行，不把离线模拟写成线上成功。

目前是单机组件。来源配额、限制和学科覆盖需要在实际任务中检查；OCR、完整复杂表格解析、远程多用户服务和通用无人值守浏览器工作者仍是后续能力。真实研究方向是否足够好用，按[科研用例](use-cases.md)逐环节验收，不能只以工具可调用或下载篇数判断。
