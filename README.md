# Paper Parallel Reader

一个用于并排阅读学术论文原文与中文翻译的本地 Web 应用。

它可以下载并缓存 PDF，从 PDF 或 arXiv LaTeX 源码中识别论文结构，过滤图表与页面噪声，通过 DeepSeek 并行翻译段落，并在左侧展示原始 PDF、右侧展示对应的中文译文。

## 功能

- PDF 原文与中文译文并排阅读
- 上传本地 PDF，或通过远程 PDF 直链下载
- 自动识别论文标题和基本元数据
- 缓存已下载的 PDF 和已经生成的翻译
- 使用 PyMuPDF/pypdf 提取 PDF，并自动选择质量更好的结果
- 下载并解析 arXiv LaTeX 源码，尽量保留章节结构
- 过滤图片、表格、图注、页眉和密集图表标签
- 通过 DeepSeek 并行翻译，可配置并发数量
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
- 当前主要针对可提取文本的学术 PDF。纯扫描版 PDF 还没有完整的 OCR 处理流程。

## 项目结构

```text
.
├── backend/
│   └── server.py
├── scripts/
│   ├── dev.sh
│   └── generate_translation_json.py
├── viewer/
│   ├── app.js
│   ├── index.html
│   ├── styles.css
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

1. 在 PDF 输入框中粘贴 PDF 直链并点击 **Load**，或者上传本地 PDF。
2. 检查自动识别的标题和输出文件名。
3. 选择内容来源模式：
   - **Auto**：arXiv 论文优先尝试 LaTeX 源码；源码下载失败时使用 PDF，其他来源直接使用 PDF。若源码包可以下载但解析为空，请手动选择 **PDF**。
   - **LaTeX**：要求使用 arXiv LaTeX 源码。
   - **PDF**：使用 PDF 文本提取与图表噪声过滤流程。
4. 设置翻译并发数量；需要检查提取效果时启用 **Dry run**，需要覆盖缓存时启用 **Force regenerate**。
5. 点击 **Generate** 开始生成。

左侧 PDF 是原文依据，右侧是按章节和段落组织的翻译结果。

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

## 本地数据

后端运行时会创建 `papers_to_translate/`，用于保存：

- 已下载或上传的 PDF
- arXiv LaTeX 源码包
- PDF 元数据
- 论文和翻译缓存索引

生成的翻译保存在 `viewer/translations/`。除项目自带的示例外，这些本地文件默认会被 Git 忽略。

## 常见问题

### Load 提示下载内容不是 PDF

输入的可能是论文介绍页或网页阅读器，而不是 PDF 直链。请找到真正的 PDF 下载地址，或者将论文下载到本地后上传。

### LaTeX 模式提示没有提取到可读文本

部分 arXiv 源码包没有实际的 LaTeX 正文，只包含一个引用已编译 PDF 的外壳文件。将来源模式切换为 **PDF**，启用 **Force regenerate** 后重新生成。

### 修改参数后仍显示旧译文

启用 **Force regenerate**，让后端跳过已有翻译缓存。

## 许可证

MIT
