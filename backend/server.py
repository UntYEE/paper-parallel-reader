#!/usr/bin/env python3
"""Local backend for generating viewer translation JSON with the DeepSeek API."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pypdf import PdfReader

from scripts.generate_translation_json import generate_translation_json


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "viewer" / "translations"
PAPERS_DIR = REPO_ROOT / "papers_to_translate"
SOURCES_DIR = PAPERS_DIR / "latex_sources"
INDEX_PATH = PAPERS_DIR / "paper_index.json"
PAPERS_DIR.mkdir(parents=True, exist_ok=True)
SOURCES_DIR.mkdir(parents=True, exist_ok=True)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env(REPO_ROOT / ".env")

app = FastAPI(title="Paper Translation Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def deepseek_api_key() -> str:
    value = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not value or value.startswith("填") or "DeepSeek API Key" in value:
        return ""
    return value


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def backend_public_url() -> str:
    return os.getenv("PAPER_BACKEND_PUBLIC_URL", "http://127.0.0.1:8787").rstrip("/")


def safe_output_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise HTTPException(status_code=400, detail="output_name is required.")
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    if not name.endswith(".json"):
        name = f"{name}.json"
    return name


def slug_output_name(value: str, fallback: str) -> str:
    source = value.strip() or fallback
    name = re.sub(r"[^A-Za-z0-9]+", "-", source).strip("-").lower()
    name = re.sub(r"-+", "-", name)
    return safe_output_name(name or fallback)


def paper_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = unquote(parsed.path).rstrip("/")
    if "arxiv.org" in parsed.netloc and "/pdf/" in path:
        arxiv_id = path.rsplit("/", 1)[-1].removesuffix(".pdf")
        return f"arxiv:{arxiv_id}"
    if "arxiv.org" in parsed.netloc and "/abs/" in path:
        arxiv_id = path.rsplit("/", 1)[-1]
        return f"arxiv:{arxiv_id}"
    if "arxiv.org" in parsed.netloc and "/e-print/" in path:
        arxiv_id = path.rsplit("/", 1)[-1]
        return f"arxiv:{arxiv_id}"
    return f"url:{url}"


def arxiv_id_from_paper_id(paper_id: str) -> str:
    return paper_id.removeprefix("arxiv:") if paper_id.startswith("arxiv:") else ""


def arxiv_source_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/e-print/{arxiv_id}"


def source_name_for_arxiv(arxiv_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", arxiv_id).strip("-")
    return f"arxiv-{safe_id}.tar.gz"


def source_path_for_arxiv(arxiv_id: str) -> Path:
    path = (SOURCES_DIR / source_name_for_arxiv(arxiv_id)).resolve()
    if path.parent != SOURCES_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid arXiv source id.")
    return path


def paper_id_from_filename(name: str) -> str:
    stem = name.removesuffix(".pdf")
    if stem.startswith("arxiv-"):
        return f"arxiv:{stem.removeprefix('arxiv-')}"
    return f"file:{stem}"


def is_pdf_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"%PDF"
    except OSError:
        return False


def load_index() -> dict[str, object]:
    if not INDEX_PATH.exists():
        return {"papers": {}}
    try:
        data = json_loads(INDEX_PATH.read_text(encoding="utf-8"))
    except ValueError:
        return {"papers": {}}
    if not isinstance(data.get("papers"), dict):
        data["papers"] = {}
    return data


def save_index(index: dict[str, object]) -> None:
    INDEX_PATH.write_text(json_dumps(index), encoding="utf-8")


def upsert_paper_index(paper_id: str, **fields: object) -> dict[str, object]:
    index = load_index()
    papers = index.setdefault("papers", {})
    assert isinstance(papers, dict)
    record = papers.setdefault(paper_id, {})
    assert isinstance(record, dict)
    record.update({key: value for key, value in fields.items() if value not in (None, "")})
    record["paperId"] = paper_id
    record["updatedAt"] = datetime.now(timezone.utc).isoformat()
    save_index(index)
    return record


def indexed_translation_path(paper_id: str) -> Optional[Path]:
    index = load_index()
    papers = index.get("papers", {})
    if not isinstance(papers, dict):
        return None
    record = papers.get(paper_id, {})
    if not isinstance(record, dict):
        return None
    translation = record.get("translationPath")
    if not isinstance(translation, str):
        return None
    path = (REPO_ROOT / translation).resolve()
    if path.parent != DEFAULT_OUTPUT_DIR.resolve():
        return None
    return path if path.exists() else None


def safe_pdf_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise HTTPException(status_code=400, detail="PDF name is required.")
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    if not name.endswith(".pdf"):
        name = f"{name}.pdf"
    return name


def name_from_pdf_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="PDF URL must start with http:// or https://.")
    path = unquote(parsed.path).rstrip("/")
    if "arxiv.org" in parsed.netloc and "/pdf/" in path:
        arxiv_id = path.rsplit("/", 1)[-1].removesuffix(".pdf")
        return safe_pdf_name(f"arxiv-{arxiv_id}")
    stem = Path(path).name or "paper"
    return safe_pdf_name(stem)


def paper_path(name: str) -> Path:
    filename = safe_pdf_name(name)
    path = (PAPERS_DIR / filename).resolve()
    if path.parent != PAPERS_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid PDF name.")
    return path


def paper_file_url(name: str) -> str:
    return f"/api/papers/{safe_pdf_name(name)}"


def paper_viewer_url(name: str) -> str:
    return f"{backend_public_url()}{paper_file_url(name)}"


def infer_title_from_pdf(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
    except Exception:
        return ""

    metadata_title = ""
    try:
        metadata_title = str(reader.metadata.get("/Title") or "").strip()
    except Exception:
        metadata_title = ""
    if metadata_title and not metadata_title.lower().startswith("untitled"):
        return re.sub(r"\s+", " ", metadata_title)

    if not reader.pages:
        return ""
    text = reader.pages[0].extract_text() or ""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    title_lines: list[str] = []

    noise_re = re.compile(
        r"^(journal|proceedings|conference|transactions|vol\.|arxiv:|doi:|\d+$)",
        re.IGNORECASE,
    )
    authorish_re = re.compile(
        r"(∗|,\s*(senior\s+member|member|fellow)|\band\b|\bIEEE\b|[A-Z][a-z]+,\s+[A-Z])"
    )
    affiliation_re = re.compile(r"^(https?://|fair\b|meta\b|google\b|university\b|department\b)", re.IGNORECASE)
    stop_re = re.compile(r"^(abstract|keywords|index terms)\b", re.IGNORECASE)

    for line in lines[:40]:
        if stop_re.match(line):
            break
        if title_lines and affiliation_re.match(line):
            break
        if noise_re.match(line):
            continue
        if title_lines and authorish_re.search(line):
            break
        if len(line) < 4:
            continue
        title_lines.append(line)
        if len(" ".join(title_lines)) >= 180:
            break

    title = " ".join(title_lines).strip()
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"\s+([:;,.])", r"\1", title)
    return title[:220]


def paper_info(path: Path, source_url: str = "") -> dict[str, object]:
    meta = read_paper_meta(path)
    effective_source = source_url or meta.get("sourceUrl", "")
    paper_id = meta.get("paperId") or (
        paper_id_from_url(effective_source) if effective_source else paper_id_from_filename(path.name)
    )
    inferred_title = infer_title_from_pdf(path)
    stored_title = meta.get("title") or ""
    title = stored_title or inferred_title
    if inferred_title and (len(title) > 140 or "http" in title.lower()):
        title = inferred_title
    output_name = slug_output_name(title, path.stem)
    return {
        "name": path.name,
        "paperId": paper_id,
        "title": title,
        "outputName": output_name,
        "sourceUrl": effective_source,
        "fileUrl": paper_file_url(path.name),
        "pdfUrl": paper_viewer_url(path.name),
        "size": path.stat().st_size,
    }


def paper_payload(path: Path, source_url: str, *, cached: bool) -> dict[str, object]:
    return {
        "ok": True,
        "cached": cached,
        **paper_info(path, source_url),
    }


def missing_paper_payload(url: str) -> dict[str, object]:
    filename = name_from_pdf_url(url)
    return {
        "ok": True,
        "cached": False,
        "name": filename,
        "paperId": paper_id_from_url(url),
        "sourceUrl": url,
        "fileUrl": "",
        "pdfUrl": "",
        "size": 0,
    }


def meta_path_for(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".meta.json")


def write_paper_meta(path: Path, source_url: str, title: str = "") -> None:
    paper_id = paper_id_from_url(source_url)
    meta = {
        "name": path.name,
        "paperId": paper_id,
        "sourceUrl": source_url,
        "title": title or infer_title_from_pdf(path),
        "savedAt": datetime.now(timezone.utc).isoformat(),
    }
    meta_path_for(path).write_text(json_dumps(meta), encoding="utf-8")
    upsert_paper_index(
        paper_id,
        pdfName=path.name,
        pdfPath=str(path.relative_to(REPO_ROOT)),
        sourceUrl=source_url,
        title=meta["title"],
    )


def read_paper_meta(path: Path) -> dict[str, str]:
    meta_path = meta_path_for(path)
    if not meta_path.exists():
        return {}
    try:
        return json_loads(meta_path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def json_loads(value: str) -> dict[str, str]:
    import json

    data = json.loads(value)
    return data if isinstance(data, dict) else {}


async def write_upload(upload: UploadFile, suffix: str) -> Path:
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail=f"{upload.filename or 'upload'} is empty.")
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    path = Path(handle.name)
    handle.write(data)
    handle.close()
    return path


async def save_uploaded_pdf(upload: UploadFile, source_url: str, fallback_name: str) -> Path:
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail=f"{upload.filename or 'PDF upload'} is empty.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Uploaded content does not look like a PDF.")

    filename = safe_pdf_name(upload.filename or fallback_name)
    path = paper_path(filename)
    path.write_bytes(data)
    write_paper_meta(path, source_url)
    return path


async def save_uploaded_pdf_response(upload: UploadFile, source_url: str, fallback_name: str) -> JSONResponse:
    path = await save_uploaded_pdf(upload, source_url, fallback_name)
    return JSONResponse(paper_payload(path, source_url, cached=False))


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "deepseek_api_key_configured": bool(deepseek_api_key()),
        "deepseek_base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "deepseek_model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
    }


@app.get("/api/papers")
def list_papers() -> dict[str, object]:
    papers = []
    for path in sorted(PAPERS_DIR.glob("*.pdf")):
        meta = read_paper_meta(path)
        paper_id = meta.get("paperId") or paper_id_from_filename(path.name)
        cached_translation = indexed_translation_path(paper_id)
        info = paper_info(path)
        papers.append(
            {
                **info,
                "translationUrl": (
                    f"/viewer/translations/{cached_translation.name}" if cached_translation else ""
                ),
            }
        )
    return {"ok": True, "papers": papers}


@app.get("/api/papers/{filename}")
def serve_paper(filename: str) -> FileResponse:
    path = paper_path(filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF not found.")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type="inline",
    )


@app.get("/api/inspect-paper/{filename}")
def inspect_paper(filename: str) -> dict[str, object]:
    path = paper_path(filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF not found.")
    return {"ok": True, **paper_info(path)}


@app.post("/api/upload-paper")
async def upload_paper(
    pdf: Annotated[UploadFile, File()],
    source_url: Annotated[str, Form()] = "",
    output_name: Annotated[str, Form()] = "uploaded-paper.pdf",
) -> JSONResponse:
    return await save_uploaded_pdf_response(pdf, source_url or f"file:{pdf.filename}", output_name)


@app.post("/api/check-paper-cache")
def check_paper_cache(url: Annotated[str, Form()]) -> JSONResponse:
    filename = name_from_pdf_url(url)
    path = paper_path(filename)
    if path.exists() and is_pdf_file(path):
        if not read_paper_meta(path).get("sourceUrl"):
            write_paper_meta(path, url)
        return JSONResponse(paper_payload(path, url, cached=True))
    return JSONResponse(missing_paper_payload(url))


def download_pdf_bytes(url: str) -> bytes:
    curl_command = [
        "curl",
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--retry",
        "1",
        "--retry-all-errors",
        "--retry-delay",
        "2",
        "--connect-timeout",
        "20",
        "--max-time",
        "75",
        "-A",
        "Mozilla/5.0 paper-reading-workflow/1.0",
        url,
    ]
    try:
        completed = subprocess.run(curl_command, check=True, capture_output=True)
        return completed.stdout
    except Exception as curl_error:
        curl_failure = curl_error

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/pdf,*/*",
            "User-Agent": "Mozilla/5.0 paper-reading-workflow/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except Exception as urllib_error:
        raise RuntimeError(f"curl failed: {curl_failure}; urllib fallback failed: {urllib_error}") from urllib_error


def download_binary_bytes(url: str, *, accept: str, max_time: str = "75") -> bytes:
    curl_command = [
        "curl",
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--retry",
        "1",
        "--retry-all-errors",
        "--retry-delay",
        "2",
        "--connect-timeout",
        "20",
        "--max-time",
        max_time,
        "-H",
        f"Accept: {accept}",
        "-A",
        "Mozilla/5.0 paper-reading-workflow/1.0",
        url,
    ]
    try:
        completed = subprocess.run(curl_command, check=True, capture_output=True)
        return completed.stdout
    except Exception as curl_error:
        curl_failure = curl_error

    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "Mozilla/5.0 paper-reading-workflow/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except Exception as urllib_error:
        raise RuntimeError(f"curl failed: {curl_failure}; urllib fallback failed: {urllib_error}") from urllib_error


def ensure_arxiv_latex_source(arxiv_id: str) -> Path:
    path = source_path_for_arxiv(arxiv_id)
    if path.exists() and path.stat().st_size > 0:
        return path

    data = download_binary_bytes(
        arxiv_source_url(arxiv_id),
        accept="application/e-print,application/x-eprint,application/gzip,application/x-gzip,*/*",
        max_time=os.getenv("ARXIV_SOURCE_DOWNLOAD_TIMEOUT", "90"),
    )
    if not data:
        raise RuntimeError("Downloaded arXiv source is empty.")
    if data.startswith(b"%PDF"):
        raise RuntimeError("arXiv returned a PDF instead of LaTeX source.")

    path.write_bytes(data)
    return path


@app.post("/api/download-paper")
def download_paper(url: Annotated[str, Form()]) -> JSONResponse:
    filename = name_from_pdf_url(url)
    path = paper_path(filename)
    if path.exists() and is_pdf_file(path):
        if not read_paper_meta(path).get("sourceUrl"):
            write_paper_meta(path, url)
        return JSONResponse(paper_payload(path, url, cached=True))

    try:
        data = download_pdf_bytes(url)
    except Exception as error:  # noqa: BLE001 - report download failures clearly to the UI.
        raise HTTPException(status_code=400, detail=f"Failed to download PDF: {error}") from error

    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Downloaded content does not look like a PDF.")

    path.write_bytes(data)
    write_paper_meta(path, url)
    return JSONResponse(paper_payload(path, url, cached=False))


@app.post("/api/generate")
async def generate(
    title: Annotated[str, Form()],
    paper_url: Annotated[str, Form()],
    output_name: Annotated[str, Form()],
    pages: Annotated[Optional[str], Form()] = None,
    coverage: Annotated[str, Form()] = "Full paper text extracted from the provided input.",
    model: Annotated[Optional[str], Form()] = None,
    max_chars: Annotated[int, Form()] = 12000,
    parallelism: Annotated[int, Form()] = env_int("DEEPSEEK_PARALLELISM", 3),
    pdf_extractor: Annotated[str, Form()] = os.getenv("PDF_TEXT_EXTRACTOR", "auto"),
    source_mode: Annotated[str, Form()] = os.getenv("PAPER_SOURCE_MODE", "auto"),
    retries: Annotated[int, Form()] = 2,
    dry_run: Annotated[bool, Form()] = False,
    force: Annotated[bool, Form()] = False,
    pdf: Annotated[Optional[UploadFile], File()] = None,
    saved_pdf: Annotated[Optional[str], Form()] = None,
    text_file: Annotated[Optional[UploadFile], File()] = None,
    text: Annotated[Optional[str], Form()] = None,
) -> JSONResponse:
    output_path = DEFAULT_OUTPUT_DIR / safe_output_name(output_name)
    temp_paths: list[Path] = []

    try:
        pdf_path: Optional[Path] = None
        latex_path: Optional[Path] = None
        text_path: Optional[Path] = None
        local_pdf_url = paper_url

        if saved_pdf:
            pdf_path = paper_path(saved_pdf)
            if not pdf_path.exists():
                raise HTTPException(status_code=404, detail="Saved PDF not found.")
            meta = read_paper_meta(pdf_path)
            paper_id = meta.get("paperId") or paper_id_from_url(paper_url) or paper_id_from_filename(saved_pdf)
            local_pdf_url = paper_viewer_url(pdf_path.name)
        elif pdf is not None:
            pdf_path = await save_uploaded_pdf(pdf, paper_url, output_path.with_suffix(".pdf").name)
            paper_id = paper_id_from_url(paper_url)
            local_pdf_url = paper_viewer_url(pdf_path.name)
        elif text_file is not None:
            text_path = await write_upload(text_file, ".txt")
            temp_paths.append(text_path)
            paper_id = paper_id_from_url(paper_url)
        elif text:
            handle = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
            text_path = Path(handle.name)
            handle.write(text)
            handle.close()
            temp_paths.append(text_path)
            paper_id = paper_id_from_url(paper_url)
        else:
            raise HTTPException(status_code=400, detail="Upload pdf, upload text_file, or provide text.")

        cached_path = indexed_translation_path(paper_id)
        if source_mode != "latex" and not force and not dry_run and cached_path:
            print(f"Cache hit for {paper_id}: {cached_path.relative_to(REPO_ROOT)}", flush=True)
            return JSONResponse(
                {
                    "ok": True,
                    "cached": True,
                    "paperId": paper_id,
                    "output": str(cached_path.relative_to(REPO_ROOT)),
                    "pdf_url": local_pdf_url,
                    "pdf_name": pdf_path.name if pdf_path else "",
                    "file_url": paper_file_url(pdf_path.name) if pdf_path else "",
                    "viewer_url": f"/viewer/?pdf={local_pdf_url}&translation=./translations/{cached_path.name}",
                    "sections": 0,
                    "paragraphs": 0,
                }
            )

        if source_mode != "latex" and not force and not dry_run and output_path.exists():
            upsert_paper_index(
                paper_id,
                pdfName=saved_pdf or "",
                sourceUrl=paper_url,
                pdfPath=str(pdf_path.relative_to(REPO_ROOT)) if pdf_path else "",
                translationPath=str(output_path.relative_to(REPO_ROOT)),
                translationName=output_path.name,
            )
            print(f"Output cache hit for {paper_id}: {output_path.relative_to(REPO_ROOT)}", flush=True)
            return JSONResponse(
                {
                    "ok": True,
                    "cached": True,
                    "paperId": paper_id,
                    "output": str(output_path.relative_to(REPO_ROOT)),
                    "pdf_url": local_pdf_url,
                    "pdf_name": pdf_path.name if pdf_path else "",
                    "file_url": paper_file_url(pdf_path.name) if pdf_path else "",
                    "viewer_url": f"/viewer/?pdf={local_pdf_url}&translation=./translations/{output_path.name}",
                    "sections": 0,
                    "paragraphs": 0,
                }
            )

        if not dry_run and not deepseek_api_key():
            raise HTTPException(status_code=400, detail="Set DEEPSEEK_API_KEY in .env or the shell.")
        if pdf_extractor not in {"auto", "pymupdf", "pypdf"}:
            raise HTTPException(status_code=400, detail="pdf_extractor must be auto, pymupdf, or pypdf.")
        if source_mode not in {"auto", "latex", "pdf"}:
            raise HTTPException(status_code=400, detail="source_mode must be auto, latex, or pdf.")

        if pdf_path and source_mode in {"auto", "latex"}:
            arxiv_id = arxiv_id_from_paper_id(paper_id)
            if arxiv_id:
                try:
                    latex_path = await run_in_threadpool(ensure_arxiv_latex_source, arxiv_id)
                    upsert_paper_index(
                        paper_id,
                        sourcePath=str(latex_path.relative_to(REPO_ROOT)),
                        sourceMode="latex",
                    )
                except Exception as error:  # noqa: BLE001 - auto should fall back, latex should report.
                    if source_mode == "latex":
                        raise HTTPException(status_code=400, detail=f"Failed to load arXiv LaTeX source: {error}") from error
                    print(f"LaTeX source unavailable for {paper_id}: {error}; falling back to PDF.", flush=True)
                    latex_path = None
            elif source_mode == "latex":
                raise HTTPException(status_code=400, detail="LaTeX source mode is only automatic for arXiv PDFs.")

        print(
            "Generating translation JSON "
            f"title={title!r} output={output_path.name!r} "
            f"saved_pdf={saved_pdf!r} pdf_upload={getattr(pdf, 'filename', None)!r} "
            f"pages={pages!r} source_mode={source_mode!r} latex={(latex_path.name if latex_path else '')!r} "
            f"pdf_extractor={pdf_extractor!r} dry_run={dry_run} force={force}",
            flush=True,
        )
        try:
            result = await run_in_threadpool(
                generate_translation_json,
                pdf=None if latex_path else pdf_path,
                text=text_path,
                latex=latex_path,
                title=title,
                paper_url=local_pdf_url,
                output=output_path,
                model=model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                pages=pages,
                coverage=coverage,
                max_chars=max_chars,
                parallelism=parallelism,
                retries=retries,
                dry_run=dry_run,
                pdf_extractor=pdf_extractor,
            )
        except Exception as error:  # noqa: BLE001 - surface extraction/API errors to the UI.
            raise HTTPException(status_code=400, detail=str(error)) from error
        paragraph_count = sum(len(section["paragraphs"]) for section in result["sections"])
        print(
            f"Generated {output_path.relative_to(REPO_ROOT)} "
            f"({len(result['sections'])} sections, {paragraph_count} paragraphs)",
            flush=True,
        )
        if not dry_run:
            upsert_paper_index(
                paper_id,
                pdfName=pdf_path.name if pdf_path else "",
                sourceUrl=paper_url,
                pdfPath=str(pdf_path.relative_to(REPO_ROOT)) if pdf_path else "",
                translationPath=str(output_path.relative_to(REPO_ROOT)),
                translationName=output_path.name,
                sourcePath=str(latex_path.relative_to(REPO_ROOT)) if latex_path else "",
                sourceMode="latex" if latex_path else "pdf",
                sections=len(result["sections"]),
                paragraphs=paragraph_count,
            )
        return JSONResponse(
            {
                "ok": True,
                "cached": False,
                "paperId": paper_id,
                "output": str(output_path.relative_to(REPO_ROOT)),
                "pdf_url": local_pdf_url,
                "pdf_name": pdf_path.name if pdf_path else "",
                "file_url": paper_file_url(pdf_path.name) if pdf_path else "",
                "viewer_url": f"/viewer/?pdf={local_pdf_url}&translation=./translations/{output_path.name}",
                "sections": len(result["sections"]),
                "paragraphs": paragraph_count,
            }
        )
    finally:
        for path in temp_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.server:app", host="127.0.0.1", port=8787, reload=False)
