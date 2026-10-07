from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from inha_parser import PARSER_VERSION, parse_detail, parse_list


@dataclass(frozen=True)
class DiscoveredNotice:
    external_id: str
    url: str
    listed: dict[str, Any]


class SourceAdapter(Protocol):
    key: str
    parser_version: str
    def list_page_url(self, source: dict[str, Any], page: int) -> str: ...
    def parse_list(self, payload: bytes, url: str) -> dict[str, Any]: ...
    def parse_detail(self, payload: bytes, url: str) -> dict[str, Any]: ...
    def assets(self, detail: dict[str, Any]) -> list[dict[str, Any]]: ...
    def includes(self, source: dict[str, Any], detail: dict[str, Any]) -> bool: ...


class InhaScholarshipAdapter:
    key = "inha_scholarship"
    parser_version = PARSER_VERSION

    def list_page_url(self, source: dict[str, Any], page: int) -> str:
        parts = urlsplit(source["list_url"])
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["page"] = str(page)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    def parse_list(self, payload: bytes, url: str) -> dict[str, Any]:
        return parse_list(payload, url)

    def parse_detail(self, payload: bytes, url: str) -> dict[str, Any]:
        return parse_detail(payload, url)

    def assets(self, detail: dict[str, Any]) -> list[dict[str, Any]]:
        assets: list[dict[str, Any]] = []
        for item in detail["attachments"]:
            assets.append({
                "occurrence_key": f"attachment:{item['external_file_id']}:{item['order']}",
                "ordinal": item["order"], "role": "attachment", "url": item["download_url"],
                "filename": item["filename"], "alt": None,
            })
        for item in detail["inline_images"]:
            candidates = [item.get("src_url"), item.get("data_src_url")]
            candidates.extend(value.get("url") for value in item.get("source_variants", []))
            url = next((value for value in candidates if value and not value.startswith("data:")), None)
            if url:
                assets.append({
                    "occurrence_key": f"inline:{item['order']}", "ordinal": item["order"],
                    "role": "inline_image", "url": url, "filename": None, "alt": item.get("alt"),
                })
        return assets

    def includes(self, source: dict[str, Any], detail: dict[str, Any]) -> bool:
        config = source.get("crawl_config") or {}
        allowed = set((config.get("category_names") or {}).values())
        return not allowed or detail.get("source_category") in allowed


def adapter_for(source: dict[str, Any]) -> SourceAdapter:
    name = (source.get("crawl_config") or {}).get("adapter", "inha_scholarship")
    if name == "inha_scholarship":
        return InhaScholarshipAdapter()
    raise ValueError(f"unknown source adapter: {name}")

