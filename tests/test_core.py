from __future__ import annotations

import io
import struct
import tempfile
import unittest
import zipfile
import zlib
from datetime import date
from pathlib import Path

from policy_harvester.ai.providers import validate_lenient_json
from policy_harvester.ai.schema import ExtractionBundle, Fact, LocalDateTime
from policy_harvester.crawling.adapters import InhaScholarshipAdapter
from policy_harvester.documents.parsers import Block, HtmlParser, HwpParser, normalized_bbox
from policy_harvester.documents.types import detect_type
from policy_harvester.pipeline.assembler import OpportunityAssembler, normalize_name
from policy_harvester.pipeline.edition import compare, core_name, signature
from policy_harvester.search import understand_query
from policy_harvester.security import validate_proxy_url
from policy_harvester.storage import FilesystemObjectStore, content_key
from pydantic import ValidationError
from revision_resolver import Candidate, FieldScope, VerifiedAmendment, resolve_field


def zip_payload(names: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in names.items():
            archive.writestr(name, value)
    return output.getvalue()


def ole_payload(streams: dict[tuple[str, ...], bytes]) -> bytes:
    """Build a minimal CFB v3 file with up to two storage levels and >=4096-byte streams.

    Large streams live in regular sectors, so no mini stream or mini FAT is needed.
    """
    end, free, fat_sector, no_stream = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF
    entries: list[dict] = [{"name": "Root Entry", "type": 5, "children": []}]
    storages: dict[str, int] = {}
    for path, data in streams.items():
        assert len(data) >= 4096 and len(data) % 512 == 0
        parent = 0
        for storage in path[:-1]:
            if storage not in storages:
                storages[storage] = len(entries)
                entries.append({"name": storage, "type": 1, "children": []})
                entries[parent]["children"].append(storages[storage])
            parent = storages[storage]
        entries.append({"name": path[-1], "type": 2, "children": [], "data": data})
        entries[parent]["children"].append(len(entries) - 1)
    dir_sectors = (len(entries) * 128 + 511) // 512
    fat = [fat_sector] + [*(range(2, dir_sectors + 1)), end]
    body = b""
    for entry in entries:
        if "data" in entry:
            start, count = len(fat), len(entry["data"]) // 512
            entry["start"] = start
            fat.extend([*range(start + 1, start + count), end])
            body += entry["data"]
    assert len(fat) <= 128
    fat += [free] * (128 - len(fat))
    # Siblings form a right-leaning chain sorted by CFB name order (length, then uppercase).
    for entry in entries:
        children = sorted(entry["children"], key=lambda i: (len(entries[i]["name"]),
                                                            entries[i]["name"].upper()))
        entry["child"] = children[0] if children else no_stream
        for left, right in zip(children, children[1:]):
            entries[left]["right"] = right
    directory = b""
    for entry in entries:
        name = (entry["name"] + "\0").encode("utf-16-le")
        directory += (name.ljust(64, b"\0") + struct.pack(
            "<HBBIII16sIQQIQ", len(name), entry["type"], 1, no_stream,
            entry.get("right", no_stream), entry["child"], b"\0" * 16, 0, 0, 0,
            entry.get("start", end), len(entry.get("data", b""))))
    directory = directory.ljust(dir_sectors * 512, b"\0")
    header = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 16 + struct.pack(
        "<HHHHH6sIIIIIIIII", 0x3E, 3, 0xFFFE, 9, 6, b"\0" * 6, 0, 1, 1, 0, 4096,
        end, 0, end, 0) + struct.pack("<I", 0) + struct.pack("<I", free) * 108)
    return header + struct.pack("<128I", *fat) + directory + body


class AiReviewTests(unittest.TestCase):
    def test_only_judgement_calls_are_reviewed(self):
        from policy_harvester.pipeline.ai_review import reviewable

        self.assertTrue(reviewable("identity_uncertain", {"operation": "same_notice_unmatched"}))
        self.assertTrue(reviewable("revision_candidate", {}))
        self.assertTrue(reviewable("other", {"quality_flags": [], "coverage": "partial"}))
        self.assertFalse(reviewable("other", {"operation": "auto_publish_blocked"}))
        self.assertFalse(reviewable("parsing_failed", {"error": "x"}))

    def test_only_confident_verdicts_act(self):
        from policy_harvester.pipeline.ai_review import ReviewVerdict

        for verdict in ("dismiss", "fix", "merge", "revise"):
            self.assertTrue(ReviewVerdict(verdict=verdict, confidence="high", reason="r").acted)
            self.assertFalse(ReviewVerdict(verdict=verdict, confidence="medium", reason="r").acted)
        self.assertFalse(ReviewVerdict(verdict="escalate", confidence="high", reason="r").acted)

    def test_corrections_are_typed_and_whitelisted(self):
        from datetime import date
        from decimal import Decimal

        from policy_harvester.pipeline.ai_review import ActionRefused, _coerce, _coerce_column

        self.assertEqual(_coerce_column("eligibility_summary", "뚜렷한 목표의식"), "뚜렷한 목표의식")
        self.assertEqual(_coerce_column("selection_capacity", "2"), 2)
        self.assertEqual(_coerce_column("selection_capacity_scope", "university_nomination"), "university_nomination")
        self.assertEqual(_coerce("date", "2026-09-18", "windows/x/end_date"), date(2026, 9, 18))
        self.assertEqual(_coerce("amount", "3,500,000원", "benefits/x/amount_max"), Decimal("3500000"))
        for column, value in (("selection_capacity", -1), ("selection_capacity_scope", "everyone"),
                              ("publication_state", "published"), ("data_quality_status", "complete")):
            with self.assertRaises(ActionRefused):
                _coerce_column(column, value)
        with self.assertRaises(ActionRefused):
            _coerce("date", "9월 18일", "windows/x/end_date")


class TransientErrorTests(unittest.TestCase):
    def test_streamed_gateway_timeout_is_transient(self):
        from policy_harvester.ai.providers import is_transient

        class APIError(Exception):
            code = None

        self.assertTrue(is_transient(APIError("error code: 504")))
        self.assertFalse(is_transient(APIError("Error code: 404 - model_not_found")))
        self.assertFalse(is_transient(ValueError("invalid JSON")))


class StorageAndDetectionTests(unittest.TestCase):
    def test_ole_structure_identifies_hwp_without_extension(self) -> None:
        hwp = ole_payload({("FileHeader",): b"HWP Document File".ljust(4096, b"\0"),
                           ("BodyText", "Section0"): b"\0" * 4096})
        self.assertEqual(detect_type(hwp, "download").format, "hwp")
        self.assertEqual(detect_type(hwp, "download").confidence, "high")
        self.assertEqual(detect_type(hwp, "misleading.doc").format, "hwp")
        word = ole_payload({("WordDocument",): b"\0" * 4096})
        self.assertEqual(detect_type(word, "download.hwp").format, "doc")
        workbook = ole_payload({("Workbook",): b"\0" * 4096})
        self.assertEqual(detect_type(workbook, "download").format, "xls")
        other = ole_payload({("PowerPoint Document",): b"\0" * 4096})
        self.assertEqual(detect_type(other, "download").format, "ole")
        self.assertEqual(detect_type(other, "legacy.hwp").confidence, "medium")

    def test_pdf_bbox_is_clamped_or_dropped(self) -> None:
        self.assertEqual(normalized_bbox(-0.1, 0.2, 0.5, 1.3), (0.0, 0.2, 0.5, 1.0))
        self.assertIsNone(normalized_bbox(0.3, 0.2, 0.3, 0.4))
        self.assertIsNone(normalized_bbox(1.2, 0.2, 1.5, 0.4))

    def test_hwp_bindata_pictures_become_transcription_requests(self) -> None:
        from PIL import Image
        from policy_harvester.documents.parsers import ParseResult, image_placeholder
        from policy_harvester.documents.vision import merge_transcriptions

        def png(width: int, height: int) -> bytes:
            output = io.BytesIO()
            Image.new("RGB", (width, height), "white").save(output, "PNG")
            return output.getvalue()

        def padded(data: bytes) -> bytes:
            return data.ljust(max(4096, -(-len(data) // 512) * 512), b"\0")

        compressor = zlib.compressobj(wbits=-15)
        poster = compressor.compress(png(400, 300)) + compressor.flush()
        header = b"HWP Document File".ljust(36, b"\0") + struct.pack("<I", 1)
        document = ole_payload({("FileHeader",): padded(header),
                                ("BodyText", "Section0"): b"\0" * 4096,
                                ("BinData", "BIN0001.png"): padded(poster),
                                ("BinData", "BIN0002.ole"): b"\1" * 4096})
        images = HwpParser()._embedded_images(document)
        self.assertEqual([image.key for image in images], ["BIN0001.png"])
        self.assertEqual(Image.open(io.BytesIO(images[0].data)).size, (400, 300))

        base = ParseResult("p", "1", "succeeded", (
            Block("x:0", "paragraph", "본문"),
            image_placeholder("x:1", "a", "page/2/image", 2),
            image_placeholder("x:2", "b", "bindata/b"),
            Block("x:3", "paragraph", "끝")), "본문\n\n끝")
        merged = merge_transcriptions(base, {"a": "포스터 제목\n\n| 금액 |\n|---|\n| 100만원 |", "b": None}, "m")
        self.assertEqual([block.kind for block in merged.blocks], ["paragraph", "paragraph", "table", "paragraph"])
        self.assertEqual(merged.blocks[1].page_number, 2)
        self.assertEqual([block.stable_key for block in merged.blocks], ["x:000000", "x:000001", "x:000002", "x:000003"])
        self.assertIn("llm_transcription", merged.quality_flags)

    def test_vision_transcription_becomes_blocks_and_tables(self) -> None:
        from PIL import Image
        from policy_harvester.documents.vision import prepare_image, transcription_blocks

        blocks = transcription_blocks(
            "우양재단 장학생 모집\n~ 4. 9. 목\n\n| 구분 | 금액 |\n|---|---|\n| 1학기 | 150만원 |\n\n신청방법",
            model="m", source_prefix="vision/block", page_number=2)
        self.assertEqual([block.kind for block in blocks], ["paragraph", "table", "paragraph"])
        self.assertEqual(blocks[1].table_data["rows"], [["구분", "금액"], ["1학기", "150만원"]])
        self.assertEqual(blocks[1].text, "구분 | 금액\n1학기 | 150만원")
        self.assertTrue(all(block.ocr_used and block.page_number == 2 for block in blocks))
        styled = transcription_blocks("# 지원 상세\n\n| 금액 |\n|---|\n| (1학기) **3**<br>(2학기) 2 |",
                                      model="m", source_prefix="v")
        self.assertEqual([(block.kind, block.text) for block in styled],
                         [("heading", "지원 상세"), ("table", "금액\n(1학기) 3 / (2학기) 2")])
        icon, poster = io.BytesIO(), io.BytesIO()
        Image.new("RGB", (64, 64)).save(icon, "PNG")
        Image.new("RGB", (3000, 1500)).save(poster, "PNG")
        self.assertIsNone(prepare_image(icon.getvalue()))
        self.assertEqual(Image.open(io.BytesIO(prepare_image(poster.getvalue()))).size, (2048, 1024))

    def test_vision_html_tables_keep_merged_cells_once(self) -> None:
        from policy_harvester.documents.vision import transcription_blocks

        blocks = transcription_blocks(
            "# 장학금 현황\n```html\n<table><tr><th>구분</th><th>학교급</th><th>금액</th></tr>"
            "<tr><td rowspan=\"2\">일반</td><td>고등</td><td rowspan=\"2\">34,000</td></tr>"
            "<tr><td>대학<br>(신입)</td></tr>"
            "<tr><td>합계</td><td colspan=\"2\">70명</td></tr></table>\n```\n※ 단위: 천원",
            model="m", source_prefix="v")
        self.assertEqual([block.kind for block in blocks], ["heading", "table", "paragraph"])
        table = blocks[1]
        self.assertEqual(table.text, "구분 | 학교급 | 금액\n일반 [2행 병합] | 고등 | 34,000 [2행 병합]\n"
                                     "↑ | 대학 / (신입) | ↑\n합계 | 70명 [2열 병합]")
        self.assertEqual(table.table_data["rows"][2], ["일반", "대학 / (신입)", "34,000"])
        self.assertEqual(table.table_data["source"], "html")
        self.assertEqual(blocks[2].text, "※ 단위: 천원")
        nested = transcription_blocks(
            "<table><tr><td>일반</td><td><table><tr><td>초등</td></tr></table></td></tr>"
            "<tr><td>합계</td><td>9</td></tr></table>\n끝", model="m", source_prefix="v")
        self.assertEqual([block.kind for block in nested], ["table", "paragraph"])
        self.assertNotIn("<", nested[0].text)
        self.assertIn("합계 | 9", nested[0].text)
        from lxml import html as lxml_html
        from policy_harvester.documents.parsers import table_grid
        grid = table_grid(lxml_html.fragment_fromstring(
            "<table><tr><td>장학</td><td>1차</td><td>2차</td><td>금액</td></tr>"
            "<tr><td>A</td><td>서류</td><td></td><td>300</td></tr><tr><td>B</td><td></td><td></td></tr></table>"))
        self.assertEqual(grid["text"], "장학 | 1차 | 2차 | 금액\nA | 서류 |  | 300\nB")

    def test_html_fragment_body_is_not_binary(self) -> None:
        fragment = "<p style='margin:0'>장학생 선발 안내</p>".encode()
        self.assertEqual(detect_type(fragment, "공고 제목 2026.12.1.html").format, "html")

    def test_content_addressed_store_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemObjectStore(Path(directory))
            first = store.put(b"same bytes")
            second = store.put(b"same bytes")
            self.assertEqual(first.storage_key, second.storage_key)
            self.assertEqual(store.get(first.storage_key), b"same bytes")
            self.assertTrue(store.exists(content_key(first.sha256)))

    def test_magic_and_zip_structure_override_filename(self) -> None:
        self.assertEqual(detect_type(b"%PDF-1.7\n", "wrong.jpg").format, "pdf")
        docx = zip_payload({"[Content_Types].xml": b"x", "word/document.xml": b"x"})
        self.assertEqual(detect_type(docx, "download.bin").format, "docx")
        hwpx = zip_payload({"mimetype": b"application/hwp+zip",
                            "Contents/content.hpf": b"x",
                            "Contents/section0.xml": b"x"})
        self.assertEqual(detect_type(hwpx, "download.exe").format, "hwpx")


class ParsingAndSchemaTests(unittest.TestCase):
    def test_html_blocks_preserve_table_rows(self) -> None:
        result = HtmlParser().parse(
            b"<html><body><h2>Title</h2><p>Body</p><table><tr><th>A</th><th>B</th>"
            b"</tr><tr><td>1</td><td>2</td></tr></table></body></html>"
        )
        self.assertEqual([block.kind for block in result.blocks],
                         ["heading", "paragraph", "table"])
        self.assertEqual(result.blocks[-1].table_data["rows"][1], ["1", "2"])

    def test_struck_out_text_is_marked(self) -> None:
        result = HtmlParser().parse("<p>마감: <del>9. 12.(금)</del> 9. 19.(금) "
                                    "<span style='text-decoration: line-through'><b>17:00</b></span>"
                                    " / 1<span>7:00</span><br>문의</p>".encode())
        self.assertEqual(result.blocks[0].text, "마감: ~~9. 12.(금)~~ 9. 19.(금) ~~17:00~~ / 17:00 문의")

    def test_html_table_spans_and_cell_paragraphs(self) -> None:
        result = HtmlParser().parse(
            "<div><table><tr><td><p>바깥 레이아웃</p>"
            "<table><tr><td rowspan='2'><p>행정직</p></td><td><p>총무</p></td></tr>"
            "<tr><td colspan='x'><p>회계</p></td></tr></table>"
            "</td></tr></table></div>".encode()
        )
        self.assertEqual([block.kind for block in result.blocks], ["paragraph", "table"])
        table = result.blocks[1].table_data
        self.assertEqual(table["rows"], [["행정직", "총무"], ["행정직", "회계"]])
        self.assertEqual(table["cells"][0]["rowspan"], 2)
        self.assertEqual(result.blocks[1].text, "행정직 | 총무\n행정직 | 회계")
        repeated = HtmlParser().parse(
            "<table><tr><td colspan='2'>병합</td><td>X</td></tr>"
            "<tr><td>O</td><td>O</td><td>X</td></tr></table>".encode())
        self.assertEqual(repeated.blocks[0].text, "병합 | X\nO | O | X")

    def test_end_of_day_2400_is_next_midnight(self) -> None:
        value = LocalDateTime.model_validate({"date": "2026-10-26", "time": "24:00:00",
                                              "timezone": "Asia/Seoul", "precision": "minute",
                                              "raw_text": "10. 26.(월) 24:00까지"})
        self.assertEqual((value.date, str(value.time)), (date(2026, 10, 27), "00:00:00"))
        self.assertEqual(value.raw_text, "10. 26.(월) 24:00까지")

    def test_bundle_normalization_never_upgrades_facts(self) -> None:
        from policy_harvester.ai.schema import normalize_fact_states
        data, notes = normalize_fact_states({"a": {"value": "x", "state": "inferred", "evidence": [1]},
                                             "b": [{"value": False, "state": "not_found", "evidence": []}],
                                             "c": {"value": 3, "state": "stated", "evidence": [2]}})
        self.assertEqual(data["a"], {"value": None, "state": "unknown", "evidence": [1]})
        self.assertEqual(data["b"][0]["value"], None)
        self.assertEqual(data["c"]["value"], 3)
        self.assertEqual(len(notes), 2)
        data, notes = normalize_fact_states({
            "name": {"value": "x", "state": "stated", "evidence": []},
            "currency": {"value": "KRW", "state": "stated", "evidence": []},
            "patches": [{"field_path": "application_windows/main/end", "scope_key": "main"}]})
        self.assertEqual(data["name"], {"value": None, "state": "unknown", "evidence": []})
        self.assertEqual(data["currency"], "KRW")
        self.assertEqual(data["patches"][0]["field_path"], "/application_windows/main/end")

    def test_placeholder_headcount_means_digits_not_unreadable(self) -> None:
        from policy_harvester.ai.schema import interpret_placeholder_counts
        ref = [{"block_id": "b", "quote": "선발인원 : 00명"}]
        selection = interpret_placeholder_counts({
            "recruitment_count": {"value": None, "state": "parsing_failed", "evidence": ref},
            "final_selection_count": {"value": 30, "state": "stated", "evidence": ref},
            "university_nomination_count": {"value": None, "state": "not_found", "evidence": []}})
        self.assertEqual(selection["recruitment_count"]["state"], "unknown")
        self.assertEqual(selection["final_selection_count"]["value"], 30)
        self.assertEqual(selection["count_text"], "두 자리 수 (원문: 00명)")
        untouched = interpret_placeholder_counts({"recruitment_count": {
            "value": None, "state": "parsing_failed", "evidence": [{"quote": "100명"}]}})
        self.assertNotIn("count_text", untouched)

    def test_lenient_json_only_fixes_syntax(self) -> None:
        fact = validate_lenient_json(Fact[str], '```json\n{"value": "a, ]", "state": "stated",'
                                     ' "evidence": [{"block_id": "b", "quote": "q\\",}",'
                                     ' "char_start": null, "char_end": null,'
                                     ' "evidence_type": "explicit", "confidence": 1,},],}\n```')
        self.assertEqual(fact.value, "a, ]")
        self.assertEqual(fact.evidence[0].quote, 'q",}')
        with self.assertRaises(ValidationError):
            validate_lenient_json(Fact[str], '{"value": "x", "state": "not_found", "evidence": [],}')

    def test_missing_state_does_not_mean_unrestricted(self) -> None:
        with self.assertRaises(ValidationError):
            Fact[str](value="invented", state="not_found", evidence=[])
        valid = Fact[str](value=None, state="not_found", evidence=[])
        self.assertIsNone(valid.value)

    def test_imprecise_date_can_keep_raw_text_without_exact_date(self) -> None:
        value = LocalDateTime(date=None, time=None, timezone="Asia/Seoul",
                              precision="month", raw_text="2027년 1월 중")
        self.assertIsNone(value.date)


class SearchIdentityAndProxyTests(unittest.TestCase):
    def test_query_understanding_only_emits_allowed_filters(self) -> None:
        result = understand_query("이번 달 안에 마감하는 대학생 생활비 장학금 100만원 이상",
                                  today=date(2026, 10, 6))
        self.assertEqual(result["student_type"], "undergraduate")
        self.assertEqual(result["benefit_type"], "living_cost")
        self.assertEqual(result["min_amount"], 1_000_000)
        self.assertEqual(result["application_end_to"], date(2026, 10, 31))
        self.assertNotIn("sql", result)

    def test_proxy_schemes_and_pagination(self) -> None:
        for value in ("http://user:pass@example.test:8080",
                      "https://example.test:8443", "socks5://example.test:1080"):
            validate_proxy_url(value)
        with self.assertRaises(ValueError):
            validate_proxy_url("ftp://example.test")
        adapter = InhaScholarshipAdapter()
        url = adapter.list_page_url({"list_url": "https://example.test/list?bbsClSeq=215"}, 3)
        self.assertIn("page=3", url)
        self.assertIn("bbsClSeq=215", url)

    def test_explicit_revision_wins_at_field_scope(self) -> None:
        scope = FieldScope("program", "2026", "/application/end", "student")
        old = Candidate("old", scope, "2026-09-23", "n1", "v1", ("e1",))
        new = Candidate("new", scope, "2026-10-08", "n2", "v2", ("e2",))
        directive = VerifiedAmendment("extend", scope, "extension", "new", ("old",),
                                      ("e3",), True, True, True, True)
        result = resolve_field([old, new], [directive])
        self.assertEqual(result.status, "resolved_explicit_update")
        self.assertEqual(result.selected_value, "2026-10-08")
        self.assertEqual(result.superseded_candidate_keys, ("old",))

    def test_edition_compare_tolerates_wording_but_not_editions(self) -> None:
        def verdict(first, second, left=None, right=None):
            return compare(signature(first, **(left or {})), signature(second, **(right or {}))).verdict

        # Reworded name of the same scholarship (provider added) is the same edition.
        self.assertEqual(verdict("2027년 의생명과학분야 대학 장학생 선발",
                                 "2027년 아산사회복지재단 의생명과학분야 대학 장학생 선발",
                                 right={"provider": "아산사회복지재단"}), "same")
        self.assertEqual(verdict("[학부-교외장학] 2027년 미래인재 장학금 선발 안내",
                                 "2027년도 미래인재장학생 모집"), "same")
        # Same name, different edition.
        self.assertEqual(verdict("2026년 미래인재 장학금", "2027년 미래인재 장학금"), "different_edition")
        self.assertEqual(verdict("미래인재 장학금", "미래인재 장학금",
                                 left={"academic_year": 2026}, right={"academic_year": 2027}),
                         "different_edition")
        self.assertEqual(verdict("26-1학기 미래인재 장학금", "2026-2학기 미래인재 장학금"), "different_edition")
        self.assertEqual(verdict("2026년 미래인재 장학금 1차", "2026년 미래인재 장학금 2차"), "different_edition")
        self.assertEqual(verdict("'26년 후반기 67, 68기 학군사관후보생 모집",
                                 "'26년 후반기 69기 학군사관후보생 모집"), "different_edition")
        self.assertEqual(verdict("미래인재 장학금", "미래인재 장학금",
                                 left={"deadlines": [date(2026, 3, 10)]},
                                 right={"deadlines": [date(2026, 9, 30)]}), "different_edition")
        # A one-sided round marker or a missing anchor is never "same".
        self.assertEqual(verdict("2026년 미래인재 장학금", "2026년 미래인재 장학금 추가모집"), "uncertain")
        self.assertEqual(verdict("미래인재 장학금", "미래인재 장학금"), "uncertain")
        self.assertEqual(verdict("미래인재 장학금", "미래인재 장학금",
                                 left={"deadlines": [date(2026, 3, 10)]},
                                 right={"deadlines": [date(2026, 3, 10)]}), "same")
        # Unrelated programs.
        self.assertEqual(verdict("2026년 가온 장학금", "2026년 나래 장학금"), "different")
        self.assertEqual(core_name("2027년 미래인재 장학금 [기간연장]"), core_name("2027년도 미래인재 장학생"))

    def test_term_ignores_year_digits(self) -> None:
        term = OpportunityAssembler._term
        self.assertEqual([term("2021년 2학기"), term("2027년 1학기"), term("26-2학기"),
                          term("2"), term("겨울학기"), term("2026")],
                         ["fall", "spring", "fall", "fall", "winter", "other"])

    def test_name_normalization_removes_revision_markers(self) -> None:
        self.assertEqual(normalize_name("[기간연장] 2026 꿈 장학금"),
                         normalize_name("2026 꿈 장학금"))


class SchemaV2Tests(unittest.TestCase):
    def test_v2_dates_counts_and_short_block_ids(self):
        from policy_harvester.ai.schema_v2 import (DateValue, ExtractionBundleV2, quote_offsets,
                                                   restore_block_ids, short_block_ids)

        self.assertEqual(DateValue.model_validate({"date": "2026-04-30", "time": "24:00"}).date.isoformat(),
                         "2026-05-01")
        self.assertEqual(DateValue.model_validate({"date": None, "month": "2026-11", "time": None}).precision,
                         "month")
        with self.assertRaises(ValueError):
            DateValue.model_validate({"date": None, "month": None, "time": "10:00"})
        data, _ = ExtractionBundleV2.normalize_payload({"opportunities": [{"selection": {
            "selection_count": {"value": None, "state": "parsing_failed",
                                "evidence": [{"block_id": "b3", "quote": "00명 선발", "evidence_type": "explicit"}]},
            "count_text": None}}]})
        selection = data["opportunities"][0]["selection"]
        self.assertEqual(selection["selection_count"]["state"], "unknown")
        self.assertEqual(selection["count_text"], "두 자리 수 (원문: 00명)")
        forward, backward = short_block_ids({"uuid-1": "가", "uuid-2": "나"})
        self.assertEqual(forward, {"uuid-1": "b1", "uuid-2": "b2"})
        self.assertEqual(restore_block_ids([{"block_id": "b2", "quote": "나"}], backward),
                         [{"block_id": "uuid-2", "quote": "나"}])
        self.assertEqual(quote_offsets("4.  30.", "마감 4. 30. 까지"), (3, 9))
        from policy_harvester.ai.schema_v2 import with_title_revision
        empty = ExtractionBundleV2.model_validate({"schema_version": "scholarship_v2", "opportunities": [],
                                                   "revisions": [], "coverage": "complete", "warnings": []})
        flagged = with_title_revision(empty, "t1", "[기간연장] 2027년 나래재단 장학생 선발 안내")
        self.assertEqual((flagged.revisions[0].kind, flagged.revisions[0].marker_evidence[0].quote),
                         ("extension", "기간연장"))
        self.assertEqual(with_title_revision(empty, "t1", "2027년 나래재단 장학생 선발 안내").revisions, [])
        self.assertEqual(with_title_revision(empty, "t1", "[재게시] 나래재단").revisions[0].kind, "repost")


class ExtractionRoutingTests(unittest.TestCase):
    def test_extraction_target_parameters_and_crosscheck_rule(self):
        from pydantic import SecretStr

        from policy_harvester.ai.providers import ProviderRegistry, structured_parameters
        from policy_harvester.ai.schema_v2 import ExtractionBundleV2
        from policy_harvester.config import Settings
        from policy_harvester.worker import needs_crosscheck

        base = Settings(database_url="postgresql+psycopg://u:p@h/db", llm_provider="openai_compatible",
                        llm_model="gemini", llm_base_url="https://gateway.example/v1",
                        llm_api_key=SecretStr("k1"), llm_max_output_tokens=131072,
                        extraction_llm_provider=None, extraction_llm_model=None,
                        extraction_llm_base_url=None, extraction_llm_api_key=None,
                        extraction_reasoning_effort=None, extraction_crosscheck_model=None)
        self.assertIsNone(ProviderRegistry(base).crosscheck())
        target = ProviderRegistry(base).extraction()
        self.assertEqual((target.provider_name, target.model, target.parameters),
                         ("openai_compatible", "gemini", {"max_tokens": 131072}))
        routed = base.model_copy(update={
            "extraction_llm_model": "muse", "extraction_llm_base_url": "https://openrouter.ai/api/v1",
            "extraction_llm_api_key": SecretStr("k2"), "extraction_reasoning_effort": "high",
            "extraction_crosscheck_model": "gemini"})
        target = ProviderRegistry(routed).extraction()
        self.assertEqual(target.model, "muse")
        self.assertEqual(target.parameters, {"max_tokens": 131072, "reasoning_effort": "high",
                                             "extra_body": {"usage": {"include": True}}})
        self.assertEqual(str(target.provider.client.base_url), "https://openrouter.ai/api/v1/")
        self.assertEqual(ProviderRegistry(routed).crosscheck().model, "gemini")
        self.assertEqual(structured_parameters("google", base, None), {"max_output_tokens": 131072})

        def bundle(items: int):
            return ExtractionBundleV2.model_construct(opportunities=[object()] * items, coverage="complete")
        short = {"b1": "가" * 100}
        self.assertFalse(needs_crosscheck(short, bundle(1)))
        self.assertTrue(needs_crosscheck(short, bundle(0)))
        self.assertTrue(needs_crosscheck(short, bundle(2)))
        self.assertTrue(needs_crosscheck({"b1": "가" * 20000}, bundle(1)))


class ApplicationStatusTests(unittest.TestCase):
    def test_status_comes_from_windows_and_time(self):
        from datetime import date, datetime, time

        from policy_harvester.api import application_status

        def window(start, end, end_time=None, kind="application", rule="fixed"):
            return {"window_kind": kind, "start_date": start, "start_time": None, "end_date": end,
                    "end_time": end_time, "closing_rule": rule}
        now = datetime(2025, 9, 19, 17, 30)
        self.assertEqual(application_status([window(date(2025, 8, 25), date(2025, 9, 19))], None, now), "active")
        self.assertEqual(application_status([window(date(2025, 8, 25), date(2025, 9, 19), time(17))], None, now),
                         "closed")
        self.assertEqual(application_status([window(date(2025, 10, 1), date(2025, 10, 9))], None, now), "upcoming")
        self.assertEqual(application_status([window(None, None)], None, now), "unknown")
        self.assertEqual(application_status([window(date(2025, 1, 1), None, rule="rolling")], None, now), "active")
        # A school-to-foundation deadline does not decide the student's status.
        self.assertEqual(application_status([window(date(2025, 8, 1), date(2025, 8, 31)),
                                             window(None, date(2025, 9, 30), kind="nomination")], None, now),
                         "closed")
        self.assertEqual(application_status([window(date(2025, 8, 25), date(2025, 9, 30))], "cancelled", now),
                         "cancelled")


class OnnxEmbeddingSpecTests(unittest.TestCase):
    def test_model_and_dimension_are_checked_before_loading(self):
        from policy_harvester.ai.onnx_embeddings import OnnxEmbeddingProvider

        spec = OnnxEmbeddingProvider.spec("google/embeddinggemma-300m", 768)
        self.assertEqual((spec.dimensions, spec.query_prefix), (768, "task: search result | query: "))
        self.assertEqual(OnnxEmbeddingProvider.spec("google/embeddinggemma-300m", 256).dimensions, 768)
        with self.assertRaises(ValueError):
            OnnxEmbeddingProvider.spec("google/embeddinggemma-300m", 1536)
        with self.assertRaises(ValueError):
            OnnxEmbeddingProvider.spec("azure.text-embedding-3-small", 1536)


class PdfRouteTests(unittest.TestCase):
    def test_pages_take_text_picture_or_scan_routes(self):
        import io

        import pymupdf
        from PIL import Image
        from policy_harvester.documents.pdf_hybrid import PdfHybridParser

        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 72), "Scholarship notice: apply by 2026-04-30 17:00.")
        page = document.new_page()
        page.insert_text((72, 72), "Poster below shows the amounts for each track.")
        poster = io.BytesIO()
        Image.new("RGB", (800, 600), "white").save(poster, "PNG")
        page.insert_image(pymupdf.Rect(72, 100, 520, 440), stream=poster.getvalue())
        document.new_page()  # no text layer: a scan
        result = PdfHybridParser().parse(document.tobytes())
        modes = {image.page_number: image.metadata["mode"] for image in result.images}
        self.assertEqual(modes, {2: "page_supplement", 3: "page_full"})
        supplement = next(image for image in result.images if image.page_number == 2)
        self.assertIn("Poster below", supplement.metadata["page_text"])
        self.assertIn("apply by 2026-04-30 17:00", result.text)
        self.assertEqual(result.quality_flags, ("scanned_pages",))

    def test_hwp_rendered_pages_needing_the_llm(self):
        import io
        import shutil

        import pymupdf
        from PIL import Image
        from policy_harvester.documents.hwp_pdf import picture_pages

        document = pymupdf.open()
        document.new_page().insert_text((72, 72), "Text only page with enough characters to count.")
        page = document.new_page()
        page.insert_text((72, 72), "Page with a poster that holds the amounts.")
        poster = io.BytesIO()
        Image.new("RGB", (800, 600), "white").save(poster, "PNG")
        page.insert_image(pymupdf.Rect(72, 100, 520, 440), stream=poster.getvalue())
        document.new_page()  # blank: skipped
        drawn = document.new_page()
        drawn.draw_rect(pymupdf.Rect(50, 50, 300, 300))  # a form of lines: its text is in the parse
        pages = picture_pages(document.tobytes())
        self.assertEqual([image.page_number for image in pages], [2])
        self.assertTrue(all(image.metadata["mode"] == "page_supplement" for image in pages))
        self.assertIn("poster", pages[0].metadata["page_text"])
        if shutil.which("rhwp") is None:
            self.skipTest("rhwp is not installed here")
        from policy_harvester.documents.hwp_pdf import render_pdf
        with self.assertRaises(Exception):
            render_pdf(b"not an hwp file", "hwp")


class CrosscheckMergeAndRetryTests(unittest.TestCase):
    def test_differences_and_transient_retry(self):
        from datetime import timedelta

        import httpx
        from openai import APIStatusError, BadRequestError
        from policy_harvester.ai.providers import TransientProviderError, is_transient
        from policy_harvester.ai.schema_v2 import ExtractionBundleV2
        from policy_harvester.worker import extraction_differences, retry_delay

        def bundle(deadline: str, amount: int, items: int = 1):
            ev = [{"block_id": "b1", "quote": "x", "evidence_type": "explicit"}]
            nf = {"value": None, "state": "not_found", "evidence": []}
            item = {"local_key": "o", "name": {"value": "장학", "state": "stated", "evidence": ev}, "aliases": [],
                    "organization": nf, "category": "scholarship", "academic_year": nf, "semester": nf,
                    "round_label": nf, "summary": "", "application_windows": [{
                        "local_key": "w", "stage": "application", "label": None, "submit_to": "university",
                        "start": nf, "end": {"value": {"date": deadline, "time": None}, "state": "stated", "evidence": ev},
                        "closing_rule": "fixed"}],
                    "benefits": [{"benefit_type": "cash", "amount_min": nf, "frequency": "once", "duration": None,
                                  "amount_max": {"value": amount, "state": "stated", "evidence": ev},
                                  "tuition_percentage": nf, "description": ""}],
                    "eligibility": {**{k: nf for k in ("student_types", "grades", "majors", "gpa_min", "gpa_scale",
                                                       "income_bracket_max", "regions", "schools", "enrollment_states")},
                                    "rule_tree": None, "residual_conditions": []},
                    "selection": {"selection_count": nf, "nomination_quota": nf, "count_text": None, "method": nf},
                    "application_methods": [], "required_documents": [], "contacts": []}
            return ExtractionBundleV2.model_validate({"schema_version": "scholarship_v2", "revisions": [],
                                                      "coverage": "complete", "warnings": [],
                                                      "opportunities": [{**item, "local_key": f"o{i}"} for i in range(items)]})
        self.assertEqual(extraction_differences(bundle("2026-04-30", 2000000), bundle("2026-04-30", 2000000)), [])
        self.assertEqual(extraction_differences(bundle("2026-04-30", 2000000), bundle("2026-05-07", 2000000)),
                         ["deadlines"])
        self.assertEqual(extraction_differences(bundle("2026-04-30", 2000000, 2), bundle("2026-04-30", 1500000)),
                         ["opportunities", "amounts"])
        request = httpx.Request("POST", "https://example.test")
        overloaded = APIStatusError("overloaded", response=httpx.Response(503, request=request), body=None)
        bad = BadRequestError("bad", response=httpx.Response(400, request=request), body=None)
        self.assertTrue(is_transient(overloaded))
        self.assertTrue(is_transient(httpx.ReadTimeout("slow")))
        self.assertTrue(is_transient(TransientProviderError("x")))
        self.assertFalse(is_transient(bad))
        self.assertFalse(is_transient(ValueError("schema")))
        self.assertEqual([retry_delay(overloaded, n) for n in (1, 2)], [timedelta(minutes=10), timedelta(minutes=30)])
        self.assertEqual(retry_delay(ValueError("x"), 1), timedelta(minutes=2))


class LlmExchangeLogTests(unittest.TestCase):
    def test_calls_are_recorded_with_raw_request_and_error(self):
        import asyncio
        import json

        from policy_harvester.ai.exchanges import _jsonable
        from policy_harvester.ai.providers import _recorded, start_exchange_log

        async def ok(**kwargs):
            return {"choices": [], "usage": {"prompt_tokens": 3}}

        async def boom(**kwargs):
            raise RuntimeError("upstream 500")

        async def scenario():
            log = start_exchange_log()
            await _recorded("extraction", "p", ok, {"model": "m", "messages": [{"role": "user", "content": "한글"}]})
            with self.assertRaises(RuntimeError):
                await _recorded("repair", "p", boom, {"model": "m", "text_format": ExtractionBundle})
            return log

        log = asyncio.run(scenario())
        self.assertEqual([entry["status"] for entry in log], ["ok", "error"])
        self.assertEqual(log[0]["request"]["messages"][0]["content"], "한글")
        self.assertIn("upstream 500", log[1]["error"])
        dumped = json.loads(json.dumps(log[1]["request"], default=_jsonable))
        self.assertEqual(dumped["text_format"]["python_type"], "ExtractionBundle")


if __name__ == "__main__":
    unittest.main()
