#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
재생에너지 모닝 브리핑 — Telegram 자동 전송
핵심 변경: Google News RSS 제거 → 재생에너지 전문 언론사 직접 RSS
국내: 이투뉴스·에너지경제·에너지데일리·그린포스트코리아 (본문 포함)
해외: CleanTechnica·Electrek·PV Magazine (content:encoded 추출)
"""

import os, re, sys, json, time
import urllib.request, urllib.error, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# ───────── 설정 ──────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
CHAT_ID    = 645475613
KST        = timezone(timedelta(hours=9))
MAX_MSG    = 4000

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta"
    "/models/gemini-1.5-flash:generateContent?key={key}"
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# RSS content:encoded 네임스페이스
_CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
_DC_NS      = "{http://purl.org/dc/elements/1.1/}"

# ───────── 카테고리 ───────────────────────────────────────────────
CATS = [
    {"key": "solar",  "label": "☀️ 태양광 & 풍력"},
    {"key": "batt",   "label": "🔋 에너지 저장 (ESS/배터리)"},
    {"key": "policy", "label": "📋 정책 & 규제"},
    {"key": "invest", "label": "💼 기업 & 투자"},
]

# ───────── 국내: 재생에너지 전문 언론사 직접 RSS ─────────────────
# Google News 대신 본문이 포함된 전문 언론사 RSS 사용
KR_FEEDS = [
    "https://www.e2news.com/rss/allArticle.xml",          # 이투뉴스
    "https://www.ekn.kr/rss/allArticle.xml",              # 에너지경제
    "https://www.energydaily.co.kr/rss/allArticle.xml",   # 에너지데일리
    "https://www.greenpostkorea.co.kr/rss/allArticle.xml", # 그린포스트코리아
    "https://www.koenergy.co.kr/rss/allArticle.xml",      # 한국에너지신문
]

KR_KW = {
    "solar":  ["태양광", "풍력", "재생에너지", "신재생", "해상풍력", "육상풍력", "태양전지", "페로브스카이트"],
    "batt":   ["배터리", "ESS", "에너지저장", "전고체", "리튬", "LFP", "NMC", "파워월", "BESS", "저장장치"],
    "policy": ["정책", "규제", "탄소중립", "RE100", "RPS", "탄소세", "전력시장", "넷제로", "온실가스", "기후"],
    "invest": ["투자", "수주", "계약", "인수", "합병", "IPO", "상장", "펀드", "조달", "프로젝트", "PPA"],
}

# ───────── 해외: 전문 RSS (content:encoded 포함) ─────────────────
INTL_FEEDS = [
    "https://cleantechnica.com/feed/",
    "https://electrek.co/feed/",
    "https://www.pv-magazine.com/feed/",
    "https://www.renewableenergyworld.com/feed/",
    "https://energymonitor.ai/feed/",
]

INTL_KW = {
    "solar":  ["solar", "wind", "photovoltaic", "pv", "turbine",
               "offshore wind", "onshore wind", "solar farm", "wind farm"],
    "batt":   ["battery", "energy storage", "ess", "bess", "lithium",
               "solid state", "grid storage", "long duration storage"],
    "policy": ["policy", "regulation", "government", "carbon", "net zero",
               "re100", "subsidy", "ira", "tariff", "climate act"],
    "invest": ["investment", "funding", "ipo", "acquisition", "deal",
               "billion", "million", "contract", "ppa", "project finance"],
}

MAX_KR   = 2
MAX_INTL = 1


# ───────── 텍스트 유틸 ───────────────────────────────────────────
def strip_tags(text: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text or "", flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_body(desc: str, content: str) -> str:
    """
    RSS description / content:encoded 중 더 긴 것을 선택해 정제.
    최대 1000자 반환.
    """
    d = strip_tags(desc or "")
    c = strip_tags(content or "")
    raw = c if len(c) > len(d) else d
    raw = re.sub(r"\s+", " ", raw).strip()
    # 광고·저작권 문구 제거
    raw = re.sub(r"(기자\s*=|▶|☞|Copyright|저작권|무단\s*전재|배포\s*금지|All rights reserved).*", "", raw)
    raw = raw.strip()
    if len(raw) > 1000:
        raw = raw[:1000] + "…"
    return raw


def parse_date(s: str):
    if not s:
        return None
    try:
        return parsedate_to_datetime(s.strip())
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except Exception:
        pass
    return None


# ───────── RSS 수집 ──────────────────────────────────────────────
def fetch_rss(url: str):
    """
    RSS URL → [(title, pub_dt, body_text, link)] 리스트.
    content:encoded 네임스페이스 포함 추출.
    """
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept": "application/rss+xml, application/xml, */*"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()

        # 인코딩 처리
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("euc-kr", errors="replace")
            text = re.sub(r'encoding="[^"]*"', 'encoding="utf-8"', text, count=1)
            raw  = text.encode("utf-8")

        # XML 파싱 (네임스페이스 등록)
        ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")
        root  = ET.fromstring(raw)
        items = []
        for item in root.findall(".//item"):
            title   = strip_tags(item.findtext("title", ""))
            desc    = item.findtext("description", "") or ""
            content = item.findtext(f"{_CONTENT_NS}encoded", "") or ""
            link    = (item.findtext("link") or item.findtext("guid") or "").strip()
            pub     = (
                item.findtext("pubDate") or
                item.findtext(f"{_DC_NS}date", "") or ""
            )
            if title:
                body = extract_body(desc, content)
                items.append((title, parse_date(pub), body, link))
        return items

    except Exception as e:
        print(f"  ⚠️ 피드 오류 [{url[:70]}]: {e}", file=sys.stderr)
        return []


# ───────── 기사 링크 ─────────────────────────────────────────────
def article_url(link: str, title: str) -> str:
    """
    국내 기사: RSS 원본 링크 그대로 사용 (한 번 클릭으로 기사 이동).
    링크가 없거나 Google 도메인이면 Naver News 검색 URL 생성.
    해외 기사: RSS 원본 링크 그대로 사용.
    """
    if link and "google.com" not in link:
        return link
    # Naver News 검색 URL (기사 제목으로 검색 → 첫 번째 결과가 해당 기사)
    q = urllib.parse.quote(title)
    return f"https://search.naver.com/search.naver?where=news&query={q}"


# ───────── Gemini 요약 ───────────────────────────────────────────
def _call_gemini(prompt: str) -> str:
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 350, "temperature": 0.2},
    }).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_URL.format(key=GEMINI_KEY),
        data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
        return result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        print(f"  ⚠️ Gemini 오류: {e} → {e.read().decode()[:100]}", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"  ⚠️ Gemini 오류: {e}", file=sys.stderr)
        return ""


def summarize(title: str, body: str) -> str:
    """
    국내·해외 공통 — 신문 소제목 스타일 3문장 한국어 요약.
    본문 600자 발췌 제공, 문장 완결 허용(토큰 여유 확보).
    """
    if not GEMINI_KEY:
        return body[:150] if body else ""

    body_excerpt = body[:600] if body else ""

    prompt = f"""당신은 재생에너지 전문 신문의 수석 에디터입니다.

[기사]
제목: {title}
{"본문: " + body_excerpt if body_excerpt else "(본문 없음 — 제목과 업계 지식만으로 작성)"}

[임무]
이 기사의 핵심을 신문 소제목 스타일로 3문장 요약하세요.
출력은 반드시 한국어입니다 (영문 기사도 한국어로).

[소제목 스타일]
- 각 문장은 완결된 하나의 소제목 — 독자가 3줄만 읽어도 기사 전체를 이해해야 함
- 수치·기업명·규모 등 구체적 팩트를 담아 임팩트 있게
- ①: 핵심 사건/결정 (무슨 일이, 누가, 얼마 규모로)
- ②: 배경·맥락 (왜 이 일이 일어났는지, 업계 트렌드)
- ③: 파급효과·전망 (업계·투자자·정책에 미치는 영향)

[절대 금지]
- 본문 문장 그대로 복사 또는 단어만 교체
- 제목 내용을 반복
- "~했다", "~밝혔다" 등 단순 보도체 종결어미

[좋은 예]
제목: "한화큐셀, 미국 조지아주 태양광 공장 확장 발표"
① 한화큐셀, 조지아 2공장 2026년 착공…모듈 연 4GW 추가·고용 2,500명
② IRA 현지생산 세액공제($35/kWh) 직접 수혜, 원가경쟁력 확보 목표
③ 美 반덤핑 관세 강화 속 현지화 선점…LG엔솔·SK온 추격전 불가피

[나쁜 예 — 절대 이렇게 쓰지 말 것]
① 한화큐셀이 미국에 태양광 공장을 확장한다고 발표했다
② 공장 확장으로 생산능력이 늘어날 것으로 예상된다
③ 관련 업계의 관심이 집중되고 있다

아래 형식만 출력 (번호·문장만, 추가 설명 없이):
①
②
③"""

    result = _call_gemini(prompt)
    if result and "①" in result:
        return result
    return body[:150] if body else ""


# ───────── 날짜 필터 ─────────────────────────────────────────────
def get_lookback() -> int:
    return 72 if datetime.now(KST).weekday() == 0 else 36

def is_recent(pub_dt, cutoff) -> bool:
    if pub_dt is None:
        return True
    # timezone-naive → UTC로 간주
    if pub_dt.tzinfo is None:
        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    return pub_dt >= cutoff


# ───────── 기사 수집 (키워드 필터링) ────────────────────────────
def collect_kr(cutoff) -> dict:
    """국내 전문 언론사 RSS → 키워드 분류 → 카테고리별 최대 MAX_KR건"""
    raw_all = []
    for url in KR_FEEDS:
        for title, pub_dt, body, link in fetch_rss(url):
            if is_recent(pub_dt, cutoff):
                raw_all.append((title, body, link))

    # 중복 제거
    seen, deduped = set(), []
    for item in raw_all:
        k = item[0][:60]
        if k not in seen:
            seen.add(k)
            deduped.append(item)

    result = {cat["key"]: [] for cat in CATS}
    for title, body, link in deduped:
        txt = title + " " + body[:200]
        for cat in CATS:
            k = cat["key"]
            if len(result[k]) >= MAX_KR:
                continue
            if any(kw in txt for kw in KR_KW[k]):
                result[k].append((title, body, link))
                break
    return result


def collect_intl(cutoff) -> dict:
    """해외 전문 RSS → 키워드 분류 → 카테고리별 최대 MAX_INTL건"""
    raw_all = []
    for url in INTL_FEEDS:
        for title, pub_dt, body, link in fetch_rss(url):
            if is_recent(pub_dt, cutoff):
                raw_all.append((title, body, link))

    seen, deduped = set(), []
    for item in raw_all:
        k = item[0][:60]
        if k not in seen:
            seen.add(k)
            deduped.append(item)

    result = {cat["key"]: [] for cat in CATS}
    for title, body, link in deduped:
        txt = title.lower() + " " + body[:200].lower()
        for cat in CATS:
            k = cat["key"]
            if len(result[k]) >= MAX_INTL:
                continue
            if any(kw in txt for kw in INTL_KW[k]):
                result[k].append((title, body, link))
                break
    return result


# ───────── 메시지 빌드 ───────────────────────────────────────────
def fmt_article(title: str, summary: str, url: str, tag: str) -> str:
    lines = [f"• {tag} {title}"]
    for line in (summary or "").splitlines():
        line = line.strip()
        if line:
            lines.append(f"  {line}")
    if url:
        lines.append(f"  🔗 {url}")
    return "\n".join(lines)


def build_messages(kr: dict, intl: dict, lookback: int) -> list:
    now  = datetime.now(KST)
    days = ["월", "화", "수", "목", "금", "토", "일"]
    note = "📌 주말 포함 3일치" if lookback == 72 else "📌 전일 기준 최신"

    header = "\n".join([
        "⚡ 재생에너지 모닝 브리핑",
        f"📅 {now.year}년 {now.month}월 {now.day}일 ({days[now.weekday()]})",
        note,
        "━━━━━━━━━━━━━━━━━━",
    ])
    footer = (
        "━━━━━━━━━━━━━━━━━━\n"
        "출처: 이투뉴스·에너지경제·에너지데일리·그린포스트 / "
        "CleanTechnica·Electrek·PV Magazine"
    )

    sections = []
    for cat in CATS:
        k     = cat["key"]
        lines = [cat["label"]]
        for title, summary, url in kr.get(k, []):
            lines.append(fmt_article(title, summary, url, "[국내]"))
        for title, summary, url in intl.get(k, []):
            lines.append(fmt_article(title, summary, url, "[해외]"))
        if len(lines) == 1:
            lines.append("• 오늘 주요 동향 없음")
        sections.append("\n".join(lines))

    msgs, current = [], header + "\n\n"
    for sec in sections:
        candidate = current + sec + "\n\n"
        if len(candidate) > MAX_MSG and len(current) > len(header) + 5:
            msgs.append(current.rstrip())
            current = sec + "\n\n"
        else:
            current = candidate
    msgs.append((current + footer).rstrip())
    return msgs


# ───────── Telegram 전송 ─────────────────────────────────────────
def _send_once(text: str) -> bool:
    if not BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN 없음", file=sys.stderr)
        return False
    payload = json.dumps({"chat_id": CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            return True
        print(f"❌ Telegram 오류: {result}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"❌ Telegram 전송 오류: {e}", file=sys.stderr)
        return False

def send_all(messages: list) -> bool:
    for i, msg in enumerate(messages):
        label = f"({i+1}/{len(messages)}) " if len(messages) > 1 else ""
        print(f"  📨 전송 중 {label}({len(msg)}자)...")
        if not _send_once(msg):
            return False
        if i < len(messages) - 1:
            time.sleep(1)
    return True


# ───────── 메인 ──────────────────────────────────────────────────
def main():
    now = datetime.now(KST)
    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] 재생에너지 브리핑 시작")
    print(f"  🤖 Gemini: {'활성화' if GEMINI_KEY else '비활성(fallback)'}")

    lookback = get_lookback()
    cutoff   = datetime.now(timezone.utc) - timedelta(hours=lookback)
    print(f"  📅 조회 기간: 최근 {lookback}시간")

    # ── 뉴스 수집 ────────────────────────────────────────────────
    kr_raw   = collect_kr(cutoff)
    intl_raw = collect_intl(cutoff)
    print(f"  🇰🇷 국내: {sum(len(v) for v in kr_raw.values())}건")
    print(f"  🌐 해외: {sum(len(v) for v in intl_raw.values())}건")

    # ── 요약 + URL 단축 ──────────────────────────────────────────
    all_items = []
    for cat_key in [c["key"] for c in CATS]:
        for t, b, l in kr_raw.get(cat_key, []):
            all_items.append(("kr", cat_key, t, b, l))
        for t, b, l in intl_raw.get(cat_key, []):
            all_items.append(("intl", cat_key, t, b, l))

    total = len(all_items)
    print(f"  ✍️  요약 중 (총 {total}건)...")

    kr   = {c["key"]: [] for c in CATS}
    intl = {c["key"]: [] for c in CATS}

    for i, (src, cat_key, title, body, link) in enumerate(all_items, 1):
        body_len = len(body)
        print(f"    [{i}/{total}] {title[:45]}... [본문 {body_len}자]")

        summary  = summarize(title, body)
        art_url  = article_url(link, title)
        time.sleep(0.8)

        if src == "kr":
            kr[cat_key].append((title, summary, art_url))
        else:
            intl[cat_key].append((title, summary, art_url))

    # ── 메시지 빌드 & 전송 ──────────────────────────────────────
    messages = build_messages(kr, intl, lookback)
    print(f"  📝 메시지 {len(messages)}개 생성")

    if send_all(messages):
        print("  ✅ 텔레그램 전송 성공!")
    else:
        print("  ❌ 텔레그램 전송 실패")
        sys.exit(1)


if __name__ == "__main__":
    main()
