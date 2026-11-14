# mart-flyer-alert

동네 마트의 새 전단을 매일 확인해서, 미리 정한 기준보다 싼 품목과 1+1 행사만 텔레그램으로 보냅니다.

| 매장 | 전단 출처 |
|---|---|
| 이마트 평촌점 | 이마트 전국 공통 전단: [이마트 월배점 카카오톡 채널](https://pf.kakao.com/_ExjSJG/posts) |
| 롯데마트 의왕점 | 롯데마트 전국 공통 전단: [롯데마트 강변점 카카오톡 채널](https://pf.kakao.com/_lBCxiK/posts) |
| 킴스클럽 평촌점 | [킴스클럽 카카오톡 채널](https://pf.kakao.com/_SBXxmxd/posts) (평촌점이 빠진 'N개점' 행사는 제외) |
| 홈플러스 평촌점 | 꺼 둠: 마트몬이 GitHub 서버 요청을 막고, 전단을 매주 올리는 카카오톡 채널을 찾지 못했다 |

처음에는 [마트몬](https://martmonster.com)에서 받았지만, 마트몬이 GitHub Actions 서버의 요청을 403 으로 거절해서 카카오톡 채널로 바꿨습니다.
이마트·롯데마트 전단은 전국 공통이라, 매주 빠짐없이 전단을 올리는 지점 채널을 씁니다(평촌점 채널은 전단을 매주 올리지 않고, 의왕점 채널은 찾지 못했습니다).
지점 채널을 바꾸려면 `config/stores.json` 의 `channel` 만 고치면 됩니다.

## 동작

`.github/workflows/flyers.yml` 이 매일 08:30, 18:30(KST)에 돕니다. GitHub 예약 실행은 늦게 시작하곤 합니다.

1. `scripts/fetch_flyers.py` 가 채널마다 최근 게시물을 읽고, 아직 처리하지 않은 전단 게시물이 있으면 이미지를 내려받아 나눕니다(작은 글씨를 줄이지 않고 읽게 하려는 것입니다).
2. 새 전단이 있을 때만 Claude 가 이미지를 읽고 품목·규격·가격·조건을 JSON 으로 옮겨 적습니다(`prompts/extract.md`, `prompts/schema.json`). 구독 토큰으로 돌기 때문에 API 요금은 들지 않고, 쓰는 양은 구독 사용 한도에서 빠집니다. Claude 에게는 Read 도구 하나만 줍니다.
3. `scripts/evaluate.py` 가 `config/criteria.json` 기준으로 판정하고 메시지를 만듭니다. 옮겨 적은 품목은 모두 `state/history/` 에 남깁니다.
4. `cards/render.py` 가 판정 결과로 1080x1350(4:5) 카드 이미지를 만듭니다. 요약(⭐ 품목은 가격표 모양), 나머지 품목과 1+1 목록, ⭐ 품목이 실린 전단 원본 조각 순서이고, 내용이 많으면 다음 장으로 이어집니다. 헤드리스 크롬으로 HTML 템플릿(`cards/templates/`)을 찍고, 글꼴은 저장소에 넣은 Pretendard(SIL OFL 1.1, `cards/fonts/LICENSE.txt`)를 씁니다.
5. `scripts/notify_telegram.py` 가 매장마다 앨범(2~10장, 첫 장에 ⭐ 품목을 적은 설명 글)을 먼저 보내고, 이어서 지금까지와 같은 텍스트 메시지를 보냅니다. 카드를 만들지 못하거나 앨범을 보내지 못해도 텍스트는 갑니다. 기준에 맞는 품목이 없어도 "새 전단이 올라왔다"는 짧은 메시지는 갑니다.
6. 목요일 18:30 실행부터 그 주 일요일까지 한 번, 세 매장의 같은 품목을 단위 가격으로 맞춘 **매장 비교 카드**를 보냅니다(`config/compare.json`, 보낸 주는 `state/compare.json`). 두 매장 이상 전단에 나온 품목만, 관심 순서대로 최대 8개를 씁니다.

카드 이미지는 실행마다 Actions 의 `cards` 아티팩트로 7일 동안 남습니다.

Claude 단계가 실패하면 그 전단은 처리 표시를 하지 않아 다음 실행이 다시 읽습니다. 같은 전단을 두 번 실패하면 "직접 확인해 주세요" 메시지를 보내고 넘어갑니다.

## 설정

저장소 Settings → Secrets and variables → Actions 에 세 값을 넣습니다(RRG 저장소와 같은 값).

- `CLAUDE_SETUP_TOKEN`: 컴퓨터에서 `claude setup-token` 을 실행해 만든 구독 토큰
- `TELEGRAM_BOT_TOKEN`: @BotFather 에서 받은 봇 토큰
- `TELEGRAM_CHAT_ID`: 메시지를 받을 대화 ID

선택: Variables 탭에 `CLAUDE_MODEL`(기본 `sonnet`)을 두면 판독 모델을 바꿉니다.

## 수동 실행

Actions → flyers → Run workflow

- `force`: 이미 처리한 전단도 다시 읽을 매장 id(`emart_pyeongchon`, `lotte_uiwang`, `kims_pyeongchon`, 쉼표로 구분) 또는 `all`
- `dry_run`: 텔레그램으로 보내지 않고 실행 요약(Summary)에만 메시지를 남깁니다. 처리 표시도 하지 않습니다
- `compare`: 요일과 이번 주 발송 여부에 상관없이 매장 비교 카드를 만들어 보냅니다

## 기준 바꾸기

`config/criteria.json` 의 규칙은 위에서부터 차례로 맞춰 보고, 처음 맞는 규칙 하나로만 판정합니다.

- `basis`: `100g` · `kg` · `count`(개수당, `count_units` 로 단위 제한). `ok` 이하이면 알림, `great` 이하이면 ⭐
- `min_pct` / `great_pct`: 할인율 기준. `basis` 기준과 함께 있으면 둘 중 하나만 맞아도 알림
- `origin`: `domestic`(국내산·한우·한돈) · `import`(수입)
- `no_seasoned`: 양념육·훈제·가공품 제외(양념 무게 때문에 100g당 가격이 싸 보이는 것을 막습니다)
- 1+1 은 절반, 2+1 은 3분의 2 가격으로 계산합니다
- 1+1 묶음 알림 대상은 `bogo` 항목(냉장·냉동 식품, 신선식품, 생필품)입니다

기준가는 2026-11 KAMIS·축산물품질평가원 소매가격보다 약 20~30% 싼 수준으로 잡았습니다. `state/history/` 에 쌓인 실제 전단 가격을 보고 다시 조정하면 됩니다.

## 매장 추가

`config/stores.json` 에 추가합니다. 카카오톡 채널은 `source: kakao` 와 채널 id(`pf.kakao.com/_xxxx` 의 `_xxxx`)를 적습니다. 마트몬(`source: martmonster`) 수집 코드도 남아 있지만, GitHub 서버에서는 403 으로 막힙니다.

## 시험

```bash
pip install pillow jinja2 playwright
python -m unittest discover -s tests -v
```

카드를 직접 찍어 보려면 Chrome 이 있는 컴퓨터에서 `work/cards.json` 을 만든 뒤 `python cards/render.py` 를 실행합니다.
