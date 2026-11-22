#!/usr/bin/env python3
"""새 전단을 찾아 이미지를 내려받고, Claude 가 읽기 좋은 크기로 나눈다.

매장 목록은 config/stores.json 에 있다.
  martmonster : 마트몬 매장 페이지 본문(rhymix_content)의 전단 이미지 목록. 목록의 해시가 바뀌면 새 전단으로 본다.
  kakao       : 카카오톡 채널 게시물 JSON(robots.txt 허용). 이미지가 여러 장이고 날짜가 있는 게시물을 전단으로 본다.
                우리 매장 채널(이마트 평촌점)을 먼저 보고, 빠지는 주는 전국 공통 전단을 올리는 지점 채널로 메운다.
                고르는 규칙은 kakao_candidate()·select_kakao_posts() 와 config/stores.json 의 _comment 에 있다.

쓰는 파일
  work/manifest.json : 이번 실행에서 처리할 전단 목록(evaluate.py 가 읽는다)
  work/batch_N.json  : Claude 판독 단계 N 이 읽을 목록. 전단 하나가 한 묶음이고, 조각이 많은 전단은 여러 묶음으로 나눈다
  work/img/<key>/    : 내려받아 나눈 이미지
  work/restricted.json : 로그인해야 볼 수 있어 읽지 못한 전단 글 안내(notify_telegram.py 가 보낸다)
  work/near_miss.json  : 전단 조건에 아깝게 못 미친 이번 주 글(check_missing.py 가 미발견 안내에 적는다)
  GITHUB_OUTPUT      : new=true|false, batches=묶음 수, restricted=true|false
읽는 파일
  state/seen.json    : 이미 처리한 전단(마트몬은 서명, 카카오는 게시물 번호·날짜 키·제목과 사진 서명·묶음별 읽은 쪽수)
  state/restricted.json : 로그인 글 안내를 보낸 게시물 번호

종료 코드는 항상 0 이다. 한 매장이 실패해도 나머지 매장은 처리하고, 실패는 경고로만 남긴다.
"""
import argparse
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import check_missing   # 같은 scripts 폴더. 이번 주 판단(due·has_current)을 같은 기준으로 쓴다

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "work")
UA = "Mozilla/5.0 (compatible; mart-flyer-alert/1.0; +https://github.com/tlzkrh1029/mart-flyer-alert)"
# Claude 는 한 세션에서 이미지를 많이 읽으면 먼저 읽은 이미지를 대화에서 비운다. 세 매장 66조각을 한 세션에 맡겼더니
# 처음 읽은 이마트 21조각이 비워져 '한도 초과로 판독 못 함'으로 돌아왔다(남은 45조각은 정상). 그래서 전단마다 세션을 따로 쓴다.
BATCH_CHUNKS = 30        # Claude 세션 하나가 읽는 조각 수 상한. 넘는 전단은 고르게 나눠 여러 세션에서 읽는다
MAX_BATCHES = 5          # 한 번 실행의 세션 수 상한. .github/workflows/flyers.yml 의 'Claude 전단 판독 1~5' 단계 수와 맞춘다
MAX_WIDTH = 1200         # 이보다 넓은 이미지는 줄인다
CHUNK_ASPECT = 0.8       # 조각 높이 = 너비 x 0.8. 1200x960(약 1.15MP)은 Claude 가 더 줄이지 않고 읽는 크기다
CHUNK_OVERLAP = 0.12     # 위아래 조각이 겹치는 비율(품목이 경계에서 잘리지 않게)
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?!\d)")
# 카카오 로그인 후에만 볼 수 있는 글에 사진 대신 붙는 자물쇠 그림(568x320)의 경로
LOCK_IMAGE = "blMDrt/btrnCPhUzNl/9xUXvvYJ6yC4uwEKqX5Gs1"
REREAD = {
    "upgrade": "같은 주 전단을 이미 보냈지만, 쪽수가 훨씬 많은 전단이 새로 올라와 다시 읽었습니다.",
    "extra": "같은 주 전단을 바로 앞에 보냈지만, 다른 채널에 쪽수가 훨씬 많은 전단이 있어 함께 보냅니다.",
    "edited": "이미 보낸 전단 게시물의 제목이나 사진이 바뀌어 다시 읽었습니다.",
    "fallback": "행사 낱말 목록에 없던 글이라 늦게 찾았습니다: '{title}'",
}


def log(msg):
    print(msg, flush=True)


def warn(msg):
    print(f"::warning::{msg}", flush=True)


def http_get(url, referer=None, accept=None, retries=2, timeout=30):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    if accept:
        headers["Accept"] = accept
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001 - 네트워크 오류는 종류와 상관없이 다시 시도한다
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- 마트몬

class _MartmonsterParser(HTMLParser):
    """매장 이름(div.shop-top-title)과 본문(div.rhymix_content)의 문단 글·이미지 주소를 모은다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._content_depth = 0     # rhymix_content div 안에 있는 동안 1 이상
        self.paragraphs = []
        self._para = None
        self.images = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div":
            if self._content_depth:
                self._content_depth += 1
            elif "rhymix_content" in cls.split():
                self._content_depth = 1
            if "shop-top-title" in cls.split():
                self._in_title = True
        if not self._content_depth:
            return
        if tag == "p":
            self._para = []
        elif tag == "img":
            src = a.get("src") or ""
            if "/files/attach/images/" in src and src not in self.images:
                self.images.append(src)

    def handle_endtag(self, tag):
        if tag == "div":
            if self._in_title:
                self._in_title = False
            if self._content_depth:
                self._content_depth -= 1
        if tag == "p" and self._para is not None:
            text = " ".join("".join(self._para).split())
            if text:
                self.paragraphs.append(text)
            self._para = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        if self._content_depth and self._para is not None:
            self._para.append(data)


def parse_martmonster(html_text, page_url):
    p = _MartmonsterParser()
    p.feed(html_text)
    period = next((t for t in p.paragraphs if "행사" in t and "기간" in t), "")
    period = period.lstrip("■ ").strip()
    images = [urllib.parse.urljoin(page_url, s) for s in p.images]
    return {"title": p.title, "period": period, "images": images}


def flyer_signature(urls):
    paths = sorted(urllib.parse.urlparse(u).path for u in urls)
    return hashlib.sha1("\n".join(paths).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 카카오톡 채널

def _post_text(post):
    return "".join(c.get("v", "") for c in (post.get("contents") or []) if isinstance(c, dict))


def _post_images(post):
    out = []
    for m in post.get("media") or []:
        if m.get("type") == "image":
            url = m.get("xlarge_url") or m.get("large_url") or m.get("url")
            if url:
                out.append(url)
    return out


def date_key(title, text=""):
    """제목(없으면 본문)의 첫 두 날짜(월.일 또는 월/일)로 행사 키를 만든다.
    같은 행사를 이미지 게시물과 멀티미디어 게시물로 두 번 올린 경우를 하나로 묶는다."""
    ds = [(m, d) for m, d in DATE_RE.findall(title or "") if 1 <= int(m) <= 12 and 1 <= int(d) <= 31]
    if not ds:
        ds = [(m, d) for m, d in DATE_RE.findall(text or "") if 1 <= int(m) <= 12 and 1 <= int(d) <= 31]
    return "|".join(f"{int(m)}.{int(d)}" for m, d in ds[:2])


def effective_title(post):
    """게시물 제목. 제목 칸이 빈 글(울산점·롯데마트 일부)은 채널 화면처럼 본문 첫 줄을 제목으로 본다."""
    title = (post.get("title") or "").strip()
    if title:
        return title
    return next((line.strip() for line in _post_text(post).splitlines() if line.strip()), "")


def post_label(post, n=50):
    """사람이 알아볼 글 이름. 제목 칸이 빈 글은 첫 줄이 '어서오세요!' 같은 인사인 경우가 있어 앞쪽 몇 줄을 이어 붙인다."""
    raw = (post.get("title") or "").strip()
    if raw:
        return raw
    joined = " ".join(line.strip() for line in _post_text(post).splitlines() if line.strip())
    return joined[:n] + ("…" if len(joined) > n else "")


def _image_media(post):
    return [m for m in post.get("media") or [] if m.get("type") == "image"]


def page_count(post):
    """전단 쪽수. 두 쪽을 펼친 가로 이미지(split_image 가 좌우로 나누는 것)는 두 쪽으로 센다."""
    n = 0
    for m in _image_media(post):
        w, h = m.get("width") or 0, m.get("height") or 0
        n += 2 if h and w > h * 1.2 else 1
    return n


def is_restricted(post):
    """카카오 로그인 후에만 볼 수 있는 글. 2025년 가을~2026년 봄 킴스클럽 전단 글이 이랬다(adult_only=true).
    사진 대신 자물쇠 그림 한 장(LOCK_IMAGE)만 와서 시스템은 전단을 읽을 수 없다."""
    if post.get("adult_only"):
        return True
    return any(LOCK_IMAGE in (m.get("xlarge_url") or m.get("url") or m.get("path") or "") for m in _image_media(post))


def post_sig(post):
    """제목과 사진 목록의 서명. 처리한 게시물을 고쳐 올렸는지(제목이나 사진이 바뀌었는지) 알아보는 데 쓴다."""
    paths = [urllib.parse.urlparse(u).path for u in _post_images(post)]
    raw = (post.get("title") or "").strip() + "\n" + "\n".join(paths)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def store_channels(store):
    """매장이 전단을 찾아볼 카카오톡 채널 목록. 예전 설정(channel 하나)도 받는다.
    채널마다 own(우리 매장 채널), regional(지역판 전단을 올리는 채널), min_images, msg_note 를 둘 수 있다."""
    chans = store.get("channels") or [{"id": store["channel"], "name": store.get("channel_name", "")}]
    return [dict(c, name=c.get("name", "")) for c in chans]


def week_start(key):
    """날짜 키 '11.19|11.25' 의 시작일 '11.19'"""
    return (key or "").split("|")[0]


def kakao_candidate(post, store, now, near=False):
    """게시물이 전단 후보인지 본다. 후보가 아니면 None.
    near=True 이면 전단 조건(낱말·사진 수)에 아깝게 못 미친 글도 near=True 로 돌려준다(fallback_posts 가 쓴다).
    제외 낱말·다른 지점 표시(단독)는 제목만 본다(제목 칸이 비면 본문 첫 줄). 본문에는 전단에도 '광고상품', '롯데마트 단독'
    같은 말이 섞여 있기 때문이다. restricted=True 인 후보는 사진을 볼 수 없어 읽지 못하고 안내만 보낸다."""
    ch = post.get("_ch") or {}
    raw_title = post.get("title") or ""
    title = effective_title(post)
    text = _post_text(post)
    key = date_key(raw_title, text)
    if not key:
        return None
    if now - (post.get("published_at") or 0) / 1000 > float(store.get("max_age_days", 8)) * 86400:
        return None
    if any(w in title for w in store.get("exclude_title") or []):
        return None
    if any(re.search(p, title) for p in store.get("exclude_title_re") or []):
        return None
    # 우리 매장 채널의 글이거나 제목에 우리 매장 이름(평촌 등)이 있으면 우리 매장 글이다
    own = bool(ch.get("own")) or any(n in title for n in store.get("own_names") or [])
    store_only = any(w in title for w in store.get("other_store_words") or [])
    if store_only and not own:          # 다른 지점의 단독 전단·단독 행사
        return None
    full = raw_title + "\n" + text
    # 'N개점' 처럼 일부 매장만 하는 행사인데 본문에 우리 매장 이름이 없으면 건너뛴다
    branch = store.get("branch_keyword", "")
    if re.search(r"\d+\s*개\s*점", full) and branch and branch not in full:
        return None
    restricted = is_restricted(post)
    images = [] if restricted else _post_images(post)
    require = store.get("require_any") or []
    need = int(ch.get("min_images", store.get("min_images", 3)))
    regular = (not require or any(w in full for w in require)) and (restricted or len(images) >= need)
    event = any(w in full for w in store.get("event_any") or [])
    near_miss = False
    if not regular:
        # 정기 전단 조건(낱말·사진 수)에 못 미쳐도 고래잇·쓱데이 같은 행사 전단은 사진 event_min_images 장부터 인정한다.
        # 이 길로 들어오는 글은 사은품 홍보 글이 많아서, 제목에 증정·스탬프 같은 낱말이 있으면 뺀다
        if any(w in title for w in store.get("event_exclude_title") or []):
            return None
        if not (event and (restricted or len(images) >= int(store.get("event_min_images", need)))):
            # 정기 전단 조건도 행사 전단 조건도 못 채운 글. 기간(시작일과 끝날)이 적혀 있고 사진이 몇 장 있으면
            # '아깝게 걸린 글'이다. 행사 이름이 바뀌어 목록에 없는 전단일 수 있어 따로 모아 둔다
            fb = store.get("fallback") or {}
            if not (near and fb and not restricted and "|" in key and len(images) >= int(fb.get("min_images", 2))):
                return None
            near_miss = True
    return {"post": post, "title": title, "text": text, "key": key, "images": images,
            "pages": 0 if restricted else page_count(post), "own": own, "store_only": store_only,
            "regional": bool(ch.get("regional")), "event": event, "restricted": restricted, "near": near_miss}


def _rank(c):
    # 우리 매장 채널 글 → 쪽수가 많은 글 → 전국판(지역판보다) → 나중에 올린 글
    return (c["own"], c["pages"], not c["regional"], c["post"].get("published_at") or 0)


def _bigger(pages, than):
    """같은 주 전단을 이미 읽었어도, 쪽수가 두 배 이상이고 세 쪽 이상 많으면 더 완전한 전단으로 보고 다시 읽는다.
    (사진 몇 장짜리 홍보 글이나 일부 쪽만 올린 글을 먼저 읽고, 진짜 전단이 나중에 올라오는 경우)"""
    return bool(than) and pages >= than * 2 and pages >= than + 3


def _kakao_groups(data, store, now):
    by_start = bool(store.get("same_week_by_start"))
    group_of = week_start if by_start else (lambda k: k)
    groups = {}
    for post in data.get("items", []):
        c = kakao_candidate(post, store, now)
        if not c:
            continue
        # 여러 채널이 같은 주 전단을 기간 표기만 달리 올리는 경우(11.12|11.18 과 11.12)를 하나로 보려고 시작일로 묶는다.
        # 우리 매장 단독 전단은 전국 공통 전단과 내용이 다르므로 따로 묶는다
        c["group"] = ("단독:" if c["store_only"] else "") + group_of(c["key"])
        groups.setdefault(c["group"], []).append(c)
    return groups, group_of


def _as_pick(c, cands, reason):
    ids = [c["post"].get("id")] + [x["post"].get("id") for x in cands if x is not c]
    info = {k: c[k] for k in ("group", "pages", "own", "regional", "title")}
    info.update(sig=post_sig(c["post"]), reason=reason)
    return (c["post"], c["images"], c["text"], c["key"], ids, info)


def select_kakao_posts(data, store, seen, now=None):
    """새로 처리할 전단 게시물을 고른다.
    반환: [(게시물, 이미지 주소, 본문, 날짜 키, 같은 행사 게시물 번호들(고른 것이 먼저), 정보)]
    정보: group(묶음), pages(쪽수), sig(제목·사진 서명), own, regional, title(제목),
          reason: new(새 전단) · extra(같은 실행에서 함께 읽는 훨씬 큰 전단) · upgrade(이미 읽은 주의 훨씬 큰 전단) · edited(고쳐 올림)
                  · fallback(그 주 전단을 못 찾아 읽어 보는 '아깝게 걸린 글', fallback_posts)
    data["items"] 에는 여러 채널의 게시물이 섞여 있을 수 있다(게시물의 '_ch' 에 채널 정보)."""
    now = now or time.time()
    seen_ids = set(seen.get("posts", []))
    skip_ids = set(seen.get("skip", []))  # 읽어 보니 전단이 아니었던 글(사은품 안내 등). 그 주 전단은 그대로 기다린다
    sigs = seen.get("sigs", {})          # 처리한 게시물 번호 → 처리할 때의 제목·사진 서명
    read = seen.get("read", {})          # 처리한 묶음 → 읽은 쪽수
    upgrade = store.get("upgrade", True)
    groups, group_of = _kakao_groups(data, store, now)
    seen_groups = {group_of(k) for k in seen.get("keys", [])}
    chosen = []

    def pick(c, cands, reason):
        chosen.append(_as_pick(c, cands, reason))

    for gkey, cands in groups.items():
        cands = [c for c in cands if c["post"].get("id") not in skip_ids]
        live = [c for c in cands if not c["restricted"]]
        # 처리한 게시물의 제목이나 사진이 바뀌었으면(고쳐 올림) 다시 읽는다. 서명이 없는 예전 게시물은 비교하지 않는다
        edited = [c for c in live if c["post"].get("id") in seen_ids
                  and str(c["post"].get("id")) in sigs and sigs[str(c["post"].get("id"))] != post_sig(c["post"])]
        if edited:
            for c in edited:
                pick(c, cands, "edited")
            continue
        fresh = sorted((c for c in live if c["post"].get("id") not in seen_ids), key=_rank, reverse=True)
        if not fresh:
            continue
        if gkey not in seen_groups and not any(c["post"].get("id") in seen_ids for c in cands):
            first = fresh[0]
            pick(first, cands, "new")
            # 우리 매장 채널의 글을 먼저 읽되, 다른 채널에 훨씬 완전한 전단이 있으면 그것도 읽는다
            rest = max(fresh[1:], key=lambda c: (c["pages"], c["post"].get("published_at") or 0), default=None)
            if upgrade and rest and _bigger(rest["pages"], first["pages"]):
                pick(rest, cands, "extra")
        elif upgrade:
            best = max(fresh, key=lambda c: (c["pages"], c["post"].get("published_at") or 0))
            if _bigger(best["pages"], read.get(gkey, 0)):
                pick(best, cands, "upgrade")
    # 우리 매장 채널의 글을 먼저 처리한다(판독 세션이 모자라면 뒤의 것이 다음 실행으로 밀린다)
    chosen.sort(key=lambda x: not x[5]["own"])
    return chosen


def restricted_posts(data, store, seen, notified, chosen_groups=(), now=None):
    """로그인해야 볼 수 있어 읽지 못하는 전단 글. 이미 알린 글, 같은 주 전단을 이미 처리했거나 이번에 처리할 글은 뺀다."""
    now = now or time.time()
    groups, group_of = _kakao_groups(data, store, now)
    seen_groups = {group_of(k) for k in seen.get("keys", [])} | set(chosen_groups)
    seen_ids = set(seen.get("posts", [])) | set(notified)
    out = []
    for gkey, cands in groups.items():
        if gkey in seen_groups:
            continue
        out += [c for c in cands if c["restricted"] and c["post"].get("id") not in seen_ids]
    return out


def near_misses(data, store, seen, today, now=None):
    """오늘이 기간에 들어가는 '아깝게 걸린 글'(쪽수가 많은 순). 처리한 글은 빼고, 읽어 보니 전단이 아니었던 글은 남긴다
    (전단 미발견 안내에 '건너뛴 글'로 적으려는 것)."""
    now = now or time.time()
    seen_ids = set(seen.get("posts", []))
    out = []
    for post in data.get("items", []):
        c = kakao_candidate(post, store, now, near=True)
        if c and c["near"] and c["post"].get("id") not in seen_ids and check_missing.has_current([c["key"]], today):
            out.append(c)
    return sorted(out, key=lambda c: (c["pages"], c["post"].get("published_at") or 0), reverse=True)


def fallback_posts(data, store, seen, covered_keys, now_dt, now=None):
    """정한 요일·시각(목요일 17시)이 지나도 그 주 전단을 못 찾았으면, 아깝게 걸린 글을 Claude 가 읽어 보게 고른다.
    행사 이름이 바뀌어 event_any 목록에 없는 전단을 되찾으려는 것이다. 전단이 아니면 evaluate.py 가 skip 에 남기고,
    다음 실행이 다음 글을 읽는다. 한 번에 fallback.max_reads 개까지."""
    cfg = store.get("fallback")
    if not cfg or not check_missing.due(cfg, now_dt):
        return []
    today = now_dt.date()
    if check_missing.has_current(list(seen.get("keys", [])) + list(covered_keys), today):
        return []
    skip = set(seen.get("skip", []))
    cands = [c for c in near_misses(data, store, seen, today, now) if c["post"].get("id") not in skip]
    for c in cands:
        c["group"] = check_missing.week_key(today) + ":" + c["key"]   # 그 주 전단 묶음과 섞이지 않게
    return [_as_pick(c, [], "fallback") for c in cands[:int(cfg.get("max_reads", 2))]]


def restricted_message(store, title, link):
    return "\n".join([
        f"🔒 {store['name']}: 로그인해야 볼 수 있는 전단 글이 올라왔습니다",
        "",
        title,
        "",
        "카카오 로그인 후에만 사진이 보이는 글이라 시스템이 전단을 읽지 못했습니다.",
        "이 전단은 알림이 가지 않으니 직접 확인해 주세요.",
        link,
    ])


def titled_with_period(title, key):
    """제목에 날짜가 없으면(예: '🛒이번 주 이마트 행사 소식') 본문에서 읽은 행사 기간을 붙인다.
    메시지·카드가 제목에서 기간을 읽기 때문이다."""
    if not key or DATE_RE.search(title or ""):
        return title
    parts = [p.replace(".", "/") for p in key.split("|")]
    # 제목 칸이 빈 게시물도 있다(2026-11-20 울산점 전단). 그때는 '이번 주 전단'으로 부른다
    return f"{(title or '').strip() or '이번 주 전단'} ({' ~ '.join(parts)})"


# ---------------------------------------------------------------- 이미지

def split_image(raw, out_dir, stem):
    """이미지를 너비 x CHUNK_ASPECT 높이의 조각으로 나눠 저장한다. 반환: 저장한 경로 목록"""
    from PIL import Image  # 워크플로에서만 필요하므로 여기서 불러온다

    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    w, h = img.size
    if w > h * 1.2:   # 두 쪽을 펼친 전단은 좌우로 나눠 각각 처리한다
        left = split_pil(img.crop((0, 0, w // 2, h)), out_dir, stem + "L")
        right = split_pil(img.crop((w // 2, 0, w, h)), out_dir, stem + "R")
        return left + right
    return split_pil(img, out_dir, stem)


def split_pil(img, out_dir, stem):
    w, h = img.size
    if w > MAX_WIDTH:
        img = img.resize((MAX_WIDTH, round(h * MAX_WIDTH / w)))
        w, h = img.size
    chunk_h = max(1, int(w * CHUNK_ASPECT))
    paths = []
    if h <= max(chunk_h * 1.15, w * 1.05):     # 정사각형에 가까운 이미지는 자르지 않는다
        boxes = [(0, 0, w, h)]
    else:
        overlap = int(chunk_h * CHUNK_OVERLAP)
        step = chunk_h - overlap
        boxes = []
        top = 0
        while True:
            bottom = min(top + chunk_h, h)
            boxes.append((0, top, w, bottom))
            if bottom >= h:
                break
            top += step
            if h - top < chunk_h * 0.4:      # 마지막 조각이 너무 얇으면 앞 조각에 붙인다
                boxes[-1] = (0, boxes[-1][1], w, h)
                break
    for i, box in enumerate(boxes):
        p = os.path.join(out_dir, f"{stem}_{chr(97 + i)}.jpg")
        img.crop(box).save(p, "JPEG", quality=90)
        paths.append(p)
    return paths


def download_and_split(urls, key, referer=None):
    out_dir = os.path.join(WORK, "img", key)
    os.makedirs(out_dir, exist_ok=True)
    chunks = []
    for i, url in enumerate(urls, 1):
        try:
            raw = http_get(url, referer=referer)
            chunks += split_image(raw, out_dir, f"{i:02d}")
        except Exception as e:  # noqa: BLE001
            warn(f"{key}: image {i} ({url}) failed: {e}")
    return chunks   # 절대 경로(Claude 의 Read 도구는 절대 경로를 받는다)


# ---------------------------------------------------------------- 판독 묶음

def parts_needed(n_chunks):
    return max(1, math.ceil(n_chunks / BATCH_CHUNKS))


def plan_batches(flyers):
    """전단마다 Claude 세션(묶음)을 따로 쓴다. 조각이 BATCH_CHUNKS 보다 많은 전단은 고르게 나눈다.
    게시물 본문(text)은 첫 묶음에만 넣어 같은 품목을 두 번 적지 않게 한다."""
    batches = []
    for f in flyers:
        imgs = f["images"]
        k = parts_needed(len(imgs))
        size = math.ceil(len(imgs) / k)
        for i in range(k):
            part = {"key": f["key"], "store_name": f["store_name"], "period": f["period"],
                    "images": imgs[i * size:(i + 1) * size], "text": f.get("text", "") if i == 0 else ""}
            if k > 1:
                part["part"] = f"{i + 1}/{k}"
            batches.append({"flyers": [part]})
    return batches


def take_slots(flyers, images, slots, sid, post_id=None):
    """이 전단에 필요한 세션 수를 남은 칸에서 뺀다. 칸이 모자라면 None(다음 실행으로 미룸).
    첫 전단은 미루지 않는다. 너무 크면 앞쪽 조각만 남긴다."""
    need = parts_needed(len(images))
    what = f"post {post_id}" if post_id else "flyer"
    if need <= slots:
        return images, slots - need
    if flyers:
        warn(f"{sid}: {len(images)} chunks need {need} reading sessions but only {slots} left; {what} deferred")
        return None, slots
    keep = slots * BATCH_CHUNKS
    warn(f"{sid}: {len(images)} chunks are too many for one run; reading only the first {keep}")
    return images[:keep], 0


# ---------------------------------------------------------------- 실행

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", default="", help="이미 처리한 전단도 다시 처리할 매장 id(쉼표 구분) 또는 all")
    args = ap.parse_args()
    force = {s.strip() for s in args.force.split(",") if s.strip()}

    stores = load_json(os.path.join(ROOT, "config", "stores.json"), {}).get("stores", [])
    seen = load_json(os.path.join(ROOT, "state", "seen.json"), {})
    notified = load_json(os.path.join(ROOT, "state", "restricted.json"), {})   # 로그인 글 안내를 보낸 게시물 번호
    os.makedirs(WORK, exist_ok=True)

    for old in os.listdir(WORK):
        if re.fullmatch(r"batch_\d+\.json", old) or old in ("restricted.json", "near_miss.json"):
            os.remove(os.path.join(WORK, old))

    flyers, restricted, near_list = [], [], {}
    slots = MAX_BATCHES
    now_dt = dt.datetime.now()   # Actions 는 TZ=Asia/Seoul
    for store in stores:
        sid = store["id"]
        if store.get("enabled") is False:
            continue
        forced = "all" in force or sid in force
        st = seen.get(sid, {})
        try:
            if store["source"] == "martmonster":
                html_text = http_get(store["url"]).decode("utf-8", "replace")
                info = parse_martmonster(html_text, store["url"])
                if not info["images"]:
                    warn(f"{sid}: no flyer images on the page")
                    continue
                sig = flyer_signature(info["images"])
                if sig == st.get("sig") and not forced:
                    log(f"{sid}: unchanged ({sig}, {info['period']})")
                    continue
                if slots <= 0:
                    warn(f"{sid}: no reading session left; deferred to the next run")
                    continue
                key = f"{sid}-{sig}"
                images = download_and_split(info["images"], key, referer=store["url"])
                if not images:
                    continue
                images, slots = take_slots(flyers, images, slots, sid)
                if images is None:
                    continue
                flyers.append({
                    "key": key, "store_id": sid, "store_name": store["name"], "source": "martmonster",
                    "title": info["title"] or store["name"], "period": info["period"], "link": store["url"],
                    "sig": sig, "images": images, "text": "", "note": store.get("msg_note", ""),
                })
                log(f"{sid}: new flyer {sig} · {info['period']} · {len(info['images'])} images → {len(images)} chunks")
            elif store["source"] == "kakao":
                items = []
                for ci, ch in enumerate(store_channels(store)):
                    if ci:
                        time.sleep(1)
                    url = f"https://pf.kakao.com/rocket-web/web/v2/profiles/{ch['id']}/posts?includePinnedPost=true"
                    try:
                        data = json.loads(http_get(url, accept="application/json").decode("utf-8"))
                    except Exception as e:  # noqa: BLE001  채널 하나가 실패해도 다른 채널은 본다
                        warn(f"{sid}: channel {ch['id']} ({ch['name']}): {e}")
                        continue
                    for post in data.get("items", []):
                        post["_ch"] = ch
                        items.append(post)
                st_now = {} if forced else st
                picks = select_kakao_posts({"items": items}, store, st_now)
                if not picks:
                    log(f"{sid}: no new posts")
                if store.get("missing_alert") or store.get("fallback"):
                    for c in near_misses({"items": items}, store, st_now, now_dt.date())[:5]:
                        ch = c["post"].get("_ch") or store_channels(store)[0]
                        near_list.setdefault(sid, []).append(
                            {"title": post_label(c["post"]), "link": f"https://pf.kakao.com/{ch['id']}/{c['post'].get('id')}"})
                    covered = [p[3] for p in picks if not p[5]["group"].startswith("단독:")]
                    extra = fallback_posts({"items": items}, store, st_now, covered, now_dt)
                    for p in extra:
                        log(f"{sid}: this week's flyer not found yet; reading near-miss post {p[0].get('id')} ({p[5]['title']})")
                    picks = picks + extra
                if store.get("restricted_alert"):
                    for c in restricted_posts({"items": items}, store, st_now, notified.get(sid, []),
                                              chosen_groups=[p[5]["group"] for p in picks]):
                        ch = c["post"].get("_ch") or store_channels(store)[0]
                        restricted.append({"store_id": sid, "post_id": c["post"].get("id"), "message": restricted_message(
                            store, c["title"], f"https://pf.kakao.com/{ch['id']}/{c['post'].get('id')}")})
                        log(f"{sid}: post {c['post'].get('id')} needs a Kakao login; notice prepared")
                for post, urls, text, dkey, ids, info in picks:
                    if slots <= 0:
                        warn(f"{sid}: no reading session left; post {post.get('id')} deferred")
                        break
                    key = f"{sid}-{post.get('id')}"
                    images = download_and_split(urls[:25], key)
                    if not images:
                        continue
                    images, slots = take_slots(flyers, images, slots, sid, post.get("id"))
                    if images is None:
                        break
                    ch = post.get("_ch") or store_channels(store)[0]
                    title = titled_with_period(info["title"], dkey)
                    note = ch.get("msg_note") or store.get("msg_note", "")
                    flyers.append({
                        "key": key, "store_id": sid, "store_name": store["name"], "source": "kakao",
                        "title": title, "period": title,
                        "link": f"https://pf.kakao.com/{ch['id']}/{post.get('id')}",
                        "channel_name": ch["name"],
                        "post_id": post.get("id"), "post_ids": ids,
                        # 우리 매장 단독 전단은 그 주 전단으로 치지 않는다(날짜 키를 남기지 않음)
                        "date_key": "" if info["group"].startswith("단독:") else dkey,
                        "group": info["group"], "pages": info["pages"], "sig": info["sig"],
                        "reread": REREAD.get(info["reason"], "").format(title=post_label(post)),
                        "images": images, "text": text[:4000],
                        "note": note.replace("{channel}", ch["name"]),
                    })
                    log(f"{sid}: {info['reason']} post {post.get('id')} from {ch['name'] or ch['id']} · {title} · "
                        f"{info['pages']} pages → {len(images)} chunks")
            else:
                warn(f"{sid}: unknown source {store['source']}")
        except Exception as e:  # noqa: BLE001
            warn(f"{sid}: {e}")

    manifest = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "flyers": flyers}
    with open(os.path.join(WORK, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    batches = plan_batches(flyers)
    for n, batch in enumerate(batches, 1):
        with open(os.path.join(WORK, f"batch_{n}.json"), "w", encoding="utf-8") as f:
            json.dump(batch, f, ensure_ascii=False, indent=1)
    if restricted:
        with open(os.path.join(WORK, "restricted.json"), "w", encoding="utf-8") as f:
            json.dump(restricted, f, ensure_ascii=False, indent=1)
    if near_list:   # check_missing.py 가 '전단을 찾지 못했습니다' 안내에 건너뛴 글로 적는다
        with open(os.path.join(WORK, "near_miss.json"), "w", encoding="utf-8") as f:
            json.dump(near_list, f, ensure_ascii=False, indent=1)
    new = "true" if flyers else "false"
    chunks = sum(len(f["images"]) for f in flyers)
    log(f"new flyers: {len(flyers)} ({chunks} chunks, {len(batches)} reading session(s)), "
        f"login-only notices: {len(restricted)}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"new={new}\nbatches={len(batches)}\nrestricted={'true' if restricted else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
