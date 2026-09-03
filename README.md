# Paper Parallel Reader

一个在本机并排阅读论文原文与中文译文的 Web 应用。

## 功能说明

- 左侧显示原始 PDF，右侧按章节和段落显示中文译文。
- 支持论文标题、关键词、arXiv ID、PDF 直链和本地 PDF 上传。
- 自动识别标题与论文信息，并缓存 PDF、LaTeX 源码和生成结果。
- arXiv 论文优先解析 LaTeX 源码；结构不完整时自动回退到 PDF 文本提取。
- 通过 DeepSeek 并行翻译，支持动态分块、失败缩批重试和中断续跑。
- 保留 LaTeX 公式、章节引用和文献引用，使用 Markdown 与 KaTeX 渲染译文。
- 将正文引用的图片、表格和算法提取为可折叠的结构化内容。
- 可选使用 Docling，只对低质量页面或缺失公式的区域进行 OCR。
- 内置论文问答，基于本地 SQLite FTS5 检索原文和译文，并校验回答引用的证据段落。
- 支持专注译文、紧凑排版、深色主题和浏览器 PDF 阅读器的原生工具。
- 所有论文、译文、问答记录和检查点默认只保存在使用者自己的电脑上。

## 部署教程

### Docker 部署（推荐）

适用于 macOS、Windows 和 Linux。请先安装 Docker Desktop，或 Docker Engine 与 Compose 插件，并申请自己的 DeepSeek API Key。

克隆项目：

```bash
git clone https://github.com/UntYEE/paper-parallel-reader.git
cd paper-parallel-reader
```

创建本地配置：

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

```text
DEEPSEEK_API_KEY=your-api-key
```

启动轻量版本：

```bash
docker compose up --build
```

浏览器打开：

```text
http://127.0.0.1:8000/viewer/
```

服务只绑定到 `127.0.0.1`，默认不会对局域网或公网开放。API Key 只进入后端进程，不会发送给浏览器或写入镜像。

停止服务：

```bash
docker compose down
```

### 启用 Docling OCR

默认镜像不安装 Docling。需要处理扫描版或低质量 PDF 时使用 OCR 配置：

```bash
docker compose -f compose.yaml -f compose.ocr.yaml up --build
```

OCR 镜像和模型较大，首次构建需要较长时间。模型会缓存在 `data/model-cache/`，后续启动可以复用。

### 原生 Python 部署

需要 Python 3.10 或更高版本。

macOS 或 Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
./scripts/dev.sh
```

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn backend.server:app --host 127.0.0.1 --port 8000 --workers 1
```

原生环境启用 OCR：

```bash
python3 -m pip install -r requirements-ocr.txt
```

然后在 `.env` 中设置：

```text
ENABLE_OCR=true
```

请始终保持单个 Uvicorn worker，避免 SQLite、缓存和同一论文任务发生并发写入冲突。

### 更新、备份与清理

更新并重新构建：

```bash
git pull
docker compose up --build
```

运行数据统一保存在项目的 `data/` 目录，包括 PDF、LaTeX 源码、译文、结构化资源、SQLite 数据库、检查点和 OCR 模型缓存。备份这个目录即可迁移本地数据。

`docker compose down` 和 `docker compose down -v` 都不会删除这个宿主机目录。需要清空全部本地数据时，先停止服务，再手动删除 `data/`。

不要提交或分享 `.env`、`data/` 及终端中可能包含敏感信息的日志。Unix 系统建议保持 `.env` 权限为 `600`。

### 常用配置

可在 `.env` 中调整：

```text
APP_PORT=8000
MAX_UPLOAD_MB=100
MAX_DOWNLOAD_MB=100
PAPER_DOWNLOAD_TIMEOUT=75
ENABLE_OCR=false
```

修改 `APP_PORT` 后，访问地址也需要使用对应端口。远程下载仅允许 HTTP/HTTPS 公网地址，并会拒绝回环、内网、链路本地、云元数据地址及跳转后的非公网地址。
