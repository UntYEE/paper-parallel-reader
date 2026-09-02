# Paper Parallel Reader

一个用于并排阅读学术论文原文与中文翻译的本地 Web 应用。

它可以下载并缓存 PDF，从 PDF 或 arXiv LaTeX 源码中识别论文结构，过滤图表与页面噪声，通过 DeepSeek 并行翻译段落，并在左侧展示原始 PDF、右侧展示对应的中文译文。

## 功能

- PDF 原文与中文译文并排阅读
- 支持专注译文、紧凑排版和深色主题，桌面端与窄屏均可使用
- 上传本地 PDF，或通过远程 PDF 直链下载
- 自动识别论文标题和基本元数据
- 缓存已下载的 PDF 和已经生成的翻译
- 优先解析 arXiv LaTeX 源码，并对结构完整性进行评分
- 使用 PyMuPDF/pypdf 提取 PDF；仅对低质量页面使用 Docling OCR
- 过滤图片、表格、图注、页眉和密集图表标签
- 通过 DeepSeek 并行翻译，并校验每个段落的 `translated / skipped / needs_ocr` 状态
- 缺失段落会自动拆成更小批次重试；OCR 后仍不确定的内容会明确标记
- LaTeX 公式在翻译前替换为不可变 token，翻译后恢复为原始数学源码
- 公式 token 缺失或乱序会自动缩批重试，最终失败时标记为 `needs_formula_recovery`
- LaTeX 交叉引用和文献引用同样使用不可变 token，译文中可跳转到对应章节、段落或 PDF 页
- 正文引用的 LaTeX 图、表和算法会提取为可折叠结构：图片保留原图，表格保留行列，伪代码保留行号和缩进
- 译文使用 Markdown 组织，并由本地 KaTeX 渲染行内公式和块级公式
- 内置论文问答抽屉，使用本地 SQLite FTS5 检索原文和译文；回答引用会由后端校验并跳回证据段落
- Dry run 模式：不调用翻译 API，只检查章节和段落提取结果

## 支持的论文来源

本工具不只支持 arXiv，目前有三种输入方式：

1. **arXiv PDF 链接**：支持 PDF 下载、缓存，并可尝试下载对应的 LaTeX 源码。
2. **其他网站的 PDF 直链**：只要链接以 `http://` 或 `https://` 开头，并且服务器最终返回真实的 PDF 文件，就可以下载和解析。
3. **本地 PDF 上传**：不依赖论文网站，适合处理来自期刊官网、会议网站、学校仓库或个人电脑的论文。

需要注意：

- 自动下载 LaTeX 源码目前只支持 arXiv，其他网站会使用 PDF 提取流程。
- 网页文章地址、需要登录的下载页、带验证码的链接，以及在网页内部动态加载 PDF 的阅读器地址，通常不是 PDF 直链。这类论文建议先下载到本地再上传。
- 部分 arXiv 投稿没有真正的 LaTeX 正文，源码包中可能只有已编译的 PDF。遇到这种情况应使用 **PDF** 模式。
- 扫描版或局部文本质量较低的 PDF 会按页调用 Docling。首次使用时可能需要下载 Docling 模型，因此会比之后的运行更慢。

## 项目结构

```text
.
├── backend/
│   ├── paper_qa.py
│   ├── paper_search.py
│   └── server.py
├── docs/
│   └── cross-reference-and-qa-plan.md
├── scripts/
│   ├── dev.sh
│   └── generate_translation_json.py
├── tests/
│   ├── test_paper_qa.py
│   ├── test_paper_search.py
│   └── test_translation_workflow.py
├── viewer/
│   ├── app.js
│   ├── index.html
│   ├── styles.css
│   ├── vendor/
│   │   ├── dompurify/
│   │   ├── katex/
│   │   └── marked/
│   └── translations/
│       └── attention-is-all-you-need.sample.json
├── .env.example
├── requirements.txt
└── README.md
```

下载的 PDF、arXiv 源码包、本地索引和生成的翻译默认不会提交到 Git。

## 环境要求

- Python 3.10 或更高版本
- 用于生成翻译的 DeepSeek API Key
- `curl`，用于更稳定地下载论文和源码包

## 安装

克隆仓库并进入项目目录：

```bash
git clone https://github.com/UntYEE/paper-parallel-reader.git
cd paper-parallel-reader
```

创建 Python 虚拟环境并安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

复制环境变量示例文件：

```bash
cp .env.example .env
```

编辑 `.env`，填入自己的 API Key：

```text
DEEPSEEK_API_KEY=your-api-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TIMEOUT=180
QA_MODEL=deepseek-v4-flash
QA_EVIDENCE_LIMIT=6
DEEPSEEK_SEARCH_BASE_URL=https://api.deepseek.com/anthropic/v1
DEEPSEEK_SEARCH_MODEL=deepseek-v4-flash
DEEPSEEK_SEARCH_MAX_USES=3
PAPER_SEARCH_TIMEOUT=45
PAPER_PDF_VERIFY_TIMEOUT=10
DOCLING_TIMEOUT=180
DOCLING_OCR_LANGUAGE=en
```

不要把包含真实 API Key 的 `.env` 提交到 GitHub。

## 启动

在项目根目录执行：

```bash
./scripts/dev.sh
```

脚本会同时启动前端和后端：

- 阅读器：`http://localhost:8000/viewer/`
- 后端接口：`http://127.0.0.1:8787`

按 `Ctrl+C` 可以同时停止两个服务。

## 使用方法

1. 在论文输入框中输入论文标题、关键词、arXiv ID 或 PDF 直链并点击 **检索**，也可以上传本地 PDF。搜索优先命中本地缓存；未命中时使用现有 `DEEPSEEK_API_KEY` 调用 DeepSeek 原生 Web Search。后端只接受结构化搜索结果，并对论文域名、响应类型和 `%PDF-` 文件头进行校验；没有可靠 PDF 时会提示手动输入链接或上传文件。
2. 检查自动识别的标题和输出文件名。
3. 选择内容来源模式：
   - **自动选择**：arXiv 论文优先解析 LaTeX 并评分；结构不完整时自动回退到 PDF。PDF 原生文本质量差的页面才会进入 Docling OCR。
   - **LaTeX**：要求使用 arXiv LaTeX 源码。
   - **PDF**：使用 PDF 文本提取与图表噪声过滤流程。
4. 设置翻译并发数量；需要检查提取效果时启用 **仅预览**，需要覆盖缓存时启用 **强制重新生成**。
5. 点击 **生成译文** 开始生成。

左侧 PDF 是原文依据，右侧是按章节和段落组织的翻译结果。译文中的公式、图表、章节和参考文献引用会保留为链接；能够定位时点击即可跳到对应段落、结构化内容或 PDF 页。正文引用的图、表和算法默认折叠在首次引用之后，点击标题即可展开。

顶部的 **专注** 会隐藏原文与生成设置，让译文占满页面；**紧凑** 适合快速扫读；**深色** 用于切换夜间界面。左侧 PDF 保留浏览器阅读器原生的缩放、分页和下载工具。

点击右下角的 `?` 可以打开论文问答。也可以点击任一段落右上角的 **提问**，让检索优先使用当前段落。问答只使用当前论文的本地索引，回答下方的证据按钮可跳回原段落；论文没有提供足够信息时会明确提示，而不是补写答案。

生成流程依次为：LaTeX 结构解析和公式保护、PDF 原生文本提取和质量评分、严格 ID 与公式 token 翻译校验、缺失项缩小批次重试、必要页面 Docling OCR 或公式增强。模型无法可靠恢复的正文保留为 `needs_ocr`，无法可靠恢复的数学内容标记为 `needs_formula_recovery`；确定属于图表、页眉等非正文的项目标记为 `skipped`，阅读器不显示这些项目。

翻译批次会根据字符数、估算 token 和公式密度动态拆分，正文与每批 4–6 项的结构化内容共用同一个 API 客户端和并发池。每个完成批次都会写入 `papers_to_translate/checkpoints/`；进程中断后，用相同论文、模型和分块参数重新生成即可续跑。

后端还提供不依赖前端的异步任务接口：`POST /api/generation-tasks` 创建基于已保存 PDF 的任务，`GET /api/generation-tasks/{task_id}` 查询状态，`GET /api/generation-tasks/{task_id}/events` 通过 SSE 返回进度。进度包括完成批次、重试次数、输入/输出 token 和预估美元费用；价格可通过 `.env` 中的三个 `DEEPSEEK_*_PRICE_USD_PER_MILLION` 参数调整。

生成 JSON 的 `contentFormat` 为 `markdown+latex`。正文 Markdown 会先经过 DOMPurify 清理，再由本地 KaTeX 渲染 `$...$`、`$$...$$`、`\(...\)` 和 `\[...\]`，运行时不依赖 CDN。

前端内置的 KaTeX、Marked 和 DOMPurify 发布文件及许可证位于 `viewer/vendor/`。

## 命令行生成

从 PDF 生成：

```bash
python3 scripts/generate_translation_json.py \
  --pdf /path/to/paper.pdf \
  --title "Paper Title" \
  --paper-url "https://example.org/paper.pdf" \
  --output viewer/translations/paper-title.json
```

从 `.tex` 文件、LaTeX 目录、zip、tarball 或 gzip 源码包生成：

```bash
python3 scripts/generate_translation_json.py \
  --latex /path/to/source.tar.gz \
  --title "Paper Title" \
  --paper-url "https://arxiv.org/pdf/xxxx.xxxxx" \
  --output viewer/translations/paper-title.json
```

添加 `--dry-run` 可以只检查提取出的章节和段落，不调用 DeepSeek。

## 测试

在虚拟环境中运行：

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖论文搜索结果校验、FTS5 问答检索、虚构证据拦截，以及公式、交叉引用、结构化内容和断点续跑等翻译流程。

## 本地数据

后端运行时会创建 `papers_to_translate/`，用于保存：

- 已下载或上传的 PDF
- arXiv LaTeX 源码包
- PDF 元数据
- 论文和翻译缓存索引
- 问答全文索引与本地会话历史（`paper_qa.sqlite3`）
- 可中断续跑的翻译批次检查点（`checkpoints/`）

LaTeX 源图像转换后的浏览器资源保存在 `viewer/paper-assets/`，与生成译文一样默认不会提交到 Git。

生成的翻译保存在 `viewer/translations/`。除项目自带的示例外，这些本地文件默认会被 Git 忽略。

## 常见问题

### Load 提示下载内容不是 PDF

输入的可能是论文介绍页或网页阅读器，而不是 PDF 直链。请找到真正的 PDF 下载地址，或者将论文下载到本地后上传。

### LaTeX 模式提示没有提取到可读文本

部分 arXiv 源码包没有实际的 LaTeX 正文，只包含一个引用已编译 PDF 的外壳文件。将来源模式切换为 **PDF**，启用 **Force regenerate** 后重新生成。

### 修改参数后仍显示旧译文

启用 **强制重新生成**，让后端跳过已有翻译缓存。批次检查点仍会按论文指纹复用；如需彻底从头运行，可删除对应的本地检查点目录。

## 许可证

MIT
