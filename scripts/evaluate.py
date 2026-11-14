#!/usr/bin/env python3
"""Claude 가 옮겨 적은 전단 품목을 기준(config/criteria.json)에 맞춰 판정하고 텔레그램 메시지를 만든다.

읽는 것
  work/manifest.json     : fetch_flyers.py 가 만든 이번 실행의 전단 목록
  CLAUDE_OUT(환경 변수)   : Claude 단계의 구조화된 출력(JSON 문자열). --input 파일로 대신 줄 수 있다
  config/criteria.json   : 알림 기준
쓰는 것
  work/messages.json     : 보낼 메시지 목록(전단 하나에 메시지 하나)
  state/history/YYYY-MM/ : 전단별로 옮겨 적은 품목과 판정(나중에 우리 동네 가격 기준을 다시 잡을 때 쓴다)
  state/seen.json        : 처리한 전단 표시. 판독에 실패하면 표시하지 않아 다음 실행이 다시 읽고,
                           같은 전단을 두 번 실패하면 표시하고 '직접 확인' 메시지를 보낸다
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "work")
STATE = os.path.join(ROOT, "state")
MAX_FAILS = 2

DOMESTIC_RE = re.compile(r"국내산|(?<![가-힣])국산|한우|한돈|육우|제주|우리\s?돼지")
IMPORTED = ("미국", "호주", "캐나다", "스페인", "칠레", "뉴질랜드", "멕시코", "브라질", "네덜란드", "덴마크",
            "독일", "프랑스", "오스트리아", "아일랜드", "중국", "베트남", "태국", "필리핀", "페루", "아르헨티나",
            "원양", "수입", "U.S", "USA")

SECTIONS = [
    ("🥩 소고기", ("소고기",)),
    ("🐷 돼지고기", ("돼지고기",)),
    ("🐔 닭고기", ("닭고기",)),
    ("🦑 오징어·수산", ("오징어", "수산")),
    ("🥬 채소", ("채소",)),
    ("🍎 과일", ("과일",)),
    ("🥚 두부·계란·우유·쌀", ("두부", "계란", "우유", "쌀")),
]
LEVEL = {None: 0, "ok": 1, "great": 2}


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


# ---------------------------------------------------------------- 품목 계산

def origin_class(item):
    # 원산지 칸을 먼저 보고, 비어 있을 때만 상품명으로 판단한다('미국산'의 '국산'처럼 겹치는 글자는 정규식으로 거른다)
    for text in (item.get("origin") or "", item.get("name") or ""):
        if any(k in text for k in IMPORTED):
            return "import"
        if DOMESTIC_RE.search(text):
            return "domestic"
    return None


def bogo_factor(item):
    deal = (item.get("deal") or "").replace(" ", "")
    if "1+1" in deal:
        return 0.5
    if "2+1" in deal:
        return 2 / 3
    return 1.0


def unit_price(item, basis, count_units=None):
    """기준 단위당 실제 가격(1+1·2+1 반영). 계산할 수 없으면 None"""
    price = item.get("price") or 0
    if price <= 0:
        return None
    price = price * bogo_factor(item)
    if basis == "100g" and item.get("grams"):
        return price / item["grams"] * 100
    if basis == "kg" and item.get("grams"):
        return price / item["grams"] * 1000
    if basis == "count" and item.get("count"):
        unit = item.get("count_unit") or ""
        if count_units and unit not in count_units:
            return None
        return price / item["count"]
    return None


def discount_pct(item):
    price, regular = item.get("price") or 0, item.get("regular") or 0
    if price > 0 and regular > price:
        return (1 - price / regular) * 100
    pct = item.get("pct")
    if isinstance(pct, (int, float)) and 0 < pct < 100:
        return float(pct)
    return None


def rule_matches(item, rule):
    if rule.get("cat") and item.get("cat") != rule["cat"]:
        return False
    name = item.get("name") or ""
    if rule.get("origin") and origin_class(item) != rule["origin"]:
        return False
    if rule.get("storage") and item.get("storage") not in rule["storage"]:
        return False
    if rule.get("no_seasoned") and (item.get("seasoned") or "양념" in name):
        return False
    if rule.get("name_any") and not any(k in name for k in rule["name_any"]):
        return False
    if rule.get("name_none") and any(k in name for k in rule["name_none"]):
        return False
    if rule.get("name_re") and not re.search(rule["name_re"], name):
        return False
    return True


def judge(item, rule):
    """반환: (등급 None|ok|great, 단위당 가격, 기준 단위)"""
    level, up, basis = None, None, None
    for spec in (rule, rule.get("alt")):
        if not spec or "basis" not in spec:
            continue
        v = unit_price(item, spec["basis"], spec.get("count_units"))
        if v is None:
            continue
        up, basis = v, spec["basis"]
        if v <= spec["ok"]:
            level = "great" if v <= spec.get("great", -1) else "ok"
        break
    pct = discount_pct(item)
    if pct is not None and "min_pct" in rule and pct >= rule["min_pct"]:
        plevel = "great" if pct >= rule.get("great_pct", 1000) else "ok"
        if LEVEL[plevel] > LEVEL[level]:
            level = plevel
    return level, up, basis


def bogo_allowed(item, cfg):
    if bogo_factor(item) == 1.0:
        return False
    return item.get("storage") in cfg.get("storage", []) or item.get("cat") in cfg.get("categories", [])


def evaluate(items, criteria):
    hits, bogos = [], []
    for it in items:
        rule = next((r for r in criteria["rules"] if rule_matches(it, r)), None)
        verdict = None
        if rule:
            level, up, basis = judge(it, rule)
            if level:
                verdict = {"level": level, "rule": rule["label"], "unit_price": up, "basis": basis}
                hits.append((it, verdict))
        if not verdict and bogo_allowed(it, criteria.get("bogo", {})):
            bogos.append(it)
        it["_verdict"] = verdict
    return hits, bogos


def dedupe(items):
    out, seen = [], set()
    for it in items:
        k = ((it.get("name") or "").replace(" ", ""), it.get("price"), it.get("grams"), it.get("count"))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


# ---------------------------------------------------------------- 메시지

def won(v):
    return f"{int(round(v)):,}원"


def basis_label(basis, item):
    if basis == "100g":
        return "100g당"
    if basis == "kg":
        return "kg당"
    return f"{item.get('count_unit') or '개'}당"


def item_line(it, verdict=None):
    meta = [m for m in (it.get("origin"), it.get("storage") if it.get("storage") in ("냉장", "냉동") else None) if m]
    name = it.get("name") or "?"
    if meta:
        name += f"({'·'.join(meta)})"
    spec = (it.get("spec") or "").split(",")[0].split("/")[0].strip()
    if spec in ("냉장", "냉동", "상온", it.get("origin")):
        spec = ""
    price = it.get("price") or 0
    parts = [name]
    if spec:
        parts.append(spec)
    head = " ".join(parts)
    if price > 0:
        head += f" {won(price)}"
    if verdict and verdict.get("level") == "great":
        head += " ⭐"
    tail = []
    up, basis = (verdict or {}).get("unit_price"), (verdict or {}).get("basis")
    if up is not None:
        shown_100g = basis == "100g" and it.get("grams") == 100 and bogo_factor(it) == 1.0
        shown_kg = basis == "kg" and it.get("grams") == 1000 and bogo_factor(it) == 1.0
        shown_count = basis == "count" and it.get("count") == 1 and bogo_factor(it) == 1.0
        if not (shown_100g or shown_kg or shown_count):
            tail.append(f"{basis_label(basis, it)} {won(up)}")
    pct = discount_pct(it)
    deal = it.get("deal") or ""
    if "%" in deal and not re.fullmatch(r"\s*(최대\s*)?\d+\s*%\s*(할인)?\s*", deal):
        tail.append(deal)            # '등심 40%·그 외 30%'처럼 할인율이 여러 개면 원문을 그대로 보인다
    else:
        if pct is not None:
            tail.append(f"{pct:.0f}%↓")
        if deal and "%" not in deal:
            tail.append(deal)
    if it.get("period"):
        tail.append(it["period"])
    if it.get("cond"):
        tail.append(it["cond"])
    return "• " + head + (" · " + " · ".join(tail) if tail else "")


def build_message(flyer, hits, bogos, unread):
    lines = [f"🛒 {flyer['store_name']} 새 전단"]
    if flyer.get("period"):
        lines.append(flyer["period"])
    if not hits and not bogos:
        lines.append("")
        lines.append("기준에 맞는 품목은 없었습니다.")
    for title, cats in SECTIONS:
        sec = [(it, v) for it, v in hits if it.get("cat") in cats]
        if not sec:
            continue
        sec.sort(key=lambda x: -LEVEL[x[1]["level"]])
        lines.append("")
        lines.append(title)
        lines += [item_line(it, v) for it, v in sec]
    if bogos:
        lines.append("")
        lines.append("🔁 1+1·2+1")
        lines += [item_line(it) for it in bogos]
    lines.append("")
    if unread:
        lines.append(f"※ 글씨를 읽지 못한 부분이 {len(unread)}곳 있습니다.")
    if flyer.get("note"):
        lines.append(f"※ {flyer['note']} 매장 판매가와 다를 수 있습니다.")
    else:
        lines.append("※ 전단 기준 가격입니다. 매장 판매가와 다를 수 있습니다.")
    lines.append(flyer["link"])
    return "\n".join(lines)


# ---------------------------------------------------------------- 상태

def mark_done(seen, flyer):
    st = seen.setdefault(flyer["store_id"], {})
    if flyer["source"] == "martmonster":
        st["sig"] = flyer["sig"]
        st["period"] = flyer.get("period", "")
    else:
        st["posts"] = (st.get("posts", []) + flyer.get("post_ids", []))[-100:]
        st["keys"] = (st.get("keys", []) + [flyer.get("date_key", "")])[-50:]
    st["at"] = time.strftime("%Y-%m-%d %H:%M")
    st.get("fail", {}).pop(flyer["key"], None)


def parse_output(raw):
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if isinstance(data, str):     # 문자열로 한 번 더 감싸인 경우
        try:
            data = json.loads(data)
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", help="Claude 출력 JSON 파일(없으면 환경 변수 CLAUDE_OUT)")
    ap.add_argument("--no-state", action="store_true", help="state/ 를 고치지 않는다(시험용)")
    args = ap.parse_args()

    manifest = load_json(os.path.join(WORK, "manifest.json"), {"flyers": []})
    criteria = load_json(os.path.join(ROOT, "config", "criteria.json"), {"rules": []})
    seen_path = os.path.join(STATE, "seen.json")
    seen = load_json(seen_path, {})
    raw = open(args.input, encoding="utf-8").read() if args.input else os.environ.get("CLAUDE_OUT", "")
    out = parse_output(raw)
    if out is None:
        print(f"::warning::no usable Claude output (step outcome: {os.environ.get('CLAUDE_CONCLUSION', '?')})")
    by_key = {f.get("key"): f for f in (out or {}).get("flyers", []) if isinstance(f, dict)}

    messages, summary = [], []
    today = time.strftime("%Y%m%d")
    for flyer in manifest.get("flyers", []):
        res = by_key.get(flyer["key"])
        if res is None:
            st = seen.setdefault(flyer["store_id"], {})
            fails = st.setdefault("fail", {})
            fails[flyer["key"]] = fails.get(flyer["key"], 0) + 1
            print(f"::warning::{flyer['key']}: not in Claude output (fail {fails[flyer['key']]}/{MAX_FAILS})")
            if fails[flyer["key"]] >= MAX_FAILS:
                messages.append(f"⚠️ {flyer['store_name']} 새 전단을 두 번 읽지 못했습니다. 직접 확인해 주세요.\n{flyer['link']}")
                mark_done(seen, flyer)
            continue
        items = dedupe([i for i in res.get("items", []) if isinstance(i, dict)])
        hits, bogos = evaluate(items, criteria)
        msg = build_message(flyer, hits, bogos, res.get("unread") or [])
        messages.append(msg)
        summary.append(f"### {flyer['store_name']}\n\n품목 {len(items)} · 알림 {len(hits)} · 1+1 {len(bogos)}\n\n```\n{msg}\n```\n")
        if not args.no_state:
            hist = {"flyer": {k: v for k, v in flyer.items() if k not in ("images", "text")},
                    "items": items, "unread": res.get("unread") or []}
            save_json(os.path.join(STATE, "history", today[:4] + "-" + today[4:6], f"{today}_{flyer['key']}.json"), hist)
            mark_done(seen, flyer)

    save_json(os.path.join(WORK, "messages.json"), messages)
    if not args.no_state:
        save_json(seen_path, seen)
    for m in messages:
        print(m, end="\n\n")
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step and summary:
        with open(step, "a", encoding="utf-8") as f:
            f.write("\n".join(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
