# 检索与缓存优化实施计划

## 问题

- 检索只有"本地命中"或"调用付费的 DeepSeek 原生网页搜索"两条路：同一个查询反复搜会反复计费，也没有结果缓存。
- 下载域名白名单只列了 37 个域名，中文期刊官网、J-STAGE、SciEngine、PMC（`pmc.ncbi.nlm.nih.gov`）等都会被拒。
- 检索提示词偏向计算机领域（arXiv/OpenReview/ACL），化学、环境、生物医学的召回差。
- 索引文件用 `write_text` 直接覆盖，写坏即全部元数据丢失；PDF 下载却用了原子写，两者不一致。
- 译文 JSON 没有版本号，缓存命中只看文件是否存在：切分逻辑升级后旧译文仍会被当作最新结果。
- 缓存键由 URL 派生，同一篇论文的 arXiv abs／PDF／镜像／本地上传会各存一份；下载文件名只取 URL 末段，不同论文可能撞名。
- 没有任何清理入口，`data/` 只增不减。

## 检索

- 新增 `backend/search_cache.py`：SQLite 缓存检索结果，键为"规范化查询 + 条数"，默认 TTL 168 小时（`PAPER_SEARCH_CACHE_TTL_HOURS=0` 可关闭），记录命中次数，`POST /api/search-cache/clear` 可清空。
- 新增四个免费学术 API 提供方：arXiv Atom、Crossref、OpenAlex、Semantic Scholar（`backend/paper_search.py`），与 DeepSeek 原生搜索并行合并；任一方失败只记录 `providerErrors`，不影响其它结果。
- `discover_papers` 顺序：arXiv ID 直连 → 本地缓存 → 检索缓存 → 学术 API + DeepSeek → 合并打分 → 只对前 `PAPER_SEARCH_VERIFY_LIMIT` 个开放 PDF 做一次 Range 校验。
- 去重键升级为 DOI / arXiv（含 `10.48550/arxiv.*` 这类 DOI）→ 归一化标题；同分时优先有开放 PDF、优先知名学术域名。
- 下载策略：公开 HTTPS 域名一律允许（仍然拒绝回环、内网、云元数据地址、带凭据 URL 与盗版镜像站），并要求 `%PDF` 魔数；网页搜索返回的未知域名只在"看起来是 PDF 直链"时才采纳，避免噪声。
- 中文查询增加字符二元组相似度，避免整句被当成一个词。
- 可选 Unpaywall 补充：设置 `PAPER_SEARCH_CONTACT_EMAIL` 后，没有开放 PDF 的 DOI 会查一次 Unpaywall，把合法的开放获取链接补进候选（Unpaywall 拒绝占位邮箱，所以默认留空跳过）。

### 实测（沙箱内已验证的部分）

- Crossref 对计算机与化学论文都能精确命中：`Quantized Side Tuning` → `doi:10.18653/v1/2024.acl-long.1`（分数 1.00）；朋友的 IBB 2015 → `doi:10.1016/j.ibiod.2014.09.002`（0.90）。
- 检索缓存生效：同一查询第二次返回 `cached=True`，耗时从 ~8–22 秒降到 ~0.001 秒。
- 单个提供方失败（示例环境里 arXiv TLS 握手失败、OpenAlex/S2 返回 429）只写入 `providerErrors`，不影响其它来源的结果。
- 中文查询在 Crossref 里召回有限（中文题录少），需要 DeepSeek 网页搜索或中文库补充，这是已知限制。

## 缓存

- `save_index` 改为原子写并保留 `paper_index.json.bak`；`load_index` 在主文件损坏时自动回退备份并自愈重写。
- 译文 JSON 新增 `generatorVersion`（`scripts/generate_translation_json.py` 的 `GENERATOR_VERSION`），索引同步记录；`/api/papers`、缓存命中的 `/api/generate` 返回 `structureStale`，前端提示"结构可更新"并自动勾选"强制重新生成"。
- 下载文件名冲突保护：同名文件属于别的 `sourceUrl` 时，文件名追加 URL 的 8 位哈希，避免读到别人的 PDF。
- 新增 `GET /api/storage`（按类别统计占用、论文数、检索缓存命中）与 `DELETE /api/papers/{paper_id}`（删除 PDF、译文、LaTeX 源码、图片资源、检查点、问答索引与会话，并清理索引条目）。
- 查看器新增"删除缓存"按钮、占用统计行与"清空检索缓存"按钮。

## 验证

- 新增测试：`tests/test_paper_search.py`（提供方解析、跨源去重、缓存命中、缓存关闭、白名单策略、PDF 校验上限）、`tests/test_paper_cache.py`（索引损坏恢复、版本过期标记、撞名保护、`/api/storage`、删除接口、问答索引清理）。
- 结论：`python -m unittest discover -s tests` 全绿（73 个 Python 用例 + 视图 4 个用例），检索与缓存行为在离线环境下由 mock 覆盖。
