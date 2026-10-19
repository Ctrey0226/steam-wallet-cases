import os
import sys
import json
import sqlite3
import pathlib
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = pathlib.Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
CATALOG_FILE = BASE_DIR / "catalog.json"
QUOTES_FILE = DATA_DIR / "quotes.json"
DB_PATH = DATA_DIR / "history.db"
STATIC_DIR = BASE_DIR / "web" / "static"

# 定时器与采集状态
SCHEDULER_CONFIG = {
    "enabled": True,
    "interval_seconds": 3600, # 默认每小时自动刷新采集一次
    "last_run": None,
    "next_run": None
}

COLLECT_STATE = {
    "is_collecting": False,
    "progress": "",
    "last_started": None,
    "last_finished": None,
    "last_error": None
}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

async def run_collector_task():
    """在后台运行 Steam 报价刷新，并写入 SQLite"""
    global COLLECT_STATE
    if COLLECT_STATE["is_collecting"]:
        return
    COLLECT_STATE["is_collecting"] = True
    COLLECT_STATE["progress"] = "正在启动采集..."
    COLLECT_STATE["last_started"] = datetime.now().isoformat()
    COLLECT_STATE["last_error"] = None
    
    try:
        cmd = [sys.executable, str(BASE_DIR / "steam_cases.py"), "fetch-all", "--batch-size", "5", "--request-interval", "5"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode == 0:
            COLLECT_STATE["progress"] = "采集完成，正在同步数据库..."
            if QUOTES_FILE.exists():
                with open(QUOTES_FILE, "r", encoding="utf-8") as f:
                    q_data = json.load(f).get("quotes", {})
                conn = get_db()
                c = conn.cursor()
                for name, q in q_data.items():
                    c.execute("SELECT id FROM quote_history WHERE market_hash_name=? AND observed_at=?", (name, q["observed_at"]))
                    if not c.fetchone():
                        c.execute("""
                            INSERT INTO quote_history (
                                market_hash_name, cn_name, item_nameid,
                                lowest_sell_cents, highest_buy_cents,
                                sell_order_count, buy_order_count, observed_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            name, q.get("cn_name"), q.get("item_nameid"),
                            q.get("lowest_sell_cents"), q.get("highest_buy_cents"),
                            q.get("sell_order_count"), q.get("buy_order_count"),
                            q.get("observed_at")
                        ))
                conn.commit()
                conn.close()
            COLLECT_STATE["progress"] = "采集与同步成功"
            COLLECT_STATE["last_finished"] = datetime.now().isoformat()
            SCHEDULER_CONFIG["last_run"] = datetime.now().isoformat()
        else:
            err = stderr.decode(errors="replace")
            COLLECT_STATE["last_error"] = f"采集进程退出码: {proc.returncode}, 错误: {err[:200]}"
            COLLECT_STATE["progress"] = "采集失败"
    except Exception as e:
        COLLECT_STATE["last_error"] = str(e)
        COLLECT_STATE["progress"] = f"采集异常: {e}"
    finally:
        COLLECT_STATE["is_collecting"] = False

async def background_scheduler_worker():
    """后台常驻定时调度协程"""
    while True:
        try:
            if SCHEDULER_CONFIG["enabled"]:
                interval = SCHEDULER_CONFIG["interval_seconds"]
                SCHEDULER_CONFIG["next_run"] = (datetime.now() + timedelta(seconds=interval)).isoformat()
                await asyncio.sleep(interval)
                if SCHEDULER_CONFIG["enabled"]:
                    await run_collector_task()
            else:
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            break
        except Exception as e:
            await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(background_scheduler_worker())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="Steam 武器箱折扣计算与行情看板", version="1.2.0", lifespan=lifespan)

# 挂载静态资源
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

def calculate_discount(cents: int, buy_price: Decimal, coefficient: Decimal = Decimal("0.86")) -> dict:
    if cents <= 0:
        return {"error": "标价必须大于0"}
    steam_price = Decimal(cents) / 100
    net = (steam_price * coefficient).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if net <= 0:
        return {"error": "预计到手小于0.01元"}
    ratio = (buy_price / net).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    discount = (ratio * 10).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cost_per_100 = (ratio * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "steam_price": float(steam_price),
        "coefficient": float(coefficient),
        "net_income": float(net),
        "buy_price": float(buy_price),
        "ratio": float(ratio),
        "discount": float(discount),
        "cost_per_100": float(cost_per_100)
    }

@app.get("/api/cases")
def list_cases():
    """获取所有 42 个武器箱的最新行情数据"""
    if not CATALOG_FILE.exists():
        raise HTTPException(status_code=500, detail="catalog.json 不存在")
    
    with open(CATALOG_FILE, "r", encoding="utf-8") as f:
        catalog = json.load(f).get("items", [])
    
    quotes = {}
    if QUOTES_FILE.exists():
        with open(QUOTES_FILE, "r", encoding="utf-8") as f:
            quotes = json.load(f).get("quotes", {})
            
    result = []
    for item in catalog:
        m_name = item["market_hash_name"]
        q = quotes.get(m_name, {})
        safe_name = m_name.replace(":", "_").replace(" ", "_") + ".png"
        img_path = f"/static/images/{safe_name}"
        
        lowest_sell_cents = q.get("lowest_sell_cents")
        highest_buy_cents = q.get("highest_buy_cents")
        
        est_net = None
        if lowest_sell_cents:
            est_net = float((Decimal(lowest_sell_cents) / 100 * Decimal("0.86")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            
        result.append({
            "market_hash_name": m_name,
            "cn_name": item.get("cn_name") or q.get("cn_name"),
            "item_nameid": item.get("item_nameid"),
            "image": img_path,
            "lowest_sell_cents": lowest_sell_cents,
            "lowest_sell_price": float(Decimal(lowest_sell_cents)/100) if lowest_sell_cents else None,
            "highest_buy_cents": highest_buy_cents,
            "highest_buy_price": float(Decimal(highest_buy_cents)/100) if highest_buy_cents else None,
            "sell_order_count": q.get("sell_order_count"),
            "buy_order_count": q.get("buy_order_count"),
            "est_net_income": est_net,
            "observed_at": q.get("observed_at")
        })
    
    return {
        "total": len(result),
        "updated_at": datetime.now().isoformat(),
        "is_collecting": COLLECT_STATE["is_collecting"],
        "scheduler": SCHEDULER_CONFIG,
        "cases": result
    }

@app.get("/api/calculate")
def calc_discount(
    lowest_cents: int = Query(..., description="Steam最低在售价(分)"),
    buy_price: float = Query(..., description="国内买入价(元)"),
    coefficient: float = Query(0.86, description="到手系数")
):
    try:
        res = calculate_discount(lowest_cents, Decimal(str(buy_price)), Decimal(str(coefficient)))
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/history/{market_hash_name}")
def get_case_history(
    market_hash_name: str,
    view: str = Query("hour", description="hour | day | month")
):
    """获取指定武器箱的历史走势数据，并在后端进行曲线压缩和聚合"""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT lowest_sell_cents, highest_buy_cents, sell_order_count, buy_order_count, observed_at
        FROM quote_history
        WHERE market_hash_name = ?
        ORDER BY observed_at ASC
    """, (market_hash_name,))
    rows = c.fetchall()
    conn.close()
    
    raw = []
    for r in rows:
        raw.append({
            "lowest_sell_price": float(Decimal(r["lowest_sell_cents"])/100) if r["lowest_sell_cents"] else None,
            "highest_buy_price": float(Decimal(r["highest_buy_cents"])/100) if r["highest_buy_cents"] else None,
            "sell_order_count": r["sell_order_count"],
            "buy_order_count": r["buy_order_count"],
            "observed_at": r["observed_at"]
        })
    
    # 根据视图（小时/天/月）进行时间聚合与曲线压缩
    compressed = []
    if view == "day":
        day_buckets = {}
        for item in raw:
            dt_str = item["observed_at"][:10] # YYYY-MM-DD
            day_buckets[dt_str] = item
        compressed = list(day_buckets.values())
    elif view == "month":
        month_buckets = {}
        for item in raw:
            dt_str = item["observed_at"][:7] # YYYY-MM
            month_buckets[dt_str] = item
        compressed = list(month_buckets.values())
    else:
        hour_buckets = {}
        for item in raw:
            dt_str = item["observed_at"][:13] # YYYY-MM-DDTHH
            hour_buckets[dt_str] = item
        compressed = list(hour_buckets.values())

    return {
        "market_hash_name": market_hash_name,
        "view": view,
        "total_raw": len(raw),
        "count": len(compressed),
        "history": compressed
    }

@app.post("/api/refresh")
def trigger_refresh(background_tasks: BackgroundTasks):
    if COLLECT_STATE["is_collecting"]:
        return {"status": "busy", "message": "当前已有采集任务在运行中", "state": COLLECT_STATE}
    background_tasks.add_task(run_collector_task)
    return {"status": "started", "message": "已在后台启动行情采集任务", "state": COLLECT_STATE}

@app.get("/api/refresh/status")
def refresh_status():
    return {
        "collector": COLLECT_STATE,
        "scheduler": SCHEDULER_CONFIG
    }

@app.get("/", response_class=HTMLResponse)
@app.head("/", response_class=HTMLResponse)
def index_page():
    html_file = BASE_DIR / "web" / "index.html"
    if not html_file.exists():
        return HTMLResponse("<h1>Web 前端尚未构建</h1>", status_code=404)
    with open(html_file, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8765, reload=False)
