#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
재생에너지 모닝 브리핑 - Telegram 자동 전송
GitHub Actions로 평일 오전 8시(KST) 실행
  - 월요일: 토~월 기사 (72시간)
  - 화~금:  전날~당일 기사 (36시간)
"""

import os
import re
import sys
import json
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import html

# ───────── 설정 ─────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
KST       = timezone(timedelta(hours=9))

# RSS 피드 목록
RSS_FEEDS = [
    "https://cleantechnica.com/feed/",
    "https://electrek.co/feed/",
    "https://www.pv-magazine.com/feed/",
    "https://www.renewableenergyworld.com/feed/",
    "https://energymonitor.ai/feed/",
]

# 분야별 분류 키워드
CATEGORIES = [
    {
        "key":      "solar_wind",
        "label":    "☀️ 태양광 & 풍력",
        "keywords": [
            "solar", "wind", "photovoltaic", "pv panel", "turbine",
            "offshore wind", "onshore wind", "rooftop solar", "floating solar",
            "agrivoltaic", "solar farm", "wind farm", "gigawatt",
        ],
    },
    {
        "key":      "battery",
        "label":    "🔋 에너지 저장 (ESS/배터리)",
        "keywords": [
            "battery", "energy storage", "ess", "bess", "lithium",
            "lfp", "solid state battery", "grid storage", "long duration",
            "flow battery", "sodium battery", "battery storage",
        ],
    },
    {
        "key":      "policy",
        "label":    "📋 정책 & 규제",
        "keywords": [
            "policy", "regulation", "legislation", "government", "ministry",
            "carbon", "re100", "net zero", "ira ", "inflation reduction",
            "renewable target", "subsidy", "tariff", "green deal",
            "carbon tax", "emission", "climate", "mandate",
        ],
    },
    {
        "key":      "investment",
        "label":    "💼 기업 & 투자",
        "keywords": [
            "investment", "funding", "ipo", "acquisition", "merger",
            "deal", "billion", "million dollar", "venture", "startup",
            "contract", "project finance", "ppa", "offtake",
        ],
    },
]

MAX_PER_CATEGORY = 3   # 분야별 최대 기사 수


# ───────── 날짜 파싱 ──────────────────────────────────────────────────────
def parse_pub_date(date_str: str):
    """RSS pubDate 문자열 → datetime(tz aware). 실패 시 None."""
    if not date_str:
        return None
    date_str = date_str.strip()
    # RFC 2822 (Mon, 09 Jun 2025 12:00:00 +0000)
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        pass
    # ISO 8601 (2025-06-09T12:00:00Z)
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


# ───────── RSS 수집 ──────────────────────────────────────────────────────
def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def fetch_feed(url: str, timeout: int = 12):
    """RSS URL → 기사 리스트 [(title, pub_date, summary)]"""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RenewableBot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read()
        root = ET.fromstring(content)
        items = []
        for item in root.findall(".//item"):
            title   = strip_html(item.findtext("title", ""))
            pub_raw = item.findtext("pubDate") or item.findtext(
                "{http://purl.org/dc/elements/1.1/}date", ""
            )
            pub_dt  = parse_pub_date(pub_raw)
            desc    = strip_html(item.findtext("description", ""))[:300]
            if title:
                items.append((title, pub_dt, desc))
        return items
    except Exception as e:
        print(f"  ⚠️  피드 오류 [{url}]: {e}", file=sys.stderr)
        return []


# ───────── 날짜 필터 ──────────────────────────────────────────────────────
def get_lookback_hours() -> int:
    """
    월요일(KST) → 72h (토~월 커버)
    화~금(KST) → 36h (전날~당일 커버, 약간의 여유 포함)
    """
    now_kst = datetime.now(KST)
    return 72 if now_kst.weekday() == 0 else 36


# ───────── 기사 분류 ──────────────────────────────────────────────────────
def categorize(articles):
    """articles: [(title, pub_date, desc)] → {category_key: [title, ...]}"""
    result  = {cat["key"]: [] for cat in CATEGORIES}
    used    = set()

    for title, _, desc in articles:
        text = (title + " " + desc).lower()
        for cat in CATEGORIES:
            if len(result[cat["key"]]) >= MAX_PER_CATEGORY:
                continue
            if any(kw in text for kw in cat["keywords"]):
                key = title[:60]
                if key not in used:
                    result[cat["key"]].append(title)
                    used.add(key)
                    break   # 하나의 기사는 첫 번째 매칭 분야에만

    return result


# ───────── 메시지 작성 ──────────────────────────────────────────────────
def build_message(categorized: dict, lookback_hours: int) -> str:
    now_kst = datetime.now(KST)
    days    = ["월", "화", "수", "목", "금", "토", "일"]
    date_str = (
        f"{now_kst.year}년 {now_kst.month}월 {now_kst.day}일"
        f" ({days[now_kst.weekday()]})"
    )

    # 월요일이면 "지난 주말 포함" 메모
    period_note = "📌 주말 포함 3일치 뉴스" if lookback_hours == 72 else "📌 전일 기준 최신 뉴스"

    lines = [
        "⚡ 재생에너지 모닝 브리핑",
        f"📅 {date_str}",
        period_note,
        "━━━━━━━━━━━━━━━━━━",
        "",
    ]

    for cat in CATEGORIES:
        articles = categorized.get(cat["key"], [])
        lines.append(cat['label'])
        if articles:
            for a in articles:
                lines.append(f"• {html.escape(a)}")
        else:
            lines.append("• 오늘 주요 동향 없음")
        lines.append("")

    lines += [
        "━━━━━━━━━━━━━━━━━━",
        "🌐 CleanTechnica · Electrek · PV Magazine · REWorld",
    ]
    return "\n".join(lines)


# ───────── Telegram 전송 ──────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ 환경 변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 없음", file=sys.stderr)
        return False

    payload = json.dumps({
        "chat_id": int(CHAT_ID),
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
        return result.get("ok", False)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"❌ Telegram 전송 오류: {e} → {body}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"❌ Telegram 전송 오류: {e}", file=sys.stderr)
        return False


# ───────── 메인 ──────────────────────────────────────────────────────────
def main():
    now_kst = datetime.now(KST)
    print(f"[{now_kst.strftime('%Y-%m-%d %H:%M KST')}] 재생에너지 브리핑 시작")

    # 1. 조회 기간 결정
    lookback_hours = get_lookback_hours()
    cutoff         = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    print(f"  📅 조회 기간: 최근 {lookback_hours}시간 ({cutoff.strftime('%m/%d %H:%M UTC')} 이후)")

    # 2. RSS 수집 + 날짜 필터
    all_articles = []
    for url in RSS_FEEDS:
        items = fetch_feed(url)
        for title, pub_dt, desc in items:
            # pubDate 없는 기사는 포함 (날짜 불명확하면 일단 포함)
            if pub_dt is None or pub_dt >= cutoff:
                all_articles.append((title, pub_dt, desc))
    print(f"  → {len(all_articles)}개 기사 수집")

    # 중복 제목 제거
    seen, unique = set(), []
    for item in all_articles:
        key = item[0][:60]
        if key not in seen:
            seen.add(key)
            unique.append(item)
    print(f"  → 중복 제거 후 {len(unique)}개")

    # 3. 분류
    categorized = categorize(unique)

    # 4. 메시지 작성 & 전송
    message = build_message(categorized, lookback_hours)
    print(f"  📨 전송 중 ({len(message)}자)")

    if send_telegram(message):
        print("  ✅ 텔레그램 전송 성공")
    else:
        print("  ❌ 텔레그램 전송 실패")
        sys.exit(1)


if __name__ == "__main__":
    main()
