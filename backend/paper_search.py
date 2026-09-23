"""Paper discovery backed by local cache and DeepSeek native web search."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

from backend.local_security import ValidatingRedirectHandler, validate_remote_url
from backend.search_cache import DEFAULT_TTL_HOURS, load as load_search_cache, save as save_search_cache


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
# Well-known scholarly hosts that the project does not restrict, but which are ranked higher when
# several providers return the same paper.
PREFERRED_EXTRA_DOMAINS = {
    "aclanthology.org",
    "biorxiv.org",
    "doi.org",
    "europepmc.org",
    "jstage.jst.go.jp",
    "medrxiv.org",
    "ncbi.nlm.nih.gov",
    "openalex.org",
    "pmc.ncbi.nlm.nih.gov",
    "sciengine.com",
    "semanticscholar.org",
}
# Hosts that only mirror copyrighted PDFs: the app refuses to download from them.
BLOCKED_DOMAINS = {
    "sci-hub.se",
    "sci-hub.st",
    "sci-hub.ru",
    "sci-hub.ren",
    "libgen.is",
    "libgen.rs",
    "libgen.st",
    "annas-archive.org",
    "annas-archive.se",
    "z-lib.io",
    "z-lib.org",
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


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def cjk_bigrams(text: str) -> set[str]:
    chars = CJK_RE.findall(text)
    if len(chars) < 2:
        return set(chars)
    return {chars[index] + chars[index + 1] for index in range(len(chars) - 1)}


def cjk_similarity(query: str, title: str) -> float:
    """Character-bigram overlap so Chinese queries are not one giant opaque token."""
    query_grams = cjk_bigrams(query)
    if not query_grams:
        return 0.0
    title_grams = cjk_bigrams(title)
    if not title_grams:
        return 0.0
    overlap = len(query_grams & title_grams) / len(query_grams)
    sequence = SequenceMatcher(
        None, re.sub(r"\s+", "", query), re.sub(r"\s+", "", title)
    ).ratio()
    return max(overlap, sequence)


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
    score = min(
        1.0,
        0.58 * sequence + 0.28 * overlap + exact_bonus + phrase_bonus + leading_term_bonus + rank_bonus,
    )
    return max(score, cjk_similarity(normalized_query, normalized_title))


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


def is_preferred_paper_url(url: str) -> bool:
    """True for well-known scholarly hosts; preferred when ranking duplicates."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    hostname = (parsed.hostname or "").casefold()
    domains = ACADEMIC_DOMAINS | PREFERRED_EXTRA_DOMAINS
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains)


def is_blocked_paper_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return True
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in BLOCKED_DOMAINS)


def is_allowed_paper_url(url: str) -> bool:
    """Public HTTPS URLs are allowed; private networks and piracy mirrors are not.

    The host is resolved and re-checked again by ``local_security`` before any request is made, so
    loosening this predicate does not open an SSRF path.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password:
        return False
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        return False
    if is_blocked_paper_url(url):
        return False
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return True
    return bool(address.is_global)


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
    try:
        timeout = max(3, int(os.getenv("PAPER_PDF_VERIFY_TIMEOUT", "10")))
        with open_validated(
            url,
            headers={
                "Accept": "application/pdf",
                "Range": "bytes=0-7",
                "User-Agent": "PaperParallelReader/1.0",
            },
            timeout=timeout,
        ) as response:
            final_url = clean_url(response.geturl())
            content_type = response.headers.get_content_type()
            magic = response.read(8)
    except Exception:
        return ""
    if not is_allowed_paper_url(final_url) or content_type != "application/pdf" or not magic.startswith(b"%PDF-"):
        return ""
    return final_url


def open_validated(url: str, *, headers: dict[str, str], timeout: float):  # noqa: ANN201
    """Open a remote URL after validating the host and every redirect target."""
    validated = validate_remote_url(url)
    opener = urllib.request.build_opener(ValidatingRedirectHandler())
    request = urllib.request.Request(validated, headers=headers)
    return opener.open(request, timeout=timeout)


def fetch_json(url: str, *, timeout: float | None = None) -> Any:
    """GET a JSON document from an academic API (public hosts only, size limited)."""
    limit = env_int("PAPER_SEARCH_RESPONSE_MB", 8) * 1024 * 1024
    seconds = timeout if timeout is not None else float(os.getenv("PAPER_SEARCH_API_TIMEOUT", "20"))
    with open_validated(
        url,
        headers={"Accept": "application/json", "User-Agent": "PaperParallelReader/1.0"},
        timeout=seconds,
    ) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise RuntimeError("Search API response is too large.")
    return json.loads(body.decode("utf-8", "replace"))


def fetch_text(url: str, *, accept: str, timeout: float | None = None) -> str:
    limit = env_int("PAPER_SEARCH_RESPONSE_MB", 8) * 1024 * 1024
    seconds = timeout if timeout is not None else float(os.getenv("PAPER_SEARCH_API_TIMEOUT", "20"))
    with open_validated(
        url,
        headers={"Accept": accept, "User-Agent": "PaperParallelReader/1.0"},
        timeout=seconds,
    ) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise RuntimeError("Search API response is too large.")
    return body.decode("utf-8", "replace")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def contact_email() -> str:
    """Crossref/OpenAlex/Unpaywall ask for a contact address in the polite pool.

    Unpaywall rejects placeholder addresses, so nothing is sent unless the user configures one.
    """
    return os.getenv("PAPER_SEARCH_CONTACT_EMAIL", "").strip()


def candidate_from_source(query: str, source: SearchSource, rank: int) -> PaperCandidate | None:
    raw_url = clean_url(source.url)
    arxiv_id = arxiv_id_from_result_url(raw_url)
    if not arxiv_id and not is_allowed_paper_url(raw_url):
        return None
    if not arxiv_id and not is_preferred_paper_url(raw_url):
        # Web search returns plenty of noise. A host we do not recognize is only accepted when it
        # looks like a direct PDF link; anything else must be pasted by the user explicitly.
        path = urllib.parse.urlparse(raw_url).path.casefold()
        if not path.endswith(".pdf"):
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


@dataclass(frozen=True)
class ProviderResult:
    name: str
    candidates: list[PaperCandidate]
    error: str = ""


def build_candidate(
    *,
    paper_id: str,
    title: str,
    query: str,
    rank: int,
    source: str,
    venue: str,
    landing_url: str,
    pdf_url: str = "",
    authors: list[str] | None = None,
    year: int | None = None,
) -> PaperCandidate:
    return PaperCandidate(
        id=paper_id,
        title=" ".join(str(title or "").split()),
        authors=list(authors or []),
        year=year,
        venue=venue,
        source=source,
        landing_url=landing_url,
        pdf_url=pdf_url,
        score=title_score(query, title, rank),
    )


def arxiv_api_provider(query: str, limit: int) -> ProviderResult:
    """arXiv Atom API: best recall for preprints and free of charge."""
    name = "arXiv API"
    try:
        url = (
            "https://export.arxiv.org/api/query?search_query="
            + urllib.parse.quote(f'all:"{query}"')
            + f"&start=0&max_results={max(1, limit)}&sortBy=relevance"
        )
        body = fetch_text(url, accept="application/atom+xml,application/xml")
    except Exception as error:  # noqa: BLE001 - a provider failure must not stop the search.
        return ProviderResult(name, [], str(error)[:200])

    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    candidates: list[PaperCandidate] = []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        return ProviderResult(name, [], f"invalid Atom response: {error}")
    for rank, entry in enumerate(root.findall("atom:entry", namespace)):
        identifier = (entry.findtext("atom:id", default="", namespaces=namespace) or "").strip()
        arxiv_id = arxiv_id_from_result_url(identifier)
        if not arxiv_id:
            continue
        title = entry.findtext("atom:title", default="", namespaces=namespace) or ""
        published = entry.findtext("atom:published", default="", namespaces=namespace) or ""
        authors = [
            (author.findtext("atom:name", default="", namespaces=namespace) or "").strip()
            for author in entry.findall("atom:author", namespace)
        ]
        candidates.append(
            build_candidate(
                paper_id=f"arxiv:{arxiv_id}",
                title=title,
                query=query,
                rank=rank,
                source=name,
                venue="arXiv",
                landing_url=f"https://arxiv.org/abs/{arxiv_id}",
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
                authors=[author for author in authors if author],
                year=int(published[:4]) if published[:4].isdigit() else None,
            )
        )
    return ProviderResult(name, candidates)


def crossref_provider(query: str, limit: int) -> ProviderResult:
    """Crossref: DOI metadata for every field, including humanities and non-English journals."""
    name = "Crossref"
    try:
        url = "https://api.crossref.org/works?query.bibliographic=" + urllib.parse.quote(query)
        url += f"&rows={max(1, limit)}"
        if contact_email():
            url += f"&mailto={urllib.parse.quote(contact_email())}"
        payload = fetch_json(url)
    except Exception as error:  # noqa: BLE001
        return ProviderResult(name, [], str(error)[:200])

    candidates: list[PaperCandidate] = []
    for rank, item in enumerate(payload.get("message", {}).get("items", []) or []):
        doi = str(item.get("DOI") or "").strip()
        titles = item.get("title") or []
        title = str(titles[0]) if titles else ""
        if not doi or not title:
            continue
        authors = [
            " ".join(part for part in (author.get("given"), author.get("family")) if part)
            for author in item.get("author") or []
        ]
        issued = (item.get("issued") or {}).get("date-parts") or []
        year = issued[0][0] if issued and issued[0] and isinstance(issued[0][0], int) else None
        venue = str((item.get("container-title") or [""])[0] or "")
        pdf_url = ""
        for link in item.get("link") or []:
            if str(link.get("content-type") or "").startswith("application/pdf"):
                pdf_url = str(link.get("URL") or "")
                break
        candidates.append(
            build_candidate(
                paper_id=f"doi:{doi.casefold()}",
                title=title,
                query=query,
                rank=rank,
                source=name,
                venue=venue or "Crossref",
                landing_url=f"https://doi.org/{doi}",
                pdf_url=pdf_url,
                authors=[author for author in authors if author],
                year=year,
            )
        )
    return ProviderResult(name, candidates)


def openalex_provider(query: str, limit: int) -> ProviderResult:
    """OpenAlex: broad coverage with open-access PDF locations."""
    name = "OpenAlex"
    try:
        url = "https://api.openalex.org/works?search=" + urllib.parse.quote(query)
        url += f"&per-page={max(1, limit)}"
        if contact_email():
            url += f"&mailto={urllib.parse.quote(contact_email())}"
        payload = fetch_json(url)
    except Exception as error:  # noqa: BLE001
        return ProviderResult(name, [], str(error)[:200])

    candidates: list[PaperCandidate] = []
    for rank, item in enumerate(payload.get("results", []) or []):
        title = str(item.get("title") or item.get("display_name") or "")
        if not title:
            continue
        doi = str(item.get("doi") or "").removeprefix("https://doi.org/")
        paper_id = f"doi:{doi.casefold()}" if doi else f"openalex:{str(item.get('id') or '').rsplit('/', 1)[-1]}"
        best = item.get("best_oa_location") or item.get("primary_location") or {}
        landing = str(best.get("landing_page_url") or item.get("doi") or item.get("id") or "")
        pdf_url = str(best.get("pdf_url") or "")
        authors = [
            str((entry.get("author") or {}).get("display_name") or "")
            for entry in item.get("authorships") or []
        ]
        candidates.append(
            build_candidate(
                paper_id=paper_id,
                title=title,
                query=query,
                rank=rank,
                source=name,
                venue=str((item.get("primary_location") or {}).get("source", {}).get("display_name") or "OpenAlex")
                if isinstance((item.get("primary_location") or {}).get("source"), dict)
                else "OpenAlex",
                landing_url=landing,
                pdf_url=pdf_url,
                authors=[author for author in authors if author],
                year=item.get("publication_year") if isinstance(item.get("publication_year"), int) else None,
            )
        )
    return ProviderResult(name, candidates)


def semantic_scholar_provider(query: str, limit: int) -> ProviderResult:
    """Semantic Scholar Graph API: good relevance ranking plus open-access PDF links."""
    name = "Semantic Scholar"
    try:
        url = (
            "https://api.semanticscholar.org/graph/v1/paper/search?query="
            + urllib.parse.quote(query)
            + f"&limit={max(1, limit)}&fields=title,year,authors,externalIds,openAccessPdf,url"
        )
        payload = fetch_json(url)
    except Exception as error:  # noqa: BLE001
        return ProviderResult(name, [], str(error)[:200])

    candidates: list[PaperCandidate] = []
    for rank, item in enumerate(payload.get("data", []) or []):
        title = str(item.get("title") or "")
        if not title:
            continue
        external = item.get("externalIds") or {}
        arxiv_id = str(external.get("ArXiv") or "").strip()
        doi = str(external.get("DOI") or "").strip()
        if arxiv_id:
            paper_id = f"arxiv:{arxiv_id}"
            landing = f"https://arxiv.org/abs/{arxiv_id}"
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        elif doi:
            paper_id = f"doi:{doi.casefold()}"
            landing = f"https://doi.org/{doi}"
            pdf_url = ""
        else:
            paper_id = f"s2:{item.get('paperId') or rank}"
            landing = str(item.get("url") or "")
            pdf_url = ""
        open_pdf = str((item.get("openAccessPdf") or {}).get("url") or "")
        candidates.append(
            build_candidate(
                paper_id=paper_id,
                title=title,
                query=query,
                rank=rank,
                source=name,
                venue="Semantic Scholar",
                landing_url=landing,
                pdf_url=open_pdf or pdf_url,
                authors=[str(author.get("name") or "") for author in item.get("authors") or []],
                year=item.get("year") if isinstance(item.get("year"), int) else None,
            )
        )
    return ProviderResult(name, candidates)


DEFAULT_PROVIDERS: tuple[Callable[[str, int], ProviderResult], ...] = (
    arxiv_api_provider,
    crossref_provider,
    openalex_provider,
    semantic_scholar_provider,
)


def run_providers(
    query: str,
    limit: int,
    providers: Iterable[Callable[[str, int], ProviderResult]],
) -> tuple[list[PaperCandidate], dict[str, str], list[str]]:
    candidates: list[PaperCandidate] = []
    errors: dict[str, str] = {}
    used: list[str] = []
    for provider in providers:
        try:
            result = provider(query, limit)
        except Exception as error:  # noqa: BLE001 - provider failures are reported, not fatal.
            errors[getattr(provider, "__name__", "provider")] = str(error)[:200]
            continue
        if result.error:
            errors[result.name] = result.error
        if result.candidates:
            used.append(result.name)
            candidates.extend(result.candidates)
    return candidates, errors, used


def unpaywall_pdf_url(doi: str) -> str:
    """Unpaywall knows the legal open-access PDF for a DOI, even for paywalled journals."""
    doi = doi.strip().casefold()
    email = contact_email()
    if not doi or not email:
        # Unpaywall rejects placeholder addresses; without a configured address we skip it.
        return ""
    url = (
        "https://api.unpaywall.org/v2/"
        + urllib.parse.quote(doi)
        + f"?email={urllib.parse.quote(email)}"
    )
    payload = fetch_json(url)
    if not isinstance(payload, dict):
        return ""
    locations = [payload.get("best_oa_location") or {}] + list(payload.get("oa_locations") or [])
    for location in locations:
        if not isinstance(location, dict):
            continue
        for key in ("url_for_pdf", "url"):
            candidate = str(location.get(key) or "")
            if candidate.lower().endswith(".pdf"):
                return candidate
    return ""


def verify_candidate_pdfs(candidates: list[PaperCandidate], limit: int) -> None:
    """Fill in and confirm the first few open-PDF links so the UI can offer a direct download."""
    if limit <= 0:
        return
    checked = 0
    for candidate in candidates:
        if checked >= limit:
            return
        if candidate.source == "Local cache":
            continue
        if not candidate.pdf_url and candidate.id.startswith("doi:"):
            try:
                candidate.pdf_url = unpaywall_pdf_url(candidate.id.removeprefix("doi:"))
            except Exception:  # noqa: BLE001 - enrichment is best effort.
                candidate.pdf_url = ""
        if not candidate.pdf_url:
            continue
        checked += 1
        candidate.pdf_url = verify_pdf_url(candidate.pdf_url)


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


def doi_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url or "")
    hostname = (parsed.hostname or "").casefold()
    if hostname != "doi.org" and not hostname.endswith(".doi.org"):
        return ""
    match = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", urllib.parse.unquote(url), re.IGNORECASE)
    return match.group(0).rstrip(".").casefold() if match else ""


def candidate_dedup_key(candidate: PaperCandidate) -> str:
    """One paper should appear once, no matter which provider or URL found it."""
    identifier = (candidate.id or "").casefold()
    if identifier.startswith("arxiv:"):
        return "arxiv:" + re.sub(r"v\d+$", "", identifier.removeprefix("arxiv:"))
    doi = identifier.removeprefix("doi:") if identifier.startswith("doi:") else ""
    for url in (candidate.landing_url, candidate.pdf_url):
        arxiv_id = arxiv_id_from_result_url(url or "")
        if arxiv_id:
            return "arxiv:" + re.sub(r"v\d+$", "", arxiv_id)
        doi = doi or doi_from_url(url or "")
    arxiv_doi = re.match(r"10\.48550/arxiv\.(.+)$", doi)
    if arxiv_doi:
        return "arxiv:" + re.sub(r"v\d+$", "", arxiv_doi.group(1))
    if doi:
        return f"doi:{doi}"
    return normalize_title(candidate.title) or identifier or "unknown"


def better_candidate(candidate: PaperCandidate, other: PaperCandidate) -> bool:
    if abs(candidate.score - other.score) > 0.02:
        return candidate.score > other.score
    if bool(candidate.pdf_url) != bool(other.pdf_url):
        return bool(candidate.pdf_url)
    if is_preferred_paper_url(candidate.landing_url) != is_preferred_paper_url(other.landing_url):
        return is_preferred_paper_url(candidate.landing_url)
    return candidate.score > other.score


def merge_candidates(candidates: Iterable[PaperCandidate], limit: int) -> list[PaperCandidate]:
    merged: dict[str, PaperCandidate] = {}
    for candidate in candidates:
        if candidate.score < 0.35:
            continue
        key = candidate_dedup_key(candidate)
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            continue
        winner, loser = (
            (candidate, existing) if better_candidate(candidate, existing) else (existing, candidate)
        )
        winner.authors = winner.authors or loser.authors
        winner.year = winner.year or loser.year
        winner.venue = winner.venue or loser.venue
        winner.landing_url = winner.landing_url or loser.landing_url
        winner.pdf_url = winner.pdf_url or loser.pdf_url
        winner.cached_name = winner.cached_name or loser.cached_name
        merged[key] = winner
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
    providers: Iterable[Callable[[str, int], ProviderResult]] | None = None,
    cache_path: Path | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    active_providers = tuple(DEFAULT_PROVIDERS if providers is None else providers)
    query = " ".join(str(query).split())
    direct = direct_arxiv_candidate(query)
    if direct:
        return {
            "results": [direct.payload()],
            "autoSelect": True,
            "manualRequired": False,
            "providerErrors": {},
            "searchMode": "arxiv-id",
            "cached": False,
        }

    local_results = merge_candidates(cached_candidates(query, records), limit)
    if should_auto_select(local_results):
        return {
            "results": [candidate.payload() for candidate in local_results],
            "autoSelect": True,
            "manualRequired": False,
            "providerErrors": {},
            "searchMode": "local-cache",
            "cached": False,
        }

    ttl_hours = env_int("PAPER_SEARCH_CACHE_TTL_HOURS", DEFAULT_TTL_HOURS)
    if use_cache and cache_path is not None:
        cached_payload = load_search_cache(cache_path, query, limit, ttl_hours=ttl_hours)
        if cached_payload:
            return {**cached_payload, "cached": True}

    candidates = list(local_results)
    errors: dict[str, str] = {}
    used: list[str] = []

    provider_candidates, provider_errors, provider_used = run_providers(query, limit, active_providers)
    candidates.extend(provider_candidates)
    errors.update(provider_errors)
    used.extend(provider_used)

    if api_key.strip():
        try:
            sources = list(searcher(query, api_key, limit))
        except Exception as error:  # noqa: BLE001 - APIs remain useful when the LLM fails.
            errors["DeepSeek Web Search"] = str(error)[:240]
            sources = []
        llm_hits = 0
        for rank, source in enumerate(sources):
            candidate = candidate_from_source(query, source, rank)
            if candidate:
                candidates.append(candidate)
                llm_hits += 1
        if llm_hits:
            used.append("DeepSeek Web Search")
    else:
        errors["DeepSeek Web Search"] = "DEEPSEEK_API_KEY is not configured; academic APIs only."

    results = merge_candidates(candidates, limit)
    verify_candidate_pdfs(results, env_int("PAPER_SEARCH_VERIFY_LIMIT", 6))
    if not results:
        search_mode = "local-cache" if local_results else "manual"
    elif used == ["DeepSeek Web Search"]:
        search_mode = "deepseek-native"
    elif "DeepSeek Web Search" in used:
        search_mode = "deepseek-native+academic-apis"
    else:
        search_mode = "academic-apis"
    payload = {
        "results": [candidate.payload() for candidate in results],
        "autoSelect": should_auto_select(results),
        "manualRequired": not results,
        "providerErrors": errors,
        "searchMode": search_mode,
    }
    if use_cache and cache_path is not None and payload["results"]:
        save_search_cache(cache_path, query, limit, payload)
    return {**payload, "cached": False}
