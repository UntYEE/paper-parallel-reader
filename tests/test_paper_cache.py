"""Cache robustness tests: index durability, staleness flags, storage and deletion."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import server
from backend.paper_qa import delete_paper_index, index_translation
from backend.server import app


SAMPLE_TRANSLATION = {
    "title": "Widget Alignment",
    "generatorVersion": "test-version",
    "sections": [
        {
            "id": "abstract",
            "title": "Abstract",
            "pageStart": 1,
            "pageEnd": 1,
            "paragraphs": [
                {
                    "id": "abstract-p1",
                    "page": 1,
                    "anchor": "Abstract, paragraph 1",
                    "sourceText": "Widgets align better with noise modelling.",
                    "status": "translated",
                    "translation": "加入噪声建模后对齐更好。",
                }
            ],
        }
    ],
}


class TempDataMixin:
    def setUp(self) -> None:  # noqa: D102 - unittest convention.
        self._temp = tempfile.TemporaryDirectory()
        # macOS temp directories are symlinked (/var -> /private/var); resolve to match the
        # paths the server derives internally.
        self.root = Path(self._temp.name).resolve()
        (self.root / "translations").mkdir(parents=True, exist_ok=True)
        (self.root / "latex_sources").mkdir(parents=True, exist_ok=True)
        (self.root / "paper-assets").mkdir(parents=True, exist_ok=True)
        (self.root / "checkpoints").mkdir(parents=True, exist_ok=True)
        self.patches = [
            patch.object(server, "DATA_ROOT", self.root),
            patch.object(server, "PAPERS_DIR", self.root),
            patch.object(server, "SOURCES_DIR", self.root / "latex_sources"),
            patch.object(server, "DEFAULT_OUTPUT_DIR", self.root / "translations"),
            patch.object(server, "ASSET_DIR", self.root / "paper-assets"),
            patch.object(server, "INDEX_PATH", self.root / "paper_index.json"),
            patch.object(server, "INDEX_BACKUP_PATH", self.root / "paper_index.json.bak"),
            patch.object(server, "QA_DB_PATH", self.root / "paper_qa.sqlite3"),
            patch.object(server, "SEARCH_CACHE_PATH", self.root / "search_cache.sqlite3"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:  # noqa: D102 - unittest convention.
        for item in reversed(self.patches):
            item.stop()
        self._temp.cleanup()


class IndexDurabilityTests(TempDataMixin, unittest.TestCase):
    def test_corrupt_index_recovers_from_backup(self) -> None:
        server.save_index({"papers": {"arxiv:1": {"paperId": "arxiv:1"}}})
        server.save_index({"papers": {"arxiv:1": {"paperId": "arxiv:1"}, "arxiv:2": {"paperId": "arxiv:2"}}})
        self.assertTrue(server.INDEX_BACKUP_PATH.exists())
        server.INDEX_PATH.write_text("{not json", encoding="utf-8")

        recovered = server.load_index()

        self.assertIn("arxiv:1", recovered["papers"])
        # The primary index is rewritten so the next start does not need the backup.
        self.assertEqual(recovered, server.load_index())
        self.assertNotIn("{not json", server.INDEX_PATH.read_text(encoding="utf-8"))

    def test_structure_staleness_tracks_generator_version(self) -> None:
        (self.root / "translations" / "widget.json").write_text("{}", encoding="utf-8")
        server.upsert_paper_index(
            "arxiv:1",
            translationName="widget.json",
            generatorVersion="old-version",
        )
        self.assertTrue(server.structure_is_stale("arxiv:1"))
        server.upsert_paper_index("arxiv:1", generatorVersion=server.GENERATOR_VERSION)
        self.assertFalse(server.structure_is_stale("arxiv:1"))
        server.upsert_paper_index("arxiv:2", generatorVersion="old-version")
        self.assertFalse(server.structure_is_stale("arxiv:2"))  # no translation indexed
        self.assertFalse(server.structure_is_stale(""))

    def test_same_basename_from_different_urls_does_not_reuse_cache(self) -> None:
        first = server.paper_filename_for_url("https://example.org/papers/manuscript.pdf")
        second = server.paper_filename_for_url("https://other.example.org/papers/manuscript.pdf")
        self.assertEqual(first, second)
        server.atomic_write_bytes(server.paper_path(first), b"%PDF-1.7")
        server.write_paper_meta(server.paper_path(first), "https://example.org/papers/manuscript.pdf")
        second = server.paper_filename_for_url("https://other.example.org/papers/manuscript.pdf")
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith(".pdf"))


class StorageEndpointTests(TempDataMixin, unittest.TestCase):
    def test_storage_reports_categories_and_paper_count(self) -> None:
        server.atomic_write_bytes(self.root / "arxiv-2503.09516.pdf", b"%PDF-1.7" + b"0" * 512)
        (self.root / "translations" / "search-r1.json").write_text("{}", encoding="utf-8")
        client = TestClient(app)
        response = client.get("/api/storage")
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(1, payload["paperCount"])
        names = {item["name"] for item in payload["categories"]}
        self.assertIn("pdfs", names)
        self.assertIn("translations", names)
        self.assertGreater(payload["totalBytes"], 0)
        self.assertIn("searchCache", payload)

    def test_delete_paper_removes_files_index_and_qa_rows(self) -> None:
        pdf_path = server.paper_path("arxiv-2503.09516.pdf")
        server.atomic_write_bytes(pdf_path, b"%PDF-1.7" + b"0" * 128)
        server.write_paper_meta(pdf_path, "https://arxiv.org/pdf/2503.09516")
        translation_path = self.root / "translations" / "search-r1.json"
        translation_path.write_text(
            __import__("json").dumps(SAMPLE_TRANSLATION), encoding="utf-8"
        )
        server.upsert_paper_index(
            "arxiv:2503.09516",
            pdfName=pdf_path.name,
            pdfPath=server.data_relative(pdf_path),
            translationName=translation_path.name,
            translationPath=server.data_relative(translation_path),
            generatorVersion=server.GENERATOR_VERSION,
        )
        assets_dir = self.root / "paper-assets" / "search-r1"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / "figure-1.png").write_bytes(b"png")
        checkpoint = self.root / "checkpoints" / "search-r1-abc.json"
        checkpoint.write_text("{}", encoding="utf-8")
        index_translation(server.QA_DB_PATH, "arxiv:2503.09516", translation_path)

        client = TestClient(app)
        response = client.delete("/api/papers/arxiv:2503.09516")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertGreater(payload["freedBytes"], 0)
        self.assertFalse(pdf_path.exists())
        self.assertFalse(translation_path.exists())
        self.assertFalse(assets_dir.exists())
        self.assertFalse(checkpoint.exists())
        self.assertNotIn("arxiv:2503.09516", server.load_index()["papers"])
        # The endpoint already removed the QA rows, so a second cleanup finds nothing.
        self.assertEqual({"chunks": 0, "messages": 0}, delete_paper_index(server.QA_DB_PATH, "arxiv:2503.09516"))

    def test_delete_paper_index_reports_removed_chunks(self) -> None:
        translation_path = self.root / "translations" / "search-r1.json"
        translation_path.write_text(__import__("json").dumps(SAMPLE_TRANSLATION), encoding="utf-8")
        index_translation(server.QA_DB_PATH, "arxiv:2503.09516", translation_path)
        removed = delete_paper_index(server.QA_DB_PATH, "arxiv:2503.09516")
        self.assertEqual(1, removed["chunks"])
        self.assertFalse((self.root / "paper_qa.sqlite3").stat().st_size == 0)

    def test_delete_unknown_paper_returns_404(self) -> None:
        client = TestClient(app)
        response = client.delete("/api/papers/arxiv:does-not-exist")
        self.assertEqual(404, response.status_code)

    def test_search_cache_clear_endpoint(self) -> None:
        from backend.search_cache import save as save_search_cache

        save_search_cache(server.SEARCH_CACHE_PATH, "widget", 6, {"results": [{"id": "x"}]})
        client = TestClient(app)
        response = client.post("/api/search-cache/clear")
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json()["removed"])

    def test_search_endpoint_uses_the_search_cache(self) -> None:
        from backend.paper_search import PaperCandidate, ProviderResult, title_score

        def fake_provider(query: str, limit: int) -> ProviderResult:
            title = f"{query.title()} explained"
            return ProviderResult(
                "Fake",
                [
                    PaperCandidate(
                        id="arxiv:2503.09516",
                        title=title,
                        authors=[],
                        year=2025,
                        venue="arXiv",
                        source="Fake",
                        landing_url="https://arxiv.org/abs/2503.09516",
                        pdf_url="",
                        score=title_score(query, title),
                    )
                ],
            )

        client = TestClient(app)
        with patch("backend.paper_search.DEFAULT_PROVIDERS", (fake_provider,)):
            first = client.post("/api/search-papers", json={"query": "widget alignment", "limit": 3}).json()
            second = client.post("/api/search-papers", json={"query": "widget alignment", "limit": 3}).json()
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(1, len(second["results"]))


if __name__ == "__main__":
    unittest.main()
