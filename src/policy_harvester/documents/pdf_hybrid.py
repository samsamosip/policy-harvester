"""PDF parsing: PyMuPDF for text, Docling only for tables, the LLM once per page with pictures.

Docling's own text reconstruction dropped and reordered characters in some Korean PDFs (one
notice lost "만원" from "종합대 200만원"), while the PDF text layer read by PyMuPDF was exact.
So text comes from the text layer, Docling contributes table structure (rows, columns, merged
cells), and text blocks inside a table's area are dropped in favour of the table.

Pixels go to the multimodal LLM per page, not per picture:
- a page without a text layer (scan) is transcribed whole;
- a page with text and pictures is sent once with its extracted text, and the model adds only
  what the pictures say (screenshots and posters in manuals used to cost one call per picture).
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import pymupdf as fitz

from .docling_parser import DOCLING_VERSION, converter, table_block_data
from .parsers import Block, EmbeddedImage, ParseResult, _key, image_placeholder, normalized_bbox

MIN_TEXT_CHARS = 30
# A picture smaller than this share of the page (icons, bullets, logos in a header) is ignored.
MIN_PICTURE_AREA = 0.04
TABLE_OVERLAP = 0.5
PAGE_TEXT_LIMIT = 6000
RENDER_ZOOM = 2.0


@dataclass(frozen=True)
class _Table:
    page: int
    box: tuple[float, float, float, float]  # top-left origin, PDF points
    text: str
    data: dict
    path: str


def _docling_tables(payload: bytes) -> list[_Table]:
    from docling.datamodel.base_models import DocumentStream
    from docling_core.types.doc import TableItem

    doc = converter().convert(DocumentStream(name="document.pdf", stream=io.BytesIO(payload))).document
    tables = []
    for item, _level in doc.iterate_items():
        if not isinstance(item, TableItem) or not item.prov:
            continue
        prov = item.prov[0]
        if prov.page_no not in doc.pages:
            continue
        box = prov.bbox.to_top_left_origin(page_height=doc.pages[prov.page_no].size.height)
        data, text_value = table_block_data(item, doc)
        if text_value:
            tables.append(_Table(prov.page_no, (box.l, box.t, box.r, box.b), text_value, data,
                                 f"docling/{item.self_ref.lstrip('#/')}"))
    return tables


def _inside(block: tuple[float, float, float, float], table: tuple[float, float, float, float]) -> bool:
    """Whether most of a text block lies within a table's area."""
    width = max(0.0, min(block[2], table[2]) - max(block[0], table[0]))
    height = max(0.0, min(block[3], table[3]) - max(block[1], table[1]))
    area = max(1e-6, (block[2] - block[0]) * (block[3] - block[1]))
    return width * height / area >= TABLE_OVERLAP


def _picture_area(page) -> float:
    """Share of the page covered by raster pictures that are large enough to carry content."""
    page_area = page.rect.width * page.rect.height
    total = 0.0
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        share = max(0.0, (x1 - x0) * (y1 - y0)) / page_area
        if share >= MIN_PICTURE_AREA:
            total += share
    return total


class PdfHybridParser:
    formats = frozenset({"pdf"})
    name, version = "pdf-hybrid", f"1.0+docling{DOCLING_VERSION}"

    def parse(self, payload: bytes, filename: str | None = None) -> ParseResult:
        flags: list[str] = []
        try:
            tables = _docling_tables(payload)
        except Exception as exc:  # noqa: BLE001 - text still comes from the text layer
            tables = []
            flags.append(f"tables_unavailable:{exc.__class__.__name__}")
        blocks: list[Block] = []
        images: list[EmbeddedImage] = []
        with fitz.open(stream=payload, filetype="pdf") as document:
            for index, page in enumerate(document):
                number = index + 1
                width, height = page.rect.width, page.rect.height
                raw_blocks = [item for item in page.get_text("blocks", sort=True) if item[6] == 0]
                if sum(len(str(item[4]).strip()) for item in raw_blocks) < MIN_TEXT_CHARS:
                    key = f"page-{number}"
                    images.append(EmbeddedImage(key, self._render(page), number,
                                                {"rendered_page": number, "mode": "page_full"}))
                    blocks.append(image_placeholder("", key, f"page/{number}/image", number))
                    flags.append("scanned_pages")
                    continue
                page_tables = [table for table in tables if table.page == number]
                placed: list[tuple[float, Block]] = []
                for raw in raw_blocks:
                    text_value = " ".join(str(raw[4]).split())
                    box = (raw[0], raw[1], raw[2], raw[3])
                    if not text_value or any(_inside(box, table.box) for table in page_tables):
                        continue
                    bbox = normalized_bbox(box[0] / width, box[1] / height, box[2] / width, box[3] / height)
                    placed.append((box[1], Block("", "paragraph", text_value, page_number=number,
                                                 source_path=f"page/{number}/text/{len(placed)}", bbox=bbox)))
                for table in page_tables:
                    x0, y0, x1, y1 = table.box
                    placed.append((y0, Block("", "table", table.text, page_number=number,
                                             source_path=table.path, table_data=table.data,
                                             bbox=normalized_bbox(x0 / width, y0 / height,
                                                                  x1 / width, y1 / height))))
                placed.sort(key=lambda item: item[0])
                blocks.extend(block for _top, block in placed)
                if _picture_area(page) > 0:
                    page_text = "\n".join(block.text for _top, block in placed)[:PAGE_TEXT_LIMIT]
                    key = f"page-{number}-pictures"
                    images.append(EmbeddedImage(key, self._render(page), number,
                                                {"rendered_page": number, "mode": "page_supplement",
                                                 "page_text": page_text}))
                    blocks.append(image_placeholder("", key, f"page/{number}/pictures", number))
        final = tuple(Block(_key("pdf", index), block.kind, block.text, page_number=block.page_number,
                            source_path=block.source_path, bbox=block.bbox, table_data=block.table_data,
                            metadata=block.metadata) for index, block in enumerate(blocks))
        return ParseResult(self.name, self.version, "succeeded", final,
                           "\n\n".join(block.text for block in final if block.text),
                           tuple(dict.fromkeys(flags)), tuple(images))

    @staticmethod
    def _render(page) -> bytes:
        return page.get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM), alpha=False).tobytes("png")
