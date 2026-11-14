#!/usr/bin/env python3
"""work/messages.json 의 메시지를 텔레그램 봇으로 보낸다(전단 하나에 메시지 하나).

환경 변수 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 가 없으면 보내지 않고 그 사실만 출력한다(실패로 처리하지 않는다).
DRY_RUN 이 비어 있지 않으면 보내지 않고 로그에만 남긴다.
텔레그램 메시지 한 건은 4096자까지이므로 긴 본문은 줄 단위로 나눠 보낸다.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIMIT = 3800


def send(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"telegram error: {payload.get('description')}")


def chunks(text):
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > LIMIT and buf:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf.strip():
        yield buf


def main():
    try:
        messages = json.load(open(os.path.join(ROOT, "work", "messages.json"), encoding="utf-8"))
    except (OSError, ValueError):
        messages = []
    if not messages:
        print("no messages")
        return 0
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if os.environ.get("DRY_RUN"):
        print(f"DRY_RUN: {len(messages)} message(s) not sent")
        return 0
    if not token or not chat_id:
        print("::warning::TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; messages not sent")
        return 0
    failed = 0
    for msg in messages:
        for part in chunks(msg):
            try:
                send(token, chat_id, part)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"::error::telegram send failed: {e}")
            time.sleep(1)
    print(f"sent {len(messages)} message(s), {failed} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
