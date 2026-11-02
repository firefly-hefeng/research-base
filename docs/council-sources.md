# 来源和开源复用评审

日期：2026 年 11 月 2 日。范围：来源覆盖、获取稳定性、可复用开源工具。本轮新增 DOAJ 和 DataCite Publications，均完成小规模真实分页验证；HAL 暂未通过当前环境连接验证，未加入可用来源数。

## 开源工具的使用优先级

| 优先级 | 官方项目和已核对的做法 | 对 Research Base 的建议 |
| --- | --- | --- |
| P0 | [OpenAlex 官方 CLI](https://github.com/ourresearch/openalex-official) 支持显式 ID/DOI、PDF/XML、检查点续传、限流和本地输出 | 大批量 OpenAlex 获取优先提供可选外部 CLI 后端；导入它的原件与元数据，保持本库身份和目录接口统一。当前未集成该后端，不将其文档性能当成本项目实测 |
| P0 | [Zotero translators](https://www.zotero.org/support/dev/translators) 区分网页、导入、导出和标识符查找；[Embedded Metadata 源码](https://raw.githubusercontent.com/zotero/translators/master/Embedded%20Metadata.js) 处理 citation_pdf_url 等标准元数据 | 普通落地页先解析明确的出版商元数据和 PDF 入口，再交浏览器。按站点建立有测试样本的解析器；需要完整 translators 时优先接现有 Zotero 环境，而非直接复制其运行时和代码 |
| P1 | [Pubget](https://github.com/neuroquery/pubget) 面向 PMC，提供 CLI/Python API；[下载源码](https://raw.githubusercontent.com/neuroquery/pubget/main/src/pubget/_download.py) 保存输入、批次、History Server 状态，并区分完成和部分完成 | 生物医学批量全文可选接 Pubget/PMC XML。保留 XML 原件供 agent 直接使用，解析步骤与获取完成分离；不为通用文献库引入神经影像分析功能 |

上述项目的主要启发是复用已有下载路径和元数据解析能力，同时保留短小、可替换的接入层。本轮只实现来源适配器，没有宣称安装或集成上述外部工具。若后续直接引入代码，先按各项目许可证确定复用方式。

## 本轮来源选择

DOAJ 补充多语言开放期刊与地区性期刊。真实查询返回尼日利亚、英国、埃及和美国期刊的文章；这是检索到的具体样本，不能推断所有地区覆盖完整。适配器使用官方 v4 定义的查询路径、页码及每页 50 条上限，保留期刊国家、语言和全文链接。[DOAJ 官方接口定义](https://doaj.org/api/v4/swagger.json)

DataCite 仅接入 publication 模式，使用资源类型过滤并在响应端排除 Dataset、Software 等。保留原始资源类型，预印本不会标成正式版；数据资产不混入论文计数。默认按相关性使用页码分页，官方上限为前 10000 条，越界返回限制错误；需要更广范围时缩小检索式或划分年份。官方 cursor 模式支持更深分页，但排序固定为创建时间，因此暂不悄悄替换当前相关性策略。[DataCite 查询](https://support.datacite.org/docs/api-queries)、[分页](https://support.datacite.org/docs/pagination)

HAL 对欧洲机构库及人文社科有潜在补充价值，官方接口支持查询、字段过滤和分页。此次 `api.hal.science` 与 `api.archives-ouvertes.fr` 的 HTTPS 请求均为 ConnectError，旧 HTTP 入口返回 502。因此当前只保留为待复核候选，不以离线样本冒充已经稳定接通。[HAL 官方 API](https://api.archives-ouvertes.fr/docs/search)

## 真实验证记录

以下请求未使用模型密钥。每个已接入来源仅取两页、每页两条，用来验证契约与记录身份，没有下载整库。

| 来源 | 查询和范围 | 实际结果 |
| --- | --- | --- |
| DOAJ | `bibjson.title:"machine learning"`；出版年 2024 至 2026 | 两页各 2 条，游标分别为 2 和 3，文章 ID 不重叠；记录包含 DOI、期刊国家和语言 |
| DataCite Publications | `titles.title:retrieval`；出版年 2024 至 2026 | 两页各 2 条，下一页为 2 和 3；均为预印本，正确关联 arXiv PDF 入口 |
| HAL | retrieval augmented generation；新旧官方域名 | HTTPS 连接失败，旧 HTTP 返回 502；未注册 |

DOAJ 样本包括 DOI [10.46481/jnsps.2025.2273](https://doi.org/10.46481/jnsps.2025.2273) 和 [10.21608/jesaun.2024.326709.1374](https://doi.org/10.21608/jesaun.2024.326709.1374)。DataCite 第二页包括 [Unified Active Retrieval for Retrieval Augmented Generation](https://doi.org/10.48550/arxiv.2406.12534) 和 [Evaluating Retrieval Quality in Retrieval-Augmented Generation](https://doi.org/10.48550/arxiv.2404.13781)。这里确认的是真实检索与分页，不代表这些文章全文均已下载。

## 后续评审检查点

1. 对每个新增来源检查实际查询、第二页、日期语义、标识符、主文入口和资源类型，而非只验证 HTTP 200。
2. 按研究方向检查前几页相关性与新增独有记录，避免只增加高度重叠的来源。PubMed 的 querytranslation 与来源限制提示应保留可见。
3. 复查重复失败的来源、分页上限和已过期游标；故障来源保留状态并隔离，不阻断其他来源。
4. 先补出版商落地页元数据解析和 PMC XML 等可复用获取路径，再扩大浏览器站点维护范围。
5. 中文专门数据库仍是覆盖缺口，需要实际可访问的开放接口或实验室浏览器路径，不据现有八来源宣称完整覆盖。
