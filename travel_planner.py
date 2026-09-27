#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
travel_planner.py
------------------
Python 응용: API 활용 국내 여행지 추천 프로그램

흐름(3단계):
  [1/3] LLM API   : 입력한 날짜에 맞는 국내 여행 도시 + 날씨/행사 추천 (JSON)
  [2/3] 지도 API  : 추천 도시 기준 맛집 검색 (JSON)
  [3/3] LLM API   : 위 데이터를 합쳐 최종 여행 리포트 생성 (Markdown)

API 키는 절대 코드에 직접 쓰지 않고 .env / 환경변수에서 읽어온다.
"""

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

# .env 파일이 있으면 환경변수로 로드 (없어도 에러 아님)
load_dotenv()

# ------------------------------------------------------------------
# 설정 (환경변수로 제공자/모델을 바꿀 수 있게 함)
# ------------------------------------------------------------------
RESULTS_DIR = Path("results")

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()   # openai | gemini
MAP_PROVIDER = os.getenv("MAP_PROVIDER", "kakao").lower()    # kakao  | naver

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

REQUEST_TIMEOUT = 30  # 초


# ------------------------------------------------------------------
# 예외 / 유틸
# ------------------------------------------------------------------
class APIKeyMissingError(Exception):
    """필수 API 키가 설정되지 않았을 때 사용."""


def log(msg: str) -> None:
    """진행 로그 출력."""
    print(msg, flush=True)


def validate_date(date_str: str) -> bool:
    """YYYY-MM-DD 형식인지 검증."""
    try:
        datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def strip_tags(text: str) -> str:
    """<b> 같은 HTML 태그 제거 (네이버 응답 title 정리용)."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def extract_json(text: str):
    """
    LLM 응답 문자열에서 JSON 객체를 최대한 안전하게 추출/파싱한다.
    - ```json ... ``` 코드블록 제거
    - 첫 '{' 부터 마지막 '}' 까지 잘라서 파싱 시도
    실패하면 None 반환.
    """
    if not text:
        return None
    cleaned = text.strip()
    # 코드펜스 제거
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # 본문 어딘가에 섞여 있을 때: 중괄호 범위만 추출
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def classify_http_status(status: int) -> str:
    """HTTP 상태코드를 오류 타입 문자열로 분류."""
    if status in (401, 403):
        return "AUTH_ERROR"
    if status == 429:
        return "QUOTA_ERROR"
    return "HTTP_ERROR"


def add_error(errors: list, step: str, err_type: str, message: str) -> None:
    """오류 목록에 항목 추가."""
    errors.append({"step": step, "type": err_type, "message": str(message)[:300]})


# ------------------------------------------------------------------
# LLM 호출 (제공자별)
# ------------------------------------------------------------------
def call_openai(messages: list, json_mode: bool = False) -> str:
    """OpenAI Chat Completions API 호출 (POST). 응답 텍스트를 반환."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise APIKeyMissingError("OPENAI_API_KEY")

    body = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "temperature": 0.7,
    }
    if json_mode:
        # JSON 강제 모드 (프롬프트에 'JSON'이라는 단어가 포함되어 있어야 함)
        body["response_format"] = {"type": "json_object"}

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def call_gemini(messages: list, json_mode: bool = False) -> str:
    """Google Gemini generateContent API 호출 (POST). 응답 텍스트를 반환."""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise APIKeyMissingError("GOOGLE_API_KEY")

    # OpenAI 형식(messages)을 Gemini 형식으로 변환.
    # system 역할은 systemInstruction 으로, 나머지는 contents 로 넣는다.
    system_texts = [m["content"] for m in messages if m["role"] == "system"]
    user_texts = [m["content"] for m in messages if m["role"] != "system"]

    body = {
        "contents": [{"parts": [{"text": "\n\n".join(user_texts)}]}],
    }
    if system_texts:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_texts)}]}
    if json_mode:
        body["generationConfig"] = {"responseMimeType": "application/json"}

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    resp = requests.post(
        url,
        # 키는 URL 쿼리스트링이 아니라 헤더로 전달(로그 노출 방지)
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def call_llm(messages: list, json_mode: bool = False) -> str:
    """설정된 제공자로 LLM 호출을 위임."""
    if LLM_PROVIDER == "gemini":
        return call_gemini(messages, json_mode=json_mode)
    return call_openai(messages, json_mode=json_mode)


def check_llm_api_key() -> str | None:
    """LLM 키가 설정되어 있는지 확인. 없으면 필요한 키 이름을 반환."""
    if LLM_PROVIDER == "gemini":
        return None if os.getenv("GOOGLE_API_KEY") else "GOOGLE_API_KEY"
    return None if os.getenv("OPENAI_API_KEY") else "OPENAI_API_KEY"


# ------------------------------------------------------------------
# [1/3] 1차 추천 (LLM) — 날씨/행사 정보 JSON
# ------------------------------------------------------------------
REQUIRED_REC_KEYS = ("recommended_city", "weather", "events", "reason")


def _rec_messages(date: str, strict: bool = False) -> list:
    """추천용 프롬프트 메시지 구성. strict=True 는 파싱 실패 후 재시도용."""
    system = "당신은 대한민국 국내 여행 추천 전문가입니다. 반드시 유효한 JSON 하나만 출력합니다."
    if strict:
        user = (
            "직전 응답이 JSON 파싱에 실패했습니다. 설명이나 코드블록 없이, "
            "아래 4개 키만 가진 순수 JSON 객체 하나만 다시 출력하세요.\n"
            "recommended_city(string), weather(string), events(string 배열), reason(string)\n"
            f"여행 날짜: {date}"
        )
    else:
        user = (
            f"여행 날짜: {date}\n"
            "위 날짜에 국내(대한민국)에서 여행하기 좋은 도시 1곳을 추천하세요.\n"
            "다른 텍스트/설명/마크다운 없이 아래 JSON 스키마 객체 하나만 출력하세요.\n"
            "{\n"
            '  "recommended_city": "도시명 (예: 제주, 강릉)",\n'
            '  "weather": "해당 시기의 일반적인 날씨 요약 (1~2문장)",\n'
            '  "events": ["행사/축제 후보 1~3개"],\n'
            '  "reason": "추천 근거 2~4문장"\n'
            "}"
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def normalize_recommendation(data: dict) -> dict:
    """필수 키를 보정하고 타입을 맞춘다."""
    events = data.get("events", [])
    if isinstance(events, str):
        events = [events]
    return {
        "recommended_city": str(data.get("recommended_city", "")).strip(),
        "weather": str(data.get("weather", "")).strip(),
        "events": [str(e) for e in events][:3],
        "reason": str(data.get("reason", "")).strip(),
    }


def get_recommendation(date: str, errors: list) -> dict:
    """
    LLM으로 1차 추천 JSON을 생성.
    파싱 실패 시 최대 1회만 재시도(무한 재시도 금지).
    """
    raw = call_llm(_rec_messages(date), json_mode=True)
    data = extract_json(raw)

    # 파싱 실패 or 필수 키 누락 → 1회 재시도
    if not data or not all(k in data for k in REQUIRED_REC_KEYS):
        add_error(errors, "recommendation", "PARSE_ERROR",
                  "1차 응답 JSON 파싱 실패 → 재시도 1회 수행")
        raw = call_llm(_rec_messages(date, strict=True), json_mode=True)
        data = extract_json(raw)

    if not data:
        raise ValueError("LLM 추천 결과를 JSON으로 파싱하지 못했습니다(재시도 후에도 실패).")

    return normalize_recommendation(data)


# ------------------------------------------------------------------
# [2/3] 맛집 검색 (지도 API)
# ------------------------------------------------------------------
def search_kakao(city: str) -> list:
    """Kakao Local 키워드 검색 (GET). 인증 헤더에 REST 키를 실어 보낸다."""
    key = os.getenv("KAKAO_REST_API_KEY")
    if not key:
        raise APIKeyMissingError("KAKAO_REST_API_KEY")

    resp = requests.get(
        "https://dapi.kakao.com/v2/local/search/keyword.json",
        headers={"Authorization": f"KakaoAK {key}"},
        params={"query": f"{city} 맛집", "size": 5},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    docs = resp.json().get("documents", [])

    results = []
    for d in docs:
        # Kakao: x=경도(lng), y=위도(lat) — 문자열로 옴
        try:
            lng = float(d.get("x")) if d.get("x") else None
            lat = float(d.get("y")) if d.get("y") else None
        except (TypeError, ValueError):
            lng = lat = None
        results.append({
            "name": d.get("place_name", ""),
            "address": d.get("road_address_name") or d.get("address_name", ""),
            "category": d.get("category_name", ""),
            "url": d.get("place_url", ""),
            "lat": lat,
            "lng": lng,
        })
    return results


def search_naver(city: str) -> list:
    """Naver Local Search (GET). 클라이언트 ID/Secret 을 헤더로 보낸다."""
    cid = os.getenv("NAVER_CLIENT_ID")
    secret = os.getenv("NAVER_CLIENT_SECRET")
    if not cid or not secret:
        raise APIKeyMissingError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET")

    resp = requests.get(
        "https://openapi.naver.com/v1/search/local.json",
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
        params={"query": f"{city} 맛집", "display": 5},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])

    results = []
    for it in items:
        # Naver: mapx=경도, mapy=위도 를 WGS84 * 1e7 정수 문자열로 반환(현행 기준)
        try:
            lng = int(it.get("mapx")) / 1e7 if it.get("mapx") else None
            lat = int(it.get("mapy")) / 1e7 if it.get("mapy") else None
        except (TypeError, ValueError):
            lng = lat = None
        results.append({
            "name": strip_tags(it.get("title", "")),
            "address": it.get("roadAddress") or it.get("address", ""),
            "category": it.get("category", ""),
            "url": it.get("link", ""),
            "lat": lat,
            "lng": lng,
        })
    return results


def search_restaurants(city: str, errors: list) -> list:
    """
    지도 API로 맛집을 검색한다.
    실패(키 미설정/인증/쿼터/네트워크/0건)해도 예외를 밖으로 던지지 않고,
    errors 에 기록한 뒤 빈 리스트를 반환하여 리포트 생성이 계속되게 한다.
    """
    try:
        if MAP_PROVIDER == "naver":
            items = search_naver(city)
        else:
            items = search_kakao(city)

        if not items:
            add_error(errors, "place_search", "EMPTY_RESULT",
                      f"'{city} 맛집' 검색 결과 0건")
            log("    - 검색 결과 0건 → '데이터 없음'으로 처리하고 계속 진행")
            return []

        log(f"    - 맛집 {len(items)}곳 검색 완료")
        return items

    except APIKeyMissingError as e:
        add_error(errors, "place_search", "API_KEY_MISSING", f"{e} 미설정")
        log(f"    - 오류: 지도 API 키({e}) 미설정 → 맛집 '데이터 없음'으로 계속 진행")
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        err_type = classify_http_status(status)
        add_error(errors, "place_search", err_type, f"HTTP {status}")
        log(f"    - 오류: 지도 API 응답 HTTP {status} → 맛집 '데이터 없음'으로 계속 진행")
    except requests.RequestException as e:
        add_error(errors, "place_search", "NETWORK_ERROR", str(e))
        log("    - 오류: 네트워크 문제 → 맛집 '데이터 없음'으로 계속 진행")
    return []


# ------------------------------------------------------------------
# [3/3] 최종 리포트 (LLM + 폴백)
# ------------------------------------------------------------------
def _report_messages(date: str, rec: dict, restaurants: list) -> list:
    system = "당신은 여행 리포트 작성자입니다. 요청한 마크다운 형식으로만 작성합니다."
    payload = {
        "date": date,
        "recommendation": rec,
        "restaurants": restaurants,
    }
    user = (
        "아래 데이터로 국내 여행 리포트를 한국어 마크다운으로 작성하세요.\n"
        "반드시 아래 순서와 제목(## 헤더)을 그대로 사용하고, "
        "'## 오류 요약' 섹션은 넣지 마세요(프로그램이 별도로 추가합니다).\n\n"
        f"# {date} 국내 여행 추천 리포트\n"
        "## 추천 지역\n## 추천 이유\n## 날씨 요약\n## 행사/축제\n"
        "## 맛집 추천\n## 1일 일정 제안\n\n"
        "맛집 목록이 비어 있으면 '맛집 추천' 섹션에 '데이터 없음 (장소 검색 결과 0건)'"
        "이라고 적으세요. '1일 일정 제안'은 오전/오후/저녁으로 나눠 제안하세요.\n\n"
        "데이터(JSON):\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_errors_section(errors: list) -> str:
    """리포트 하단에 붙일 오류 요약 섹션(항상 정확하게 프로그램이 생성)."""
    lines = ["## 오류 요약(errors)", ""]
    if not errors:
        lines.append("- 오류 없음")
    else:
        for e in errors:
            lines.append(f"- **{e['step']}** / `{e['type']}` — {e['message']}")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
    lines.append("```")
    return "\n".join(lines)


def build_report_fallback(date: str, rec: dict, restaurants: list, errors: list) -> str:
    """LLM 없이 데이터만으로 리포트를 조립(오프라인/실패 시 폴백)."""
    events = rec.get("events") or []
    events_md = "\n".join(f"- {e}" for e in events) if events else "- 데이터 없음"

    if restaurants:
        rows = []
        for r in restaurants:
            parts = [f"**{r.get('name', '')}**"]
            if r.get("category"):
                parts.append(f"({r['category']})")
            line = f"- {' '.join(parts)}"
            if r.get("address"):
                line += f"\n  - 주소: {r['address']}"
            if r.get("url"):
                line += f"\n  - 링크: {r['url']}"
            rows.append(line)
        restaurants_md = "\n".join(rows)
    else:
        restaurants_md = "- 데이터 없음 (장소 검색 결과 0건)"

    city = rec.get("recommended_city", "")
    body = f"""# {date} 국내 여행 추천 리포트

## 추천 지역
{city}

## 추천 이유
{rec.get('reason', '') or '데이터 없음'}

## 날씨 요약
{rec.get('weather', '') or '데이터 없음'}

## 행사/축제
{events_md}

## 맛집 추천
{restaurants_md}

## 1일 일정 제안
- 오전: {city} 대표 명소 관광 후 가볍게 산책
- 오후: 위 맛집 리스트 중 한 곳에서 점심, 인근 카페/전시/자연 명소 탐방
- 저녁: 지역 대표 음식으로 저녁 식사 후 야경/휴식

"""
    return body + build_errors_section(errors) + "\n"


def generate_report(date: str, rec: dict, restaurants: list, errors: list,
                    mock: bool = False) -> str:
    """
    최종 리포트를 생성.
    - mock 모드가 아니면 LLM으로 본문을 생성하고, 실패하면 폴백으로 조립.
    - '오류 요약' 섹션은 항상 프로그램이 직접 붙여 정확도를 보장.
    """
    if not mock:
        try:
            body = call_llm(_report_messages(date, rec, restaurants), json_mode=False)
            body = body.strip()
            # LLM이 실수로 오류 섹션을 넣었다면 제거
            body = re.split(r"\n#+\s*오류 요약", body)[0].rstrip()
            return body + "\n\n" + build_errors_section(errors) + "\n"
        except Exception as e:  # noqa: BLE001 - 리포트는 어떤 경우에도 생성되어야 함
            add_error(errors, "report", "LLM_ERROR", str(e))
            log("    - 리포트 LLM 생성 실패 → 템플릿(폴백)으로 생성")

    return build_report_fallback(date, rec, restaurants, errors)


# ------------------------------------------------------------------
# 저장 / 캐시
# ------------------------------------------------------------------
def raw_json_path(date: str) -> Path:
    return RESULTS_DIR / f"{date}_raw.json"


def report_path(date: str) -> Path:
    return RESULTS_DIR / f"{date}_travel_plan.md"


def save_outputs(date: str, rec: dict, restaurants: list, errors: list,
                 report_md: str) -> tuple[Path, Path]:
    """원본 JSON과 리포트 Markdown을 results/ 에 저장."""
    RESULTS_DIR.mkdir(exist_ok=True)
    raw = {
        "date": date,
        "recommendation": rec,
        "restaurants": restaurants,
        "errors": errors,
    }
    rp = raw_json_path(date)
    mp = report_path(date)
    rp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    mp.write_text(report_md, encoding="utf-8")
    return rp, mp


# ------------------------------------------------------------------
# 메인 파이프라인
# ------------------------------------------------------------------
def run(date: str, mock: bool = False, refresh: bool = False) -> None:
    errors: list = []

    # (보너스) 캐싱: 같은 날짜의 원본 JSON이 이미 있으면 API 호출을 건너뛴다.
    if not refresh and raw_json_path(date).exists():
        log(f"캐시 발견: {raw_json_path(date)} (API 호출 없이 재사용)")
        cached = json.loads(raw_json_path(date).read_text(encoding="utf-8"))
        rec = cached.get("recommendation", {})
        restaurants = cached.get("restaurants", [])
        errors = cached.get("errors", [])
        # 리포트가 없으면 폴백으로 재생성
        if not report_path(date).exists():
            report_md = build_report_fallback(date, rec, restaurants, errors)
            save_outputs(date, rec, restaurants, errors, report_md)
        log(f"\n완료! {report_path(date)} 를 확인하세요.")
        return

    # LLM 키 확인 (mock 모드에서는 건너뜀). 미설정이면 즉시 종료 + 안내.
    if not mock:
        missing = check_llm_api_key()
        if missing:
            log(f"[오류] 필수 LLM API 키가 설정되지 않았습니다: {missing}")
            log("설정 방법(현재 터미널 세션 기준):")
            log(f'  macOS/Linux : export {missing}="YOUR_KEY"')
            log(f'  Windows PS  : $env:{missing}="YOUR_KEY"')
            log("또는 프로젝트 폴더에 .env 파일을 만들고 키를 적어주세요(.env.example 참고).")
            sys.exit(1)

    # [1/3] 1차 추천
    log("[1/3] 1차 추천 생성 중(LLM)...")
    if mock:
        rec = _mock_recommendation()
    else:
        try:
            rec = get_recommendation(date, errors)
        except APIKeyMissingError as e:
            log(f"[오류] LLM API 키 미설정: {e}")
            sys.exit(1)
        except Exception as e:  # noqa: BLE001
            log(f"[치명적 오류] 1차 추천 생성 실패: {e}")
            add_error(errors, "recommendation", "FATAL", str(e))
            # 추천이 없으면 이후 단계 의미가 없으므로 최소 리포트만 남기고 종료
            rec = {"recommended_city": "", "weather": "", "events": [], "reason": ""}
            report_md = build_report_fallback(date, rec, [], errors)
            save_outputs(date, rec, [], errors, report_md)
            sys.exit(1)
    log(f'    - recommended_city: "{rec.get("recommended_city", "")}"')

    # [2/3] 맛집 검색
    log("[2/3] 맛집 검색 중(지도/장소 API)...")
    if mock:
        restaurants = _mock_restaurants()
        log(f"    - 맛집 {len(restaurants)}곳 검색 완료")
    else:
        restaurants = search_restaurants(rec.get("recommended_city", ""), errors)

    # [3/3] 최종 리포트
    log("[3/3] 최종 리포트 생성 중(LLM)...")
    report_md = generate_report(date, rec, restaurants, errors, mock=mock)
    log("    - 리포트 생성 완료")

    rp, mp = save_outputs(date, rec, restaurants, errors, report_md)
    log(f"\n완료! {mp} 를 확인하세요. (원본 데이터: {rp})")


# ------------------------------------------------------------------
# Mock 데이터 (API 키 없이 전체 흐름/출력 형식을 확인하는 용도)
# ------------------------------------------------------------------
def _mock_recommendation() -> dict:
    return {
        "recommended_city": "제주",
        "weather": "3월 중순 평균 15°C 내외, 바람이 있으나 비교적 온화함",
        "events": ["유채꽃 축제", "제주 들불축제"],
        "reason": ("3월 중순은 제주가 봄꽃을 즐기기 좋은 시기입니다. "
                   "항공/숙박도 성수기 대비 부담이 적고, 야외 활동에 무리가 적습니다."),
    }


def _mock_restaurants() -> list:
    return [
        {"name": "제주 흑돼지 명가", "address": "제주 제주시 노형동 123",
         "category": "음식점 > 한식 > 육류,고기", "url": "http://place.map.kakao.com/1",
         "lat": 33.489, "lng": 126.498},
        {"name": "성산 해녀의 집", "address": "제주 서귀포시 성산읍 456",
         "category": "음식점 > 한식 > 해물,생선", "url": "http://place.map.kakao.com/2",
         "lat": 33.458, "lng": 126.941},
        {"name": "제주 고기국수 본점", "address": "제주 제주시 일도이동 789",
         "category": "음식점 > 한식 > 국수", "url": "http://place.map.kakao.com/3",
         "lat": 33.503, "lng": 126.529},
        {"name": "애월 오션뷰 카페", "address": "제주 제주시 애월읍 101",
         "category": "음식점 > 카페", "url": "http://place.map.kakao.com/4",
         "lat": 33.463, "lng": 126.309},
        {"name": "중문 갈치조림 맛집", "address": "제주 서귀포시 색달동 202",
         "category": "음식점 > 한식 > 해물,생선", "url": "http://place.map.kakao.com/5",
         "lat": 33.246, "lng": 126.412},
    ]


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="travel_planner.py",
        description="API를 활용한 국내 여행지 추천 프로그램 (LLM + 지도 API)",
    )
    p.add_argument("--date", "-d", required=True, metavar='"YYYY-MM-DD"',
                   help="여행 날짜 (예: 2026-03-15)")
    p.add_argument("--mock", action="store_true",
                   help="API 키/네트워크 없이 샘플 데이터로 전체 흐름을 확인")
    p.add_argument("--refresh", action="store_true",
                   help="캐시를 무시하고 API를 다시 호출")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not validate_date(args.date):
        log(f'[오류] 날짜 형식이 올바르지 않습니다: "{args.date}"')
        log('       YYYY-MM-DD 형식으로 입력하세요. 예) --date "2026-03-15"')
        parser.print_usage()
        sys.exit(2)

    run(args.date, mock=args.mock, refresh=args.refresh)


if __name__ == "__main__":
    main()
