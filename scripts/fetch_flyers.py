#!/usr/bin/env python3
"""새 전단을 찾아 이미지를 내려받고, Claude 가 읽기 좋은 크기로 나눈다.

매장 목록은 config/stores.json 에 있다.
  martmonster : 마트몬 매장 페이지 본문(rhymix_content)의 전단 이미지 목록. 목록의 해시가 바뀌면 새 전단으로 본다.
  kakao       : 카카오톡 채널 게시물 JSON(robots.txt 허용). 이미지가 여러 장이고 날짜가 있는 게시물을 전단으로 본다.
                이마트·롯데마트 전단은 전국 공통이라, 매주 빠짐없이 전단을 올리는 지점 채널을 쓴다.

쓰는 파일
  work/manifest.json : 이번 실행에서 처리할 전단 목록(evaluate.py 가 읽는다)
  work/batch_N.json  : Claude 판독 단계 N 이 읽을 목록. 전단 하나가 한 묶음이고, 조각이 많은 전단은 여러 묶음으로 나눈다
  work/img/<key>/    : 내려받아 나눈 이미지
  GITHUB_OUTPUT      : new=true|false, batches=묶음 수
읽는 파일
  state/seen.json    : 이미 처리한 전단(마트몬은 서명, 카카오는 게시물 번호와 날짜 키)

종료 코드는 항상 0 이다. 한 매장이 실패해도 나머지 매장은 처리하고, 실패는 경고로만 남긴다.
"""
import argparse
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


def store_channels(store):
    """매장이 전단을 찾아볼 카카오톡 채널 목록(우선순위 순). 예전 설정(channel 하나)도 받는다."""
    chans = store.get("channels") or [{"id": store["channel"], "name": store.get("channel_name", "")}]
    return [{"id": c["id"], "name": c.get("name", "")} for c in chans]


def week_start(key):
    """날짜 키 '11.19|11.25' 의 시작일 '11.19'"""
    return (key or "").split("|")[0]


def select_kakao_posts(data, store, seen, now=None):
    """새로 처리할 전단 게시물을 고른다. 반환: [(게시물, 이미지 주소, 본문, 날짜 키, 같은 행사 게시물 번호들)]
    data["items"] 에는 여러 채널의 게시물이 섞여 있을 수 있다(게시물의 '_ch' 에 채널 정보)."""
    now = now or time.time()
    seen_ids = set(seen.get("posts", []))
    seen_keys = set(seen.get("keys", []))
    branch = store.get("branch_keyword", "")
    require = store.get("require_any") or []     # 예: ["전단"] → 제목이나 본문에 이 낱말이 있는 게시물만
    exclude = store.get("exclude_any") or []     # 예: ["광고상품"] → 이 낱말이 있으면 사진이 많아도 전단으로 보지 않는다
    min_images = int(store.get("min_images", 3))
    max_age = float(store.get("max_age_days", 8)) * 86400
    # 여러 채널이 같은 주 전단을 기간 표기만 달리 올리는 경우(11.12|11.18 과 11.12)를 하나로 보려고 시작일로 묶는다
    by_start = bool(store.get("same_week_by_start"))
    group_of = week_start if by_start else (lambda k: k)
    seen_groups = {group_of(k) for k in seen_keys}
    groups = {}
    for post in data.get("items", []):
        title = post.get("title") or ""
        images = _post_images(post)
        text = _post_text(post)
        key = date_key(title, text)
        if len(images) < min_images or not key:
            continue
        if require and not any(w in title + text for w in require):
            continue
        if exclude and any(w in title + text for w in exclude):
            continue
        published = (post.get("published_at") or 0) / 1000
        if now - published > max_age:
            continue
        # 'N개점' 처럼 일부 매장만 하는 행사인데 본문에 우리 매장 이름이 없으면 건너뛴다
        if re.search(r"\d+\s*개\s*점", text + title) and branch and branch not in text + title:
            continue
        groups.setdefault(group_of(key), []).append((post, images, text, key))
    chosen = []
    for gkey, posts in groups.items():
        if gkey in seen_groups or any(p.get("id") in seen_ids for p, _, _, _ in posts):
            continue
        # 사진이 많은 게시물(전단 전체에 가까운 것)을 고르고, 같으면 나중에 올린 것을 고른다
        posts.sort(key=lambda x: (len(x[1]), x[0].get("published_at") or 0), reverse=True)
        post, images, text, key = posts[0]
        chosen.append((post, images, text, key, [p.get("id") for p, _, _, _ in posts]))
    return chosen


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
    os.makedirs(WORK, exist_ok=True)

    for old in os.listdir(WORK):
        if re.fullmatch(r"batch_\d+\.json", old):
            os.remove(os.path.join(WORK, old))

    flyers = []
    slots = MAX_BATCHES
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
                picks = select_kakao_posts({"items": items}, store, {} if forced else st)
                if not picks:
                    log(f"{sid}: no new posts")
                for post, urls, text, dkey, ids in picks:
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
                    title = titled_with_period(post.get("title") or "", dkey)
                    flyers.append({
                        "key": key, "store_id": sid, "store_name": store["name"], "source": "kakao",
                        "title": title, "period": title,
                        "link": f"https://pf.kakao.com/{ch['id']}/{post.get('id')}",
                        "channel_name": ch["name"],
                        "post_ids": ids, "date_key": dkey, "images": images, "text": text[:4000],
                        "note": store.get("msg_note", "").replace("{channel}", ch["name"]),
                    })
                    log(f"{sid}: new post {post.get('id')} from {ch['name'] or ch['id']} · {title} · {len(images)} images")
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
    new = "true" if flyers else "false"
    chunks = sum(len(f["images"]) for f in flyers)
    log(f"new flyers: {len(flyers)} ({chunks} chunks, {len(batches)} reading session(s))")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"new={new}\nbatches={len(batches)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
