#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
재생에너지 모닝 브리핑 — Telegram 자동 전송
- Google News 리다이렉트 → 실제 기사 URL 추출
- Gemini 1.5 Flash 고품질 요약 (제목 반복 방지 강화)
- TinyURL로 실제 기사 URL 단축
- 국내 70 : 해외 30 비율
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

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# ───────── 카테고리 ───────────────────────────────────────────────
CATS = [
    {"key": "solar",  "label": "☀️ 태양광 & 풍력"},
    {"key": "batt",   "label": "🔋 에너지 저장 (ESS/배터리)"},
    {"key": "policy", "label": "📋 정책 & 규제"},
    {"key": "invest", "label": "💼 기업 & 투자"},
]

# ───────── 국내 뉴스: Google News 한국어 RSS ─────────────────────
def _gnews(q: str) -> str:
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    )

KR_FEEDS = {
    "solar":  _gnews("태양광 풍력 재생에너지"),
    "batt":   _gnews("배터리 ESS 에너지저장장치"),
    "policy": _gnews("재생에너지 정책 탄소중립 RE100"),
    "invest": _gnews("재생에너지 투자 수주 계약"),
}

# ───────── 해외 뉴스: 전문 RSS 피드 ──────────────────────────────
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
    return re.sub(r"<[^>]+>", "", text or "").strip()

def clean_text(raw: str, max_len: int = 500) -> str:
    t = re.sub(r"\s+", " ", strip_tags(raw)).strip()
    return (t[:max_len] + "…") if len(t) > max_len else t

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


# ───────── URL 처리 ──────────────────────────────────────────────
def resolve_url(url: str) -> str:
    """
    Google News 리다이렉트 URL → 실제 기사 URL 추출.
    ex) https://news.google.com/rss/articles/... → https://www.hankyung.com/...
    실패 시 원본 URL 반환.
    """
    if not url:
        return url
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            final = resp.geturl()
            # Google 로그인/동의 페이지면 HTML에서 canonical 찾기
            if "google.com" in final or "accounts.google" in final:
                html = resp.read().decode("utf-8", errors="replace")
                # <link rel="canonical"> 또는 meta refresh
                m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', html, re.I)
                if m:
                    return m.group(1)
                m = re.search(r'content=["\']0;\s*url=([^"\']+)["\']', html, re.I)
                if m:
                    return m.group(1)
                return url  # 실패
            return final
    except Exception:
        return url


def shorten_url(url: str) -> str:
    """TinyURL로 URL 단축 (무료, API 키 불필요). 실패 시 원본 반환."""
    if not url or len(url) < 40:
        return url
    try:
        api = "https://tinyurl.com/api-create.php?" + urllib.parse.urlencode({"url": url})
        req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            short = resp.read().decode("utf-8").strip()
        return short if short.startswith("http") else url
    except Exception:
        return url


# ───────── 기사 본문 수집 ────────────────────────────────────────
def fetch_article_body(url: str, max_chars: int = 1200) -> str:
    """
    실제 기사 URL(리다이렉트 해소 후)에서 본문 텍스트 추출.
    실패 시 빈 문자열.
    """
    if not url or "google.com" in url:
        return ""
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw_html = resp.read()

        # 인코딩 감지
        try:
            html = raw_html.decode("utf-8")
        except UnicodeDecodeError:
            html = raw_html.decode("euc-kr", errors="replace")

        # <article> 영역 우선 시도
        article_m = re.search(r"<article[^>]*>(.*?)</article>", html, re.DOTALL | re.IGNORECASE)
        source = article_m.group(1) if article_m else html

        # <p> 태그 추출
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", source, re.DOTALL | re.IGNORECASE)
        body = " ".join(
            strip_tags(p) for p in paragraphs
            if len(strip_tags(p)) > 50        # 짧은 캡션·광고 제외
        )
        body = re.sub(r"\s+", " ", body).strip()

        if len(body) < 80:
            return ""
        return (body[:max_chars] + "…") if len(body) > max_chars else body

    except Exception:
        return ""


# ───────── RSS 수집 ──────────────────────────────────────────────
def fetch_rss(url: str):
    """RSS URL → [(title, pub_dt, desc, link)] 리스트"""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RenewableBot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()

        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("euc-kr", errors="replace")
            text = re.sub(r'encoding="[^"]*"', 'encoding="utf-8"', text, count=1)
            raw  = text.encode("utf-8")

        root  = ET.fromstring(raw)
        items = []
        for item in root.findall(".//item"):
            title = strip_tags(item.findtext("title", ""))
            desc  = item.findtext("description", "") or ""
            link  = (
                item.findtext("link", "") or item.findtext("guid", "") or ""
            ).strip()
            pub   = item.findtext("pubDate") or item.findtext(
                "{http://purl.org/dc/elements/1.1/}date", ""
            )
            if title:
                items.append((title, parse_date(pub), desc, link))
        return items

    except Exception as e:
        print(f"  ⚠️ 피드 오류 [{url[:70]}]: {e}", file=sys.stderr)
        return []


# ───────── Gemini 고품질 요약 ────────────────────────────────────
_BAD_GOOD_EXAMPLE = """
[나쁜 예 — 절대 이렇게 하지 마세요]
제목: "삼성SDI, 미국에 배터리 공장 건설 추진"
  × ① 삼성SDI가 미국에 배터리 공장을 건설하려 함
  × ② 삼성SDI의 미국 배터리 공장 추진 계획이 공개됨
  × ③ 미국에 배터리 공장이 세워질 예정임

[좋은 예 — 이렇게 작성하세요]
  ① IRA 세액공제(최대 $35/kWh) 확보 위한 현지 생산 전략
  ② 미 전기차 시장 성장으로 현지 조달 압박 거세져
  ③ LG엔솔·SK온과 북미 투자 경쟁 심화, 공급과잉 리스크도
"""


def _call_gemini(prompt: str) -> str:
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 220,
            "temperature":     0.25,
        },
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
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ⚠️ Gemini HTTP 오류: {e} → {body[:150]}", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"  ⚠️ Gemini 오류: {e}", file=sys.stderr)
        return ""


def summarize(title: str, desc: str, body: str) -> str:
    """
    Gemini 고품질 요약.
    - body(기사 본문) 있으면 → 팩트 기반 요약
    - body 없으면 → Gemini 업계 지식 기반 분석
    - 제목 반복 방지 규칙 엄격 적용
    """
    if not GEMINI_KEY:
        return clean_text(desc or body, 150)

    if body:
        prompt = f"""당신은 재생에너지 전문 애널리스트입니다.
아래 기사 본문을 바탕으로 3줄 핵심 요약을 작성하세요.

[기사 제목]
{title}

[기사 본문]
{body}

[엄격한 작성 규칙 — 위반 시 무효]
1. 제목 문장을 그대로 쓰거나 단어 순서·표현만 바꾸는 것 절대 금지
2. ①은 반드시 수치·날짜·기업명·지명 등 구체적 팩트 포함
3. ②는 이 뉴스의 배경이 되는 업계 맥락 (제목에 없는 정보)
4. ③은 투자자·경쟁사·정책에 미치는 시사점
5. 영문 기사라면 한국어로 번역해서 작성
6. 각 줄 40자 이내, 간결·전문적으로
{_BAD_GOOD_EXAMPLE}
출력 형식만 작성 (다른 설명·문장 없이):
① [구체적 팩트]
② [업계 배경]
③ [시사점]"""

    else:
        desc_hint = clean_text(desc, 300) if desc else ""
        prompt = f"""당신은 재생에너지 업계 수석 애널리스트입니다.
아래 헤드라인만 보고, 업계 전문 지식을 적극 활용해 3줄 분석을 작성하세요.

[뉴스 헤드라인]
{title}
{f"[추가 단서]{chr(10)}{desc_hint}" if desc_hint else ""}

[엄격한 작성 규칙 — 반드시 준수]
1. 헤드라인 문장을 그대로 쓰거나 비슷한 말로 바꾸는 것 절대 금지
2. 헤드라인에 이미 있는 정보(기업명·행위)를 단순 반복하는 것 금지
3. ①은 헤드라인 이면의 핵심 — 왜(why)/얼마나(scale)/어떻게(how)
4. ②는 이 뉴스가 나온 시장·업계 배경·트렌드 (헤드라인에 없는 맥락)
5. ③은 투자자·기업·정책 관점의 구체적 시사점
6. 불확실한 내용은 "~로 보임", "~가능성" 등 표현 사용
7. 각 줄 40자 이내
{_BAD_GOOD_EXAMPLE}
출력 형식만 작성 (다른 설명·문장 없이):
① [핵심 이면]
② [업계 배경]
③ [시사점]"""

    result = _call_gemini(prompt)

    # ①②③ 형식 검증
    if result and "①" in result and "②" in result:
        return result
    # fallback: desc가 있으면 정제해서 반환
    return clean_text(desc or "", 150)


# ───────── 날짜 필터 ─────────────────────────────────────────────
def get_lookback() -> int:
    return 72 if datetime.now(KST).weekday() == 0 else 36

def is_recent(pub_dt, cutoff) -> bool:
    return pub_dt is None or pub_dt >= cutoff


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
        "출처: Google News KR · CleanTechnica · Electrek · PV Magazine"
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
        print(f"❌ Telegram 응답 오류: {result}", file=sys.stderr)
        return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"❌ Telegram HTTP 오류: {e} → {body}", file=sys.stderr)
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

    # ── 국내 뉴스 수집 ──────────────────────────────────────────
    kr_raw = {}
    for cat_key, url in KR_FEEDS.items():
        selected = []
        for title, pub_dt, desc, link in fetch_rss(url):
            if is_recent(pub_dt, cutoff) and len(selected) < MAX_KR:
                selected.append((title, desc, link))
        kr_raw[cat_key] = selected
    print(f"  🇰🇷 국내: {sum(len(v) for v in kr_raw.values())}건")

    # ── 해외 뉴스 수집 ──────────────────────────────────────────
    raw_intl = []
    for url in INTL_FEEDS:
        raw_intl.extend(
            (t, dt, d, l) for t, dt, d, l in fetch_rss(url)
            if is_recent(dt, cutoff)
        )
    seen, deduped = set(), []
    for item in raw_intl:
        k = item[0][:60]
        if k not in seen:
            seen.add(k)
            deduped.append(item)

    intl_raw = {cat["key"]: [] for cat in CATS}
    for t, _, d, l in deduped:
        txt = t.lower()
        for cat in CATS:
            k = cat["key"]
            if len(intl_raw[k]) >= MAX_INTL:
                continue
            if any(kw in txt for kw in INTL_KW[k]):
                intl_raw[k].append((t, d, l))
                break
    print(f"  🌐 해외: {sum(len(v) for v in intl_raw.values())}건")

    # ── URL 해소 + 본문 수집 + 요약 + 단축 ─────────────────────
    all_articles = []
    for cat_key, articles in kr_raw.items():
        for title, desc, raw_link in articles:
            all_articles.append(("kr", cat_key, title, desc, raw_link))
    for cat_key, articles in intl_raw.items():
        for title, desc, raw_link in articles:
            all_articles.append(("intl", cat_key, title, desc, raw_link))

    total = len(all_articles)
    print(f"  🔗 URL 해소 + 본문 수집 + 요약 중 (총 {total}건)...")

    kr   = {cat["key"]: [] for cat in CATS}
    intl = {cat["key"]: [] for cat in CATS}

    for i, (src, cat_key, title, desc, raw_link) in enumerate(all_articles, 1):
        print(f"    [{i}/{total}] {title[:50]}...")

        # 1) Google News 리다이렉트 → 실제 기사 URL
        real_url = resolve_url(raw_link) if "google.com" in raw_link else raw_link
        print(f"         URL: {real_url[:70]}")

        # 2) 본문 수집
        body = fetch_article_body(real_url)
        has_body = len(body) > 80
        print(f"         본문: {'있음 ({} chars)'.format(len(body)) if has_body else '없음 → 지식 기반 요약'}")

        # 3) Gemini 요약
        summary = summarize(title, clean_text(desc, 300), body if has_body else "")

        # 4) URL 단축 (실제 기사 URL 기준)
        short_url = shorten_url(real_url)

        time.sleep(0.8)  # Gemini rate limit

        if src == "kr":
            kr[cat_key].append((title, summary, short_url))
        else:
            intl[cat_key].append((title, summary, short_url))

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
