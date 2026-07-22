# Paper Parallel Reader

A standalone local web app for reading academic papers side by side with terminology-preserving Chinese translations.

It downloads and caches PDFs, extracts paper structure from PDF or arXiv LaTeX source, filters figures and table artifacts, translates paragraphs through DeepSeek, and displays the original PDF beside the generated translation.

## Features

- Side-by-side PDF and Chinese translation reader
- Local PDF upload and remote PDF download
- Automatic title and paper metadata detection
- PDF cache and generated translation cache
- PDF extraction with PyMuPDF/pypdf quality selection
- arXiv LaTeX source download and structure-aware parsing
- Figure, table, caption, page-header, and dense chart-label filtering
- Parallel DeepSeek translation with configurable concurrency
- Dry-run mode for inspecting section and paragraph extraction without API calls

## Project Layout

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

Downloaded PDFs, arXiv source packages, generated indexes, and local translations are ignored by Git.

## Requirements

- Python 3.10+
- A DeepSeek API key for translation generation
- `curl` for reliable paper/source downloads

## Install

```bash
git clone https://github.com/UntYEE/paper-parallel-reader.git
cd paper-parallel-reader
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set your API key:

```text
DEEPSEEK_API_KEY=your-api-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TIMEOUT=180
```

## Start

Start the frontend and backend together:

```bash
./scripts/dev.sh
```

Then open:

```text
http://localhost:8000/viewer/
```

The backend runs at `http://127.0.0.1:8787`.

## Use the Reader

1. Paste an arXiv PDF URL into the PDF field and click **Load**, or upload a local PDF.
2. Confirm the detected title and output filename.
3. Select a source mode:
   - **Auto**: prefer cached/downloadable arXiv LaTeX source, then fall back to PDF.
   - **LaTeX**: require arXiv LaTeX source.
   - **PDF**: use the PDF extraction and visual-artifact filtering pipeline.
4. Set translation parallelism and optionally enable **Dry run** or **Force regenerate**.
5. Click **Generate**.

The left pane remains the authoritative PDF. The right pane contains translation-only JSON organized as sections and paragraphs.

## Command-Line Generation

Generate from PDF:

```bash
python3 scripts/generate_translation_json.py \
  --pdf /path/to/paper.pdf \
  --title "Paper Title" \
  --paper-url "https://arxiv.org/pdf/xxxx.xxxxx" \
  --output viewer/translations/paper-title.json
```

Generate from a `.tex` file, LaTeX directory, zip, tarball, or gzip source:

```bash
python3 scripts/generate_translation_json.py \
  --latex /path/to/source.tar.gz \
  --title "Paper Title" \
  --paper-url "https://arxiv.org/pdf/xxxx.xxxxx" \
  --output viewer/translations/paper-title.json
```

Add `--dry-run` to inspect the extracted section/paragraph scaffold without calling DeepSeek.

## Local Data

The backend creates `papers_to_translate/` at runtime. It stores:

- downloaded PDFs
- arXiv LaTeX source archives
- PDF metadata
- the paper/translation cache index

Generated translations are stored under `viewer/translations/`. These local artifacts are ignored by default, except for the bundled sample.

## License

MIT
