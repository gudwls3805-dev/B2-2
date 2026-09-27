# 국내 여행지 추천 프로그램

여행 **날짜 하나**를 넣으면, 그 시기에 갈 만한 국내 도시 · 맛집 · 하루 일정을 **리포트 한 장**으로 뽑아 주는 CLI 프로그램.

## 왜 만들었나

여행 날짜는 정해졌는데 "어디로 가서 / 뭘 먹고 / 하루를 어떻게 보낼지"는 매번 검색을 여러 번 돌려 흩어진 정보로 직접 조합해야 한다. 이 과정을 날짜 입력 한 번으로 초안까지 만들어 주자는 것이 목적이다.

그래서 흐름을 세 단계로 나눴다. 각 단계가 다음 단계의 입력이 된다.

```
[1/3] LLM API   →  날짜로 추천 도시·날씨·행사 생성 (JSON)
[2/3] 지도 API  →  그 도시로 맛집 검색       (JSON)
[3/3] LLM API   →  위 둘을 합쳐 리포트 작성   (Markdown)
```

1단계 출력을 **JSON으로 고정**한 이유가 여기 있다. 사람이 읽는 문장이 아니라 2단계가 바로 꺼내 쓸 수 있는 `recommended_city` 값이 필요하기 때문이다.

> 참고: 날씨·행사는 LLM이 생성한 "일반적인" 정보이지 실제 예보나 공식 일정이 아니다. 이 프로그램의 목표는 정확한 예보가 아니라 여러 API를 엮어 구조화된 결과를 만드는 흐름이다(자세한 한계는 아래 [한계와 미구현](#한계와-미구현) 참고).

## 요구 사항

- Python 3.10 이상 (미션 지정 환경)
- 패키지: `requests`, `python-dotenv`

```bash
pip install -r requirements.txt
```

## API 키 설정

키는 코드에 쓰지 않고 `.env` 파일이나 환경변수에서 읽는다. 이유는 세 가지다 — 협업/공유 중 실수로 키가 공개되는 것을 막고, 키를 바꿔도 코드를 안 고쳐도 되며, 과금·쿼터가 걸린 서비스에서 사고를 예방한다.

### .env 파일 (권장)

```bash
cp .env.example .env      # 만들어진 .env에 실제 키를 채운다
```

사용할 제공자에 해당하는 키만 채우면 된다. 제공자는 미션이 "택1"로 정한 범위 안에서 고르되, 코드를 안 건드리고 바꿀 수 있도록 환경변수로 뺐다. 기본값은 하나를 정해야 해서 `openai` + `kakao`로 뒀다.

| 용도 | 제공자 (환경변수 값) | 채워야 할 키 |
|------|----------------------|--------------|
| LLM  | `LLM_PROVIDER=openai` (기본) | `OPENAI_API_KEY` |
| LLM  | `LLM_PROVIDER=gemini` | `GOOGLE_API_KEY` |
| 지도 | `MAP_PROVIDER=kakao` (기본) | `KAKAO_REST_API_KEY` |
| 지도 | `MAP_PROVIDER=naver` | `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` |

### 환경변수 (현재 터미널 세션에만 적용)

```bash
# macOS / Linux
export OPENAI_API_KEY="YOUR_KEY"
export KAKAO_REST_API_KEY="YOUR_KEY"
```

```powershell
# Windows PowerShell
$env:OPENAI_API_KEY="YOUR_KEY"
$env:KAKAO_REST_API_KEY="YOUR_KEY"
```

키 발급처: OpenAI(platform.openai.com), Google AI Studio(aistudio.google.com), Kakao Developers(developers.kakao.com), Naver Developers(developers.naver.com).

## 실행 방법

```bash
# 기본 실행 — 날짜는 필수, 형식은 YYYY-MM-DD
python travel_planner.py --date "2026-03-15"

# 키·네트워크 없이 샘플 데이터로 흐름과 출력 형식만 확인
python travel_planner.py --date "2026-03-15" --mock

# 캐시를 무시하고 API를 다시 호출
python travel_planner.py --date "2026-03-15" --refresh
```

| 옵션 | 설명 |
|------|------|
| `--date`, `-d` | (필수) 여행 날짜 `YYYY-MM-DD`. 형식이 틀리면 사용법을 출력하고 종료(코드 2) |
| `--mock` | 키 없이 샘플 데이터로 전체 파이프라인 실행 |
| `--refresh` | 같은 날짜의 캐시를 무시하고 새로 호출 |

정상 실행 시 화면:

```
[1/3] 1차 추천 생성 중(LLM)...
    - recommended_city: "제주"
[2/3] 맛집 검색 중(지도/장소 API)...
    - 맛집 5곳 검색 완료
[3/3] 최종 리포트 생성 중(LLM)...
    - 리포트 생성 완료

완료! results/2026-03-15_travel_plan.md 를 확인하세요.
```

## 결과물 확인

`results/` 폴더에 실행 날짜 기준으로 두 파일이 생긴다.

- `results/<날짜>_raw.json` — 원본 데이터
  - `recommendation` : 1차 추천 JSON (도시 · 날씨 · 행사 · 이유)
  - `restaurants` : 맛집 리스트 (0건이면 빈 배열)
  - `errors` : 실행 중 발생한 오류 요약 (없으면 빈 배열)
- `results/<날짜>_travel_plan.md` — 최종 리포트

리포트 섹션은 아래 순서로 고정되어 있다.

```
# <날짜> 국내 여행 추천 리포트
## 추천 지역 / ## 추천 이유 / ## 날씨 요약 / ## 행사/축제
## 맛집 추천 / ## 1일 일정 제안 / ## 오류 요약(errors)
```

## 핵심 설계 결정

이 프로그램에서 "왜 이렇게 했지?"라는 질문이 나올 만한 지점만 추린다.

- **LLM 호출을 공식 SDK가 아니라 `requests`로 직접** — REST 요청/응답 구조(헤더에 키, POST 바디에 프롬프트)를 코드에 그대로 드러내려는 목적이고, 의존성도 `requests` 하나로 줄어든다.
- **1단계 출력을 JSON으로 강제** — 2단계가 `recommended_city`를 코드로 바로 꺼내 써야 하므로, 문장이 아니라 파싱 가능한 구조가 필요하다. OpenAI는 `response_format`, Gemini는 `responseMimeType`으로 JSON 모드를 켠다.
- **JSON 파싱 실패 시 재시도는 1회뿐** — 미션 제약(무한 재시도 금지). 1회 재요청 때는 "필수 4개 키만 순수 JSON으로" 프롬프트를 더 강하게 바꾼다.
- **지도 API가 실패해도 리포트는 계속 생성** — 맛집은 부가 정보라 없어도 리포트의 가치가 유지되고, 미션도 이를 요구한다. 실패는 맛집 "데이터 없음"으로 처리하고 `errors`에 기록한다.
- **키는 헤더로만 전달** — Kakao는 `Authorization: KakaoAK`, Naver는 `X-Naver-Client-*`, Gemini도 URL 쿼리스트링 대신 `x-goog-api-key` 헤더를 쓴다. 키가 URL·로그에 남는 것을 피하기 위해서다.

## 예외 처리

먼저 외부 API에서 나올 수 있는 오류를 나열하고, 각각의 대응을 정했다.

| 예상 오류 | 감지 방법 | 대응 |
|-----------|-----------|------|
| LLM 키 미설정 | 실행 시작 시 키 존재 확인 | 즉시 종료(코드 1) + 설정 방법 안내 |
| 지도 인증 실패 | HTTP 401 / 403 → `AUTH_ERROR` | 맛집 "데이터 없음", 리포트는 계속 |
| 쿼터 초과 | HTTP 429 → `QUOTA_ERROR` | 위와 동일 |
| 네트워크 오류 | `requests` 예외 → `NETWORK_ERROR` | 위와 동일 |
| 맛집 0건 | 결과 배열 비어 있음 → `EMPTY_RESULT` | 위와 동일 |
| LLM JSON 파싱 실패 | `json.loads` 실패 → `PARSE_ERROR` | 프롬프트 강화 후 1회 재시도 |

모든 오류는 `{step, type, message}` 형태로 `errors` 리스트에 쌓이고, 원본 JSON과 리포트 하단 "오류 요약" 섹션에 그대로 남는다. 오류가 없으면 빈 리스트다.

## 보너스: 결과 캐싱

같은 `--date`로 다시 실행하면, 이미 있는 `results/<날짜>_raw.json`을 재사용하고 API 호출을 건너뛴다. 외부 API 호출 비용·속도를 아끼기 위한 것이다. 새로 받으려면 `--refresh`를 붙인다.

## 한계와 미구현

실물과 문서를 맞추기 위해, 안 되는 것과 안 한 것을 그대로 적는다.

- **복수 지역 추천은 미구현.** 1차 추천은 `recommended_city` 한 곳만 낸다. 보너스 과제의 `recommended_cities`(2~3곳) 확장은 넣지 않았다.
- **캐시는 날짜만 기준으로 판단한다.** 원본 JSON 존재 여부만 보므로, 제공자(`LLM_PROVIDER`)나 모델을 바꾸고 같은 날짜로 실행해도 옛 캐시를 그대로 쓴다. 바뀐 설정으로 다시 받으려면 `--refresh`가 필요하다.
- **네트워크 재시도·백오프 없음.** 일시적 네트워크 오류도 1회 실패로 처리하고 넘어간다(맛집만 "데이터 없음").
- **Naver 좌표 변환은 현행 응답 포맷 가정.** `mapx/mapy`를 `÷1e7`로 변환하는데, 응답 규격이 다르면 좌표가 어긋날 수 있다. 실제 키로 첫 연동 시 응답 구조를 확인하는 게 안전하다.
- **날씨·행사는 검증되지 않은 생성 정보.** 실제 기상·축제 일정은 별도로 확인해야 한다.

## 보안 주의

- 실제 키가 든 `.env`는 절대 커밋·공유하지 않는다(`.gitignore`에 포함). 공유용으로는 값이 빈 `.env.example`만 쓴다.
- README·로그·결과 파일 어디에도 실제 키 값을 적지 않는다.
- 키가 유출되면 각 콘솔에서 즉시 재발급·폐기(rotate)한다.
