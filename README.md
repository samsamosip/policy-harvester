# Policy Harvester

인하대학교 장학 게시판을 매일 전체 재확인하고, 목록·게시글 HTML·첨부·본문 이미지·HTTP 관측을
원본 그대로 보존한 뒤, 문서를 block 단위로 파싱하고 LLM으로 구조화해 검토 가능한 opportunity
API로 발행하는 Python 서비스다. 수집기, 단계별 worker, FastAPI public API, 관리자 UI는 서로 다른
프로세스로 실행된다.

가장 중요한 원칙: **원본·과거 version·처리 결과를 삭제하거나 덮어쓰지 않는다.** 변경은 항상 새
version/run/decision 행으로 남고, 애매한 identity와 충돌은 자동 병합하지 않고 review queue로 보낸다.

## 빠른 시작

```bash
cp .env.example .env          # SESSION_SECRET, MASTER_KEY, AI credential 설정
docker compose up -d postgres object-store
docker compose run --rm migrate
docker compose run --rm api policy-admin init --email admin@example.com   # 대화형 비밀번호
docker compose up -d api
```

- Public API/OpenAPI: `http://localhost:8000/docs`
- 관리자 UI: `http://localhost:8000/admin`

## 설정

`.env`(비밀값 포함, git/docker ignore 대상)와 관리자 `AI Settings` 화면의 key별 override로 설정한다.
우선순위는 **DB 관리자 override > `.env` > 코드 기본값**이다. provider secret을 DB에 저장하려면
`MASTER_KEY`가 필요하며, 없으면 환경변수 credential만 쓴다. 비밀값(API key, proxy 인증정보,
`SESSION_SECRET`, `MASTER_KEY`)은 로그·문서·commit에 복사하지 않는다.

| 항목 | 키 | 비고 |
|---|---|---|
| LLM | `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL` | `openai`, `openai_compatible`, `anthropic`, `google`. compatible endpoint가 JSON Schema 모드를 거절하면 JSON object 모드와 repair 요청으로 자동 전환 |
| Embedding | `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` | profile(모델·차원·chunker)별로 색인을 분리 |
| 객체 저장소 | `OBJECT_STORE_BACKEND=s3`, `S3_*` | 로컬은 SeaweedFS(`object-store:8333`). path-style S3만 쓰므로 AWS S3/R2/Ceph RGW로 교체 가능 |
| 수집 | `CRAWL_USER_AGENT`, `CRAWL_TLS_VERIFY=true`, `CRAWL_PROXY_URL`(선택) | 아래 참고 |
| 공개 | `AUTO_PUBLISH` | true여도 `complete` 품질이고 source가 범위 내·가용·최신일 때만 자동 공개 |

**수집 접속:** 인하대 서버는 curl 기본 User-Agent 요청을 응답 없이 끊는다. 설정한
`CRAWL_USER_AGENT`(봇 식별 문자열)로는 직접 접속과 TLS 검증이 정상이라 proxy가 필요 없다.
proxy가 필요하면 `CRAWL_PROXY_URL`(http/https/socks5/socks5h) 또는 관리자 `Sources` 화면의
source별 암호화 proxy를 쓴다. 한 번만 proxy 없이 돌리려면
`docker compose run --rm -e CRAWL_PROXY_URL= -e CRAWL_TLS_VERIFY=true api policy-admin crawl`.

**MinIO 대신 SeaweedFS:** MinIO 공식 저장소가 2026-04-25 archive 되어 SeaweedFS 4.47(`weed mini`)로
바꿨다. `mini`는 로컬/MVP 구성이며, production은 master/volume/filer 영속성·복제·TLS·backup을 따로 설계한다.

## 운영 명령

| 작업 | 명령 |
|---|---|
| 전체 수집(모든 목록 페이지·상세·첨부) | `docker compose run --rm api policy-admin crawl --source inha-kr-8` |
| 제한 수집 | `... crawl --source inha-kr-8 --max-pages 1 --max-notices 5` |
| 파싱만 상시 처리(LLM 미호출) | `docker compose run -d --name parse-worker worker policy-worker run --forever --stages parse_document` |
| AI 큐를 상한을 두고 배치 처리 | `docker compose run --rm worker policy-worker run --drain --max-jobs 50 --stages structure,embed` |
| 전 단계 상시 처리 | `docker compose up -d worker` |
| 일일 scheduler | `docker compose up -d scheduler` (`last_successful_run_at`이 없으면 전체 수집부터 시작) |
| 범위 내 미처리 notice 큐잉 | `docker compose run --rm api policy-admin enqueue-unprocessed --dry-run` 후 옵션 없이 |
| cross-notice 동일성 후보 일괄 생성 | `docker compose run --rm api policy-admin propose-identity-candidates` |
| 객체 무결성(SHA-256·크기) | `docker compose run --rm api policy-admin verify-objects` |

`--stages`는 `parse_document,structure,embed`의 부분집합이다. 여러 worker를 동시에 띄워도 job은
`FOR UPDATE SKIP LOCKED`로 한 번씩만 처리된다. 15분 이상 heartbeat가 없는 running job은 자동 회수된다.

**최초 backfill 권장 순서:** 전체 수집 → 파싱 전용 worker 여러 개 → `--stages structure,embed --max-jobs N`으로
LLM 비용을 확인하며 나눠 처리 → `propose-identity-candidates` → 관리자 review.
crawler는 `scope_status=excluded`(장학 분류가 아닌 고정 공지 등)를 큐에 넣지 않고, worker도 범위 밖
notice에는 LLM을 호출하지 않는다.

### worker 여러 개 돌리기

작업은 `FOR UPDATE SKIP LOCKED`로 가져가고, 실행 중에는 1분마다 heartbeat를 갱신한다(15분 동안 갱신이 없으면
멈춘 작업으로 보고 다른 worker가 이어받는다). 그래서 worker 프로세스는 몇 개든 같은 큐를 나눠 쓸 수 있다.
`compose.yaml`의 worker는 단계별 서비스이고 개수는 `.env`로 정한다(없으면 각 1개, 전체 단계 `worker`는 0개):

| 변수 | 서비스·단계 | 16코어·64GB 권장 | 성격 |
|---|---|---|---|
| `WORKER_PARSE_REPLICAS` | `worker-parse` · 파싱(Docling, 이미지 전사) | 3 | CPU·메모리(프로세스당 약 2~3GB, 4 thread) |
| `WORKER_LLM_REPLICAS` | `worker-llm` · 구조화 추출 | 6 | LLM 대기 위주. provider 요청 한도와 비용이 상한 |
| `WORKER_EMBED_REPLICAS` | `worker-embed` · 임베딩 | 1 | 내장 ONNX 모델이면 CPU(`ONNX_THREADS`) |

`.env`를 바꾼 뒤 `docker compose up -d`로 반영한다. 일회성 명령은 `docker compose run --rm worker …`를 쓴다.

### 이미지 빌드(CI)와 배포

`.github/workflows/docker.yml`이 Blacksmith runner에서 이미지 하나(api·worker·scheduler·migrate 공용)를 만든다.

- 모든 push(브랜치 무관): 빌드 → `ghcr.io/samsamosip/policy-harvester:sha-<커밋>` push → 그 이미지 안에서
  단위 테스트 → main이면 통과 후 `latest`를 같은 이미지에 붙인다. 같은 브랜치에 새로 push하면 진행 중인
  빌드는 취소된다(main 제외). runner는 Ubuntu 24.04, 플랫폼은 linux/amd64만(CPU torch, rhwp x86_64).
- 레지스트리 인증은 workflow의 `GITHUB_TOKEN`(packages: write)이라 저장소 secret이 필요 없다. 필요한 것은
  GitHub organization에 Blacksmith GitHub App 설치뿐이다.
- 서버에서는 빌드하지 않고 받는다: `.env`에 `POLICY_IMAGE=ghcr.io/samsamosip/policy-harvester:latest`,
  패키지가 비공개면 `docker login ghcr.io`(read:packages 권한 token) 후
  `docker compose pull && docker compose up -d --no-build`.

### 백업·복원·초기화

DB와 객체 저장소, `MASTER_KEY`가 한 recovery point다. DB만 복구되고 객체가 없으면 실패로 본다.

```bash
docker compose exec -T postgres pg_dump -U policy -d policy --format=custom > backups/policy-$(date -u +%Y%m%dT%H%M%SZ).dump
docker compose exec -T postgres pg_restore -U policy -d policy --clean --if-exists < backups/<file>.dump
docker compose run --rm api policy-admin verify-objects    # 복원 후 객체 대조
```

데이터를 비우고 다시 수집할 때는 위 백업 후 관리자 계정·source 행을 보존하고
`DROP SCHEMA inha_policy CASCADE; DROP TABLE public.alembic_version;` → `docker compose run --rm migrate`
→ 보존한 행 복원 순서로 한다. 객체 저장소는 content-addressed라 지우지 않아도 같은 bytes를 재사용한다.
`backups/`는 git/docker ignore 대상이다.

## 테스트

```bash
# 단위 테스트 (외부 호출 없음)
docker compose run --rm -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/workspace/src:/workspace \
  -v "$PWD:/workspace" -w /workspace worker python -m unittest discover -s tests -v

# 통합 테스트: 임시 *_test DB 생성 → migration → 실행 → 삭제
scripts/integration-test.sh
```

통합 테스트는 `.env`의 **실제 LLM/embedding**을 사용한다(약 10회 미만의 추출 호출, 4분 내외).
현실적인 공고 몇 건(복수 장학금, 원공고, 기간연장, 재게시, 이전 연도, 범위 밖)을 한 번 seed하고
실제 DB trigger를 거쳐 파이프라인, 관리자 POST(CSRF/RBAC, override, publish, merge/unmerge, split,
job retry), 정정·연장 적용, 동일성 후보, 품질 상한, 실패 원본 재검증을 검사한다. 모델 문구에
의존하지 않도록 구조와 불변식을 검사한다. 같은 게시글 재추출 매칭처럼 모델이 관여하지 않는
결정론적 로직은 고정 추출 결과를 assembler에 직접 넣어 검사한다.

## 코드 구조

| 경로 | 역할 |
|---|---|
| `src/policy_harvester/crawling/` | source adapter, 전체 목록/상세/자산 수집, fetch snapshot, notice version |
| `src/policy_harvester/documents/` | magic/ZIP/OLE 형식 판별, HTML/PDF/DOC/DOCX/HWP/HWPX/XLSX/PPTX/ZIP block 파서, `vision.py`(이미지 LLM 전사) |
| `src/policy_harvester/ai/` | provider 추상화, 추출 schema(`ExtractionBundle`)와 prompt version |
| `src/policy_harvester/pipeline/assembler.py` | 추출 결과 → opportunity version/evidence, 품질 판정, 같은 게시글 매칭 |
| `src/policy_harvester/pipeline/edition.py` | 이름/에디션 비교 규칙 |
| `src/policy_harvester/pipeline/identity.py` | cross-notice 동일성 후보 제안 |
| `src/policy_harvester/pipeline/revisions.py` | 검증된 정정·연장 → 새 immutable version |
| `src/policy_harvester/worker.py` | parse/structure/embed worker, scheduler |
| `src/policy_harvester/api.py` | public API, hybrid 검색, change feed |
| `src/policy_harvester/admin.py`, `templates/` | 관리자 인증/RBAC/화면/작업 |
| `schema.sql`, `migrations/` | append-only schema, 제약·trigger(공개 guard, 변경 이벤트) |
| `inha_parser.py`, `revision_resolver.py` | 사이트 parser CLI, 필드 resolution 규칙(서비스에서도 사용) |
| `tests/`, `scripts/integration-test.sh` | 단위·통합 테스트 |

## 데이터 처리 규칙

### 저장과 version

- `fetch_snapshots`: 요청마다의 관측(200/304/실패 모두). `notice_versions`: 의미 있는 원문 변경
  (제목·본문 구조·링크·첨부 bytes). `extraction_runs`: 모델·prompt·schema·입력 hash별 처리 실행.
  `opportunity_versions`: 검증·발행 단위. 조회수 변화로 재처리하지 않는다.
- 파일은 SHA-256 content address로 한 번만 저장하고, URL·파일명·등장 위치는 발생별로 보존한다.
- 재파싱은 문서의 새 attempt를 추가하며, 추출은 문서별 **최신 attempt**만 입력으로 쓴다.
- 동일 입력(blocks hash·prompt·model)의 성공 추출은 재사용한다. 실패한 추출도 provider 원본 응답을
  보존하고, 재시도 때는 LLM을 다시 부르기 전에 보존 원본을 현재 검증 규칙으로 재검증한다.

### 문서 파싱

| 형식 | 방식 |
|---|---|
| HTML 본문 | lxml(`huge_tree`). 본문은 항상 HTML로 처리(fragment라 sniffing 불가), charset 없는 bytes는 UTF-8 우선. 인라인 요소는 붙여 읽고(`1<span>7:00</span>`=17:00) 블록·`<br>`만 구분. 취소선은 `~~옛 값~~`으로 보존 |
| 표 | rowspan/colspan을 grid로 펼치고 원본 cell span 보존. 표 안 문단은 별도 block으로 중복하지 않음(한 열짜리 layout 표 제외) |
| 이미지(포스터 등) | **OCR 없음.** 이미지를 통째로 multimodal LLM에 보내 원문 전사(표는 HTML, 병합 칸 표시) → 문단/표 block |
| PDF | 본문은 **PyMuPDF 텍스트층**, 표만 **Docling**(TableFormer)에서 가져오고 표 영역의 텍스트는 표로 대체(Docling의 텍스트 재구성은 일부 한국어 PDF에서 글자를 빠뜨렸다). 텍스트층이 없는 페이지는 페이지째 LLM 전사, 그림이 있는 페이지는 **페이지 이미지 1장 + 그 페이지 텍스트**를 보내 그림에만 있는 장학 정보를 보충(그림마다 호출하지 않음) |
| DOCX/PPTX/XLSX | **Docling**(OCR 끔). 문서 안 그림은 LLM 전사 |
| HWP | OLE 구조(`FileHeader`+`BodyText`)로 판별. 텍스트·표는 직접 파싱(`hwp5html`로 표 보존 → `hwp5txt`와 비교해 빠진 줄 복구). **rhwp**(0.8.7, 이미지에 한국어 글꼴 포함)로 PDF를 만들어 그림이 있는 페이지만 PDF와 같은 방식으로 페이지 단위 LLM 전사, 그 PDF를 `derived_files`에 남겨 관리자 미리보기로 쓴다. rhwp 실패 시 예전처럼 `BinData` 그림을 하나씩 전사 |
| DOC(Word 97) | `antiword` |
| HWPX | XML 파서(표 보존) + HWP와 같은 rhwp 페이지 처리·미리보기 |
| ZIP | member 수·크기 제한 후 내부 형식별 파싱(내부 이미지도 LLM 전사), 손상·암호화 member는 warning |

rhwp PDF로 Docling을 돌리면 HWP 표의 30%를 놓쳤고(표본 40개), 텍스트는 단어 기준 99%가 보존됐다(679개).
그래서 HWP는 직접 파싱을 쓰고 PDF는 페이지·미리보기에만 쓴다. 새 경로가 실패한 파일은 예전 파서
(PyMuPDF/hwp5/python-docx/openpyxl/python-pptx)로 처리하고 `partial`+`pdf_hybrid_failed:*`/`rhwp_failed:*`/
`docling_failed:*`로 남긴다. Docling 모델(layout,
TableFormer)과 CPU용 torch는 Docker 빌드 때 이미지에 포함되며 실행 중에는 내려받지 않는다(`HF_HUB_OFFLINE=1`).
Docling은 worker마다 모델을 올리므로(수백 MB~2GB) 파싱 worker 수는 메모리에 맞춰 정한다.

이미지 전사: 긴 변 2048px JPEG로 줄여 보내고, 아이콘 크기(200×120 미만)와 페이지 면적 4% 미만 그림은 건너뛴다.
한 문서 안의 전사 호출은 동시에 4개까지 보낸다. 같은 이미지는 worker 안에서
SHA-256으로 한 번만 전사하고, 같은 파일의 문서 단위 파싱은 DB 캐시(binary asset + parser + 모델 + 전사 prompt
version)로 재사용한다. 전사 실패 시 로컬 OCR fallback 없이 job이 재시도되고 최종 실패는 review로 간다.
5개 이상 게시글에 반복되는 본문 이미지(사이트 배너 등)는 추출 입력에서 빼고 manifest에 기록한다.
파싱 worker도 이미지 전사 때문에 LLM을 호출한다.

PostgreSQL에 저장할 수 없는 NUL 문자는 저장 직전에 제거한다.

### LLM 추출과 검증

- 스키마는 `scholarship_v2`(`ai/schema_v2.py`, prompt `scholarship-ko-2.0`)다. 입력 block은 prompt에서
  `b1, b2, …`(manifest 순서)로 부르고 저장 전에 block id로 되돌린다. 모든 사실은
  `stated / not_found / explicitly_none / unknown / parsing_failed / conflict` 상태와 block·인용을 가진다.
  v1(`ai/schema.py`)은 이전 실행을 읽을 때만 쓴다.
- v2는 프로그램이 계산할 수 있는 것을 모델에게 묻지 않는다: 인용 위치·confidence, timezone, 날짜 precision,
  통화(KRW), 모집 상태(기간과 오늘로 계산), 자격 원문(인용된 block 텍스트), 동일성 후보, 공고 id.
  필터에 쓰는 값은 enum(학생 구분, 학적, 학기, 지급 주기, 분류)이고, 결과 발표는 `stage=result` 일정,
  인원은 `selection_count`(최종 선발)·`nomination_quota`(학교 추천)·`count_text`, 신청 경로별 주소를 둔다.
- 추출 모델은 `EXTRACTION_LLM_*`(없으면 `LLM_*`)이고 이미지 전사는 항상 `LLM_*`다. 기본 구성은 추출 =
  Muse Spark 1.3(OpenRouter, thinking high), 전사·교차 확인 = Gemini 3.8 Flash(PwC).
  `LLM_MAX_OUTPUT_TOKENS`(기본 131072)를 모든 추출 호출에 보낸다. 지정하지 않으면 provider 기본(약 6.4만)에서
  잘려 트랙이 많은 공고가 실패했다.
- 교차 확인: 결과가 비었거나, 장학이 2개 이상이거나, 입력이 20,000자 이상이면(전체의 약 11%)
  `EXTRACTION_CROSSCHECK_MODEL`로 한 번 더 추출한다. 장학 수·신청 마감·금액·인원이 다르면 교차 확인 모델이
  두 결과를 원문과 대조해 합치고, 합친 결과가 정상 흐름으로 간다(합치기에 실패하면 더 많이 찾은 쪽을 쓰고
  `crosscheck_disagreement` 경고로 검토에 보낸다). 주 모델의 일시 장애(429·5xx·시간 초과)는 10분·30분 뒤
  재시도하고, 마지막 시도에서만 교차 확인 모델로 대신 추출한다.
  각 실행은 별도 `extraction_runs` 행과 `llm_exchanges` 기록을 남긴다.
- AI 2차 검토: 판단이 필요한 검토(품질 확인, 동일성 확인, 정정·연장 후보)는 사람에게 바로 보내지 않고
  `REVIEW_LLM_MODEL`(기본 구성 `bedrock.anthropic.claude-opus-5-5`, `LLM_*` endpoint, thinking
  `REVIEW_REASONING_EFFORT`)이 공고 원문·추출 결과·관련 장학을 보고 먼저 처리한다(`review` 작업, worker-llm이
  추출 대기열보다 먼저 처리). high 확신일 때만 직접 행동한다.
  - dismiss: 문제없음. 품질 검토면 초안을 공개 대상으로 올린다(`ai_review_cleared`; 근거 충돌이 남은 초안은
    사람이 공개할 때처럼 `needs_review` 그대로 공개).
  - fix(품질): 공개 전 초안의 값(요약·대상·인원과 그 의미·신청 기간 날짜·금액 등)을 원문 근거로 고친 뒤
    검색 색인을 다시 만들고 공개한다(`ai_corrected`, 수정 전후 값은 검토 항목과 감사 로그에 남는다).
  - merge(동일성): 같은 모집인 장학을 공개된 것 중 가장 오래된 장학으로 병합한다(`identity_decisions`
    actor `ai_review`). 병합 후보 중 다른 장학은 제안을 기각한다.
  - revise(정정·연장 후보): 이전 장학(제목·출처 공고 제목 유사도 상위 12개 중 모델이 고른 것)에 연장·정정을
    `RevisionService`로 새 version으로 적용하고(인용은 정정 공고 block에서 찾음, actor `ai_review`), 정정 공고에서
    따로 만들어진 같은 장학은 그 장학으로 병합한다.
  그 밖에(원문 모순, 장학 분리)는 판단 근거를 붙여 검토함에 올리고, 검토 모델이 끝내
  실패해도 그대로 올린다. 처리·수집 실패나 DB 공개 규칙 거부는 바로 올라간다. 모델을 비우면 꺼진다.
- 인용이 block에 실제로 없으면 버리지 않고 warning과 `needs_review`로 남긴다.
- 검증 전 결정론적 정리: code fence·trailing comma 제거, 모델이 `state`에 넣은 미정의 값(예: `inferred`)은
  `unknown`으로 내리고 값을 버림, 미확정 state에 붙은 값 제거, 인용 없는 `stated`는 `unknown`으로 내림,
  문자열 필드(`currency` 등)에 fact 객체가 오면 stated 값만 꺼냄, `field_path` 앞 `/` 보정
  (사실로 격상하는 정리는 하지 않음).
  그래도 실패하면 repair 요청. `24:00`은 다음날 `00:00`으로 동치 변환하고 원문 표기는 `raw_text`에 남긴다.
- compatible endpoint가 JSON Schema 모드를 거절하면 그 endpoint·model은 프로세스 안에서 기억해 바로 JSON
  object 모드를 쓴다. 출력이 1만~2만 토큰이라 `LLM_TIMEOUT_SECONDS`는 300 이상을 권장한다.
  gateway가 오래 걸리는 요청을 끊으면(PwC는 약 12분) `LLM_STREAM=true`로 스트리밍한다.
- LLM은 영속 ID, 병합, 공개를 결정하지 않는다.
- 모든 provider 호출(추출, JSON repair, 이미지 전사)의 요청 body와 응답 body는 가공 없이 객체 저장소에
  JSON으로 남고 `llm_exchanges`(append-only)가 용도·모델·소요시간·토큰·성공 여부·`extraction_run_id`와 함께
  가리킨다. 실패한 호출도 요청과 provider 오류 body를 남긴다. 관리자 공고 상세의 "LLM 요청·응답 원본"에서 연다.

### 품질과 공개

`needs_review`(모델 warning, 근거 불일치, 충돌, 추출 coverage 미완) > `partial`(`partial` 문서나 미완료
첨부 수집이 근거에 있음) > `complete` 순으로 판정한다. LLM 전사는 `partial`로 보지 않는다. 이 규칙은 DB 공개
trigger와 같다. 자동 공개는 `complete`만, 수동 공개는 reviewer가 한다. 자동 공개가 DB guard에
거부되면 embedding은 남기고 review(`auto_publish_blocked`)만 만든다.

### 동일성: 이름은 조금씩 달라도, 에디션은 다르면 안 된다

이름을 `core_name`(괄호 머리말·연도·학기·회차·기수·기관명·`선발/안내/모집` 등 일반어 제거,
`장학생=장학금`)과 에디션 토큰으로 나눠 비교한다(`pipeline/edition.py`).

| 판정 | 조건 | 처리 |
|---|---|---|
| `same` | core 이름 유사도 ≥ 0.82, 충돌 없음, 연도·접수일 같은 기준점이 하나 이상 일치 | 같은 opportunity |
| `different_edition` | 양쪽에 있는 연도/학기/반기/회차(`N차`, 추가모집, 재모집)/기수가 다르거나 마감일이 150일 넘게 차이. 추출값과 이름 속 토큰은 따로 비교 | 다른 opportunity, `program_key`만 공유 |
| `uncertain` | 회차·기수 표시가 한쪽에만 있음(`X` vs `X 추가모집`), 기준점 없음 등 | `same`으로 보지 않음 |
| `different` | core 이름 유사도 < 0.55 | 무관 |

- **같은 게시글 재추출:** 미점유 기존 opportunity 중 `same`이 정확히 하나면 새 version. 한 추출의 두 항목이
  같은 opportunity를 차지할 수 없다. 단일 항목 게시글이면 이름이 달라도(`different_edition`만 제외)
  같은 슬롯으로 보되 이름이 크게 바뀌면 review(`same_slot_renamed`). 그 외는 새 opportunity +
  review(`same_notice_unmatched`, 비교 근거 포함).
- **다른 게시글:** 새 opportunity마다 `same`/`uncertain` 후보 상위 3개를 `proposed` merge decision으로
  기록하고 review(`cross_notice_candidates`). 관리자 비교 화면에서 병합(제안을 supersede하는 confirmed
  decision)하거나 거절(rejected decision). 이전 연도 같은 사업은 제안하지 않는다.

### 정정·연장

우선순위는 파일 형식이나 게시 순서가 아니라 **확인된 변경 지시와 적용 범위**로 정한다. 같은 게시글의
수정은 재추출로 새 version을 만든다. 다른 게시글의 연장·정정은:

1. 추출의 `revisions`가 `revision_candidate` review가 된다.
2. 관리자 `정정·연장 비교` 화면에서 대상 opportunity의 현재 접수기간·근거와 제안 patch·원문 block을 비교한다.
3. 같은 회차, 같은 field 범위, 새 값을 모두 확인 체크해야 적용된다. 인용은 원문 block에 있어야 한다.
4. 한 transaction에서 confirmed `link_notice` decision, 기존 행을 복사한 새 draft version, 새 값·변경표식
   evidence, `superseded`로 표시된 이전 근거 사본, `revision_resolver.resolve_field`로 계산한
   `field_resolutions`, embed job을 만든다. 이전 version은 바뀌지 않는다.
5. 지원 path: `/application_windows/<window_key>/start|end`, `/title`, `/summary`, `/source_status_override`.
   공개 시 변경 피드는 `extended`/`corrected`/`cancelled`.

제목에 `[연장]`만 있고 새 값이 확인되지 않거나, 본문·첨부가 다른데 변경 지시가 없으면 현재 값을 바꾸지 않는다.

### 검색과 임베딩

- `GET /v1/opportunities?q=…`는 `search_mode`로 순위를 정한다: `hybrid`(기본, 어휘 0.45 + 의미 0.55),
  `lexical`, `vector`. 응답 `query`에 해석된 필터, `vector_used`, `vector_error`가 나온다. 벡터를 못 쓰면
  hybrid는 어휘 검색으로 내려가고 그 사실을 알리며, `vector`는 503이다.
- 질의에서 학생 구분·지역(시·도 + "거주/사는/출신")·금액·소득 구간·"모집 중" 등을 필터로 해석한다. 지역·학생
  구분 필터는 "지원할 수 있는가" 기준이라 제한이 없는 장학도 포함한다.
- 임베딩 기본값은 내장 모델 EmbeddingGemma-300m(ONNX fp32, 768차원, CPU)이다. 한국어 검색어 120개 ×
  공고 985건 평가에서 recall@10 0.79(e5-small 0.69, e5-large 0.72, 외부 text-embedding-3-small 0.48),
  질의당 35ms. 모델은 이미지에 포함된다(`ONNX_MODELS_DIR`, Gemma 이용 약관). `EMBEDDING_PROVIDER`를
  `openai_compatible` 등으로 두면 외부 API를 쓴다.
- 임베딩 provider·모델·차원을 바꾸면 새 색인(profile)이 생기고 전체 재계산이 자동으로 큐에 들어간다
  (공개는 하지 않음). 계산하는 동안 검색은 기존 색인을 쓰고, 다 끝나면 worker가 새 색인으로 바꾼다.
  AI 설정 > 검색 색인에서 진행 상황을 보고 "임베딩 다시 계산"·"이 색인으로 전환"을 직접 할 수 있다.

### Public API

- 모든 `/v1` 요청은 `X-API-Key` header가 필요하다. key는 관리자 화면 **API key** 메뉴(admin 역할)에서
  발급·폐기하고, 발급 직후에만 전체 key가 보인다. DB에는 SHA-256과 앞 11자 prefix만 남고, 발급·폐기는 감사 로그에
  기록된다. key마다 분당 요청 한도가 있고 넘으면 429 + `Retry-After`. 로컬에서만 `API_KEY_REQUIRED=false`로 끌 수 있다.
- `/docs`는 Swagger UI다. 자산(swagger-ui-dist 5.33.1, checksum 고정)은 이미지에 포함되어 CDN 없이 CSP 안에서 동작하고,
  Authorize에 key를 넣어 바로 호출해 볼 수 있다. 스키마는 `/openapi.json`(관리자 경로는 제외).

| 경로 | 내용 |
|---|---|
| `GET /v1/opportunities` | SQL 필터 + pg_trgm/FTS + embedding 가중 검색. 허용된 한국어 질의 해석만(임의 SQL 없음). stale source 제외 |
| `GET /v1/opportunities/{id}` | 현재 공개 version, 기간·혜택·자격·근거·출처·첨부·품질, manual override 반영. merged는 308 |
| `GET /v1/opportunities/{id}/versions`, `/sources` | version 이력, 출처 |
| `GET /v1/notices/{id}`, `/versions`, `/observations` | 원문, 원문 version, 관측 이력 |
| `GET /v1/assets/{id}` | 자산 metadata (원본 재배포는 관리자 정책) |
| `GET /v1/changes` | 변경 피드(`created/updated/extended/corrected/cancelled/merged/split/unpublished/...`) |
| `GET /v1/codes`, `/v1/sources/{id}/health` | 코드, 수집 상태 |

## 현재 상태

검증 결과는 아래 "검증 기록"에 갱신한다.

## 남은 작업

**데이터 정확성**
- revision patch path 확장(benefits/eligibility/required_documents), 새 값 미확인 pending amendment 기록,
  LLM patch path → canonical path 자동 변환(현재 관리자가 JSON 수정)
- merge 시 winner의 composite version 재조립, split 시 원래 opportunity에서 해당 항목을 뺀 새 version
- 기관명 정규화, 대상·채널 비교를 동일성 판정에 추가

**문서**
- 내장 이미지의 자식 asset 저장(현재 전사 텍스트만 block으로 저장), DOCX/PPTX/HWPX 내부 이미지 전사
- Docling 표 결과도 병합 셀이 많은 표에서 칸이 밀리는 경우가 있다. 텍스트층과의 대조 검사로 깨진 표 페이지만
  vision으로 보내는 경로는 아직 없다
- QR 해석, ZIP 내부 lineage 정규화
- 형식별 gold fixture와 누락률 회귀 테스트

**관리자/검색/운영**
- revision 화면의 window 선택형 입력과 evidence picker, merge preview, split 항목 선택 UI, bulk reprocess 화면
- 고정 corpus 검색 평가(Recall@10/nDCG@10), 한국어 형태소 분석기, 안정적인 search cursor
- 범위 밖 source의 public detail 응답 정책(현재 `source_is_stale`과 함께 200)
- Prometheus/OpenTelemetry, provider별 비용 계산, 자동 backup/restore와 정기 restore test,
  production TLS·secret manager, hash lock

## 검증 기록

(갱신 중)

## 참고 자료

- [pyhwp 변환기와 실험적 기능 범위](https://pyhwp.readthedocs.io/en/latest/converters.html)
- [한컴 HWPX 구조](https://tech.hancom.com/hwpxformat/)
- [pgvector](https://github.com/pgvector/pgvector), [pg_trgm](https://www.postgresql.org/docs/current/pgtrgm.html)
- [SeaweedFS](https://github.com/seaweedfs/seaweedfs), [MinIO archive 공지](https://github.com/minio/minio)
- [온통청년 API](https://www.data.go.kr/data/15143273/openapi.do)
