#!/usr/bin/env python3
"""이번 주 전단을 찾지 못한 매장을 알린다.

config/stores.json 에서 missing_alert 를 둔 매장만 본다. 정한 요일·시각(예: 금요일 17시)이 지났는데
오늘이 기간에 들어가는 전단을 하나도 찾지 못했으면 짧은 안내 메시지를 만든다. 그 주 일요일까지 매 실행이
다시 확인하고, 한 번 보낸 매장은 그 주에 다시 보내지 않는다.

'찾았다'의 기준은 판독 성공 여부가 아니라 전단 게시물을 발견했는지다. 판독에 실패한 전단은
evaluate.py 가 따로 '직접 확인' 메시지를 보내기 때문이다.

읽는 파일
  config/stores.json   : missing_alert 설정과 채널 이름
  state/seen.json      : 매장별로 처리한 전단의 날짜 키(11.19|11.25)
  state/missing.json   : 매장별로 안내를 보낸 주
  work/manifest.json   : 이번 실행에서 새로 찾은 전단(있으면)
쓰는 파일
  work/missing.json    : {"week": "2026-W47", "stores": [...], "messages": [...]}
  GITHUB_OUTPUT        : ready=true (보낼 안내가 있을 때)
"""
import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "work")
STATE = os.path.join(ROOT, "state")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def week_key(day):
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def nearest(m, d, today):
    """월·일만 있는 날짜에 오늘과 가장 가까운 연도를 붙인다"""
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            c = dt.date(y, m, d)
        except ValueError:
            continue
        if best is None or abs((c - today).days) < abs((best - today).days):
            best = c
    return best


def key_range(key, today):
    """'11.19|11.25' → (11/19, 11/25). 끝 날짜가 없으면 시작일부터 7일로 본다."""
    parts = []
    for p in (key or "").split("|"):
        try:
            m, d = (int(x) for x in p.split("."))
        except ValueError:
            continue
        c = nearest(m, d, today)
        if c:
            parts.append(c)
    if not parts:
        return None
    start = parts[0]
    end = parts[1] if len(parts) > 1 and parts[1] >= start else start + dt.timedelta(days=6)
    return start, end


def has_current(keys, today):
    for k in keys:
        r = key_range(k, today)
        if r and r[0] <= today <= r[1]:
            return True
    return False


def due(cfg, now):
    """정한 요일·시각부터 그 주 일요일까지"""
    wd = now.weekday()
    return wd > cfg["weekday"] or (wd == cfg["weekday"] and now.hour >= cfg.get("after_hour", 0))


def message(store, cfg):
    names = ", ".join(c.get("name") or c["id"] for c in store.get("channels") or [{"id": store.get("channel", ""), "name": store.get("channel_name", "")}])
    lines = [f"⚠️ {store['name']}: 이번 주 전단을 찾지 못했습니다",
             "",
             f"출처로 쓰는 카카오톡 채널({names})에 이번 주 전단이 올라오지 않았습니다.",
             "이번 주에는 알림이 가지 않으니, 필요하시면 직접 확인해 주세요."]
    if cfg.get("link"):
        lines += ["", f"{cfg.get('link_name') or '전단 보기'}: {cfg['link']}"]
    return "\n".join(lines)


def find_missing(stores, seen, sent, found_now, now):
    today = now.date()
    wk = week_key(today)
    out = []
    for s in stores:
        cfg = s.get("missing_alert")
        if not cfg or s.get("enabled") is False:
            continue
        if sent.get(s["id"]) == wk or not due(cfg, now):
            continue
        keys = list(seen.get(s["id"], {}).get("keys", [])) + found_now.get(s["id"], [])
        if has_current(keys, today):
            continue
        out.append((s, message(s, cfg)))
    return wk, out


def main():
    stores = load_json(os.path.join(ROOT, "config", "stores.json"), {}).get("stores", [])
    seen = load_json(os.path.join(STATE, "seen.json"), {})
    sent = load_json(os.path.join(STATE, "missing.json"), {})
    found_now = {}
    for f in load_json(os.path.join(WORK, "manifest.json"), {}).get("flyers", []):
        if f.get("date_key"):
            found_now.setdefault(f["store_id"], []).append(f["date_key"])
    wk, missing = find_missing(stores, seen, sent, found_now, dt.datetime.now())
    os.makedirs(WORK, exist_ok=True)
    path = os.path.join(WORK, "missing.json")
    if not missing:
        if os.path.exists(path):
            os.remove(path)
        print("missing: none")
        return 0
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"week": wk, "stores": [s["id"] for s, _ in missing], "messages": [m for _, m in missing]},
                  f, ensure_ascii=False, indent=1)
    print(f"missing: {', '.join(s['id'] for s, _ in missing)} ({wk})")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("ready=true\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
