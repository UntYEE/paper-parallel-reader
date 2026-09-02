import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.paper_qa import (
    CitationValidationError,
    answer_question,
    get_history,
    index_status,
    index_translation,
    retrieve_evidence,
    validate_qa_response,
)


class FakeCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, content: str) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(content))


class PaperQATests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = root / "paper_qa.sqlite3"
        self.translation_path = root / "translation.json"
        self.translation_path.write_text(
            json.dumps(
                {
                    "title": "Search Test",
                    "sections": [
                        {
                            "id": "method",
                            "title": "Method",
                            "paragraphs": [
                                {
                                    "id": "method-p1",
                                    "page": 3,
                                    "status": "translated",
                                    "sourceText": "The model learns to call a search engine with reinforcement learning.",
                                    "translation": "该模型通过强化学习来调用搜索引擎。",
                                },
                                {
                                    "id": "method-p2",
                                    "page": 4,
                                    "status": "translated",
                                    "sourceText": "The reward is based on final answer correctness.",
                                    "translation": "奖励取决于最终答案是否正确。",
                                },
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_index_and_bilingual_retrieval(self) -> None:
        indexed = index_translation(self.db_path, "arxiv:test", self.translation_path)
        self.assertEqual(2, indexed["chunks"])
        self.assertTrue(index_status(self.db_path, "arxiv:test", self.translation_path)["ready"])
        evidence = retrieve_evidence(self.db_path, "arxiv:test", "怎样用强化学习调用搜索引擎？", limit=1)
        self.assertEqual("method-p1", evidence[0]["paragraphId"])

    def test_unknown_model_citation_is_rejected(self) -> None:
        evidence = [{"evidenceId": "S1"}]
        with self.assertRaises(CitationValidationError):
            validate_qa_response(
                {"answerMarkdown": "Unsupported [S9]", "usedEvidenceIds": ["S9"], "insufficientEvidence": False},
                evidence,
            )

    def test_answer_is_saved_with_validated_citation(self) -> None:
        index_translation(self.db_path, "arxiv:test", self.translation_path)
        client = FakeClient(
            json.dumps(
                {"answerMarkdown": "模型使用强化学习。[S1]", "usedEvidenceIds": ["S1"], "insufficientEvidence": False},
                ensure_ascii=False,
            )
        )
        answer = answer_question(
            self.db_path,
            "arxiv:test",
            "session-1",
            "模型如何使用搜索？",
            client,
            "fake-model",
            selected_paragraph_id="method-p1",
        )
        self.assertEqual("method-p1", answer["citations"][0]["paragraphId"])
        history = get_history(self.db_path, "arxiv:test", "session-1")
        self.assertEqual(["user", "assistant"], [item["role"] for item in history])


if __name__ == "__main__":
    unittest.main()
