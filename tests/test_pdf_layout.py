"""Layout tests for journal PDFs: column order, running heads, figure text, CJK headings."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.generate_translation_json import (
    PdfTextBlock,
    order_blocks_reading_order,
    pdf_structure_hints,
    segment_document,
)

try:  # PyMuPDF is required by requirements.txt but keep the tests importable without it.
    import fitz
except Exception:  # noqa: BLE001
    fitz = None


BODY = (
    "The reaction was monitored by high performance liquid chromatography and the results were "
    "consistent across three independent runs, which supports the general conclusion. "
)


def text_block(text, x0, y0, x1, y1, *, size=9.0, bold=0.0, lines=1, margin=False, visual=False):
    return PdfTextBlock(text, size, bold, lines, margin, (x0, y0, x1, y1), visual)


class ReadingOrderTests(unittest.TestCase):
    def test_columns_are_read_column_by_column_after_full_width_blocks(self) -> None:
        blocks = [
            text_block("Right column body", 310, 300, 560, 400),
            text_block("Wide title spanning the page", 60, 60, 560, 90),
            text_block("Left column heading", 60, 120, 280, 140),
            text_block("Left column body", 60, 150, 280, 500),
            text_block("Right column heading", 310, 120, 560, 140),
        ]
        self.assertEqual(
            [
                "Wide title spanning the page",
                "Left column heading",
                "Left column body",
                "Right column heading",
                "Right column body",
            ],
            [block.text for block in order_blocks_reading_order(blocks)],
        )

    def test_single_column_blocks_keep_top_to_bottom_order(self) -> None:
        blocks = [
            text_block("Second paragraph", 60, 300, 520, 400),
            text_block("First paragraph", 60, 100, 520, 200),
        ]
        self.assertEqual(
            ["First paragraph", "Second paragraph"],
            [block.text for block in order_blocks_reading_order(blocks)],
        )


@unittest.skipUnless(fitz is not None, "PyMuPDF is required for PDF layout tests")
class JournalPdfTests(unittest.TestCase):
    def _write_two_column_journal(self, path: Path) -> None:
        document = fitz.open()
        page = document.new_page(width=595, height=842)
        page.insert_text((60, 40), "J. Widget Sci. 2024, 3, 100-112", fontsize=8, fontname="heit")
        page.insert_text((60, 80), "Widget Alignment in Noisy Environments", fontsize=15, fontname="hebo")
        page.insert_text((60, 105), "Ada Lovelace and Alan Turing", fontsize=9, fontname="helv")
        page.insert_text((60, 140), "Abstract", fontsize=10, fontname="hebo")
        page.insert_textbox(fitz.Rect(60, 150, 535, 250), BODY * 2, fontsize=9, fontname="helv")
        page.insert_text((60, 260), "1 Introduction", fontsize=10, fontname="hebo")
        page.insert_textbox(fitz.Rect(60, 270, 285, 700), BODY * 3, fontsize=9, fontname="helv")
        page.insert_text((320, 260), "2 Methods", fontsize=10, fontname="hebo")
        page.insert_textbox(fitz.Rect(320, 270, 545, 700), BODY * 3, fontsize=9, fontname="helv")

        for page_number in (2, 3):
            page = document.new_page(width=595, height=842)
            page.insert_text(
                (60, 40),
                f"W. Lovelace et al. / J. Widget Sci. 3 (2024) 100 e 112 {page_number + 100}",
                fontsize=8,
                fontname="heit",
            )
            # A vector figure with bold axis labels: this text must never become a section.
            page.draw_rect(fitz.Rect(150, 560, 450, 720))
            page.insert_text((160, 600), "Bray-Curtis dissimilarity", fontsize=7, fontname="hebo")
            page.insert_text((160, 620), "Jaccard index", fontsize=7, fontname="hebo")
            if page_number == 2:
                page.insert_text((320, 200), "3 Results", fontsize=10, fontname="hebo")
            page.insert_textbox(fitz.Rect(320, 210, 545, 520), BODY * 4, fontsize=9, fontname="helv")
            page.insert_textbox(fitz.Rect(60, 150, 285, 520), BODY * 3, fontsize=9, fontname="helv")
        document.save(str(path))
        document.close()

    def _write_chinese_journal(self, path: Path) -> None:
        document = fitz.open()
        page = document.new_page(width=595, height=842)
        page.insert_text((60, 60), "废水中有机污染物的厌氧处理研究", fontsize=16, fontname="china-s")
        page.insert_text((60, 90), "李伟成，王春燕", fontsize=10, fontname="china-s")
        page.insert_textbox(
            fitz.Rect(60, 110, 535, 190), "摘要：本文报道了一种实用的处理方法。" + BODY, fontsize=9, fontname="china-s"
        )
        page.insert_text((60, 200), "关键词：厌氧处理；磺酰氯；废水", fontsize=9, fontname="china-s")
        page.insert_text((60, 220), "1 引言", fontsize=11, fontname="china-s")
        page.insert_textbox(fitz.Rect(60, 230, 535, 400), BODY * 2, fontsize=9, fontname="china-s")
        page = document.new_page(width=595, height=842)
        page.insert_text((60, 60), "2 实验部分", fontsize=11, fontname="china-s")
        page.insert_textbox(fitz.Rect(60, 70, 535, 230), BODY * 2, fontsize=9, fontname="china-s")
        page.insert_text((60, 240), "3 结论", fontsize=11, fontname="china-s")
        page.insert_textbox(fitz.Rect(60, 250, 535, 340), BODY, fontsize=9, fontname="china-s")
        page.insert_text((60, 360), "参考文献", fontsize=11, fontname="china-s")
        page.insert_text((60, 385), "[1] 张三. 题目. 期刊, 2015.", fontsize=9, fontname="china-s")
        document.save(str(path))
        document.close()

    def _sections(self, path: Path):
        pages = segment_document_pages(path)
        hints, noise, blocked = pdf_structure_hints(path)
        return segment_document(pages, heading_hints=hints, noise_hints=noise, blocked_hints=blocked)

    def test_two_column_journal_with_figure_and_running_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.pdf"
            self._write_two_column_journal(path)
            sections = self._sections(path)

        titles = [section.title for section in sections]
        self.assertEqual(["Abstract", "1 Introduction", "2 Methods", "3 Results"], titles)
        self.assertTrue(all("Lovelace" not in title for title in titles))
        self.assertTrue(all("Bray-Curtis" not in title for title in titles))

    def test_chinese_journal_sections_and_reference_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chinese.pdf"
            self._write_chinese_journal(path)
            sections = self._sections(path)

        self.assertEqual(["Abstract", "1 引言", "2 实验部分", "3 结论"], [s.title for s in sections])
        body = " ".join(paragraph.source for section in sections for paragraph in section.paragraphs)
        self.assertNotIn("张三", body)  # the reference list is not translated


def segment_document_pages(path: Path):
    from scripts.generate_translation_json import extract_pdf_pages_pymupdf

    return extract_pdf_pages_pymupdf(path, None)


if __name__ == "__main__":
    unittest.main()
