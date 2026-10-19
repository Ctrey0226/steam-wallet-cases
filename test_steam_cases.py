import contextlib
import io
import tempfile
import unittest
from urllib.error import HTTPError
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import steam_cases as app


def case(index):
    return {"market_hash_name": f"Test {index} Case", "cn_name": f"测试 {index} 武器箱", "item_nameid": index + 1}


def payload():
    return {"success": 1, "price_prefix": "¥", "price_suffix": "", "lowest_sell_order": "1400",
            "highest_buy_order": "1300", "sell_order_count": "1,234", "buy_order_count": "20"}


class CollectorTests(unittest.TestCase):
    def test_currency_and_empty_order_books(self):
        value = payload()
        self.assertEqual(app.parse_quote(case(0), value, app.now())["sell_order_count"], 1234)
        value["lowest_sell_order"] = None
        self.assertIsNone(app.parse_quote(case(0), value, app.now())["lowest_sell_cents"])
        value.pop("sell_order_count")
        self.assertIsNone(app.parse_quote(case(0), value, app.now())["sell_order_count"])
        value["price_prefix"] = "$"
        with self.assertRaisesRegex(ValueError, "人民币"):
            app.parse_quote(case(0), value, app.now())

    def test_requested_discount_direction(self):
        result = app.estimate(1400, Decimal("10"), Decimal("0.86"))
        self.assertEqual(result["net"], Decimal("12.04"))
        self.assertAlmostEqual(float(result["ratio"]), 0.83056478)

    def test_retry_after_never_retries_immediately(self):
        self.assertEqual(app.retry_delay("900"), 900)
        self.assertEqual(app.retry_delay("5"), 300)
        self.assertEqual(app.retry_delay("nonsense"), 300)

    def test_client_rate_limit_and_invalid_json_stop_without_retry(self):
        item = case(0)
        client = app.SteamClient(5, 25, 1, "urllib")
        response = HTTPError("https://steamcommunity.com", 429, "limit", {"Retry-After": "900"}, None)
        with patch.object(client, "read", side_effect=response) as request:
            with self.assertRaises(app.StopCollection) as result:
                client.get(item)
            self.assertEqual(result.exception.cooldown, 900)
            self.assertEqual(request.call_count, 1)
        client.next_request = 0
        with patch.object(client, "read", return_value=b"<html>Verify</html>") as request:
            with self.assertRaises(app.StopCollection) as result:
                client.get(item)
            self.assertEqual(result.exception.cooldown, 300)
            self.assertEqual(request.call_count, 1)
            self.assertIn("Verify", result.exception.diagnostic["body"])

    def test_client_enforces_request_spacing(self):
        client = app.SteamClient(5, 25, 0, "urllib")
        clock = [0.0]
        starts = []

        def sleep(seconds):
            clock[0] += seconds

        def read(url):
            starts.append(clock[0])
            return app.json.dumps(payload()).encode("utf-8")

        with patch.object(app.time, "monotonic", side_effect=lambda: clock[0]), patch.object(app.time, "sleep", side_effect=sleep), patch.object(client, "read", side_effect=read):
            client.get(case(0))
            client.get(case(1))
        self.assertEqual(starts, [0.0, 5.0])

    def test_failed_probe_blocks_bulk(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = app.parser().parse_args(["fetch", "--output", temporary, "--max-batches", "1"])
            collector = app.Collector(args, [case(i) for i in range(7)])
            responses = []

            def get(item):
                responses.append(item)
                value = payload()
                value["lowest_sell_order"] = None
                return app.parse_quote(item, value, app.now()), value

            collector.client.get = get
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(collector.run(), 1)
            self.assertEqual(len(responses), 3)
            self.assertNotIn("probe", collector.state)

    def test_batch_limit_and_resume_without_duplicate_quotes(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = app.parser().parse_args(["fetch", "--output", temporary, "--max-batches", "1"])
            items = [case(i) for i in range(7)]
            requested = []

            def get(item):
                requested.append(item["market_hash_name"])
                value = payload()
                return app.parse_quote(item, value, app.now()), value

            first = app.Collector(args, items)
            first.client.get = get
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(first.run(), 0)
            self.assertEqual(len(first.quotes), 3)
            self.assertEqual(first.used_batches, 1)
            second = app.Collector(args, items)
            second.client.get = get
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(second.run(), 0)
            self.assertEqual(len(second.quotes), 7)
            self.assertEqual(len(requested), 7)
            self.assertEqual(len(set(requested)), 7)

    def test_rate_limit_keeps_progress_and_enforces_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            catalog = directory / "catalog.json"
            app.atomic_json(catalog, {"items": [case(i) for i in range(7)]})
            calls = []

            def get(client, item):
                calls.append(item)
                if len(calls) == 2:
                    raise app.StopCollection("HTTP 429", 300)
                value = payload()
                return app.parse_quote(item, value, app.now()), value

            arguments = ["fetch", "--catalog", str(catalog), "--output", str(directory / "data")]
            with patch.object(app.SteamClient, "get", get), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(arguments), 2)
                self.assertEqual(app.main(arguments), 2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(app.read_json(directory / "data" / "quotes.json")["quotes"]), 1)
            state = app.read_json(directory / "data" / "progress.json")
            self.assertGreater(datetime.fromisoformat(state["blocked_until"]), datetime.now(timezone.utc))
            self.assertNotIn("probe", state)

    def test_output_lock_prevents_parallel_collectors(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = app.OutputLock(Path(temporary))
            try:
                with self.assertRaisesRegex(ValueError, "已有采集进程"):
                    app.OutputLock(Path(temporary))
            finally:
                first.close()
            app.OutputLock(Path(temporary)).close()


if __name__ == "__main__":
    unittest.main()
