# Steam 武器箱首次报价采集

Python 3.10+，无需安装 Python 第三方包。选择器目录共 **42 种 CS2 武器箱**，排除了贴纸胶囊、纪念包和终端机。程序匿名读取 Steam 人民币买卖盘口，并保存 JSON、CSV 和原始响应。

## 直接使用

Windows 先双击 **`01-probe.cmd`**，成功后双击 **`02-fetch-all.cmd`**。如果 `python` 未在 PATH 中，请在命令行使用 Python 的完整路径。

也可在本目录运行：

```powershell
# 先试采 3 种：裂空、变革、梦魇；最多 1 批
python steam_cases.py probe --max-batches 1

# 试采通过后，采集剩余武器箱；跳过已有成功记录
python steam_cases.py fetch
```

也可以直接执行 `fetch`：它会检查试采门禁，未通过时先试采，通过后才进入后续批次。门禁对应同一目录且有效期为 24 小时。试采和全量阶段共享当次运行的批次预算。

默认配置：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--probe-count` | `3` | 试采数量，须全部取得有效人民币最低卖价 |
| `--batch-size` | `5` | 每批最多 5 个饰品 |
| `--max-batches` | `10` | 本轮最多 10 批，包含本轮试采批次 |
| `--request-interval` | `5` | 上次请求结束后，至少等待 5 秒才发下一次 |
| `--batch-interval` | `20` | 相邻批次至少间隔 20 秒 |
| `--timeout` | `25` | 单次请求超时秒数 |
| `--retries` | `1` | 网络错误或 HTTP 5xx 最多重试一次，先等 30 秒 |
| `--output` | `data` | 数据和续传状态目录 |

以上为程序的保守默认配置，不是 Steam 承诺的官方配额。请求与批间隔都不能小于 1 秒。所有请求（包括重试）均经过同一个限速器。

需要更严格地限制一次运行的请求量时：

```powershell
# 每次最多 2 批 × 4 个；单次网络失败也不重试
python steam_cases.py fetch --batch-size 4 --max-batches 2 --request-interval 8 --batch-interval 30 --retries 0
```

达到批次上限会正常保存并结束，重复同一命令继续下一批。Ctrl+C 中断后也可继续。同一数据目录不允许两个采集进程同时运行；请只运行一个采集窗口。

## 输出

- `data/quotes.csv`：可用 Excel 打开，包含中文名、最低在售价、最高求购价、数量和采集时间。
- `data/quotes.json`：后续网页可直接使用的结构化报价，价格整数单位为**人民币分**。
- `data/raw/<item_nameid>.json`：每种武器箱的 Steam 原始响应，包括盘口深度。
- `data/progress.json`：试采状态、最近错误和限流冷却信息。
- 若响应校验失败，`data/last_invalid_response.json` 保存最近一次失败响应的截断内容，便于排查。

“采集时间”是程序获取响应的 UTC 时间，**不是 Steam 报价源的更新时间或实际成交时间**。Steam 返回的数据可能存在缓存。最低在售价和最高求购价属于当前挂单报价。

Steam 省略的挂单数量在 JSON 中为 `null`、CSV 中为空，不会填造为 0。

这是首次快照采集程序：已有成功记录会保留，重新运行主要用于补齐缺失条目。需要采集新一轮全部报价时，请指定**新的输出目录**：

```powershell
python steam_cases.py fetch --output data-new-snapshot
```

## 手填平台价计算折扣

```powershell
python steam_cases.py list
python steam_cases.py calc --item "裂空武器箱" --buy-price 2.80
python steam_cases.py calc --item "Fracture Case" --buy-price 2.80 --coefficient 0.86
```

`calc` 使用本地已采集报价，不额外请求 Steam。同时计算最低在售价挂单、最高求购价参考两种比例：

```text
预计到账 = Steam 价格 × 0.86，按人民币分四舍五入
充值比例 = 国内平台实际买入成本 ÷ 预计到账
折扣 = 充值比例 × 10
```

系数 `0.86` 是用户指定的估算值，不等于 Steam 精确手续费算法。CS2 交易接收后的保护期会影响可出售时间；此处当前行情不保证之后的实际成交价格。

## 失败时的行为

- 403 / 429：立即停止，不自动重试；尊重 `Retry-After`，至少冷却 5 分钟，后续运行也会检查冷却。
- 非 JSON、接口 `success` 失败、无法确认人民币币种：停止并冷却 5 分钟，避免错误报价进入全量阶段。
- 网络错误 / 5xx：默认等 30 秒后重试一次，仍失败则停止；可用 `--retries 0` 禁止重试。
- 不进行登录、自动交易、验证码处理或代理轮换。

本机实测 Python `urllib` 的直连会超时，系统 PowerShell 请求可成功。因此 Windows 默认使用 PowerShell 的网络栈，优先 `pwsh`、其次系统 `powershell`；其他系统使用标准库 `urllib`。可用 `--transport urllib` 或 `--transport powershell` 显式选择。不会因 Steam 返回 403 / 429 而切换传输方式。

## 验证与目录来源

离线测试：`python -m unittest -v`，不向 Steam 发请求。

目录使用 `appid=730`、`currency=23`，以商品的 `item_nameid` 查询 [Steam 人民币盘口](https://steamcommunity.com/market/itemordershistogram?country=CN&language=schinese&currency=23&item_nameid=176185874&two_factor=0)。

- 商品名称与 ID 来自 [EricZhu-42/SteamTradingSite-ID-Mapper](https://github.com/EricZhu-42/SteamTradingSite-ID-Mapper)，依 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 使用，做了武器箱子集提取及字段重命名。
- 武器箱分类与 [ByMykel/CSGO-API](https://github.com/ByMykel/CSGO-API) 的 `type == "Case"` 容器列表交叉核验，两个目录的 42 个名称完全一致。
- 包含 `CS:GO Weapon Case 2` 与 `CS:GO Weapon Case 3`。目录为本次核验快照，不宣称未来新增武器箱会自动加入。

`catalog.json` 保存目录来源、采集时间和授权信息；常规采集直接使用本地目录，不反复下载商品目录。

---

## 7. Web 可视化看板与折扣计算器

本项目现已提供现代化 Web 交互界面：
- **服务端口**：默认 `8765`
- **启动命令**：`./start_web.sh` 或 `python3 server.py`
- **核心能力**：
  1. 完整展示 42 款 CS2 武器箱官方高清图标与中英文对照；
  2. 实时展示 Steam 最低在售、求购及在售挂单深度；
  3. 点击任意卡片弹出折扣计算弹窗，输入国内平台买入价，即时计算折算折扣（如 8.14 折）、充值比例及每百元余额成本；
  4. 支持自定义到账系数（默认 0.86）；
  5. 接入 SQLite 数据库（`data/history.db`）持续记录历史盘口与趋势；
  6. 提供一键后台安全采集更新接口。
