"""수집·판정 로직 시험. 실행: python -m unittest discover -s tests -v"""
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import check_missing  # noqa: E402
import evaluate  # noqa: E402
import fetch_flyers  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
NOW = 1794645000  # 2026-11-14 16:30 KST 무렵


def fixture(name):
    return open(os.path.join(FIX, name), encoding="utf-8").read()


class TestMartmonster(unittest.TestCase):
    def test_parse(self):
        info = fetch_flyers.parse_martmonster(fixture("martmonster_3695.html"), "https://martmonster.com/gyeonggi/3695")
        self.assertEqual(info["title"], "이마트 평촌점")
        self.assertEqual(info["period"], "전단 행사 기간 : 11월 12일(목) ~ 11월 18일(수) * 비신선식품 ~ 12/2(수)까지")
        self.assertEqual(len(info["images"]), 12)
        self.assertTrue(all(u.startswith("https://martmonster.com/files/attach/images/167705/") for u in info["images"]))
        self.assertNotIn("other.jpg", " ".join(info["images"]))

    def test_signature_ignores_order(self):
        a = ["https://x/files/a.jpg", "https://x/files/b.jpg"]
        self.assertEqual(fetch_flyers.flyer_signature(a), fetch_flyers.flyer_signature(a[::-1]))
        self.assertNotEqual(fetch_flyers.flyer_signature(a), fetch_flyers.flyer_signature(a[:1]))


class TestKakao(unittest.TestCase):
    STORE = {"id": "kims_pyeongchon", "branch_keyword": "평촌", "min_images": 3, "max_age_days": 8}

    def picks(self, seen=None):
        data = json.loads(fixture("kakao_posts.json"))
        return fetch_flyers.select_kakao_posts(data, self.STORE, seen or {}, now=NOW)

    def test_select(self):
        got = {p[0]["id"]: p for p in self.picks()}
        # 같은 행사를 두 번 올린 게시물은 이미지가 많은 쪽 하나만, 평촌이 빠진 'N개점' 행사·이미지 없는 글·오래된 글은 뺀다
        self.assertEqual(sorted(got), [115194066, 115300000])
        self.assertEqual(got[115300000][3], "11.18|11.24")
        self.assertEqual(sorted(got[115300000][4]), [115300000, 115300001])
        self.assertEqual(len(got[115300000][1]), 9)

    def test_date_key(self):
        self.assertEqual(fetch_flyers.date_key("11/12(목) ~ 11/18(수) 금주 행사 안내"), "11.12|11.18")
        self.assertEqual(fetch_flyers.date_key("[이마트월배점]금주 전단행사 안내(11/12(목) ~ 11/18(수)"), "11.12|11.18")
        self.assertEqual(fetch_flyers.date_key("김장 원스톱 쇼핑", "※전단적용기간: 11/12(목) ~ 11/18(수)"), "11.12|11.18")
        self.assertEqual(fetch_flyers.date_key("와인 大 축제", "1/2 할인"), "1.2")

    def test_require_any(self):
        data = {"items": [
            {"id": 1, "title": "11/12(목) ~ 11/18(수) 금주 행사 안내", "published_at": NOW * 1000,
             "contents": [{"t": "text", "v": "※전단적용기간: 11/12(목) ~ 11/18(수)"}], "media": [{"type": "image", "xlarge_url": f"u{i}"} for i in range(5)]},
            {"id": 2, "title": "절임배추 사전예약 11/20", "published_at": NOW * 1000,
             "contents": [{"t": "text", "v": "사전예약"}], "media": [{"type": "image", "xlarge_url": f"v{i}"} for i in range(5)]},
        ]}
        store = {"id": "lotte", "require_any": ["전단"], "min_images": 3, "max_age_days": 8}
        self.assertEqual([p[0]["id"] for p in fetch_flyers.select_kakao_posts(data, store, {}, now=NOW)], [1])

    def test_real_channels(self):
        # 2026-11-14 기준: 와인·김장 게시물은 거르고 주간 전단 게시물 하나만 고른다
        stores = {s["id"]: s for s in json.load(open(os.path.join(ROOT, "config", "stores.json"), encoding="utf-8"))["stores"]}
        fx = json.loads(fixture("kakao_emart_lotte.json"))
        emart = fetch_flyers.select_kakao_posts(fx["emart_wolbae"], stores["emart_pyeongchon"], {}, now=NOW)
        lotte = fetch_flyers.select_kakao_posts(fx["lotte_gangbyeon"], stores["lotte_uiwang"], {}, now=NOW)
        self.assertEqual([p[0]["id"] for p in emart], [115205793])
        self.assertEqual(emart[0][3], "11.12|11.18")
        self.assertEqual([p[0]["id"] for p in lotte], [115193409])

    def test_seen(self):
        self.assertEqual([p[0]["id"] for p in self.picks({"keys": ["11.18|11.24"], "posts": [115194066]})], [])


class TestSplit(unittest.TestCase):
    def run_split(self, w, h):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (w, h), "white").save(buf, "JPEG")
        with tempfile.TemporaryDirectory() as d:
            paths = fetch_flyers.split_image(buf.getvalue(), d, "01")
            return [Image.open(p).size for p in paths]

    def test_flyer_page(self):
        self.assertEqual(self.run_split(1200, 1800), [(1200, 960), (1200, 955)])

    def test_square(self):
        self.assertEqual(self.run_split(800, 800), [(800, 800)])

    def test_long(self):
        sizes = self.run_split(1080, 5000)
        self.assertEqual(len(sizes), 7)
        self.assertTrue(all(h >= 300 for _, h in sizes))

    def test_wide_spread(self):
        # 두 쪽을 펼친 전단(6142x4252)은 좌우로 나눈 뒤 각각 1200 너비로 줄여 위아래로 자른다
        sizes = self.run_split(6142, 4252)
        self.assertEqual(len(sizes), 4)
        self.assertTrue(all(w == 1200 for w, _ in sizes))

    def test_lotte_page(self):
        self.assertEqual(len(self.run_split(2657, 3839)), 2)


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        self.criteria = json.load(open(os.path.join(ROOT, "config", "criteria.json"), encoding="utf-8"))
        out = json.loads(fixture("claude_output.json"))
        self.items = evaluate.dedupe(out["flyers"][0]["items"])
        self.hits, self.bogos = evaluate.evaluate(self.items, self.criteria)
        self.hit = {it["name"]: v for it, v in self.hits}

    def test_dedupe(self):
        self.assertEqual(sum(1 for i in self.items if i["name"] == "프라임 척아이롤"), 1)

    def test_beef(self):
        self.assertEqual(self.hit["프라임 척아이롤"]["level"], "great")       # 수입 냉장 2,988원 ≤ 3,000원
        self.assertEqual(self.hit["프라임 척아이롤"]["rule"], "수입 소고기(냉장)")
        self.assertEqual(self.hit["호주산 찜갈비"]["level"], "ok")             # 수입 갈비 2,690원 ≤ 3,000원
        self.assertEqual(self.hit["호주 냉장 앞다리 국거리"]["level"], "great")
        self.assertEqual(self.hit["브랜드한우 전품목"]["level"], "ok")         # 한우 40% 할인
        for name in ("냉동 척아이롤 샤브샤브", "양념 안창살 소불고기", "한우 헤비스테이크 3종"):
            self.assertNotIn(name, self.hit)

    def test_others(self):
        self.assertEqual(self.hit["한돈 삼겹살"]["level"], "great")
        self.assertNotIn("국내산 앞다리", self.hit)                           # 1,260원 > 1,200원
        self.assertEqual(self.hit["백숙용 생닭"]["level"], "great")           # kg당 3,980원
        self.assertEqual(self.hit["선동오징어"]["level"], "great")            # 마리당 1,980원
        self.assertEqual(self.hit["바나나"]["level"], "ok")                   # 송이당 2,990원
        self.assertEqual(self.hit["제주 무"]["level"], "ok")
        for name in ("동물복지 계란", "팬시 엔비사과", "대파", "제철맞은 방어회"):
            self.assertNotIn(name, self.hit)

    def test_origin(self):
        self.assertEqual(evaluate.origin_class({"origin": "미국산"}), "import")
        self.assertEqual(evaluate.origin_class({"origin": "중국산"}), "import")
        self.assertEqual(evaluate.origin_class({"origin": "국내산"}), "domestic")
        self.assertEqual(evaluate.origin_class({"origin": "", "name": "한우 등심"}), "domestic")
        self.assertIsNone(evaluate.origin_class({"origin": "별도표기", "name": "양념 소불고기"}))

    def test_bogo(self):
        names = sorted(i["name"] for i in self.bogos)
        self.assertEqual(names, ["비비고 왕교자", "키친타월", "풀무원 부침두부"])

    def test_message(self):
        flyer = {"key": "k", "store_name": "이마트 평촌점", "period": "전단 행사 기간 : 11월 12일(목) ~ 11월 18일(수)",
                 "link": "https://martmonster.com/gyeonggi/3695", "note": "이마트 전단은 전국 공통이고 가격은 은평점 기준입니다."}
        msg = evaluate.build_message(flyer, self.hits, self.bogos, ["x"])
        self.assertIn("• 프라임 척아이롤(미국산·냉장) 100g 2,988원 ⭐ · 40%↓ · 11/12~11/15 · 신세계포인트 적립 시", msg)
        self.assertIn("🔁 1+1·2+1", msg)
        self.assertIn("• 브랜드한우 전품목(국내산·냉장) · 등심 40%·등심 외 30% 할인 · 신세계포인트 적립 시", msg)
        self.assertIn("100g당 497원", evaluate.item_line(self.bogos[0], {"level": None, "unit_price": 496.7, "basis": "100g"}))
        self.assertLess(len(msg), 3800)
        print("\n" + msg)


class TestRun(unittest.TestCase):
    """manifest → evaluate.main → messages.json·seen.json 까지 한 번에"""

    def test_main(self):
        with tempfile.TemporaryDirectory() as d:
            work, state = os.path.join(d, "work"), os.path.join(d, "state")
            os.makedirs(work)
            manifest = {"flyers": [
                {"key": "emart_pyeongchon-abc123", "store_id": "emart_pyeongchon", "store_name": "이마트 평촌점",
                 "source": "martmonster", "period": "기간", "link": "L", "sig": "abc123", "images": [], "note": ""},
                {"key": "lotte_uiwang-def456", "store_id": "lotte_uiwang", "store_name": "롯데마트 의왕점",
                 "source": "martmonster", "period": "기간", "link": "L2", "sig": "def456", "images": [], "note": ""},
            ]}
            json.dump(manifest, open(os.path.join(work, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False)
            evaluate.WORK, evaluate.STATE = work, state
            sys.argv = ["evaluate.py", "--input", os.path.join(FIX, "claude_output.json")]
            evaluate.main()
            seen = json.load(open(os.path.join(state, "seen.json"), encoding="utf-8"))
            self.assertEqual(seen["emart_pyeongchon"]["sig"], "abc123")
            self.assertNotIn("sig", seen["lotte_uiwang"])                  # 출력에 없는 전단은 처리 표시를 하지 않는다
            self.assertEqual(seen["lotte_uiwang"]["fail"]["lotte_uiwang-def456"], 1)
            msgs = json.load(open(os.path.join(work, "messages.json"), encoding="utf-8"))
            self.assertEqual(len(msgs), 1)
            # 두 번째 실패에서는 '직접 확인' 메시지를 보내고 처리 표시를 한다
            evaluate.main()
            seen = json.load(open(os.path.join(state, "seen.json"), encoding="utf-8"))
            self.assertEqual(seen["lotte_uiwang"]["sig"], "def456")
            msgs = json.load(open(os.path.join(work, "messages.json"), encoding="utf-8"))
            self.assertTrue(any(m.startswith("⚠️ 롯데마트 의왕점") for m in msgs))

            cards = json.load(open(os.path.join(work, "cards.json"), encoding="utf-8"))
            self.assertEqual(cards[0]["msg"], 0)                            # 카드 재료는 메시지 순번과 짝을 이룬다

    def test_mostly_unread_is_failure(self):
        imgs = [f"/w/{i:02d}_a.jpg" for i in range(1, 9)]
        flyer = {"images": imgs}
        bad = {"items": [{"name": "x"}], "unread": [f"{p}: 한도 초과로 판독 못 함" for p in imgs[:6]]}
        ok = {"items": [{"name": "x"}], "unread": [f"{imgs[0]}: 가격이 흐림"]}
        self.assertTrue(evaluate.mostly_unread(flyer, bad))
        self.assertFalse(evaluate.mostly_unread(flyer, ok))
        self.assertFalse(evaluate.mostly_unread({"images": imgs[:2]}, bad))   # 조각이 적은 전단은 보지 않는다



class TestBatches(unittest.TestCase):
    """전단마다 Claude 세션을 따로 쓰고, 조각이 많은 전단은 나눠 읽은 뒤 합친다"""

    def flyer(self, key, n, text=""):
        return {"key": key, "store_id": key.split("-")[0], "store_name": key, "period": "기간",
                "images": [f"/w/{key}/{i:02d}_a.jpg" for i in range(1, n + 1)], "text": text}

    def test_plan(self):
        b = fetch_flyers.plan_batches([self.flyer("emart-1", 24), self.flyer("lotte-2", 10), self.flyer("kims-3", 32, "본문")])
        self.assertEqual([len(x["flyers"][0]["images"]) for x in b], [24, 10, 16, 16])   # 32조각은 16+16
        self.assertEqual([x["flyers"][0].get("part") for x in b], [None, None, "1/2", "2/2"])
        self.assertEqual([x["flyers"][0]["text"] for x in b[2:]], ["본문", ""])             # 본문은 첫 묶음에만
        self.assertTrue(all(len(x["flyers"]) == 1 for x in b))                            # 세션 하나에 전단 하나

    def test_slots(self):
        imgs = list(range(32))
        got, left = fetch_flyers.take_slots([], imgs, 5, "kims")
        self.assertEqual((len(got), left), (32, 3))
        got, left = fetch_flyers.take_slots(["x"], imgs, 1, "kims")                       # 칸이 모자라면 다음 실행으로
        self.assertEqual((got, left), (None, 1))
        got, left = fetch_flyers.take_slots([], list(range(200)), 5, "big")              # 첫 전단은 앞쪽만 읽는다
        self.assertEqual((len(got), left), (150, 0))

    def test_merge(self):
        f = self.flyer("kims-3", 32)
        b = fetch_flyers.plan_batches([f, self.flyer("emart-1", 8)])
        p1, p2 = b[0]["flyers"][0]["images"], b[1]["flyers"][0]["images"]
        out = lambda key, name, unread=(): json.dumps({"flyers": [{"key": key, "items": [{"name": name}], "unread": list(unread)}]})
        both = evaluate.merge_batches(b, {1: out("kims-3", "a"), 2: out("kims-3", "b"), 3: out("emart-1", "c")})
        self.assertEqual([i["name"] for i in both["kims-3"]["items"]], ["a", "b"])
        self.assertIn("emart-1", both)
        # 한 묶음이 빠지면 그 전단 전체를 실패로 본다(일부만 읽은 결과로 알림을 보내지 않는다)
        miss = evaluate.merge_batches(b, {1: out("kims-3", "a"), 2: "", 3: out("emart-1", "c")})
        self.assertNotIn("kims-3", miss)
        self.assertIn("emart-1", miss)
        # 한 묶음을 대부분 읽지 못해도 실패
        bad = [f"{p}: 한도 초과로 판독 못 함" for p in p2[:10]]
        half = evaluate.merge_batches(b, {1: out("kims-3", "a"), 2: out("kims-3", "b", bad), 3: out("emart-1", "c")})
        self.assertNotIn("kims-3", half)
        self.assertEqual(len(p1) + len(p2), 32)



NOW22 = 1795316400  # 2026-11-22 12:40 KST 무렵


def kpost(pid, title, body, n, ch, hours_ago, size=None, tag=""):
    media = [{"type": "image", "xlarge_url": f"https://k.kakaocdn.net/dn/{pid}{tag}/{i}/img_xl.jpg"} for i in range(n)]
    for m in media:
        if size:
            m["width"], m["height"] = size
    return {"id": pid, "title": title, "contents": [{"t": "text", "v": body}], "media": media,
            "published_at": int((NOW22 - hours_ago * 3600) * 1000), "_ch": {"id": ch, "name": ch}}


class TestEmartChannels(unittest.TestCase):
    """2026-11-22 이마트 출처 조사 결과를 그대로 옮긴 시험"""

    STORES = {s["id"]: s for s in json.load(open(os.path.join(ROOT, "config", "stores.json"), encoding="utf-8"))["stores"]}
    EMART = STORES["emart_pyeongchon"]
    # 2026-11-20 이마트 울산점 채널의 실제 게시물 두 개(본문 일부)
    ULSAN_FLYER = ("🛒이번 주 이마트 행사 소식",
                   "▶신선식품 : 11/19(목) – 11/25(수)\n▶그 외 품목 : 11/12(목) – 12/2(수)\n우리 가족 취향 저격할 신선 먹거리부터")
    ULSAN_AD = ("이번주 광고상품 안내",
                "🗓행사기간 : 11/19(목) ~ 11/25(수)\n· 파머스픽 고당도 감귤 / 돌빌레 감귤\n· 다발무 3,983원\n· 생굴\n· 달링다운 와규 윗등심살")

    def pick(self, items, seen=None):
        return fetch_flyers.select_kakao_posts({"items": items}, self.EMART, seen or {}, now=NOW22)

    def test_config(self):
        chans = fetch_flyers.store_channels(self.EMART)
        self.assertEqual([c["name"] for c in chans],
                         ["이마트 평촌점", "이마트 월배점", "이마트 천안터미널점", "이마트 원주점", "이마트 울산점"])
        self.assertEqual([c["name"] for c in chans if c.get("own")], ["이마트 평촌점"])
        self.assertEqual([c["name"] for c in chans if c.get("regional")], ["이마트 천안터미널점"])
        self.assertEqual(len(fetch_flyers.store_channels(self.STORES["lotte_uiwang"])), 3)
        self.assertEqual(fetch_flyers.store_channels({"channel": "_x", "channel_name": "a"}), [{"id": "_x", "name": "a"}])

    def test_ulsan_flyer_not_ad(self):
        flyer = kpost(115302369, *self.ULSAN_FLYER, 4, "_avxcxob", 46)
        ad = kpost(115302321, *self.ULSAN_AD, 4, "_avxcxob", 47)
        got = self.pick([flyer, ad])
        self.assertEqual([(p[0]["id"], p[3], p[4]) for p in got], [(115302369, "11.19|11.25", [115302369])])
        # 광고상품 글을 나중에 올려도 전단을 고른다(제외 낱말이 없으면 이 경우 광고상품 글이 골라졌다)
        ad_later = kpost(115302400, *self.ULSAN_AD, 4, "_avxcxob", 40)
        self.assertEqual([p[0]["id"] for p in self.pick([flyer, ad_later])], [115302369])
        # 월배점의 같은 광고상품 글만 있는 주에는 아무것도 고르지 않는다
        self.assertEqual(self.pick([kpost(1, "[이마트월배점] " + self.ULSAN_AD[0], self.ULSAN_AD[1], 4, "_ExjSJG", 90)]), [])

    def test_rules(self):
        # 2026-11-22 결정: 이마트도 사진 3장부터 정기 전단으로 인정한다(2장은 아니다)
        three = kpost(2, "[이마트월배점] 미리 준비하는 김장!! 11/5(목) ~ 11/11(수) 전단", "김장", 3, "_ExjSJG", 10)
        two = kpost(4, "[이마트월배점] 금주 전단행사 11/12(목) ~ 11/18(수)", "", 2, "_ExjSJG", 10)
        no_word = kpost(3, "[이마트월배점] 이마트 PICK! 경동나비엔 온수매트!", "🗓행시기간 : 11/19(목) ~ 11/25(수)", 4, "_ExjSJG", 10)
        self.assertEqual([p[0]["id"] for p in self.pick([three, two, no_word])], [2])

    def test_same_week_across_channels(self):
        # 11/12 주(게시 시각은 이번 주로 옮김): 월배점(기간 11/12~11/18, 12장)과 천안터미널점(본문 11/12(목)~18(수), 6장)이 같은 전단을 올렸다
        wolbae = kpost(115205793, "[이마트월배점]금주 전단행사 안내(11/12(목) ~ 11/18(수)", "행사기간 : 11/12(목) ~ 11/18(수)", 12, "_ExjSJG", 30)
        cheonan = kpost(115206000, "[이마트천안터미널점] 금주전단행사", "행사기간 : 11/12(목)~18(수)", 6, "_xdGQxls", 10)
        self.assertEqual(fetch_flyers.date_key(cheonan["title"], _text(cheonan)), "11.12")
        got = self.pick([cheonan, wolbae])
        self.assertEqual(len(got), 1)                           # 시작일이 같으면 한 번만 처리한다
        self.assertEqual(got[0][0]["id"], 115205793)            # 사진이 많은 월배점 것을 읽는다
        self.assertEqual(sorted(got[0][4]), [115205793, 115206000])
        self.assertEqual(got[0][0]["_ch"]["id"], "_ExjSJG")
        # 다음 실행에서 천안터미널점 글만 남아 있어도(기간 표기가 달라도) 다시 읽지 않는다
        self.assertEqual(self.pick([cheonan], {"keys": ["11.12|11.18"], "posts": []}), [])

    def test_fallback_channel(self):
        # 월배점에 이번 주 전단이 없으면 다른 채널의 전단을 고르고, 그 채널 이름을 출처로 쓴다
        flyer = kpost(115302369, *self.ULSAN_FLYER, 4, "_avxcxob", 46)
        got = self.pick([kpost(1, "[이마트월배점] " + self.ULSAN_AD[0], self.ULSAN_AD[1], 4, "_ExjSJG", 90), flyer])
        self.assertEqual([p[0]["_ch"]["id"] for p in got], ["_avxcxob"])

    def test_title_with_period(self):
        self.assertEqual(fetch_flyers.titled_with_period("🛒이번 주 이마트 행사 소식", "11.19|11.25"),
                         "🛒이번 주 이마트 행사 소식 (11/19 ~ 11/25)")
        self.assertEqual(fetch_flyers.titled_with_period("", "11.19|11.25"), "이번 주 전단 (11/19 ~ 11/25)")
        self.assertEqual(fetch_flyers.titled_with_period("11/19(목) ~ 11/25(수) 금주 행사 안내", "11.19|11.25"),
                         "11/19(목) ~ 11/25(수) 금주 행사 안내")


def _text(post):
    return fetch_flyers._post_text(post)


class TestMissing(unittest.TestCase):
    STORES = TestEmartChannels.STORES

    def run_at(self, when, seen=None, sent=None, found=None):
        import datetime as dt
        now = dt.datetime(*when)
        wk, got = check_missing.find_missing(list(self.STORES.values()), seen or {}, sent or {}, found or {}, now)
        return wk, [s["id"] for s, _ in got], [m for _, m in got]

    def test_friday_evening(self):
        last_week = {"emart_pyeongchon": {"keys": ["11.12|11.18"]}, "lotte_uiwang": {"keys": ["11.19|11.25"]}}
        wk, ids, msgs = self.run_at((2026, 11, 20, 17, 50), seen=last_week)
        self.assertEqual((wk, ids), ("2026-W47", ["emart_pyeongchon"]))   # 롯데마트는 이번 주 전단이 있다
        self.assertIn("이마트 월배점, 이마트 천안터미널점, 이마트 원주점, 이마트 울산점", msgs[0])
        self.assertIn("https://eapp.emart.com/leaflet/leafletView_EL.do", msgs[0])

    def test_not_due_or_already_sent(self):
        seen = {"emart_pyeongchon": {"keys": ["11.12|11.18"]}, "lotte_uiwang": {"keys": ["11.19|11.25"]}}
        self.assertEqual(self.run_at((2026, 11, 20, 13, 0), seen=seen)[1], [])          # 금요일 낮에는 아직
        self.assertEqual(self.run_at((2026, 11, 19, 20, 0), seen=seen)[1], [])          # 목요일
        self.assertEqual(self.run_at((2026, 11, 22, 3, 0), seen=seen, sent={"emart_pyeongchon": "2026-W47"})[1], [])
        self.assertEqual(self.run_at((2026, 11, 22, 3, 0), seen=seen)[1], ["emart_pyeongchon"])   # 일요일 새벽 실행도 보낸다

    def test_found_now_or_event(self):
        seen = {"lotte_uiwang": {"keys": ["11.19|11.25"]}}
        self.assertEqual(self.run_at((2026, 11, 20, 18, 0), seen=seen, found={"emart_pyeongchon": ["11.19"]})[1], [])
        # 쓱데이처럼 기간이 긴 행사(10/30~11/8)가 이어지는 주에는 보내지 않는다
        ev = {"emart_pyeongchon": {"keys": ["10.30|11.8"]}, "lotte_uiwang": {"keys": ["11.5|11.11"]}}
        self.assertEqual(self.run_at((2026, 11, 6, 18, 0), seen=ev)[1], [])

    def test_not_flyer_is_not_found(self):
        manifest = {"flyers": [{"key": "emart_pyeongchon-7", "store_id": "emart_pyeongchon", "date_key": "11.19|11.25"},
                               {"key": "lotte_uiwang-8", "store_id": "lotte_uiwang", "date_key": "11.19|11.25"},
                               {"key": "emart_pyeongchon-9", "store_id": "emart_pyeongchon", "date_key": ""}]}
        self.assertEqual(check_missing.found_now_keys(manifest, ["emart_pyeongchon-7"]), {"lotte_uiwang": ["11.19|11.25"]})


# ---------------------------------------------------------------- 2026-11-22 결정한 수집 규칙

PORTRAIT, SPREAD = (1280, 1850), (6378, 4606)
STORES = TestEmartChannels.STORES
EMART, LOTTE, KIMS = STORES["emart_pyeongchon"], STORES["lotte_uiwang"], STORES["kims_pyeongchon"]


def cpost(store, pid, title, body, n, ch_id, hours_ago, size=PORTRAIT, tag="", **extra):
    """채널 설정(own·regional 등)을 붙인 게시물. 실제 실행에서도 fetch_flyers.main 이 '_ch' 에 채널 설정을 붙인다."""
    p = kpost(pid, title, body, n, ch_id, hours_ago, size, tag)
    p["_ch"] = {c["id"]: c for c in fetch_flyers.store_channels(store)}.get(ch_id, {"id": ch_id, "name": ch_id})
    p.update(extra)
    return p


def picks(store, items, seen=None):
    return [(p[0]["id"], p[5]["reason"]) for p in fetch_flyers.select_kakao_posts({"items": items}, store, seen or {}, now=NOW22)]


# 2026-11-17 이마트 평촌점 채널의 실제 전단 게시물(제목 칸이 비어 있고, 두 쪽을 펼친 사진 2장)
PC_BODY = ("신선 먹거리 행사 \n기간:11/19(목)~11/25(수)\n\n매주 일요일 정상 영업 \n금주 전단 상품 한눈에 보기\n"
           " http://emart.kr/onLeaflet?isShareUrl=Y \n\n○이마트앱 단골설정 고객 대상 이벤트\n-사은품: 물티슈 + 장바구니")


class TestOwnStoreAndPages(unittest.TestCase):
    def test_title_and_pages(self):
        pc = cpost(EMART, 115271695, "", PC_BODY, 2, "_ZNRxnxj", 120, SPREAD)
        self.assertEqual(fetch_flyers.effective_title(pc), "신선 먹거리 행사")
        self.assertEqual(fetch_flyers.page_count(pc), 4)                     # 펼친 사진 2장 = 4쪽
        self.assertEqual(fetch_flyers.page_count(cpost(EMART, 1, "t", "", 4, "_avxcxob", 1)), 4)

    def test_own_channel_two_images(self):
        pc = cpost(EMART, 115271695, "", PC_BODY, 2, "_ZNRxnxj", 120, SPREAD)
        self.assertEqual(picks(EMART, [pc]), [(115271695, "new")])          # 우리 매장 채널은 사진 2장부터
        two = cpost(EMART, 2, "[이마트월배점] 금주 전단행사 11/19(목) ~ 11/25(수)", "", 2, "_ExjSJG", 10)
        self.assertEqual(picks(EMART, [two]), [])                            # 다른 채널은 3장부터

    def test_own_first_then_much_bigger(self):
        pc = cpost(EMART, 115271695, "", PC_BODY, 2, "_ZNRxnxj", 120, SPREAD)
        ulsan = cpost(EMART, 115302369, *TestEmartChannels.ULSAN_FLYER, 4, "_avxcxob", 46)
        self.assertEqual(picks(EMART, [ulsan, pc]), [(115271695, "new")])   # 4쪽 대 4쪽: 우리 매장 채널 것만
        wolbae = cpost(EMART, 115205793, "[이마트월배점]금주 전단행사 안내(11/19(목) ~ 11/25(수)", "", 12, "_ExjSJG", 30)
        # 월배점이 12쪽이면(4쪽의 두 배 이상, 세 쪽 이상 많음) 그것도 읽는다. 우리 매장 채널 것이 먼저
        self.assertEqual(picks(EMART, [wolbae, pc]), [(115271695, "new"), (115205793, "extra")])

    def test_regional_tiebreak(self):
        cheonan = cpost(EMART, 115219040, "[이마트천안터미널점] 금주전단행사", "11/12(목) 천안지역 특별혜택 전단", 6, "_xdGQxls", 20,
                        (1203, 857))
        wolbae = cpost(EMART, 115205793, "[이마트월배점]금주 전단행사 안내(11/12(목) ~ 11/18(수)", "", 12, "_ExjSJG", 30)
        self.assertEqual(fetch_flyers.page_count(cheonan), 12)
        self.assertEqual(picks(EMART, [cheonan, wolbae]), [(115205793, "new")])   # 12쪽 대 12쪽이면 전국판
        self.assertEqual(picks(EMART, [cheonan]), [(115219040, "new")])            # 천안판만 있으면 그것을 읽는다


class TestEventAndTitleRules(unittest.TestCase):
    def test_event_flyer_two_images(self):
        festa = cpost(EMART, 1, "[이마트월배점]고래잇페스타 가격 봉인 세일", "🗓기간 : 8/14(목)~8/17(일)\n복숭아 초특가", 2, "_ExjSJG", 5)
        self.assertEqual(picks(EMART, [festa]), [(1, "new")])
        ssg = cpost(EMART, 2, "", "어서오세요! 돌아왔습니다~✨\n대한민국 시그니처 세일, 2026 쓱데이🎁\n🗓기 간 : 11/5(목) ~ 11/8(일)",
                    6, "_avxcxob", 5)
        self.assertEqual(picks(EMART, [ssg]), [(2, "new")])                  # '전단' 낱말이 없는 쓱데이 전단(울산점)

    def test_giveaway_posts(self):
        cup = cpost(EMART, 3, "고래잇 이중 유리컵까지 챙겨가고래잇", "8/14(목)~8/17(일) 3만원 이상 구매 시 증정", 2, "_ExjSJG", 5)
        keyring = cpost(EMART, 4, "고래잇 한복 키링 받아가고래잇!", "9/3(목)~9/6(일) 선착순", 4, "_ExjSJG", 5)
        sticker = cpost(EMART, 5, "[이마트월배점] 스티커 모으고 쓱데이 담요 받으세요!☃️", "11/5(목)~11/8(일) 스티커 5개", 3, "_ExjSJG", 5)
        self.assertEqual(picks(EMART, [cup, keyring, sticker]), [])
        # 정기 전단 조건(전단 낱말·사진 3장)을 채우면 제목에 '증정'이 있어도 전단이다
        weekly = cpost(EMART, 6, "[이마트월배점] 금주 전단행사(장바구니 증정) 11/19(목)~11/25(수)", "", 4, "_ExjSJG", 5)
        self.assertEqual(picks(EMART, [weekly]), [(6, "new")])

    def test_lotte_tongkeun_day(self):
        day = cpost(LOTTE, 114457878, "# 9월 통큰데이로 가을 타는 입맛 회복하세요♡",
                    "【9월 통큰데이 9/3(목)~9/6(일)】\n▶ 소 LA 갈비 (1kg/냉동) → 29,980원", 4, "_wxiuXn", 5)
        self.assertEqual(picks(LOTTE, [day]), [(114457878, "new")])

    def test_ad_word_in_title_only(self):
        # 천안터미널점 7/23 전단은 본문에 '이번주 광고상품 안내'가 있지만 제목은 전단이다
        cheonan = cpost(EMART, 114043579, "[이마트천안터미널점] 금주 전단행사 7/23(목)~7/29(수)",
                        "[📣] 이번주 광고상품 안내\n🗓행시기간 : 7/23(목) ~ 7/29(수)", 3, "_xdGQxls", 5)
        self.assertEqual(picks(EMART, [cheonan]), [(114043579, "new")])
        # 제목 칸이 빈 울산점 광고상품 글은 본문 첫 줄을 제목으로 보고 뺀다
        ad = cpost(EMART, 115302321, "", "이번주 광고상품 안내\n\n🗓행사기간 : 11/19(목) ~ 11/25(수)\n· 생굴 이번 주 이마트 행사",
                   4, "_avxcxob", 5)
        self.assertEqual(picks(EMART, [ad]), [])


class TestStoreOnlyPosts(unittest.TestCase):
    WEEKLY = ("[이마트월배점]금주 전단행사 안내(11/19(목) ~ 11/25(수)", "")

    def test_other_store_dandok(self):
        cheonan = cpost(EMART, 113337121, "[이마트천안터미널점] 금주 단독 전단 행사", "📅 행사기간 : 11/19(목) ~ 11/25(수)",
                        8, "_xdGQxls", 5)
        perk = cpost(EMART, 112303546, "[고래잇한 혜택]11월 천안터미널점 단독 행사 안내", "11/19~11/30 고래잇 이스탬프", 3, "_xdGQxls", 5)
        gangbyeon = cpost(LOTTE, 9, "강변점 단독>봄맞이 신선식품 고객감사 SALE", "11/19(목)~11/25(수) 행사 안내", 4, "_lBCxiK", 5)
        self.assertEqual(picks(EMART, [cheonan, perk]), [])
        self.assertEqual(picks(LOTTE, [gangbyeon]), [])

    def test_own_dandok_is_separate_and_first(self):
        own = cpost(EMART, 7, "[이마트평촌점] 금주 단독 전단 행사", "기간:11/19(목)~11/25(수)\n금주 전단 평촌점 단독 특가", 3,
                    "_ZNRxnxj", 5)
        weekly = cpost(EMART, 8, *self.WEEKLY, 12, "_ExjSJG", 30)
        got = fetch_flyers.select_kakao_posts({"items": [weekly, own]}, EMART, {}, now=NOW22)
        self.assertEqual([(p[0]["id"], p[5]["group"]) for p in got], [(7, "단독:11.19"), (8, "11.19")])
        # 그 주 전국 공통 전단을 이미 처리했어도 우리 매장 단독 전단은 읽는다
        self.assertEqual(picks(EMART, [own], {"keys": ["11.19|11.25"], "posts": [8]}), [(7, "new")])

    def test_kims(self):
        d3 = cpost(KIMS, 115243288, "🖤 D-3, 블랙 킴스데이 미리보기", "11.18(수) 시작! 5일간 이어지는 BLACK KIM’S DAY🔥", 8, "_SBXxmxd", 50)
        d2 = cpost(KIMS, 115267907, "🖤 D-2, 킴스 블랙위크 가격 먼저 공개합니다🔥", "11.18(수)-24(화) 한우대전", 9, "_SBXxmxd", 30)
        d1 = cpost(KIMS, 115267945, "🖤 D-1, 가격 대박… 블랙위크 내일부터 시작🔥", "11.18(수) 시작", 10, "_SBXxmxd", 29)
        week = cpost(KIMS, 115268813, "🖤11.18(수)-24(화) 블랙킴스위크", "✅ 진행점포 (26개점) 평촌점", 24, "_SBXxmxd", 28)
        gangseo = cpost(KIMS, 112296854, "💖 강서점 킴스클럽 단독 | 11.20(금)-22(일)", "강서점", 3, "_SBXxmxd", 5)
        self.assertEqual(picks(KIMS, [d3, d2, d1, week, gangseo]), [(115268813, "new")])
        pyeongchon = cpost(KIMS, 3, "💖 평촌점 킴스클럽 단독 | 11.20(금)-22(일)", "평촌점", 3, "_SBXxmxd", 5)
        self.assertEqual(picks(KIMS, [pyeongchon]), [(3, "new")])


class TestRereads(unittest.TestCase):
    def test_edited_post(self):
        # 2026-11-17 킴스클럽 115268813: 주간 전단(24장)을 처리한 뒤 주말장(27장)으로 고쳐 올렸다
        before = cpost(KIMS, 115268813, "🖤11.18(수)-24(화) 블랙킴스위크", "평촌", 24, "_SBXxmxd", 28, tag="a")
        after = cpost(KIMS, 115268813, "🖤11.20(금)-22(일) 킴스블랙위크 주말장", "평촌", 27, "_SBXxmxd", 28, tag="b")
        seen = {"posts": [115268813], "keys": ["11.18|11.24"], "sigs": {"115268813": fetch_flyers.post_sig(before)}}
        self.assertEqual(picks(KIMS, [before], seen), [])
        self.assertEqual(picks(KIMS, [after], seen), [(115268813, "edited")])
        self.assertEqual(picks(KIMS, [after], dict(seen, sigs={})), [])        # 서명이 없는 예전 게시물은 비교하지 않는다

    def test_much_bigger_flyer_later(self):
        seen = {"posts": [1], "keys": ["11.19|11.25"], "read": {"11.19": 3}}
        big = cpost(EMART, 2, "[이마트월배점]고래잇페스타 11/19(목) ~ 11/22(일)", "전단", 10, "_ExjSJG", 5)
        small = cpost(EMART, 3, "[이마트원주점] 이마트 원주점 전단 행사 11/19(목) ~ 11/25(수)", "", 5, "_muxdcxj", 5)
        self.assertEqual(picks(EMART, [big], seen), [(2, "upgrade")])
        self.assertEqual(picks(EMART, [small], seen), [])                    # 5쪽은 3쪽의 두 배가 안 된다
        self.assertEqual(picks(EMART, [big], dict(seen, read={})), [])        # 읽은 쪽수를 모르면 다시 읽지 않는다
        self.assertEqual(picks(dict(EMART, upgrade=False), [big], seen), [])

    def test_not_flyer_keeps_week_open(self):
        promo = cpost(EMART, 7, "고래잇 에코백의 등장!", "11/19(목)~11/22(일) 고래잇 에코백", 3, "_ExjSJG", 30)
        real = cpost(EMART, 8, "[이마트월배점]고래잇페스타 11/19(목) ~ 11/22(일)", "전단", 4, "_ExjSJG", 5)
        self.assertEqual(picks(EMART, [promo]), [(7, "new")])                # 사진 3장 행사 글은 일단 읽어 본다
        self.assertEqual(picks(EMART, [promo, real], {"skip": [7]}), [(8, "new")])
        self.assertEqual(picks(EMART, [promo], {"skip": [7]}), [])


LOCKED = {"type": "image", "width": 568, "height": 320,
          "xlarge_url": f"https://k.kakaocdn.net/dn/{fetch_flyers.LOCK_IMAGE}/img_xl.jpg"}


class TestLoginOnly(unittest.TestCase):
    def locked(self, pid=111977402, hours_ago=5, **extra):
        p = cpost(KIMS, pid, "💖12.31(수)-1.6(화)│물가잡는 킴스클럽🥳", "✅ 진행점포 (25개점) 평촌점", 0, "_SBXxmxd", hours_ago,
                  adult_only=True, **extra)
        p["media"] = [dict(LOCKED)]
        return p

    def test_detect(self):
        p = self.locked()
        self.assertTrue(fetch_flyers.is_restricted(p))
        self.assertTrue(fetch_flyers.is_restricted(dict(p, adult_only=False)))   # 자물쇠 그림만 와도 로그인 글이다
        self.assertFalse(fetch_flyers.is_restricted(cpost(KIMS, 1, "t", "", 3, "_SBXxmxd", 1)))

    def test_notice_once(self):
        p = self.locked()
        data = {"items": [p]}
        self.assertEqual(fetch_flyers.select_kakao_posts(data, KIMS, {}, now=NOW22), [])
        got = fetch_flyers.restricted_posts(data, KIMS, {}, [], now=NOW22)
        self.assertEqual([c["post"]["id"] for c in got], [111977402])
        self.assertEqual(fetch_flyers.restricted_posts(data, KIMS, {}, [111977402], now=NOW22), [])
        self.assertEqual(fetch_flyers.restricted_posts(data, KIMS, {"keys": ["12.31|1.6"]}, [], now=NOW22), [])
        msg = fetch_flyers.restricted_message(KIMS, got[0]["title"], "https://pf.kakao.com/_SBXxmxd/111977402")
        self.assertTrue(msg.startswith("🔒 킴스클럽 평촌점: 로그인해야 볼 수 있는 전단 글이 올라왔습니다"))
        self.assertIn("💖12.31(수)-1.6(화)│물가잡는 킴스클럽🥳", msg)


class TestMarkDone(unittest.TestCase):
    FLYER = {"key": "emart_pyeongchon-8", "store_id": "emart_pyeongchon", "source": "kakao", "post_id": 8,
             "post_ids": [8, 7, 115302369], "date_key": "11.19|11.25", "group": "11.19", "pages": 12, "sig": "abc"}

    def test_flyer(self):
        seen = {"emart_pyeongchon": {"posts": [115302369], "keys": ["11.19|11.25"], "read": {"11.19": 4}}}
        evaluate.mark_done(seen, dict(self.FLYER))
        st = seen["emart_pyeongchon"]
        self.assertEqual(st["posts"], [115302369, 8, 7])                    # 같은 번호를 두 번 적지 않는다
        self.assertEqual(st["read"], {"11.19": 12})
        self.assertEqual(st["sigs"], {"8": "abc"})

    def test_not_flyer(self):
        seen = {}
        evaluate.mark_done(seen, dict(self.FLYER), is_flyer=False)
        st = seen["emart_pyeongchon"]
        self.assertEqual(st.get("skip"), [8])                                 # 고른 글만 건너뛰고
        self.assertNotIn("keys", st)                                          # 그 주는 처리한 것으로 치지 않는다
        self.assertNotIn("posts", st)

    def test_own_dandok_has_no_week_key(self):
        seen = {}
        evaluate.mark_done(seen, dict(self.FLYER, date_key="", group="단독:11.19"))
        self.assertNotIn("keys", seen["emart_pyeongchon"])

    def test_main_skips_not_flyer(self):
        with tempfile.TemporaryDirectory() as d:
            work, state = os.path.join(d, "work"), os.path.join(d, "state")
            os.makedirs(work)
            flyer = dict(self.FLYER, store_name="이마트 평촌점", title="고래잇 에코백의 등장!", period="t", link="L",
                         images=[], note="", reread="")
            json.dump({"flyers": [flyer]}, open(os.path.join(work, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False)
            out = os.path.join(d, "out.json")
            json.dump({"flyers": [{"key": flyer["key"], "is_flyer": False, "items": []}]}, open(out, "w", encoding="utf-8"))
            evaluate.WORK, evaluate.STATE = work, state
            sys.argv = ["evaluate.py", "--input", out]
            evaluate.main()
            self.assertEqual(json.load(open(os.path.join(work, "messages.json"), encoding="utf-8")), [])
            self.assertEqual(json.load(open(os.path.join(work, "not_flyers.json"), encoding="utf-8")), [flyer["key"]])
            seen = json.load(open(os.path.join(state, "seen.json"), encoding="utf-8"))
            self.assertEqual(seen["emart_pyeongchon"]["skip"], [8])

    def test_reread_line(self):
        flyer = {"store_name": "이마트 평촌점", "period": "기간", "link": "L", "reread": fetch_flyers.REREAD["upgrade"]}
        self.assertIn("※ 같은 주 전단을 이미 보냈지만", evaluate.build_message(flyer, [], [], []))


if __name__ == "__main__":
    unittest.main()
