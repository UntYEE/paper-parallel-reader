#!/usr/bin/env python3
"""Generate section/paragraph translation JSON for the paper parallel reader."""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import json
import os
import re
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI
from pypdf import PdfReader


HEADING_RE = re.compile(
    r"^(?:(?i:abstract|references|acknowledg(?:e)?ments?|appendix)|[A-Z]\.\s+[A-Z][A-Za-z].+|(?:[1-9]\.?\s+|[1-9][0-9]?(?:\.[0-9]+)+\.?\s+)[A-Z][A-Za-z].+)$",
)
ABSTRACT_LINE_RE = re.compile(r"^abstract\b\s*[:—–-]?\s*(.*)$", re.IGNORECASE)
EMBEDDED_HEADING_RE = re.compile(
    r"([1-9][0-9]?(?:\.[0-9]+)*\.?\s+[A-Z][A-Za-z][A-Za-z0-9 ,:;()/-]{0,100})$"
)
FOOTNOTE_START_RE = re.compile(r"^[∗*†‡]\s*")
PAGE_ARTIFACT_RE = re.compile(
    r"^(\d+|arXiv:.*|[0-9]+(?:st|nd|rd|th)\s+Conference\s+on\s+.+)$",
    re.IGNORECASE,
)
PAGE_HEADER_RE = re.compile(r"^[A-Z][A-Za-z0-9 ,:;'-]{20,}\s+\d+$")
CAPTION_RE = re.compile(r"^(figure|table)\s+\d+\s*[:.]", re.IGNORECASE)
INLINE_CAPTION_RE = re.compile(r"(figure|table)\s+\d+\s*[:.]", re.IGNORECASE)
STOP_SECTION_RE = re.compile(
    r"^(references|bibliography|acknowledg(?:e)?ments?|appendix)\b",
    re.IGNORECASE,
)
PAGE_TITLE_RE = re.compile(r"^.+\s+\d+\s+I\d+\s*[·.]\s*T\d+", re.IGNORECASE)
VISUAL_TOKEN_RE = re.compile(r"\b(?:I|T|IN)\s*\d+\b|\bT\s*N\b|⋮|⋱|·")
MODEL_DIAGRAM_RE = re.compile(
    r"(Image\s+Encoder|Text\s+Encoder|A photo of|zero-shot prediction|Contrastive pre-training|dataset classi)",
    re.IGNORECASE,
)
CHART_LABEL_RE = re.compile(
    r"(#\s*of|average\s+score|accuracy|error\s+rate|gflops|top-?1|r@\d|"
    r"train\s+size|test\s+size|evaluation\s+metric|frequency|labeled\s+examples|"
    r"examples\s+per\s+class|score\s*\(%\)|pm0|pm10)",
    re.IGNORECASE,
)
TABLE_DENSE_RE = re.compile(
    r"\b(dataset|classes|model|metric|food-?101|cifar|imagenet|eurosat|resisc|"
    r"flowers102|birdsnap|sun397|stanford\s+cars|fgvc|mnist|kinetics)\b",
    re.IGNORECASE,
)
PDF_EXTRACTOR = Literal["auto", "pymupdf", "pypdf"]
SKIP_LATEX_ENVS = {
    "figure",
    "figure*",
    "table",
    "table*",
    "tikzpicture",
    "axis",
    "equation",
    "equation*",
    "align",
    "align*",
    "gather",
    "gather*",
    "multline",
    "multline*",
    "algorithm",
    "algorithm*",
    "algorithmic",
    "lstlisting",
}
LATEX_SECTION_RE = re.compile(
    r"\\(?P<command>part|chapter|section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\s*\{(?P<title>[^{}]+)\}",
    re.IGNORECASE,
)


@dataclass
class Paragraph:
    id: str
    page: int
    anchor: str
    source: str
    translation: str = ""
    note: str = ""


@dataclass
class Section:
    id: str
    title: str
    page_start: int
    page_end: int
    paragraphs: list[Paragraph] = field(default_factory=list)


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "section"


def parse_page_range(value: str | None, total_pages: int) -> set[int] | None:
    if not value:
        return None

    pages: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            pages.update(range(max(1, start), min(total_pages, end) + 1))
        else:
            page = int(part)
            if 1 <= page <= total_pages:
                pages.add(page)
    return pages


def extract_pdf_pages_pypdf(path: Path, page_range: str | None) -> list[tuple[int, str]]:
    reader = PdfReader(str(path))
    wanted_pages = parse_page_range(page_range, len(reader.pages))
    pages: list[tuple[int, str]] = []
    for index, page in enumerate(reader.pages, start=1):
        if wanted_pages is not None and index not in wanted_pages:
            continue
        text = page.extract_text() or ""
        pages.append((index, text))
    return pages


def fitz_block_text(block: dict[str, Any]) -> str:
    lines: list[str] = []
    for line in block.get("lines", []):
        spans = [span.get("text", "").strip() for span in line.get("spans", [])]
        text = re.sub(r"\s+", " ", " ".join(span for span in spans if span)).strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def rect_area(rect: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = rect
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def rect_overlap_ratio(
    rect: tuple[float, float, float, float],
    other: tuple[float, float, float, float],
) -> float:
    x0, y0, x1, y1 = rect
    ox0, oy0, ox1, oy1 = other
    ix0, iy0 = max(x0, ox0), max(y0, oy0)
    ix1, iy1 = min(x1, ox1), min(y1, oy1)
    overlap = rect_area((ix0, iy0, ix1, iy1))
    area = rect_area(rect)
    return overlap / area if area else 0.0


def is_dense_visual_block(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return True

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    visual_lines = sum(1 for line in lines if is_visual_artifact_line(line))
    digit_groups = len(re.findall(r"\d+(?:\.\d+)?", normalized))
    compact_digit_runs = len(re.findall(r"[A-Za-z)]\d{2,}|\d{2,}[A-Za-z(]", normalized))
    sentence_marks = len(re.findall(r"[.!?](?:\s|$)", normalized))
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", normalized)

    if is_visual_artifact(normalized):
        return True
    if visual_lines >= 2 and visual_lines >= max(2, len(lines) // 2):
        return True
    if CAPTION_RE.match(normalized) or INLINE_CAPTION_RE.search(normalized):
        return True
    if digit_groups >= 10 and sentence_marks <= 1 and len(words) <= 40:
        return True
    if compact_digit_runs >= 3 and sentence_marks <= 1 and len(normalized) <= 1200:
        return True
    return False


def extract_pdf_pages_pymupdf(path: Path, page_range: str | None) -> list[tuple[int, str]]:
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception as error:  # noqa: BLE001 - caller decides whether to fall back.
        raise RuntimeError("PyMuPDF is not available.") from error

    document = fitz.open(str(path))
    wanted_pages = parse_page_range(page_range, document.page_count)
    pages: list[tuple[int, str]] = []

    for index in range(document.page_count):
        page_number = index + 1
        if wanted_pages is not None and page_number not in wanted_pages:
            continue

        page = document[index]
        payload = page.get_text("dict", sort=True)
        image_rects: list[tuple[float, float, float, float]] = []
        text_blocks: list[tuple[tuple[float, float, float, float], str]] = []

        for block in payload.get("blocks", []):
            bbox = tuple(float(value) for value in block.get("bbox", (0, 0, 0, 0)))
            if block.get("type") == 1:
                image_rects.append(bbox)
                continue
            if block.get("type") != 0:
                continue

            text = fitz_block_text(block)
            if not text:
                continue
            if any(rect_overlap_ratio(bbox, image_rect) > 0.35 for image_rect in image_rects):
                continue
            if is_dense_visual_block(text):
                continue

            text_blocks.append((bbox, text))

        page_text = "\n\n".join(text for _, text in text_blocks)
        pages.append((page_number, page_text))

    document.close()
    return pages


def extract_pdf_pages(path: Path, page_range: str | None, extractor: PDF_EXTRACTOR = "auto") -> list[tuple[int, str]]:
    if extractor == "pypdf":
        return extract_pdf_pages_pypdf(path, page_range)
    if extractor == "pymupdf":
        return extract_pdf_pages_pymupdf(path, page_range)

    candidates: list[tuple[float, list[tuple[int, str]]]] = []
    for extract in (extract_pdf_pages_pymupdf, extract_pdf_pages_pypdf):
        try:
            pages = extract(path, page_range)
            if any(text.strip() for _, text in pages):
                candidates.append((score_extraction_quality(pages), pages))
        except Exception:
            continue

    if not candidates:
        return extract_pdf_pages_pypdf(path, page_range)
    return min(candidates, key=lambda item: item[0])[1]


def extract_text_pages(path: Path) -> list[tuple[int, str]]:
    return [(1, path.read_text(encoding="utf-8"))]


def read_text_lossy(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def strip_latex_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        escaped = False
        output: list[str] = []
        for char in line:
            if char == "%" and not escaped:
                break
            output.append(char)
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
        lines.append("".join(output))
    return "\n".join(lines)


def latex_root_for(path: Path) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    if path.is_dir():
        return path, None
    if path.suffix.lower() == ".tex":
        return path.parent, None

    temp = tempfile.TemporaryDirectory(prefix="paper-latex-")
    temp_path = Path(temp.name)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                target = (temp_path / member.filename).resolve()
                if temp_path.resolve() not in target.parents and target != temp_path.resolve():
                    raise RuntimeError("Unsafe path in zip archive.")
            archive.extractall(temp_path)
        return temp_path, temp

    try:
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                target = (temp_path / member.name).resolve()
                if temp_path.resolve() not in target.parents and target != temp_path.resolve():
                    raise RuntimeError("Unsafe path in tar archive.")
            archive.extractall(temp_path)
        return temp_path, temp
    except tarfile.TarError:
        pass

    try:
        with gzip.open(path, "rb") as compressed:
            data = compressed.read()
        if b"\\documentclass" not in data and b"\\begin{document}" not in data:
            raise RuntimeError("gzip payload does not look like LaTeX.")
        (temp_path / "main.tex").write_bytes(data)
        return temp_path, temp
    except Exception as error:  # noqa: BLE001 - report the archive type problem below.
        temp.cleanup()
        raise RuntimeError(f"Unsupported LaTeX source input: {path}") from error


def find_main_tex(root: Path, original: Path) -> Path:
    if original.suffix.lower() == ".tex" and original.exists():
        return original

    candidates = sorted(root.rglob("*.tex"))
    if not candidates:
        raise RuntimeError("No .tex files found in LaTeX source.")

    scored: list[tuple[int, Path]] = []
    for candidate in candidates:
        text = read_text_lossy(candidate)
        score = 0
        if "\\documentclass" in text:
            score += 100
        if "\\begin{document}" in text:
            score += 80
        if "\\title" in text:
            score += 10
        score += min(len(text) // 1000, 20)
        scored.append((score, candidate))
    return max(scored, key=lambda item: item[0])[1]


def resolve_tex_include(root: Path, current: Path, include_name: str) -> Path | None:
    include_name = include_name.strip()
    if not include_name:
        return None
    candidate = (current.parent / include_name).resolve()
    candidates = [candidate]
    if candidate.suffix.lower() != ".tex":
        candidates.append(candidate.with_suffix(".tex"))
    candidates.append((root / include_name).resolve())
    if (root / include_name).suffix.lower() != ".tex":
        candidates.append((root / include_name).with_suffix(".tex").resolve())
    for path in candidates:
        try:
            if path.exists() and path.is_file() and root.resolve() in path.resolve().parents:
                return path
        except OSError:
            continue
    return None


def inline_latex_inputs(path: Path, root: Path, seen: set[Path] | None = None) -> str:
    seen = seen or set()
    path = path.resolve()
    if path in seen:
        return ""
    seen.add(path)

    text = strip_latex_comments(read_text_lossy(path))
    include_re = re.compile(r"\\(?:input|include)\s*\{([^{}]+)\}")

    def replace_include(match: re.Match[str]) -> str:
        include_path = resolve_tex_include(root, path, match.group(1))
        if not include_path:
            return ""
        return "\n" + inline_latex_inputs(include_path, root, seen) + "\n"

    return include_re.sub(replace_include, text)


def drop_latex_environment(text: str, env: str) -> str:
    escaped_env = re.escape(env)
    pattern = re.compile(
        rf"\\begin\{{{escaped_env}\}}.*?\\end\{{{escaped_env}\}}",
        re.IGNORECASE | re.DOTALL,
    )
    return pattern.sub("\n", text)


def normalize_latex_inline(text: str) -> str:
    replacements = {
        "~": " ",
        r"\%": "%",
        r"\&": "&",
        r"\_": "_",
        r"\#": "#",
        r"\{": "{",
        r"\}": "}",
        "---": "-",
        "--": "-",
        "``": '"',
        "''": '"',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"\\(?:cite|citet|citep|citealp|citeauthor|citeyear|ref|eqref|autoref|cref|Cref)\*?(?:\[[^\]]*\]){0,2}\{[^{}]*\}", "", text)
    text = re.sub(r"\\(?:url|href)\s*\{([^{}]+)\}(?:\{([^{}]+)\})?", lambda m: m.group(2) or m.group(1), text)
    text = re.sub(r"\\(?:emph|textbf|textit|texttt|textsc|underline|textnormal)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:mathrm|mathbf|mathit|mathcal|operatorname)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\$[^$]{1,240}\$", " ", text)
    text = re.sub(r"\\\[[\s\S]*?\\\]", " ", text)
    text = re.sub(r"\\\([\s\S]*?\\\)", " ", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?(?:\{[^{}]*\})?", " ", text)
    text = re.sub(r"[{}]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def latex_to_plain_text(text: str) -> str:
    text = strip_latex_comments(text)
    document_match = re.search(r"\\begin\{document\}([\s\S]*)", text, re.IGNORECASE)
    if document_match:
        text = document_match.group(1)
    text = re.sub(r"\\end\{document\}[\s\S]*$", "", text, flags=re.IGNORECASE)

    for env in SKIP_LATEX_ENVS:
        text = drop_latex_environment(text, env)

    text = re.sub(r"\\caption(?:\[[^\]]*\])?\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\\maketitle\b", "\n", text)
    text = re.sub(r"\\begin\{abstract\}", "\nAbstract\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\\end\{abstract\}", "\n", text, flags=re.IGNORECASE)

    section_levels = {
        "part": 1,
        "chapter": 1,
        "section": 1,
        "subsection": 2,
        "subsubsection": 3,
    }
    counters = [0, 0, 0]

    def replace_section(match: re.Match[str]) -> str:
        command = match.group("command").lower()
        title = normalize_latex_inline(match.group("title")).strip()
        level = section_levels.get(command)
        if not level:
            return f"\n{title}\n"
        counters[level - 1] += 1
        for index in range(level, len(counters)):
            counters[index] = 0
        number = ".".join(str(value) for value in counters[:level] if value)
        return f"\n{number}. {title}\n"

    text = LATEX_SECTION_RE.sub(replace_section, text)
    text = re.sub(r"\\(?:paragraph|subparagraph)\*?\s*\{([^{}]+)\}", lambda m: f"\n{normalize_latex_inline(m.group(1)).strip()}\n", text)
    text = normalize_latex_inline(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_latex_pages(path: Path) -> list[tuple[int, str]]:
    root, temp = latex_root_for(path)
    try:
        main_tex = find_main_tex(root, path)
        expanded = inline_latex_inputs(main_tex, root)
        plain = latex_to_plain_text(expanded)
    finally:
        if temp:
            temp.cleanup()
    if not plain.strip():
        raise RuntimeError("No readable text was extracted from LaTeX source.")
    return [(1, plain)]


def split_page_paragraphs(text: str) -> list[str]:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"-\n(?=[a-z])", "", cleaned)
    paragraphs: list[str] = []

    def append_text(lines: list[str]) -> None:
        if not lines:
            return
        joined = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if not joined:
            return
        if len(joined) <= 1200:
            paragraphs.append(joined)
            return

        sentences = re.split(r"(?<=[.!?])\s+", joined)
        buffer: list[str] = []
        for sentence in sentences:
            buffer.append(sentence)
            if len(" ".join(buffer)) > 900:
                paragraphs.append(" ".join(buffer).strip())
                buffer = []
        if buffer:
            paragraphs.append(" ".join(buffer).strip())

    for block in re.split(r"\n\s*\n", cleaned):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        buffer: list[str] = []
        for line in lines:
            if HEADING_RE.match(line) and len(line) <= 120:
                append_text(buffer)
                paragraphs.append(line)
                buffer = []
                continue
            buffer.append(line)
        append_text(buffer)

    return [paragraph for paragraph in paragraphs if len(paragraph) >= 12 or HEADING_RE.match(paragraph)]


def embedded_heading(line: str) -> str:
    match = EMBEDDED_HEADING_RE.search(line)
    if not match:
        return ""
    heading = match.group(1).strip()
    return heading if HEADING_RE.match(heading) else ""


def is_visual_artifact(paragraph: str) -> bool:
    text = re.sub(r"\s+", " ", paragraph).strip()
    if not text:
        return True
    lower = text.lower()
    visual_hits = len(VISUAL_TOKEN_RE.findall(text))
    digit_groups = len(re.findall(r"\d+(?:\.\d+)?", text))
    number_hits = len(re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?%?(?![A-Za-z])", text))
    sentence_marks = len(re.findall(r"[.!?](?:\s|$)", text))
    compact_digit_runs = len(re.findall(r"[A-Za-z)]\d{2,}|\d{2,}[A-Za-z(]", text))
    if PAGE_TITLE_RE.match(text):
        return True
    if visual_hits >= 6 and MODEL_DIAGRAM_RE.search(text):
        return True
    if visual_hits >= 10 and len(text) < 1800:
        return True
    if MODEL_DIAGRAM_RE.search(text) and lower.count("pepper") >= 2:
        return True
    if (number_hits >= 10 or digit_groups >= 12 or compact_digit_runs >= 1) and CHART_LABEL_RE.search(text) and sentence_marks <= 2:
        return True
    if number_hits >= 12 and compact_digit_runs >= 3 and sentence_marks <= 2:
        return True
    if number_hits >= 18 and TABLE_DENSE_RE.search(text) and sentence_marks <= 3:
        return True
    return False


def is_visual_artifact_line(line: str) -> bool:
    text = re.sub(r"\s+", " ", line).strip()
    if not text:
        return False

    digit_groups = len(re.findall(r"\d+(?:\.\d+)?", text))
    compact_digit_runs = len(re.findall(r"[A-Za-z)]\d{2,}|\d{2,}[A-Za-z(]", text))
    sentence_marks = len(re.findall(r"[.!?](?:\s|$)", text))

    if re.fullmatch(r"[\d\s.+\-–—%]+", text) and digit_groups >= 3:
        return True
    if CHART_LABEL_RE.search(text) and (digit_groups >= 3 or compact_digit_runs >= 1 or len(text) <= 80):
        return True
    if digit_groups >= 8 and TABLE_DENSE_RE.search(text) and sentence_marks <= 1:
        return True
    return False


def clean_extracted_pages(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    cleaned_pages: list[tuple[int, str]] = []
    seen_abstract = False
    skip_front_matter = True

    for page, text in pages:
        lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        output: list[str] = []
        in_author_footnote = False
        caption_lines_remaining = 0

        for line in lines:
            if not line:
                caption_lines_remaining = 0
                output.append("")
                continue

            abstract_match = ABSTRACT_LINE_RE.match(line)
            if abstract_match:
                seen_abstract = True
                skip_front_matter = False
                in_author_footnote = False
                output.append("Abstract")
                abstract_text = abstract_match.group(1).strip()
                if abstract_text:
                    output.append(abstract_text)
                continue

            if skip_front_matter and not seen_abstract:
                continue

            if FOOTNOTE_START_RE.match(line):
                heading = embedded_heading(line)
                if heading:
                    in_author_footnote = False
                    output.append(heading)
                    continue
                in_author_footnote = True
                continue

            if CAPTION_RE.match(line) or INLINE_CAPTION_RE.search(line):
                caption_lines_remaining = 3
                continue

            if caption_lines_remaining:
                if HEADING_RE.match(line) and len(line) <= 120:
                    caption_lines_remaining = 0
                else:
                    caption_lines_remaining -= 1
                    continue

            if in_author_footnote:
                if HEADING_RE.match(line) and not line.lower().startswith(("references", "acknowledg", "appendix")):
                    in_author_footnote = False
                else:
                    continue

            if PAGE_ARTIFACT_RE.match(line) or PAGE_HEADER_RE.match(line):
                continue
            if is_visual_artifact_line(line):
                continue

            output.append(line)

        cleaned_pages.append((page, "\n".join(output)))

    return cleaned_pages


def new_section(title: str, page: int, existing_ids: set[str]) -> Section:
    base_id = slugify(title)
    section_id = base_id
    suffix = 2
    while section_id in existing_ids:
        section_id = f"{base_id}-{suffix}"
        suffix += 1
    existing_ids.add(section_id)
    return Section(id=section_id, title=title, page_start=page, page_end=page)


def segment_document(pages: list[tuple[int, str]]) -> list[Section]:
    pages = clean_extracted_pages(pages)
    sections: list[Section] = []
    section_ids: set[str] = set()
    current = new_section("Front Matter", pages[0][0] if pages else 1, section_ids)
    sections.append(current)

    paragraph_counts: dict[str, int] = {}

    for page, text in pages:
        for paragraph in split_page_paragraphs(text):
            if is_visual_artifact(paragraph):
                continue
            if HEADING_RE.match(paragraph) and len(paragraph) <= 120:
                if STOP_SECTION_RE.match(paragraph):
                    return [section for section in sections if section.paragraphs]
                current = new_section(paragraph, page, section_ids)
                sections.append(current)
                continue

            current.page_end = page
            count = paragraph_counts.get(current.id, 0) + 1
            paragraph_counts[current.id] = count
            paragraph_id = f"{current.id}-p{count}"
            current.paragraphs.append(
                Paragraph(
                    id=paragraph_id,
                    page=page,
                    anchor=f"{current.title}, paragraph {count}",
                    source=paragraph,
                )
            )

    return [section for section in sections if section.paragraphs]


def heading_order_key(title: str) -> tuple[int, ...] | None:
    match = re.match(r"^([1-9][0-9]?(?:\.[0-9]+)*)\.?\s+", title)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def score_extraction_quality(pages: list[tuple[int, str]]) -> float:
    try:
        sections = segment_document(pages)
    except Exception:
        return 1_000_000.0
    if not sections:
        return 1_000_000.0

    paragraphs = [paragraph.source for section in sections for paragraph in section.paragraphs]
    text = "\n".join(paragraphs)
    paragraph_count = len(paragraphs)
    score = 0.0

    if not any(section.title.lower().startswith("abstract") for section in sections[:3]):
        score += 80
    if paragraph_count < 8:
        score += 200
    if paragraph_count > 220:
        score += (paragraph_count - 220) * 0.8

    visual_needles = (
        "Pepper the",
        "I1·T",
        "I1 ·T",
        "Zero-Shot CLIP vs",
        "Average Score",
        "Train size",
        "Text Encoder",
        "Image Encoder",
    )
    for needle in visual_needles:
        if needle in text:
            score += 70

    previous_key: tuple[int, ...] | None = None
    for section in sections:
        title = section.title.strip()
        key = heading_order_key(title)
        if key:
            if previous_key and key < previous_key:
                score += 35
            previous_key = key
        if re.match(r"^[1-9]\s+[a-z]", title):
            score += 35
        if len(title) > 90 and not title.lower().startswith(("abstract", "references", "acknowledg", "appendix")):
            score += 15

    return score


def chunk_paragraphs(sections: list[Section], max_chars: int) -> list[list[Paragraph]]:
    chunks: list[list[Paragraph]] = []
    current: list[Paragraph] = []
    current_chars = 0

    for section in sections:
        for paragraph in section.paragraphs:
            size = len(paragraph.source)
            if current and current_chars + size > max_chars:
                chunks.append(current)
                current = []
                current_chars = 0
            current.append(paragraph)
            current_chars += size

    if current:
        chunks.append(current)
    return chunks


def translate_chunk(client: OpenAI, model: str, chunk: list[Paragraph], retries: int) -> dict[str, dict[str, str]]:
    payload = {
        "paragraphs": [
            {
                "id": paragraph.id,
                "page": paragraph.page,
                "anchor": paragraph.anchor,
                "source": paragraph.source,
            }
            for paragraph in chunk
        ]
    }
    system_prompt = (
        "You are a professional academic translator specialized in close reading of research papers. "
        "Your task is to translate only the main body paragraphs of academic PDF papers into Chinese.\n"
        "\n"
        "Scope requirements:\n"
        "- Translate the Abstract and the paper's main body text, ending before References/Bibliography.\n"
        "- Keep the Abstract as its own paragraph under the Abstract section.\n"
        "- Exclude all content before the Abstract, including title, author information, affiliations, keywords, venue information, copyright notices, and metadata.\n"
        "- Exclude References/Bibliography and everything after it, including appendices, acknowledgements, author biographies, supplementary material, funding statements, and ethics statements, unless explicitly requested.\n"
        "- Exclude page headers, footers, page numbers, figure/table captions, footnotes, equations-only blocks, and other non-paragraph artifacts unless they are necessary for understanding the surrounding main text.\n"
        "- Omit residual OCR or PDF extraction artifacts that look like figure labels, chart axes, legend text, table cells, dense numeric series, or diagram node labels.\n"
        "- If a paragraph does not belong to the main body, omit it from the output rather than translating it.\n"
        "\n"
        "Translation requirements:\n"
        "- Translate into formal, precise, and academically appropriate Chinese.\n"
        "- Preserve the logical structure, terminology, and argumentative nuance of the original text.\n"
        "- Translate literally enough for close reading; do not paraphrase loosely or summarize.\n"
        "- Preserve formulas, citations, section numbers, variable names, dataset names, method names, model names, and technical abbreviations.\n"
        "- For important technical terms, keep the English term in parentheses when useful, especially on first occurrence.\n"
        "- Keep mathematical notation, inline equations, citation markers, and references to figures/tables readable and faithful to the original.\n"
        "- Do not invent explanations, background knowledge, or missing content.\n"
        "\n"
        "Output requirements:\n"
        "- Return valid JSON only. Do not include markdown, comments, or extra text outside JSON.\n"
        "- JSON shape: {\"items\":[{\"id\":\"paragraph-id\",\"translation\":\"中文翻译\",\"note\":\"\"}]}.\n"
        "- Do not drop paragraph ids for translated main-body paragraphs.\n"
        "- Keep the original paragraph order.\n"
        "- Use the note field only for brief translation notes, such as ambiguous terminology or unresolved OCR/PDF extraction issues. Otherwise set note to an empty string.\n"
        "- If no valid main-body paragraphs are found, return {\"items\":[]}."
    )
    user_prompt = (
        "Translate this json payload. Return json with the same paragraph ids:\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                stream=False,
                max_tokens=8192,
            )
            content = response.choices[0].message.content or ""
            if not content.strip():
                raise RuntimeError("DeepSeek returned empty JSON content.")
            data = json.loads(content)
            if not isinstance(data.get("items"), list):
                raise ValueError("Response JSON must contain an items array.")
            return {item["id"]: item for item in data["items"]}
        except Exception as error:  # noqa: BLE001 - retries should catch API/JSON failures.
            last_error = error
            if attempt < retries:
                time.sleep(2**attempt)

    raise RuntimeError(f"Translation failed after {retries + 1} attempts: {last_error}")


def apply_translations(sections: list[Section], translations: dict[str, dict[str, str]]) -> None:
    for section in sections:
        for paragraph in section.paragraphs:
            item = translations.get(paragraph.id)
            if not item:
                paragraph.translation = ""
                paragraph.note = "Missing translation from API response."
                continue
            paragraph.translation = item.get("translation", "")
            paragraph.note = item.get("note", "")


def build_output(
    title: str,
    paper_url: str,
    coverage: str,
    source_note: str,
    sections: list[Section],
) -> dict[str, Any]:
    return {
        "title": title,
        "paperUrl": paper_url,
        "coverage": coverage,
        "source": source_note,
        "sections": [
            {
                "id": section.id,
                "title": section.title,
                "pageStart": section.page_start,
                "pageEnd": section.page_end,
                "paragraphs": [
                    {
                        "id": paragraph.id,
                        "page": paragraph.page,
                        "anchor": paragraph.anchor,
                        "translation": paragraph.translation,
                        **({"note": paragraph.note} if paragraph.note else {}),
                    }
                    for paragraph in section.paragraphs
                ],
            }
            for section in sections
        ],
    }


def create_deepseek_client() -> OpenAI:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    timeout = float(os.getenv("DEEPSEEK_TIMEOUT", "180"))
    if not api_key:
        raise RuntimeError("Set DEEPSEEK_API_KEY before calling the DeepSeek API.")
    return OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def bounded_parallelism(value: int, chunk_count: int) -> int:
    return max(1, min(value, chunk_count, 8))


def generate_translation_json(
    *,
    pdf: Path | None,
    text: Path | None,
    latex: Path | None = None,
    title: str,
    paper_url: str,
    output: Path,
    model: str | None,
    pages: str | None,
    coverage: str,
    max_chars: int,
    parallelism: int,
    retries: int,
    dry_run: bool,
    pdf_extractor: PDF_EXTRACTOR = "auto",
) -> dict[str, Any]:
    if pdf:
        extracted_pages = extract_pdf_pages(pdf, pages, pdf_extractor)
        source_name = str(pdf)
    elif latex:
        extracted_pages = extract_latex_pages(latex)
        source_name = str(latex)
    elif text:
        extracted_pages = extract_text_pages(text)
        source_name = str(text)
    else:
        raise ValueError("Either pdf, latex, or text must be provided.")

    if not extracted_pages:
        raise RuntimeError("No text pages were extracted.")

    sections = segment_document(extracted_pages)
    if not sections:
        raise RuntimeError("No paragraphs were detected.")

    if dry_run:
        for section in sections:
            for paragraph in section.paragraphs:
                paragraph.translation = "TODO"
                paragraph.note = "Dry run placeholder."
    else:
        active_model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        translations: dict[str, dict[str, str]] = {}
        chunks = chunk_paragraphs(sections, max_chars)
        workers = bounded_parallelism(parallelism, len(chunks))
        if workers == 1:
            client = create_deepseek_client()
            for index, chunk in enumerate(chunks, start=1):
                print(
                    f"Translating chunk {index}/{len(chunks)} ({len(chunk)} paragraphs)...",
                    file=sys.stderr,
                    flush=True,
                )
                translations.update(translate_chunk(client, active_model, chunk, retries))
        else:
            print(
                f"Translating {len(chunks)} chunks with parallelism={workers}...",
                file=sys.stderr,
                flush=True,
            )

            def translate_indexed_chunk(index_and_chunk: tuple[int, list[Paragraph]]) -> tuple[int, dict[str, dict[str, str]]]:
                index, chunk = index_and_chunk
                print(
                    f"Starting chunk {index}/{len(chunks)} ({len(chunk)} paragraphs)...",
                    file=sys.stderr,
                    flush=True,
                )
                return index, translate_chunk(create_deepseek_client(), active_model, chunk, retries)

            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(translate_indexed_chunk, (index, chunk))
                    for index, chunk in enumerate(chunks, start=1)
                ]
                for future in concurrent.futures.as_completed(futures):
                    index, chunk_translations = future.result()
                    translations.update(chunk_translations)
                    print(
                        f"Finished chunk {index}/{len(chunks)}.",
                        file=sys.stderr,
                        flush=True,
                    )
        apply_translations(sections, translations)

    source_note = (
        f"Generated from {source_name}. The PDF remains the authoritative original; "
        "this JSON stores Chinese translations and short anchors, not full original text."
    )
    result = build_output(title, paper_url, coverage, source_note, sections)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--pdf", type=Path, help="Path to a local PDF.")
    input_group.add_argument("--latex", type=Path, help="Path to a LaTeX .tex file, source directory, or archive.")
    input_group.add_argument("--text", type=Path, help="Path to extracted plain text.")
    parser.add_argument("--title", required=True, help="Paper title for the JSON metadata.")
    parser.add_argument("--paper-url", required=True, help="PDF or paper URL for the left reader pane.")
    parser.add_argument("--output", type=Path, required=True, help="Output JSON path.")
    parser.add_argument("--model", default=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"), help="DeepSeek model.")
    parser.add_argument("--pages", help="PDF page range, for example 1-3 or 1,3,5-7.")
    parser.add_argument(
        "--pdf-extractor",
        choices=("auto", "pymupdf", "pypdf"),
        default=os.getenv("PDF_TEXT_EXTRACTOR", "auto"),
        help="PDF text extractor. auto prefers PyMuPDF layout filtering and falls back to pypdf.",
    )
    parser.add_argument("--coverage", default="Full paper text extracted from the provided input.")
    parser.add_argument("--max-chars", type=int, default=12000, help="Approximate source chars per API call.")
    parser.add_argument(
        "--parallelism",
        type=int,
        default=env_int("DEEPSEEK_PARALLELISM", 3),
        help="Number of translation chunks to process concurrently. Use 1 for serial translation.",
    )
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true", help="Write section/paragraph scaffold without API calls.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        generate_translation_json(
            pdf=args.pdf,
            text=args.text,
            latex=args.latex,
            title=args.title,
            paper_url=args.paper_url,
            output=args.output,
            model=args.model,
            pages=args.pages,
            coverage=args.coverage,
            max_chars=args.max_chars,
            parallelism=args.parallelism,
            retries=args.retries,
            dry_run=args.dry_run,
            pdf_extractor=args.pdf_extractor,
        )
    except Exception as error:  # noqa: BLE001 - command line should print friendly errors.
        print(str(error), file=sys.stderr)
        return 1

    print(f"Wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
