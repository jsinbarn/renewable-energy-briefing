#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
재생에너지 모닝 브리핑 — Telegram 자동 전송
핵심 변경(고도화):
  1) RSS 발췌본이 아니라 기사 '원문 본문 전체'를 직접 다운로드·추출해 요약 입력으로 사용
  2) 본문 추출 실패(차단/오류) 시 RSS 발췌본으로 자동 폴백
  3) Gemini 모델 업데이트(1.5-flash 종료 → 기본 2.5-flash, GEMINI_MODEL로 override)
     + 본문 최대 4000자 전달 / 출력 토큰 상향 / 소제목 요약 프롬프트 강화
  4) Gemini 미사용 시에도 본문 기반 3줄 폴백 요약 생성
국내: 이투뉴스·에너지경제·에너지데일리·그린포스트코리아·한국에너지신문 (본문 직접 수집)
해외: CleanTechnica·Electrek·PV Magazine·REW·EnergyMonitor (본문 직접 수집)
"""

import os, re, sys, json, time
import urllib.request, urllib.error, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# ───────── 설정 ──────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "").strip()
# 1.5-flash / 2.0-flash 는 종료됨 → 기본값 2.5-flash (필요 시 환경변수로 변경)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
CHAT_ID      = 645475613
KST          = timezone(timedelta(hours=9))
MAX_MSG      = 4000

# 요약 입력으로 넘길 본문 최대 길이(글자)
BODY_MAX_FOR_SUMMARY = 4000

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta"
    "/models/{model}:generateContent?key={key}"
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

# 본문에서 걸러낼 보일러플레이트(저작권·구독·공유 안내 등)
_BOILERPLATE_PAT = re.compile(
    r"(저작권|무단\s*전재|재배포\s*금지|All rights reserved|Copyright|"
    r"구독\s*신청|뉴스레터|newsletter|subscribe|관련\s*기사|이\s*기사를\s*공유|"
    r"sign up|advertisement|광고)", re.IGNORECASE
)


# ───────── 텍스트 유틸 ───────────────────────────────────────────
def strip_tags(text: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text or "", flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"&[a-z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_body(raw: str, limit: int = BODY_MAX_FOR_SUMMARY) -> str:
    """본문 정제: 공백 정리 + 광고/저작권 꼬리 제거 + 길이 제한."""
    raw = re.sub(r"\s+", " ", raw or "").strip()
    raw = re.sub(
        r"(기자\s*=|▶|☞|Copyright|저작권|무단\s*전재|배포\s*금지|All rights reserved).*",
        "", raw
    ).strip()
    if len(raw) > limit:
        raw = raw[:limit] + "…"
    return raw


def extract_body(desc: str, content: str) -> str:
    """RSS description / content:encoded 중 더 긴 것을 정제(폴백용)."""
    d = strip_tags(desc or "")
    c = strip_tags(content or "")
    raw = c if len(c) > len(d) else d
    return clean_body(raw, limit=1500)


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


# ───────── 기사 본문 전체 추출 (핵심 고도화) ─────────────────────
def _extract_main_text(html: str) -> str:
    """
    기사 HTML에서 본문 텍스트를 추출.
    전략: 비콘텐츠 영역 제거 → <article> 우선 → <p> 단락 밀도 기반 추출.
    외부 패키지 없이 표준 라이브러리(regex)만 사용.
    """
    if not html:
        return ""

    # 1) 비콘텐츠 영역 제거 (head/title 포함)
    html = re.sub(r"(?is)<head[^>]*>.*?</head>", " ", html)
    html = re.sub(
        r"(?is)<(script|style|noscript|template|svg|nav|header|footer|aside|form)[^>]*>.*?</\1>",
        " ", html
    )

    # 2) 본문 후보 영역 선정: <article> → 본문 컨테이너 → 전체
    region = html
    m = re.search(r"(?is)<article[^>]*>(.*?)</article>", html)
    if m and len(m.group(1)) > 400:
        region = m.group(1)
    else:
        # 흔한 본문 컨테이너 id/class 패턴
        m2 = re.search(
            r'(?is)<div[^>]*(?:id|class)="[^"]*(?:article|content|view|entry|post|news)[^"]*"[^>]*>(.*?)</div>\s*(?:</div>|<footer|$)',
            html
        )
        if m2 and len(m2.group(1)) > 400:
            region = m2.group(1)

    # 3) <p> 단락 추출
    paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", region)
    texts = []
    for p in paras:
        t = strip_tags(p)
        if len(t) >= 40 and not _BOILERPLATE_PAT.search(t):
            texts.append(t)

    body = " ".join(texts)

    # 4) <p>가 거의 없으면(단락 태그 미사용 사이트) <br>/<div> 기반 폴백
    if len(body) < 200:
        flat = re.sub(r"(?is)<br[^>]*>", "\n", region)
        flat = strip_tags(flat)
        body = flat if len(flat) > len(body) else body

    return clean_body(body)


def fetch_full_article(url: str) -> str:
    """
    기사 원문 페이지를 다운로드해 본문 전체를 추출.
    실패하거나 차단되면 빈 문자열 반환(호출부에서 RSS 폴백).
    """
    if not url or "search.naver.com" in url or "news.google.com" in url:
        return ""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype.lower() and ctype:
                return ""
            raw = resp.read(2_000_000)  # 최대 2MB
            charset = resp.headers.get_content_charset()

        html = None
        for enc in [charset, "utf-8", "euc-kr", "cp949"]:
            if not enc:
                continue
            try:
                html = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if html is None:
            html = raw.decode("utf-8", errors="replace")

        return _extract_main_text(html)
    except Exception as e:
        print(f"  ⚠️ 본문 수집 실패 [{url[:60]}]: {e}", file=sys.stderr)
        return ""


# ───────── RSS 수집 ──────────────────────────────────────────────
def fetch_rss(url: str):
    """RSS URL → [(title, pub_dt, body_text, link)] 리스트."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept": "application/rss+xml, application/xml, */*"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()

        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("euc-kr", errors="replace")
            text = re.sub(r'encoding="[^"]*"', 'encoding="utf-8"', text, count=1)
            raw  = text.encode("utf-8")

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
    if link and "google.com" not in link:
        return link
    q = urllib.parse.quote(title)
    return f"https://search.naver.com/search.naver?where=news&query={q}"


# ───────── Gemini 요약 ───────────────────────────────────────────
# 2.5-flash 등은 기본적으로 'thinking'이 켜져 있어 출력 토큰을 추론에 소비 →
# 요약이 중간에 잘리는 원인. thinkingBudget=0 으로 끄고 출력 토큰을 넉넉히 확보.
# (thinkingConfig 미지원 모델이면 자동 비활성 후 재시도)
_GEN_THINKING = {"enabled": True}


def _call_gemini(prompt: str, retries: int = 1) -> str:
    gen = {"maxOutputTokens": 1024, "temperature": 0.3}
    if _GEN_THINKING["enabled"]:
        gen["thinkingConfig"] = {"thinkingBudget": 0}
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen,
    }).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_URL.format(model=GEMINI_MODEL, key=GEMINI_KEY),
        data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        cand  = (result.get("candidates") or [{}])[0]
        parts = cand.get("content", {}).get("parts", []) or []
        # 텍스트 파트만 결합(추론/기타 파트 제외)
        return "".join(
            p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p
        ).strip()
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode()[:200]
        except Exception:
            msg = ""
        # thinkingConfig 미지원 → 비활성 후 1회 재시도
        if _GEN_THINKING["enabled"] and ("thinking" in msg.lower() or e.code == 400):
            _GEN_THINKING["enabled"] = False
            if retries > 0:
                return _call_gemini(prompt, retries - 1)
        print(f"  ⚠️ Gemini 오류: {e} → {msg}", file=sys.stderr)
        return ""
    except Exception as e:
        if retries > 0:
            time.sleep(1.0)
            return _call_gemini(prompt, retries - 1)
        print(f"  ⚠️ Gemini 오류: {e}", file=sys.stderr)
        return ""


def _clip_sentence(s: str, n: int = 110) -> str:
    """문장을 단어 경계에서 깔끔하게 자른다(중간에 잘리지 않도록)."""
    s = s.strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    if sp > n * 0.5:
        cut = cut[:sp]
    return cut.rstrip(" ,.·–-") + "…"


def _fallback_summary(title: str, body: str) -> str:
    """Gemini 미사용/실패 시: 본문 앞부분을 완결된 3개 문장으로 분할한 폴백 요약."""
    if not body:
        return ""
    # 한국어 '~다.' 및 영어 문장부호 기준 분리
    sents = re.split(r"(?<=다\.)\s*|(?<=[.!?])\s+", body)
    sents = [s.strip() for s in sents if len(s.strip()) > 15]
    picked = sents[:3] if sents else [body[:110]]
    marks = ["①", "②", "③"]
    return "\n".join(f"{marks[i]} {_clip_sentence(s)}" for i, s in enumerate(picked))


def summarize(title: str, body: str) -> str:
    """본문 전체를 입력으로 받아 신문 소제목 스타일 3문장 한국어 요약."""
    if not GEMINI_KEY:
        return _fallback_summary(title, body)

    body_excerpt = (body or "")[:BODY_MAX_FOR_SUMMARY]
    has_body = len(body_excerpt) > 80

    prompt = f"""당신은 재생에너지 전문 신문의 수석 에디터입니다.

[기사]
제목: {title}
{"본문: " + body_excerpt if has_body else "(본문 없음 — 제목과 업계 지식만으로 작성)"}

[임무]
위 기사 본문 전체를 읽고 핵심만 신문 소제목 스타일로 3문장 요약하세요.
출력은 반드시 한국어입니다. 해외(영문) 기사도 반드시 한국어로 번역·요약하세요. 영어 단어를 그대로 나열하지 마세요(고유명사·약어 제외).
본문에 실제로 등장한 사실(수치·기업명·날짜·규모)만 사용하고, 본문에 없는 내용은 지어내지 마세요.

[소제목 스타일]
- 각 문장은 완결된 하나의 소제목 — 독자가 3줄만 읽어도 기사 전체를 이해해야 함
- 수치·기업명·규모 등 본문의 구체적 팩트를 담아 임팩트 있게
- ①: 핵심 사건/결정 (무슨 일이, 누가, 얼마 규모로)
- ②: 배경·맥락 (왜 이 일이 일어났는지, 업계 트렌드)
- ③: 파급효과·전망 (업계·투자자·정책에 미치는 영향)
- 각 줄은 40자 안팎으로 간결하게, 단 반드시 완결된 구(句)로 끝낼 것

[문장 완결 — 매우 중요]
- 세 줄 모두 의미가 끊기지 않는 완성된 문장/구로 작성
- 말줄임표(…)나 미완성 형태로 끝내지 말 것
- 문장이 길어질 것 같으면 내용을 줄여서라도 반드시 완결할 것

[절대 금지]
- 본문 문장 그대로 복사 또는 단어만 교체
- 제목 내용을 단순 반복
- "~했다", "~밝혔다" 등 단순 보도체 종결어미 (체언/명사형 종결로)

[좋은 예]
제목: "한화큐셀, 미국 조지아주 태양광 공장 확장 발표"
① 한화큐셀, 조지아 2공장 2026년 착공…모듈 연 4GW 추가·고용 2,500명
② IRA 현지생산 세액공제 직접 수혜, 원가경쟁력 확보 목표
③ 美 반덤핑 관세 강화 속 현지화 선점…경쟁사 추격전 불가피

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
        # ①②③ 줄만 남기고 꼬리 말줄임표·잡음 정리
        lines = []
        for ln in result.splitlines():
            ln = ln.strip()
            if ln and ln[0] in "①②③":
                ln = ln.rstrip(" .…")
                lines.append(ln)
        if len(lines) >= 3:
            return "\n".join(lines[:3])
        return result
    return _fallback_summary(title, body)


# ───────── 날짜 필터 ─────────────────────────────────────────────
def get_lookback() -> int:
    return 72 if datetime.now(KST).weekday() == 0 else 36

def is_recent(pub_dt, cutoff) -> bool:
    if pub_dt is None:
        return True
    if pub_dt.tzinfo is None:
        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    return pub_dt >= cutoff


# ───────── 기사 수집 (전역 중복 제거 + 카테고리 우선순위) ────────
def _dedup_key(title: str) -> str:
    """제목 정규화 키: 소문자화 + 공백/기호 제거 후 앞 40자."""
    return re.sub(r"[^0-9a-z가-힣]", "", (title or "").lower())[:40]


def _categorize(txt: str, kw_map: dict):
    """CATS 정의 순서대로 검사 → 가장 먼저 매칭되는 카테고리 1개만 반환."""
    for cat in CATS:
        if any(kw in txt for kw in kw_map[cat["key"]]):
            return cat["key"]
    return None


def collect_feeds(feeds, kw_map, max_per, lower, cutoff, seen) -> dict:
    """
    피드 수집 → 전역 중복 제거(제목+링크, seen 공유) →
    겹치는 기사는 CATS 순서상 가장 빠른 카테고리에만 1회 배정.
    seen 은 국내·해외 호출 간 공유되어 브리핑 전체에서 중복을 제거한다.
    """
    result = {cat["key"]: [] for cat in CATS}
    for url in feeds:
        for title, pub_dt, body, link in fetch_rss(url):
            if not is_recent(pub_dt, cutoff):
                continue
            tkey = _dedup_key(title)
            lkey = (link or "").split("?")[0].rstrip("/")
            # 이미 등장한 기사(제목 또는 링크 중복)는 건너뜀
            if (tkey and tkey in seen) or (lkey and lkey in seen):
                continue
            if tkey:
                seen.add(tkey)
            if lkey:
                seen.add(lkey)

            txt = (title + " " + body[:200])
            if lower:
                txt = txt.lower()
            cat = _categorize(txt, kw_map)
            if cat is None or len(result[cat]) >= max_per:
                continue
            result[cat].append((title, body, link))
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
    print(f"  🤖 Gemini: {'활성화 (' + GEMINI_MODEL + ')' if GEMINI_KEY else '비활성(fallback)'}")

    lookback = get_lookback()
    cutoff   = datetime.now(timezone.utc) - timedelta(hours=lookback)
    print(f"  📅 조회 기간: 최근 {lookback}시간")

    # 국내·해외가 seen 을 공유 → 브리핑 전체에서 중복 기사 제거,
    # 겹치는 기사는 가장 빠른 카테고리에만 배정
    seen = set()
    kr_raw   = collect_feeds(KR_FEEDS,   KR_KW,   MAX_KR,   False, cutoff, seen)
    intl_raw = collect_feeds(INTL_FEEDS, INTL_KW, MAX_INTL, True,  cutoff, seen)
    print(f"  🇰🇷 국내: {sum(len(v) for v in kr_raw.values())}건")
    print(f"  🌐 해외: {sum(len(v) for v in intl_raw.values())}건")

    all_items = []
    for cat_key in [c["key"] for c in CATS]:
        for t, b, l in kr_raw.get(cat_key, []):
            all_items.append(("kr", cat_key, t, b, l))
        for t, b, l in intl_raw.get(cat_key, []):
            all_items.append(("intl", cat_key, t, b, l))

    total = len(all_items)
    print(f"  ✍️  본문 수집 + 요약 중 (총 {total}건)...")

    kr   = {c["key"]: [] for c in CATS}
    intl = {c["key"]: [] for c in CATS}

    for i, (src, cat_key, title, rss_body, link) in enumerate(all_items, 1):
        # 1) 기사 원문 본문 전체 수집 → 2) 실패 시 RSS 발췌본 폴백
        full_body = fetch_full_article(link)
        if len(full_body) > len(rss_body):
            body = full_body
            src_tag = f"원문 {len(full_body)}자"
        else:
            body = rss_body
            src_tag = f"RSS폴백 {len(rss_body)}자"
        print(f"    [{i}/{total}] {title[:42]}... [{src_tag}]")

        summary = summarize(title, body)
        art_url = article_url(link, title)
        time.sleep(0.8)

        if src == "kr":
            kr[cat_key].append((title, summary, art_url))
        else:
            intl[cat_key].append((title, summary, art_url))

    messages = build_messages(kr, intl, lookback)
    print(f"  📝 메시지 {len(messages)}개 생성")

    if send_all(messages):
        print("  ✅ 텔레그램 전송 성공!")
    else:
        print("  ❌ 텔레그램 전송 실패")
        sys.exit(1)


if __name__ == "__main__":
    main()
