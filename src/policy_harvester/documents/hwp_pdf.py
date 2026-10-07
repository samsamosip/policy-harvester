"""HWP/HWPX through rhwp: render to PDF, then parse that PDF like any other.

The PDF gives HWP documents the same page structure as PDFs (page-level picture transcription,
Docling tables) and is the preview shown in the admin, since browsers cannot display HWP.
The direct HWP parse stays as the reference: if the rendered PDF's text misses too much of it
(fonts, unsupported objects), the direct parse is used instead and the result says so.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

from .parsers import DerivedFile, DocumentParser, ParseResult
from .pdf_hybrid import PdfHybridParser

RHWP_BIN = os.environ.get("RHWP_BIN", "rhwp")
RHWP_VERSION = os.environ.get("RHWP_VERSION", "0.8.7")
# Without system fonts rhwp writes pages with no text at all; these ship in the image.
FONT_ARGS = ("--font-path", "/usr/share/fonts", "--fallback-sans", "Noto Sans CJK KR",
             "--fallback-serif", "Noto Serif CJK KR")
MIN_COVERAGE = 0.9
TIMEOUT_SECONDS = 180


def render_pdf(payload: bytes, format_name: str) -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        source, output = Path(directory) / f"input.{format_name}", Path(directory) / "output.pdf"
        source.write_bytes(payload)
        subprocess.run([RHWP_BIN, "export-pdf", str(source), "-o", str(output), *FONT_ARGS],
                       check=True, capture_output=True, timeout=TIMEOUT_SECONDS)
        return output.read_bytes()


def line_coverage(reference: str, candidate: str) -> float:
    """Share of the reference's lines (4+ characters) that appear in the candidate, ignoring
    whitespace: rendered PDFs often lose the spaces between words, not the words."""
    lines = {re.sub(r"\s+", "", line) for line in reference.splitlines()}
    lines = {line for line in lines if len(line) >= 4}
    if not lines:
        return 1.0
    text = re.sub(r"\s+", "", candidate)
    return sum(1 for line in lines if line in text) / len(lines)


class HwpPdfParser:
    formats = frozenset({"hwp", "hwpx"})
    name, version = "rhwp-pdf", f"1.0+rhwp{RHWP_VERSION}+{PdfHybridParser.version}"

    def __init__(self, direct: dict[str, DocumentParser]):
        self.direct = direct

    def parse(self, payload: bytes, filename: str | None = None, *, format_name: str = "hwp") -> ParseResult:
        reference = self.direct[format_name].parse(payload, filename)
        pdf = render_pdf(payload, format_name)
        derived = (DerivedFile("pdf_render", pdf, f"rhwp {RHWP_VERSION}"),)
        rendered = PdfHybridParser().parse(pdf)
        coverage = line_coverage(reference.text, rendered.text)
        if coverage < MIN_COVERAGE:
            return replace(reference, parser_name=self.name, parser_version=self.version, derived=derived,
                           quality_flags=(*reference.quality_flags, f"rhwp_low_coverage:{coverage:.2f}"))
        return replace(rendered, parser_name=self.name, parser_version=self.version, derived=derived)
