"""Steam CS2 case quotes: probe first, collect in bounded batches, resume locally.

Python 3.10+; standard library only. No login, API key, or trading operations.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
ENDPOINT = "https://steamcommunity.com/market/itemordershistogram"
PROBE_NAMES = ("Fracture Case", "Revolution Case", "Dreams & Nightmares Case")
USER_AGENT = "SteamCaseQuoteCollector/1.0 (personal price research)"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_catalog(path: Path) -> list[dict]:
    catalog = read_json(path)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("items"), list):
        raise ValueError(f"目录文件无效：{path}")
    items = catalog["items"]
    names = set()
    ids = set()
    if not items:
        raise ValueError("武器箱目录为空。")
    for item in items:
        name, item_id = item.get("market_hash_name"), item.get("item_nameid")
        if not name or not item.get("cn_name") or not isinstance(item_id, int) or item_id <= 0:
            raise ValueError(f"目录条目缺少有效名称或 item_nameid：{item}")
        if name in names or item_id in ids:
            raise ValueError(f"目录名称或 ID 重复：{name}")
        names.add(name)
        ids.add(item_id)
    return items


def fingerprint(items: list[dict]) -> str:
    content = json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def number(value, field: str, *, optional: bool = False):
    if value is None or value == "":
        if optional:
            return None
        raise ValueError(f"Steam 响应缺少 {field}")
    text = str(value).replace(",", "").strip()
    if not text.isascii() or not text.isdigit():
        raise ValueError(f"Steam {field} 不是有效非负整数：{value!r}")
    result = int(text)
    return None if optional and result == 0 else result


def parse_quote(item: dict, payload: dict, observed_at: str) -> dict:
    if not isinstance(payload, dict) or payload.get("success") not in (1, True):
        raise ValueError("Steam 响应未成功，停止本轮采集。")
    # Do not interpret USD or another currency's integer amounts as CNY.
    symbol = str(payload.get("price_prefix", "")) + str(payload.get("price_suffix", ""))
    if not any(token in symbol for token in ("¥", "￥", "CNY", "RMB")):
        raise ValueError(f"Steam 未返回可确认的人民币币种标识：{symbol!r}")
    return {
        **item,
        "currency": "CNY",
        "observed_at": observed_at,
        "lowest_sell_cents": number(payload.get("lowest_sell_order"), "lowest_sell_order", optional=True),
        "highest_buy_cents": number(payload.get("highest_buy_order"), "highest_buy_order", optional=True),
        # Steam may omit aggregate quantities; that does not invalidate prices.
        "sell_order_count": (number(payload["sell_order_count"], "sell_order_count")
                             if payload.get("sell_order_count") not in (None, "") else None),
        "buy_order_count": (number(payload["buy_order_count"], "buy_order_count")
                            if payload.get("buy_order_count") not in (None, "") else None),
        "source": ENDPOINT,
    }


def retry_delay(header: str | None, default: int = 300) -> int:
    if header:
        try:
            seconds = int(header)
        except ValueError:
            try:
                date = parsedate_to_datetime(header)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = math.ceil((date - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                return default
        return max(default, seconds)
    return default


class StopCollection(Exception):
    def __init__(self, message: str, cooldown: int = 0, diagnostic=None):
        super().__init__(message)
        self.cooldown = cooldown
        self.diagnostic = diagnostic


class OutputLock:
    """OS lock, automatically released on process exit (including forced exit)."""

    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.stream = (folder / ".collector.lock").open("a+b")
        try:
            self.stream.seek(0, 2)
            if self.stream.tell() == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise ValueError("此数据目录已有采集进程运行，请勿同时运行两个采集窗口。") from error

    def close(self):
        self.stream.close()


class SteamClient:
    def __init__(self, interval: float, timeout: float, retries: int, transport: str = "auto"):
        self.interval = interval
        self.timeout = timeout
        self.retries = retries
        self.next_request = 0.0
        self.transport = ("powershell" if os.name == "nt" else "urllib") if transport == "auto" else transport

    def read(self, url: str) -> bytes:
        if self.transport == "urllib":
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(request, timeout=self.timeout) as response:
                return response.read(2_000_001)
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            raise OSError("未找到 PowerShell，请安装或使用 --transport urllib。")
        # Only a fixed Steam URL plus validated numeric IDs is sent to the helper.
        escaped_url = url.replace("'", "''")
        script = f"""
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {{
    $r = Invoke-WebRequest -UseBasicParsing -Uri '{escaped_url}' -TimeoutSec {math.ceil(self.timeout)} -Headers @{{'User-Agent'='{USER_AGENT}';Accept='application/json'}}
    [pscustomobject]@{{status=[int]$r.StatusCode;body=[string]$r.Content;retry_after=[string]$r.Headers['Retry-After']}} | ConvertTo-Json -Compress
}} catch {{
    $problem = $_
    $status = 0
    $retryAfter = ''
    if ($problem.Exception.Response) {{
        $status = [int]$problem.Exception.Response.StatusCode
        try {{ $retryAfter = [string](@($problem.Exception.Response.Headers.GetValues('Retry-After'))[0]) }} catch {{}}
    }}
    [pscustomobject]@{{status=$status;body='';retry_after=$retryAfter;error=$problem.Exception.Message}} | ConvertTo-Json -Compress
}}
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            result = subprocess.run(
                [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True, timeout=self.timeout + 15,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("PowerShell 请求超时。") from error
        if result.returncode:
            raise OSError("PowerShell 请求助手执行失败：" + result.stderr.decode("utf-8", errors="replace")[:400])
        try:
            value = json.loads(result.stdout.decode("utf-8-sig"))
            status = int(value["status"])
        except (ValueError, KeyError, UnicodeError) as error:
            raise OSError("PowerShell 请求助手返回无效结果。") from error
        if status == 0:
            raise OSError(value.get("error", "网络请求失败"))
        if status != 200:
            raise HTTPError(url, status, value.get("error", "HTTP error"), {"Retry-After": value.get("retry_after", "")}, None)
        return value["body"].encode("utf-8")

    def get(self, item: dict) -> tuple[dict, dict]:
        params = urlencode({
            "country": "CN", "language": "schinese", "currency": 23,
            "item_nameid": item["item_nameid"], "two_factor": 0, "norender": 1,
        })
        url = f"{ENDPOINT}?{params}"
        for attempt in range(self.retries + 1):
            time.sleep(max(0.0, self.next_request - time.monotonic()))
            try:
                body = self.read(url)
                if len(body) > 2_000_000:
                    raise StopCollection("Steam 响应异常过大，停止采集。", 300)
                try:
                    payload = json.loads(body.decode("utf-8-sig"))
                    quote = parse_quote(item, payload, now())
                except (ValueError, UnicodeError) as error:
                    raise StopCollection(f"响应校验失败：{error}", 300, {
                        "observed_at": now(), "market_hash_name": item["market_hash_name"],
                        "body": body[:200_000].decode("utf-8-sig", errors="replace"),
                    }) from error
                return quote, payload
            except HTTPError as error:
                if error.code in (403, 429):
                    delay = retry_delay(error.headers.get("Retry-After"))
                    raise StopCollection(f"Steam HTTP {error.code}，保存进度后停止；不自动重试限流。", delay) from error
                if error.code < 500 or attempt == self.retries:
                    raise StopCollection(f"Steam HTTP {error.code}；已停止，重新运行可继续。") from error
                print(f"  HTTP {error.code}，30 秒后重试一次。", flush=True)
            except (URLError, TimeoutError, OSError) as error:
                if attempt == self.retries:
                    raise StopCollection(f"网络请求失败：{error}；已停止，重新运行可继续。") from error
                print("  网络暂时失败，30 秒后重试一次。", flush=True)
            finally:
                self.next_request = time.monotonic() + self.interval
            self.next_request = max(self.next_request, time.monotonic() + 30)
        raise AssertionError("unreachable")


def money(cents) -> str:
    return "" if cents is None else f"{Decimal(cents) / 100:.2f}"


def estimate(cents: int, buy_price: Decimal, coefficient: Decimal) -> dict:
    net = (Decimal(cents) / 100 * coefficient).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if net <= 0:
        raise ValueError("预计到账小于人民币最小单位，无法计算比例。")
    ratio = buy_price / net
    return {"net": net, "ratio": ratio, "discount": ratio * 10, "cost_per_100": ratio * 100}


class Collector:
    def __init__(self, args, items: list[dict]):
        self.args, self.items = args, items
        self.folder = args.output.resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state = read_json(self.folder / "progress.json", {})
        self.quotes = read_json(self.folder / "quotes.json", {"currency": "CNY", "quotes": {}})["quotes"]
        self.client = SteamClient(args.request_interval, args.timeout, args.retries, args.transport)
        self.used_batches = 0
        self.requested = 0
        self.succeeded = 0
        self.catalog_hash = fingerprint(items)

    def save(self) -> None:
        self.state["updated_at"] = now()
        atomic_json(self.folder / "progress.json", self.state)
        atomic_json(self.folder / "quotes.json", {"currency": "CNY", "updated_at": now(), "quotes": self.quotes})
        with (self.folder / "quotes.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("中文名", "market_hash_name", "item_nameid", "最低在售价_元", "最高求购价_元", "在售数量", "求购数量", "采集时间_UTC"))
            for item in self.items:
                quote = self.quotes.get(item["market_hash_name"])
                if quote:
                    writer.writerow((quote["cn_name"], quote["market_hash_name"], quote["item_nameid"],
                                     money(quote["lowest_sell_cents"]), money(quote["highest_buy_cents"]),
                                     quote["sell_order_count"], quote["buy_order_count"], quote["observed_at"]))

    def collect(self, items: list[dict]) -> list[dict]:
        done = []
        size = self.args.batch_size
        for start in range(0, len(items), size):
            if self.used_batches >= self.args.max_batches:
                print("达到本轮批次上限，保存进度；再次运行可继续。", flush=True)
                break
            if self.used_batches:
                print(f"批间等待 {self.args.batch_interval:g} 秒……", flush=True)
                time.sleep(self.args.batch_interval)
            batch = items[start:start + size]
            self.used_batches += 1
            print(f"批次 {self.used_batches}/{self.args.max_batches}，本批 {len(batch)} 个", flush=True)
            for item in batch:
                self.requested += 1
                quote, raw = self.client.get(item)
                self.quotes[item["market_hash_name"]] = quote
                atomic_json(self.folder / "raw" / f"{item['item_nameid']}.json", {
                    "observed_at": quote["observed_at"], "market_hash_name": item["market_hash_name"], "response": raw,
                })
                self.succeeded += 1
                self.save()
                done.append(quote)
                print(f"  OK {item['cn_name']} | 在售 ¥{money(quote['lowest_sell_cents']) or '无报价'}"
                      f" | 求购 ¥{money(quote['highest_buy_cents']) or '无报价'}", flush=True)
        return done

    def gate_is_current(self) -> bool:
        probe = self.state.get("probe", {})
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(probe["passed_at"])
            return probe.get("catalog_hash") == self.catalog_hash and timedelta(0) <= age <= timedelta(hours=24)
        except (KeyError, ValueError, TypeError):
            return False

    def probe(self) -> bool:
        preferred = sorted(self.items, key=lambda item: (
            PROBE_NAMES.index(item["market_hash_name"]) if item["market_hash_name"] in PROBE_NAMES else len(PROBE_NAMES),
            item["market_hash_name"],
        ))[:min(self.args.probe_count, len(self.items))]
        # Invalidate the previous gate before a new test, including interrupted tests.
        self.state.pop("probe", None)
        self.save()
        print(f"先试采 {len(preferred)} 种武器箱；全部通过后才允许全量采集。", flush=True)
        quotes = self.collect(preferred)
        if len(quotes) != len(preferred) or any(quote["lowest_sell_cents"] is None for quote in quotes):
            print("试采未完整通过，本轮不进行全量采集。", flush=True)
            return False
        self.state["probe"] = {"passed_at": now(), "catalog_hash": self.catalog_hash,
                               "items": [quote["market_hash_name"] for quote in quotes]}
        self.save()
        print("试采通过：响应成功、人民币币种和最低在售价均有效。", flush=True)
        return True

    def run(self) -> int:
        blocked_until = self.state.get("blocked_until")
        if blocked_until and datetime.fromisoformat(blocked_until) > datetime.now(timezone.utc):
            print(f"上次响应要求冷却，暂不发送请求。下次可尝试时间（UTC）：{blocked_until}", flush=True)
            return 2
        self.state.pop("blocked_until", None)
        if self.args.command == "probe":
            return 0 if self.probe() else 1
        if not self.gate_is_current() and not self.probe():
            return 1
        force = getattr(self.args, "force", False)
        pending = self.items if force else [item for item in self.items if not self.has_quote(item)]
        print(f"目录 {len(self.items)} 种，待采集 {len(pending)} 种（强制刷新: {force}）。", flush=True)
        self.collect(pending)
        return 0

    def has_quote(self, item: dict) -> bool:
        quote = self.quotes.get(item["market_hash_name"], {})
        return quote.get("item_nameid") == item["item_nameid"] and quote.get("currency") == "CNY"


def positive_decimal(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("请输入有效数字。") from error
    if not result.is_finite() or result <= 0:
        raise argparse.ArgumentTypeError("数字必须大于 0 且为有限数。")
    return result


def positive_int(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("必须为正整数。")
    return result


def nonnegative_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("必须为非负有限数。")
    return result


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Steam 武器箱报价采集：先试采、分批限速、保存进度。")
    commands = command.add_subparsers(dest="command", required=True)
    for name in ("list", "probe", "fetch", "calc"):
        sub = commands.add_parser(name)
        sub.add_argument("--catalog", type=Path, default=ROOT / "catalog.json")
        sub.add_argument("--output", type=Path, default=ROOT / "data")
        if name in ("probe", "fetch"):
            sub.add_argument("--probe-count", type=positive_int, default=3)
            sub.add_argument("--batch-size", type=positive_int, default=5)
            sub.add_argument("--max-batches", type=positive_int, default=10)
            sub.add_argument("--request-interval", type=nonnegative_float, default=5)
            sub.add_argument("--batch-interval", type=nonnegative_float, default=20)
            sub.add_argument("--timeout", type=nonnegative_float, default=25)
            sub.add_argument("--retries", type=int, choices=(0, 1), default=1)
            sub.add_argument("--force", action="store_true", help="强制重新采集所有商品，覆盖旧报价。")
            sub.add_argument("--transport", choices=("auto", "powershell", "urllib"), default="auto",
                             help="auto 在 Windows 使用系统 PowerShell，其他系统使用 urllib。")
        if name == "calc":
            sub.add_argument("--item", required=True, help="中文名或 market_hash_name，可使用唯一匹配的部分名称。")
            sub.add_argument("--buy-price", required=True, type=positive_decimal)
            sub.add_argument("--coefficient", type=positive_decimal, default=Decimal("0.86"))
    return command


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    collector = None
    output_lock = None
    try:
        items = load_catalog(args.catalog)
        if args.command == "list":
            for item in items:
                print(f"{item['cn_name']} | {item['market_hash_name']} | {item['item_nameid']}")
            print(f"共 {len(items)} 种武器箱。")
            return 0
        if args.command == "calc":
            if args.coefficient > 1:
                raise ValueError("到账系数应不大于 1。")
            exact = [item for item in items if args.item in (item["cn_name"], item["market_hash_name"])]
            found = exact or [item for item in items if args.item.casefold() in (item["cn_name"] + item["market_hash_name"]).casefold()]
            if len(found) != 1:
                raise ValueError(f"饰品匹配到 {len(found)} 项，请使用完整名称或先运行 list。")
            item = found[0]
            quote = read_json(args.output / "quotes.json", {"quotes": {}})["quotes"].get(item["market_hash_name"])
            if not quote:
                raise ValueError("该饰品尚无报价，请先执行 probe 或 fetch。")
            print(f"{item['cn_name']} | 平台买入 ¥{args.buy_price:.2f} | 系数 {args.coefficient}")
            print(f"报价采集时间（UTC）：{quote['observed_at']}，此命令不刷新行情。")
            for label, field in (("最低在售价挂单", "lowest_sell_cents"), ("最高求购价参考", "highest_buy_cents")):
                cents = quote[field]
                if cents is None:
                    print(f"{label}：无报价")
                    continue
                result = estimate(cents, args.buy_price, args.coefficient)
                print(f"{label}：Steam ¥{money(cents)} → 预计到账 ¥{result['net']:.2f}"
                      f" | 比例 {result['ratio']:.4f}（{result['discount']:.2f} 折）"
                      f" | 每 ¥100 余额成本 ¥{result['cost_per_100']:.2f}")
            print("0.86 为估算系数；当前报价不保证冷却结束后的成交价格。")
            return 0
        if args.request_interval < 1 or args.batch_interval < 1 or args.timeout < 1:
            raise ValueError("请求间隔、批间隔和超时均须至少 1 秒。")
        output_lock = OutputLock(args.output.resolve())
        collector = Collector(args, items)
        print(f"请求间隔 {args.request_interval:g}s；每批 {args.batch_size} 个；批间隔 {args.batch_interval:g}s；"
              f"本轮最多 {args.max_batches} 批。", flush=True)
        result = collector.run()
        collector.state["last_run"] = {"finished_at": now(), "command": args.command,
                                       "batches": collector.used_batches, "requested_items": collector.requested,
                                       "succeeded_items": collector.succeeded, "exit_code": result}
        collector.save()
        print(f"本轮成功 {collector.succeeded} 个；目录已保存 {sum(collector.has_quote(item) for item in items)}/{len(items)}。", flush=True)
        print(f"数据目录：{collector.folder}", flush=True)
        return result
    except StopCollection as error:
        if collector:
            collector.state.pop("probe", None)
            if error.cooldown:
                collector.state["blocked_until"] = (datetime.now(timezone.utc) + timedelta(seconds=error.cooldown)).isoformat(timespec="seconds")
            collector.state["last_error"] = {"at": now(), "message": str(error)}
            if error.diagnostic is not None:
                atomic_json(collector.folder / "last_invalid_response.json", error.diagnostic)
            collector.save()
        print(str(error), file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        if collector:
            collector.save()
        print("\n已中断，成功记录已保存。", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    finally:
        if output_lock:
            output_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
