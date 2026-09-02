import json
import unittest
from unittest.mock import MagicMock, patch

from backend.paper_search import (
    PaperCandidate,
    SearchSource,
    candidate_from_source,
    deepseek_native_search,
    discover_papers,
    is_allowed_paper_url,
    merge_candidates,
    title_score,
    verify_pdf_url,
)


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
        self.assertFalse(is_allowed_paper_url("https://arxiv.org.attacker.test/paper.pdf"))
        self.assertFalse(is_allowed_paper_url("http://arxiv.org/pdf/2503.09516"))

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
        with patch("urllib.request.urlopen", return_value=valid):
            self.assertEqual("https://arxiv.org/pdf/2503.09516", verify_pdf_url(valid.url))
        wrong_type = FakeResponse(b"%PDF-1.7", valid.url, "text/html")
        with patch("urllib.request.urlopen", return_value=wrong_type):
            self.assertEqual("", verify_pdf_url(valid.url))
        wrong_magic = FakeResponse(b"<html", valid.url, "application/pdf")
        with patch("urllib.request.urlopen", return_value=wrong_magic):
            self.assertEqual("", verify_pdf_url(valid.url))

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
            result = discover_papers("Search-R1 Training LLMs", [], api_key="secret", searcher=lambda *_args: sources)
        self.assertEqual(1, len(result["results"]))
        self.assertEqual("deepseek-native", result["searchMode"])

    def test_search_failure_requires_manual_input(self) -> None:
        result = discover_papers(
            "not found",
            [],
            api_key="secret",
            searcher=lambda *_args: (_ for _ in ()).throw(TimeoutError("offline")),
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


if __name__ == "__main__":
    unittest.main()
