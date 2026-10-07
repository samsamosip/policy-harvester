from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import olefile


@dataclass(frozen=True)
class DetectedType:
    format: str
    mime: str
    extension: str
    confidence: str


SIGNATURES = (
    (b"%PDF-", "pdf", "application/pdf", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png", ".png"),
    (b"\xff\xd8\xff", "jpeg", "image/jpeg", ".jpg"),
    (b"GIF87a", "gif", "image/gif", ".gif"),
    (b"GIF89a", "gif", "image/gif", ".gif"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole", "application/x-ole-storage", ".bin"),
)


def _zip_type(payload: bytes) -> DetectedType:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return DetectedType("binary", "application/octet-stream", ".bin", "low")
    if "mimetype" in names and "Contents/content.hpf" in names:
        return DetectedType("hwpx", "application/vnd.hancom.hwpx", ".hwpx", "high")
    if "[Content_Types].xml" in names:
        if any(name.startswith("word/") for name in names):
            return DetectedType("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx", "high")
        if any(name.startswith("xl/") for name in names):
            return DetectedType("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx", "high")
        if any(name.startswith("ppt/") for name in names):
            return DetectedType("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx", "high")
    return DetectedType("zip", "application/zip", ".zip", "high")


def detect_type(payload: bytes, filename: str | None = None) -> DetectedType:
    for signature, format_name, mime, extension in SIGNATURES:
        if payload.startswith(signature):
            if format_name == "ole":
                try:
                    with olefile.OleFileIO(io.BytesIO(payload)) as document:
                        paths = document.listdir(streams=True, storages=True)
                        is_hwp = document.exists("FileHeader") and any(
                            path and path[0] == "BodyText" for path in paths)
                        is_doc = document.exists("WordDocument")
                        is_xls = document.exists("Workbook") or document.exists("Book")
                    if is_hwp:
                        return DetectedType("hwp", "application/x-hwp", ".hwp", "high")
                    if is_doc:
                        return DetectedType("doc", "application/msword", ".doc", "high")
                    if is_xls:
                        return DetectedType("xls", "application/vnd.ms-excel", ".xls", "high")
                except (OSError, IOError):
                    pass
                if filename and Path(filename).suffix.lower() == ".hwp":
                    return DetectedType("hwp", "application/x-hwp", ".hwp", "medium")
            return DetectedType(format_name, mime, extension, "high")
    if payload.startswith(b"PK\x03\x04"):
        return _zip_type(payload)
    stripped = payload[:1024].lstrip().lower()
    if stripped.startswith((b"<!doctype html", b"<html", b"<?xml")):
        return DetectedType("html", "text/html", ".html", "medium")
    suffix = Path(filename or "").suffix.lower()
    hints = {
        ".html": ("html", "text/html"),
        ".htm": ("html", "text/html"),
        ".hwp": ("hwp", "application/x-hwp"),
        ".hwpx": ("hwpx", "application/vnd.hancom.hwpx"),
        ".pdf": ("pdf", "application/pdf"),
        ".docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ".xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ".pptx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ".jpg": ("jpeg", "image/jpeg"),
        ".jpeg": ("jpeg", "image/jpeg"),
        ".png": ("png", "image/png"),
        ".zip": ("zip", "application/zip"),
    }
    if suffix in hints:
        name, mime = hints[suffix]
        return DetectedType(name, mime, suffix, "low")
    return DetectedType("binary", "application/octet-stream", suffix or ".bin", "low")
