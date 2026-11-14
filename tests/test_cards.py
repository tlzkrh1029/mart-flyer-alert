"""카드 데이터 정리(cards/cardlib.py)와 앨범 발송 준비 시험. 크롬 없이 돈다."""
import datetime as dt
import glob
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "cards"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import cardlib as C  # noqa: E402
import notify_telegram  # noqa: E402

TODAY = dt.date(2026, 11, 14)
CRIT = json.load(open(os.path.join(ROOT, "config", "criteria.json"), encoding="utf-8"))
STORES = {s["id"]: s for s in json.load(open(os.path.join(ROOT, "config", "stores.json"), encoding="utf-8"))["stores"]}


def history(store_id):
    f = glob.glob(os.path.join(HERE, "fixtures", "history", f"*_{store_id}-*.json"))[0]
    d = json.load(open(f, encoding="utf-8"))
    return d["flyer"], d["items"]


def ctx(store_id):
    flyer, items = history(store_id)
    return C.store_context(flyer, items, CRIT, STORES[store_id], today=TODAY)


class TestText(unittest.TestCase):
    def test_period(self):
        self.assertEqual(C.period_text("[이마트월배점]금주 전단행사 안내(11/12(목) ~ 11/18(수)", TODAY),
                         "11월 12일(목)부터 18일(수)까지")
        self.assertEqual(C.period_text("[11.13(금)-15(일), 3일간] 킴스클럽 주말장", TODAY), "11월 13일(금)부터 15일(일)까지")
        self.assertEqual(C.until_text("11/12~11/15", TODAY), "11월 15일(일)까지")

    def test_year_near_today(self):
        # 1월에 보는 12월 전단은 지난해 날짜로 요일을 붙인다
        self.assertEqual(C.date_of(12, 31, dt.date(2027, 1, 2)), dt.date(2026, 12, 31))

    def test_josa(self):
        self.assertEqual(C.josa("롯데마트"), "롯데마트가")
        self.assertEqual(C.josa("킴스클럽"), "킴스클럽이")
        self.assertEqual(C.josa("전단 원본 2장", "은", "는"), "전단 원본 2장은")

    def test_names(self):
        it = {"name": "호주 냉장 앞다리 국거리", "origin": "호주산", "storage": "냉장", "spec": "100g"}
        self.assertEqual(C.label_name(it), "앞다리 국거리")
        sq = {"name": "선동 오징어", "origin": "원양산", "storage": "냉동", "spec": "중", "count": 5, "count_unit": "마리"}
        self.assertEqual(C.label_name(sq), "선동 오징어(중)")
        self.assertEqual(C.qty_text(sq), "5마리")
        self.assertEqual(C.qty_text({"spec": "4-7입봉"}), "4~7개 1봉")

    def test_chunk_where(self):
        self.assertEqual(C.chunk_where("/w/03_b.jpg", ["03_a.jpg", "03_b.jpg"]), "3쪽 아래")
        self.assertEqual(C.chunk_where("/w/02L_a.jpg", ["02L_a.jpg", "02L_b.jpg", "02R_a.jpg"]), "2쪽 왼쪽 위")
        self.assertEqual(C.chunk_where("/w/05_b.jpg", ["05_a.jpg", "05_b.jpg", "05_c.jpg"]), "5쪽 가운데")
        self.assertEqual(C.chunk_where("/w/01_a.jpg", ["01_a.jpg"]), "1쪽")


class TestStore(unittest.TestCase):
    def test_emart(self):
        c = ctx("emart_pyeongchon")
        self.assertEqual(len(c["all"]), 6)
        self.assertEqual([h["label_name"] for h in c["heroes"]], ["프라임 척아이롤", "앞다리 국거리", "자연촌 고소한두부"])
        chuck = c["heroes"][0]
        self.assertEqual((chuck["price_num"], chuck["regular_txt"], chuck["sticker"]["big"]), ("2,988", "4,980원", "40%"))
        self.assertEqual(chuck["v"]["crit"], "기준: 100g당 3,000원 이하")
        self.assertEqual(c["bogos"]["count"], 13)
        self.assertEqual(c["bogos"]["title_deal"], "1+1·2+1")

    def test_lotte_merge(self):
        c = ctx("lotte_uiwang")
        self.assertEqual(c["heroes"][0]["label_name"], "보쌈용 돼지고기 삼겹살·목심")
        self.assertEqual(len(c["all"]), 5)

    def test_kims_group(self):
        c = ctx("kims_pyeongchon")
        groups = [r for r in c["rows"] if r.get("group")]
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0]["name"].startswith("9개 품목 50%"))
        self.assertIsNone(c["bogos"])

    def test_caption(self):
        c = ctx("emart_pyeongchon")
        cap = C.caption(c, 2, 5)
        self.assertIn("⭐ 프라임 척아이롤 100g 2,988원 (11/15까지)", cap)
        self.assertTrue(cap.endswith("전단 원본 2장은 사진 5장에 있습니다."))
        self.assertLess(len(cap), 1024)

    def test_crop_pages_need_star_and_file(self):
        c = ctx("emart_pyeongchon")
        with tempfile.TemporaryDirectory() as d:
            for it in c["all"]:
                name = os.path.basename(it.get("img") or "x.jpg")
                it["img"] = os.path.join(d, name)
            for n in ("02_a", "02_b", "03_a", "03_b", "06_a", "06_b"):
                open(os.path.join(d, n + ".jpg"), "wb").close()
            pages = C.crop_pages(c["all"])
        # 06_b 에는 추천(✓)만 있어서 빠진다
        self.assertEqual([p["where"] for p in pages], ["2쪽 위", "3쪽 아래"])
        self.assertEqual([x["label_name"] for x in pages[1]["items"]][0], "프라임 척아이롤")


class TestCompare(unittest.TestCase):
    def test_compare(self):
        cfg = json.load(open(os.path.join(ROOT, "config", "compare.json"), encoding="utf-8"))
        stores = []
        for s in cfg["stores"]:
            flyer, items = history(s["id"])
            stores.append((s["id"], s["name"], flyer, items))
        cc = C.compare_context(stores, cfg, TODAY, cfg["max_rows"])
        self.assertEqual([r["canon"] for r in cc["rows"]],
                         ["국산 삼겹살·목심", "국산 앞다리", "닭고기", "오징어", "사과", "감귤", "양파", "쌀"])
        wins = {c["name"]: c["wins"] for c in cc["cols"]}
        self.assertEqual(wins, {"이마트": 1, "롯데마트": 6, "킴스클럽": 1})
        self.assertEqual(cc["headline"], "롯데마트가 8개 중 6개 품목에서 가장 쌉니다.")
        pork = cc["rows"][0]["cells"]
        self.assertIsNone(pork[0])                       # 이마트 전단에는 국산 삼겹살이 없다
        self.assertEqual((pork[1]["price"], pork[2]["price"]), ("1,650원", "2,390원"))   # 냉동 대패·캐나다산은 빠진다


class TestNotify(unittest.TestCase):
    def test_flyer_end(self):
        self.assertEqual(C.flyer_end({"title": "[11.13(금)-15(일), 3일간] 킴스클럽 주말장"}, TODAY), dt.date(2026, 11, 15))
        self.assertEqual(C.flyer_end({"title": "11/12(목) ~ 11/18(수) 금주 행사 안내"}, TODAY), dt.date(2026, 11, 18))
        self.assertEqual(C.flyer_end({"title": "12/28(월) ~ 1/3(일)"}, dt.date(2027, 1, 2)), dt.date(2027, 1, 3))
        self.assertIsNone(C.flyer_end({"title": "주말 특가"}, TODAY))

    def test_compare_skips_ended_flyers(self):
        import render
        with tempfile.TemporaryDirectory() as d:
            month = os.path.join(d, "history", "2026-11")
            os.makedirs(month)
            for name, title in (("20261112_kims_pyeongchon-1.json", "[11.12(목)-18(수)] 주간 행사"),
                                ("20261114_kims_pyeongchon-2.json", "[11.13(금)-15(일), 3일간] 킴스클럽 주말장")):
                json.dump({"flyer": {"title": title}, "items": []}, open(os.path.join(month, name), "w", encoding="utf-8"))
            old, render.STATE = render.STATE, d
            try:
                pick = lambda day: (render.latest_history("kims_pyeongchon", day) or {}).get("flyer", {}).get("title")
                self.assertIn("주말장", pick(dt.date(2026, 11, 14)))           # 둘 다 진행 중이면 최근 것
                self.assertIn("주간 행사", pick(dt.date(2026, 11, 17)))        # 주말 전단이 끝나면 아직 진행 중인 것
                self.assertIsNone(pick(dt.date(2026, 11, 19)))                  # 모두 끝났으면 비교에서 뺀다
            finally:
                render.STATE = old

    def test_multipart(self):
        body, ctype = notify_telegram.multipart({"chat_id": "1", "media": "[]"}, [("f0", "01.png", b"PNG", "image/png")])
        boundary = ctype.split("boundary=")[1]
        self.assertIn(f"--{boundary}".encode(), body)
        self.assertIn(b'name="f0"; filename="01.png"', body)
        self.assertTrue(body.rstrip().endswith(f"--{boundary}--".encode()))


if __name__ == "__main__":
    unittest.main()
