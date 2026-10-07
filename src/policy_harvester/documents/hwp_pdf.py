"""HWP/HWPX: direct parse for text and tables, rhwp's PDF rendering for pages and preview.

The direct parse (hwp5html/hwp5txt, HWPX XML) reads the document's own structure, so its text
and tables are exact; Docling on the rendered PDF found 30% fewer tables. What the rendering adds
is pages: pictures and text-less pages are sent to the multimodal LLM once per page (instead of
once per embedded picture), and the PDF is the admin preview, since browsers cannot show HWP.
If rhwp fails, the direct parse runs as before, embedded pictures included.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import pymupdf as fitz

from .parsers import Block, DerivedFile, DocumentParser, EmbeddedImage, ParseResult, _key, image_placeholder
from .pdf_hybrid import PAGE_TEXT_LIMIT, RENDER_ZOOM, _picture_area

RHWP_BIN = os.environ.get("RHWP_BIN", "rhwp")
RHWP_VERSION = os.environ.get("RHWP_VERSION", "0.8.7")
# Without system fonts rhwp writes pages with no text at all; these ship in the image.
FONT_ARGS = ("--font-path", "/usr/share/fonts", "--fallback-sans", "Noto Sans CJK KR",
             "--fallback-serif", "Noto Serif CJK KR")
TIMEOUT_SECONDS = 300


def render_pdf(payload: bytes, format_name: str) -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        source, output = Path(directory) / f"input.{format_name}", Path(directory) / "output.pdf"
        source.write_bytes(payload)
        subprocess.run([RHWP_BIN, "export-pdf", str(source), "-o", str(output), *FONT_ARGS],
                       check=True, capture_output=True, timeout=TIMEOUT_SECONDS)
        return output.read_bytes()


def picture_pages(pdf: bytes) -> list[EmbeddedImage]:
    """Rendered pages with pictures, each sent once with the page's own text so the model adds
    only what the pictures say. Pages of lines and boxes (forms) carry no new text: the direct
    parse already has every character of an HWP document."""
    images = []
    with fitz.open(stream=pdf, filetype="pdf") as document:
        for index, page in enumerate(document):
            text_value = page.get_text().strip()
            if _picture_area(page) == 0:
                continue
            png = page.get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM), alpha=False).tobytes("png")
            images.append(EmbeddedImage(f"page-{index + 1}-pictures", png, index + 1,
                                        {"rendered_page": index + 1, "mode": "page_supplement",
                                         "page_text": text_value[:PAGE_TEXT_LIMIT]}))
    return images


class HwpPdfParser:
    formats = frozenset({"hwp", "hwpx"})
    name, version = "hwp-rhwp-pages", f"1.0+rhwp{RHWP_VERSION}"

    def __init__(self, direct: dict[str, DocumentParser]):
        self.direct = direct

    def parse(self, payload: bytes, filename: str | None = None, *, format_name: str = "hwp") -> ParseResult:
        pdf = render_pdf(payload, format_name)
        direct = self.direct[format_name].parse(payload, filename)
        # Embedded pictures are covered by the rendered pages; drop their per-picture placeholders.
        blocks = [block for block in direct.blocks if "pending_image" not in block.metadata]
        pages = picture_pages(pdf)
        for image in pages:
            blocks.append(image_placeholder("", image.key, f"rendered/page/{image.page_number}",
                                            image.page_number))
        final = tuple(Block(_key("hwp", index), block.kind, block.text, page_number=block.page_number,
                            source_path=block.source_path, bbox=block.bbox, table_data=block.table_data,
                            ocr_used=block.ocr_used, metadata=block.metadata) for index, block in enumerate(blocks))
        return replace(direct, parser_name=self.name, parser_version=self.version, blocks=final,
                       text="\n\n".join(block.text for block in final if block.text), images=tuple(pages),
                       derived=(DerivedFile("pdf_render", pdf, f"rhwp {RHWP_VERSION}"),))
