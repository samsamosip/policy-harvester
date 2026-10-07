#!/usr/bin/env python3
"""인하대 공지사항의 저장된 HTML을 파싱하는 실행 가능한 예제.

Python 3.11+ / 의존성: lxml (pip install lxml)

    python inha_parser.py --kind list --html list.html \
        --url 'https://www.inha.ac.kr/bbs/kr/8/artclList.do?bbsClSeq=215'
    python inha_parser.py --kind detail --html detail.html \
        --url 'https://www.inha.ac.kr/bbs/kr/8/45514/artclView.do'

네트워크 요청, 다운로드, OCR, LLM 호출, DB 쓰기를 수행하지 않는다.
2026-10-06에 수집한 목록 2개와 상세 8개의 HTML로 확인한 선택자를 사용한다.
필수 구조가 바뀌면 LayoutChanged를 발생시켜 빈 데이터의 정상 저장을 막는다.

revision_signals는 제목/본문에 실제 나타난 표현의 관측값이다. 위치는 반환 JSON의
title 또는 content_text에 대한 Python 문자열 인덱스이다. '연장' 표제만으로 대상
신청기간을 확정하지 않으며, 같은 게시글/사업으로 병합하거나 필드를 덮어쓰지 않는다.
가까운 날짜/기간 이름과 조건절 여부를 함께 반환하므로 별도 resolver가 근거를 검토할
수 있다. 관측값 키 추가는 DB/정책 스키마 변경을 수행하지 않는다.

content_html은 보존용 HTML이다. 브라우저에 표시하기 전 별도 sanitize가 필요하다.
텍스트는 inline span을 임의의 공백으로 나누지 않는다. 블록/행 경계는 개행,
표의 셀 경계는 탭으로 남긴다. CSS나 JavaScript의 브라우저 렌더링을 재현하지는 않는다.

html_semantic_sha256은 조회수/메뉴/이전다음글/주석을 제외한 의미 정보의 해시다.
같은 URL의 첨부/이미지 바이너리 교체는 감지하지 못한다. 수집 단계에서 실제 파일을
다운로드하여 얻은 SHA-256과 함께 별도의 원문 revision fingerprint를 만들어야
한다. 파서/OCR 버전은 재처리 실행에 별도로 기록한다. 이 해시만 보고 파일
재검사를 영구히 생략하면 안 된다.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
from html import escape
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from lxml import etree, html


PARSER_VERSION = "inha-html-2026-10-06.2"
FINGERPRINT_VERSION = "inha-html-semantic-v1"
REVISION_OBSERVATION_VERSION = "notice-revision-observations-v1"
ARTICLE_PATH = re.compile(r"/bbs/([^/]+)/(\d+)/(\d+)/artclView\.do$")
DOWNLOAD_PATH = re.compile(r"/bbs/([^/]+)/(\d+)/(\d+)/download\.do$")
URL_CANDIDATE = re.compile(r"(?i)(?:https?://|www\.)[^\s<>\"']+")
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "caption", "dd", "details",
    "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
    "ol", "p", "pre", "section", "summary", "table", "tbody", "tfoot", "thead",
    "ul",
}
SKIP_TEXT_TAGS = {"script", "style", "template", "noscript"}
REVISION_MARKER_PATTERNS = (
    ("extension", re.compile(r"(?:(?:기간|기한|접수|신청|모집|제출)\s*)?연장")),
    ("correction", re.compile(r"정정|수정|변경")),
    ("cancellation", re.compile(r"취소|철회|중단|폐지")),
    ("reopened", re.compile(r"재\s*(?:모집|접수)|(?:모집|접수|신청)\s*재개|재개")),
    ("republication", re.compile(r"재\s*(?:공고|공지|안내|게시|등록)|다시\s*(?:공고|공지|게시)")),
    ("additional_round", re.compile(r"추가\s*(?:모집|선발|접수|공고|공지)")),
    ("replacement", re.compile(r"대체\s*(?:공고|공지|안내)|(?:이전|기존|종전)\s*(?:공고|공지)[^\n]{0,20}(?:대체|갈음)")),
    ("recruitment", re.compile(r"모집|선발\s*(?:공고|안내)|대상자\s*선발")),
)
# Keep these as raw mentions. Date normalization/window identification belongs to
# structured extraction, not a regex assumption about which deadline was extended.
DATE_MENTION = re.compile(
    r"(?<!\d)(?:20\d{2}|\d{2})\s*(?:년|[./-])\s*\d{1,2}\s*"
    r"(?:월|[./-])\s*\d{1,2}(?:\s*일|\.)?"
    r"(?:\s*\([월화수목금토일]\))?(?:\s*\d{1,2}\s*:\s*\d{2})?"
)
WINDOW_LABEL = re.compile(
    r"(?:접수|신청|제출|모집)\s*(?:기한|기간)|사업\s*기간|발표\s*(?:일|예정일)"
)
CONDITIONAL_QUALIFIER = re.compile(r"(?:경우|때에는|시에는|될\s*수|할\s*수|가능성|예정|변동\s*가능)")


class LayoutChanged(RuntimeError):
    """Expected selectors/fields are absent, ambiguous, or no longer parseable."""


def has_class(name: str) -> str:
    # XPath class-token match: 'artclView' must not match 'artclViewHead'.
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


def require_one(node: etree._Element, xpath: str, label: str) -> etree._Element:
    found = node.xpath(xpath)
    if len(found) != 1:
        raise LayoutChanged(f"{label}: expected 1 element, found {len(found)}")
    return found[0]


def parse_document(markup: str | bytes) -> etree._Element:
    # Comments may contain old/hidden metadata (notably 수정일). Never parse them.
    # huge_tree lifts libxml2's nesting limit; pasted Word markup can exceed 256 levels and the
    # default parser then silently drops the rest of the page.
    parser = html.HTMLParser(remove_comments=True, no_network=True, huge_tree=True)
    try:
        return html.document_fromstring(markup, parser=parser)
    except (etree.ParserError, ValueError) as exc:
        raise LayoutChanged(f"HTML document could not be parsed: {exc}") from exc


def normalize_inline(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def _render_text(node: etree._Element) -> str:
    if not isinstance(node.tag, str) or node.tag.lower() in SKIP_TEXT_TAGS:
        return ""
    tag = node.tag.lower()
    if tag == "br":
        return "\n"
    if tag == "tr":
        cells = [child for child in node if isinstance(child.tag, str)
                 and child.tag.lower() in {"td", "th"}]
        if cells:
            return "\n" + "\t".join(_render_text(cell).strip() for cell in cells) + "\n"
    # Concatenation preserves '<span>10</span><span>0만 원</span>' as '100만 원'.
    parts = [node.text or ""]
    for child in node:
        parts.append(_render_text(child))
        parts.append(child.tail or "")
    text = "".join(parts)
    return "\n" + text + "\n" if tag in BLOCK_TAGS else text


def content_text(node: etree._Element) -> str:
    value = unicodedata.normalize("NFC", _render_text(node)).replace("\r\n", "\n")
    value = value.replace("\r", "\n").replace("\xa0", " ")
    # Keep tabs and line breaks: they carry cell/paragraph boundaries.
    value = re.sub(r"[^\S\n\t]+", " ", value)
    value = re.sub(r" *\t *", "\t", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def element_text(node: etree._Element) -> str:
    return normalize_inline(content_text(node))


def semantic_dom(node: etree._Element):
    """표 구조 변경도 감지하되 글꼴/색상 등 단순 CSS 변화는 제외한다."""
    if not isinstance(node.tag, str) or node.tag.lower() in SKIP_TEXT_TAGS:
        return None
    retained = {key: value for key, value in node.attrib.items()
                if key in {"href", "src", "alt", "title", "rowspan", "colspan", "scope"}}
    if "line-through" in (node.get("style") or ""):
        retained["struck"] = "true"
    return {
        "tag": node.tag.lower(), "text": normalize_inline(node.text),
        "tail": normalize_inline(node.tail), "attributes": retained,
        "children": [value for child in node
                     if (value := semantic_dom(child)) is not None],
    }


def parse_integer(value: str, label: str, *, nullable: bool = False) -> int | None:
    cleaned = value.replace(",", "").strip()
    if not cleaned and nullable:
        return None
    if not re.fullmatch(r"\d+", cleaned):
        raise LayoutChanged(f"{label}: expected integer, got {value!r}")
    return int(cleaned)


def parse_date(value: str | None, label: str) -> str | None:
    if not value:
        return None
    match = re.fullmatch(r"(\d{4})[./-]\s*(\d{1,2})[./-]\s*(\d{1,2})\.?", value.strip())
    if not match:
        raise LayoutChanged(f"{label}: unrecognized date {value!r}")
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError as exc:
        raise LayoutChanged(f"{label}: invalid date {value!r}") from exc


def absolute_http_url(raw: str | None, page_url: str) -> str | None:
    if not raw or not raw.strip() or raw.strip().startswith("#"):
        return None
    resolved = urljoin(page_url, raw.strip())
    parsed = urlsplit(resolved)
    return resolved if parsed.scheme in {"http", "https"} and parsed.netloc else None


def article_identity(page_url: str) -> dict:
    parts = urlsplit(page_url)
    match = ARTICLE_PATH.fullmatch(parts.path)
    if parts.scheme not in {"http", "https"} or not parts.netloc or not match:
        raise ValueError("--url must be an absolute /bbs/{site}/{board}/{id}/artclView.do URL")
    site, board_id, article_id = match.groups()
    return {
        "site_key": site,
        "board_id": board_id,
        "external_article_id": article_id,
        "canonical_url": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
    }


def dl_values(container: etree._Element, xpath: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for dl in container.xpath(xpath):
        term = require_one(dl, "./dt", "metadata dt")
        definition = require_one(dl, "./dd", "metadata dd")
        label = element_text(term)
        if label in values:
            raise LayoutChanged(f"Duplicate metadata label: {label}")
        values[label] = element_text(definition)
    return values


def parse_list(markup: str | bytes, page_url: str) -> dict:
    doc = parse_document(markup)
    table = require_one(doc, f"//table[{has_class('artclTable')}]", "table.artclTable")
    tbody = require_one(table, "./tbody", "table.artclTable tbody")
    rows = tbody.xpath("./tr")
    if not rows:
        raise LayoutChanged("table.artclTable tbody has no rows")
    current = require_one(doc, f"//*[{has_class('_curPage')}]", "._curPage")
    total = require_one(doc, f"//*[{has_class('_totPage')}]", "._totPage")
    current_page = parse_integer(element_text(current), "current page")
    total_pages = parse_integer(element_text(total), "total pages")
    if not (1 <= current_page <= total_pages):
        raise LayoutChanged("Inconsistent current/total page values")

    category_ids = doc.xpath("//input[@name='bbsClSeq']/@value")
    category_id = next((value for value in category_ids if value), None)
    if category_id is None:
        category_id = parse_qs(urlsplit(page_url).query).get("bbsClSeq", [None])[0]
    active = doc.xpath(f"//a[{has_class('_active')} and contains(@onclick, 'bbsClSeq')]")
    category_label = element_text(active[0]) if len(active) == 1 else None

    items = []
    for order, row in enumerate(rows, start=1):
        link = require_one(row, f".//a[{has_class('artclLinkView')}]", f"list row {order} article link")
        article_url = absolute_http_url(link.get("href"), page_url)
        if article_url is None:
            raise LayoutChanged(f"list row {order}: article href missing or not HTTP(S)")
        try:
            identity = article_identity(article_url)
        except ValueError as exc:
            raise LayoutChanged(f"list row {order}: article URL route changed") from exc

        def cell_text(class_name: str) -> str:
            return element_text(require_one(row, f"./td[{has_class(class_name)}]", class_name))

        title = element_text(link)
        if not title:
            raise LayoutChanged(f"list row {order}: empty article title")
        attachment_nodes = row.xpath(f".//*[{has_class('attach_file')}]")
        attachment_count = 0
        if attachment_nodes:
            matches = re.search(r"(\d+)\s*개", " ".join(element_text(n) for n in attachment_nodes))
            if not matches:
                raise LayoutChanged(f"list row {order}: attachment count format changed")
            attachment_count = int(matches.group(1))
        items.append({
            **identity,
            "list_order": order,
            "list_number_text": cell_text("_artclTdNum"),
            "is_pinned": "headline" in (row.get("class") or "").split(),
            "title": title,
            "author": cell_text("_artclTdWriter") or None,
            "published_date": parse_date(cell_text("_artclTdRdate"), "list published date"),
            "view_count": parse_integer(cell_text("_artclTdAccess"), "list view count", nullable=True),
            "attachment_count_reported": attachment_count,
            # Globally pinned articles appear even in the scholarship filter.
            "source_category": None,
            "category_requires_detail": True,
        })
    return {
        "kind": "list",
        "parser_version": PARSER_VERSION,
        "source_url": page_url,
        "category_filter": {"id": category_id, "label": category_label},
        "current_page": current_page,
        "total_pages": total_pages,
        "items": items,
    }


def srcset_candidates(value: str):
    """Read URL/descriptor candidates without splitting commas inside data URLs."""
    rest = value
    while rest:
        rest = rest.lstrip(" \t\n\r\f,")
        if not rest:
            break
        match = re.match(r"[^ \t\n\r\f]+", rest)
        raw_url = match.group(0)
        rest = rest[len(raw_url):]
        if raw_url.endswith(","):
            yield raw_url.rstrip(","), ""
            continue
        depth, end = 0, 0
        while end < len(rest):
            char = rest[end]
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            elif char == "," and depth == 0:
                break
            end += 1
        descriptor = normalize_inline(rest[:end])
        rest = rest[end + 1:] if end < len(rest) else ""
        yield raw_url, descriptor


def collect_images(body: etree._Element, page_url: str) -> list[dict]:
    images = []
    for order, img in enumerate(body.xpath(".//img"), start=1):
        variants = []
        source_nodes = [img]
        picture = next((ancestor for ancestor in img.iterancestors()
                        if ancestor.tag == "picture"), None)
        if picture is not None:
            source_nodes.extend(picture.xpath("./source"))
        for source in source_nodes:
            for attribute in ("src", "data-src", "data-original", "srcset", "data-srcset"):
                raw = source.get(attribute)
                if not raw:
                    continue
                candidates = srcset_candidates(raw) if "srcset" in attribute else [(raw, "")]
                for raw_url, descriptor in candidates:
                    variants.append({
                        "element": source.tag,
                        "attribute": attribute,
                        "raw_url": raw_url,
                        "url": absolute_http_url(raw_url, page_url),
                        "descriptor": descriptor or None,
                        "media": source.get("media"),
                        "declared_type": source.get("type"),
                    })
        ancestors = img.xpath("ancestor::a[@href][1]")
        parent_href = ancestors[0].get("href") if ancestors else None
        images.append({
            "order": order,
            "src_url": absolute_http_url(img.get("src"), page_url),
            "data_src_url": absolute_http_url(img.get("data-src"), page_url),
            "alt": img.get("alt"),
            "title": img.get("title"),
            "width": img.get("width"),
            "height": img.get("height"),
            "parent_link_raw_href": parent_href,
            "parent_link_url": absolute_http_url(parent_href, page_url),
            "source_variants": variants,
        })
    return images


def collect_links(body: etree._Element, page_url: str) -> list[dict]:
    return [{
        "order": order,
        "raw_href": link.get("href"),
        "url": absolute_http_url(link.get("href"), page_url),
        "text": element_text(link),
        "title": link.get("title"),
        "contains_image": bool(link.xpath(".//img")),
        "purpose": "unknown",  # A foundation/news/Instagram URL is not automatically an application URL.
    } for order, link in enumerate(body.xpath(".//a[@href]"), start=1)]


def collect_plain_url_candidates(text: str, links: list[dict]) -> list[dict]:
    hrefs = {link["url"] for link in links if link["url"]}
    result = []
    for match in URL_CANDIDATE.finditer(text):
        raw = match.group(0).rstrip(".,;!?，。；、’”")
        for closer, opener in ((")", "("), ("]", "["), ("}", "{")):
            while raw.endswith(closer) and raw.count(closer) > raw.count(opener):
                raw = raw[:-1]
        inferred = raw.lower().startswith("www.")
        candidate = "https://" + raw if inferred else raw
        result.append({
            "text": raw,
            "url_candidate": candidate,
            "scheme_inferred": inferred,
            "text_start": match.start(),
            "text_end": match.start() + len(raw),
            "also_present_as_href": candidate in hrefs,
            "validated": False,
            "purpose": "unknown",
        })
    return result


def collect_attachments(form: etree._Element, page_url: str) -> list[dict]:
    candidates = [dl for dl in form.xpath("./dl")
                  if dl.xpath("./dt") and element_text(dl.xpath("./dt")[0]) == "첨부파일"]
    if len(candidates) != 1:
        raise LayoutChanged("Expected exactly one 첨부파일 field")
    dd = require_one(candidates[0], "./dd", "첨부파일 dd")
    attachments = []
    for order, link in enumerate(dd.xpath(".//a[@href]"), start=1):
        target = absolute_http_url(link.get("href"), page_url)
        match = DOWNLOAD_PATH.fullmatch(urlsplit(target).path) if target else None
        if not match:
            raise LayoutChanged(f"Attachment URL route changed: {link.get('href')!r}")
        site, board_id, file_id = match.groups()
        filename = element_text(link)
        if not filename:
            raise LayoutChanged("Empty attachment filename")
        attachments.append({
            "order": order,
            "site_key": site,
            "board_id": board_id,
            # This is a FILE ID, not the article ID from artclView.do.
            "external_file_id": file_id,
            "filename": filename,
            "extension_hint": Path(filename).suffix.lower() or None,
            "download_url": target,
            "sha256": None,  # Filled after downloading actual bytes.
        })
    if not attachments and "없습니다" not in element_text(dd):
        raise LayoutChanged("Attachment field has neither recognized links nor an explicit no-files message")
    return attachments


def marker_context(text: str, start: int, end: int, source_field: str) -> tuple[int, int]:
    """Return an exact source-relative paragraph, with a bounded long-paragraph fallback."""
    if source_field == "title":
        return 0, len(text)
    previous = text.rfind("\n\n", 0, start)
    following = text.find("\n\n", end)
    left = previous + 2 if previous >= 0 else 0
    right = following if following >= 0 else len(text)
    if right - left > 500:
        left, right = max(left, start - 200), min(right, end + 200)
    return left, right


def collect_revision_signals(title: str, text: str) -> list[dict]:
    """Observations only: lexical markers do not establish a revision or precedence."""
    signals = []
    for source_field, source in (("title", title), ("content_text", text)):
        local_signals = []
        for kind, pattern in REVISION_MARKER_PATTERNS:
            for match in pattern.finditer(source):
                # Real hy fixture says '수정 테이프': this is a stationery item,
                # not a correction to the notice. Do not turn it into an event.
                if kind == "correction" and re.match(r"수정\s*테이프", source[match.start():]):
                    continue
                left, right = marker_context(source, match.start(), match.end(), source_field)
                context = source[left:right]
                conditional = kind in {"extension", "correction", "cancellation"} and bool(
                    CONDITIONAL_QUALIFIER.search(context)
                )
                mentions = []
                for mention in DATE_MENTION.finditer(context):
                    mentions.append({
                        "text": mention.group(0),
                        "start": left + mention.start(),
                        "end": left + mention.end(),
                    })
                labels = [{
                    "text": label.group(0),
                    "start": left + label.start(),
                    "end": left + label.end(),
                } for label in WINDOW_LABEL.finditer(context)]
                local_signals.append({
                    "kind": kind,
                    "source_field": source_field,
                    "matched_text": match.group(0),
                    "start": match.start(),
                    "end": match.end(),
                    "context": context,
                    "context_start": left,
                    "context_end": right,
                    "evidence_type": "title_marker" if source_field == "title" else "body_marker",
                    "statement_scope": "conditional_clause" if conditional else "unqualified_text",
                    "possible_revision_candidate": not conditional and kind != "recruitment",
                    "nearby_date_mentions": mentions,
                    "nearby_window_labels": labels,
                    "target_window_resolved": False,
                    "candidate_only": True,
                })
        # Stable ordering; no timestamps, randomness, LLM interpretation or merge.
        signals.extend(sorted(local_signals, key=lambda item: (item["start"], item["end"], item["kind"])))
    return signals


def collect_same_board_references(links: list[dict], plain_candidates: list[dict], page_url: str) -> list[dict]:
    """References inside the body only. Previous/next navigation is never an input.

    Require the same hostname, site key and board ID. Cross-host mirrors need an
    explicitly configured canonical-source mapping in the ingestion/resolver layer.
    Even a direct link does not by itself establish replacement or equivalence.
    """
    source_identity = article_identity(page_url)
    source_host = urlsplit(page_url).hostname
    observations = []
    for index, link in enumerate(links):
        observations.append({
            "url": link["url"],
            "source_field": "links",
            "source_index": index,
            "reference_type": "body_href",
            "link_text": link["text"],
        })
    for index, candidate in enumerate(plain_candidates):
        observations.append({
            "url": candidate["url_candidate"],
            "source_field": "plain_text_url_candidates",
            "source_index": index,
            "reference_type": "body_plaintext_url_candidate",
            "text_start": candidate["text_start"],
            "text_end": candidate["text_end"],
        })
    references = []
    for observation in observations:
        url = observation["url"]
        if not url or urlsplit(url).hostname != source_host:
            continue
        try:
            target = article_identity(url)
        except ValueError:
            continue
        if (target["site_key"], target["board_id"]) != (
            source_identity["site_key"], source_identity["board_id"]
        ):
            continue
        references.append({
            **observation,
            "target_external_article_id": target["external_article_id"],
            "canonical_target_url": target["canonical_url"],
            "is_self_reference": target["external_article_id"] == source_identity["external_article_id"],
            "relationship": "unresolved",
            "candidate_only": True,
        })
    return references


def parse_detail(markup: str | bytes, page_url: str) -> dict:
    identity = article_identity(page_url)
    doc = parse_document(markup)
    # Authors sometimes paste another article's rendered page into the body, which nests a
    # second title/body/head/form inside .artclView. Only page-level elements count.
    outside_body = f"not(ancestor::*[{has_class('artclView')}])"
    title_node = require_one(doc, f"//h2[{has_class('artclViewTitle')} and {outside_body}]",
                             "h2.artclViewTitle")
    body = require_one(doc, f"//*[{has_class('artclView')} and {outside_body}]", ".artclView")
    head = require_one(doc, f"//*[{has_class('artclViewHead')} and {outside_body}]", ".artclViewHead")
    form = require_one(doc, f"//*[{has_class('artclItem')} and {has_class('viewForm')} and {outside_body}]",
                       ".artclItem.viewForm")
    title = element_text(title_node)
    if not title:
        raise LayoutChanged("Empty article title")
    metadata = dl_values(head, ".//dl")
    for label in ("분류", "작성일", "작성자", "조회수"):
        if label not in metadata:
            raise LayoutChanged(f"Required visible article metadata missing: {label}")
    fields = dl_values(form, "./dl")
    for label in ("담당자", "연락처", "이메일", "첨부파일"):
        if label not in fields:
            raise LayoutChanged(f"Required article form field missing: {label}")

    text = content_text(body)
    body_html = escape(body.text or "", quote=False) + "".join(
        html.tostring(child, encoding="unicode", with_tail=True) for child in body
    )
    images = collect_images(body, page_url)
    links = collect_links(body, page_url)
    plain_candidates = collect_plain_url_candidates(text, links)
    attachments = collect_attachments(form, page_url)
    contact = {
        "name": fields["담당자"] or None,
        "phone_text": fields["연락처"] or None,
        "email_text": fields["이메일"] or None,
    }
    # Preserve meaningful strikethrough signals; an old deadline may stay in text.
    struck_text = [element_text(node) for node in body.xpath(
        ".//del | .//s | .//strike | .//*[contains(@style, 'line-through')]"
    ) if element_text(node)]
    semantic = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "title": title,
        "source_category": metadata["분류"],
        "author": metadata["작성자"],
        "published_date": metadata["작성일"],
        "content_text": text,
        "content_structure": semantic_dom(body),
        "struck_text": struck_text,
        "contact": contact,
        "links": links,
        "images": images,
        "attachments": [{k: v for k, v in item.items() if k != "sha256"}
                        for item in attachments],
    }
    fingerprint = hashlib.sha256(json.dumps(
        semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return {
        "kind": "detail",
        "parser_version": PARSER_VERSION,
        "source_url": page_url,
        **identity,
        "title": title,
        "source_category": metadata["분류"] or None,
        "published_date": parse_date(metadata["작성일"], "published date"),
        "source_updated_date": parse_date(metadata.get("수정일"), "visible updated date"),
        "source_updated_date_basis": "visible_header" if metadata.get("수정일") else None,
        "author": metadata["작성자"] or None,
        "view_count": parse_integer(metadata["조회수"], "view count", nullable=True),
        "visible_metadata": metadata,
        "contact": contact,
        "content_html": body_html,
        "content_text": text,
        "struck_text": struck_text,
        "inline_images": images,
        "attachments": attachments,
        "links": links,
        "plain_text_url_candidates": plain_candidates,
        # Additive parser observations, intentionally excluded from the existing
        # semantic hash. Their extraction can evolve without changing raw evidence.
        "revision_observation_version": REVISION_OBSERVATION_VERSION,
        "revision_signals": collect_revision_signals(title, text),
        "same_board_article_references": collect_same_board_references(links, plain_candidates, page_url),
        "html_semantic_sha256": fingerprint,
        "fingerprint_version": FINGERPRINT_VERSION,
        "fingerprint_includes_asset_bytes": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kind", choices=("list", "detail"), required=True)
    parser.add_argument("--html", type=Path, required=True, help="Path to saved HTML; no network request")
    parser.add_argument("--url", required=True, help="Original absolute page URL for URL resolution")
    args = parser.parse_args(argv)
    try:
        payload = args.html.read_bytes()
        result = (parse_list if args.kind == "list" else parse_detail)(payload, args.url)
    except (LayoutChanged, OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
