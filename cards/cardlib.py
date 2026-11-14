"""전단 판독 결과를 카드에 넣을 형태로 정리한다(그리기는 render.py 가 한다).

여기 있는 함수는 모두 순수 계산이라 크롬 없이도 시험할 수 있다.
"""
import datetime as dt
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import evaluate as ev  # noqa: E402

WEEK = "월화수목금토일"
CAT_ORDER = ["소고기", "돼지고기", "닭고기", "오징어", "수산", "두부", "계란", "과일", "채소", "우유", "쌀"]
UNIT_NAME = {"미": "마리", "입": "개", "과": "개", "통": "포기"}
SIZE_WORDS = {"소", "중", "대", "특", "특대", "왕특", "왕특대", "대형", "중형", "소형"}
TRIVIAL_SPEC = {"개", "1개", "봉", "1봉", "팩", "1팩", "통", "단", "마리", "박스", "1박스", "상품별 규격 상이"}


# ---------------------------------------------------------------- 글자

def won(v):
    return f"{int(round(v)):,}원"


def num(v):
    return f"{int(round(v)):,}"


def josa(word, with_final="이", without_final="가"):
    """받침이 있으면 with_final, 없으면 without_final 을 붙인다(이/가, 은/는, 과/와)"""
    ch = word[-1] if word else ""
    if "가" <= ch <= "힣":
        return word + (with_final if (ord(ch) - 0xAC00) % 28 else without_final)
    return word + with_final


def date_of(m, d, today=None):
    """월·일만 있는 날짜에 오늘과 가장 가까운 연도를 붙인다(12월 전단을 1월에 보는 경우 등)"""
    today = today or dt.date.today()
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            c = dt.date(y, m, d)
        except ValueError:
            continue
        if best is None or abs((c - today).days) < abs((best - today).days):
            best = c
    return best


def md(m, d, with_month=True, today=None):
    day = date_of(m, d, today)
    w = f"({WEEK[day.weekday()]})" if day else ""
    return (f"{m}월 " if with_month else "") + f"{d}일{w}"


def parse_range(text):
    """'11/12(목) ~ 11/18(수)', '[11.13(금)-15(일)' → ((11, 12), (11, 18))"""
    m = re.search(r"(\d{1,2})[./](\d{1,2})", text or "")
    if not m:
        return None
    a = (int(m.group(1)), int(m.group(2)))
    m2 = re.search(r"[~\-–]\s*(?:(\d{1,2})[./])?(\d{1,2})", text[m.end():])
    b = (int(m2.group(1)) if m2.group(1) else a[0], int(m2.group(2))) if m2 else a
    if not (1 <= a[0] <= 12 and 1 <= b[0] <= 12):
        return None
    return a, b


def period_text(title, today=None):
    r = parse_range(title)
    if not r:
        return ""
    (am, ad), (bm, bd) = r
    if (am, ad) == (bm, bd):
        return md(am, ad, today=today) + " 하루"
    return f"{md(am, ad, today=today)}부터 {md(bm, bd, with_month=(am != bm), today=today)}까지"


def until_text(p, today=None):
    """품목 기간 '11/12~11/15' → '11월 15일(일)까지'"""
    r = parse_range(p)
    if not r:
        return ""
    (am, ad), (bm, bd) = r
    if (am, ad) == (bm, bd):
        return md(am, ad, today=today) + " 하루"
    return md(bm, bd, today=today) + "까지"


def short_cond(c):
    if not c:
        return ""
    c = re.sub(r"\(([^)]*카드[^)]*)\)", "", c)            # 행사카드(롯데/삼성/KB국민카드) → 행사카드
    c = re.sub(r"회원\s*한정,\s*1인\s*최대\s*(\d+만원)\s*할인", r"회원, 1인 \1까지", c)
    c = re.sub(r"회원\s*기준\s*기간\s*내\s*1인\s*최대\s*(\d+만원)\s*할인", r"회원, 1인 \1까지", c)
    return c.strip(" ,")


def unit_label(basis, item):
    if basis == "100g":
        return "100g당"
    if basis == "kg":
        return "1kg당"
    u = item.get("count_unit") or "개"
    return f"{UNIT_NAME.get(u, u)}당"


def origin_line(it):
    parts = []
    o = (it.get("origin") or "").strip()
    if o and o not in ("별도표기", "모름"):
        parts.append(o)
    if it.get("storage") in ("냉장", "냉동"):
        parts.append(it["storage"])
    return " ".join(parts)


def spec_short(it):
    s = (it.get("spec") or "").split(",")[0].split("/")[0].strip()
    s = re.sub(r"\s*※.*$", "", s)
    if s in ("냉장", "냉동", "상온", it.get("origin") or "-") or s in TRIVIAL_SPEC:
        return ""
    return s


def clean_name(it):
    """원산지 줄과 겹치는 단어를 이름에서 뺀다(호주 냉장 앞다리 국거리 → 앞다리 국거리)"""
    ol = origin_line(it)
    name = it.get("name") or "?"
    if not ol:
        return name
    drop = set()
    for w in ol.split():
        drop.add(w)
        if w.endswith("산") and len(w) > 2:
            drop.add(w[:-1])
    kept = [w for w in name.split() if w not in drop]
    return " ".join(kept) if kept else name


def label_name(it):
    name = clean_name(it)
    s = spec_short(it)
    if s in SIZE_WORDS and s not in (it.get("name") or ""):
        name += f"({s})"
    return name


def qty_text(it, per100=False):
    """원산지 줄 뒤에 붙일 수량(5마리, 1kg, 6~10개)"""
    s = spec_short(it)
    if s in SIZE_WORDS:
        s = ""
    if per100 and s == "100g":
        return ""
    if s:
        s = re.sub(r"(\d+)\s*[~\-]\s*(\d+)\s*입\s*봉", r"\1~\2개 1봉", s)
        s = re.sub(r"(\d+)\s*[~\-]\s*(\d+)\s*입", r"\1~\2개", s)
        s = re.sub(r"(\d+)\s*미$", r"\1마리", s)
        s = re.sub(r"(\d+)\s*입$", r"\1개", s)
        return s
    c = it.get("count")
    if c and float(c).is_integer() and c > 1:
        u = it.get("count_unit") or "개"
        return f"{int(c)}{UNIT_NAME.get(u, u)}"
    return ""


def sticker(it):
    pct = ev.discount_pct(it)
    deal = (it.get("deal") or "").replace(" ", "")
    if "1+1" in deal:
        return {"big": "1+1", "small": ""}
    if "2+1" in deal:
        return {"big": "2+1", "small": ""}
    if pct is not None:
        return {"big": f"{pct:.0f}%", "small": "할인"}
    m = re.search(r"(\d+(?:천|만)원)할인", deal)
    if m:
        return {"big": m.group(1).replace("원", ""), "small": "원 할인"}
    return None


def deal_note(it, price=0):
    d = (it.get("deal") or "").strip()
    if not d or d == "월간가격" or "%" in d or re.search(r"[12]\s*\+\s*1", d):
        return ""
    if re.fullmatch(r"\d+(천|만)?원\s*할인", d.replace(" ", "")):
        return ""
    if price and num(price) in d:          # '10개 구매 시 9,900원'은 가격과 수량이 이미 보여 준다
        return ""
    return d


# ---------------------------------------------------------------- 판정 설명

def verdict(it, rule):
    """어느 기준으로 걸렸는지와 그 기준값을 사람이 읽는 문장으로"""
    lvl_b, lvl_p, up, basis = None, None, None, None
    for spec in (rule, rule.get("alt")):
        if not spec or "basis" not in spec:
            continue
        v = ev.unit_price(it, spec["basis"], spec.get("count_units"))
        if v is None:
            continue
        up, basis = v, spec["basis"]
        if v <= spec["ok"]:
            lvl_b = ("great", spec["great"]) if v <= spec.get("great", -1) else ("ok", spec["ok"])
        break
    pct = ev.discount_pct(it)
    if pct is not None and "min_pct" in rule and pct >= rule["min_pct"]:
        lvl_p = ("great", rule["great_pct"]) if pct >= rule.get("great_pct", 1000) else ("ok", rule["min_pct"])
    rank = {"ok": 1, "great": 2}
    use_b = bool(lvl_b) and (not lvl_p or rank[lvl_b[0]] >= rank[lvl_p[0]])
    chosen = lvl_b if use_b else lvl_p
    level = chosen[0] if chosen else "ok"
    head = "강력 추천" if level == "great" else "추천"
    if use_b:
        crit = f"기준: {unit_label(basis, it)} {num(lvl_b[1])}원 이하"
    elif lvl_p:
        crit = f"기준: 할인율 {lvl_p[1]}% 이상"
    else:
        crit = ""
    return {"level": level, "head": head, "crit": crit, "unit_price": up, "basis": basis}


def merge_same_deals(hits):
    """같은 값·같은 조건으로 나온 품목(삼겹살/목심 등)을 하나로 합친다"""
    groups = {}
    for it, v in hits:
        k = (it.get("cat"), it.get("price"), it.get("grams"), it.get("count"), it.get("deal"), it.get("cond"),
             it.get("period"), it.get("storage"))
        groups.setdefault(k, []).append((it, v))
    out = []
    for g in groups.values():
        if len(g) == 1:
            out.append(g[0])
            continue
        names = [(x[0].get("name") or "").split() for x in g]
        n = 0
        while all(len(w) > n for w in names) and len({tuple(w[:n + 1]) for w in names}) == 1:
            n += 1
        merged = " ".join(names[0][:n] + ["·".join(" ".join(w[n:]) for w in names)])
        out.append((dict(g[0][0], name=merged), g[0][1]))
    return out


def cat_rank(c):
    return CAT_ORDER.index(c) if c in CAT_ORDER else len(CAT_ORDER)


def enrich(it, rule, today=None):
    v = verdict(it, rule)
    price = it.get("price") or 0
    up, basis = v["unit_price"], v["basis"]
    per100 = basis == "100g" and it.get("grams") == 100 and ev.bogo_factor(it) == 1.0
    if per100:
        unit_line = "100g당 가격"
    elif up is not None:
        approx = bool(re.search(r"\d\s*[~\-]\s*\d", it.get("spec") or "")) and basis not in ("100g", "kg")
        unit_line = f"{unit_label(basis, it)} 약 {won(round(up, -1))}" if approx else f"{unit_label(basis, it)} {won(up)}"
    else:
        unit_line = spec_short(it)
    cond_lines = [x for x in (until_text(it.get("period") or "", today), short_cond(it.get("cond"))) if x]
    if (it.get("deal") or "") == "월간가격":
        cond_lines.insert(0, "월간 가격 행사")
    pct = ev.discount_pct(it)
    return dict(
        it,
        v=v,
        label_name=label_name(it),
        origin_line=origin_line(it),
        qty=qty_text(it, per100),
        sub=", ".join(x for x in (origin_line(it), qty_text(it, per100)) if x),
        per100=per100,
        price_num=num(price) if price else "",
        regular_txt=won(it["regular"]) if it.get("regular") and it["regular"] > price else "",
        sticker=sticker(it),
        unit_line=unit_line,
        cond_lines=cond_lines,
        alt_big=(f"최대 {pct:.0f}%" if not price and pct else ""),
        deal_note=deal_note(it, price),
    )


def group_rows(rest, min_group=3):
    """같은 조건·같은 할인(예: 이멤버 회원 50%)으로 묶인 품목이 3개 이상이면 한 줄로 합친다"""
    keyed = {}
    for r in rest:
        keyed.setdefault((short_cond(r.get("cond")), r.get("deal") or ""), []).append(r)
    out, used = [], set()
    for r in rest:
        if id(r) in used:
            continue
        k = (short_cond(r.get("cond")), r.get("deal") or "")
        g = keyed[k]
        if len(g) >= min_group and k[1]:
            deal_txt = k[1].strip()
            out.append({
                "group": True,
                "cat": "·".join(sorted({x["cat"] for x in g}, key=cat_rank)),
                "name": f"{len(g)}개 품목 {deal_txt + ' 할인' if deal_txt.endswith('%') else deal_txt}",
                "meta": ", ".join(x["name"] for x in g),
                "cond": k[0],
                "level": "great" if any(x["v"]["level"] == "great" for x in g) else "ok",
            })
            used.update(id(x) for x in g)
        else:
            out.append(dict(r, group=False))
            used.add(id(r))
    return out


# ---------------------------------------------------------------- 1+1

def bogo_entry(it, today=None):
    price = it.get("price") or 0
    f = ev.bogo_factor(it)
    deal = "1+1" if f == 0.5 else "2+1"
    spec = spec_short(it)
    if not re.search(r"\d", spec):            # '윌유메리미' 같은 향·종류 이름은 빼고 규격만 남긴다
        spec = ""
    elif re.fullmatch(r"\d+입", spec):
        spec = spec.replace("입", "개입")
    mix = "교차구매" in (it.get("cond") or "").replace(" ", "")
    meta = [x for x in (spec, "교차 구매 가능" if mix else "", until_text(it.get("period") or "", today)) if x]
    return {
        "deal": deal,
        "name": it.get("name") or "?",
        "price": won(price) if price else "",
        "each": f"개당 {won(price * f)}" if price else "상품별 가격",
        "food": it.get("cat") not in ("생필품", "기타"),
        "meta": ", ".join(meta),
    }


def bogo_sections(bogos, today=None):
    rows = [bogo_entry(x, today) for x in bogos]
    food = [r for r in rows if r["food"]]
    goods = [r for r in rows if not r["food"]]
    has21 = any(r["deal"] == "2+1" for r in rows)
    return {
        "title_deal": "1+1·2+1" if has21 else "1+1",
        "count": len(rows),
        "lede": "개당 가격은 1+1은 2개, 2+1은 3개를 샀을 때로 계산했습니다." if has21
                else "개당 가격은 2개를 샀을 때로 계산했습니다.",
        "sections": [s for s in (("냉장·냉동 식품", food), ("생필품", goods)) if s[1]],
    }


# ---------------------------------------------------------------- 매장 한 곳

def store_context(flyer, items, criteria, store_cfg=None, hero_max=3, today=None):
    rules = {r["label"]: r for r in criteria.get("rules", [])}
    items = ev.dedupe([{k: v for k, v in i.items() if k != "_verdict"} for i in items])
    hits, bogos = ev.evaluate(items, criteria)
    hits = merge_same_deals(hits)
    rich = [enrich(it, rules[v["rule"]], today) for it, v in hits]
    rich.sort(key=lambda r: (0 if r["v"]["level"] == "great" else 1, cat_rank(r.get("cat")),
                             -(ev.discount_pct(r) or 0)))
    heroes, rest = rich[:hero_max], rich[hero_max:]
    rest.sort(key=lambda r: (cat_rank(r.get("cat")), 0 if r["v"]["level"] == "great" else 1))
    note = (store_cfg or {}).get("msg_note") or flyer.get("note") or ""
    footer_head = note.split("(")[0].strip() + " 기준이며" if "전국 공통" in note else "전단 기준이며"
    return dict(
        store=flyer.get("store_name") or (store_cfg or {}).get("name") or "",
        period=period_text(flyer.get("title") or flyer.get("period") or "", today),
        lede=f"기준을 통과한 품목 {len(rich)}개를 골랐습니다." if rich else "기준을 통과한 품목이 없습니다.",
        heroes=heroes,
        rows=group_rows(rest),
        all=rich,
        bogos=bogo_sections(bogos, today) if bogos else None,
        footer=f"{footer_head}, 매장 판매가와 다를 수 있습니다.",
    )


CHUNK_RE = re.compile(r"(\d+)([LR]?)_([a-z])\.jpg$")


def chunk_where(path, siblings=None):
    """'.../03_b.jpg' → '3쪽 아래'. 같은 이미지의 조각 수로 위·가운데·아래를 정한다"""
    m = CHUNK_RE.search(path or "")
    if not m:
        return ""
    no, side, part = int(m.group(1)), m.group(2), m.group(3)
    side_txt = {"L": " 왼쪽", "R": " 오른쪽"}.get(side, "")
    n = 1
    d = os.path.dirname(path)
    if siblings is None and os.path.isdir(d):
        siblings = os.listdir(d)
    if siblings:
        n = len([s for s in siblings if re.fullmatch(rf"0*{no}{side}_[a-z]\.jpg", s)])
    idx = ord(part) - 97
    if n <= 1:
        pos = ""
    elif n == 2:
        pos = ("위", "아래")[min(idx, 1)]
    else:
        pos = "위" if idx == 0 else ("아래" if idx == n - 1 else "가운데")
    return f"{no}쪽{side_txt}" + (f" {pos}" if pos else "")


def crop_pages(rich):
    """⭐ 품목이 있는 조각만 고른다. 조각마다 그 안에서 기준을 통과한 품목을 함께 적는다"""
    by_chunk = {}
    for it in rich:
        p = it.get("img") or ""
        if CHUNK_RE.search(p) and os.path.exists(p):
            by_chunk.setdefault(p, []).append(it)
    out = []
    for path in sorted(by_chunk, key=lambda p: os.path.basename(p)):
        items = by_chunk[path]
        if not any(x["v"]["level"] == "great" for x in items):
            continue
        items.sort(key=lambda x: 0 if x["v"]["level"] == "great" else 1)
        out.append({"where": chunk_where(path), "image": path, "items": items})
    return out


def caption(ctx, ncrop, total):
    """앨범 첫 장에 붙이는 설명 글. 알림 미리보기와 텔레그램 검색에 쓰이므로 ⭐ 품목을 글자로 남긴다"""
    lines = [f"🛒 {ctx['store']} 새 전단"]
    if ctx["period"]:
        lines.append(ctx["period"])
    greats = [x for x in ctx["all"] if x["v"]["level"] == "great"]
    for x in greats:
        amount = "100g" if x["per100"] else (x["qty"] or "")
        price = f"{x['price_num']}원" if x["price_num"] else x["alt_big"]
        line = " ".join(p for p in ("⭐", x["label_name"], amount, price) if p)
        r = parse_range(x.get("period") or "")
        if r:
            line += f" ({r[1][0]}/{r[1][1]}까지)"
        lines.append(line)
    rest = []
    n_ok = len(ctx["all"]) - len(greats)
    if n_ok:
        rest.append(f"추천 {n_ok}개")
    if ctx["bogos"]:
        rest.append(f"{ctx['bogos']['title_deal']} {ctx['bogos']['count']}개")
    if ncrop:
        rest.append(f"전단 원본 {ncrop}장")
    if rest:
        lines.append("그 밖의 " + josa(", ".join(rest), "은", "는") + f" 사진 {total}장에 있습니다.")
    text = "\n".join(lines)
    return text[:1000]


# ---------------------------------------------------------------- 매장 비교

def compare_pick(items, cat, criteria=None):
    """한 매장의 품목 중 비교 항목(cat)에 맞는 것의 최저 단위 가격"""
    best = None
    for it in items:
        if not ev.rule_matches(it, cat["match"]):
            continue
        v = ev.unit_price(it, cat["basis"], cat.get("count_units"))
        if v is None:
            continue
        if best is None or v < best[0]:
            best = (v, it)
    return best


def compare_note(it, basis, unit=""):
    parts = []
    s = spec_short(it)
    if s in SIZE_WORDS:
        parts.append(s)
    q = qty_text(it, per100=(basis == "100g" and it.get("grams") == 100))
    if q and basis != "100g" and q != unit:
        parts.append(q)
    deal = (it.get("deal") or "").replace("할인", "").strip()
    pct = ev.discount_pct(it)
    if "%" in deal and len(deal) <= 12:
        parts.append(deal)
    elif pct:
        parts.append(f"{pct:.0f}%")
    note = ", ".join(parts)
    if len(note) > 12 and len(parts) > 1:      # 칸이 좁아 두 줄이 되지 않도록, 길면 할인 조건 하나만 남긴다
        note = parts[-1]
    return note


def compare_context(stores, cfg, today=None, max_rows=8):
    """stores: [(store_id, 이름, flyer, items)]. 두 매장 이상에 값이 있는 항목만, 관심 순서대로 max_rows 개"""
    today = today or dt.date.today()
    rows, wins = [], {sid: 0 for sid, *_ in stores}
    for cat in cfg["items"]:
        cells = []
        for sid, _, _, items in stores:
            best = compare_pick(items, cat)
            if not best:
                cells.append(None)
                continue
            v, it = best
            if cat.get("per"):
                v *= cat["per"]
            approx = cat["basis"] == "count" and bool(re.search(r"\d\s*[~\-]\s*\d", it.get("spec") or ""))
            cells.append({"sid": sid, "v": v, "approx": approx, "note": compare_note(it, cat["basis"], cat.get("unit", ""))})
        filled = [c for c in cells if c]
        if len(filled) < 2:
            continue
        best = min(filled, key=lambda c: c["v"])
        for c in filled:
            c["best"] = c is best
            shown = round(c["v"], -1) if c["approx"] else c["v"]
            c["price"] = ("약 " if c["approx"] else "") + won(shown)
        wins[best["sid"]] += 1
        rows.append({"canon": cat["label"], "unit": cat["unit"], "cells": cells})
        if len(rows) >= max_rows:
            break
    cols = [{"sid": sid, "name": name, "wins": 0} for sid, name, *_ in stores]
    for c in cols:
        c["wins"] = sum(1 for r in rows for x in r["cells"] if x and x["best"] and x["sid"] == c["sid"])
    top = max(cols, key=lambda c: c["wins"]) if rows else cols[0]
    periods = []
    for sid, name, flyer, _ in stores:
        r = parse_range(flyer.get("title") or flyer.get("period") or "")
        if r:
            periods.append(f"{name} {r[0][0]}/{r[0][1]}~{r[1][0]}/{r[1][1]}")
    when = f"{today.month}월 {today.day}일({WEEK[today.weekday()]})"
    headline = (f"{josa(top['name'])} {len(rows)}개 중 {top['wins']}개 품목에서 가장 쌉니다." if rows else "")
    return {
        "title": "세 마트 가격 비교" if len(stores) == 3 else f"{len(stores)}개 마트 가격 비교",
        "period": f"{when} 기준, 전단에 함께 나온 {len(rows)}개 품목의 단위 가격입니다.",
        "headline": headline,
        "cols": cols, "rows": rows, "top": top["sid"],
        "footer": "단위 가격만 비교했고 품질 차이는 반영하지 않았습니다. 전단 기간: " + ", ".join(periods) + ".",
        "caption": "\n".join(x for x in [
            "📊 이번 주 마트 가격 비교",
            f"{when} 기준",
            headline,
            "가장 싼 곳: " + ", ".join(f"{c['name']} {c['wins']}개" for c in sorted(cols, key=lambda c: -c["wins"])),
        ] if x),
    }
