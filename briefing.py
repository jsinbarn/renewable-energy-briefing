#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
재생에너지 모닝 브리핑 - Telegram 자동 전송
Gemini 1.5 Flash로 기사별 3줄 한국어 요약 생성
국내 70% : 해외 30% / 평일 오전 8시(KST) 실행
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
MAX_MSG    = 4000   # 텔레그램 메시지 분할 기준

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


# ───────── RSS 파싱 유틸 ─────────────────────────────────────────
def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def clean_desc(raw: str, max_len: int = 150) -> str:
    t = re.sub(r"\s+", " ", strip_tags(raw)).strip()
    if len(t) < 20:
        return ""
    return t[:max_len].rsplit(" ", 1)[0] + "…" if len(t) > max_len else t


def parse_date(s: str):
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


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
            link  = (item.findtext("link", "") or item.findtext("guid", "") or "").strip()
            pub   = item.findtext("pubDate") or item.findtext(
                "{http://purl.org/dc/elements/1.1/}date", ""
            )
            if title:
                items.append((title, parse_date(pub), desc, link))
        return items

    except Exception as e:
        print(f"  ⚠️ 피드 오류 [{url[:70]}]: {e}", file=sys.stderr)
        return []


# ───────── Gemini 3줄 요약 ───────────────────────────────────────
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta"
    "/models/gemini-1.5-flash:generateContent?key={key}"
)

def summarize(title: str, desc: str) -> str:
    """
    Gemini 1.5 Flash로 기사 3줄 한국어 요약 생성.
    API 키 없거나 오류 시 RSS description으로 fallback.
    """
    if not GEMINI_KEY:
        return clean_desc(desc)

    raw_desc = clean_desc(desc, max_len=500)
    prompt = (
        "아래 재생에너지 기사를 한국어로 핵심만 3줄 요약해주세요.\n"
        "영문 기사라면 한국어로 번역해서 요약하세요.\n\n"
        f"제목: {title}\n"
        f"내용: {raw_desc if raw_desc else '(본문 없음 — 제목 기반으로 요약)'}\n\n"
        "출력 형식 (이 형식만 출력, 다른 설명 없이):\n"
        "① [핵심 내용 1 — 20자 이내]\n"
        "② [핵심 내용 2 — 20자 이내]\n"
        "③ [핵심 내용 3 — 20자 이내]"
    )

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 150,
            "temperature":     0.2,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        GEMINI_URL.format(key=GEMINI_KEY),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
        text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ⚠️ Gemini HTTP 오류: {e} → {body[:200]}", file=sys.stderr)
        return clean_desc(desc)
    except Exception as e:
        print(f"  ⚠️ Gemini 오류: {e}", file=sys.stderr)
        return clean_desc(desc)


# ───────── 날짜 필터 ─────────────────────────────────────────────
def get_lookback() -> int:
    return 72 if datetime.now(KST).weekday() == 0 else 36


def is_recent(pub_dt, cutoff) -> bool:
    return pub_dt is None or pub_dt >= cutoff


# ───────── 메시지 빌드 ───────────────────────────────────────────
def fmt_article(title: str, summary: str, link: str, tag: str) -> str:
    lines = [f"• {tag} {title}"]
    if summary:
        for line in summary.splitlines():
            line = line.strip()
            if line:
                lines.append(f"  {line}")
    if link:
        lines.append(f"  🔗 {link}")
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
    footer = "━━━━━━━━━━━━━━━━━━\n출처: Google News KR · CleanTechnica · Electrek · PV Magazine"

    sections = []
    for cat in CATS:
        k     = cat["key"]
        lines = [cat["label"]]

        items = []
        for title, summary, link in kr.get(k, []):
            items.append(fmt_article(title, summary, link, "[국내]"))
        for title, summary, link in intl.get(k, []):
            items.append(fmt_article(title, summary, link, "[해외]"))

        lines += items if items else ["• 오늘 주요 동향 없음"]
        sections.append("\n".join(lines))

    # 4000자 기준 분할
    msgs    = []
    current = header + "\n\n"
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
    total = len(messages)
    for i, msg in enumerate(messages):
        label = f"({i+1}/{total}) " if total > 1 else ""
        print(f"  📨 전송 중 {label}({len(msg)}자)...")
        if not _send_once(msg):
            return False
        if i < total - 1:
            time.sleep(1)
    return True


# ───────── 메인 ──────────────────────────────────────────────────
def main():
    now = datetime.now(KST)
    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] 재생에너지 브리핑 시작")
    print(f"  🤖 Gemini 요약: {'활성화' if GEMINI_KEY else '비활성 (RSS fallback)'}")

    lookback = get_lookback()
    cutoff   = datetime.now(timezone.utc) - timedelta(hours=lookback)
    print(f"  📅 조회 기간: 최근 {lookback}시간 ({cutoff.strftime('%m/%d %H:%M UTC')} 이후)")

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
            (t, dt, d, l)
            for t, dt, d, l in fetch_rss(url)
            if is_recent(dt, cutoff)
        )
    seen, deduped = set(), []
    for item in raw_intl:
        key = item[0][:60]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    intl_raw = {cat["key"]: [] for cat in CATS}
    for t, _, d, l in deduped:
        text = t.lower()
        for cat in CATS:
            k = cat["key"]
            if len(intl_raw[k]) >= MAX_INTL:
                continue
            if any(kw in text for kw in INTL_KW[k]):
                intl_raw[k].append((t, d, l))
                break
    print(f"  🌐 해외: {sum(len(v) for v in intl_raw.values())}건")

    # ── Gemini 요약 생성 ─────────────────────────────────────────
    print("  ✍️  Gemini 요약 생성 중...")
    total_articles = sum(len(v) for v in kr_raw.values()) + sum(len(v) for v in intl_raw.values())
    count = 0

    kr = {}
    for cat_key, articles in kr_raw.items():
        kr[cat_key] = []
        for title, desc, link in articles:
            count += 1
            print(f"    [{count}/{total_articles}] {title[:40]}...")
            summary = summarize(title, desc)
            kr[cat_key].append((title, summary, link))
            time.sleep(0.5)   # Gemini rate limit 여유

    intl = {}
    for cat_key, articles in intl_raw.items():
        intl[cat_key] = []
        for title, desc, link in articles:
            count += 1
            print(f"    [{count}/{total_articles}] {title[:40]}...")
            summary = summarize(title, desc)
            intl[cat_key].append((title, summary, link))
            time.sleep(0.5)

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
