# 文献来源适配器

当前提供九个可独立选择、分页和报告错误的开放来源。ACL Anthology 需显式指定官方集合与原生查询，默认通用检索仍选择其余八个来源。多来源能补充索引与学科覆盖，但结果存在重叠；来源数量不等于完整覆盖率。检索结果保留实际来源及标识符，交由文献库去重。

| 规范名称 | 覆盖与用途 | 分页和注意事项 |
| --- | --- | --- |
| `openalex` | 综合学科，作品、引用关系、开放全文位置 | 无密钥用普通页码，最多前 10000 条；配置 OPENALEX_API_KEY 后用游标；部分缓存全文也需要密钥和用量预算 |
| `crossref` | DOI 注册元数据，适合跨出版商发现与补全 | 游标；显式按相关性 score 降序排列，发表时间用于范围过滤；PDF 链接不保证可直接开放访问 |
| `arxiv` | 物理、数学、计算机等预印本，明确版本编号 | 偏移；时间过滤为提交日期，保留与期刊发表时间的区别 |
| `europe_pmc` | 生命科学与生物医学，含 PubMed 等来源及开放全文入口 | 游标；也接受 `europepmc` 别名，返回和清单始终使用规范名称 |
| `pubmed` | 生物医学文摘与主题检索 | ESearch 加 EFetch；一页会产生两次 HTTP 请求，单查询最多暴露前 10000 个结果 |
| `semantic_scholar` | 跨学科学术图谱、开放 PDF 和外部标识关联 | bulk token；服务方每批最多 1000 条，不支持指定小批量，返回完整批次防止丢失数据 |
| `doaj` | 多语言开放获取期刊论文，补充地区性期刊 | 页码；按官方 v4 定义每页最多 50；保留期刊国家、语言和原始全文入口 |
| `datacite` | 仓储中的预印本、文本出版物、报告和学位论文 | 相关性排序及页码；前 10000 条上限显式报告；仅 publication 类型 |
| `acl_anthology` | 明示 ACL/EMNLP/EACL/Findings 等官方会议集合的题名与摘要 | XML 快照 SHA256 绑定续接；需原生 JSON 查询；月份/年份范围不冒充精确发布日期 |

PubMed 和 Europe PMC 有大量重叠，OpenAlex、Crossref、Semantic Scholar、DOAJ 和 DataCite 也并非彼此独立的文献集合。中文专门数据库和部分专业会议的覆盖仍需后续来源补充。HAL 已核对官方接口，但当前网络环境连接失败，因此暂不列入可用来源。

## 接口和扩展

`SourceRegistry` 提供 `list_sources()`、`search_page(name, query, cursor, page_size, date_from, date_to)` 和 `register(name, adapter)`。Python 调用中分页参数使用关键字。结果是共享模型 SourcePage，包含 Candidate、下一页游标、来源估计总数及限制提示。

自定义适配器提供 info 字典和 search 方法，返回同一 SourcePage。HTTP 客户端可注入，便于离线测试与调用方管理连接。registry 自己创建的客户端由 close 或上下文管理器关闭，注入客户端的生命周期归调用方。

page_size 是单页期望大小，各来源限制见表。Semantic Scholar bulk 返回服务方完整批次，不在适配器中截掉多余结果；否则继续 token 会让被截数据永远丢失。调用方可以分批处理收到的结果。

request_count 统计实际 HTTP 尝试数，包括 PubMed 的两步请求和瞬时错误重试。调用方在页边界检查预算时，最后一页可能略超出请求预算，应报告实际计数。

ACL 的原生查询例如 `{"collections":["2025.acl","2025.findings","2026.acl","2026.emnlp","2026.findings"],"any":["retrieval augmented","retrieval-augmented","RAG"],"all":[]}`，经 source_queries.acl_anthology 传入，并显式选择该源。`any` 是任一词组、`all` 是全部词组；采用词边界匹配。Findings 的官方文件是 year.findings，acl/emnlp 属于其中的卷，不能猜成 year.findings-acl 文件。

每个集合首次请求都获取当前官方 XML，后续页绑定同一 SHA256；跨进程重取后内容变化会返回 SOURCE_SNAPSHOT_CHANGED，要求另建检索，不假装无缝穷尽。失败、缺失、错误集合和不完整 XML 不当作空结果。单文件最多 25MB，缓存有界。适配器 request_count 可观察真实网络尝试；持久 job 的 requests 历史字段按至少一个预算单位计每次来源分页，因此缓存 ACL 页仍消耗预算单位，不能把它直接当成 HTTP 数。

OpenAlex 当前无密钥时仅允许游标第一页，继续远程游标会返回 401。新任务因此使用官方支持的 basic paging；保存的页码状态固定单页大小，不能换大小续读。到 10000 条后返回 SOURCE_RESULT_LIMIT，不能报告已穷尽结果。旧远程游标仍需 OPENALEX_API_KEY，或另建无密钥检索任务；不会静默跳回第一页。见[官方分页说明](https://help.openalex.org/api/paging/)与[认证说明](https://help.openalex.org/api/authentication/)。

## 日期 身份和失败处理

支持 ISO 日期起止过滤，各来源使用自己的公开语法。arXiv 使用 submittedDate，PubMed 使用 pdat，Europe PMC 使用 FIRST_PDATE，其他来源使用发表日期过滤。不能把这些时间口径当成完全一致。

DOAJ 的 bibjson.year 和 DataCite 的 publicationYear 只能按年筛选，适配器在 warnings 明确标注不能保证月日边界。DataCite 使用官方支持的 resource-type-id 过滤，再防御性检查响应 resourceTypeGeneral，只接受 Text、Preprint、JournalArticle、ConferencePaper、Report 和 Dissertation；Dataset、Software 等不进入 Candidate 或论文计数。候选 extra 标明 resource_kind=publication 和实际类型。后续数据集或软件搜索应使用独立资产接口。

Crossref 使用 query.bibliographic，并显式设置 sort=score、order=desc。这样有限请求预算首先得到更相关的候选，而不会因游标查询的隐式排序把仅匹配个别词的新论文优先返回。日期上下界仍独立生效；相关性排序不等于按最新排序，也不替代纳入前的主题筛选。

PubMed 保留调用方检索式和 NCBI 自动词语映射语义，不擅自把所有词强制改成 AND 或精确短语。适配器将 ESearch 返回的 querytranslation 和 warninglist 放入 SourcePage.warnings，调用方可在 job 来源提示中观察是否发生了同义词、MeSH 或 OR 扩展。对于非医学专有短语，可由研究者或 agent 显式通过 source_queries.pubmed 提供带 Title/Abstract 标签、引号和布尔运算的检索式；此选择应有意识地权衡召回和相关性。

arXiv 元数据中的期刊 DOI 通常指向正式版。适配器将其放在 Candidate.extra.published_doi，保持预印本 Candidate.doi 为空并保留带版本的 arXiv 标识，避免把预印本与正式版误当成同一版本。其他跨源标识冲突由库的身份管理层处理。

arXiv 初投与版本更新时间、Europe PMC 首次发表/入库/更新日期及 ACL 发表月份按原来源保留。get_paper.publication_history 提供有来源、版本和观察时间的轻量日期记录，不把索引更新当发表、把 2026 修订当初投，或从 DOI 内数字猜发布日期。

DataCite 的 10.48550/arXiv.* DOI 则标识预印本自身：保留这个 DOI，同时关联 arxiv 标识并标记为 preprint，即使资源通用类型写作 Text 也不把它认作正式发表版。对于其他仓储文献，无法确认版本时保持 unknown。

遇到限流返回 SOURCE_RATE_LIMITED 和 Retry-After，交给调度层决定续接时间；不在适配器中长时间阻塞。网络与服务端瞬时故障最多重试三次，默认遵守每来源的最小请求间隔。HTTP 错误中不包含请求 URL、响应正文或凭据。

PubMed 超过 10000 条的结果在到达上限时返回 SOURCE_RESULT_LIMIT，不能把它当成已经穷尽文献。应缩小或划分日期/主题范围后另行检索；已返回的记录保持有效。Semantic Scholar bulk 本身也受提供方的总结果量限制，若服务方终止分页，不承诺超出服务限制的覆盖。

可选配置为 OPENALEX_API_KEY、SEMANTIC_SCHOLAR_API_KEY、NCBI_API_KEY 和 NCBI_EMAIL，均从运行环境读取。没有密钥仍可请求公开检索，但可用额度及限流由服务方决定。

## 依据和测试

- [OpenAlex API](https://help.openalex.org/api/) 和 [全文说明](https://help.openalex.org/access/fulltext/)。
- [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) 和 [Crossref 官方排序参数说明](https://github.com/CrossRef/rest-api-doc#sorting)。
- [arXiv API 手册](https://info.arxiv.org/help/api/user-manual.html)。
- [Europe PMC REST 服务](https://europepmc.org/RestfulWebService)。
- [NCBI E-utilities](https://dataguide.nlm.nih.gov/eutilities/utilities.html) 和 [PubMed 搜索上限说明](https://ncbiinsights.ncbi.nlm.nih.gov/2021/10/05/updated-pubmed-api/)。
- [Semantic Scholar API](https://api.semanticscholar.org/api-docs/graph)。
- [DOAJ 官方 API 定义](https://doaj.org/api/v4/swagger.json)。
- [DataCite 查询和过滤](https://support.datacite.org/docs/api-queries) 与 [分页规则](https://support.datacite.org/docs/pagination)。
- [ACL Anthology 数据与开发](https://aclanthology.org/info/development/)及其[官方 XML 数据目录](https://github.com/acl-org/acl-anthology/tree/master/data/xml)。

tests/test_sources.py 与 tests/test_acl_source.py 使用 HTTP 模拟样本检查来源归一、日期参数、分页、版本关联、快照变化、真实请求计数、限流和错误脱敏。实时服务结果与网络环境另作显式冒烟检查，不把实时排序和特定论文数量写成稳定测试断言。

新增来源另有 DOAJ 多语言期刊、两页游标、年粒度日期、DataCite 排除数据集及预印本归类等检查。2026 年 11 月 2 日实际通过 DOAJ 与 DataCite 各两页检索，记录与优先级评审见[来源议员评审](council-sources.md)。
