#!/usr/bin/env python3
"""Local backend for generating viewer translation JSON with the DeepSeek API."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import re
import shutil
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf import PdfReader

from backend.paper_qa import (
    answer_question,
    clear_history,
    get_history,
    index_status,
    index_translation,
)
from backend.paper_search import discover_papers
from backend.local_security import DownloadTooLargeError, UnsafeRemoteURLError, download_remote_bytes
from backend.task_store import get_task, mark_unfinished_tasks_interrupted, now_iso, save_task, update_task
from scripts.generate_translation_json import create_deepseek_client, generate_translation_json


REPO_ROOT = Path(__file__).resolve().parents[1]
VIEWER_DIR = REPO_ROOT / "viewer"


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


ENV_PATH = REPO_ROOT / ".env"
load_env(ENV_PATH)


def configured_path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


DATA_ROOT = configured_path("PAPER_DATA_DIR", REPO_ROOT / "data")
PAPERS_DIR = DATA_ROOT
SOURCES_DIR = DATA_ROOT / "latex_sources"
DEFAULT_OUTPUT_DIR = DATA_ROOT / "translations"
ASSET_DIR = DATA_ROOT / "paper-assets"
MODEL_CACHE_DIR = configured_path("MODEL_CACHE_DIR", DATA_ROOT / "model-cache")
INDEX_PATH = DATA_ROOT / "paper_index.json"
QA_DB_PATH = DATA_ROOT / "paper_qa.sqlite3"
TASK_DB_PATH = DATA_ROOT / "generation_tasks.sqlite3"
for directory in (DATA_ROOT, SOURCES_DIR, DEFAULT_OUTPUT_DIR, ASSET_DIR, MODEL_CACHE_DIR):
    directory.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HF_HOME", str(MODEL_CACHE_DIR / "huggingface"))
os.environ.setdefault("TORCH_HOME", str(MODEL_CACHE_DIR / "torch"))


def copy_missing_tree(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            copy_missing_tree(child, destination / child.name)
    elif not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def migrate_legacy_data() -> None:
    marker = DATA_ROOT / ".legacy-data-copied-v1"
    legacy_papers = (REPO_ROOT / "papers_to_translate").resolve()
    if marker.exists() or DATA_ROOT == legacy_papers:
        return
    copy_missing_tree(legacy_papers, DATA_ROOT)
    legacy_translations = VIEWER_DIR / "translations"
    if legacy_translations.exists():
        for translation in legacy_translations.glob("*.json"):
            if translation.name != "attention-is-all-you-need.sample.json":
                copy_missing_tree(translation, DEFAULT_OUTPUT_DIR / translation.name)
    copy_missing_tree(VIEWER_DIR / "paper-assets", ASSET_DIR)
    marker.write_text("Legacy data was copied without deleting its original files.\n", encoding="utf-8")


def warn_if_env_permissions_are_broad(path: Path) -> None:
    if os.name != "posix" or not path.exists():
        return
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        print("Warning: .env is readable by other local users; consider running chmod 600 .env.", flush=True)


migrate_legacy_data()
warn_if_env_permissions_are_broad(ENV_PATH)
mark_unfinished_tasks_interrupted(TASK_DB_PATH)


def configured_origins() -> list[str]:
    raw = os.getenv("APP_ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000")
    return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]


ALLOWED_ORIGINS = configured_origins()

app = FastAPI(title="Paper Translation Backend")
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def reject_cross_origin_writes(request: Request, call_next):  # noqa: ANN001
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in ALLOWED_ORIGINS:
            return JSONResponse(status_code=403, content={"detail": "Cross-origin local API request rejected."})
    return await call_next(request)


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


def data_relative(path: Path) -> str:
    return str(path.resolve().relative_to(DATA_ROOT))


def storage_label(path: Path) -> str:
    try:
        return data_relative(path)
    except ValueError:
        return path.name


def translation_file_url(path: Path) -> str:
    return f"/viewer/translations/{safe_output_name(path.name)}"


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


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


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
    translation_name = record.get("translationName")
    if isinstance(translation_name, str) and translation_name:
        path = (DEFAULT_OUTPUT_DIR / safe_output_name(translation_name)).resolve()
        if path.parent == DEFAULT_OUTPUT_DIR.resolve() and path.exists():
            return path
    translation = record.get("translationPath")
    if not isinstance(translation, str) or not translation:
        return None
    candidates = [(DATA_ROOT / translation).resolve(), (REPO_ROOT / translation).resolve()]
    for path in candidates:
        if path.parent in {DEFAULT_OUTPUT_DIR.resolve(), (VIEWER_DIR / "translations").resolve()} and path.exists():
            return path
    return None


def ensure_qa_index(paper_id: str) -> tuple[Path, dict[str, object]]:
    translation_path = indexed_translation_path(paper_id)
    if not translation_path:
        raise HTTPException(status_code=404, detail="No generated translation is indexed for this paper.")
    status = index_translation(QA_DB_PATH, paper_id, translation_path)
    if not status.get("ready"):
        raise HTTPException(status_code=400, detail="The translation contains no readable paragraphs to index.")
    return translation_path, status


class PaperChatRequest(BaseModel):
    sessionId: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=6000)
    selectedParagraphId: Optional[str] = Field(default=None, max_length=240)
    historyLimit: int = Field(default=8, ge=0, le=20)


class PaperSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    limit: int = Field(default=6, ge=1, le=10)


class GenerationTaskRequest(BaseModel):
    savedPdf: str = Field(min_length=1, max_length=240)
    title: str = Field(min_length=1, max_length=500)
    paperUrl: str = Field(default="", max_length=2000)
    outputName: str = Field(min_length=1, max_length=240)
    pages: Optional[str] = Field(default=None, max_length=200)
    coverage: str = Field(default="Full paper text extracted from the provided input.", max_length=1000)
    model: Optional[str] = Field(default=None, max_length=120)
    maxChars: int = Field(default=4000, ge=800, le=20000)
    parallelism: int = Field(default_factory=lambda: env_int("DEEPSEEK_PARALLELISM", 3), ge=1, le=8)
    retries: int = Field(default=2, ge=0, le=5)
    pdfExtractor: str = Field(default_factory=lambda: os.getenv("PDF_TEXT_EXTRACTOR", "auto"))
    sourceMode: str = Field(default_factory=lambda: os.getenv("PAPER_SOURCE_MODE", "auto"))
    force: bool = False


GENERATION_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="paper-generation")


def update_generation_task(task_id: str, **fields: object) -> None:
    update_task(TASK_DB_PATH, task_id, **fields)


def public_generation_task(task_id: str) -> dict[str, object]:
    task = get_task(TASK_DB_PATH, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Generation task not found.")
    return task


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
    return paper_file_url(name)


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

    candidate_lines = lines[:40]
    email_re = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
    institution_re = re.compile(r"\b(UC|university|institute|college|laboratory|lab|research)\b", re.IGNORECASE)
    person_name_re = re.compile(r"^(?:[A-Z][A-Za-z'.-]+\s+){1,4}[A-Z][A-Za-z'.-]+$")

    for index, line in enumerate(candidate_lines):
        if stop_re.match(line):
            break
        if title_lines and affiliation_re.match(line):
            break
        if noise_re.match(line):
            continue
        if title_lines and authorish_re.search(line):
            break
        next_lines = candidate_lines[index + 1:index + 3]
        if title_lines and person_name_re.match(line) and any(
            email_re.search(next_line) or institution_re.search(next_line) for next_line in next_lines
        ):
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
        pdfPath=data_relative(path),
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


def json_event_dumps(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str) -> dict[str, str]:
    import json

    data = json.loads(value)
    return data if isinstance(data, dict) else {}


async def read_upload_limited(upload: UploadFile) -> bytes:
    max_bytes = env_int("MAX_UPLOAD_MB", 100) * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=f"Upload exceeds the {env_int('MAX_UPLOAD_MB', 100)} MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


async def write_upload(upload: UploadFile, suffix: str) -> Path:
    data = await read_upload_limited(upload)
    if not data:
        raise HTTPException(status_code=400, detail=f"{upload.filename or 'upload'} is empty.")
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    path = Path(handle.name)
    handle.write(data)
    handle.close()
    return path


async def save_uploaded_pdf(upload: UploadFile, source_url: str, fallback_name: str) -> Path:
    data = await read_upload_limited(upload)
    if not data:
        raise HTTPException(status_code=400, detail=f"{upload.filename or 'PDF upload'} is empty.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Uploaded content does not look like a PDF.")

    filename = safe_pdf_name(upload.filename or fallback_name)
    path = paper_path(filename)
    atomic_write_bytes(path, data)
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
                    translation_file_url(cached_translation) if cached_translation else ""
                ),
            }
        )
    return {"ok": True, "papers": papers}


@app.post("/api/search-papers")
async def search_papers(request: PaperSearchRequest) -> JSONResponse:
    index = load_index()
    records = index.get("papers", {})
    cached_records = list(records.values()) if isinstance(records, dict) else []
    result = await run_in_threadpool(
        discover_papers,
        request.query.strip(),
        cached_records,
        request.limit,
        deepseek_api_key(),
    )
    return JSONResponse({"ok": True, "query": request.query.strip(), **result})


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
    return download_remote_bytes(
        url,
        accept="application/pdf,*/*",
        max_bytes=env_int("MAX_DOWNLOAD_MB", 100) * 1024 * 1024,
        timeout=float(os.getenv("PAPER_DOWNLOAD_TIMEOUT", "75")),
    )


def download_binary_bytes(url: str, *, accept: str, max_time: str = "75") -> bytes:
    return download_remote_bytes(
        url,
        accept=accept,
        max_bytes=env_int("MAX_DOWNLOAD_MB", 100) * 1024 * 1024,
        timeout=float(max_time),
    )


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

    atomic_write_bytes(path, data)
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
    except DownloadTooLargeError as error:
        raise HTTPException(status_code=413, detail=f"PDF download rejected: {error}") from error
    except UnsafeRemoteURLError as error:
        raise HTTPException(status_code=400, detail=f"PDF URL rejected: {error}") from error
    except Exception as error:  # noqa: BLE001 - report download failures clearly to the UI.
        raise HTTPException(status_code=400, detail=f"Failed to download PDF: {error}") from error

    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Downloaded content does not look like a PDF.")

    atomic_write_bytes(path, data)
    write_paper_meta(path, url)
    return JSONResponse(paper_payload(path, url, cached=False))


def run_generation_task(task_id: str, request: GenerationTaskRequest) -> None:
    update_generation_task(task_id, status="running")
    try:
        pdf_path = paper_path(request.savedPdf)
        if not pdf_path.exists():
            raise RuntimeError("Saved PDF not found.")
        if request.pdfExtractor not in {"auto", "pymupdf", "pypdf"}:
            raise RuntimeError("pdfExtractor must be auto, pymupdf, or pypdf.")
        if request.sourceMode not in {"auto", "latex", "pdf"}:
            raise RuntimeError("sourceMode must be auto, latex, or pdf.")

        meta = read_paper_meta(pdf_path)
        paper_id = meta.get("paperId") or paper_id_from_url(request.paperUrl) or paper_id_from_filename(pdf_path.name)
        paper_id = str(paper_id)
        local_pdf_url = paper_viewer_url(pdf_path.name)
        latex_path: Optional[Path] = None
        if request.sourceMode in {"auto", "latex"}:
            arxiv_id = arxiv_id_from_paper_id(paper_id)
            if arxiv_id:
                try:
                    latex_path = ensure_arxiv_latex_source(arxiv_id)
                except Exception:
                    if request.sourceMode == "latex":
                        raise
            elif request.sourceMode == "latex":
                raise RuntimeError("LaTeX source mode is only automatic for arXiv PDFs.")

        output_path = DEFAULT_OUTPUT_DIR / safe_output_name(request.outputName)

        def report(progress: dict[str, object]) -> None:
            update_generation_task(task_id, progress=progress)

        result = generate_translation_json(
            pdf=pdf_path if request.sourceMode != "latex" else None,
            text=None,
            latex=latex_path if request.sourceMode != "pdf" else None,
            title=request.title,
            paper_url=local_pdf_url,
            output=output_path,
            model=request.model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            pages=request.pages,
            coverage=request.coverage,
            max_chars=request.maxChars,
            parallelism=request.parallelism,
            retries=request.retries,
            dry_run=False,
            pdf_extractor=request.pdfExtractor,
            progress_callback=report,
            resume=not request.force,
        )
        result["paperId"] = paper_id
        output_path.write_text(json_dumps(result), encoding="utf-8")
        paragraph_count = sum(len(section["paragraphs"]) for section in result["sections"])
        upsert_paper_index(
            paper_id,
            pdfName=pdf_path.name,
            sourceUrl=request.paperUrl,
            pdfPath=data_relative(pdf_path),
            translationPath=data_relative(output_path),
            translationName=output_path.name,
            sourcePath=data_relative(latex_path) if latex_path else "",
            sourceMode=result.get("extractionMethod", "latex" if latex_path else "pdf"),
            sections=len(result["sections"]),
            paragraphs=paragraph_count,
        )
        try:
            index_translation(QA_DB_PATH, paper_id, output_path)
        except Exception as error:  # noqa: BLE001 - generation remains successful.
            print(f"QA indexing failed for {paper_id}: {error}", flush=True)
        update_generation_task(
            task_id,
            status="completed",
            progress=result.get("translationProgress", {}),
            result={
                "paperId": paper_id,
                "output": data_relative(output_path),
                "translationUrl": translation_file_url(output_path),
                "pdfUrl": local_pdf_url,
                "sections": len(result["sections"]),
                "paragraphs": paragraph_count,
            },
        )
    except Exception as error:  # noqa: BLE001 - task errors are reported through status.
        current = public_generation_task(task_id)
        progress = dict(current.get("progress", {})) if isinstance(current.get("progress"), dict) else {}
        progress["status"] = "failed"
        update_generation_task(task_id, status="failed", progress=progress, error=str(error))


@app.post("/api/generation-tasks", status_code=202)
def create_generation_task(request: GenerationTaskRequest) -> dict[str, object]:
    if not deepseek_api_key():
        raise HTTPException(status_code=400, detail="Set DEEPSEEK_API_KEY before generating.")
    task_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    save_task(TASK_DB_PATH, {
        "taskId": task_id,
        "status": "queued",
        "revision": 0,
        "createdAt": now,
        "updatedAt": now,
        "progress": {
            "status": "queued",
            "completedBatches": 0,
            "totalBatches": 0,
            "retryCount": 0,
            "totalTokens": 0,
            "estimatedCostUsd": 0,
        },
    })
    GENERATION_EXECUTOR.submit(run_generation_task, task_id, request)
    return public_generation_task(task_id)


@app.get("/api/generation-tasks/{task_id}")
def get_generation_task(task_id: str) -> dict[str, object]:
    return public_generation_task(task_id)


@app.get("/api/generation-tasks/{task_id}/events")
async def generation_task_events(task_id: str) -> StreamingResponse:
    public_generation_task(task_id)

    async def stream():
        revision = -1
        while True:
            task = public_generation_task(task_id)
            current_revision = int(task.get("revision", 0))
            if current_revision != revision:
                revision = current_revision
                yield f"data: {json_event_dumps(task)}\n\n"
            if task.get("status") in {"completed", "failed", "interrupted"}:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/generate")
async def generate(
    title: Annotated[str, Form()],
    paper_url: Annotated[str, Form()],
    output_name: Annotated[str, Form()],
    pages: Annotated[Optional[str], Form()] = None,
    coverage: Annotated[str, Form()] = "Full paper text extracted from the provided input.",
    model: Annotated[Optional[str], Form()] = None,
    max_chars: Annotated[int, Form()] = 4000,
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
            print(f"Cache hit for {paper_id}: {storage_label(cached_path)}", flush=True)
            return JSONResponse(
                {
                    "ok": True,
                    "cached": True,
                    "paperId": paper_id,
                    "output": storage_label(cached_path),
                    "translation_url": translation_file_url(cached_path),
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
                pdfPath=data_relative(pdf_path) if pdf_path else "",
                translationPath=data_relative(output_path),
                translationName=output_path.name,
            )
            print(f"Output cache hit for {paper_id}: {data_relative(output_path)}", flush=True)
            return JSONResponse(
                {
                    "ok": True,
                    "cached": True,
                    "paperId": paper_id,
                    "output": data_relative(output_path),
                    "translation_url": translation_file_url(output_path),
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
                        sourcePath=data_relative(latex_path),
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
                pdf=pdf_path if source_mode != "latex" else None,
                text=text_path,
                latex=latex_path if source_mode != "pdf" else None,
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
                resume=not force,
            )
        except Exception as error:  # noqa: BLE001 - surface extraction/API errors to the UI.
            raise HTTPException(status_code=400, detail=str(error)) from error
        result["paperId"] = paper_id
        output_path.write_text(json_dumps(result), encoding="utf-8")
        paragraph_count = sum(len(section["paragraphs"]) for section in result["sections"])
        status_counts = result.get("statusCounts", {})
        print(
            f"Generated {data_relative(output_path)} "
            f"({len(result['sections'])} sections, {paragraph_count} paragraphs)",
            flush=True,
        )
        if not dry_run:
            upsert_paper_index(
                paper_id,
                pdfName=pdf_path.name if pdf_path else "",
                sourceUrl=paper_url,
                pdfPath=data_relative(pdf_path) if pdf_path else "",
                translationPath=data_relative(output_path),
                translationName=output_path.name,
                sourcePath=data_relative(latex_path) if latex_path else "",
                sourceMode=result.get("extractionMethod", "latex" if latex_path else "pdf"),
                sections=len(result["sections"]),
                paragraphs=paragraph_count,
            )
            try:
                await run_in_threadpool(index_translation, QA_DB_PATH, paper_id, output_path)
            except Exception as error:  # noqa: BLE001 - translation remains usable if QA indexing fails.
                print(f"QA indexing failed for {paper_id}: {error}", flush=True)
        return JSONResponse(
            {
                "ok": True,
                "cached": False,
                "paperId": paper_id,
                "output": data_relative(output_path),
                "translation_url": translation_file_url(output_path),
                "pdf_url": local_pdf_url,
                "pdf_name": pdf_path.name if pdf_path else "",
                "file_url": paper_file_url(pdf_path.name) if pdf_path else "",
                "viewer_url": f"/viewer/?pdf={local_pdf_url}&translation=./translations/{output_path.name}",
                "sections": len(result["sections"]),
                "paragraphs": paragraph_count,
                "status_counts": status_counts,
                "extraction_method": result.get("extractionMethod", ""),
            }
        )
    finally:
        for path in temp_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


@app.get("/api/papers/{paper_id}/qa-status")
def paper_qa_status(paper_id: str) -> dict[str, object]:
    translation_path = indexed_translation_path(paper_id)
    if not translation_path:
        raise HTTPException(status_code=404, detail="No generated translation is indexed for this paper.")
    status = index_status(QA_DB_PATH, paper_id, translation_path)
    return {"ok": True, "paperId": paper_id, **status}


@app.post("/api/papers/{paper_id}/chat")
async def paper_chat(paper_id: str, request: PaperChatRequest) -> JSONResponse:
    if not deepseek_api_key():
        raise HTTPException(status_code=400, detail="Set DEEPSEEK_API_KEY before asking questions.")
    _translation_path, status = await run_in_threadpool(ensure_qa_index, paper_id)
    try:
        answer = await run_in_threadpool(
            answer_question,
            QA_DB_PATH,
            paper_id,
            request.sessionId,
            request.question.strip(),
            create_deepseek_client(),
            os.getenv("QA_MODEL", os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")),
            request.selectedParagraphId,
            request.historyLimit,
            env_int("QA_EVIDENCE_LIMIT", 6),
        )
    except Exception as error:  # noqa: BLE001 - expose a useful local API error.
        raise HTTPException(status_code=400, detail=f"Paper question failed: {error}") from error
    return JSONResponse({"ok": True, "paperId": paper_id, "index": status, **answer})


@app.get("/api/papers/{paper_id}/chat/{session_id}")
def paper_chat_history(paper_id: str, session_id: str) -> dict[str, object]:
    ensure_qa_index(paper_id)
    return {"ok": True, "paperId": paper_id, "messages": get_history(QA_DB_PATH, paper_id, session_id)}


@app.delete("/api/papers/{paper_id}/chat/{session_id}")
def delete_paper_chat_history(paper_id: str, session_id: str) -> dict[str, object]:
    clear_history(QA_DB_PATH, paper_id, session_id)
    return {"ok": True, "paperId": paper_id}


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/viewer/")


@app.get("/viewer/translations/{filename}", include_in_schema=False)
def serve_translation(filename: str) -> FileResponse:
    safe_name = safe_output_name(filename)
    candidates = [DEFAULT_OUTPUT_DIR / safe_name, VIEWER_DIR / "translations" / safe_name]
    for path in candidates:
        resolved = path.resolve()
        if resolved.parent == path.parent.resolve() and resolved.exists():
            return FileResponse(resolved, media_type="application/json")
    raise HTTPException(status_code=404, detail="Translation not found.")


app.mount("/viewer/paper-assets", StaticFiles(directory=ASSET_DIR), name="paper-assets")
app.mount("/viewer", StaticFiles(directory=VIEWER_DIR, html=True), name="viewer")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.server:app",
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=env_int("APP_PORT", 8000),
        reload=False,
        workers=1,
    )
