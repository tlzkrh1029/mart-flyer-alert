#!/usr/bin/env python3
"""work/ 의 메시지를 텔레그램 봇으로 보낸다.

  work/messages.json : 전단 하나에 텍스트 메시지 하나
  work/albums.json   : 메시지 순번 → 카드 이미지 앨범(cards/render.py). 있으면 그 텍스트 앞에 앨범을 먼저 보낸다
  work/compare.json  : 목요일 매장 비교 카드. 보내고 나면 state/compare.json 에 이번 주를 적는다

앨범은 2~10장을 한 묶음(sendMediaGroup)으로, 1장이면 일반 사진(sendPhoto)으로 보낸다. 설명 글은 첫 장에 붙는다.
앨범을 보내지 못해도 텍스트는 그대로 보낸다(앨범 실패는 경고로만 남긴다).
환경 변수 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 가 없으면 보내지 않고 그 사실만 출력한다(실패로 처리하지 않는다).
DRY_RUN 이 비어 있지 않으면 보내지 않고 로그에만 남긴다.
텔레그램 메시지 한 건은 4096자까지이므로 긴 본문은 줄 단위로 나눠 보낸다.
"""
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "work")
LIMIT = 3800
ALBUM_MAX = 10
NET_TRIES = 3               # 연결이 끊겼을 때 다시 보내는 횟수


def load(name, default):
    try:
        with open(os.path.join(WORK, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def multipart(fields, files):
    boundary = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out += [f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{k}"'.encode(), b"",
                str(v).encode("utf-8")]
    for name, filename, data, ctype in files:
        out += [f"--{boundary}".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode(),
                f"Content-Type: {ctype}".encode(), b"", data]
    out += [f"--{boundary}--".encode(), b""]
    return b"\r\n".join(out), f"multipart/form-data; boundary={boundary}"


def api(token, method, fields, files=None, retry=True):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        body, ctype = multipart(fields, files)
    else:
        body, ctype = urllib.parse.urlencode(fields).encode(), "application/x-www-form-urlencoded"
    payload = None
    for attempt in range(NET_TRIES):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            payload = json.loads(e.read().decode("utf-8") or "{}")
            break
        except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException) as e:
            # 큰 앨범을 올린 직후 연결이 끊기는 일이 있어, 잠깐 쉬고 다시 보낸다
            if attempt == NET_TRIES - 1:
                raise
            print(f"::warning::telegram {method}: {e}; retrying")
            time.sleep(3 * (attempt + 1))
    if payload.get("ok"):
        return payload["result"]
    wait = (payload.get("parameters") or {}).get("retry_after")
    if retry and wait:              # 너무 빨리 보내면 텔레그램이 기다릴 시간을 알려 준다
        time.sleep(min(int(wait) + 1, 60))
        return api(token, method, fields, files, retry=False)
    raise RuntimeError(f"telegram {method} error: {payload.get('description')}")


def send_text(token, chat_id, text):
    api(token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"})


def send_album(token, chat_id, paths, caption):
    """사진 1장은 sendPhoto, 2~10장은 sendMediaGroup. 10장을 넘으면 10장씩 나눈다(설명 글은 첫 묶음 첫 장)"""
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        raise RuntimeError("no image files")
    sizes = []
    for start in range(0, len(paths), ALBUM_MAX):
        group = paths[start:start + ALBUM_MAX]
        cap = caption if start == 0 else ""
        files = [(f"f{i}", os.path.basename(p), open(p, "rb").read(), "image/png") for i, p in enumerate(group)]
        if len(group) == 1:
            fields = {"chat_id": chat_id}
            if cap:
                fields["caption"] = cap
            res = api(token, "sendPhoto", fields, [("photo",) + files[0][1:]])
            results = [res]
        else:
            media = [{"type": "photo", "media": f"attach://f{i}"} for i in range(len(group))]
            if cap:
                media[0]["caption"] = cap
            results = api(token, "sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)},
                          files)
        for m in results:           # 텔레그램이 보관한 가장 큰 사진 크기(압축 확인용)
            ph = max(m.get("photo") or [{}], key=lambda x: x.get("width", 0) * x.get("height", 0))
            sizes.append(f"{ph.get('width')}x{ph.get('height')}")
        time.sleep(1)
    return sizes


def chunks(text):
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > LIMIT and buf:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf.strip():
        yield buf


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    messages = load("messages.json", [])
    albums = load("albums.json", {})
    compare = load("compare.json", None)
    missing = load("missing.json", None)
    if not messages and not compare and not missing:
        print("no messages")
        return 0
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if os.environ.get("DRY_RUN"):
        print(f"DRY_RUN: {len(messages)} message(s), {len(albums)} album(s), compare {'yes' if compare else 'no'}, "
              f"missing {len((missing or {}).get('messages', []))}; not sent")
        for m in (missing or {}).get("messages", []):
            print(m)
        return 0
    if not token or not chat_id:
        print("::warning::TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; messages not sent")
        return 0
    failed = 0
    for i, msg in enumerate(messages):
        al = albums.get(str(i))
        if al:
            try:
                sizes = send_album(token, chat_id, al["files"], al.get("caption", ""))
                print(f"album {i}: {len(al['files'])} image(s) sent, stored as {', '.join(sizes)}")
            except Exception as e:  # noqa: BLE001  앨범이 실패해도 텍스트는 보낸다
                print(f"::warning::album {i} not sent: {e}")
        for part in chunks(msg):
            try:
                send_text(token, chat_id, part)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"::error::telegram send failed: {e}")
            time.sleep(1)
    if compare:
        try:
            sizes = send_album(token, chat_id, compare["files"], compare.get("caption", ""))
            print(f"compare: {len(compare['files'])} image(s) sent, stored as {', '.join(sizes)}")
            path = os.path.join(ROOT, "state", "compare.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"week": compare.get("week"), "sent_at": time.strftime("%Y-%m-%d %H:%M")}, f,
                          ensure_ascii=False, indent=1)
                f.write("\n")
        except Exception as e:  # noqa: BLE001  보내지 못하면 표시하지 않아 다음 실행이 다시 보낸다
            failed += 1
            print(f"::error::compare card not sent: {e}")
    if missing:
        path = os.path.join(ROOT, "state", "missing.json")
        state = load_state(path)
        for sid, msg in zip(missing.get("stores", []), missing.get("messages", [])):
            try:
                send_text(token, chat_id, msg)
                state[sid] = missing.get("week")
                print(f"missing: notice sent for {sid}")
            except Exception as e:  # noqa: BLE001  보내지 못하면 표시하지 않아 다음 실행이 다시 보낸다
                failed += 1
                print(f"::error::missing notice for {sid} not sent: {e}")
            time.sleep(1)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
            f.write("\n")
    print(f"sent {len(messages)} message(s), {len(albums)} album(s), {failed} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
