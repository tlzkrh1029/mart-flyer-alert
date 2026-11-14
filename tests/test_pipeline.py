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


if __name__ == "__main__":
    unittest.main()
