"""PDF/DOCX/PPTX/XLSX through Docling, with Docling OCR disabled.

Docling recovers reading order and table structure (TableFormer) from the text layer. Anything
that needs reading pixels goes to the multimodal LLM instead: PDF pages without a text layer
and pictures embedded in documents become image placeholders for the worker to transcribe.
"""
from __future__ import annotations

import io
import os
import threading
from importlib.metadata import version as package_version

import pymupdf as fitz

from .parsers import (Block, EmbeddedImage, ParseResult, _key, image_placeholder, normalized_bbox)

DOCLING_VERSION = package_version("docling")
MIN_TEXT_CHARS = 30
EXTENSIONS = {"pdf": ".pdf", "docx": ".docx", "pptx": ".pptx", "xlsx": ".xlsx"}
KINDS = {"title": "heading", "section_header": "heading", "list_item": "list_item",
         "caption": "caption", "footnote": "footnote", "page_header": "page_header",
         "page_footer": "page_footer"}

_converter = None
_converter_lock = threading.Lock()


def converter():
    """One converter per process: model loading takes seconds and a lot of memory."""
    global _converter
    with _converter_lock:
        if _converter is None:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption

            options = PdfPipelineOptions(
                do_ocr=False, do_table_structure=True, generate_picture_images=True,
                images_scale=2.0, artifacts_path=os.environ.get("DOCLING_ARTIFACTS_PATH"))
            options.table_structure_options.do_cell_matching = True
            _converter = DocumentConverter(
                allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.PPTX, InputFormat.XLSX],
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
        return _converter


def scanned_pages(payload: bytes) -> dict[int, bytes]:
    """1-based page numbers without a usable text layer, rendered for transcription."""
    pages = {}
    with fitz.open(stream=payload, filetype="pdf") as document:
        for index, page in enumerate(document):
            if sum(len(str(item[4]).strip()) for item in page.get_text("blocks")) < MIN_TEXT_CHARS:
                pages[index + 1] = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
    return pages


def table_block_data(table, doc) -> tuple[dict, str]:
    """Grid with spans repeated per covered cell, plus one-value-per-source-cell row text."""
    cells = table.data.table_cells
    height, width = table.data.num_rows, table.data.num_cols
    origin: dict[tuple[int, int], int] = {}
    for index, cell in enumerate(cells):
        for row in range(cell.start_row_offset_idx, min(cell.end_row_offset_idx, height)):
            for col in range(cell.start_col_offset_idx, min(cell.end_col_offset_idx, width)):
                origin[(row, col)] = index
    rows = [[cells[origin[(y, x)]].text.strip() if (y, x) in origin else "" for x in range(width)]
            for y in range(height)]
    lines = []
    for y in range(height):
        seen: list[int] = []
        for x in range(width):
            index = origin.get((y, x))
            if index is not None and index not in seen and cells[index].text.strip():
                seen.append(index)
        if seen:
            lines.append(" | ".join(cells[index].text.strip() for index in seen))
    data = {"rows": rows, "source": "docling",
            "cells": [{"row": cell.start_row_offset_idx, "col": cell.start_col_offset_idx,
                       "rowspan": cell.row_span, "colspan": cell.col_span,
                       "header": bool(cell.column_header or cell.row_header),
                       "text": cell.text.strip()} for cell in cells]}
    return data, "\n".join(lines)


class DoclingParser:
    formats = frozenset(EXTENSIONS)
    name, version = "docling", f"1.0+{DOCLING_VERSION}"

    def parse(self, payload: bytes, filename: str | None = None, *,
              format_name: str = "pdf") -> ParseResult:
        from docling.datamodel.base_models import DocumentStream
        from docling_core.types.doc import PictureItem, TableItem

        scanned = scanned_pages(payload) if format_name == "pdf" else {}
        stream = DocumentStream(name=f"document{EXTENSIONS[format_name]}", stream=io.BytesIO(payload))
        doc = converter().convert(stream).document
        blocks: list[Block] = []
        images: list[EmbeddedImage] = []
        by_page: dict[int | None, list[Block]] = {}
        page_order: list[int | None] = []

        def place(page: int | None, block: Block) -> None:
            if page not in by_page:
                by_page[page] = []
                page_order.append(page)
            by_page[page].append(block)

        for item, _level in doc.iterate_items():
            prov = item.prov[0] if getattr(item, "prov", None) else None
            page = prov.page_no if prov else None
            if page in scanned:
                continue  # the whole page goes to the LLM instead
            bbox = None
            if prov is not None and page in doc.pages:
                size = doc.pages[page].size
                box = prov.bbox.to_top_left_origin(page_height=size.height)
                bbox = normalized_bbox(box.l / size.width, box.t / size.height,
                                       box.r / size.width, box.b / size.height)
            path = f"docling/{item.self_ref.lstrip('#/')}"
            if isinstance(item, TableItem):
                data, text_value = table_block_data(item, doc)
                if text_value:
                    place(page, Block("", "table", text_value, page_number=page, source_path=path,
                                      bbox=bbox, table_data=data))
            elif isinstance(item, PictureItem):
                image = item.get_image(doc)
                if image is None:
                    continue
                output = io.BytesIO()
                image.convert("RGB").save(output, "PNG")
                key = f"picture-{len(images)}"
                images.append(EmbeddedImage(key, output.getvalue(), page, {"docling_ref": item.self_ref}))
                place(page, image_placeholder("", key, path, page, {"docling_ref": item.self_ref}))
            else:
                text_value = (getattr(item, "text", "") or "").strip()
                if text_value:
                    label = getattr(item.label, "value", str(item.label))
                    place(page, Block("", KINDS.get(label, "paragraph"), text_value, page_number=page,
                                      source_path=path, bbox=bbox, metadata={"docling_label": label}))
        for page, png in scanned.items():
            key = f"page-{page}"
            images.append(EmbeddedImage(key, png, page, {"rendered_page": page}))
            by_page.setdefault(page, []).append(image_placeholder("", key, f"page/{page}/image", page))
            if page not in page_order:
                page_order.append(page)
        ordered = sorted(page_order, key=lambda value: (value is None, value or 0))
        for page in ordered:
            blocks.extend(by_page[page])
        final = tuple(Block(_key("docling", index), block.kind, block.text, page_number=block.page_number,
                            source_path=block.source_path, bbox=block.bbox, table_data=block.table_data,
                            metadata=block.metadata) for index, block in enumerate(blocks))
        flags = ("scanned_pages",) if scanned else ()
        return ParseResult(self.name, self.version, "succeeded", final,
                           "\n\n".join(block.text for block in final if block.text), flags, tuple(images))
