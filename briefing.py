#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
재생에너지 모닝 브리핑 - Telegram 자동 전송
Gemini 1.5 Flash 고품질 3줄 요약 / TinyURL 단축 / 국내 70:해외 30
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


# ───────── HTML / 텍스트 유틸 ────────────────────────────────────
def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def clean_text(raw: str, max_len: int = 800) -> str:
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


# ───────── 기사 본문 수집 ────────────────────────────────────────
def fetch_article_body(url: str, max_chars: int = 1000) -> str:
    """
    기사 URL → 본문 텍스트 추출.
    Google News 리다이렉트도 자동 처리. 실패 시 빈 문자열.
    """
    if not url:
        return ""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # <article> 또는 <p> 태그에서 본문 추출
        article_match = re.search(
            r"<article[^>]*>(.*?)</article>", html, re.DOTALL | re.IGNORECASE
        )
        source = article_match.group(1) if article_match else html

        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", source, re.DOTALL | re.IGNORECASE)
        body = " ".join(
            strip_tags(p) for p in paragraphs
            if len(strip_tags(p)) > 40          # 짧은 캡션 제외
        )
        body = re.sub(r"\s+", " ", body).strip()

        if len(body) < 100:
            return ""
        return (body[:max_chars] + "…") if len(body) > max_chars else body

    except Exception:
        return ""


# ───────── RSS 수집 ──────────────────────────────────────────────
def fetch_rss(url: str):
    """RSS URL → [(title, pub_dt, desc, link)]"""
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


# ───────── URL 단축 (TinyURL) ────────────────────────────────────
def shorten_url(url: str) -> str:
    """TinyURL API로 URL 단축 (무료, API 키 불필요). 실패 시 원본 반환."""
    if not url:
        return url
    try:
        api = "https://tinyurl.com/api-create.php?" + urllib.parse.urlencode({"url": url})
        req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            short = resp.read().decode("utf-8").strip()
        return short if short.startswith("http") else url
    except Exception:
        return url


# ───────── Gemini 고품질 요약 ────────────────────────────────────
def _call_gemini(prompt: str) -> str:
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 200,
            "temperature":     0.3,
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


def summarize(title: str, desc: str, link: str) -> str:
    """
    기사 본문 수집 → Gemini 고품질 요약.
    본문 없으면 Gemini 배경 지식 기반 요약.
    API 키 없거나 오류 시 RSS description fallback.
    """
    if not GEMINI_KEY:
        return clean_text(desc, 120) if desc else ""

    # 1) 기사 본문 수집 시도
    body = fetch_article_body(link)

    # 2) 본문 있으면 → 팩트 기반 요약
    if body:
        prompt = f"""당신은 재생에너지 전문 애널리스트입니다.
아래 기사 본문을 바탕으로 핵심 정보를 3줄로 요약하세요.

[기사 제목]
{title}

[기사 본문]
{body}

[요약 원칙]
- 제목을 그대로 반복하지 말 것
- ①은 무슨 일이 일어났는지 (구체적 사실: 회사명·수치·지역 포함)
- ②는 규모·배경·관련 주체 등 맥락
- ③은 업계·시장·정책에 미치는 영향 또는 의의
- 영문 기사라면 한국어로 번역해서 작성
- 각 줄 35자 이내, 간결하고 전문적으로

출력 형식만 작성 (다른 설명 없이):
① [핵심 사실]
② [배경·맥락]
③ [영향·의의]"""

    # 3) 본문 없으면 → Gemini 배경 지식 기반 분석
    else:
        desc_hint = clean_text(desc, 300) if desc else ""
        prompt = f"""당신은 재생에너지 업계 전문 애널리스트입니다.
아래 뉴스 헤드라인을 보고, 재생에너지 업계 전문 지식을 바탕으로 3줄 분석을 작성하세요.

[뉴스 헤드라인]
{title}
{f'[추가 정보]{chr(10)}{desc_hint}' if desc_hint else ''}

[작성 원칙]
- 헤드라인을 단순히 반복하지 말 것
- ①은 이 뉴스의 핵심 사실이나 변화 (구체적으로)
- ②는 이 뉴스가 나온 업계 배경 또는 트렌드
- ③은 기업·투자자·정책 관점에서의 시사점
- 불확실한 내용은 "~로 보임", "~가능성" 등으로 표현
- 각 줄 35자 이내

출력 형식만 작성 (다른 설명 없이):
① [핵심 사실]
② [업계 배경]
③ [시사점]"""

    result = _call_gemini(prompt)

    # 결과 검증: ①②③ 형식인지 확인
    if result and "①" in result:
        return result
    # fallback
    return clean_text(desc, 120) if desc else ""


# ───────── 날짜 필터 ─────────────────────────────────────────────
def get_lookback() -> int:
    return 72 if datetime.now(KST).weekday() == 0 else 36


def is_recent(pub_dt, cutoff) -> bool:
    return pub_dt is None or pub_dt >= cutoff


# ───────── 메시지 빌드 ───────────────────────────────────────────
def fmt_article(title: str, summary: str, short_url: str, tag: str) -> str:
    lines = [f"• {tag} {title}"]
    for line in (summary or "").splitlines():
        line = line.strip()
        if line:
            lines.append(f"  {line}")
    if short_url:
        lines.append(f"  🔗 {short_url}")
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
        items = []
        for title, summary, url in kr.get(k, []):
            items.append(fmt_article(title, summary, url, "[국내]"))
        for title, summary, url in intl.get(k, []):
            items.append(fmt_article(title, summary, url, "[해외]"))
        lines += items if items else ["• 오늘 주요 동향 없음"]
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

    # ── Gemini 요약 + TinyURL 단축 ──────────────────────────────
    total = (sum(len(v) for v in kr_raw.values())
             + sum(len(v) for v in intl_raw.values()))
    print(f"  ✍️  요약 + URL 단축 중 (총 {total}건)...")
    count = 0

    def process(articles):
        nonlocal count
        result = []
        for title, desc, link in articles:
            count += 1
            print(f"    [{count}/{total}] {title[:45]}...")
            summary   = summarize(title, desc, link)
            short_url = shorten_url(link)
            result.append((title, summary, short_url))
            time.sleep(0.8)   # Gemini rate limit 여유
        return result

    kr   = {k: process(v) for k, v in kr_raw.items()}
    intl = {k: process(v) for k, v in intl_raw.items()}

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
