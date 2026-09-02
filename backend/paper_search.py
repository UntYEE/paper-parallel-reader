"""Paper discovery backed by local cache and DeepSeek native web search."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable


ARXIV_ID_RE = re.compile(r"^(?:arxiv\s*:\s*)?(\d{4}\.\d{4,5}(?:v\d+)?)$", re.IGNORECASE)
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
ACADEMIC_DOMAINS = {
    "academic.oup.com",
    "arxiv.org",
    "aclanthology.org",
    "aip.scitation.org",
    "biorxiv.org",
    "bmj.com",
    "cambridge.org",
    "cell.com",
    "dl.acm.org",
    "doi.org",
    "frontiersin.org",
    "ieeexplore.ieee.org",
    "jamanetwork.com",
    "jmlr.org",
    "journals.aps.org",
    "journals.plos.org",
    "link.springer.com",
    "medrxiv.org",
    "mdpi.com",
    "nature.com",
    "nejm.org",
    "onlinelibrary.wiley.com",
    "openaccess.thecvf.com",
    "openreview.net",
    "osf.io",
    "pmlr.press",
    "proceedings.neurips.cc",
    "pubs.acs.org",
    "pubmed.ncbi.nlm.nih.gov",
    "researchsquare.com",
    "science.org",
    "sciencedirect.com",
    "semanticscholar.org",
    "ssrn.com",
    "tandfonline.com",
    "thelancet.com",
    "zenodo.org",
}


@dataclass
class PaperCandidate:
    id: str
    title: str
    authors: list[str]
    year: int | None
    venue: str
    source: str
    landing_url: str
    pdf_url: str
    score: float = 0.0
    cached_name: str = ""

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            "id": data["id"],
            "title": data["title"],
            "authors": data["authors"],
            "year": data["year"],
            "venue": data["venue"],
            "source": data["source"],
            "landingUrl": data["landing_url"],
            "pdfUrl": data["pdf_url"],
            "score": round(data["score"], 4),
            "cachedName": data["cached_name"],
        }


@dataclass(frozen=True)
class SearchSource:
    url: str
    title: str = ""
    published_at: str = ""
    snippet: str = ""


def normalize_title(value: str) -> str:
    return " ".join(WORD_RE.findall(value.casefold()))


def title_score(query: str, title: str, rank: int = 0) -> float:
    normalized_query = normalize_title(query)
    normalized_title = normalize_title(title)
    if not normalized_query or not normalized_title:
        return 0.0
    query_words = set(normalized_query.split())
    title_words = set(normalized_title.split())
    title_compact = normalized_title.replace(" ", "")
    matched_words = {
        word for word in query_words if word in title_words or (len(word) >= 4 and word in title_compact)
    }
    overlap = len(matched_words) / max(1, len(query_words))
    sequence = SequenceMatcher(None, normalized_query, normalized_title).ratio()
    exact_bonus = 0.18 if normalized_query == normalized_title else 0.0
    phrase_bonus = 0.08 if normalized_query in normalized_title else 0.0
    first_word = normalized_query.split()[0]
    leading_term_bonus = 0.12 if len(first_word) >= 4 and title_compact.startswith(first_word) else 0.0
    rank_bonus = 0.06 / (rank + 1)
    return min(
        1.0,
        0.58 * sequence + 0.28 * overlap + exact_bonus + phrase_bonus + leading_term_bonus + rank_bonus,
    )


def arxiv_id_from_query(query: str) -> str:
    match = ARXIV_ID_RE.fullmatch(query.strip())
    return match.group(1) if match else ""


def direct_arxiv_candidate(query: str) -> PaperCandidate | None:
    arxiv_id = arxiv_id_from_query(query)
    if not arxiv_id:
        return None
    return PaperCandidate(
        id=f"arxiv:{arxiv_id}",
        title=f"arXiv {arxiv_id}",
        authors=[],
        year=None,
        venue="arXiv",
        source="arXiv ID",
        landing_url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
        score=1.0,
    )


def extract_deepseek_sources(payload: dict[str, Any]) -> list[SearchSource]:
    blocks = payload.get("content") or []
    result_blocks = [block for block in blocks if block.get("type") == "web_search_tool_result"]
    if not result_blocks:
        raise RuntimeError("DeepSeek returned no structured web_search_tool_result blocks.")

    snippets: dict[str, str] = {}
    for block in blocks:
        if block.get("type") != "text":
            continue
        for citation in block.get("citations") or []:
            url = str(citation.get("url") or "")
            cited_text = str(citation.get("cited_text") or "")
            if url and cited_text and url not in snippets:
                snippets[url] = cited_text

    sources: list[SearchSource] = []
    seen: set[str] = set()
    for block in result_blocks:
        for item in block.get("content") or []:
            if item.get("type") != "web_search_result":
                continue
            url = str(item.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(
                SearchSource(
                    url=url,
                    title=str(item.get("title") or ""),
                    published_at=str(item.get("page_age") or ""),
                    snippet=snippets.get(url, ""),
                )
            )
    return sources


def deepseek_native_search(query: str, api_key: str, limit: int) -> list[SearchSource]:
    api_key = api_key.strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is required for paper search.")
    max_uses = max(1, min(5, int(os.getenv("DEEPSEEK_SEARCH_MAX_USES", "3"))))
    request_body = {
        "model": os.getenv("DEEPSEEK_SEARCH_MODEL", os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")),
        "max_tokens": max(256, int(os.getenv("DEEPSEEK_SEARCH_MAX_TOKENS", "1200"))),
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Find the original academic papers whose own title or central contribution matches "
                            f"this title-or-keyword query: {query}. First infer likely canonical title variants, "
                            "including common hyphenation and adjacent-word compounds, and search those variants. "
                            "Prioritize the original paper's canonical page and direct PDF on arXiv, OpenReview, "
                            "ACL Anthology, PubMed, major publishers, or conference proceedings. Exclude papers "
                            "whose own title is unrelated and that merely cite, mention, or apply the requested "
                            "paper or method. Return web search results for the original matching papers."
                        ),
                    }
                ],
            }
        ],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}],
    }
    base_url = os.getenv("DEEPSEEK_SEARCH_BASE_URL", "https://api.deepseek.com/anthropic/v1").rstrip("/")
    endpoint = f"{base_url}/messages"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(request_body).encode("utf-8"),
        method="POST",
        headers={
            "x-api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "PaperParallelReader/1.0",
        },
    )
    timeout = max(5, int(os.getenv("PAPER_SEARCH_TIMEOUT", "45")))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if clean_url(response.geturl()) != clean_url(endpoint):
                raise RuntimeError("DeepSeek search endpoint redirected unexpectedly.")
            payload = json.loads(response.read())
    except Exception as error:
        raise RuntimeError(f"DeepSeek native search failed: {error}") from error

    return extract_deepseek_sources(payload)[: max(limit * 2, limit)]


def is_allowed_paper_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    hostname = (parsed.hostname or "").casefold()
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in ACADEMIC_DOMAINS)


def clean_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def arxiv_id_from_result_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    if not (hostname == "arxiv.org" or hostname.endswith(".arxiv.org") or hostname.startswith("arxiv-org.")):
        return ""
    decoded = urllib.parse.unquote(f"{parsed.path}?{parsed.query}")
    match = re.search(
        r"(?:/(?:abs|pdf|html)/|oai(?:%3A|:)arxiv\.org(?:%3A|:))(\d{4}\.\d{4,5}(?:v\d+)?)",
        decoded,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def verify_pdf_url(url: str) -> str:
    if not is_allowed_paper_url(url):
        return ""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/pdf",
            "Range": "bytes=0-7",
            "User-Agent": "PaperParallelReader/1.0",
        },
    )
    try:
        timeout = max(3, int(os.getenv("PAPER_PDF_VERIFY_TIMEOUT", "10")))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = clean_url(response.geturl())
            content_type = response.headers.get_content_type()
            magic = response.read(8)
    except Exception:
        return ""
    if not is_allowed_paper_url(final_url) or content_type != "application/pdf" or not magic.startswith(b"%PDF-"):
        return ""
    return final_url


def candidate_from_source(query: str, source: SearchSource, rank: int) -> PaperCandidate | None:
    raw_url = clean_url(source.url)
    arxiv_id = arxiv_id_from_result_url(raw_url)
    if not arxiv_id and not is_allowed_paper_url(raw_url):
        return None
    parsed = urllib.parse.urlparse(raw_url)
    hostname = (parsed.hostname or "").casefold()
    path = urllib.parse.unquote(parsed.path).rstrip("/")
    title = " ".join(source.title.split()) or hostname
    landing_url = raw_url
    pdf_url = raw_url if path.casefold().endswith(".pdf") else ""
    paper_id = f"url:{raw_url}"
    venue = hostname

    if arxiv_id:
        paper_id = f"arxiv:{arxiv_id}"
        landing_url = f"https://arxiv.org/abs/{arxiv_id}"
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        venue = "arXiv"
    elif hostname == "openreview.net":
        query_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
        if query_id:
            paper_id = f"openreview:{query_id}"
            landing_url = f"https://openreview.net/forum?id={urllib.parse.quote(query_id)}"
            pdf_url = f"https://openreview.net/pdf?id={urllib.parse.quote(query_id)}"
            venue = "OpenReview"
    elif hostname.endswith("aclanthology.org") and path and not pdf_url:
        pdf_url = f"{raw_url.rstrip('/')}.pdf"
        venue = "ACL Anthology"
    elif hostname == "pmlr.press" and path.casefold().endswith(".html"):
        pdf_url = raw_url[: -len(".html")] + ".pdf"
        venue = "PMLR"
    elif hostname.endswith("jmlr.org") and path.casefold().endswith(".html"):
        pdf_url = raw_url[: -len(".html")] + ".pdf"
        venue = "JMLR"

    if pdf_url:
        pdf_url = verify_pdf_url(pdf_url)

    published_at = source.published_at
    year_match = YEAR_RE.search(published_at)
    return PaperCandidate(
        id=paper_id,
        title=title,
        authors=[],
        year=int(year_match.group()) if year_match else None,
        venue=venue,
        source="DeepSeek Web Search",
        landing_url=landing_url,
        pdf_url=pdf_url,
        score=title_score(query, title, rank),
    )


def search_deepseek_papers(query: str, api_key: str, limit: int) -> list[PaperCandidate]:
    candidates = []
    for rank, source in enumerate(deepseek_native_search(query, api_key, limit)):
        candidate = candidate_from_source(query, source, rank)
        if candidate:
            candidates.append(candidate)
    return candidates


def cached_candidates(query: str, records: Iterable[dict[str, Any]]) -> list[PaperCandidate]:
    results: list[PaperCandidate] = []
    for record in records:
        title = str(record.get("title") or "").strip()
        if not title:
            continue
        score = title_score(query, title)
        if score < 0.55:
            continue
        results.append(
            PaperCandidate(
                id=str(record.get("paperId") or ""),
                title=title,
                authors=[],
                year=None,
                venue="Local cache",
                source="Local cache",
                landing_url=str(record.get("sourceUrl") or ""),
                pdf_url=str(record.get("sourceUrl") or ""),
                score=min(1.0, score + 0.08),
                cached_name=str(record.get("pdfName") or ""),
            )
        )
    return results


def merge_candidates(candidates: Iterable[PaperCandidate], limit: int) -> list[PaperCandidate]:
    merged: dict[str, PaperCandidate] = {}
    for candidate in candidates:
        if candidate.score < 0.35:
            continue
        key = candidate.id.casefold() if candidate.id else normalize_title(candidate.title)
        if key.startswith("arxiv:"):
            key = re.sub(r"v\d+$", "", key)
        existing = merged.get(key)
        if not existing:
            merged[key] = candidate
            continue
        if candidate.score > existing.score:
            candidate.cached_name = candidate.cached_name or existing.cached_name
            candidate.pdf_url = candidate.pdf_url or existing.pdf_url
            merged[key] = candidate
        else:
            existing.cached_name = existing.cached_name or candidate.cached_name
            existing.pdf_url = existing.pdf_url or candidate.pdf_url
    return sorted(merged.values(), key=lambda item: (item.score, bool(item.pdf_url)), reverse=True)[:limit]


def should_auto_select(results: list[PaperCandidate]) -> bool:
    if not results or not results[0].pdf_url or results[0].score < 0.93:
        return False
    return len(results) == 1 or results[0].score - results[1].score >= 0.12


def discover_papers(
    query: str,
    records: Iterable[dict[str, Any]],
    limit: int = 6,
    api_key: str = "",
    searcher: Callable[[str, str, int], list[SearchSource]] = deepseek_native_search,
) -> dict[str, Any]:
    direct = direct_arxiv_candidate(query)
    if direct:
        return {
            "results": [direct.payload()],
            "autoSelect": True,
            "manualRequired": False,
            "providerErrors": {},
            "searchMode": "arxiv-id",
        }

    local_results = merge_candidates(cached_candidates(query, records), limit)
    if should_auto_select(local_results):
        return {
            "results": [candidate.payload() for candidate in local_results],
            "autoSelect": True,
            "manualRequired": False,
            "providerErrors": {},
            "searchMode": "local-cache",
        }

    candidates = list(local_results)
    errors: dict[str, str] = {}
    try:
        for rank, source in enumerate(searcher(query, api_key, limit)):
            candidate = candidate_from_source(query, source, rank)
            if candidate:
                candidates.append(candidate)
    except Exception as error:  # noqa: BLE001 - local candidates remain useful when search fails.
        errors["DeepSeek Web Search"] = str(error)[:240]

    results = merge_candidates(candidates, limit)
    return {
        "results": [candidate.payload() for candidate in results],
        "autoSelect": should_auto_select(results),
        "manualRequired": not results,
        "providerErrors": errors,
        "searchMode": "deepseek-native" if results and not errors else ("local-cache" if results else "manual"),
    }
