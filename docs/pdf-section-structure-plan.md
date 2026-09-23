# 无 LaTeX 期刊 PDF 的章节切分方案

## 问题

arXiv 上的计算机论文几乎都带 LaTeX 源码，章节结构可以直接读源码；化学、材料、环境、生物医学等期刊只提供排版好的 PDF，用户反馈了三个具体现象：

- 章节分类不灵敏，`Introduction` 之类的标题被并进了摘要段落。
- 页眉被当成正文标题，比如把页面顶端的 “2.1 …” 识别成章节。
- 分页处不好检测，跨页的段落被拆成两段，页码也对不上。

## 根因

- PDF 没有语义结构，只能靠排版反推；旧规则只认三类标题：`abstract/references/acknowledgements/appendix`、`A. Title`、`1.1 Title`，期刊常见的无编号标题一律漏掉。
- 旧的前置内容跳过逻辑依赖一行能被 `^abstract` 匹配的标题，遇到拆字排版（`a b s t r a c t`）或没有 Abstract 标题的期刊，会把整篇正文一起丢掉。
- 没有任何“页面边缘重复行”的概念，页眉、期刊脚注、DOI、页码行只要碰巧像标题就会新建章节。
- 段落按页切分，跨页续写的段落被拆成两段，页码只保留起始页。

## 实现

### 标题候选

- `JOURNAL_SECTION_NAMES` + `SECTION_NAME_HEADING_RE`：识别 `Introduction`、`Experimental Section`、`Materials and Methods`、`Results and Discussion`、`Conclusions`、`Supporting Information` 等无编号标题，允许前缀数字、罗马数字或单字母编号，允许全大写。
- `is_section_heading()` 统一判断，供分行、清洗、切分三处复用。

### PDF 版式提示

- `pdf_page_blocks()` 用 PyMuPDF 取每个文本块的字号、加粗比例、行数和页面位置。
- `pdf_structure_hints()` 输出 `(heading_hints, noise_hints)`：
  - 正文字号取非页边距块中长文本的加权众数，避免被页眉字号带偏。
  - 单行候选：命名标题，或字号明显大于正文，或在正文号加粗且不超过 12 词。
  - 折行候选：至少 8 个字母且不超过 10 词，避免把化学结构标签（`C / CH 3`）当成章节。
  - 页边距内重复出现的短行（数字统一掩码，因此 “… 81 e 88 82/83” 视为同一行）判定为页眉页脚噪声，不生成章节。
- `segment_document()` 在有版式提示时进入严格模式：只有版式提示或编号标题能开新章节；没有提示（纯文本、pypdf 回退）时沿用宽松的文本规则，保持原有计算机论文行为。

### 摘要与前置内容

- `ABSTRACT_HEADING_RE` 兼容 `Abstract`、`ABSTRACT`、`Abstract:` 和拆字排版。
- `Keywords` 行不再单独成节，关键词并入摘要段落。
- 期刊信息栏（`Article history`、收稿日期、`journal homepage`、DOI 等）按 `ARTICLE_META_*` 丢弃。
- 找不到 Abstract 标题时，`clean_extracted_pages(..., recover_front_matter=True)` 从首页第一个真正的正文段落开始保留内容，而不是整篇清空。

### 分页

- `paragraph_continues_across_pages()` 判断上一页结尾没有句末标点、下一页首字符小写或数字的情况，合并为一段。
- `Paragraph.page_end` 记录结束页，输出 JSON 增加 `pageEnd`（与起始页不同时才写），viewer 显示 `p. 3-4`。

## 验证

- 真实期刊 PDF（`对乙酰氨基苯磺酰氯-IBB2015.pdf`，International Biodeterioration & Biodegradation，8 页，只有 PDF）：改动前 `segment_document()` 返回空（页眉、拆字摘要都会触发失败），改动后得到 5 个章节：`Abstract`、`Introduction`、`Materials and methods`、`Results and discussion`、`Conclusion`。
- arXiv 计算机论文 PDF（PPO, `1707.06347`）：章节标题与改动前完全一致，仅跨页段落合并后段落数 -1。
- LaTeX 源码路径（`--latex`）前后输出一致。
- 新增测试：`test_unnumbered_journal_headings_split_sections`、`test_repeated_running_head_and_footer_lines_are_ignored`、`test_repeated_running_head_with_page_numbers_is_page_furniture`、`test_pdf_layout_hints_separate_headings_from_running_heads`、`test_paragraphs_split_across_pages_are_merged`。
- 端到端 dry run：

```bash
python3 scripts/generate_translation_json.py \
  --pdf 对乙酰氨基苯磺酰氯-IBB2015.pdf \
  --title "Anaerobic treatment of p-ASC-containing wastewater" \
  --paper-url "file:///tmp/ibd2015.pdf" \
  --output /tmp/ibd2015.json --dry-run
```

## 后续可选

- 对低置信度的标题候选，可用一次小的模型调用复核（例如把候选清单发给 DeepSeek 判断哪些是章节标题）。
- 把版式提示接进 `extraction_quality_summary()`，让提取器选择与 OCR 判定也使用标题质量，而不是只看纯文本。

## 第二轮：真实期刊 PDF 的泛化

用 5 篇真实论文测试后发现四个新问题，都已修复：

| 真实样例 | 问题 | 处理 |
| --- | --- | --- |
| ACL 2024（双栏，摘要跨栏） | PyMuPDF 的块顺序按 y 排，右栏正文排在左栏之前；`1` 与 `Introduction` 分成两行，标题丢失 | 新增 XY-cut 阅读顺序（`order_blocks_reading_order`）；`merge_numbered_heading_lines` 把边栏编号与标题拼回一行 |
| Scientific Reports（Nature 系） | 图内 7pt 加粗坐标轴文字、作者单位被当成章节；正文没有 Abstract 标题导致 `extraction_is_usable` 为假 | 新增图表区域识别（`page_visual_regions`：表格 + 图形簇 + 图片）、`is_short_line_stack` 丢弃表格列、单位/引用样式否决；首个块改名为 `Main text`，`extraction_is_usable` 允许“无摘要但结构完整” |
| PLOS ONE | 正文样板句 `competing interests.` 变成章节 | `heading_text_allowed` 否决句尾小写句点、全小写行、含邮箱/DOI/et al. 的行 |
| 色谱（中文期刊，《Chinese Journal of Chromatography》） | 中文标题完全识别不出、正文碎片变成章节、参考文献没有停止、CJK 字间空格影响翻译 | 多语言词表（中/日/德/法/西）、中文编号（`1 引言`、`第1章`、`一、`）、CJK 专属启发式（按字数量纲、禁止句末标点）、`normalize_cjk_spacing` 去掉 CJK 之间多余空格 |

同时把“加粗是否可信”做成文档级判断（某些 CJK 字体所有字形都标 bold）、把正文基准字号改为“非图表非页边距块的字数加权众数”（中文期刊的参考文献 7.4pt 曾把正文基准带偏）。

### 第二轮验证结果

| 论文 | 结果 |
| --- | --- |
| ACL 2024（2024.acl-long.1） | Abstract / 1 Introduction / 2.1 … 4.7 / 5 Conclusion，共 13 节，无脚注与表头误判 |
| Scientific Reports（s41598-024-82944-0） | Main text / Methods / Results / Discussion |
| PLOS ONE（pone.0353753） | Abstract / Introduction / 各方法小节 / Results / Discussion / Conclusion，共 17 节 |
| 色谱（10.3724/SP.J.1123.2025.03019） | Abstract（中文）/ Abstract（英文）/ 1 实验部分 / 2 结果与讨论 / 3 结论，参考文献不再入库 |
| IBB 2015（朋友的 PDF，Elsevier） | Abstract / Introduction / Materials and methods / Results and discussion / Conclusion |
| PPO arXiv 1707.06347（回归） | 8 节，脚注不再被当成章节（改动前有 2 处） |

PDF 在 `tests/test_pdf_layout.py` 里由 PyMuPDF 现场生成，CI 无需二进制样例即可回归。
