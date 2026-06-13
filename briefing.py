#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
재생에너지 모닝 브리핑 - Telegram 자동 전송
GitHub Actions로 평일 오전 8시(KST) 실행
  - 월요일: 토~월 기사 (72시간)
  - 화~금:  전날~당일 기사 (36시간)
  - 국내 70% : 해외 30% (카테고리당 국내 2건 + 해외 1건)
"""

import os
import re
import sys
import json
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# ───────── 환경 변수 (앞뒤 공백 제거) ────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = 645475613
KST       = timezone(timedelta(hours=9))

# ───────── 카테고리 정의 ──────────────────────────────────────────
CATS = [
    {"key": "solar",  "label": "☀️ 태양광 & 풍력"},
    {"key": "batt",   "label": "🔋 에너지 저장 (ESS/배터리)"},
    {"key": "policy", "label": "📋 정책 & 규제"},
    {"key": "invest", "label": "💼 기업 & 투자"},
]

# ───────── 국내 뉴스: Google News 한국어 RSS (카테고리별 검색) ────
def _gnews(q):
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

# 해외 기사 카테고리 분류 키워드
INTL_KW = {
    "solar":  ["solar", "wind", "photovoltaic", "pv", "turbine",
               "offshore wind", "onshore wind", "solar farm", "wind farm"],
    "batt":   ["battery", "energy storage", "ess", "bess", "lithium",
               "solid state battery", "grid storage", "long duration"],
    "policy": ["policy", "regulation", "government", "carbon", "net zero",
               "re100", "subsidy", "ira", "tariff", "mandate", "climate"],
    "invest": ["investment", "funding", "ipo", "acquisition", "merger",
               "deal", "billion", "million", "contract", "ppa", "venture"],
}

MAX_KR   = 2  # 카테고리당 국내 최대
MAX_INTL = 1  # 카테고리당 해외 최대


# ───────── 유틸 ──────────────────────────────────────────────────
def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def parse_date(s: str):
    """RFC 2822 / ISO 8601 → datetime(tz). 실패 시 None."""
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
    """RSS URL → [(title, pub_dt)] 목록 반환. 오류 시 빈 리스트."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RenewableBot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()

        # 인코딩 자동 감지 (EUC-KR 등 대응)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("euc-kr", errors="replace")
            text = re.sub(r'encoding="[^"]*"', 'encoding="utf-8"', text, count=1)
            raw  = text.encode("utf-8")

        root  = ET.fromstring(raw)
        items = []
        for item in root.findall(".//item"):
            title = strip_tags(item.findtext("title", ""))
            pub   = item.findtext("pubDate") or item.findtext(
                "{http://purl.org/dc/elements/1.1/}date", ""
            )
            if title:
                items.append((title, parse_date(pub)))
        return items

    except Exception as e:
        print(f"  ⚠️ 피드 오류 [{url[:70]}]: {e}", file=sys.stderr)
        return []


# ───────── 날짜 필터 ─────────────────────────────────────────────
def get_lookback() -> int:
    """월요일(KST) → 72h, 화~금 → 36h."""
    return 72 if datetime.now(KST).weekday() == 0 else 36


def is_recent(pub_dt, cutoff) -> bool:
    """pubDate 없는 기사(날짜 불명)는 포함, 있으면 cutoff 이후만."""
    return pub_dt is None or pub_dt >= cutoff


# ───────── 메시지 작성 ───────────────────────────────────────────
def build_message(kr: dict, intl: dict, lookback: int) -> str:
    now  = datetime.now(KST)
    days = ["월", "화", "수", "목", "금", "토", "일"]
    note = "📌 주말 포함 3일치 뉴스" if lookback == 72 else "📌 전일 기준 최신 뉴스"

    lines = [
        "⚡ 재생에너지 모닝 브리핑",
        f"📅 {now.year}년 {now.month}월 {now.day}일 ({days[now.weekday()]})",
        note,
        "━━━━━━━━━━━━━━━━━━",
        "",
    ]

    for cat in CATS:
        k = cat["key"]
        lines.append(cat["label"])

        articles = []
        for t in kr.get(k, []):
            articles.append(f"[국내] {t}")
        for t in intl.get(k, []):
            articles.append(f"[해외] {t}")

        if articles:
            for a in articles:
                lines.append(f"• {a}")
        else:
            lines.append("• 오늘 주요 동향 없음")
        lines.append("")

    lines += [
        "━━━━━━━━━━━━━━━━━━",
        "출처: Google News KR · CleanTechnica · Electrek · PV Magazine",
    ]
    return "\n".join(lines)


# ───────── Telegram 전송 ─────────────────────────────────────────
def send_telegram(message: str) -> bool:
    # 시크릿 검증
    if not BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN 없음", file=sys.stderr)
        return False
    if not CHAT_ID:
        print("❌ TELEGRAM_CHAT_ID 없음", file=sys.stderr)
        return False

    # Chat ID 숫자 변환
    print(f"  🔑 Token: ...{BOT_TOKEN[-8:]}")
    print(f"  🔑 Chat ID: {CHAT_ID}")

    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text":    message,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=payload,
        method="POST",
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


# ───────── 메인 ──────────────────────────────────────────────────
def main():
    now = datetime.now(KST)
    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] 재생에너지 브리핑 시작")

    lookback = get_lookback()
    cutoff   = datetime.now(timezone.utc) - timedelta(hours=lookback)
    print(f"  📅 조회 기간: 최근 {lookback}시간 ({cutoff.strftime('%m/%d %H:%M UTC')} 이후)")

    # ── 국내 뉴스 (카테고리별 Google News 검색) ──────────────────
    kr = {}
    for cat_key, url in KR_FEEDS.items():
        arts = fetch_rss(url)
        kr[cat_key] = [t for t, dt in arts if is_recent(dt, cutoff)][:MAX_KR]
    print(f"  🇰🇷 국내: {sum(len(v) for v in kr.values())}건")

    # ── 해외 뉴스 (전문 RSS → 키워드 분류) ──────────────────────
    raw_intl = []
    for url in INTL_FEEDS:
        raw_intl.extend(
            (t, dt) for t, dt in fetch_rss(url) if is_recent(dt, cutoff)
        )

    # 중복 제거
    seen, deduped = set(), []
    for t, dt in raw_intl:
        k = t[:60]
        if k not in seen:
            seen.add(k)
            deduped.append((t, dt))

    intl = {cat["key"]: [] for cat in CATS}
    for t, _ in deduped:
        text = t.lower()
        for cat in CATS:
            k = cat["key"]
            if len(intl[k]) >= MAX_INTL:
                continue
            if any(kw in text for kw in INTL_KW[k]):
                intl[k].append(t)
                break
    print(f"  🌐 해외: {sum(len(v) for v in intl.values())}건")

    # ── 메시지 작성 & 전송 ───────────────────────────────────────
    message = build_message(kr, intl, lookback)
    print(f"  📨 전송 중 ({len(message)}자)...")

    if send_telegram(message):
        print("  ✅ 텔레그램 전송 성공!")
    else:
        print("  ❌ 텔레그램 전송 실패")
        sys.exit(1)


if __name__ == "__main__":
    main()
