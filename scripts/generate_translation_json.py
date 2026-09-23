#!/usr/bin/env python3
"""Generate section/paragraph translation JSON for the paper parallel reader."""

from __future__ import annotations

import argparse
import copy
import concurrent.futures
import gzip
import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import quote

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
CAPTION_RE = re.compile(
    r"^(?:(?:figure|table|scheme|chart)\s+\d+\s*[:.]|(?:fig|tab)\.?\s*\d+\s*[:.]|(?:图|表|式)\s*\d+)",
    re.IGNORECASE,
)
INLINE_CAPTION_RE = re.compile(
    r"(?:(?:figure|table|scheme|chart)\s+\d+\s*[:.]|(?:图|表)\s*\d+)",
    re.IGNORECASE,
)
# Section names for journals that print headings without any numbering. The list carries the
# common English names plus the CJK, German, French, and Spanish equivalents, because PDF-only
# literature is dominated by those languages.
JOURNAL_SECTION_NAMES = (
    "abstract",
    "introduction",
    "background",
    "related work",
    "related works",
    "literature review",
    "theory",
    "theoretical background",
    "materials and methods",
    "material and methods",
    "methods",
    "method",
    "methodology",
    "experimental",
    "experimental section",
    "experimental procedures",
    "experimental methods",
    "experimental details",
    "computational details",
    "results",
    "results and discussion",
    "results and conclusions",
    "discussion",
    "conclusions",
    "conclusion",
    "concluding remarks",
    "conclusion and outlook",
    "future work",
    "outlook",
    "acknowledgment",
    "acknowledgments",
    "acknowledgement",
    "acknowledgements",
    "references",
    "bibliography",
    "notes and references",
    "appendix",
    "appendices",
    "supporting information",
    "supplementary information",
    "supplementary material",
    "supporting material",
    "author contributions",
    "conflicts of interest",
    "conflict of interest",
    "competing interests",
    "declaration of competing interest",
    "data availability",
    "data availability statement",
    "funding",
    "abbreviations",
    # Chinese
    "摘要",
    "引言",
    "绪论",
    "前言",
    "研究背景",
    "相关工作",
    "文献综述",
    "理论基础",
    "材料与方法",
    "材料和方法",
    "方法",
    "方法学",
    "研究方法",
    "实验",
    "实验部分",
    "实验方法",
    "实验材料",
    "结果",
    "结果与分析",
    "结果与讨论",
    "结果和讨论",
    "讨论",
    "结论",
    "结论与展望",
    "总结",
    "展望",
    "致谢",
    "参考文献",
    "引用文献",
    "附录",
    "支持信息",
    "补充材料",
    "作者贡献",
    "利益冲突",
    "数据可用性",
    "基金项目",
    # German / French / Spanish
    "zusammenfassung",
    "einleitung",
    "hintergrund",
    "methoden",
    "ergebnisse",
    "diskussion",
    "schlussfolgerung",
    "danksagung",
    "literaturverzeichnis",
    "introduction générale",
    "méthodes",
    "méthodologie",
    "résultats",
    "discussion",
    "conclusion",
    "remerciements",
    "références",
    "introducción",
    "métodos",
    "resultados",
    "discusión",
    "conclusión",
    "agradecimientos",
    "referencias",
)
# Numbering styles: "1.2", "I.", "A.", "§3", "第1章", "一、", "（二）", "2" (CJK headings often
# separate the number from the title with a space).
SECTION_NUMBER_PATTERN = (
    r"(?:第\s*[0-9一二三四五六七八九十]+\s*[章节節部篇]"
    r"|§\s*[0-9]+(?:\.[0-9]+)*"
    r"|[0-9]+(?:\.[0-9]+)*"
    r"|[IVXLC]+"
    r"|[A-Z]"
    r"|[一二三四五六七八九十]+"
    r"|[（(][0-9一二三四五六七八九十]+[)）])"
    r"[.)、．]?\s*"
)
BARE_SECTION_NUMBER_RE = re.compile(
    r"^(?:[0-9]+(?:\.[0-9]+)*|[IVXLC]+|[A-Z]|[一二三四五六七八九十]+)[.)、．]?$"
)
STOP_SECTION_NAMES = (
    "references",
    "reference list",
    "references cited",
    "literature cited",
    "works cited",
    "bibliography",
    "notes and references",
    "acknowledgment",
    "acknowledgments",
    "acknowledgement",
    "acknowledgements",
    "appendix",
    "appendices",
    "supporting information",
    "supplementary information",
    "supplementary material",
    "conflicts of interest",
    "conflict of interest",
    "declaration of competing interest",
    "author contributions",
    "credit authorship contribution statement",
    "data availability",
    "data availability statement",
    "参考文献",
    "引用文献",
    "致谢",
    "附录",
    "支持信息",
    "补充材料",
    "作者贡献",
    "利益冲突",
    "数据可用性",
    "literaturverzeichnis",
    "danksagung",
    "anhang",
    "références",
    "remerciements",
    "annexe",
    "referencias",
    "agradecimientos",
    "apéndice",
)
ABSTRACT_LABELS = ("abstract", "摘要", "概要", "内容提要", "zusammenfassung", "r\u00e9sum\u00e9", "resumen")
KEYWORD_LABELS = ("keyword", "key word", "关键词", "关键字", "schlüsselwörter", "mots-clés", "palabras clave")
STOP_SECTION_RE = re.compile(
    r"^(?:" + SECTION_NUMBER_PATTERN + r")?"
    r"(?P<name>"
    + "|".join(re.escape(name) for name in sorted(STOP_SECTION_NAMES, key=len, reverse=True))
    + r")\s*[:.]?$",
    re.IGNORECASE,
)
# Journals outside CS/ML usually print unnumbered headings ("Introduction", "Experimental
# Section", "Results and Discussion", ...). Accept them as headings, optionally prefixed by a
# number, a Roman numeral, or a single letter label.
SECTION_NAME_HEADING_RE = re.compile(
    r"^(?:" + SECTION_NUMBER_PATTERN + r")?"
    r"(?P<name>"
    + "|".join(re.escape(name) for name in sorted(JOURNAL_SECTION_NAMES, key=len, reverse=True))
    + r")\s*[:.]?$",
    re.IGNORECASE,
)
ABSTRACT_HEADING_RE = re.compile(
    r"^\s*a\s*b\s*s\s*t\s*r\s*a\s*c\s*t\b\s*[:：—–-]?\s*(.*)$",
    re.IGNORECASE,
)
KEYWORDS_LINE_RE = re.compile(
    r"^\s*(?:"
    + "|".join(re.escape(label) for label in sorted(KEYWORD_LABELS, key=len, reverse=True))
    + r")\s*[:：]?\s*(.*)$",
    re.IGNORECASE,
)
# Page furniture that journals repeat on every page: DOIs, footers, download stamps, and the
# "Journal Name 2015, 80, 1234-1240" style running head.
TEXT_FURNITURE_RE = re.compile(
    r"(?:doi\s*:|https?://|dx\.doi\.org|downloaded\s+(?:from|by)|all\s+rights\s+reserved|"
    r"©|copyright|supporting\s+information|page\s+\d+\s+of\s+\d+|"
    r"\bvol\.?\s*\d+|\bissue\s+\d+|\bed\.\s*\d+|\bno\.\s*\d+|crossmark|open\s+access|"
    r"author\s+manuscript|advance\s+article|just\s+accepted|published\s+(?:online|on|as)\b|"
    r"received\s+\d|accepted\s+\d|submitted\s+\d)",
    re.IGNORECASE,
)
PUBLICATION_HEADER_RE = re.compile(
    r"(?:\b(?:19|20)\d{2}\s*,\s*\d+\s*,\s*\d+)"
    r"|(?:\b(?:19|20)\d{2}\b[^\n]{0,40}?\b\d+\s*[–—-]\s*\d+)"
    r"|(?:\(\s*(?:19|20)\d{2}\s*\)\s*\d+\s*(?:e|–|—|-)\s*\d+)",
    re.IGNORECASE,
)
# Author affiliations and reference-like lines are printed with running numbers, which makes them
# look like numbered headings.
AFFILIATION_RE = re.compile(
    r"\b(?:university|universität|université|institute|institution|department|faculty|school|"
    r"college|laboratory|laboratories|centre|center|hospital|academy|correspondence)\b",
    re.IGNORECASE,
)
CITATION_LIKE_RE = re.compile(r"et al\.|doi\s*:|@|\(\s*(?:19|20)\d{2}[a-z]?\s*\)", re.IGNORECASE)
# Metadata box lines that journals print next to the abstract.
ARTICLE_META_LINE_RE = re.compile(
    r"^(?:received|received in revised form|revised|accepted|available online|published|"
    r"article history|article info|contents lists available|journal homepage|homepage|"
    r"e-?mail address|corresponding author|https?://|www\.)",
    re.IGNORECASE,
)
ARTICLE_META_DATE_RE = re.compile(
    r"^\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{4}$",
    re.IGNORECASE,
)
# Letter-spaced labels ("A r t i c l e   i n f o") that must never become sections.
ARTICLE_META_COMPACT_LABELS = {"articleinfo", "highlights", "graphicalabstract"}
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
TRANSLATION_STATUSES = {"translated", "skipped", "needs_ocr", "needs_formula_recovery"}
# Bump when section splitting or the output schema changes so cached translations can be flagged
# as stale instead of silently showing an older structure.
GENERATOR_VERSION = "2026.09.1"
MATH_TOKEN_RE = re.compile(r"@@MATH_[0-9]{4,}@@")
REFERENCE_TOKEN_RE = re.compile(r"@@(?:XREF|CITE)_[0-9]{4,}@@")
LITERAL_TOKEN_RE = re.compile(r"@@LITERAL_[0-9]{4,}@@")
LABEL_TOKEN_RE = re.compile(r"@@LABEL_[0-9]{4,}@@")
PROTECTED_TOKEN_RE = re.compile(r"@@(?:MATH|XREF|CITE|LITERAL)_[0-9]{4,}@@")
REFERENCE_COMMAND_RE = re.compile(
    r"\\(?P<command>cite|citet|citep|citealp|citeauthor|citeyear|ref|eqref|autoref|cref|Cref)"
    r"\*?(?:\[[^\]]*\]){0,2}\{(?P<keys>[^{}]+)\}"
)
MATH_ENVIRONMENTS = (
    "equation",
    "equation*",
    "align",
    "align*",
    "alignat",
    "alignat*",
    "gather",
    "gather*",
    "multline",
    "multline*",
    "displaymath",
)
SKIP_LATEX_ENVS = {
    "figure",
    "figure*",
    "table",
    "table*",
    "tikzpicture",
    "axis",
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
    page_end: int = 0
    status: str = ""
    translation: str = ""
    note: str = ""


@dataclass
class Section:
    id: str
    title: str
    page_start: int
    page_end: int
    paragraphs: list[Paragraph] = field(default_factory=list)


class ProtectedTokenError(ValueError):
    """Raised when one translated paragraph changes immutable tokens."""

    def __init__(self, message: str, item_id: str = "") -> None:
        super().__init__(message)
        self.item_id = item_id


class FormulaTokenError(ProtectedTokenError):
    """Raised when a translated paragraph drops or duplicates protected formulas."""


class ReferenceTokenError(ProtectedTokenError):
    """Raised when a translated paragraph drops or duplicates protected references."""


class EmptyTranslationResponseError(RuntimeError):
    """Raised when DeepSeek returns no visible JSON content."""


class NonRetryableTranslationError(RuntimeError):
    """Raised for API failures that cannot succeed without external action."""


ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass
class TranslationRun:
    """Shared API client, counters, and checkpoint state for one paper run."""

    client: OpenAI
    fingerprint: str
    checkpoint_path: Path | None = None
    progress_callback: ProgressCallback | None = None
    completed_batches: int = 0
    total_batches: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    checkpoint_batches: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.checkpoint_path and self.checkpoint_path.exists():
            try:
                data = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
                if data.get("fingerprint") == self.fingerprint and isinstance(data.get("batches"), dict):
                    self.checkpoint_batches = data["batches"]
                    self.prompt_tokens = int(data.get("promptTokens", 0))
                    self.completion_tokens = int(data.get("completionTokens", 0))
                    self.cached_prompt_tokens = int(data.get("cachedPromptTokens", 0))
                    self.retries = int(data.get("retries", 0))
            except (OSError, ValueError, TypeError):
                self.checkpoint_batches = {}

    def snapshot(self, status: str = "running") -> dict[str, Any]:
        input_price = float(os.getenv("DEEPSEEK_INPUT_PRICE_USD_PER_MILLION", "0.44"))
        cached_price = float(os.getenv("DEEPSEEK_CACHED_INPUT_PRICE_USD_PER_MILLION", "0.014"))
        output_price = float(os.getenv("DEEPSEEK_OUTPUT_PRICE_USD_PER_MILLION", "1.32"))
        uncached = max(0, self.prompt_tokens - self.cached_prompt_tokens)
        estimated_cost = (
            uncached * input_price + self.cached_prompt_tokens * cached_price + self.completion_tokens * output_price
        ) / 1_000_000
        return {
            "status": status,
            "completedBatches": self.completed_batches,
            "totalBatches": self.total_batches,
            "retryCount": self.retries,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.prompt_tokens + self.completion_tokens,
            "cachedPromptTokens": self.cached_prompt_tokens,
            "estimatedCostUsd": round(estimated_cost, 6),
        }

    def emit(self, status: str = "running") -> None:
        if self.progress_callback:
            self.progress_callback(self.snapshot(status))

    def record_response(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if not usage:
            return
        details = getattr(usage, "prompt_tokens_details", None)
        with self._lock:
            self.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
            self.cached_prompt_tokens += int(getattr(details, "cached_tokens", 0) or 0)
        self.emit()

    def record_retry(self) -> None:
        with self._lock:
            self.retries += 1
        self.emit()

    def cached(self, batch_id: str) -> dict[str, dict[str, str]] | None:
        value = self.checkpoint_batches.get(batch_id)
        return copy.deepcopy(value) if value else None

    def complete(self, batch_id: str, translations: dict[str, dict[str, str]]) -> None:
        with self._lock:
            self.checkpoint_batches[batch_id] = copy.deepcopy(translations)
            self.completed_batches += 1
            self._write_checkpoint_locked()
        self.emit()

    def _write_checkpoint_locked(self) -> None:
        if not self.checkpoint_path:
            return
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fingerprint": self.fingerprint,
            "batches": self.checkpoint_batches,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "cachedPromptTokens": self.cached_prompt_tokens,
            "retries": self.retries,
        }
        handle = tempfile.NamedTemporaryFile(
            delete=False,
            dir=self.checkpoint_path.parent,
            prefix=f".{self.checkpoint_path.name}.",
            suffix=".part",
            mode="w",
            encoding="utf-8",
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(self.checkpoint_path)
        finally:
            temp_path.unlink(missing_ok=True)


@dataclass
class ReferenceBundle:
    references: dict[str, dict[str, Any]] = field(default_factory=dict)
    labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    citations: dict[str, dict[str, Any]] = field(default_factory=dict)
    label_tokens: dict[str, str] = field(default_factory=dict)
    assets: list[dict[str, Any]] = field(default_factory=list)
    literals: dict[str, str] = field(default_factory=dict)


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


def is_short_line_stack(text: str) -> bool:
    """Table columns and axis stacks extract as many short lines without prose."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) < 3:
        return False
    return all(
        text_weight(line) <= 4 and len(line.split()) <= 3 and not re.search(r"[.!?。；;]\s*$", line)
        for line in lines
    )


def merge_rects(
    rects: list[tuple[float, float, float, float]],
    gap: float = 6.0,
) -> list[tuple[float, float, float, float]]:
    """Merge touching or overlapping rectangles so drawings can be seen as one region."""
    merged = [tuple(rect) for rect in rects]
    changed = True
    while changed and len(merged) > 1:
        changed = False
        result: list[tuple[float, float, float, float]] = []
        for rect in merged:
            for index, kept in enumerate(result):
                if (
                    rect[0] - gap <= kept[2]
                    and kept[0] - gap <= rect[2]
                    and rect[1] - gap <= kept[3]
                    and kept[1] - gap <= rect[3]
                ):
                    result[index] = (
                        min(kept[0], rect[0]),
                        min(kept[1], rect[1]),
                        max(kept[2], rect[2]),
                        max(kept[3], rect[3]),
                    )
                    changed = True
                    break
            else:
                result.append(rect)
        merged = result
    return merged


def page_visual_regions(page: Any) -> list[tuple[float, float, float, float]]:
    """Figure and table areas: their text is decoration (axis labels, cells), not prose."""
    page_rect = (0.0, 0.0, float(page.rect.width), float(page.rect.height))
    page_area = rect_area(page_rect) or 1.0
    regions: list[tuple[float, float, float, float]] = []

    try:
        for table in page.find_tables().tables:
            regions.append(tuple(float(value) for value in table.bbox))
    except Exception:  # noqa: BLE001 - table detection is optional.
        pass

    try:
        drawings = [tuple(float(value) for value in item["rect"]) for item in page.get_drawings()]
        for rect in merge_rects(drawings):
            if rect_area(rect) >= page_area * 0.04 and rect[3] - rect[1] >= 30:
                regions.append(rect)
    except Exception:  # noqa: BLE001 - drawings are optional.
        pass

    try:
        for info in page.get_image_info():
            regions.append(tuple(float(value) for value in info.get("bbox", page_rect)))
    except Exception:  # noqa: BLE001 - image geometry is optional.
        pass

    return regions


def block_inside_regions(
    bbox: tuple[float, float, float, float],
    regions: list[tuple[float, float, float, float]],
    threshold: float = 0.6,
) -> bool:
    return any(rect_overlap_ratio(bbox, region) >= threshold for region in regions)


def widest_whitespace_gap(
    intervals: list[tuple[float, float]],
    minimum: float,
) -> tuple[float, float, float] | None:
    """Widest gap that no block crosses: a candidate column gutter or band separator."""
    if not intervals:
        return None
    spans = sorted(intervals)
    best: tuple[float, float, float] | None = None
    end = spans[0][1]
    for start, stop in spans[1:]:
        gap = start - end
        if gap >= minimum and (best is None or gap > best[0]):
            best = (gap, end, start)
        end = max(end, stop)
    return best


def order_blocks_reading_order(blocks: list[Any], depth: int = 0) -> list[Any]:
    """XY-cut reading order: full-width bands first, then columns left to right."""
    if depth >= 6 or len(blocks) <= 1:
        return list(blocks)

    x_intervals = [(block.bbox[0], block.bbox[2]) for block in blocks]
    y_intervals = [(block.bbox[1], block.bbox[3]) for block in blocks]
    width = max(x1 for _x0, x1 in x_intervals) - min(x0 for x0, _x1 in x_intervals)
    height = max(y1 for _y0, y1 in y_intervals) - min(y0 for y0, _y1 in y_intervals)
    vertical = widest_whitespace_gap(x_intervals, max(9.0, width * 0.02)) if width else None
    horizontal = widest_whitespace_gap(y_intervals, max(6.0, height * 0.012)) if height else None
    vertical_score = (vertical[0] / width) if vertical and width else 0.0
    horizontal_score = (horizontal[0] / height) if horizontal and height else 0.0

    if vertical and vertical_score >= 0.025 and vertical_score >= horizontal_score * 0.7:
        middle = (vertical[1] + vertical[2]) / 2
        first = [block for block in blocks if (block.bbox[0] + block.bbox[2]) / 2 <= middle]
        second = [block for block in blocks if (block.bbox[0] + block.bbox[2]) / 2 > middle]
        if first and second:
            return order_blocks_reading_order(first, depth + 1) + order_blocks_reading_order(second, depth + 1)

    if horizontal:
        middle = (horizontal[1] + horizontal[2]) / 2
        first = [block for block in blocks if (block.bbox[1] + block.bbox[3]) / 2 <= middle]
        second = [block for block in blocks if (block.bbox[1] + block.bbox[3]) / 2 > middle]
        if first and second:
            return order_blocks_reading_order(first, depth + 1) + order_blocks_reading_order(second, depth + 1)

    return sorted(blocks, key=lambda block: (round(block.bbox[1], 1), block.bbox[0]))


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
        records = page_block_records(page)
        kept = [
            record
            for record in records
            if not is_dense_visual_block(record.text)
            and not is_short_line_stack(record.text)
            and not (record.visual and text_weight(record.text) <= 20 and not CAPTION_RE.match(record.text))
        ]
        page_text = "\n\n".join(record.text for record in order_blocks_reading_order(kept))
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


@dataclass
class PdfTextBlock:
    text: str
    size: float
    bold_ratio: float
    line_count: int
    in_margin: bool
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    visual: bool = False


def is_bold_span(span: dict[str, Any]) -> bool:
    if int(span.get("flags") or 0) & (1 << 4):
        return True
    return bool(re.search(r"(bold|black|heavy|semibold|demi)", str(span.get("font") or ""), re.IGNORECASE))


def page_block_records(page: Any) -> list[PdfTextBlock]:
    """Text blocks of one page with typography, geometry, and figure/table flags."""
    height = float(page.rect.height) or 1.0
    regions = page_visual_regions(page)
    records: list[PdfTextBlock] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        text = fitz_block_text(block)
        if not text:
            continue
        sizes: list[float] = []
        bold_chars = 0
        total_chars = 0
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_text = str(span.get("text") or "")
                if not span_text.strip():
                    continue
                sizes.append(float(span.get("size") or 0.0))
                total_chars += len(span_text)
                if is_bold_span(span):
                    bold_chars += len(span_text)
        if not sizes:
            continue
        bbox = tuple(float(value) for value in block.get("bbox", (0.0, 0.0, 0.0, 0.0)))
        records.append(
            PdfTextBlock(
                text=text,
                size=max(sizes),
                bold_ratio=(bold_chars / total_chars) if total_chars else 0.0,
                line_count=len(block.get("lines") or []),
                in_margin=bbox[1] <= height * 0.12 or bbox[3] >= height * 0.88,
                bbox=bbox,
                visual=block_inside_regions(bbox, regions),
            )
        )
    return records


def pdf_page_blocks(path: Path, page_range: str | None = None) -> list[tuple[int, list[PdfTextBlock]]]:
    """Read per-block typography so journal layouts can be segmented without LaTeX."""
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - typography hints are optional.
        return []

    try:
        document = fitz.open(str(path))
    except Exception:  # noqa: BLE001 - unreadable PDFs simply get no hints.
        return []

    pages: list[tuple[int, list[PdfTextBlock]]] = []
    try:
        wanted_pages = parse_page_range(page_range, document.page_count)
        for index in range(document.page_count):
            page_number = index + 1
            if wanted_pages is not None and page_number not in wanted_pages:
                continue
            records = order_blocks_reading_order(page_block_records(document[index]))
            pages.append((page_number, records))
    finally:
        document.close()

    return pages


def heading_candidate_kind(record: PdfTextBlock, body_size: float, bold_reliable: bool) -> bool:
    """Typography verdict for a single block: is it shaped like a section heading?"""
    text = record.text
    stripped = text.strip()
    if not heading_text_allowed(stripped):
        return False
    named = is_section_heading(text)
    enlarged = body_size > 0 and record.size >= body_size * 1.08
    bold = bold_reliable and record.bold_ratio >= 0.6
    decorated = enlarged or bold
    if named:
        return True

    cjk_chars = cjk_char_count(text)
    if cjk_chars >= 2 and cjk_chars / max(1, len(text)) >= 0.25:
        # Chinese and Japanese headings are short, unpunctuated, and often merely bold.
        return bool(
            record.line_count <= 1
            and text_weight(text) <= 24
            and not CJK_SENTENCE_END_RE.search(text)
            and not any(mark in text for mark in ("。", "；", "，", "、"))
            and decorated
        )

    letters = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", text))
    words = len(text.split())
    if bool(re.search(r"[.!?]\s*$", text)) and words >= 5:
        return False
    if record.line_count <= 1:
        # Single words are only headings when they are known section names.
        return letters >= 3 and 2 <= words <= 12 and decorated
    # Wrapped headings must carry more text so figure fragments ("C\nCH 3") stay out.
    return letters >= 8 and words <= 10 and decorated


def line_looks_like_heading(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > 60:
        return False
    if len(text.split()) > 6:
        # Footnotes and table rows keep running long after a heading would stop.
        return False
    if not heading_text_allowed(text):
        return False
    if re.search(r"[.!?。]\s*$", text) and not SECTION_NAME_HEADING_RE.match(text):
        return False
    return is_section_heading(text)


def heading_text_allowed(text: str) -> bool:
    """Reject prose, affiliations, and reference lines that only look like headings."""
    stripped = text.strip()
    if not stripped:
        return False
    # "competing interests." / "the end of the discussion." are sentence fragments.
    if stripped.endswith(".") and re.search(r"[a-z]{2,}\.$", stripped):
        return False
    if re.match(r"^\s*\d+\s", stripped) and AFFILIATION_RE.search(stripped):
        return False
    if CITATION_LIKE_RE.search(stripped):
        return False
    if PUBLICATION_HEADER_RE.search(stripped):
        # "J. Widget Sci. 2024, 3, 100-112" is a running head, not a section.
        return False
    if cjk_char_count(stripped) == 0 and not re.search(r"[A-Z]", stripped):
        # An all-lowercase line is running prose, not a journal heading.
        return False
    return True


def pdf_structure_hints(
    path: Path,
    page_range: str | None = None,
) -> tuple[dict[int, set[str]], set[str], set[str]]:
    """Detect headings, running heads, and non-prose regions from PDF typography.

    Returns ``(heading_hints, noise_hints, blocked_hints)``: normalized heading text per page,
    normalized margin lines that repeat across pages (running heads, journal footers, DOIs),
    and text that must never become a section (figure labels, table cells).
    """
    pages = pdf_page_blocks(path, page_range)
    if not pages:
        return {}, set(), set()

    body_weights: Counter[float] = Counter()
    margin_pages: dict[str, set[int]] = {}
    blocked: set[str] = set()
    bold_chars = 0
    total_chars = 0

    for page_number, records in pages:
        for record in records:
            rounded = round(record.size, 1)
            total_chars += len(record.text)
            bold_chars += int(len(record.text) * record.bold_ratio)
            if record.visual:
                blocked.add(normalize_heading_text(record.text))
                for line in record.text.split("\n"):
                    blocked.add(normalize_heading_text(line))
            elif not record.in_margin:
                body_weights[rounded] += len(record.text)
            if record.in_margin and not record.visual:
                # Running heads usually carry a page number, so compare with digits masked.
                for line in record.text.split("\n"):
                    margin_pages.setdefault(noise_match_key(line), set()).add(page_number)
                margin_pages.setdefault(noise_match_key(record.text), set()).add(page_number)

    # Some CJK fonts are flagged bold for every glyph, which makes boldness useless as a signal.
    bold_reliable = bool(total_chars) and (bold_chars / total_chars) < 0.6
    # Character-weighted mode of the prose blocks: figure labels, table cells, and the reference
    # list are usually a different size than the running body text.
    body_size = body_weights.most_common(1)[0][0] if body_weights else 0.0
    page_count = len(pages)
    threshold = 2 if page_count <= 4 else max(2, int(page_count * 0.35))
    noise = {key for key, seen_pages in margin_pages.items() if len(seen_pages) >= threshold}

    hints: dict[int, set[str]] = {}
    for page_number, records in pages:
        page_hints = hints.setdefault(page_number, set())
        for record in records:
            text = record.text
            if record.visual:
                continue
            if record.in_margin and noise_match_key(text) in noise:
                continue
            if TEXT_FURNITURE_RE.search(text) or CAPTION_RE.match(text) or PUBLICATION_HEADER_RE.search(text):
                continue
            if is_visual_artifact(text):
                continue
            in_margin = record.in_margin
            named = is_section_heading(text)
            enlarged = body_size > 0 and record.size >= body_size * 1.08
            accepted = False
            if record.line_count <= 2 and len(text) <= 120:
                candidate = heading_candidate_kind(record, body_size, bold_reliable)
                if in_margin:
                    # Running heads and corner labels sit in the margin; only real titles and
                    # section names printed there (chapter openers) qualify.
                    candidate = (named and len(text) <= 60) or (
                        enlarged and 10 <= len(text) <= 60 and len(text.split()) <= 6
                    )
                if candidate:
                    page_hints.add(normalize_heading_text(text))
                    accepted = True
            if accepted:
                continue
            # Merged blocks keep a heading next to its body ("Methods\nField sampling\nBody")
            # or a marginal number next to its title ("1\nIntroduction").
            lines = text.split("\n")
            prose_like = any(text_weight(line) >= 6 or len(line) >= 40 for line in lines)
            if not prose_like:
                # A stack of short lines is a table column, not a heading followed by text.
                blocked.add(normalize_heading_text(text))
                for line in lines:
                    key = normalize_heading_text(line)
                    if key not in page_hints:
                        blocked.add(key)
                continue
            for line in lines:
                key = normalize_heading_text(line)
                if key in blocked or key in noise:
                    continue
                if line_looks_like_heading(line):
                    page_hints.add(key)
            if in_margin:
                # Headers, footnotes, and badges in the margin must never become sections, but
                # their text stays in the body so nothing is lost.
                blocked.add(normalize_heading_text(text))
                for line in lines:
                    key = normalize_heading_text(line)
                    if key not in page_hints:
                        blocked.add(key)

    blocked |= noise
    return hints, noise, blocked
def low_quality_page_numbers(pages: list[tuple[int, str]]) -> set[int]:
    low_quality: set[int] = set()
    for page, text in pages:
        compact = re.sub(r"\s+", "", text)
        readable = sum(1 for char in compact if char.isalnum())
        replacement_chars = text.count("\ufffd") + text.count("\x00")
        if readable < 120 or (compact and replacement_chars / len(compact) > 0.02):
            low_quality.add(page)
    return low_quality


def extract_pdf_pages_docling(
    path: Path,
    page_range: str | None,
    *,
    force_ocr: bool,
    enrich_formulas: bool = False,
) -> list[tuple[int, str]]:
    if os.getenv("ENABLE_OCR", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("Docling OCR is disabled. Restart with ENABLE_OCR=true to enable it.")
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import OcrAutoOptions, PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except Exception as error:  # noqa: BLE001 - Docling is an optional fallback.
        raise RuntimeError("Docling is not installed. Install project requirements with Python 3.10+.") from error

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = force_ocr
    pipeline_options.do_table_structure = False
    pipeline_options.do_formula_enrichment = enrich_formulas
    pipeline_options.document_timeout = float(os.getenv("DOCLING_TIMEOUT", "180"))
    if force_ocr:
        pipeline_options.ocr_options = OcrAutoOptions(
            lang=[os.getenv("DOCLING_OCR_LANGUAGE", "en")],
            force_full_page_ocr=True,
        )

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )
    total_pages = max(1, len(PdfReader(str(path)).pages))
    wanted_pages = parse_page_range(page_range, total_pages)
    page_items: dict[int, list[str]] = {}
    included_labels = {
        "text",
        "paragraph",
        "section_header",
        "list_item",
        "formula",
        "code",
    }

    selected_pages = wanted_pages or set(range(1, total_pages + 1))

    page_spans: list[tuple[int, int]] = []
    for page in sorted(selected_pages):
        if not page_spans or page != page_spans[-1][1] + 1:
            page_spans.append((page, page))
        else:
            page_spans[-1] = (page_spans[-1][0], page)

    for page_span in page_spans:
        result = converter.convert(path, page_range=page_span)
        for item, _level in result.document.iterate_items():
            text = str(getattr(item, "text", "") or "").strip()
            label = getattr(getattr(item, "label", None), "value", str(getattr(item, "label", "")))
            if not text or label not in included_labels:
                continue
            if label == "formula" and enrich_formulas and not text.startswith(("$", r"\[", r"\(")):
                text = f"$$\n{text}\n$$"
            provenance = getattr(item, "prov", None) or []
            if not provenance:
                continue
            page = int(provenance[0].page_no)
            if page not in selected_pages:
                continue
            page_items.setdefault(page, []).append(text)

    return [(page, "\n\n".join(page_items.get(page, []))) for page in sorted(selected_pages)]


def extract_pdf_pages_adaptive(
    path: Path,
    page_range: str | None,
    extractor: PDF_EXTRACTOR = "auto",
) -> tuple[list[tuple[int, str]], set[int]]:
    pages = extract_pdf_pages(path, page_range, extractor)
    low_quality = low_quality_page_numbers(pages)
    if not low_quality:
        return pages, set()

    requested = ",".join(str(page) for page in sorted(low_quality))
    try:
        print(
            f"Native PDF text is weak on pages {requested}; running Docling OCR fallback...",
            file=sys.stderr,
            flush=True,
        )
        ocr_pages = dict(
            extract_pdf_pages_docling(
                path,
                requested,
                force_ocr=True,
                enrich_formulas=True,
            )
        )
    except Exception as error:  # noqa: BLE001 - native extraction remains usable.
        print(f"Docling OCR fallback unavailable: {error}", file=sys.stderr, flush=True)
        return pages, set()

    merged: list[tuple[int, str]] = []
    replaced: set[int] = set()
    for page, text in pages:
        ocr_text = ocr_pages.get(page, "").strip()
        native_readable = sum(char.isalnum() for char in text)
        ocr_readable = sum(char.isalnum() for char in ocr_text)
        if page in low_quality and ocr_readable >= max(120, native_readable):
            merged.append((page, ocr_text))
            replaced.add(page)
        else:
            merged.append((page, text))
    return merged, replaced


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
        candidates.append(Path(f"{candidate}.tex"))
    root_candidate = (root / include_name).resolve()
    candidates.append(root_candidate)
    if root_candidate.suffix.lower() != ".tex":
        candidates.append(Path(f"{root_candidate}.tex"))
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


def parse_bibtex_entries(root: Path) -> dict[str, dict[str, str]]:
    """Parse the small, common BibTeX subset needed for citation display."""
    entries: dict[str, dict[str, str]] = {}
    entry_re = re.compile(r"@(?P<type>[A-Za-z]+)\s*\{\s*(?P<key>[^,\s]+)\s*,", re.IGNORECASE)
    field_re = re.compile(
        r"(?P<name>[A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:\{(?P<braced>(?:[^{}]|\{[^{}]*\})*)\}|\"(?P<quoted>(?:[^\"\\]|\\.)*)\")\s*,?",
        re.DOTALL,
    )
    for bib_path in sorted(root.rglob("*.bib")):
        text = strip_latex_comments(read_text_lossy(bib_path))
        cursor = 0
        while match := entry_re.search(text, cursor):
            depth = 1
            index = match.end()
            while index < len(text) and depth:
                if text[index] == "{":
                    depth += 1
                elif text[index] == "}":
                    depth -= 1
                index += 1
            body = text[match.end(): max(match.end(), index - 1)]
            fields = {
                item.group("name").lower(): re.sub(
                    r"\s+", " ", (item.group("braced") or item.group("quoted") or "").replace("{", "").replace("}", "")
                ).strip()
                for item in field_re.finditer(body)
            }
            entries[match.group("key").strip()] = {
                "type": match.group("type").lower(),
                "title": fields.get("title", ""),
                "authors": fields.get("author", ""),
                "year": fields.get("year", ""),
                "url": fields.get("url", "") or fields.get("doi", ""),
            }
            cursor = max(index, match.end())
    return entries


def classify_latex_labels(text: str) -> dict[str, dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    counters = {"equation": 0, "figure": 0, "table": 0, "algorithm": 0}
    section_counters = [0, 0, 0]
    current_section = ""
    events = re.compile(
        r"\\(?P<section>section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\s*\{(?P<title>[^{}]+)\}"
        r"|\\begin\{(?P<environment>equation\*?|align\*?|alignat\*?|gather\*?|multline\*?|figure\*?|table\*?|algorithm\*?)\}"
        r"|\\end\{(?P<end_environment>[^{}]+)\}"
        r"|\\label\s*\{(?P<label>[^{}]+)\}",
        re.IGNORECASE,
    )
    environment_stack: list[str] = []
    environment_start_stack: list[int] = []
    for match in events.finditer(text):
        if match.group("section"):
            level = {"section": 1, "subsection": 2, "subsubsection": 3}[match.group("section").lower()]
            section_counters[level - 1] += 1
            for index in range(level, len(section_counters)):
                section_counters[index] = 0
            current_section = ".".join(str(value) for value in section_counters[:level] if value)
        elif match.group("environment"):
            environment = match.group("environment").lower().rstrip("*")
            kind = "equation" if environment in {"equation", "align", "alignat", "gather", "multline"} else environment
            environment_stack.append(kind)
            environment_start_stack.append(match.end())
            counters[kind] = counters.get(kind, 0) + 1
        elif match.group("end_environment"):
            if environment_stack:
                environment_stack.pop()
                environment_start_stack.pop()
        elif match.group("label"):
            key = match.group("label").strip()
            prefix_kind = {
                "eq": "equation",
                "fig": "figure",
                "tab": "table",
                "tbl": "table",
                "alg": "algorithm",
                "sec": "section",
            }.get(key.split(":", 1)[0].lower())
            kind = environment_stack[-1] if environment_stack else (prefix_kind or "section")
            number = str(counters.get(kind, 0)) if kind != "section" else current_section
            if kind == "equation" and environment_start_stack:
                tag_matches = list(re.finditer(r"\\tag\*?\s*\{([^{}]+)\}", text[environment_start_stack[-1]:match.start()]))
                if tag_matches:
                    number = tag_matches[-1].group(1).strip()
            labels[key] = {"kind": kind, "number": number, "title": "", "page": None}
    return labels


def protect_latex_references(text: str, bundle: ReferenceBundle) -> str:
    citation_order: dict[str, int] = {
        key: int(value["number"])
        for key, value in bundle.citations.items()
        if value.get("number") is not None
    }

    def replace_reference(match: re.Match[str]) -> str:
        command = match.group("command")
        keys = [key.strip() for key in match.group("keys").split(",") if key.strip()]
        token_kind = "CITE" if command.lower().startswith("cite") else "XREF"
        token = f"@@{token_kind}_{len(bundle.references) + 1:04d}@@"
        if token_kind == "CITE":
            for key in keys:
                if key not in citation_order:
                    citation_order[key] = len(citation_order) + 1
                record = bundle.citations.setdefault(key, {})
                record["number"] = citation_order[key]
        bundle.references[token] = {"command": command, "keys": keys, "kind": token_kind.lower()}
        return token

    return REFERENCE_COMMAND_RE.sub(replace_reference, text)


def protect_literal_tokens(text: str, bundle: ReferenceBundle) -> str:
    def replace_literal(match: re.Match[str]) -> str:
        token = f"@@LITERAL_{len(bundle.literals) + 1:04d}@@"
        bundle.literals[token] = match.group(0)
        return token

    return re.sub(r"</?[A-Za-z][A-Za-z0-9_-]*(?:\s[^>\n]*)?>", replace_literal, text)


def reference_tokens(text: str) -> list[str]:
    return REFERENCE_TOKEN_RE.findall(text)


def protected_tokens(text: str) -> list[str]:
    return PROTECTED_TOKEN_RE.findall(text)


def reference_markdown(record: dict[str, Any], bundle: ReferenceBundle) -> str:
    keys = record.get("keys", [])
    command = str(record.get("command", "ref"))
    if record.get("kind") == "cite":
        numbers = [str(bundle.citations.get(key, {}).get("number", "?")) for key in keys]
        label = f"[{', '.join(numbers)}]"
        target = quote(keys[0], safe="") if keys else ""
        return f"[{label}](#cite:{target})" if target else label

    key = keys[0] if keys else ""
    label_data = bundle.labels.get(key, {})
    number = str(label_data.get("number") or key or "?")
    kind = label_data.get("kind", "reference")
    prefix = {
        "equation": "式",
        "figure": "图",
        "table": "表",
        "algorithm": "算法",
        "section": "第",
    }.get(kind, "引用")
    if command.lower() == "ref":
        label = number
    elif kind == "section":
        label = f"第 {number} 节"
    else:
        label = f"{prefix} ({number})" if kind == "equation" else f"{prefix} {number}"
    return f"[{label}](#xref:{quote(key, safe='')})" if key else label


def restore_reference_tokens(text: str, bundle: ReferenceBundle) -> str:
    return REFERENCE_TOKEN_RE.sub(
        lambda match: reference_markdown(bundle.references.get(match.group(0), {}), bundle),
        text,
    )


def restore_literal_tokens(text: str, bundle: ReferenceBundle) -> str:
    return LITERAL_TOKEN_RE.sub(lambda match: bundle.literals.get(match.group(0), match.group(0)), text)


def latex_command_argument(text: str, command: str) -> str:
    match = re.search(rf"\\{re.escape(command)}(?:\[[^\]]*\])?\s*\{{", text, re.IGNORECASE)
    if not match:
        return ""
    start = match.end()
    depth = 1
    index = start
    while index < len(text) and depth:
        if text[index] == "{" and (index == 0 or text[index - 1] != "\\"):
            depth += 1
        elif text[index] == "}" and (index == 0 or text[index - 1] != "\\"):
            depth -= 1
        index += 1
    return text[start:index - 1].strip() if depth == 0 else ""


def normalize_asset_text(text: str) -> str:
    text = re.sub(r"\\textcolor\s*\{[^{}]+\}\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:Require|Ensure|State|While|If|ElsIf|Else|EndIf|EndWhile)\b", " ", text)
    text = re.sub(r"\\(?:toprule|midrule|bottomrule|hline|centering)\b", " ", text)
    text = normalize_latex_inline(text)
    text = re.sub(r"\\(?=\s)", " ", text)
    return re.sub(r"\s+", " ", text).strip(" \\&")


def parse_algorithm_steps(body: str) -> list[dict[str, Any]]:
    algorithmic_match = re.search(
        r"\\begin\{algorithmic\}(?:\[[^\]]*\])?([\s\S]*?)\\end\{algorithmic\}",
        body,
        re.IGNORECASE,
    )
    content = algorithmic_match.group(1) if algorithmic_match else body
    steps: list[dict[str, Any]] = []
    indent = 0
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("%"):
            continue
        command_match = re.match(r"\\(?P<command>Require|Ensure|State|While|If|ElsIf|Else|EndIf|EndWhile)\b(?P<rest>.*)", line)
        command = command_match.group("command") if command_match else "State"
        rest = command_match.group("rest").strip() if command_match else line
        if command in {"EndIf", "EndWhile", "ElsIf", "Else"}:
            indent = max(0, indent - 1)
        if rest.startswith("{") and rest.endswith("}"):
            rest = rest[1:-1]
        text = normalize_asset_text(rest)
        if command in {"EndIf", "EndWhile"}:
            text = "End if" if command == "EndIf" else "End while"
        elif command == "Else":
            text = "Else"
        if text:
            steps.append({"keyword": command.lower(), "indent": indent, "source": text, "translation": ""})
        if command in {"While", "If", "ElsIf", "Else"}:
            indent += 1
    return steps


def parse_table_rows(body: str) -> list[list[dict[str, str]]]:
    tabular_match = re.search(
        r"\\begin\{(?:tabular\*?|tabularx)\}(?:\[[^\]]*\])?\s*\{(?:[^{}]|\{[^{}]*\})*\}([\s\S]*?)"
        r"\\end\{(?:tabular\*?|tabularx)\}",
        body,
        re.IGNORECASE,
    )
    content = tabular_match.group(1) if tabular_match else body
    content = re.sub(r"\\(?:toprule|midrule|bottomrule|hline)\b", "", content)
    raw_rows = re.split(r"(?<!\\)\\\\(?:\[[^\]]*\])?", content)
    rows: list[list[dict[str, str]]] = []
    for raw_row in raw_rows:
        cells = [normalize_asset_text(cell) for cell in re.split(r"(?<!\\)&", raw_row)]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append([{"source": cell, "translation": ""} for cell in cells])
    return rows


def resolve_graphic_path(root: Path, name: str) -> Path | None:
    candidate = (root / name.strip()).resolve()
    candidates = [candidate]
    if not candidate.suffix:
        candidates.extend(candidate.with_suffix(suffix) for suffix in (".pdf", ".png", ".jpg", ".jpeg", ".webp"))
    for path in candidates:
        try:
            if path.is_file() and root.resolve() in path.parents:
                return path
        except OSError:
            continue
    return None


def extract_structured_assets(text: str, root: Path, bundle: ReferenceBundle) -> None:
    asset_re = re.compile(
        r"\\begin\{(?P<kind>figure|table|algorithm)(?:\*)?\}(?:\[[^\]]*\])?(?P<body>[\s\S]*?)"
        r"\\end\{(?P=kind)(?:\*)?\}",
        re.IGNORECASE,
    )
    main_end_match = re.search(r"\\(?:bibliography|appendix)\b|\\section\*?\s*\{\s*(?:Acknowledg|References)", text, re.IGNORECASE)
    main_end = main_end_match.start() if main_end_match else len(text)
    for index, match in enumerate(asset_re.finditer(text), start=1):
        kind = match.group("kind").lower()
        body = match.group("body")
        label_match = re.search(r"\\label\s*\{([^{}]+)\}", body)
        label = label_match.group(1).strip() if label_match else f"{kind}:generated-{index}"
        label_data = bundle.labels.get(label, {})
        asset: dict[str, Any] = {
            "id": f"asset-{slugify(label)}",
            "label": label,
            "kind": kind,
            "number": label_data.get("number") or str(index),
            "captionSource": normalize_asset_text(latex_command_argument(body, "caption")),
            "captionTranslation": "",
            "referenced": False,
            "inMainBody": match.start() < main_end,
        }
        preceding_sections = list(LATEX_SECTION_RE.finditer(text, 0, match.start()))
        if preceding_sections:
            asset["sectionTitle"] = normalize_asset_text(preceding_sections[-1].group("title"))
        if kind == "table":
            asset["rows"] = parse_table_rows(body)
        elif kind == "algorithm":
            asset["steps"] = parse_algorithm_steps(body)
        else:
            images: list[dict[str, Any]] = []
            for graphic in re.finditer(r"\\includegraphics(?:\[[^\]]*\])?\s*\{([^{}]+)\}", body):
                image_path = resolve_graphic_path(root, graphic.group(1))
                if image_path:
                    images.append({"_bytes": image_path.read_bytes(), "_suffix": image_path.suffix.lower()})
            asset["images"] = images
        bundle.assets.append(asset)
        label_data = bundle.labels.setdefault(
            label,
            {"kind": kind, "number": asset["number"], "title": asset["captionSource"], "page": None},
        )
        label_data["targetAssetId"] = asset["id"]
        label_data["title"] = asset["captionSource"]


def formula_tokens(text: str) -> list[str]:
    return MATH_TOKEN_RE.findall(text)


def restore_math_tokens(text: str, formulas: dict[str, str]) -> str:
    return MATH_TOKEN_RE.sub(lambda match: formulas.get(match.group(0), match.group(0)), text)


def protect_latex_math(
    text: str,
    formulas: dict[str, str] | None = None,
    bundle: ReferenceBundle | None = None,
) -> tuple[str, dict[str, str]]:
    formulas = formulas if formulas is not None else {}

    def register(latex: str, *, display: bool) -> str:
        token = f"@@MATH_{len(formulas) + 1:04d}@@"
        normalized = latex.strip()
        formulas[token] = f"$$\n{normalized}\n$$" if display else f"${normalized}$"
        return token

    for env in MATH_ENVIRONMENTS:
        escaped_env = re.escape(env)
        pattern = re.compile(
            rf"\\begin\{{{escaped_env}\}}([\s\S]*?)\\end\{{{escaped_env}\}}",
            re.IGNORECASE,
        )

        def replace_environment(match: re.Match[str], environment: str = env) -> str:
            body = match.group(1)
            markers: list[str] = []
            if bundle is not None:
                for label_match in re.finditer(r"\\label\s*\{([^{}]+)\}", body):
                    key = label_match.group(1).strip()
                    token = f"@@LABEL_{len(bundle.label_tokens) + 1:04d}@@"
                    bundle.label_tokens[token] = key
                    markers.append(token)
            body = re.sub(r"\\label\s*\{[^{}]*\}", "", body).strip()
            formula = register(f"\\begin{{{environment}}}\n{body}\n\\end{{{environment}}}", display=True)
            return " ".join([formula, *markers])

        text = pattern.sub(replace_environment, text)

    text = re.sub(
        r"\\\[([\s\S]*?)\\\]",
        lambda match: register(match.group(1), display=True),
        text,
    )
    text = re.sub(
        r"\\\(([\s\S]*?)\\\)",
        lambda match: register(match.group(1), display=False),
        text,
    )
    text = re.sub(
        r"(?<!\\)\$\$([\s\S]*?)(?<!\\)\$\$",
        lambda match: register(match.group(1), display=True),
        text,
    )
    text = re.sub(
        r"(?<!\\)\$(?!\$)([\s\S]*?)(?<!\\)\$(?!\$)",
        lambda match: register(match.group(1), display=False),
        text,
    )
    return text, formulas


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


def latex_to_protected_document(
    text: str,
    bibliography: dict[str, dict[str, str]] | None = None,
    source_root: Path | None = None,
) -> tuple[str, dict[str, str], ReferenceBundle]:
    text = strip_latex_comments(text)
    bundle = ReferenceBundle(labels=classify_latex_labels(text), citations=bibliography or {})
    simple_macros = {
        match.group("name"): match.group("value")
        for match in re.finditer(
            r"\\(?:newcommand|renewcommand)\s*\{\\(?P<name>[A-Za-z]+)\}"
            r"(?:\s*\[0\])?\s*\{(?P<value>[^{}]*(?:\{[^{}]*\}[^{}]*)*)\}",
            text,
        )
    }
    for name, value in simple_macros.items():
        text = re.sub(rf"\\{re.escape(name)}\b", lambda _match, replacement=value: replacement, text)
    one_argument_macros: dict[str, str] = {}
    macro_start_re = re.compile(r"\\(?:newcommand|renewcommand)\s*\{\\(?P<name>[A-Za-z]+)\}\s*\[1\]\s*\{")
    for macro_match in macro_start_re.finditer(text):
        start = macro_match.end()
        depth = 1
        index = start
        while index < len(text) and depth:
            if text[index] == "{" and text[index - 1] != "\\":
                depth += 1
            elif text[index] == "}" and text[index - 1] != "\\":
                depth -= 1
            index += 1
        if depth == 0:
            one_argument_macros[macro_match.group("name")] = text[start:index - 1]
    for name, template in one_argument_macros.items():
        call_re = re.compile(rf"\\{re.escape(name)}\s*\{{([^{{}}]*)\}}")
        text = call_re.sub(lambda match, value=template: value.replace("#1", match.group(1)), text)
    document_match = re.search(r"\\begin\{document\}([\s\S]*)", text, re.IGNORECASE)
    if document_match:
        text = document_match.group(1)
    text = re.sub(r"\\end\{document\}[\s\S]*$", "", text, flags=re.IGNORECASE)
    text = protect_literal_tokens(text, bundle)
    text = protect_latex_references(text, bundle)
    text, formulas = protect_latex_math(text, bundle=bundle)
    if source_root is not None:
        extract_structured_assets(text, source_root, bundle)

    def replace_label(match: re.Match[str]) -> str:
        token = f"@@LABEL_{len(bundle.label_tokens) + 1:04d}@@"
        bundle.label_tokens[token] = match.group(1).strip()
        return token

    text = re.sub(r"\\label\s*\{([^{}]+)\}", replace_label, text)

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
    return "\n".join(line for line in lines if line), formulas, bundle


def latex_to_protected_text(text: str) -> tuple[str, dict[str, str]]:
    protected, formulas, _bundle = latex_to_protected_document(text)
    return protected, formulas


def latex_to_plain_text(text: str) -> str:
    protected, formulas = latex_to_protected_text(text)
    return restore_math_tokens(protected, formulas)


def extract_latex_document_with_references(
    path: Path,
) -> tuple[list[tuple[int, str]], dict[str, str], ReferenceBundle]:
    root, temp = latex_root_for(path)
    try:
        main_tex = find_main_tex(root, path)
        expanded = inline_latex_inputs(main_tex, root)
        bibliography = parse_bibtex_entries(root)
        protected, formulas, bundle = latex_to_protected_document(expanded, bibliography, root)
    finally:
        if temp:
            temp.cleanup()
    if not protected.strip():
        raise RuntimeError("No readable text was extracted from LaTeX source.")
    return [(1, protected)], formulas, bundle


def extract_latex_document(path: Path) -> tuple[list[tuple[int, str]], dict[str, str]]:
    pages, formulas, _bundle = extract_latex_document_with_references(path)
    return pages, formulas


def extract_latex_pages(path: Path) -> list[tuple[int, str]]:
    pages, formulas = extract_latex_document(path)
    return [(page, restore_math_tokens(text, formulas)) for page, text in pages]


def normalize_heading_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]")
CJK_SENTENCE_END_RE = re.compile(r"[。！？；;]\s*$")


def cjk_char_count(text: str) -> int:
    return len(CJK_RE.findall(text))


def text_weight(text: str) -> int:
    """How much text a block carries; CJK text has no spaces so characters are counted."""
    spaced_words = len(text.split())
    return max(spaced_words, cjk_char_count(text) + len(re.findall(r"[A-Za-z]{2,}", text)))


def normalize_cjk_spacing(text: str) -> str:
    """PDF text extraction scatters spaces around CJK glyphs, which hurts translation."""
    text = re.sub(r"(?<=[\u3400-\u4dbf\u4e00-\u9fff])\s+(?=[\u3400-\u4dbf\u4e00-\u9fff])", "", text)
    text = re.sub(r"\s+(?=[，。；：、？！）“”‘’（）《》])", "", text)
    text = re.sub(r"(?<=[（“‘《])\s+", "", text)
    return text


def noise_match_key(text: str) -> str:
    """Comparison key for page furniture; digits are masked so page numbers do not matter."""
    return re.sub(r"\d+", "#", normalize_heading_text(text))


def is_section_heading(line: str) -> bool:
    """True when a standalone line looks like a section heading in any layout style."""
    text = re.sub(r"\s+", " ", line).strip()
    if not text or len(text) > 120:
        return False
    if not heading_text_allowed(text):
        return False
    if HEADING_RE.match(text):
        return True
    return bool(SECTION_NAME_HEADING_RE.match(text))


def abstract_line_remainder(line: str) -> str | None:
    """Return the inline abstract body when the line is an abstract heading, else None."""
    text = line.strip()
    if not text:
        return None
    match = ABSTRACT_HEADING_RE.match(text)
    if match:
        return match.group(1).strip()
    compact = re.sub(r"\s+", "", text).casefold()
    for label in ABSTRACT_LABELS:
        compact_label = re.sub(r"\s+", "", label).casefold()
        if compact.startswith(compact_label):
            return re.sub(r"\s+", "", text[len(label) :]).lstrip(":：—–-. ")
    return None


def keywords_line_remainder(line: str) -> str | None:
    text = line.strip()
    if not text or len(text) > 200:
        return None
    match = KEYWORDS_LINE_RE.match(text)
    if not match:
        return None
    return match.group(1).strip()


def split_page_paragraphs(
    text: str,
    heading_hints: set[str] | None = None,
) -> list[str]:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"-\n(?=[a-z])", "", cleaned)
    paragraphs: list[str] = []
    hints = heading_hints or set()

    def is_heading_line(line: str) -> bool:
        if len(line) > 120:
            return False
        if is_section_heading(line):
            return True
        return normalize_heading_text(line) in hints

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
            if is_heading_line(line):
                if len(buffer) == 1 and BARE_SECTION_NUMBER_RE.match(buffer[0]):
                    # ACL/IEEE style prints the section number in the margin, so it lands in its
                    # own line right before the title.
                    paragraphs.append(f"{buffer[0]} {line}")
                    buffer = []
                    continue
                append_text(buffer)
                paragraphs.append(line)
                buffer = []
                continue
            buffer.append(line)
        append_text(buffer)

    return [
        paragraph
        for paragraph in paragraphs
        # CJK sentences are short in characters, so weigh them instead of counting letters.
        if len(paragraph) >= 12 or text_weight(paragraph) >= 6 or is_heading_line(paragraph)
    ]


def merge_numbered_heading_lines(lines: list[str]) -> list[str]:
    """Join a margin section number with the title printed next to it ("1" + "Introduction")."""
    merged: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if BARE_SECTION_NUMBER_RE.match(line) and not is_section_heading(line):
            follower = lines[index + 1] if index + 1 < len(lines) else ""
            if follower and is_section_heading(follower):
                merged.append(f"{line} {follower}")
                index += 2
                continue
        merged.append(line)
        index += 1
    return merged


def embedded_heading(line: str) -> str:
    match = EMBEDDED_HEADING_RE.search(line)
    if not match:
        return ""
    heading = match.group(1).strip()
    return heading if is_section_heading(heading) else ""


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


def repeated_furniture_lines(pages: list[tuple[int, str]]) -> set[str]:
    """Short lines repeated across pages that are page furniture rather than content."""
    occurrences: dict[str, set[int]] = {}
    samples: dict[str, str] = {}
    for page, text in pages:
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            candidate = re.sub(r"\s+", " ", line).strip()
            if not candidate or len(candidate) > 120:
                continue
            key = noise_match_key(candidate)
            occurrences.setdefault(key, set()).add(page)
            samples.setdefault(key, candidate)

    if len(pages) <= 1:
        return set()
    threshold = 2 if len(pages) <= 4 else max(2, int(len(pages) * 0.35))
    return {
        key
        for key, seen_pages in occurrences.items()
        if len(seen_pages) >= threshold
        and (
            TEXT_FURNITURE_RE.search(samples[key])
            or PUBLICATION_HEADER_RE.search(samples[key])
            or PAGE_HEADER_RE.search(samples[key])
        )
    }


def clean_extracted_pages(
    pages: list[tuple[int, str]],
    *,
    noise_hints: set[str] | None = None,
    heading_hints: dict[int, set[str]] | None = None,
    recover_front_matter: bool = False,
) -> list[tuple[int, str]]:
    cleaned_pages: list[tuple[int, str]] = []
    seen_abstract = False
    skip_front_matter = not recover_front_matter
    noise = {noise_match_key(line) for line in (noise_hints or set())}
    noise |= repeated_furniture_lines(pages)
    hints = heading_hints or {}
    started_body = not recover_front_matter
    first_page = pages[0][0] if pages else 0

    for page, text in pages:
        lines = [
            normalize_cjk_spacing(line.strip())
            for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        ]
        lines = merge_numbered_heading_lines(lines)
        output: list[str] = []
        in_author_footnote = False
        caption_lines_remaining = 0
        page_hints = hints.get(page, set())

        for line in lines:
            if not line:
                caption_lines_remaining = 0
                output.append("")
                continue

            if noise_match_key(line) in noise:
                continue

            abstract_text = abstract_line_remainder(line)
            if abstract_text is not None:
                seen_abstract = True
                skip_front_matter = False
                started_body = True
                in_author_footnote = False
                output.append("Abstract")
                if abstract_text:
                    output.append(abstract_text)
                continue

            if not skip_front_matter:
                keywords_text = keywords_line_remainder(line)
                if keywords_text is not None:
                    if keywords_text:
                        output.append(keywords_text)
                    continue

            if skip_front_matter and not seen_abstract:
                continue

            if (
                ARTICLE_META_LINE_RE.match(line)
                or ARTICLE_META_DATE_RE.match(line)
                or re.sub(r"[\s.]+", "", line).casefold() in ARTICLE_META_COMPACT_LABELS
            ):
                continue

            if not started_body:
                # Journal PDFs without an "Abstract" heading: keep the body from the first real
                # paragraph instead of dropping the whole document as front matter.
                if page != first_page:
                    started_body = True
                elif len(line.split()) >= 20 and len(line) >= 120 and not is_section_heading(line):
                    started_body = True
                elif normalize_heading_text(line) in page_hints or is_section_heading(line):
                    started_body = True
                else:
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
                if is_section_heading(line):
                    caption_lines_remaining = 0
                else:
                    caption_lines_remaining -= 1
                    continue

            if in_author_footnote:
                if is_section_heading(line) and not line.lower().startswith(("references", "acknowledg", "appendix")):
                    in_author_footnote = False
                else:
                    continue

            if PAGE_ARTIFACT_RE.match(line) or PAGE_HEADER_RE.match(line):
                continue
            if is_visual_artifact_line(line):
                continue

            output.append(line)

        cleaned_pages.append((page, "\n".join(output)))

    if not recover_front_matter and pages and not any(text.strip() for _page, text in cleaned_pages):
        return clean_extracted_pages(
            pages,
            noise_hints=noise_hints,
            heading_hints=heading_hints,
            recover_front_matter=True,
        )
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


def paragraph_continues_across_pages(previous: Paragraph, current: Paragraph) -> bool:
    """True when a paragraph seems to run from the previous page into the current one."""
    if current.page != previous.page + 1:
        return False
    previous_text = re.sub(r"\s+", " ", previous.source).strip()
    current_text = re.sub(r"\s+", " ", current.source).strip()
    if len(previous_text) < 200 or not current_text:
        return False
    if re.search(r"[.!?:;”’\"')\]]\s*$", previous_text):
        return False
    if not (current_text[0].islower() or current_text[0].isdigit() or current_text[0] in "(["):
        return False
    if re.match(r"^(?:figure|table|scheme|chart|eq\.?)\s*\d", current_text, re.IGNORECASE):
        return False
    return True


def is_heading_paragraph(
    paragraph: str,
    hints: set[str],
    compact_hints: set[str],
    blocked_hints: set[str],
) -> bool:
    """Decide whether a paragraph starts a new section.

    Typography hints win outright; text patterns are trusted unless the page markup shows the
    line belongs to a figure, a table, or the running head.
    """
    key = normalize_heading_text(paragraph)
    if key in blocked_hints:
        return False
    if key in hints or re.sub(r"\s+", "", key) in compact_hints:
        return True
    return is_section_heading(paragraph)


def segment_document(
    pages: list[tuple[int, str]],
    *,
    heading_hints: dict[int, set[str]] | None = None,
    noise_hints: set[str] | None = None,
    blocked_hints: set[str] | None = None,
) -> list[Section]:
    hints = heading_hints or {}
    hints_available = bool(hints)
    compact_by_page = {
        page: {re.sub(r"\s+", "", key) for key in keys} for page, keys in hints.items()
    }
    blocked = set(blocked_hints or set())
    pages = clean_extracted_pages(pages, noise_hints=noise_hints, heading_hints=hints)
    noise_keys = {noise_match_key(line) for line in (noise_hints or set())}
    sections: list[Section] = []
    section_ids: set[str] = set()
    current = new_section("Front Matter", pages[0][0] if pages else 1, section_ids)
    sections.append(current)

    paragraph_counts: dict[str, int] = {}
    previous: Paragraph | None = None

    for page, text in pages:
        page_hints = hints.get(page, set())
        page_compact_hints = compact_by_page.get(page, set())
        for paragraph in split_page_paragraphs(text, heading_hints=page_hints):
            if len(paragraph) <= 160 and noise_match_key(paragraph) in noise_keys:
                continue
            if is_visual_artifact(paragraph):
                continue
            key = normalize_heading_text(paragraph)
            confirmed = key in page_hints or re.sub(r"\s+", "", key) in page_compact_hints
            stop_candidate = bool(STOP_SECTION_RE.match(paragraph))
            if stop_candidate and hints_available and not confirmed:
                # A table column called "References" is not the reference section: without
                # typographic confirmation the line stays plain body text.
                paragraph_is_heading = False
            else:
                paragraph_is_heading = is_heading_paragraph(
                    paragraph, page_hints, page_compact_hints, blocked
                )
            if paragraph_is_heading and hints_available and not confirmed:
                # Typography exists but did not confirm this line: keep only short headings, so
                # footnotes and list items ("1 While DQN works well on game environments…") stay
                # in the body text.
                stripped = paragraph.strip()
                if SECTION_NAME_HEADING_RE.match(stripped):
                    paragraph_is_heading = len(stripped.split()) <= 6
                elif HEADING_RE.match(stripped):
                    paragraph_is_heading = len(stripped.split()) <= 8
            if paragraph_is_heading:
                if stop_candidate:
                    return finalize_sections(sections)
                current = new_section(paragraph, page, section_ids)
                sections.append(current)
                previous = None
                continue

            current.page_end = page
            count = paragraph_counts.get(current.id, 0) + 1
            paragraph_counts[current.id] = count
            paragraph_id = f"{current.id}-p{count}"
            item = Paragraph(
                id=paragraph_id,
                page=page,
                anchor=f"{current.title}, paragraph {count}",
                source=paragraph,
                page_end=page,
            )
            if previous is not None and paragraph_continues_across_pages(previous, item):
                previous.source = re.sub(r"\s+", " ", f"{previous.source} {item.source}").strip()
                previous.page_end = page
                continue
            current.paragraphs.append(item)
            previous = item

    return finalize_sections(sections)


def finalize_sections(sections: list[Section]) -> list[Section]:
    kept = [section for section in sections if section.paragraphs]
    if kept and kept[0].title == "Front Matter":
        # No abstract heading in this PDF (Nature-style layout): label the opening block readably.
        kept[0].title = "Main text"
    return kept


def associate_label_targets(sections: list[Section], bundle: ReferenceBundle) -> None:
    for section in sections:
        retained: list[Paragraph] = []
        for paragraph in section.paragraphs:
            for token in LABEL_TOKEN_RE.findall(paragraph.source):
                key = bundle.label_tokens.get(token, "")
                if not key:
                    continue
                label = bundle.labels.setdefault(
                    key,
                    {"kind": "reference", "number": key, "title": "", "page": paragraph.page},
                )
                label["targetSectionId"] = section.id
                label["page"] = paragraph.page
                if label.get("kind") != "section":
                    label["targetParagraphId"] = paragraph.id
            paragraph.source = re.sub(r"\s*@@LABEL_[0-9]{4,}@@\s*", " ", paragraph.source).strip()
            if paragraph.source:
                retained.append(paragraph)
        section.paragraphs = retained

    assets_by_label = {asset["label"]: asset for asset in bundle.assets}
    for section in sections:
        for paragraph in section.paragraphs:
            for token in reference_tokens(paragraph.source):
                record = bundle.references.get(token, {})
                for key in record.get("keys", []):
                    asset = assets_by_label.get(key)
                    if asset and not asset.get("referenced"):
                        asset["referenced"] = True
                        asset["firstReferencedBy"] = paragraph.id
    for asset in bundle.assets:
        if asset.get("referenced") or asset.get("kind") != "figure" or not asset.get("inMainBody"):
            continue
        section_title = str(asset.get("sectionTitle") or "").lower()
        matching_section = next(
            (
                section
                for section in sections
                if section.paragraphs
                and (section.title.lower().endswith(section_title) or section_title in section.title.lower())
            ),
            None,
        )
        if matching_section:
            asset["referenced"] = True
            asset["firstReferencedBy"] = matching_section.paragraphs[0].id


def restore_all_tokens(text: str, formulas: dict[str, str], bundle: ReferenceBundle) -> str:
    return restore_literal_tokens(
        restore_math_tokens(restore_reference_tokens(text, bundle), formulas),
        bundle,
    )


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


def extraction_quality_summary(pages: list[tuple[int, str]]) -> dict[str, Any]:
    try:
        sections = segment_document(pages)
    except Exception:
        sections = []
    paragraphs = [paragraph for section in sections for paragraph in section.paragraphs]
    return {
        "score": score_extraction_quality(pages),
        "sections": len(sections),
        "paragraphs": len(paragraphs),
        "characters": sum(len(paragraph.source) for paragraph in paragraphs),
        "has_abstract": any(section.title.lower().startswith("abstract") for section in sections[:3]),
        "has_headings": len(sections) >= 2,
    }


def extraction_is_usable(summary: dict[str, Any]) -> bool:
    return bool(
        # Journals such as Nature print no abstract heading at all; a well split body is enough.
        (summary["has_abstract"] or summary.get("has_headings"))
        and summary["paragraphs"] >= 8
        and summary["characters"] >= 1200
        and summary["score"] < 300
    )


def log_extraction_quality(source: str, summary: dict[str, Any]) -> None:
    print(
        f"Extraction quality [{source}]: score={summary['score']:.1f}, "
        f"sections={summary['sections']}, paragraphs={summary['paragraphs']}, "
        f"characters={summary['characters']}",
        file=sys.stderr,
        flush=True,
    )


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate suitable for batching."""

    words_and_symbols = re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]", text)
    return max(1, len(words_and_symbols))


def chunk_units(
    units: list[Paragraph],
    max_chars: int,
    *,
    max_items: int | None = None,
    target_items: int | None = None,
) -> list[list[Paragraph]]:
    chunks: list[list[Paragraph]] = []
    current: list[Paragraph] = []
    current_chars = 0
    current_tokens = 0
    current_formulas = 0
    max_tokens = max(600, int(max_chars * 0.58))
    max_formulas = max(4, min(12, max_chars // 450))

    for paragraph in units:
        size = len(paragraph.source)
        tokens = estimate_tokens(paragraph.source)
        formulas = len(formula_tokens(paragraph.source))
        item_limit = max_items is not None and len(current) >= max_items
        target_reached = target_items is not None and len(current) >= target_items
        resource_limit = (
            current_chars + size > max_chars
            or current_tokens + tokens > max_tokens
            or current_formulas + formulas > max_formulas
        )
        can_split_for_resources = max_items != 6 or len(current) >= 4
        if current and (item_limit or (resource_limit and can_split_for_resources) or (target_reached and formulas > 0)):
            chunks.append(current)
            current = []
            current_chars = 0
            current_tokens = 0
            current_formulas = 0
        current.append(paragraph)
        current_chars += size
        current_tokens += tokens
        current_formulas += formulas

    if current:
        chunks.append(current)

    # Structured batches should normally stay in the requested 4-6 item window.
    if max_items == 6 and len(chunks) > 1 and len(chunks[-1]) < 4:
        while len(chunks[-1]) < 4 and len(chunks[-2]) > 4:
            chunks[-1].insert(0, chunks[-2].pop())
    return chunks


def chunk_paragraphs(sections: list[Section], max_chars: int) -> list[list[Paragraph]]:
    return chunk_units(
        [paragraph for section in sections for paragraph in section.paragraphs],
        max_chars,
    )


def batch_fingerprint(kind: str, chunk: list[Paragraph]) -> str:
    digest = hashlib.sha256()
    digest.update(kind.encode("utf-8"))
    for paragraph in chunk:
        digest.update(paragraph.id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(paragraph.source.encode("utf-8"))
        digest.update(b"\0")
    return f"{kind}:{digest.hexdigest()[:20]}"


def validate_translation_response(data: Any, chunk: list[Paragraph]) -> dict[str, dict[str, str]]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Response JSON must contain an items array.")

    expected_ids = [paragraph.id for paragraph in chunk]
    expected_set = set(expected_ids)
    validated: dict[str, dict[str, str]] = {}
    for raw_item in data["items"]:
        if not isinstance(raw_item, dict):
            raise ValueError("Each translation item must be an object.")
        item_id = str(raw_item.get("id", ""))
        if item_id not in expected_set:
            raise ValueError(f"Unexpected paragraph id in response: {item_id!r}")
        if item_id in validated:
            raise ValueError(f"Duplicate paragraph id in response: {item_id}")
        status = str(raw_item.get("status", ""))
        if status not in TRANSLATION_STATUSES:
            raise ValueError(f"Invalid status for {item_id}: {status!r}")
        translation = str(raw_item.get("translation", "") or "").strip()
        note = str(raw_item.get("note", "") or "").strip()
        if status == "translated" and not translation:
            raise ValueError(f"Translated item {item_id} has no translation.")
        paragraph = next(paragraph for paragraph in chunk if paragraph.id == item_id)
        if status == "translated":
            expected_formulas = Counter(formula_tokens(paragraph.source))
            translated_formulas = Counter(formula_tokens(translation))
            if expected_formulas != translated_formulas:
                raise FormulaTokenError(
                    f"Formula tokens changed for {item_id}: expected {expected_formulas}, "
                    f"received {translated_formulas}.",
                    item_id,
                )
            expected_non_math = Counter(
                token for token in protected_tokens(paragraph.source) if not token.startswith("@@MATH_")
            )
            translated_non_math = Counter(
                token for token in protected_tokens(translation) if not token.startswith("@@MATH_")
            )
            if expected_non_math != translated_non_math:
                raise ReferenceTokenError(
                    f"Reference tokens changed for {item_id}: expected {expected_non_math}, "
                    f"received {translated_non_math}.",
                    item_id,
                )
        if status != "translated":
            translation = ""
        if status == "skipped" and not note:
            note = "Skipped because this item is not main-body prose."
        if status == "needs_ocr" and not note:
            note = "The source is not readable enough to translate safely."
        if status == "needs_formula_recovery" and not note:
            note = "Mathematical notation could not be recovered reliably."
        validated[item_id] = {
            "id": item_id,
            "status": status,
            "translation": translation,
            "note": note,
        }

    missing = [item_id for item_id in expected_ids if item_id not in validated]
    if missing:
        raise ValueError(f"Response omitted paragraph ids: {', '.join(missing)}")
    return validated


def translate_chunk(
    client: OpenAI,
    model: str,
    chunk: list[Paragraph],
    retries: int,
    ocr_context_by_page: dict[int, str] | None = None,
    structured: bool = False,
    run: TranslationRun | None = None,
) -> dict[str, dict[str, str]]:
    payload = {
        "paragraphs": [
            {
                "id": paragraph.id,
                "page": paragraph.page,
                "anchor": paragraph.anchor,
                "source": paragraph.source,
            }
            for paragraph in chunk
        ],
        **(
            {
                "ocr_page_context": {
                    str(page): text[:12000]
                    for page, text in ocr_context_by_page.items()
                    if any(paragraph.page == page for paragraph in chunk)
                }
            }
            if ocr_context_by_page
            else {}
        ),
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
        "- Exclude page headers, footers, page numbers, figure/table captions, footnotes, and other non-paragraph artifacts. Preserve equations that belong to the main argument.\n"
        "- Treat residual OCR or PDF extraction artifacts such as figure labels, chart axes, legend text, table cells, dense numeric series, or diagram node labels as skipped.\n"
        "- If a paragraph does not belong to the main body, return it with status skipped.\n"
        "\n"
        "Translation requirements:\n"
        "- Translate into formal, precise, and academically appropriate Chinese.\n"
        "- Preserve the logical structure, terminology, and argumentative nuance of the original text.\n"
        "- Translate literally enough for close reading; do not paraphrase loosely or summarize.\n"
        "- Preserve formulas, citations, section numbers, variable names, dataset names, method names, model names, and technical abbreviations.\n"
        "- For important technical terms, keep the English term in parentheses when useful, especially on first occurrence.\n"
        "- Keep mathematical notation, inline equations, citation markers, and references to figures/tables readable and faithful to the original.\n"
        "- Formula, reference, and literal placeholders such as @@MATH_0001@@, @@XREF_0001@@, @@CITE_0001@@, and @@LITERAL_0001@@ are immutable. Copy every placeholder exactly once. Never translate, rename, remove, or duplicate one. Reorder placeholders only when Chinese grammar requires it, while preserving which statement each placeholder belongs to.\n"
        "- Do not invent explanations, background knowledge, or missing content.\n"
        "\n"
        "Output requirements:\n"
        "- Return valid JSON only. Do not include markdown, comments, or extra text outside JSON.\n"
        "- JSON shape: {\"items\":[{\"id\":\"paragraph-id\",\"status\":\"translated\",\"translation\":\"中文翻译\",\"note\":\"\"}]}.\n"
        "- Return every input paragraph id exactly once. Never omit an id.\n"
        "- status must be translated, skipped, needs_ocr, or needs_formula_recovery.\n"
        "- Use translated for readable main-body content and provide a non-empty Chinese translation.\n"
        "- Use skipped only for definite non-body material or visual artifacts, with an empty translation and a short reason.\n"
        "- Use needs_ocr when the item may contain useful content but is too corrupted or incomplete to translate safely. Do not guess missing content.\n"
        "- Use needs_formula_recovery when prose is readable but mathematical notation appears missing or corrupted and no protected formula placeholder is available. Do not invent a formula.\n"
        "- If ocr_page_context is provided, use only the matching page context to repair the difficult item; do not add unrelated page content.\n"
        "- Keep the original paragraph order.\n"
        "- Use the note field only for brief translation notes, such as ambiguous terminology or unresolved OCR/PDF extraction issues. Otherwise set note to an empty string.\n"
        "- Even if no valid main-body paragraphs are found, return every id with status skipped."
    )
    if structured:
        system_prompt = (
            "You translate structured academic paper content into precise Chinese. The inputs are figure captions, "
            "table cells, training templates, or pseudocode steps that the main text explicitly references. "
            "Translate every readable item without summarizing. Preserve variable names, control-flow meaning, "
            "technical terms, XML-like tokens, and ordering. Formula and reference placeholders such as "
            "@@MATH_0001@@, @@XREF_0001@@, @@CITE_0001@@, and @@LITERAL_0001@@ are immutable and must each be copied exactly once. "
            "Return JSON only with shape {\"items\":[{\"id\":\"item-id\",\"status\":\"translated\","
            "\"translation\":\"中文\",\"note\":\"\"}]}. Return every id exactly once. status must be translated, "
            "skipped, needs_ocr, or needs_formula_recovery; use skipped only for an actually empty/decorative item. "
            "Never invent missing content."
        )
    user_prompt = (
        "Translate this json payload. Return json with the same paragraph ids:\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    last_error: Exception | None = None
    empty_response_failures = 0
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
                extra_body={"thinking": {"type": "disabled"}},
            )
            if run:
                run.record_response(response)
            content = response.choices[0].message.content or ""
            if not content.strip():
                raise EmptyTranslationResponseError("DeepSeek returned empty JSON content.")
            data = json.loads(content)
            return validate_translation_response(data, chunk)
        except Exception as error:  # noqa: BLE001 - retries should catch API/JSON failures.
            last_error = error
            if isinstance(error, ProtectedTokenError):
                raise
            if is_non_retryable_api_error(error):
                status_code = getattr(error, "status_code", "unknown")
                raise NonRetryableTranslationError(
                    f"DeepSeek request cannot be retried (HTTP {status_code}): {error}"
                ) from error
            if isinstance(error, EmptyTranslationResponseError):
                empty_response_failures += 1
                if empty_response_failures >= 2:
                    break
            if attempt < retries:
                if run:
                    run.record_retry()
                time.sleep(2**attempt)

    attempts = min(retries + 1, 2) if isinstance(last_error, EmptyTranslationResponseError) else retries + 1
    raise RuntimeError(f"Translation failed after {attempts} attempts: {last_error}")


def is_non_retryable_api_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    return isinstance(status_code, int) and 400 <= status_code < 500 and status_code not in {408, 429}


def failed_translation(paragraph: Paragraph, error: Exception) -> dict[str, dict[str, str]]:
    status = "needs_formula_recovery" if formula_tokens(paragraph.source) else "needs_ocr"
    print(f"Translation recovery failed for {paragraph.id}: {error}", file=sys.stderr, flush=True)
    if isinstance(error, FormulaTokenError):
        note = "公式占位符存在遗漏或重复，已停止自动恢复以避免生成错误公式。"
    elif isinstance(error, ReferenceTokenError):
        note = "引用占位符存在遗漏或重复，已停止自动恢复以避免错误引用。"
    else:
        note = "该段落在重试后仍无法可靠恢复。"
    return {
        paragraph.id: {
            "id": paragraph.id,
            "status": status,
            "translation": "",
            "note": note,
        }
    }


def recover_protected_token_failure(
    model: str,
    chunk: list[Paragraph],
    error: ProtectedTokenError,
    retries: int,
    ocr_context_by_page: dict[int, str] | None,
    structured: bool,
    client: OpenAI,
    run: TranslationRun | None,
) -> dict[str, dict[str, str]]:
    problem_index = next(
        (index for index, paragraph in enumerate(chunk) if paragraph.id == error.item_id),
        None,
    )
    if problem_index is None:
        raise error
    if len(chunk) == 1:
        return failed_translation(chunk[0], error)

    print(
        f"Protected-token validation failed for {error.item_id}; retrying that paragraph directly.",
        file=sys.stderr,
        flush=True,
    )
    if run:
        run.record_retry()
    recovered: dict[str, dict[str, str]] = {}
    segments = [
        chunk[:problem_index],
        chunk[problem_index : problem_index + 1],
        chunk[problem_index + 1 :],
    ]
    for segment in segments:
        if segment:
            recovered.update(
                translate_chunk_with_harness(
                    model,
                    segment,
                    retries,
                    ocr_context_by_page,
                    structured,
                    client,
                    run,
                )
            )
    return recovered


def translate_chunk_with_harness(
    model: str,
    chunk: list[Paragraph],
    retries: int,
    ocr_context_by_page: dict[int, str] | None = None,
    structured: bool = False,
    client: OpenAI | None = None,
    run: TranslationRun | None = None,
) -> dict[str, dict[str, str]]:
    active_client = client or create_deepseek_client()
    try:
        return translate_chunk(
            active_client,
            model,
            chunk,
            retries,
            ocr_context_by_page,
            structured,
            run,
        )
    except NonRetryableTranslationError:
        raise
    except ProtectedTokenError as error:
        return recover_protected_token_failure(
            model,
            chunk,
            error,
            retries,
            ocr_context_by_page,
            structured,
            active_client,
            run,
        )
    except Exception as error:  # noqa: BLE001 - split failed batches before giving up.
        if len(chunk) > 1:
            midpoint = len(chunk) // 2
            if run:
                run.record_retry()
            print(
                f"Translation batch failed ({error}); retrying as {midpoint}+{len(chunk) - midpoint} paragraphs.",
                file=sys.stderr,
                flush=True,
            )
            recovered = translate_chunk_with_harness(
                model,
                chunk[:midpoint],
                retries,
                ocr_context_by_page,
                structured,
                active_client,
                run,
            )
            recovered.update(
                translate_chunk_with_harness(
                    model,
                    chunk[midpoint:],
                    retries,
                    ocr_context_by_page,
                    structured,
                    active_client,
                    run,
                )
            )
            return recovered

        return failed_translation(chunk[0], error)


def apply_translations(
    sections: list[Section],
    translations: dict[str, dict[str, str]],
    formulas: dict[str, str] | None = None,
    references: ReferenceBundle | None = None,
) -> None:
    formulas = formulas or {}
    references = references or ReferenceBundle()
    for section in sections:
        for paragraph in section.paragraphs:
            item = translations.get(paragraph.id)
            if not item:
                paragraph.status = "needs_ocr"
                paragraph.translation = ""
                paragraph.note = "Missing translation from API response."
                continue
            paragraph.status = item.get("status", "needs_ocr")
            paragraph.translation = restore_all_tokens(item.get("translation", ""), formulas, references)
            paragraph.note = item.get("note", "")


def structured_asset_units(bundle: ReferenceBundle) -> tuple[list[Paragraph], dict[str, tuple[dict[str, Any], str]]]:
    units: list[Paragraph] = []
    targets: dict[str, tuple[dict[str, Any], str]] = {}

    def register(asset: dict[str, Any], field: str, source: str, suffix: str) -> None:
        if not source.strip():
            return
        unit_id = f"{asset['id']}-{suffix}"
        units.append(Paragraph(id=unit_id, page=1, anchor=f"{asset['kind']} {asset['number']}", source=source))
        targets[unit_id] = (asset, field)

    for asset in bundle.assets:
        if not asset.get("referenced"):
            continue
        register(asset, "captionTranslation", asset.get("captionSource", ""), "caption")
        for row_index, row in enumerate(asset.get("rows", []), start=1):
            for cell_index, cell in enumerate(row, start=1):
                source = cell.get("source", "")
                if len(row) == 1 or len(source) >= 80:
                    register(asset, f"cell:{row_index - 1}:{cell_index - 1}", source, f"r{row_index}c{cell_index}")
        for step_index, step in enumerate(asset.get("steps", []), start=1):
            register(asset, f"step:{step_index - 1}", step.get("source", ""), f"step{step_index}")
    return units, targets


def apply_structured_asset_translations(
    bundle: ReferenceBundle,
    translations: dict[str, dict[str, str]],
    targets: dict[str, tuple[dict[str, Any], str]],
) -> None:
    for unit_id, (asset, field) in targets.items():
        item = translations.get(unit_id, {})
        translation = item.get("translation", "") if item.get("status") == "translated" else ""
        if field == "captionTranslation":
            asset[field] = translation
        elif field.startswith("cell:"):
            _, row_index, cell_index = field.split(":")
            asset["rows"][int(row_index)][int(cell_index)]["translation"] = translation
        elif field.startswith("step:"):
            asset["steps"][int(field.split(":")[1])]["translation"] = translation


def translate_all_batches(
    body_chunks: list[list[Paragraph]],
    structured_chunks: list[list[Paragraph]],
    model: str,
    retries: int,
    parallelism: int,
    run: TranslationRun,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Run body and structured batches through one shared concurrency limit."""

    queued: list[tuple[str, list[Paragraph]]] = []
    for index in range(max(len(body_chunks), len(structured_chunks))):
        if index < len(body_chunks):
            queued.append(("body", body_chunks[index]))
        if index < len(structured_chunks):
            queued.append(("structured", structured_chunks[index]))

    run.total_batches = len(queued)
    body_translations: dict[str, dict[str, str]] = {}
    structured_translations: dict[str, dict[str, str]] = {}
    pending: list[tuple[str, str, list[Paragraph]]] = []
    for kind, chunk in queued:
        batch_id = batch_fingerprint(kind, chunk)
        cached = run.cached(batch_id)
        if cached is not None:
            (body_translations if kind == "body" else structured_translations).update(cached)
            run.completed_batches += 1
        else:
            pending.append((kind, batch_id, chunk))
    run.emit()

    def translate_batch(
        item: tuple[str, str, list[Paragraph]],
    ) -> tuple[str, str, dict[str, dict[str, str]]]:
        kind, batch_id, chunk = item
        result = translate_chunk_with_harness(
            model,
            chunk,
            retries,
            structured=kind == "structured",
            client=run.client,
            run=run,
        )
        run.complete(batch_id, result)
        return kind, batch_id, result

    workers = bounded_parallelism(parallelism, len(pending)) if pending else 1
    if pending:
        print(
            f"Translating {len(pending)} remaining batches with shared parallelism={workers} "
            f"({len(body_chunks)} body, {len(structured_chunks)} structured)...",
            file=sys.stderr,
            flush=True,
        )
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(translate_batch, item) for item in pending]
        for future in concurrent.futures.as_completed(futures):
            kind, batch_id, translations = future.result()
            (body_translations if kind == "body" else structured_translations).update(translations)
    return body_translations, structured_translations


def materialize_structured_images(bundle: ReferenceBundle, output: Path) -> None:
    if output.parent.name == "translations":
        asset_root = output.parent.parent / "paper-assets" / output.stem
        asset_url_root = f"./paper-assets/{output.stem}"
    else:
        asset_root = output.parent / f"{output.stem}-assets"
        asset_url_root = f"./{output.stem}-assets"
    for asset in bundle.assets:
        raw_images = asset.get("images", [])
        urls: list[str] = []
        if not asset.get("referenced"):
            asset["images"] = []
            continue
        for image_index, image in enumerate(raw_images, start=1):
            data = image.pop("_bytes", b"")
            suffix = image.pop("_suffix", ".png")
            if not data:
                continue
            asset_root.mkdir(parents=True, exist_ok=True)
            if suffix == ".pdf":
                import pymupdf

                document = pymupdf.open(stream=data, filetype="pdf")
                if not document.page_count:
                    continue
                pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                filename = f"{asset['id']}-{image_index}.png"
                pixmap.save(str(asset_root / filename))
                document.close()
            else:
                safe_suffix = suffix if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"} else ".png"
                filename = f"{asset['id']}-{image_index}{safe_suffix}"
                (asset_root / filename).write_bytes(data)
            urls.append(f"{asset_url_root}/{filename}")
        asset["images"] = urls


def serialized_structured_assets(
    bundle: ReferenceBundle,
    formulas: dict[str, str],
) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for asset in bundle.assets:
        if not asset.get("referenced"):
            continue
        item = copy.deepcopy({key: value for key, value in asset.items() if not key.startswith("_")})
        item["captionSource"] = restore_all_tokens(item.get("captionSource", ""), formulas, bundle)
        item["captionTranslation"] = restore_all_tokens(item.get("captionTranslation", ""), formulas, bundle)
        for row in item.get("rows", []):
            for cell in row:
                cell["source"] = restore_all_tokens(cell.get("source", ""), formulas, bundle)
                cell["translation"] = restore_all_tokens(cell.get("translation", ""), formulas, bundle)
        for step in item.get("steps", []):
            step["source"] = restore_all_tokens(step.get("source", ""), formulas, bundle)
            step["translation"] = restore_all_tokens(step.get("translation", ""), formulas, bundle)
        serialized.append(item)
    return serialized


def serialized_cross_reference_labels(
    bundle: ReferenceBundle,
    formulas: dict[str, str],
) -> dict[str, dict[str, Any]]:
    labels = copy.deepcopy(bundle.labels)
    for metadata in labels.values():
        metadata["title"] = restore_all_tokens(metadata.get("title", ""), formulas, bundle)
    return labels


def recover_needs_formula_with_docling(
    pdf: Path,
    sections: list[Section],
    translations: dict[str, dict[str, str]],
    model: str,
    retries: int,
    client: OpenAI | None = None,
    run: TranslationRun | None = None,
) -> set[int]:
    difficult = [
        paragraph
        for section in sections
        for paragraph in section.paragraphs
        if translations.get(paragraph.id, {}).get("status") == "needs_formula_recovery"
    ]
    if not difficult:
        return set()

    pages = {paragraph.page for paragraph in difficult}
    page_range = ",".join(str(page) for page in sorted(pages))
    print(
        f"{len(difficult)} items requested formula recovery on pages {page_range}; running Docling formula enrichment...",
        file=sys.stderr,
        flush=True,
    )
    try:
        formula_context = dict(
            extract_pdf_pages_docling(
                pdf,
                page_range,
                force_ocr=False,
                enrich_formulas=True,
            )
        )
    except Exception as error:  # noqa: BLE001 - retain explicit formula recovery status.
        print(f"Docling formula recovery unavailable: {error}", file=sys.stderr, flush=True)
        return set()

    recovered_pages: set[int] = set()
    for page in sorted(pages):
        page_paragraphs = [paragraph for paragraph in difficult if paragraph.page == page]
        if not formula_context.get(page, "").strip():
            continue
        recovered = translate_chunk_with_harness(
            model,
            page_paragraphs,
            retries,
            {page: formula_context[page]},
            client=client,
            run=run,
        )
        for paragraph_id, item in recovered.items():
            translations[paragraph_id] = item
            if item.get("status") != "needs_formula_recovery":
                recovered_pages.add(page)
    return recovered_pages


def recover_needs_ocr_with_docling(
    pdf: Path,
    sections: list[Section],
    translations: dict[str, dict[str, str]],
    model: str,
    retries: int,
    client: OpenAI | None = None,
    run: TranslationRun | None = None,
) -> set[int]:
    difficult = [
        paragraph
        for section in sections
        for paragraph in section.paragraphs
        if translations.get(paragraph.id, {}).get("status") == "needs_ocr"
    ]
    if not difficult:
        return set()

    pages = {paragraph.page for paragraph in difficult}
    page_range = ",".join(str(page) for page in sorted(pages))
    print(
        f"{len(difficult)} items requested OCR help on pages {page_range}; running Docling...",
        file=sys.stderr,
        flush=True,
    )
    try:
        ocr_context = dict(extract_pdf_pages_docling(pdf, page_range, force_ocr=True))
    except Exception as error:  # noqa: BLE001 - retain explicit needs_ocr statuses.
        print(f"Docling recovery unavailable: {error}", file=sys.stderr, flush=True)
        return set()

    recovered_pages: set[int] = set()
    for page in sorted(pages):
        page_paragraphs = [paragraph for paragraph in difficult if paragraph.page == page]
        if not ocr_context.get(page, "").strip():
            continue
        recovered = translate_chunk_with_harness(
            model,
            page_paragraphs,
            retries,
            {page: ocr_context[page]},
            client=client,
            run=run,
        )
        for paragraph_id, item in recovered.items():
            translations[paragraph_id] = item
            if item.get("status") != "needs_ocr":
                recovered_pages.add(page)
    return recovered_pages


def build_output(
    title: str,
    paper_url: str,
    coverage: str,
    source_note: str,
    sections: list[Section],
    extraction_method: str,
    formula_count: int,
    formulas: dict[str, str] | None = None,
    references: ReferenceBundle | None = None,
) -> dict[str, Any]:
    formulas = formulas or {}
    references = references or ReferenceBundle()
    status_counts = {status: 0 for status in sorted(TRANSLATION_STATUSES)}
    for section in sections:
        for paragraph in section.paragraphs:
            status_counts[paragraph.status or "needs_ocr"] += 1
    return {
        "title": title,
        "paperUrl": paper_url,
        "coverage": coverage,
        "source": source_note,
        "generatorVersion": GENERATOR_VERSION,
        "extractionMethod": extraction_method,
        "contentFormat": "markdown+latex",
        "formulaCount": formula_count,
        "crossReferences": {"labels": serialized_cross_reference_labels(references, formulas)},
        "citations": references.citations,
        "structuredContent": serialized_structured_assets(references, formulas),
        "statusCounts": status_counts,
        "sections": [
            {
                "id": section.id,
                "title": restore_all_tokens(section.title, formulas, references),
                "pageStart": section.page_start,
                "pageEnd": section.page_end,
                "paragraphs": [
                    {
                        "id": paragraph.id,
                        "page": paragraph.page,
                        **({"pageEnd": paragraph.page_end} if paragraph.page_end > paragraph.page else {}),
                        "anchor": restore_all_tokens(paragraph.anchor, formulas, references),
                        "sourceText": restore_all_tokens(paragraph.source, formulas, references),
                        "status": paragraph.status or "needs_ocr",
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


def source_display_name(path: Path) -> str:
    return path.name


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
    progress_callback: ProgressCallback | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    extracted_pages: list[tuple[int, str]] = []
    source_name = ""
    extraction_method = ""
    extraction_ocr_pages: set[int] = set()
    formula_recovery_pages: set[int] = set()
    formulas: dict[str, str] = {}
    references = ReferenceBundle()
    pdf_pages_used = False

    if text:
        extracted_pages = extract_text_pages(text)
        source_name = source_display_name(text)
        extraction_method = "text"
    elif latex:
        try:
            latex_pages, formulas, references = extract_latex_document_with_references(latex)
            latex_summary = extraction_quality_summary(latex_pages)
            log_extraction_quality("latex", latex_summary)
            if extraction_is_usable(latex_summary) or not pdf:
                extracted_pages = latex_pages
                source_name = source_display_name(latex)
                extraction_method = "latex"
            else:
                print("LaTeX extraction is incomplete; falling back to PDF.", file=sys.stderr, flush=True)
        except Exception as error:  # noqa: BLE001 - auto mode may still use the PDF.
            if not pdf:
                raise
            print(f"LaTeX extraction failed ({error}); falling back to PDF.", file=sys.stderr, flush=True)

    if not extracted_pages and pdf:
        formulas = {}
        extracted_pages, extraction_ocr_pages = extract_pdf_pages_adaptive(pdf, pages, pdf_extractor)
        pdf_pages_used = True
        native_summary = extraction_quality_summary(extracted_pages)
        log_extraction_quality("pdf-native", native_summary)
        source_name = source_display_name(pdf)
        extraction_method = "pdf-native+docling-ocr" if extraction_ocr_pages else "pdf-native"

        if not extraction_is_usable(native_summary):
            try:
                docling_pages = extract_pdf_pages_docling(
                    pdf,
                    pages,
                    force_ocr=True,
                    enrich_formulas=True,
                )
                docling_summary = extraction_quality_summary(docling_pages)
                log_extraction_quality("docling-ocr", docling_summary)
                if docling_summary["score"] < native_summary["score"]:
                    extracted_pages = docling_pages
                    extraction_method = "docling-ocr"
                    extraction_ocr_pages = {page for page, _text in docling_pages}
            except Exception as error:  # noqa: BLE001 - keep the best native result.
                print(f"Full Docling fallback unavailable: {error}", file=sys.stderr, flush=True)
    elif not extracted_pages and not text:
        raise ValueError("Either pdf, latex, or text must be provided.")

    if not extracted_pages:
        raise RuntimeError("No text pages were extracted.")

    if extraction_method != "latex":
        protected_pages: list[tuple[int, str]] = []
        for page, page_text in extracted_pages:
            protected_text, formulas = protect_latex_math(page_text, formulas)
            protected_pages.append((page, protected_text))
        extracted_pages = protected_pages

    heading_hints: dict[int, set[str]] = {}
    noise_hints: set[str] = set()
    blocked_hints: set[str] = set()
    if pdf_pages_used and pdf is not None:
        try:
            heading_hints, noise_hints, blocked_hints = pdf_structure_hints(pdf, pages)
            print(
                f"PDF layout hints: {sum(len(items) for items in heading_hints.values())} headings, "
                f"{len(noise_hints)} running-head/footer lines, "
                f"{len(blocked_hints)} figure/table text blocks.",
                flush=True,
            )
        except Exception as error:  # noqa: BLE001 - hints are a best-effort improvement.
            print(f"PDF layout hints unavailable: {error}", file=sys.stderr, flush=True)
            heading_hints, noise_hints, blocked_hints = {}, set(), set()

    sections = segment_document(
        extracted_pages,
        heading_hints=heading_hints,
        noise_hints=noise_hints,
        blocked_hints=blocked_hints,
    )
    associate_label_targets(sections, references)
    if not sections:
        raise RuntimeError("No paragraphs were detected.")
    body_formula_count = sum(
        len(formula_tokens(paragraph.source))
        for section in sections
        for paragraph in section.paragraphs
    )

    if dry_run:
        for section in sections:
            for paragraph in section.paragraphs:
                paragraph.status = "translated"
                paragraph.translation = "TODO"
                paragraph.note = "Dry run placeholder."
    else:
        active_model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        body_chunks = chunk_paragraphs(sections, max_chars)
        structured_units, structured_targets = structured_asset_units(references)
        structured_chunks = chunk_units(
            structured_units,
            min(max_chars, 2600),
            max_items=6,
            target_items=5,
        )
        fingerprint_data = {
            "model": active_model,
            "maxChars": max_chars,
            "body": [[paragraph.id, paragraph.source] for chunk in body_chunks for paragraph in chunk],
            "structured": [[paragraph.id, paragraph.source] for paragraph in structured_units],
        }
        paper_fingerprint = hashlib.sha256(
            json.dumps(fingerprint_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        data_root = Path(
            os.getenv("PAPER_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data"))
        ).expanduser().resolve()
        checkpoint_path = data_root / "checkpoints" / (
            f"{output.stem}-{paper_fingerprint[:16]}.json"
        )
        if not resume:
            checkpoint_path.unlink(missing_ok=True)
        run = TranslationRun(
            client=create_deepseek_client(),
            fingerprint=paper_fingerprint,
            checkpoint_path=checkpoint_path,
            progress_callback=progress_callback,
        )
        translations, structured_translations = translate_all_batches(
            body_chunks,
            structured_chunks,
            active_model,
            retries,
            parallelism,
            run,
        )
        apply_structured_asset_translations(references, structured_translations, structured_targets)
        recovery_pages: set[int] = set()
        if pdf and extraction_method.startswith(("pdf", "docling")):
            formula_recovery_pages = recover_needs_formula_with_docling(
                pdf,
                sections,
                translations,
                active_model,
                retries,
                run.client,
                run,
            )
            recovery_pages = recover_needs_ocr_with_docling(
                pdf,
                sections,
                translations,
                active_model,
                retries,
                run.client,
                run,
            )
        extraction_ocr_pages.update(recovery_pages)
        apply_translations(sections, translations, formulas, references)

    materialize_structured_images(references, output)
    source_note = (
        f"Generated from {source_name} using {extraction_method}. "
        f"Docling OCR pages: {', '.join(map(str, sorted(extraction_ocr_pages))) or 'none'}. "
        f"Docling formula recovery pages: {', '.join(map(str, sorted(formula_recovery_pages))) or 'none'}. "
        "The PDF remains the authoritative original; "
        "this JSON stores Chinese translations, source paragraphs, formulas, and cross-reference metadata."
    )
    result = build_output(
        title,
        paper_url,
        coverage,
        source_note,
        sections,
        extraction_method,
        body_formula_count,
        formulas,
        references,
    )
    if not dry_run:
        result["translationProgress"] = run.snapshot("completed")
        run.emit("completed")
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
    parser.add_argument("--max-chars", type=int, default=4000, help="Approximate source chars per API call.")
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
