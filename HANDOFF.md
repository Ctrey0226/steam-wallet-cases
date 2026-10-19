# Steam 武器箱折扣查询项目交接

交接日期：2026-10-19（Asia/Shanghai）。本项目已经完成 Steam 武器箱目录、首次真实报价采集、命令行折扣计算及验证，网页尚未开发。

## 1. Hermes 本次接收任务

本次授权是**接收项目、核对交接、发送完成汇报，然后等待用户继续开发指令**。

- 阅读本文及 `README.md`，核对程序、目录和现有数据已完整落盘；可做本地文件检查及离线单元测试。
- 完成接收后，由 Hermes 使用其已配置的 QQ 能力，向 **QQ 1468682093** 发送一条交接完成汇报。内容包括服务器实际项目路径、42/42 条快照及采集时间、测试结果、网页未开发、已等待下一步指令。发送失败应如实记录，不能声称已送达。
- 此次接收不启动开发、重新采集行情、安装抓取工具、创建定时任务或部署公网网页。这些工作等待用户后续指令。
- 本文不含登录凭据、Cookie、Token、服务器密码或密钥；接收项目也不需要 Steam 登录信息。

## 2. 用户目标与已经确认的范围

用户希望比较“国内平台实际买入成本”和“在 Steam 卖出后得到的余额”，筛选充值比例较低的 CS2 饰品；最初也希望看两端价格走势，避免热门挂刀商品被炒高后折扣缩水。

需求已收敛为：

1. 目前只读取 Steam 人民币行情，商品范围为 42 种 CS2 武器箱。
2. 国内平台买入价格由用户手填；当前不接入 UU/悠悠有品、网易 BUFF 等国内平台的自动抓取。
3. 使用用户指定的默认到账系数 `0.86` 计算折扣，并保留调整系数的能力。
4. 先少量试采，验证成功后才能分批、限速采集全目录，并能中断续传。
5. 后续要做网页；当前交付为标准库 Python 命令行工具和真实首次快照，没有 Web 服务、前端或数据库。

研究背景：没有验证到一套可直接采用、维护活跃且同时满足国内平台接入、Steam 行情历史和正确充值比例计算的高 star 网页成品。[buff2steam](https://github.com/hldh214/buff2steam) 在调研时约 204 stars，提供 BUFF/Steam 比价和简易 Flask 网页，但只用内存保存当前报价，没有历史趋势，也没有 UU 接入。[uupinfetch](https://github.com/juggperc/uupinfetch) 虽有 FastAPI 页面和 SQLite 骨架，但调研时只有 1 star；源码存在与本需求方向相反的折扣公式、模糊搜索直接取首条商品等问题，且未找到独立 LICENSE 文件，不宜直接作为可靠底座。

国内平台实时接入通常依赖会话 Cookie、Token 或设备信息，接口可用性及访问限制也需另行验证。结合用户的范围调整，本项目不继续国内平台抓取路线；上述项目仅为调研参考，不是当前运行依赖。

其余调研记录（Star 为调研时快照，不保证以后不变）：[SteamTradingSiteTracker](https://github.com/EricZhu-42/SteamTradingSiteTracker) 2,322 stars，最贴近原始产品构想，但作者已公告 2026-05-15 停止维护，公开仓库也不是完整站点；[oddish](https://github.com/puppylpg/oddish) 322 stars，为已归档的 Python 桌面工具；[CheapSteam](https://github.com/YukiCoco/CheapSteam) 226 stars，含网页但已归档且代码较旧；[SteamTradingSite-ID-Mapper](https://github.com/EricZhu-42/SteamTradingSite-ID-Mapper) 153 stars，当前只使用其目录映射数据。

曾调查 [BUFF 官方开发者 API](https://buff.163.com/developer/documentation) 和 [CSQAQ 三方挂刀行情 API](https://s.apifox.cn/apidoc/docs-site/4711104/api-187131823)、[三方历史图表 API](https://s.apifox.cn/apidoc/docs-site/4711104/api-187131781)。用户随后明确决定只用 Steam，国内平台价手填；因此这些接口均未接入、未注册购买或配置 Token，也不要在交接后恢复国内平台抓取。

Steam 匿名人民币盘口已实测可用，原生中文搜索接口虽返回成功但查询/币种参数疑似被忽略，所以当前使用本地中文目录与精确 `item_nameid`，避免模糊搜索首条匹配。匿名 `pricehistory` 本轮返回 HTTP 400，历史走势尚未实现。Steam 官方费用口径为平台 5% 与 CS2 10% 按卖家净额计费并分别取整，大额近似到账为标价除以 1.15；代码目前保留用户指定的 0.86 估算，不实现精确费用模式。

## 3. 文件位置与搬迁清单

本地原始绝对路径：

```text
C:\Users\ctrey\Documents\私有云\steam-case-fetcher
```

本文其余相对路径均以项目根目录为基准。服务器实际落盘路径由接收方确认，并写入完成汇报，不假定沿用 Windows 路径。

| 相对路径 | 用途 |
| --- | --- |
| `HANDOFF.md` | 本次技术与任务边界交接 |
| `README.md` | 操作说明、限速行为和数据来源 |
| `steam_cases.py` | 目录读取、网络请求、采集、续传、导出和折扣计算 |
| `test_steam_cases.py` | 9 项离线单元测试 |
| `catalog.json` | 42 个武器箱的中文名、`market_hash_name`、`item_nameid` 及来源归属 |
| `01-probe.cmd` | Windows 试采入口，最多 1 批 |
| `02-fetch-all.cmd` | Windows 补齐全目录入口 |
| `data/quotes.json` | 当前结构化报价；价格单位为人民币分 |
| `data/quotes.csv` | 带 UTF-8 BOM 的表格；价格单位为人民币元 |
| `data/raw/<item_nameid>.json` | 42 份原始 Steam 响应，含盘口深度及观测时间 |
| `data/progress.json` | 门禁、冷却、最近运行结果和历史错误 |
| `data/collection_summary.json` | 本次 42/42 采集结果与运行参数摘要 |

请整体保留 `data/`。`.gitignore` 忽略了 `data/` 和 `__pycache__/`，仅复制受 Git 跟踪文件会漏掉真实数据。`__pycache__/` 无需搬迁。`data/.collector.lock` 是操作系统文件锁使用的普通文件，其存在不代表当前有采集进程；进程结束后锁会释放。

## 4. 目录来源与许可

- Steam 应用为 `appid=730`，请求货币参数为 `currency=23`，国家 `CN`，语言 `schinese`。
- `catalog.json` 建立于 `2026-10-18T16:26:26.3254331+00:00`，即北京时间 2026-10-19 00:26:26。
- 商品名称和 Steam `item_nameid` 映射来自 [EricZhu-42/SteamTradingSite-ID-Mapper](https://github.com/EricZhu-42/SteamTradingSite-ID-Mapper)，使用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 授权；本项目进行了武器箱子集提取、字段重命名和分类关联。来源归属保留在 `catalog.json` 与 `README.md`，搬迁和后续分发时继续保留。
- 分类使用 [ByMykel/CSGO-API](https://github.com/ByMykel/CSGO-API) 的 `public/api/en/crates.json`，以 `type == "Case"` 交叉核验，两个目录的 42 个名称一致。
- 42 种中包含 `CS:GO Weapon Case 2`、`CS:GO Weapon Case 3`，排除了贴纸胶囊、纪念包和终端机。目录是本次核验快照，不自动发现未来新增商品。
- 当前目录未包含本项目独立的 `LICENSE` 文件；不要把调研参考项目的许可证视为本项目的软件许可证。

## 5. 已有真实数据

交接时重新做了只读核对：目录与报价名称、ID 全部一致；42 份原始响应用当前 `parse_quote` 重放后逐条等于 `quotes.json`；CSV 有 42 行数据。

| 指标 | 实际结果 |
| --- | --- |
| 目录 / 成功报价 / 原始响应文件 | 42 / 42 / 42 |
| 货币 | 全部 CNY |
| 有效最低在售价 / 最高求购价 | 42 / 42 |
| 本批数量字段缺失 | 在售数量 0 条；求购数量 0 条 |
| 最早观测（北京时间） | 2026-10-19 00:34:11 |
| 最晚观测（北京时间） | 2026-10-19 00:40:52 |
| 对应 UTC 区间 | 2026-10-18 16:34:11—16:40:52 |
| 最终运行结果 | 9 批；42 个请求条目；42 个成功；退出码 0 |
| 本次实际配置 | 每批 5 个、最多 10 批、请求间隔 5 秒、批间隔 20 秒、`retries=0` |
| 相邻响应观测时间的最小间隔 | 6 秒 |
| 最低在售价范围 | ¥0.88（千瓦武器箱）—¥1,000.00（反恐精英武器箱） |
| 最高求购价范围 | ¥0.85（千瓦武器箱）—¥824.00（反恐精英武器箱） |

这些是依次获取的盘口快照，不是同一时刻的全市场快照。`observed_at` 是程序收到响应的 UTC 时间，不是 Steam 源数据更新时间，也不是成交时间；Steam 响应可能有缓存。

`progress.json` 中保留了早先缺少 `sell_order_count` 的历史错误。之后请求增加 `norender=1`，解析也兼容数量字段缺失，最终已成功完成 42/42；判断本次完成状态请结合较新的 `last_run`、`quotes.json` 和 `collection_summary.json`，不要将旧 `last_error` 当作当前失败。

## 6. 程序结构与操作

运行环境为 Python 3.10+，只使用标准库，无需 `pip install`。核心结构：`load_catalog` 校验目录，`SteamClient` 负责传输及请求限速，`parse_quote` 校验响应，`Collector` 管理试采/分批/续传/保存，`estimate` 使用 `Decimal` 计算折扣，`main` 提供命令行及退出处理。

以下命令供后续获准操作时使用；本次交接只需阅读、离线检查，不启动 `probe` 或 `fetch`：

```bash
# 离线操作；Linux 通常用 python3 替代 python
python -m unittest -v
python steam_cases.py list
python steam_cases.py calc --item "裂空武器箱" --buy-price 2.80 --coefficient 0.86

# 后续获准采集时：先试采，再补齐；fetch 也会自动检查试采门禁
python steam_cases.py probe --max-batches 1
python steam_cases.py fetch

# 新一轮全目录快照须使用新输出目录，保留旧数据
python steam_cases.py fetch --output data-new-snapshot
```

| 参数 | 默认值 / 行为 |
| --- | --- |
| `--catalog` | 脚本所在目录的 `catalog.json` |
| `--output` | 脚本所在目录的 `data/`；显式相对路径按运行时工作目录解析 |
| `--probe-count` | 3，优先裂空、变革、梦魇，全部取得有效人民币最低卖价才通过 |
| `--batch-size` / `--max-batches` | 5 / 10，试采和全量共享当次批次预算 |
| `--request-interval` | 5 秒，从每次请求尝试结束后计时；含重试 |
| `--batch-interval` | 20 秒，相邻批次间等待 |
| `--timeout` | 25 秒；请求间隔、批间隔和超时均须至少 1 秒 |
| `--retries` | 默认 1，可设 0；仅网络错误或 5xx 最多重试一次，先等待至少 30 秒 |
| `--transport` | `auto`；可显式选 `urllib` 或 `powershell` |
| `calc --coefficient` | 默认 0.86，必须大于 0 且不大于 1 |

网络目标为 `https://steamcommunity.com/market/itemordershistogram`，通过本地目录的 `item_nameid` 获取最低在售价、最高求购价、数量及原始盘口。匿名请求，不需要 API Key、Cookie 或 Steam 登录；不执行交易。

### 费用及折扣口径

```text
预计到账 = Steam 价格 × 系数，按人民币分 ROUND_HALF_UP 四舍五入
充值比例 = 用户手填的实际买入成本 ÷ 预计到账
显示折扣 = 充值比例 × 10
每 100 元 Steam 余额的成本 = 充值比例 × 100
```

例如国内买入 ¥10、Steam 最低在售 ¥14，系数 0.86：预计到账 ¥12.04，充值比例 0.8306，约 8.31 折，每 ¥100 余额成本 ¥83.06。`calc` 同时输出最低在售价挂单和最高求购价参考两套结果，使用本地快照，不联网刷新。

`0.86` 是用户指定的估算系数，不能据此声称 Steam 按买家总价固定收取 14% 手续费，也不等于精确手续费算法。最低挂单价不保证成交；CS2 接收物品后的保护期/可出售时间也会影响最终售价。

### 限流、停止和续传

- 试采通过记录绑定目录内容 SHA-256，24 小时内有效；重新试采会先清除旧门禁。试采失败不进入全量。
- 每成功一个商品就保存结构化结果和原始响应；JSON 使用临时文件加 `os.replace` 原子替换，CSV 是可重建导出。同一输出目录使用 OS 文件锁防止并发采集。
- 403 / 429 立即停止，不自动重试；尊重 `Retry-After`，至少冷却 300 秒，冷却写入状态，重启也检查。
- 非 JSON、`success` 失败、无法确认人民币币种或无效价格字段停止并冷却 300 秒；异常响应截断诊断写入 `data/last_invalid_response.json`（仅出现此类错误时生成）。
- Ctrl+C 会保存已成功记录；达到批次上限正常保存结束，再运行继续。退出码：0 为本轮正常结束（可能仅达到批次上限），1 为参数/目录/试采等失败，2 为请求停止或冷却，130 为用户中断。
- 已有成功报价按名称、ID、CNY 匹配后跳过，不因变旧而刷新。24 小时门禁到期只会重新试采，不能当成全目录刷新；新一轮全量使用新 `--output`。

### Windows 与 Linux 传输差异

本机实测 Python `urllib` 直连曾超时，而同机 PowerShell 网络请求成功。因此 `auto` 在 Windows 选择 PowerShell，优先 `pwsh`，其次系统 `powershell`；其他系统选择 `urllib`。PowerShell 以隐藏子进程运行，返回统一结构后走相同的状态校验和限速逻辑；403 / 429 不触发换传输方式。

Linux 服务器的出网、DNS/TLS、Steam 可达性、`urllib` 行为及 `fcntl` 文件锁尚未在目标服务器验证。接收阶段可运行离线测试、检查 Python 版本；联网验证待用户授权后先做少量 probe，再决定是否继续。不要把 Windows 网络成功视为 Linux 已经验证通过。

## 7. 验证记录与已知限制

现有 **9 项离线单元测试通过**，覆盖币种/缺失字段、折扣方向、Retry-After、429 与非 JSON 停止、请求间隔、试采失败门禁、批次预算/续传、冷却持久化和文件锁。

独立审查未修改采集代码、未请求 Steam，额外验证了模拟时钟下的批间等待、Ctrl+C 后跳过成功记录，以及 403/429、500 重试、助手超时等分支。审查发现并促成修复了 PowerShell 7 错误响应中 `Retry-After` 取值丢失的问题；修复后使用本机 HTTPServer，分别由真实 `pwsh` 与 Windows PowerShell 进程验证 200/403/429/500，900 秒头值均正确传播。

当前定位下未发现剩余阻塞问题，但仍有明确边界：没有历史价格数据库、走势图、持续更新、网页、部署配置或自动国内平台取价；42 个目录不自动扩展；缺失挂单数量保留 `null` 而不是伪造 0。服务器环境尚待验证，当前数据仅为上述时间段的首次快照。

## 8. 后续网页方向（等待用户继续下达开发指令）

后续适合从现有 `catalog.json` 和 `data/quotes.json` 做网页：42 箱中文选择/搜索、展示卖价与求购价、用户手填国内实际成本、可调默认 0.86 系数、两种到账与折扣结果、按充值比例排序，并清楚显示快照时间、人民币单位和缺失数据。

价格走势需要在获准后设计持续采样和带时间戳的存储，不能把当前单次快照绘成历史趋势。Web 技术栈、用户输入是否保存、服务器部署方式、采样周期和历史保留策略均未确定。Hermes 当前完成接收及 QQ 汇报后等待用户说明下一阶段，保留现有数据作为基线。
