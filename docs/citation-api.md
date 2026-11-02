# 种子定位和引用扩展接口

ScholarlyClient 提供不依赖本地库的真实来源查询。种子只是检索条件，只有提供方实际返回且通过身份匹配的记录才成为 Candidate。内核负责将这些候选写入集合；该客户端不会把用户输入直接当作论文记录。

## 种子定位

```python
from research_base.scholarly import ScholarlyClient

service = ScholarlyClient(http_client)
result = service.lookup({"arxiv": "2408.08067"})
# result: candidates 为 Candidate 对象列表，另有 matched 与 attempts。
```

支持 DOI、arXiv、PMID、OpenAlex ID，或只有标题的种子。默认尝试 OpenAlex、Crossref 和 arXiv；sources 如显式传入，只接受这三个名称。PMID 通过 OpenAlex 的标识查询定位，不声称直接调用 PubMed。

标识种子只能由真实匹配的标识或提供方明确的发表关系命中，不能用相似标题替代。只有标题的种子要求完整归一标题一致，过短通用标题不作为身份依据；可选作者、年份进一步约束。arXiv 原始记录的发表 DOI 放在 extra.published_doi，避免把预印本当作正式版。

10.48550/arXiv 形式的 DOI 作为 arXiv 命名空间别名处理：提供方真实返回相同 arXiv 标识时，允许其 canonical DOI 为正式发表 DOI，返回记录仍保留实际 DOI。普通发表 DOI 冲突不会被这一别名规则忽略，显式 arXiv 修订号也必须匹配。

## 引用扩展

```python
result = service.neighbors(
    {"identifiers": {"openalex": "W4417297892"}},
    direction="citations",
    page_size=25,
    date_from="2024-01-01",
)
page = result["page"]
```

- citations 查询哪些工作引用种子，使用 OpenAlex 的 cites 过滤并按发表时间降序返回。
- references 读取种子的 referenced_works，按实际 OpenAlex ID 批量取回记录，不用近似标题搜索代替引用关系。
- 返回 SourcePage、provider 和逐步骤 attempts。requests_count 累计真实 HTTP 尝试数，包括重试和种子定位，供调用方预算管理。
- next_cursor 是自包含的继续读取状态，绑定种子、方向、日期和页大小。换查询不能复用；后向参考列表变化或提供方拒绝旧游标时明确报错，不能当作已读完。
- 候选保持原提供方标识、全文入口与可用的版本类型；引用关系不代表研究相关性，纳入和全文获取仍是后续步骤。

OpenAlex 是工作层级的引用索引，不提供每个预印本修订版各自完整的引用图。indexed references 为空不能推出论文 PDF 没有参考文献；数据集等非论文记录会被排除，SourcePage.total 仍反映提供方的索引计数。日期过滤后的后向总数未知时返回 null。

### 公开分页和用量边界

没有 OPENALEX_API_KEY 时，前向引用使用官方支持的 page/per_page 基础分页，继续状态保存 mode=basic 和页码；请求页超出公开 10000 条结果窗口时返回 SOURCE_RESULT_LIMIT，不能据此标记已读完。有 key 时使用 provider cursor，并保存 mode=cursor；旧 cursor 缺少 key 时返回 SOURCE_AUTH_REQUIRED，需设置 key 或重启为 basic，不能把两种位置默默混用。[官方分页](https://help.openalex.org/api/paging/)、[认证说明](https://help.openalex.org/api/authentication/)

客户端通过 requests_count 报告包含重试的实际 HTTP 次数，现行预算由工作流在种子或引用页边界检查，属于批次软边界。一次种子后备定位或引用页可包含多个请求，尚未增加半个请求序列的内部恢复游标；调用方必须记录实际用量和超出值，不能把它描述为逐 HTTP 请求硬上限。

## 验证

离线测试覆盖精确标识、完整标题、版本关系、错误包、请求计数、凭据隔离、引用方向、分页和游标失效。2026 年 11 月 2 日的小型真实验证取得：RAGChecker 和 HawkBench 均由提供方定位，三篇种子的前向引用可读；scPerturBench 的后向参考也可分页取得。RAG 两篇种子的 OpenAlex 参考数组当时为空，该缺口保留显示。

## 官方依据

- [OpenAlex API recipes](https://help.openalex.org/how-to/api-recipes/) 给出 cites 前向查询和 openalex OR 后向批量 ID 查询。
- [OpenAlex 引用数据说明](https://help.openalex.org/data/works/citations/) 解释参考匹配与索引遗漏，不能将索引视作 PDF 完整书目。
- [OpenAlex 排序](https://help.openalex.org/api/sorting/) 给出 publication_date 排序方式。
- [arXiv API 手册](https://info.arxiv.org/help/api/user-manual.html) 定义 id_list、Atom 响应和版本标识。
- [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) 提供 DOI 记录与标题查询入口。
