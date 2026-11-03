import unittest
import sqlite3
import pathlib
import json
from decimal import Decimal
from fastapi.testclient import TestClient

from server import app, DB_PATH, CATALOG_FILE, QUOTES_FILE, calculate_discount

client = TestClient(app)

class TestSteamCasesWeb(unittest.TestCase):
    def test_calculate_discount(self):
        # 标价 10.00 元 (1000分)，到手系数 0.86，国内买入 7.00 元
        res = calculate_discount(1000, Decimal("7.00"), Decimal("0.86"))
        self.assertEqual(res["steam_price"], 10.0)
        self.assertEqual(res["net_income"], 8.6)
        self.assertEqual(res["buy_price"], 7.0)
        self.assertAlmostEqual(res["discount"], 8.14, places=2)
        self.assertAlmostEqual(res["cost_per_100"], 81.40, places=2)

    def test_api_cases(self):
        resp = client.get("/api/cases")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 42)
        self.assertEqual(len(data["cases"]), 42)
        # 检查图片路径
        first = data["cases"][0]
        self.assertTrue(first["image"].startswith("/static/images/"))
        self.assertIn(".png", first["image"])

    def test_api_calculate(self):
        resp = client.get("/api/calculate?lowest_cents=364&buy_price=2.50&coefficient=0.86")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["steam_price"], 3.64)
        self.assertTrue("discount" in data)

    def test_api_history(self):
        resp = client.get("/api/history/Fracture%20Case")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["market_hash_name"], "Fracture Case")
        self.assertGreaterEqual(data["count"], 1)

    def test_index_page(self):
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("CS2 武器箱行情 | 采购折扣看板", resp.text)
        self.assertIn('aria-labelledby="inventoryTitle"', resp.text)
        self.assertIn('role="dialog"', resp.text)
        self.assertIn("calcModal", resp.text)
        self.assertIn('href="./inventory"', resp.text)
        self.assertIn('href="./account"', resp.text)
        self.assertIn("tactical.css", resp.text)
        self.assertEqual(client.get("/static/tactical.css").status_code, 200)

    def test_personal_account_pages_are_served_without_inventory_data(self):
        for path in ("/account", "/inventory"):
            resp = client.get(path)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("个人库存管理", resp.text)
            self.assertIn("通过 Steam 登录并绑定", resp.text)
            self.assertIn("account.js", resp.text)
            self.assertIn("no-store", resp.headers.get("cache-control", ""))
        self.assertEqual(client.get("/static/account.js").status_code, 200)
        self.assertEqual(client.get("/static/account.css").status_code, 200)

    def test_account_api_is_integrated_and_inventory_requires_login(self):
        account = client.get('/api/account')
        self.assertEqual(account.status_code, 200)
        self.assertFalse(account.json()['authenticated'])
        self.assertEqual(client.get('/api/inventory').status_code, 401)

    def test_all_images_exist(self):
        img_dir = pathlib.Path(__file__).parent / "web" / "static" / "images"
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            catalog = json.load(f)["items"]
        for item in catalog:
            safe_name = item["market_hash_name"].replace(":", "_").replace(" ", "_") + ".png"
            img_file = img_dir / safe_name
            self.assertTrue(img_file.exists(), f"Image missing: {img_file}")
            self.assertGreater(img_file.stat().st_size, 1000, f"Image file too small: {img_file}")

if __name__ == "__main__":
    unittest.main()
