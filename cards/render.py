#!/usr/bin/env python3
"""전단 알림 카드(1080x1350 PNG)를 만든다. 헤드리스 크롬(Playwright, 시스템 Chrome)으로 HTML 템플릿을 찍는다.

  python cards/render.py              work/cards.json(evaluate.py 가 씀) → work/cards/<key>/NN.png, work/albums.json
  python cards/render.py --compare    목요일 매장 비교 카드 → work/cards/compare/NN.png, work/compare.json
                                      (--force 면 요일과 이번 주 발송 여부를 보지 않는다)

카드를 만들지 못해도 텍스트 메시지는 그대로 간다. 이 스크립트가 실패하면 albums.json 이 비어 notify 가 텍스트만 보낸다.
"""
import argparse
import datetime as dt
import glob
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import cardlib as C  # noqa: E402

WORK = os.path.join(ROOT, "work")
STATE = os.path.join(ROOT, "state")
OUT = os.path.join(WORK, "cards")
FONT = "file://" + os.path.join(HERE, "fonts", "PretendardVariable.woff2")
W, H = 1080, 1350
MAX_ALBUM = 10          # 텔레그램 앨범 한 묶음의 최대 사진 수


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def env():
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from markupsafe import Markup
    e = Environment(loader=FileSystemLoader(os.path.join(HERE, "templates")),
                    autoescape=select_autoescape(["html", "j2"]), trim_blocks=True, lstrip_blocks=True)
    e.globals["CSS"] = Markup(e.get_template("base.css").render(font_url=FONT))
    return e


class Renderer:
    def __init__(self, page, jenv):
        self.page, self.env, self.n = page, jenv, 0
        self.tmp = os.path.join(OUT, "_tmp")
        os.makedirs(self.tmp, exist_ok=True)

    def _load(self, tpl, fixed, ctx):
        self.n += 1
        path = os.path.join(self.tmp, f"p{self.n % 4}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.env.get_template(tpl).render(css=self.env.globals["CSS"], fixed=fixed, **ctx))
        self.page.goto("file://" + path)
        self.page.evaluate("document.fonts.ready.then(() => true)")

    def height(self, tpl, **ctx):
        self._load(tpl, False, ctx)
        return self.page.evaluate("document.querySelector('.page').scrollHeight")

    def shoot(self, tpl, png, **ctx):
        self._load(tpl, True, ctx)
        os.makedirs(os.path.dirname(png), exist_ok=True)
        self.page.screenshot(path=png, clip={"x": 0, "y": 0, "width": W, "height": H})


def groups_of(blocks):
    out = []
    for b in blocks:
        c = b.get("c")
        if out and out[-1]["container"] == c:
            out[-1]["blocks"].append(b)
        else:
            out.append({"container": c, "blocks": [b]})
    return out


def paginate(r, tpl, blocks, ctx_fn):
    """블록을 앞에서부터 채우다가 1350을 넘으면 다음 장으로 넘긴다. 머리글(keep)은 다음 블록과 붙여 둔다"""
    pages, cur, i = [], [], 0
    while i < len(blocks):
        trial = cur + [blocks[i]]
        if r.height(tpl, groups=groups_of(trial), **ctx_fn(not pages, False)) <= H:
            cur, i = trial, i + 1
            continue
        if not cur:            # 블록 하나가 한 장보다 크면 그 블록만 따로 둔다(잘려도 다음으로 넘어간다)
            pages.append([blocks[i]])
            i += 1
            continue
        carry = []
        while cur and cur[-1].get("keep"):
            carry.insert(0, cur.pop())
        pages.append(cur)
        cur = carry
    if cur:
        pages.append(cur)
    return pages


def footer_page(r, tpl, pages, ctx_fn):
    """출처 문구는 뒤에서부터 자리가 남는 장에 넣는다. 자리가 없으면 뺀다(텍스트 메시지에 있다)"""
    for i in range(len(pages) - 1, -1, -1):
        if r.height(tpl, groups=groups_of(pages[i]), **ctx_fn(i == 0, True)) <= H:
            return i + 1
    return 0


def store_blocks(ctx):
    blocks = [{"k": "hero", "d": h, "c": "rail"} for h in ctx["heroes"]]
    if ctx["rows"]:
        blocks.append({"k": "sec", "title": "기준을 통과한 나머지 품목", "keep": True})
        blocks += [{"k": "row", "d": x} for x in ctx["rows"]]
    b = ctx["bogos"]
    if b:
        blocks.append({"k": "bogo_head", "keep": True, "d": b})
        for title, rows in b["sections"]:
            blocks.append({"k": "sec", "title": title, "count": f"{len(rows)}개", "keep": True})
            blocks += [{"k": "brow", "d": x} for x in rows]
    return blocks


def render_store(r, entry, criteria, stores_cfg, today):
    flyer = entry["flyer"]
    ctx = C.store_context(flyer, entry["items"], criteria, stores_cfg.get(flyer.get("store_id")), today=today)
    blocks = store_blocks(ctx)
    if not blocks:
        return None
    crops = C.crop_pages(ctx["all"])
    base = {k: ctx[k] for k in ("store", "period", "lede", "footer")}

    def ctx_fn(first, last, no=1, total=9):
        return dict(base, first=first, last=last, no=no, total=total)

    pages = paginate(r, "page.html.j2", blocks, ctx_fn)[:MAX_ALBUM]
    crops = crops[:max(0, MAX_ALBUM - len(pages))]
    fp = footer_page(r, "page.html.j2", pages, ctx_fn)
    total = len(pages) + len(crops)
    out_dir = os.path.join(OUT, flyer["key"])
    shutil.rmtree(out_dir, ignore_errors=True)
    files = []
    for i, pb in enumerate(pages, 1):
        png = os.path.join(out_dir, f"{i:02d}.png")
        r.shoot("page.html.j2", png, groups=groups_of(pb), **ctx_fn(i == 1, i == fp, i, total))
        files.append(png)
    for j, cp in enumerate(crops, len(pages) + 1):
        png = os.path.join(out_dir, f"{j:02d}.png")
        r.shoot("crop.html.j2", png, store=ctx["store"], period=ctx["period"], no=j, total=total,
                where=cp["where"], image="file://" + cp["image"], items=cp["items"])
        files.append(png)
    return {"files": files, "caption": C.caption(ctx, len(crops), total),
            "pages": len(pages), "crops": len(crops)}


def browser_page(p):
    try:
        b = p.chromium.launch(channel="chrome")
    except Exception:  # noqa: BLE001  시스템 Chrome 이 없으면 Playwright 가 받아 둔 Chromium 을 쓴다
        b = p.chromium.launch()
    return b, b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)


def summary(lines):
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step and lines:
        with open(step, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main_albums():
    from playwright.sync_api import sync_playwright
    entries = load_json(os.path.join(WORK, "cards.json"), [])
    albums = {}
    if not entries:
        print("no flyers to draw")
        save_json(os.path.join(WORK, "albums.json"), albums)
        return 0
    criteria = load_json(os.path.join(ROOT, "config", "criteria.json"), {"rules": []})
    stores_cfg = {s["id"]: s for s in load_json(os.path.join(ROOT, "config", "stores.json"), {"stores": []})["stores"]}
    today = dt.date.today()
    lines = ["### 카드 이미지", ""]
    with sync_playwright() as p:
        b, page = browser_page(p)
        r = Renderer(page, env())
        for e in entries:
            key = e["flyer"].get("key")
            try:
                res = render_store(r, e, criteria, stores_cfg, today)
            except Exception as ex:  # noqa: BLE001  한 매장이 실패해도 나머지와 텍스트는 보낸다
                print(f"::warning::{key}: card render failed: {ex}")
                continue
            if res:
                albums[str(e["msg"])] = res
                print(f"{key}: {len(res['files'])} image(s), caption {len(res['caption'])} chars")
                lines.append(f"- {e['flyer'].get('store_name')}: 사진 {len(res['files'])}장"
                             f"(요약 {res['pages']}장, 전단 원본 {res['crops']}장), 설명 글 {len(res['caption'])}자")
        b.close()
    save_json(os.path.join(WORK, "albums.json"), albums)
    summary(lines)
    return 0


# ---------------------------------------------------------------- 매장 비교

def week_key(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def latest_history(store_id, today, days=8):
    best = None
    for f in glob.glob(os.path.join(STATE, "history", "*", f"*_{store_id}-*.json")):
        name = os.path.basename(f)
        try:
            d = dt.datetime.strptime(name[:8], "%Y%m%d").date()
        except ValueError:
            continue
        if 0 <= (today - d).days <= days and (best is None or name > os.path.basename(best)):
            best = f
    return load_json(best, None) if best else None


def main_compare(force):
    from playwright.sync_api import sync_playwright
    cfg = load_json(os.path.join(ROOT, "config", "compare.json"), {})
    state = load_json(os.path.join(STATE, "compare.json"), {})
    now = dt.datetime.now()
    today = now.date()
    wk = week_key(today)
    sched = cfg.get("schedule", {"weekday": 3, "after_hour": 17})
    wd = today.weekday()
    # 목요일 저녁부터 보내고, 그때 보내지 못했으면 그 주 일요일까지 다음 실행이 다시 보낸다
    due = state.get("week") != wk and (wd > sched["weekday"] or (wd == sched["weekday"] and now.hour >= sched["after_hour"]))
    if not (due or force):
        print(f"compare: not due (weekday {today.weekday()}, hour {now.hour}, last {state.get('week')})")
        return 0
    stores = []
    for s in cfg.get("stores", []):
        h = latest_history(s["id"], today)
        if h:
            items = [{k: v for k, v in i.items() if k != "_verdict"} for i in h.get("items", [])]
            stores.append((s["id"], s["name"], h.get("flyer", {}), items))
    if len(stores) < 2:
        print(f"compare: only {len(stores)} store(s) with this week's flyer; skipped")
        return 0
    cc = C.compare_context(stores, cfg, today, cfg.get("max_rows", 8))
    if not cc["rows"]:
        print("compare: no item appears in two or more flyers; skipped")
        return 0
    base = {k: cc[k] for k in ("title", "period", "headline", "cols", "top", "footer")}

    def ctx(first, last, no=1, total=1):
        return dict(base, first=first, last=last, no=no, total=total)

    rows = cc["rows"]
    files = []
    shutil.rmtree(os.path.join(OUT, "compare"), ignore_errors=True)
    with sync_playwright() as p:
        b, page = browser_page(p)
        r = Renderer(page, env())
        pages = [rows]
        for n in range(1, len(rows) + 1):      # 줄을 n장에 고르게 나눠 모든 장이 들어가는 가장 작은 n
            size = -(-len(rows) // n)
            pages = [rows[k:k + size] for k in range(0, len(rows), size)]
            if all(r.height("compare.html.j2", rows=pr, **ctx(i == 0, i == len(pages) - 1)) <= H
                   for i, pr in enumerate(pages)):
                break
        for i, pr in enumerate(pages, 1):
            png = os.path.join(OUT, "compare", f"{i:02d}.png")
            r.shoot("compare.html.j2", png, rows=pr, **ctx(i == 1, i == len(pages), i, len(pages)))
            files.append(png)
        b.close()
    save_json(os.path.join(WORK, "compare.json"), {"files": files, "caption": cc["caption"], "week": wk})
    print(f"compare: {len(files)} image(s), {len(rows)} rows, caption {len(cc['caption'])} chars")
    summary(["### 매장 비교 카드", "", f"- 사진 {len(files)}장, {len(rows)}개 품목", "", "```", cc["caption"], "```"])
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("ready=true\n")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--compare", action="store_true", help="매장 비교 카드를 만든다")
    ap.add_argument("--force", action="store_true", help="요일과 이번 주 발송 여부를 보지 않는다")
    a = ap.parse_args()
    sys.exit(main_compare(a.force) if a.compare else main_albums())
