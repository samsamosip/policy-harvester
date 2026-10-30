# Backend 공고 불러오기 연동

첨부 `공고 불러오기 API 설계.pdf`와 `backend-main`의 `ImporterClient`,
`ImportJobService`, `ImportPrefill`을 기준으로 구현했다. 기존 공고 수집·발행 API와 별도 큐다.
사용자 프로필은 받지 않으며, 결과는 사용자가 직접 등록 화면에서 확인할 초안이다.

## 실행과 설정

```bash
docker compose run --rm migrate
docker compose up -d api import-worker
```

API와 worker에 동일한 DB, AI 설정을 제공한다. URL은 HTML 본문을 가져와 기존 구조화
provider를 사용한다(`EXTRACTION_LLM_*`, 미설정 시 `LLM_*`). 이미지에는 기존 `LLM_*`
멀티모달 전사를 먼저 적용한다. JavaScript 실행이 필요한 페이지, PDF 직접 링크는 지원하지
않으며 `FETCH_FAILED`가 된다. 실제 AI 자격 증명이 없으면 작업은 `INTERNAL`로 실패한다.

Backend 환경 설정:

- `IMPORTER_BASE_URL`: 이 Harvester의 외부 접근 가능한 주소
- `IMPORTER_API_KEY`: Harvester 관리자 화면에서 backend 전용으로 발급한 API key
- `IMPORTER_CALLBACK_BASE_URL`: backend의 외부 주소. 로컬에서는 비워 polling만 사용
- `IMPORTER_CALLBACK_SECRET`: 아래 Harvester 설정과 같은 공유 비밀값

Harvester 환경 설정:

- `IMPORT_CALLBACK_BASE_URL`: backend의 `IMPORTER_CALLBACK_BASE_URL`과 동일한 base URL
- `IMPORT_CALLBACK_SECRET`: backend의 `IMPORTER_CALLBACK_SECRET`과 동일한 비밀값

두 callback 설정이 없으면 callbackUrl 없는 요청만 허용한다. callbackUrl은 설정한 base URL에
`/api/v1/internal/import-jobs/{requestId}/result`를 붙인 주소와 정확히 일치해야 한다.
임의의 콜백 대상이나 내부 주소로 서명된 요청을 보내지 않는다. 공개 http(s) 기본 포트만
지원하며 URL과 리다이렉트의 DNS 결과에서 내부 주소를 거절한다. 배포 환경에서도
프록시·egress 정책으로 내부망 및 메타데이터 주소 접근을 제한해야 한다.

## HTTP 계약

모든 요청은 기존 `X-API-Key` 인증과 요청 한도를 사용한다.

```http
POST /v1/import-jobs
Content-Type: application/json
X-API-Key: <backend key>

{"requestId":"6f1c2b7e-1d1a-4c55-9a6b-0d6a3a1e2f00","type":"url","url":"https://example.go.kr/notice/123"}
```

선택 필드 `callbackUrl`은 생략하거나 빈 문자열로 전달할 수 있다. 즉시 202와
`{"requestId":"…","status":"queued"}`를 반환한다. 같은 requestId의 동시 재시도도 하나의
작업만 만들며 기존 상태를 반환한다. 입력·콜백은 최초 요청 것을 유지한다. 다른 API key로
만든 작업은 생성 재시도와 조회 모두 404가 된다. backend key를 교체할 때는 이전 key의
미완료 작업을 먼저 처리해야 한다.

이미지는 multipart/form-data의 `requestId`, `type=image`, 선택 `callbackUrl`, `image` 파일로
보낸다. 실제 파일 내용과 MIME을 검증하며 PNG/JPEG/WEBP, 최대 10MiB, 최대 4천만 픽셀을
허용한다. 잘못된 입력은 400, 키 오류는 401, 용량 초과는 413이다.

`GET /v1/import-jobs/{requestId}`는 queued/running 상태 또는 최종 결과를 반환한다.
최종 결과는 설계서대로 `requestId`, `status=succeeded`, `result` 또는
`status=failed`, `error={code,message}`다. 필드는 다음과 같다.

| 필드 | 규칙 |
| --- | --- |
| title / organization | 앞뒤 공백 제거, 최대 300 / 255자, 없으면 null |
| applyEndDate | 한국 날짜 YYYY-MM-DD, 여러 신청 기간 중 가장 늦은 마감, 상시면 null |
| applyEndTime | 명시된 HH:mm만, 마감 날짜가 없으면 null |
| category | 일자리 / 주거 / 교육 / 복지문화 / 참여권리, 장학금은 교육 |
| memo | 개인정보를 제외한 지원 내용·신청 방법 요약, 최대 1,000자 |
| sourceUrl | URL 요청의 원래 주소, 이미지 요청은 null |

backend `ImportPrefill`은 title/organization을 각각 100자로 자르고 sourceUrl을 화면의
applyUrl로 변환한다. 이 동작은 backend가 담당한다. 첨부 backend의 사용자 API는 현재
URL만 보내지만 Harvester는 설계서의 이미지 요청까지 지원한다.

실패 코드는 `FETCH_FAILED`, `NOT_A_NOTICE`, `UNREADABLE_IMAGE`, `TIMEOUT`, `INTERNAL`이다.
처리는 시작 후 60초로 제한한다. worker 중단으로 남은 running 작업도 TIMEOUT으로 정리한다.
최종 결과와 업로드는 완료 후 24시간 보관하고 worker가 삭제한다. 만료 즉시 조회는 404다.
대기 작업도 생성 후 24시간에 만료한다. 이 삭제는 기존 원본·공고·version 테이블과 무관하다.

콜백은 최종 조회 결과와 같은 JSON을 UTF-8 bytes로 직렬화하고
`X-Signature = hex(HMAC-SHA256(secret, X-Timestamp + "." + rawBody))`를 전송한다.
`X-Timestamp`는 현재 유닉스 초다. non-2xx, 연결 실패, 5초 내 응답 없음은 실패로 처리하고
실패 시점부터 10초 → 60초 → 300초 후 최대 3회 재전송한다(최초 포함 4회).
시도와 다음 실행 시간을 DB에 기록하므로 재시작에도 유지된다. 콜백 실패와 무관하게
polling 결과는 유지한다. 첨부 backend는 3분 후 미완료 작업을 실패로 확정하므로,
5분 뒤 최종 콜백보다 조회 fallback이 먼저 결과를 회수하도록 정상 설정해야 한다.

## 검증

단위 테스트는 `python -m unittest discover -s tests -v`.
새 DB를 만들고 `DATABASE_URL`을 그 DB로 지정해 `alembic upgrade head` 실행 후:

```bash
python -m unittest discover -s tests/integration -p test_import_jobs.py -v
```

이 테스트는 실제 PostgreSQL에서 요청·동시 재시도·API key별 격리·이미지 검증·만료·중단 복구·
서명과 콜백 재시도를 확인한다. AI와 외부 callback HTTP 경계는 대체하므로 비용이 발생하지 않는다.
실제 provider 추출 품질과 실제 배포된 Java backend 수신까지의 종단 간 검증은 별도다.
