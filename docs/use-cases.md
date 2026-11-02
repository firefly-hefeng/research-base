# 科研工作流用例和验收方法

这两个用例检验 Research Base 能否把研究问题接到相关论文、原始材料和实际科研决策。重点是检索范围、全文和材料的可用性、agent 自主读取，以及是否参考当前相关研究。论文数量和模型回答长度不单独作为通过标准。

## 首轮执行状态（历史记录）

截至 2026 年 11 月 2 日，两个用例已真实执行多源检索、筛选、获取、作者 README 关联、项目接入及 DeepSeek 原文分析，并在开发完成后经过多 agent 议会审查。下面的查询计划是可重复执行的设计，实际运行采用每个查询 4 次请求的较小预算，暂停状态保留。已知种子通过定向来源查询补找，未用手动写入冒充召回。

| 用例 | 计划状态 | 尚需实际验证 |
| --- | --- | --- |
| RAG 评估实验设计 | 已执行并产出经议会修订的方案；纳入 5 篇，全文 5 篇 | 完整方向检索、其余候选筛选、实际基准实验 |
| 单细胞扰动预测的外推评估 | 已执行并产出经议会修订的方案；纳入 5 篇，全文 2 篇，其中 1 篇经真实浏览器补取 | 3 篇全文未完成：1 项需机构登录，2 项等待浏览器；实际基准实验 |

### 实际运行记录

| 项目 | RAG 评估 | 单细胞扰动 |
| --- | --- | --- |
| 集合中未纳入的 candidate | 1602 | 318 |
| 模型筛选实际输入 | 27 条本地相关性预筛与种子候选 | 27 条本地相关性预筛与种子候选 |
| 广检索种子召回 | 0/2，定向查询后找回 2/2 | 2/3，定向查询后找回 3/3 |
| 作者材料 | RAGChecker README 快照 | scPerturBench 和 STATE README 快照 |
| 项目规则 | 实际加载，启动前准备一次上下文 | 实际加载，启动前准备一次上下文 |
| 修复后模型分析 | completed，4 次工具调用 | completed，4 次工具调用 |

上述候选数不包含已纳入的 5 篇；不表示都已逐条筛选。发现任务仍有预算暂停，没有将其计为完整前沿覆盖。初次分析因冗余信息达到上下文限制；修复默认集合范围和输出后，仅重跑分析正常完成，不能将不同输入和策略的前后用量当作受控效率对比。首次流水线记录约 162 秒与 98 秒，重跑没有单独计时，不作为总耗时或人工节时结论。

本机产物位于忽略目录，新克隆项目需自行运行：

- RAG 运行记录 `.research-base/use-cases/rag-evaluation/run-result.json` 和研究方案 `.research-base/use-cases/rag-evaluation/research-plan.md`。
- 单细胞运行记录 `.research-base/use-cases/single-cell-perturbation/run-result.json`、研究方案 `.research-base/use-cases/single-cell-perturbation/research-plan.md` 及浏览器交回 `.research-base/use-cases/single-cell-perturbation/browser-result.json`。
- 可移交的原件目录清单：RAG 材料包 `.research-base/exports/rag-evaluation/manifest.json`、单细胞材料包 `.research-base/exports/single-cell-perturbation/manifest.json`。每项包含相对 bundle_path 和本机可读位置；未获取的材料保留状态。

上述均为相对于项目根的本机生成路径，不随公开仓库提供；新克隆项目不会自带这些论文、运行记录或材料包。

研究方案是 Agent 生成后由议会对照原件修订的草稿，不是未经修改的模型答案，实验尚未运行。议会纠正了 Systema 评分定义与训练特征选择的混淆，并实际读取 scPerturBench 的 CPA 更正段；详细问题与后续取舍见[议会记录](council-review.md)。

## 第二轮复跑

本轮复用下面的两个研究问题、查询计划和种子集合，检验新增的来源核实种子恢复、引用图和全文备用路线。原首轮报告与方案保持不变；最新结果、获取缺口、可复跑脚本和材料包入口见[第二轮优化记录](optimization-round2.md)。本轮不重新生成研究方案或执行科研实验，不将新增引用候选称作已经读完的前沿文献。

## 两个用例共用的执行约定

每次在独立的本地库或明确的 collection 中运行。保存实际查询、来源状态、分页/预算停止原因和返回的 job ID；`paused` 或来源限流不能记为完整检索。预算是首轮演示的请求预算，可续接，不是纳入文献的下载上限。

候选由研究者或 agent 查看标题、摘要和必要原文后决定纳入。对相关的全部纳入项发起主文获取；暂时失败项留在集合和任务状态中。外部来源只给出摘要时，不能声称已经阅读该论文的实验部分。

同机 agent 可以直接使用 `list_materials`、`get_paper` 或 `read_material(view="original")` 返回的 `local_path`、`text_path` 和 `pages_path`。通过路径阅读原件和调用读取工具同等有效，无需重复生成知识卡。跨机器使用 `export_collection(format="bundle")` 的副本及相对 bundle_path，不能把本机绝对路径当成远程可访问地址。

在首个用例中至少完成一次真实浏览器交接；若开放接口已取得全部所需主文，应选择一个尚未有直接全文的真实相关候选测试接续，或如实记录该轮未覆盖浏览器路径。不能把人工写入的 PDF 或离线样本计为浏览器下载成功。

## 用例一 为实验室 RAG 系统设计可诊断的评估

### 研究者问题

实验室准备比较 BM25、稠密检索、混合检索和重排序。如何分开判断检索质量、回答忠实性和答案覆盖，避免只用单一 LLM 总分就认定方案更好？如何选择与当前相关研究可比的任务、数据和成本指标？

本用例要求最终产出一份可执行的评估方案：任务范围、数据与分层、对照方法、指标、人工抽查和成本记录。组件不运行这些模型的完整基准实验，科研 agent 应先明确研究计划再另行执行实验。

### 种子论文和原始材料

| 工作或材料 | 作为检索与阅读入口的原因 | 原始资料入口 |
| --- | --- | --- |
| RAGChecker 2024 | 提供检索和生成的细粒度诊断思路；作为基础工作，不视作当前全部前沿 | [论文](https://arxiv.org/abs/2408.08067)、[官方 PDF](https://papers.nips.cc/paper_files/paper/2024/file/27245589131d17368cccdfa990cbf16e-Paper-Datasets_and_Benchmarks_Track.pdf)、[作者代码](https://github.com/amazon-science/RAGChecker) |
| HawkBench 2025 | 从多类信息需求与领域考察 RAG；检查只测事实问答是否限制结论 | [NeurIPS 论文页](https://proceedings.neurips.cc/paper_files/paper/2025/hash/59fe60482e2e5faf557c37d121994663-Abstract-Datasets_and_Benchmarks_Track.html)、[官方 PDF](https://proceedings.neurips.cc/paper_files/paper/2025/file/59fe60482e2e5faf557c37d121994663-Paper-Datasets_and_Benchmarks_Track.pdf)、[补充材料](https://proceedings.neurips.cc/paper_files/paper/2025/file/59fe60482e2e5faf557c37d121994663-Supplemental-Datasets_and_Benchmarks_Track.zip) |
| TREC RAG 2026 | 官方任务、开发材料和评估工具可作为近期实验设计参照；与论文资料分别标记类型 | [官方项目](https://trec-rag.github.io/)、[RAGDoll 仓库](https://github.com/castorini/RAGDoll) |

这些入口用于检查召回和材料可读性，不能代替围绕研究问题的发现检索。TREC 项目页属于持续更新的项目材料；评估时间、数据版本和代码提交应在实际使用时记录，不把不同年份的基准直接混为同一设置。

### 首轮检索计划

以下 JSON 中每个 searches 条目可直接作为 discover_papers 的参数补上实际 collection_id。前一条覆盖基础工作，后一条优先发现近期更新；两次查询都需保留自己的范围和完成状态。

```json
{
  "case_id": "rag-evaluation",
  "collection_name": "RAG evaluation and diagnostics",
  "research_task": "为实验室RAG系统设计可诊断的评估：分别衡量检索、生成忠实性、答案覆盖、不同信息需求和运行成本，并与近期工作比较。",
  "searches": [
    {
      "query": "retrieval augmented generation evaluation benchmark",
      "sources": ["openalex", "crossref", "arxiv", "semantic_scholar"],
      "source_queries": {
        "arxiv": "all:\"retrieval augmented generation\" AND (all:evaluation OR all:benchmark)"
      },
      "date_from": "2024-01-01",
      "date_to": "2026-11-02",
      "budget": {"max_requests": 16}
    },
    {
      "query": "RAG evaluation retrieval generation benchmark",
      "sources": ["crossref", "arxiv", "semantic_scholar"],
      "source_queries": {
        "arxiv": "all:RAG AND (all:evaluation OR all:benchmark)"
      },
      "date_from": "2025-01-01",
      "date_to": "2026-11-02",
      "budget": {"max_requests": 12}
    }
  ],
  "seed_checks": [
    {"title": "RAGChecker: A Fine-grained Framework for Diagnosing Retrieval-Augmented Generation", "arxiv": "2408.08067"},
    {"title": "HawkBench: Investigating Resilience of RAG Methods on Stratified Information-Seeking Tasks", "doi": "10.52202/085713-2087"}
  ]
}
```

### 筛选 获取和自主阅读

1. 使用 `paper list --membership candidate` 分页浏览候选；优先纳入直接讨论 RAG 评估、检索与生成诊断、数据构建、LLM 评判可靠性或任务分层的工作。只有把 RAG 用于某个应用而没有评估方法贡献的文章，可视研究需要排除或另标用途。
2. 对全部纳入项调用 acquire_papers，再运行 worker。取得的预印本和正式版保留版本身份；未获取项经过浏览器领取、下载、交回和校验，不悄悄从集合删除。
3. agent 先从 prepare_context 获取入口，再通过原始 PDF 路径、分页文本或读取工具查看任务定义、指标、数据构造与局限。涉及评估实现时登记作者仓库，并阅读 README 和所需评估代码；补充 ZIP 可由研究者或宿主下载后导入关联。
4. 核对近期工作是否改变数据覆盖、任务类别或评估流程。区别“检索结果好”“生成回答好”“指标与人类判断一致”三类问题，不把它们合成一个未经解释的排名。
5. 将 collection 接到试点研究项目的根规则，再提出“只比较答案总分是否足够”的追问，验证 agent 会回到资料而不是只凭常识作答。

### 可验收结果

- 至少两个来源返回可用结果，所有来源的失败或限流可见。检查种子召回；漏检时保留事实，允许以已知元数据补入，但不能把补入算作召回。
- 全部纳入项均有全文获取结果或明确等待/失败状态。至少两篇相关论文的原件可通过绝对路径打开；至少一篇近年工作进入实质比较，不能只按日期列标题。
- agent 实际读取了方法或评估章节，以及至少一个作者代码/补充材料入口。必要的关键材料缺失时，缩小结论范围。
- 评估方案分别说明检索指标、生成指标、任务分层、人工复核和成本；说明这些选择与所读工作的关系，不虚构已有实验结果。
- attach 重复执行不新增区块，已有规则保留；追问触发了上下文查询和必要材料读取。可由工具调用和产生的研究方案检查，无需逐篇阅读证明。

## 用例二 为单细胞扰动预测设计可信的外推评估

### 研究者问题

实验室想比较单细胞基础模型和简单预测基线。目标是在未见扰动或未见细胞背景下预测响应。应该如何划分训练测试、选择基线和指标，避免将重构能力、数据泄漏或平均表达相似性误判成有效的生物学外推？

本用例最终产出数据划分、基线、指标、代码版本与执行顺序的研究计划，不要求在文献组件验收中下载全部细胞数据或训练大型模型。

### 种子论文和当前代码材料

| 工作或材料 | 作为检索与阅读入口的原因 | 原始资料入口 |
| --- | --- | --- |
| scPerturBench | 跨细胞背景和扰动的评估工作，可检查划分、基线和度量的适用范围；PubMed 标明 2025 年 12 月在线、2026 年 2 月卷期 | [PubMed 元数据](https://pubmed.ncbi.nlm.nih.gov/41381899/)、[论文](https://doi.org/10.1038/s41592-025-02980-0)、[作者仓库](https://github.com/bm2-lab/scPerturBench) |
| State | 作为面向不同细胞背景的模型与实现入口，需阅读其评估设置后再决定能否与目标任务比较 | [论文](https://www.biorxiv.org/content/10.1101/2025.06.26.661135)、[作者代码](https://github.com/ArcInstitute/state) |
| Systema | 用于追踪超出系统性变化的扰动预测评价问题；必须取得并阅读相关部分后再采用其方法判断 | [论文](https://doi.org/10.1038/s41587-025-02777-8) |

一个具体的材料更新检查是：scPerturBench 官方仓库 README 的“Notice on CPA results in the genetic perturbation benchmark”说明旧 CPA 结果存在问题，相关汇总已移除，脚本已修正并计划重跑。实际使用时应重新查看该段及代码版本，不能仅凭论文摘要或旧表格给出 CPA 排名。[作者说明](https://github.com/bm2-lab/scPerturBench#notice-on-cpa-results-in-the-genetic-perturbation-benchmark)

### 首轮检索计划

```json
{
  "case_id": "single-cell-perturbation",
  "collection_name": "Single-cell perturbation generalization",
  "research_task": "为单细胞扰动响应预测设计未见扰动和未见细胞背景的评估，比较基础模型与简单基线，检查数据泄漏、指标适用范围及当前代码更新。",
  "searches": [
    {
      "query": "single cell perturbation response prediction benchmark",
      "sources": ["pubmed", "europe_pmc", "crossref", "openalex", "semantic_scholar"],
      "source_queries": {
        "pubmed": "(\"single-cell\"[Title/Abstract] OR \"single cell\"[Title/Abstract]) AND perturbation[Title/Abstract] AND (prediction[Title/Abstract] OR benchmark[Title/Abstract])",
        "europe_pmc": "\"single cell\" AND perturbation AND (prediction OR benchmark)"
      },
      "date_from": "2024-01-01",
      "date_to": "2026-11-02",
      "budget": {"max_requests": 20}
    },
    {
      "query": "single cell perturbation generalization evaluation baseline",
      "sources": ["pubmed", "europe_pmc", "arxiv"],
      "source_queries": {
        "arxiv": "all:\"single cell\" AND all:perturbation AND (all:generalization OR all:benchmark)"
      },
      "date_from": "2025-01-01",
      "date_to": "2026-11-02",
      "budget": {"max_requests": 12}
    }
  ],
  "seed_checks": [
    {"title": "Benchmarking algorithms for generalizable single-cell perturbation response prediction", "doi": "10.1038/s41592-025-02980-0", "pmid": "41381899"},
    {"title": "Predicting cellular responses to perturbation across diverse contexts with State", "doi": "10.1101/2025.06.26.661135"},
    {"title": "Systema: a framework for evaluating genetic perturbation response prediction beyond systematic variation", "doi": "10.1038/s41587-025-02777-8"}
  ]
}
```

### 筛选 获取和自主阅读

1. 区分预测未见扰动、未见细胞背景、组合扰动与普通表达重构。纳入直接涉及目标泛化任务、基线或评价方法的论文；仅讨论通用细胞表征、没有目标任务证据的工作不自动作为更强预测方法。
2. 对纳入项尽量取得全文。PubMed 与 Europe PMC 的重叠记录应按标识归一，不能当作两篇独立研究；正式发表年、在线日期和预印本版本分别保留。
3. 先阅读主文中的数据划分、实验假设与基线，再查看对应补充表、预处理和评价代码。登记 scPerturBench 和 State 的代码入口，由宿主按需要获取文件；记录实际阅读的代码版本。大型数据与容器先保存入口，按研究需要另行获取。
4. 验证评估时输入的是对照细胞还是测试扰动细胞，检查训练与测试的细胞背景、扰动和预处理边界。对于旧 CPA 汇总的变更，查当前作者说明及脚本，不把无法复核的历史排名带入方案。
5. 将 collection 接到实际研究目录，要求 agent 面对“相关系数更高是否就代表更强泛化”的追问时，结合已读材料给出条件、基线和待验证实验。

### 可验收结果

- 至少一个生物医学专业来源和一个通用来源返回相关结果，来源重叠、限流与失败可见；种子漏检如实记录。
- 所有纳入项有全文获取状态。至少两篇与目标任务直接相关的论文原件可读；仅摘要的工作不用于具体实验细节判断。
- agent 能从材料列表自主选择论文、补充材料和当前代码入口；实际比较至少两种不同泛化设置，并指出它们不能直接互换。
- 最终研究计划明确测试划分、简单基线、指标与预处理边界。涉及 CPA 旧结果时反映当前作者说明，或明确尚未完成核对；不得照抄过时排名。
- 局部规则接入后，新的分析任务会查询集合并读取必要材料。文本提取失败时可以打开 PDF，不能把“不可检索”误写成“没有全文”。

## 实际运行结果应怎样补充

每次真实运行只需记录使用的库/集合、执行日期、查询和 job ID、逐来源状态、候选与纳入数量、全文可用/等待/失败数量、原件目录或 bundle 位置，以及研究方案的实际产物位置。浏览器接续和 agent 分析分别标记是否真实执行。

若来源全部不可用、相关论文未能取得、agent 未实际读取材料或规则未被宿主加载，应将该环节标为未通过或未覆盖，并保留其余成功结果。调整来源、补充已知材料或换宿主后可续测，但需要保留哪些结果来自检索、哪些来自显式补入的区别。
