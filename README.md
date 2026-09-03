# Paper Parallel Reader

更新：0.1.5版本Windows系统可以稳定使用

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

### 下载后双击启动（推荐）

1. 安装并启动 [Docker Desktop](https://www.docker.com/products/docker-desktop/)。
2. 打开 [GitHub Releases](https://github.com/UntYEE/paper-parallel-reader/releases/latest)，下载最新的 `paper-parallel-reader-版本号.zip`。
3. Windows 用户在解压前右键点击 ZIP，选择 **属性**，勾选底部的 **解除锁定**，再点击 **应用**；然后解压。
4. macOS 双击 `启动.command`；Windows 双击 `启动-Windows.bat`。
5. 首次启动时输入自己的 DeepSeek API Key，并选择普通版或 OCR 版。

启动器会自动下载预构建镜像、启动服务并打开浏览器，不需要安装 Git、Python，也不需要编译项目。API Key 的输入内容不会显示，只会保存在解压目录的 `.env` 文件中。

macOS 如果阻止首次运行，可以右键点击 `启动.command`，选择 **打开**。

如果 Windows 已经解压后才出现“智能应用控制已阻止可能不安全的文件”：

1. 删除刚才解压出的文件夹。
2. 右键点击原始 ZIP，选择 **属性 → 解除锁定 → 应用**。
3. 重新解压 ZIP，再双击 `启动-Windows.bat`。

如果命令窗口显示“`'p' 不是内部或外部命令`”或“`'ershell.exe' 不是内部或外部命令`”，说明使用的是早期发布包，请重新下载最新版 Release。新版 Windows 启动器使用标准 CRLF 换行和纯英文脚本文件名，可兼容 Windows 10/11 自带的 Windows PowerShell。

只应对从本项目 GitHub Release 下载并核对过 SHA-256 的文件解除锁定，不要关闭 Smart App Control。如果电脑由学校或公司管理且没有“解除锁定”选项，请保留安全策略，改用下方 Docker 命令启动或联系管理员。

### 使用 Docker 命令启动

适用于熟悉终端的 macOS、Windows 和 Linux 用户。请准备 Docker Desktop，或 Docker Engine 与 Compose 插件，并申请自己的 DeepSeek API Key。

克隆项目并创建配置：

```bash
git clone https://github.com/UntYEE/paper-parallel-reader.git
cd paper-parallel-reader
cp .env.example .env
```

编辑 `.env`，至少填写：

```text
DEEPSEEK_API_KEY=your-api-key
```

下载预构建镜像并启动轻量版本：

```bash
docker compose pull
docker compose up -d
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
docker compose -f compose.yaml -f compose.ocr.yaml pull
docker compose -f compose.yaml -f compose.ocr.yaml up -d
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

### 从源码构建镜像

开发或无法使用 GHCR 预构建镜像时，可以在本机编译基础版本：

```bash
docker compose -f compose.yaml -f compose.build.yaml up --build
```

本机构建 OCR 版本：

```bash
docker compose -f compose.yaml -f compose.ocr.yaml -f compose.build-ocr.yaml up --build
```

### 更新、备份与清理

使用 Release ZIP 时，下载并解压新版本，再将旧目录中的 `.env` 和 `data/` 移到新目录。使用 Git 克隆的项目时：

```bash
git pull
docker compose pull
docker compose up -d
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
PAPER_READER_IMAGE=ghcr.io/untyee/paper-parallel-reader:latest
PAPER_READER_OCR_IMAGE=ghcr.io/untyee/paper-parallel-reader:ocr
```

修改 `APP_PORT` 后，访问地址也需要使用对应端口。远程下载仅允许 HTTP/HTTPS 公网地址，并会拒绝回环、内网、链路本地、云元数据地址及跳转后的非公网地址。
