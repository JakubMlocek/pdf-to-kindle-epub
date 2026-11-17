"""Integration checks use a generated PDF, never a third-party book."""

import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

import fitz
from lxml import etree
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pdf_to_epub import DEFAULT_PROFILE, block_html, clean, graphic_zones, page_numbers, validate_epub

SCRIPT = Path(__file__).resolve().parents[1] / "pdf_to_epub.py"


class ConversionTests(unittest.TestCase):
    def test_real_pdf_to_epub_with_navigation_and_graphics(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "input.pdf", Path(directory) / "output.epub"
            with fitz.open() as document:
                page = document.new_page(width=400, height=600)
                page.insert_text((30, 50), "First chapter", fontsize=22)
                page.insert_text((30, 95), "Example body text and literal --flag.", fontsize=11)
                image = io.BytesIO()
                Image.new("RGB", (160, 80), "navy").save(image, "PNG")
                page.insert_image(fitz.Rect(30, 130, 190, 210), stream=image.getvalue())
                page = document.new_page(width=500, height=650)
                page.insert_text((30, 50), "Second chapter", fontsize=22)
                page.insert_text((30, 95), "Another page with readable text.", fontsize=11)
                document.set_toc([[1, "First chapter", 1], [2, "Same-page bookmark", 1], [1, "Second chapter", 2]])
                document.set_metadata({"title": "Generated test document", "author": "Test author"})
                document.save(source)
            result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            validate_epub(output)
            with zipfile.ZipFile(output) as archive:
                contents = "\n".join(archive.read(name).decode() for name in archive.namelist() if name.endswith(".xhtml"))
                self.assertIn("literal --flag", contents)
                self.assertIn("Another page with readable text", contents)
                self.assertIn("Same-page bookmark", archive.read("EPUB/nav.xhtml").decode())
                self.assertTrue(any(name.endswith(".jpg") for name in archive.namelist()))
                etree.fromstring(archive.read("EPUB/content.opf"))
            original = output.read_bytes()
            result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(output)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already exists", result.stderr)
            self.assertEqual(output.read_bytes(), original)

    def test_snapshot_cover_and_no_bookmarks(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "input.pdf", Path(directory) / "output.epub"
            with fitz.open() as document:
                for text in ["Cover page", "Print-only column layout"]:
                    page = document.new_page()
                    page.insert_text((30, 80), text)
                document.save(source)
            result = subprocess.run([
                sys.executable, str(SCRIPT), str(source), str(output),
                "--cover-page", "1", "--snapshot-pages", "2", "--title", "Snapshot test",
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            validate_epub(output)
            with zipfile.ZipFile(output) as archive:
                section = archive.read("EPUB/section-000.xhtml").decode()
                self.assertIn('src="cover.jpg"', section)
                self.assertIn("Source PDF page 2", section)
                self.assertNotIn("Print-only column layout", section)

    def test_hyphen_normalization_and_ranges(self):
        self.assertEqual(clean("on-\xadpremises and --flag"), "on-premises and --flag")
        self.assertEqual(page_numbers("1,3-4", 4), {0, 2, 3})
        for bad in ["0", "3-1", "5", "1,", "oops"]:
            with self.assertRaises(ValueError):
                page_numbers(bad, 4)

    def test_hyphenated_words_across_lines(self):
        def paragraph(first, second):
            return {
                "bbox": (30, 40, 300, 65),
                "lines": [
                    {"bbox": (30, 40, 300, 51), "spans": [{"text": first, "font": "Body-Bold", "size": 11}]},
                    {"bbox": (30, 52, 300, 63), "spans": [{"text": second, "font": "Body-Bold", "size": 11}]},
                ],
            }
        def rendered_text(block):
            root = etree.fromstring(block_html(block, DEFAULT_PROFILE, [24, 20, 16, 14]))
            return "".join(root.itertext())
        self.assertEqual(rendered_text(paragraph("implemen-", "tation")), "implementation")
        self.assertEqual(rendered_text(paragraph("on-\xad", "premises")), "on-premises")

    def test_image_overlapping_a_text_block_preserves_the_entire_block(self):
        # Regression: Rect |= rect rebinds a loop variable; it does not update
        # the rectangle in the zones list, which previously caused an endless loop.
        blocks = [
            {"type": 1, "bbox": (72, 72, 180, 180)},
            {"type": 0, "bbox": (72, 146, 453, 421), "lines": []},
        ]
        zones = graphic_zones(None, blocks, DEFAULT_PROFILE, fitz.Rect(0, 0, 500, 600), [24, 20, 16, 14], tables=False)
        self.assertEqual(len(zones), 1)
        self.assertTrue(zones[0].contains(fitz.Rect(72, 146, 453, 421)))


if __name__ == "__main__":
    unittest.main()
