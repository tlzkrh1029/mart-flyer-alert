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


def kpost(pid, title, body, n, ch, hours_ago):
    return {"id": pid, "title": title, "contents": [{"t": "text", "v": body}],
            "media": [{"type": "image", "xlarge_url": f"{pid}_{i}"} for i in range(n)],
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
        self.assertEqual([c["name"] for c in fetch_flyers.store_channels(self.EMART)],
                         ["이마트 월배점", "이마트 천안터미널점", "이마트 원주점", "이마트 울산점"])
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
        three = kpost(2, "[이마트월배점] 미리 준비하는 김장!! 11/5(목) ~ 11/11(수) 전단", "김장", 3, "_ExjSJG", 10)
        no_word = kpost(3, "[이마트월배점] 이마트 PICK! 경동나비엔 온수매트!", "🗓행시기간 : 11/19(목) ~ 11/25(수)", 4, "_ExjSJG", 10)
        self.assertEqual(self.pick([three, no_word]), [])     # 사진 3장, '전단'·'이번 주 이마트 행사' 없음

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


if __name__ == "__main__":
    unittest.main()
