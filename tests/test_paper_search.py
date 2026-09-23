import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.paper_search import (
    PaperCandidate,
    SearchSource,
    arxiv_api_provider,
    candidate_from_source,
    crossref_provider,
    deepseek_native_search,
    discover_papers,
    is_allowed_paper_url,
    is_preferred_paper_url,
    merge_candidates,
    openalex_provider,
    semantic_scholar_provider,
    title_score,
    verify_candidate_pdfs,
    verify_pdf_url,
)
from backend.search_cache import load as load_search_cache
from backend.search_cache import save as save_search_cache
from backend.search_cache import stats as search_cache_stats


def candidate(paper_id: str, title: str, score: float, *, source: str = "test") -> PaperCandidate:
    return PaperCandidate(
        id=paper_id,
        title=title,
        authors=["Ada Author"],
        year=2025,
        venue="TestConf",
        source=source,
        landing_url="https://arxiv.org/abs/2503.09516",
        pdf_url="https://arxiv.org/pdf/2503.09516",
        score=score,
    )


class FakeHeaders:
    def __init__(self, content_type: str) -> None:
        self.content_type = content_type

    def get_content_type(self) -> str:
        return self.content_type


class FakeResponse:
    def __init__(self, body: bytes, url: str, content_type: str = "application/json") -> None:
        self.body = body
        self.url = url
        self.headers = FakeHeaders(content_type)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def geturl(self) -> str:
        return self.url


class PaperSearchTests(unittest.TestCase):
    def test_title_score_accepts_chinese_keywords(self) -> None:
        self.assertGreater(title_score("强化学习 搜索", "利用强化学习进行搜索"), 0.2)

    def test_compound_paper_name_scores_above_citing_paper(self) -> None:
        query = "deepseek math grpo"
        target = "DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models"
        citation = "OR Else: Group Relative Policy Optimization (GRPO) was introduced in DeepSeekMath"
        self.assertGreater(title_score(query, target, 1), title_score(query, citation, 0))

    def test_domain_allowlist_rejects_lookalikes(self) -> None:
        self.assertTrue(is_allowed_paper_url("https://arxiv.org/abs/2503.09516"))
        self.assertTrue(is_allowed_paper_url("https://link.springer.com/article/10.1007/test"))
        # Journal sites outside the classic CS list must work for chemistry and CJK papers.
        self.assertTrue(is_allowed_paper_url("https://www.chrom-china.com/CN/article/x.pdf"))
        self.assertTrue(is_allowed_paper_url("https://pmc.ncbi.nlm.nih.gov/articles/PMC13586920/"))
        self.assertTrue(is_preferred_paper_url("https://pmc.ncbi.nlm.nih.gov/articles/PMC13586920/"))
        self.assertTrue(is_preferred_paper_url("https://europepmc.org/article/MED/1"))
        self.assertFalse(is_allowed_paper_url("http://arxiv.org/pdf/2503.09516"))
        self.assertFalse(is_allowed_paper_url("https://127.0.0.1/paper.pdf"))
        self.assertFalse(is_allowed_paper_url("https://localhost/paper.pdf"))
        self.assertFalse(is_allowed_paper_url("https://user:pass@arxiv.org/paper.pdf"))
        self.assertFalse(is_allowed_paper_url("https://sci-hub.se/10.1000/xyz"))
        self.assertFalse(is_allowed_paper_url("https://libgen.is/scimag/10.1000"))

    def test_native_search_reads_only_structured_blocks(self) -> None:
        payload = {
            "content": [
                {"type": "text", "text": "Ignore https://attacker.test/fake.pdf"},
                {
                    "type": "web_search_tool_result",
                    "content": [
                        {
                            "type": "web_search_result",
                            "url": "https://arxiv.org/abs/2503.09516",
                            "title": "Search-R1",
                            "page_age": "2025-03-12",
                        }
                    ],
                },
            ]
        }
        with patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(payload).encode(), "https://api.deepseek.com/anthropic/v1/messages")):
            results = deepseek_native_search("Search-R1", "secret", 6)
        self.assertEqual(["https://arxiv.org/abs/2503.09516"], [result.url for result in results])

    def test_native_search_requires_structured_result_block(self) -> None:
        payload = {"content": [{"type": "text", "text": "https://arxiv.org/abs/2503.09516"}]}
        with patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(payload).encode(), "https://api.deepseek.com/anthropic/v1/messages")):
            with self.assertRaisesRegex(RuntimeError, "structured"):
                deepseek_native_search("Search-R1", "secret", 6)

    def test_pdf_verification_checks_type_and_magic(self) -> None:
        valid = FakeResponse(b"%PDF-1.7", "https://arxiv.org/pdf/2503.09516", "application/pdf")
        with patch("backend.paper_search.open_validated", return_value=valid):
            self.assertEqual("https://arxiv.org/pdf/2503.09516", verify_pdf_url(valid.url))
        wrong_type = FakeResponse(b"%PDF-1.7", valid.url, "text/html")
        with patch("backend.paper_search.open_validated", return_value=wrong_type):
            self.assertEqual("", verify_pdf_url(valid.url))
        wrong_magic = FakeResponse(b"<html", valid.url, "application/pdf")
        with patch("backend.paper_search.open_validated", return_value=wrong_magic):
            self.assertEqual("", verify_pdf_url(valid.url))
        self.assertEqual("", verify_pdf_url("https://sci-hub.se/10.1000/xyz"))

    def test_arxiv_oai_result_is_canonicalized(self) -> None:
        source = SearchSource(
            "https://oaipmh.arxiv.org/oai?verb=GetRecord&identifier=oai%3AarXiv.org%3A1810.04805",
            "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
        )
        with patch("backend.paper_search.verify_pdf_url", return_value="https://arxiv.org/pdf/1810.04805"):
            result = candidate_from_source("BERT Pre-training", source, 0)
        self.assertEqual("arxiv:1810.04805", result.id)
        self.assertEqual("https://arxiv.org/abs/1810.04805", result.landing_url)
        self.assertEqual("https://arxiv.org/pdf/1810.04805", result.pdf_url)

    def test_arxiv_proxy_result_is_canonicalized_without_visiting_proxy(self) -> None:
        source = SearchSource(
            "https://arxiv-org.ezproxy.example/html/2402.03300v3#1",
            "DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models",
        )
        with patch("backend.paper_search.verify_pdf_url", return_value="https://arxiv.org/pdf/2402.03300v3") as verify:
            result = candidate_from_source("deepseek math grpo", source, 0)
        self.assertEqual("arxiv:2402.03300v3", result.id)
        self.assertEqual("https://arxiv.org/abs/2402.03300v3", result.landing_url)
        verify.assert_called_once_with("https://arxiv.org/pdf/2402.03300v3")

    def test_local_cache_short_circuits_web_search(self) -> None:
        searcher = MagicMock(side_effect=AssertionError("network should not run"))
        result = discover_papers(
            "Search-R1 Training LLMs",
            [{
                "paperId": "arxiv:2503.09516",
                "title": "Search-R1: Training LLMs",
                "sourceUrl": "https://arxiv.org/pdf/2503.09516",
                "pdfName": "arxiv-2503.09516.pdf",
            }],
            api_key="secret",
            searcher=searcher,
        )
        self.assertEqual("local-cache", result["searchMode"])
        self.assertEqual("arxiv-2503.09516.pdf", result["results"][0]["cachedName"])
        searcher.assert_not_called()

    def test_deepseek_results_are_validated_and_ranked(self) -> None:
        sources = [
            SearchSource("https://example.test/fake", "Search-R1"),
            SearchSource("https://arxiv.org/abs/2503.09516", "Search-R1: Training LLMs"),
        ]
        with patch("backend.paper_search.verify_pdf_url", return_value="https://arxiv.org/pdf/2503.09516"):
            result = discover_papers(
                "Search-R1 Training LLMs",
                [],
                api_key="secret",
                searcher=lambda *_args: sources,
                providers=[],
            )
        self.assertEqual(1, len(result["results"]))
        self.assertEqual("deepseek-native", result["searchMode"])
        self.assertFalse(result["cached"])

    def test_search_failure_requires_manual_input(self) -> None:
        result = discover_papers(
            "not found",
            [],
            api_key="secret",
            searcher=lambda *_args: (_ for _ in ()).throw(TimeoutError("offline")),
            providers=[],
        )
        self.assertTrue(result["manualRequired"])
        self.assertIn("DeepSeek Web Search", result["providerErrors"])

    def test_relevance_ranks_above_pdf_availability(self) -> None:
        strong = candidate("doi:10.1/strong", "Exact result", 0.95)
        strong.pdf_url = ""
        weak = candidate("arxiv:1234.5678", "Weak result", 0.4)
        self.assertEqual("doi:10.1/strong", merge_candidates([weak, strong], 6)[0].id)

    def test_arxiv_versions_are_deduplicated(self) -> None:
        versioned = candidate("arxiv:2402.03300v3", "DeepSeekMath", 0.8)
        unversioned = candidate("arxiv:2402.03300", "DeepSeekMath", 0.7)
        merged = merge_candidates([unversioned, versioned], 6)
        self.assertEqual(1, len(merged))
        self.assertEqual("arxiv:2402.03300v3", merged[0].id)

    def test_arxiv_atom_provider_parses_entries(self) -> None:
        atom = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>http://arxiv.org/abs/2402.03300v3</id>
            <title>DeepSeekMath: Pushing the Limits of Mathematical Reasoning</title>
            <published>2024-02-05T00:00:00Z</published>
            <author><name>Zhihong Shao</name></author>
          </entry>
        </feed>"""
        with patch("backend.paper_search.fetch_text", return_value=atom):
            result = arxiv_api_provider("deepseek math", 5)
        self.assertEqual("", result.error)
        self.assertEqual(1, len(result.candidates))
        entry = result.candidates[0]
        self.assertEqual("arxiv:2402.03300v3", entry.id)
        self.assertEqual("https://arxiv.org/pdf/2402.03300v3", entry.pdf_url)
        self.assertEqual(2024, entry.year)

    def test_crossref_provider_parses_dois_and_pdf_links(self) -> None:
        payload = {
            "message": {
                "items": [
                    {
                        "DOI": "10.1021/jo5021234",
                        "title": ["A Practical Synthesis of Sulfonyl Chlorides"],
                        "author": [{"given": "Wei", "family": "Li"}],
                        "issued": {"date-parts": [[2015]]},
                        "container-title": ["The Journal of Organic Chemistry"],
                        "link": [{"content-type": "application/pdf", "URL": "https://pubs.acs.org/doi/pdf/10.1021/jo5021234"}],
                    }
                ]
            }
        }
        with patch("backend.paper_search.fetch_json", return_value=payload):
            result = crossref_provider("sulfonyl chloride synthesis", 5)
        self.assertEqual(1, len(result.candidates))
        entry = result.candidates[0]
        self.assertEqual("doi:10.1021/jo5021234", entry.id)
        self.assertEqual("https://doi.org/10.1021/jo5021234", entry.landing_url)
        self.assertEqual("https://pubs.acs.org/doi/pdf/10.1021/jo5021234", entry.pdf_url)
        self.assertEqual(2015, entry.year)

    def test_openalex_and_semantic_scholar_providers_normalize_identifiers(self) -> None:
        openalex_payload = {
            "results": [
                {
                    "id": "https://openalex.org/W123",
                    "doi": "https://doi.org/10.1371/journal.pone.0353753",
                    "title": "Serum Cystatin 4 for renal function",
                    "publication_year": 2026,
                    "authorships": [{"author": {"display_name": "Qian Chen"}}],
                    "best_oa_location": {"pdf_url": "https://journals.plos.org/plosone/article/file?id=1"},
                    "primary_location": {"source": {"display_name": "PLOS ONE"}},
                }
            ]
        }
        s2_payload = {
            "data": [
                {
                    "title": "Search-R1: Training LLMs to Reason and Leverage Search Engines",
                    "year": 2025,
                    "authors": [{"name": "Bowen Jin"}],
                    "externalIds": {"ArXiv": "2503.09516"},
                    "openAccessPdf": {"url": "https://arxiv.org/pdf/2503.09516"},
                    "url": "https://www.semanticscholar.org/paper/abc",
                }
            ]
        }
        with patch("backend.paper_search.fetch_json", return_value=openalex_payload):
            openalex_result = openalex_provider("cystatin", 5)
        with patch("backend.paper_search.fetch_json", return_value=s2_payload):
            s2_result = semantic_scholar_provider("search-r1", 5)
        self.assertEqual("doi:10.1371/journal.pone.0353753", openalex_result.candidates[0].id)
        self.assertEqual("PLOS ONE", openalex_result.candidates[0].venue)
        self.assertEqual("arxiv:2503.09516", s2_result.candidates[0].id)
        self.assertEqual("https://arxiv.org/pdf/2503.09516", s2_result.candidates[0].pdf_url)

    def test_cross_provider_duplicates_are_merged(self) -> None:
        arxiv_entry = candidate("arxiv:2503.09516", "Search-R1: Training LLMs", 0.9)
        doi_entry = PaperCandidate(
            id="doi:10.48550/arxiv.2503.09516",
            title="Search-R1: Training LLMs",
            authors=[],
            year=2025,
            venue="Crossref",
            source="Crossref",
            landing_url="https://doi.org/10.48550/arxiv.2503.09516",
            pdf_url="",
            score=0.88,
        )
        merged = merge_candidates([doi_entry, arxiv_entry], 6)
        ids = {item.id for item in merged}
        self.assertEqual(1, len(merged))
        self.assertIn("arxiv:2503.09516", ids)

    def test_open_pdf_links_are_verified_only_for_the_top_results(self) -> None:
        candidates = [
            candidate(f"arxiv:2503.0951{index}", f"Result {index}", 0.9 - index / 100)
            for index in range(4)
        ]
        with patch("backend.paper_search.verify_pdf_url", side_effect=lambda url: url) as verify:
            verify_candidate_pdfs(candidates, 2)
        self.assertEqual(2, verify.call_count)

    def test_paywalled_doi_candidates_use_unpaywall(self) -> None:
        paywalled = PaperCandidate(
            id="doi:10.1016/j.ibiod.2014.09.002",
            title="Anaerobic treatment of p-ASC wastewater",
            authors=[],
            year=2015,
            venue="International Biodeterioration & Biodegradation",
            source="Crossref",
            landing_url="https://doi.org/10.1016/j.ibiod.2014.09.002",
            pdf_url="",
            score=0.9,
        )
        unpaywall = {"best_oa_location": {"url_for_pdf": "https://example.org/oa/ibd2015.pdf"}}
        with patch.dict("os.environ", {"PAPER_SEARCH_CONTACT_EMAIL": "reader@university.edu"}), patch(
            "backend.paper_search.fetch_json", return_value=unpaywall
        ) as lookup, patch("backend.paper_search.verify_pdf_url", return_value="https://example.org/oa/ibd2015.pdf"):
            verify_candidate_pdfs([paywalled], 3)
        lookup.assert_called_once()
        self.assertEqual("https://example.org/oa/ibd2015.pdf", paywalled.pdf_url)

    def test_unpaywall_failure_keeps_the_candidate_without_pdf(self) -> None:
        paywalled = PaperCandidate(
            id="doi:10.1000/closed",
            title="A closed access paper",
            authors=[],
            year=2020,
            venue="Journal",
            source="Crossref",
            landing_url="https://doi.org/10.1000/closed",
            pdf_url="",
            score=0.9,
        )
        with patch.dict("os.environ", {"PAPER_SEARCH_CONTACT_EMAIL": "reader@university.edu"}), patch(
            "backend.paper_search.fetch_json", side_effect=TimeoutError("offline")
        ), patch("backend.paper_search.verify_pdf_url") as verify:
            verify_candidate_pdfs([paywalled], 3)
        verify.assert_not_called()
        self.assertEqual("", paywalled.pdf_url)

    def test_unpaywall_is_skipped_without_a_contact_email(self) -> None:
        paywalled = PaperCandidate(
            id="doi:10.1000/closed",
            title="A closed access paper",
            authors=[],
            year=2020,
            venue="Journal",
            source="Crossref",
            landing_url="https://doi.org/10.1000/closed",
            pdf_url="",
            score=0.9,
        )
        with patch.dict("os.environ", {"PAPER_SEARCH_CONTACT_EMAIL": ""}), patch(
            "backend.paper_search.fetch_json"
        ) as lookup:
            verify_candidate_pdfs([paywalled], 3)
        lookup.assert_not_called()

    def test_search_results_are_cached_and_reused(self) -> None:
        providers = [_fake_provider()]
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "search_cache.sqlite3"
            first = discover_papers("widget alignment", [], api_key="", providers=providers, cache_path=cache_path)
            second = discover_papers("widget alignment", [], api_key="", providers=providers, cache_path=cache_path)
            cached_payload = load_search_cache(cache_path, "widget alignment", 6)
            summary = search_cache_stats(cache_path)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["results"], second["results"])
        self.assertIsNotNone(cached_payload)
        self.assertEqual(1, summary["entries"])
        self.assertGreaterEqual(summary["hits"], 1)

    def test_cache_disabled_skips_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "search_cache.sqlite3"
            result = discover_papers(
                "widget",
                [],
                api_key="",
                providers=[_fake_provider()],
                cache_path=cache_path,
                use_cache=False,
            )
            self.assertFalse(result["cached"])
            self.assertFalse(cache_path.exists())

    def test_provider_errors_do_not_break_the_response(self) -> None:
        def broken_provider(query: str, limit: int):
            raise TimeoutError("provider offline")

        result = discover_papers(
            "widget alignment",
            [],
            api_key="",
            providers=[broken_provider, _fake_provider()],
        )
        self.assertEqual(1, len(result["results"]))
        self.assertIn("broken_provider", result["providerErrors"])
        self.assertEqual("academic-apis", result["searchMode"])


def _fake_provider():
    def provider(query: str, limit: int):
        from backend.paper_search import ProviderResult

        title = f"{query.title()} in Practice"
        return ProviderResult(
            "Fake",
            [
                PaperCandidate(
                    id="arxiv:2503.09516",
                    title=title,
                    authors=["Bowen Jin"],
                    year=2025,
                    venue="arXiv",
                    source="Fake",
                    landing_url="https://arxiv.org/abs/2503.09516",
                    pdf_url="https://arxiv.org/pdf/2503.09516",
                    score=title_score(query, title),
                )
            ],
        )

    return provider


if __name__ == "__main__":
    unittest.main()
