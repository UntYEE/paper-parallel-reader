import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.generate_translation_json import (
    FormulaTokenError,
    NonRetryableTranslationError,
    Paragraph,
    PdfTextBlock,
    ReferenceTokenError,
    TranslationRun,
    associate_label_targets,
    build_output,
    chunk_units,
    extraction_is_usable,
    extract_latex_pages,
    formula_tokens,
    latex_to_protected_document,
    latex_to_protected_text,
    low_quality_page_numbers,
    noise_match_key,
    order_blocks_reading_order,
    parse_bibtex_entries,
    pdf_structure_hints,
    reference_tokens,
    repeated_furniture_lines,
    restore_all_tokens,
    restore_math_tokens,
    segment_document,
    translate_chunk,
    translate_chunk_with_harness,
    translate_all_batches,
    validate_translation_response,
)


def translation_response(items):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=__import__("json").dumps({"items": items})))],
    )


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.completions = FakeCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)


class FakeStatusError(RuntimeError):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class TranslationWorkflowTests(unittest.TestCase):
    def test_dynamic_chunking_limits_formulas_and_structured_batch_size(self) -> None:
        formula_units = [
            Paragraph(
                id=f"p{index}",
                page=1,
                anchor="math",
                source=f"Text @@MATH_{index:04d}@@ @@MATH_{index + 20:04d}@@ @@MATH_{index + 40:04d}@@",
            )
            for index in range(1, 7)
        ]
        formula_chunks = chunk_units(formula_units, 4000)
        self.assertGreater(len(formula_chunks), 1)
        self.assertTrue(all(sum(len(formula_tokens(item.source)) for item in chunk) <= 8 for chunk in formula_chunks))

        structured = [Paragraph(id=f"s{index}", page=1, anchor="table", source="cell") for index in range(11)]
        structured_chunks = chunk_units(structured, 2600, max_items=6, target_items=5)
        self.assertEqual([6, 5], [len(chunk) for chunk in structured_chunks])

    def test_shared_runner_interleaves_kinds_and_resumes_from_checkpoint(self) -> None:
        body_chunks = [
            [Paragraph(id="b1", page=1, anchor="body", source="Body one.")],
            [Paragraph(id="b2", page=1, anchor="body", source="Body two.")],
        ]
        structured_chunks = [
            [Paragraph(id="s1", page=1, anchor="table", source="Cell one.")],
            [Paragraph(id="s2", page=1, anchor="table", source="Cell two.")],
        ]
        responses = [
            translation_response([{"id": item_id, "status": "translated", "translation": "译文", "note": ""}])
            for item_id in ("b1", "s1", "b2", "s2")
        ]
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            client = FakeClient(responses)
            run = TranslationRun(client=client, fingerprint="paper-v1", checkpoint_path=checkpoint)
            body, structured = translate_all_batches(body_chunks, structured_chunks, "model", 0, 1, run)

            call_ids = [
                __import__("json").loads(call["messages"][1]["content"].split("\n\n", 1)[1])["paragraphs"][0]["id"]
                for call in client.completions.calls
            ]
            self.assertEqual(["b1", "s1", "b2", "s2"], call_ids)
            self.assertEqual({"b1", "b2"}, set(body))
            self.assertEqual({"s1", "s2"}, set(structured))
            self.assertTrue(checkpoint.exists())

            resumed_client = FakeClient([])
            resumed = TranslationRun(client=resumed_client, fingerprint="paper-v1", checkpoint_path=checkpoint)
            resumed_body, resumed_structured = translate_all_batches(
                body_chunks, structured_chunks, "model", 0, 2, resumed
            )
            self.assertEqual(0, len(resumed_client.completions.calls))
            self.assertEqual(4, resumed.completed_batches)
            self.assertEqual({"b1", "b2"}, set(resumed_body))
            self.assertEqual({"s1", "s2"}, set(resumed_structured))

    def test_translation_disables_thinking_and_limits_empty_response_retry(self) -> None:
        paragraph = Paragraph(id="p1", page=1, anchor="a", source="Readable prose.")
        empty = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])
        client = FakeClient([empty, empty, empty])

        with self.assertRaisesRegex(RuntimeError, "after 2 attempts"):
            translate_chunk(client, "deepseek-v4-flash", [paragraph], retries=5)

        self.assertEqual(2, len(client.completions.calls))
        self.assertEqual(
            {"thinking": {"type": "disabled"}},
            client.completions.calls[0]["extra_body"],
        )

    def test_non_retryable_api_error_fails_immediately(self) -> None:
        paragraph = Paragraph(id="p1", page=1, anchor="a", source="Readable prose.")
        client = FakeClient([FakeStatusError(402), translation_response([])])

        with self.assertRaises(NonRetryableTranslationError):
            translate_chunk(client, "deepseek-v4-flash", [paragraph], retries=5)

        self.assertEqual(1, len(client.completions.calls))

    def test_formula_failure_retries_only_the_problem_paragraph(self) -> None:
        chunk = [
            Paragraph(id="p1", page=1, anchor="a", source="First paragraph."),
            Paragraph(id="p2", page=1, anchor="b", source="Formula @@MATH_0001@@ then @@MATH_0002@@."),
            Paragraph(id="p3", page=1, anchor="c", source="Last paragraph."),
        ]
        outcomes = [
            translation_response(
                [
                    {"id": "p1", "status": "translated", "translation": "第一段。", "note": ""},
                    {
                        "id": "p2",
                        "status": "translated",
                        "translation": "公式仅保留 @@MATH_0002@@。",
                        "note": "",
                    },
                    {"id": "p3", "status": "translated", "translation": "最后一段。", "note": ""},
                ]
            ),
            translation_response(
                [{"id": "p1", "status": "translated", "translation": "第一段。", "note": ""}]
            ),
            translation_response(
                [
                    {
                        "id": "p2",
                        "status": "translated",
                        "translation": "公式仅保留 @@MATH_0002@@。",
                        "note": "",
                    }
                ]
            ),
            translation_response(
                [{"id": "p3", "status": "translated", "translation": "最后一段。", "note": ""}]
            ),
        ]
        client = FakeClient(outcomes)

        with patch("scripts.generate_translation_json.create_deepseek_client", return_value=client):
            translated = translate_chunk_with_harness("deepseek-v4-flash", chunk, retries=2)

        self.assertEqual(4, len(client.completions.calls))
        self.assertEqual("translated", translated["p1"]["status"])
        self.assertEqual("needs_formula_recovery", translated["p2"]["status"])
        self.assertEqual("translated", translated["p3"]["status"])

    def test_latex_dotted_input_and_simple_macro(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tex").write_text(
                r"""
\documentclass{article}
\newcommand{\modelname}{\textsc{Search-R1}}
\begin{document}
\begin{abstract}
This paper introduces \modelname for research.
\end{abstract}
\input{1.introduction}
\end{document}
""",
                encoding="utf-8",
            )
            (root / "1.introduction.tex").write_text(
                r"\section{Introduction}\n\modelname improves retrieval reasoning.",
                encoding="utf-8",
            )
            pages = extract_latex_pages(root)

        self.assertIn("Search-R1", pages[0][1])
        self.assertIn("Introduction", pages[0][1])
        self.assertIn("improves retrieval reasoning", pages[0][1])

    def test_translation_response_requires_every_id_and_status(self) -> None:
        chunk = [
            Paragraph(id="p1", page=1, anchor="a", source="Readable prose."),
            Paragraph(id="p2", page=1, anchor="b", source="Figure labels."),
        ]
        valid = validate_translation_response(
            {
                "items": [
                    {"id": "p1", "status": "translated", "translation": "可读正文", "note": ""},
                    {"id": "p2", "status": "skipped", "translation": "", "note": "figure"},
                ]
            },
            chunk,
        )
        self.assertEqual("translated", valid["p1"]["status"])
        self.assertEqual("", valid["p2"]["translation"])

        with self.assertRaisesRegex(ValueError, "omitted"):
            validate_translation_response({"items": [valid["p1"]]}, chunk)

    def test_latex_math_is_protected_and_restored(self) -> None:
        source = r"""
\begin{document}
\begin{abstract}
We use $P_\theta(y\mid x)$ below.
\end{abstract}
\section{Method}
The objective is
\begin{equation}
\mathcal{L}(\theta)=\mathbb{E}[R(x)].
\end{equation}
\end{document}
"""
        protected, formulas = latex_to_protected_text(source)
        self.assertEqual(2, len(formulas))
        self.assertEqual(2, len(formula_tokens(protected)))
        restored = restore_math_tokens(protected, formulas)
        self.assertIn(r"$P_\theta(y\mid x)$", restored)
        self.assertIn(r"\begin{equation}", restored)

    def test_formula_tokens_must_be_preserved_exactly(self) -> None:
        paragraph = Paragraph(
            id="math-p1",
            page=1,
            anchor="math",
            source="Objective @@MATH_0001@@ and @@MATH_0002@@.",
        )
        valid = validate_translation_response(
            {
                "items": [
                    {
                        "id": "math-p1",
                        "status": "translated",
                        "translation": "目标 @@MATH_0001@@ 与 @@MATH_0002@@。",
                        "note": "",
                    }
                ]
            },
            [paragraph],
        )
        self.assertEqual("translated", valid["math-p1"]["status"])

        reordered = validate_translation_response(
            {
                "items": [
                    {
                        "id": "math-p1",
                        "status": "translated",
                        "translation": "目标 @@MATH_0002@@ 与 @@MATH_0001@@。",
                        "note": "",
                    }
                ]
            },
            [paragraph],
        )
        self.assertEqual("translated", reordered["math-p1"]["status"])

        with self.assertRaises(FormulaTokenError):
            validate_translation_response(
                {
                    "items": [
                        {
                            "id": "math-p1",
                            "status": "translated",
                            "translation": "目标只保留 @@MATH_0002@@。",
                            "note": "",
                        }
                    ]
                },
                [paragraph],
            )

    def test_latex_cross_references_and_citations_are_preserved(self) -> None:
        source = r"""
\begin{document}
\begin{abstract}Prior work \citep{alpha,beta} motivates this study.\end{abstract}
\section{Method}\label{sec:method}
See \autoref{sec:method} and \eqref{eq:loss}.
\begin{equation}\tag{A}\label{eq:loss}L(x)=x^2\end{equation}
\end{document}
"""
        protected, formulas, references = latex_to_protected_document(source)
        self.assertEqual(3, len(reference_tokens(protected)))
        self.assertEqual("A", references.labels["eq:loss"]["number"])
        self.assertEqual(1, references.citations["alpha"]["number"])
        self.assertEqual(2, references.citations["beta"]["number"])

        sections = segment_document([(1, protected)])
        associate_label_targets(sections, references)
        restored = restore_all_tokens(sections[-1].paragraphs[0].source, formulas, references)
        self.assertIn("#xref:sec%3Amethod", restored)
        self.assertNotIn("@@LABEL_", restored)
        self.assertTrue(references.labels["sec:method"].get("targetSectionId"))

    def test_reference_tokens_must_be_preserved_exactly(self) -> None:
        paragraph = Paragraph(id="ref-p1", page=1, anchor="ref", source="See @@XREF_0001@@.")
        with self.assertRaises(ReferenceTokenError):
            validate_translation_response(
                {"items": [{"id": "ref-p1", "status": "translated", "translation": "参见正文。", "note": ""}]},
                [paragraph],
            )

    def test_cross_reference_metadata_restores_formula_tokens(self) -> None:
        references = latex_to_protected_document(
            r"\begin{document}\begin{table}\caption{Results on $D$}\label{tab:results}\end{table}\end{document}"
        )[2]
        formulas = {"@@MATH_0001@@": "$D$"}
        references.labels["tab:results"]["title"] = "Results on @@MATH_0001@@"

        output = build_output("Paper", "", "full", "test", [], "latex", 1, formulas, references)

        self.assertEqual("Results on $D$", output["crossReferences"]["labels"]["tab:results"]["title"])

    def test_bibtex_metadata_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "paper.bib").write_text(
                '@article{alpha, title={A Useful Paper}, author={Ada Lovelace and Alan Turing}, year={2024}, url={https://example.test/paper}}',
                encoding="utf-8",
            )
            entries = parse_bibtex_entries(root)
        self.assertEqual("A Useful Paper", entries["alpha"]["title"])
        self.assertEqual("2024", entries["alpha"]["year"])

    def test_referenced_latex_assets_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "diagram.png").write_bytes(b"fake-png")
            source = r"""
\begin{document}
\begin{abstract}We introduce the method.\end{abstract}
\section{Method}
See Figure \ref{fig:flow}, Table \ref{tab:prompt}, and Algorithm \ref{alg:run}.
\begin{figure}\includegraphics{diagram.png}\caption{System flow}\label{fig:flow}\end{figure}
\begin{table}\begin{tabular}{cc}Input & Output\\Question & Answer\end{tabular}\caption{Prompt fields}\label{tab:prompt}\end{table}
\begin{algorithm}\caption{Run search}\label{alg:run}\begin{algorithmic}[1]
\Require Query $x$
\State Initialize result
\While{not done}
\State Search for $x$
\EndWhile
\end{algorithmic}\end{algorithm}
\end{document}
"""
            protected, _formulas, references = latex_to_protected_document(source, source_root=root)
            sections = segment_document([(1, protected)])
            associate_label_targets(sections, references)

        assets = {asset["label"]: asset for asset in references.assets}
        self.assertEqual(2, len(assets["tab:prompt"]["rows"]))
        self.assertEqual(5, len(assets["alg:run"]["steps"]))
        self.assertEqual(1, len(assets["fig:flow"]["images"]))
        self.assertTrue(all(asset["referenced"] for asset in assets.values()))
        self.assertEqual("asset-tab-prompt", references.labels["tab:prompt"]["targetAssetId"])

    def test_low_quality_pages_are_selected_for_ocr(self) -> None:
        pages = [
            (1, "x 1 ?"),
            (2, "This is readable native PDF text. " * 20),
        ]
        self.assertEqual({1}, low_quality_page_numbers(pages))

    def test_numbered_acknowledgments_stops_main_body(self) -> None:
        pages = [
            (
                1,
                "Abstract\n\nReadable abstract paragraph.\n\n"
                "1. Introduction\n\nReadable introduction paragraph.\n\n"
                "7. Acknowledgments\n\nThanks to everyone.\n\n"
                "8. Appendix\n\nAppendix material.",
            )
        ]
        sections = segment_document(pages)
        self.assertEqual(["Abstract", "1. Introduction"], [section.title for section in sections])

    def test_unnumbered_journal_headings_split_sections(self) -> None:
        pages = [
            (
                1,
                "a b s t r a c t\n\nThe compound was prepared in two steps.\n\n"
                "INTRODUCTION\n\nSulfonyl chlorides are versatile intermediates.\n\n"
                "Experimental Section\n\nCompound 2a was prepared as described.\n\n"
                "Results and discussion\n\nThe reaction proceeded smoothly.\n\n"
                "Conclusions\n\nThis route is practical.",
            )
        ]
        sections = segment_document(pages)
        self.assertEqual(
            ["Abstract", "INTRODUCTION", "Experimental Section", "Results and discussion", "Conclusions"],
            [section.title for section in sections],
        )
        self.assertEqual([1, 1, 1, 1, 1], [section.page_start for section in sections])

    def test_repeated_running_head_and_footer_lines_are_ignored(self) -> None:
        running_head = "2.1 Synthesis of 4-acetamidobenzenesulfonyl chloride"
        footer = "J. Org. Chem. 2015, 80, 1234-1240"
        pages = [
            (1, f"Abstract\n\nBody about the synthesis route.\n\n{footer}"),
            (2, f"{running_head}\n\nIntroduction\n\nBody about sulfonyl chlorides.\n\n{footer}"),
            (3, f"{running_head}\n\nResults and Discussion\n\nBody about yields.\n\n{footer}"),
        ]
        noise = {noise_match_key(running_head), noise_match_key(footer)}
        sections = segment_document(pages, noise_hints=noise)
        self.assertEqual(["Abstract", "Introduction", "Results and Discussion"], [s.title for s in sections])
        self.assertNotIn(running_head, " ".join(p.source for s in sections for p in s.paragraphs))

        repeated = repeated_furniture_lines(pages)
        self.assertIn(noise_match_key(footer), repeated)

    def test_repeated_running_head_with_page_numbers_is_page_furniture(self) -> None:
        def running_head(page_number: int) -> str:
            return f"W. Li et al. / Int. J. 98 (2015) 81 e 88 {page_number}"

        pages = [
            (index, f"Body text of page {index}.\n\n{running_head(80 + index)}") for index in range(1, 6)
        ]
        repeated = repeated_furniture_lines(pages)
        self.assertIn(noise_match_key(running_head(81)), repeated)

    def test_pdf_layout_hints_separate_headings_from_running_heads(self) -> None:
        pages = [
            (
                1,
                [
                    PdfTextBlock("A Practical Synthesis of 4-Acetamidobenzenesulfonyl Chloride", 15.0, 0.9, 1, False),
                    PdfTextBlock("Yiyang Chen and Friend", 9.0, 0.0, 1, False),
                    PdfTextBlock("Abstract", 10.0, 0.9, 1, False),
                    PdfTextBlock("We report a practical synthesis of the title compound.", 9.0, 0.0, 1, False),
                    PdfTextBlock("1. Introduction", 10.0, 0.9, 1, False),
                ],
            ),
            (
                2,
                [
                    PdfTextBlock("J. Org. Chem. 2015, 80, 1234-1240", 8.0, 0.0, 1, True),
                    PdfTextBlock("2.1 Synthesis of 4-acetamidobenzenesulfonyl chloride", 8.0, 0.0, 1, True),
                    PdfTextBlock("Results and Discussion", 10.0, 0.9, 1, False),
                ],
            ),
            (
                3,
                [
                    PdfTextBlock("J. Org. Chem. 2015, 80, 1234-1240", 8.0, 0.0, 1, True),
                    PdfTextBlock("2.1 Synthesis of 4-acetamidobenzenesulfonyl chloride", 8.0, 0.0, 1, True),
                    PdfTextBlock("More body about yields.", 9.0, 0.0, 1, False),
                ],
            ),
        ]
        with patch("scripts.generate_translation_json.pdf_page_blocks", return_value=pages):
            hints, noise, blocked = pdf_structure_hints(Path("unused.pdf"))

        self.assertEqual({"abstract", "1. introduction", "a practical synthesis of 4-acetamidobenzenesulfonyl chloride"}, hints[1])
        self.assertNotIn("abstract", hints.get(2, set()))
        self.assertEqual({"results and discussion"}, hints[2])
        self.assertEqual(
            {
                noise_match_key("J. Org. Chem. 2015, 80, 1234-1240"),
                noise_match_key("2.1 Synthesis of 4-acetamidobenzenesulfonyl chloride"),
            },
            noise,
        )
        # Running heads are also blocked so text rules cannot revive them.
        self.assertTrue(noise <= blocked)

        text_pages = [
            (1, "Abstract\n\nWe report a practical synthesis.\n\n1. Introduction\n\nSulfonyl chlorides matter."),
            (
                2,
                "2.1 Synthesis of 4-acetamidobenzenesulfonyl chloride\n\n"
                "Results and Discussion\n\nThe reaction proceeded smoothly.",
            ),
            (
                3,
                "2.1 Synthesis of 4-acetamidobenzenesulfonyl chloride\n\nMore body about yields.",
            ),
        ]
        sections = segment_document(text_pages, heading_hints=hints, noise_hints=noise)
        self.assertEqual(["Abstract", "1. Introduction", "Results and Discussion"], [s.title for s in sections])

    def test_paragraphs_split_across_pages_are_merged(self) -> None:
        first_half = "The reaction was carried out in a 250 mL three-necked flask under nitrogen, and the mixture was stirred " * 3
        pages = [
            (1, first_half.strip()),
            (2, "overnight before the solvent was removed under reduced pressure."),
        ]
        sections = segment_document(pages)
        paragraphs = [paragraph for section in sections for paragraph in section.paragraphs]
        self.assertEqual(1, len(paragraphs))
        self.assertEqual(1, paragraphs[0].page)
        self.assertEqual(2, paragraphs[0].page_end)
        self.assertIn("overnight before the solvent was removed", paragraphs[0].source)

    def test_chinese_journal_headings_and_stop_section(self) -> None:
        pages = [
            (
                1,
                "摘要：本文报道了一种实用的处理方法。\n\n关键词：厌氧处理；磺酰氯；废水\n\n"
                "1 引言\n\n芳香磺酸盐广泛应用于工业化学品。\n\n"
                "2 实验部分\n\n采用高效液相色谱进行测定。\n\n"
                "3 结果与讨论\n\n反应收率较高且重复性良好。",
            ),
            (2, "4 结论\n\n该路线简单可靠。\n\n参考文献\n\n[1] 张三. 题目. 期刊, 2015."),
        ]
        sections = segment_document(pages)
        self.assertEqual(
            ["Abstract", "1 引言", "2 实验部分", "3 结果与讨论", "4 结论"],
            [section.title for section in sections],
        )

    def test_margin_number_is_merged_into_heading(self) -> None:
        pages = [
            (
                1,
                "Abstract\n\nThe paper introduces a generic framework for widget alignment.\n\n"
                "1\nIntroduction\n\nRecent advancements in large language models have changed the field.",
            )
        ]
        sections = segment_document(pages)
        self.assertEqual(["Abstract", "1 Introduction"], [section.title for section in sections])

    def test_figure_and_table_text_never_becomes_a_heading(self) -> None:
        pages = [
            (
                1,
                [
                    PdfTextBlock("Bray-Curtis dissimilarity Jaccard index", 9.0, 1.0, 1, False, visual=True),
                    PdfTextBlock("Results", 11.0, 0.9, 1, False),
                ],
            ),
            (
                2,
                [
                    PdfTextBlock("Bray-Curtis dissimilarity Jaccard index", 9.0, 1.0, 1, False, visual=True),
                    PdfTextBlock("Discussion", 11.0, 0.9, 1, False),
                ],
            ),
        ]
        with patch("scripts.generate_translation_json.pdf_page_blocks", return_value=pages):
            hints, _noise, blocked = pdf_structure_hints(Path("unused.pdf"))

        figure_label = "bray-curtis dissimilarity jaccard index"
        self.assertIn(figure_label, blocked)
        self.assertNotIn(figure_label, hints.get(1, set()))
        self.assertEqual({"results"}, hints[1])
        self.assertEqual({"discussion"}, hints[2])

        text_pages = [
            (
                1,
                "Abstract\n\nWe quantify the contribution of environmental microbiota.\n\n"
                "Bray-Curtis dissimilarity Jaccard index\n\n"
                "Results\n\nThe communities differed between groups.",
            )
        ]
        sections = segment_document(text_pages, heading_hints=hints, blocked_hints=blocked)
        self.assertEqual(["Abstract", "Results"], [section.title for section in sections])

    def test_reading_order_sorts_columns_and_full_width_blocks(self) -> None:
        def block(text, x0, y0, x1, y1):
            return PdfTextBlock(text, 9.0, 0.0, 1, False, bbox=(x0, y0, x1, y1))

        blocks = [
            block("Intro right column text", 310, 300, 560, 400),
            block("Wide title spanning both columns", 60, 60, 560, 90),
            block("Left column heading", 60, 120, 280, 140),
            block("Left column body text", 60, 150, 280, 500),
            block("Right column heading", 310, 120, 560, 140),
        ]
        ordered = [item.text for item in order_blocks_reading_order(blocks)]
        self.assertEqual(
            [
                "Wide title spanning both columns",
                "Left column heading",
                "Left column body text",
                "Right column heading",
                "Intro right column text",
            ],
            ordered,
        )

    def test_nature_style_document_without_abstract_heading_still_segments(self) -> None:
        body = "The alignment of widgets is a central problem in engineering practice. " * 4
        pages = [
            (1, "Widgets align better when the noise is modelled\n\nAda Lovelace, Alan Turing\n\n" + body),
            (2, "Methods\n\nWe trained a small model on the widget corpus. " * 3),
            (3, "References\n\n1. Author, A. Nature 2024."),
        ]
        sections = segment_document(pages)
        self.assertEqual(["Main text", "Methods"], [section.title for section in sections])

    def test_extraction_without_abstract_heading_is_still_usable(self) -> None:
        summary = {
            "score": 180.0,
            "sections": 2,
            "paragraphs": 9,
            "characters": 2000,
            "has_abstract": False,
            "has_headings": True,
        }
        self.assertTrue(extraction_is_usable(summary))
        summary["has_headings"] = False
        self.assertFalse(extraction_is_usable(summary))


if __name__ == "__main__":
    unittest.main()
