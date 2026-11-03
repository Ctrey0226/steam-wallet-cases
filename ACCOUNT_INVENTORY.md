# Steam 账户与个人库存接入

## 范围与身份模型

本版是只读库存管理，不包含自动挂单、购买、接受或发送交易，也不接收 Steam 密码、浏览器 Cookie、手机令牌或 API Key。

使用 Steam 官方 OpenID 作为本站登录方式：首次通过 Steam 验证时创建本站用户并绑定唯一 SteamID；之后同一 Steam 身份回到同一用户。本版没有独立的本站用户名/密码注册，也没有手填 SteamID 绑定入口，避免填写他人 SteamID 后冒充已验证身份。

- `/account`：绑定说明、本人已验证身份、退出本站。
- `/inventory`：只在已登录绑定后读取该用户的 CS2 可见库存。
- `/api/account`：当前登录状态，不接受指定用户身份。
- `/api/inventory`：仅从服务端会话解析本人 SteamID；不能用 URL 参数选择其他账户。
- `/auth/steam`、`/auth/steam/callback`：Steam 官方 OpenID 登录流程。
- `/auth/logout`：POST + CSRF 校验，撤销本站会话。

“库存仅本人可查看”指本站的个人库存接口按登录账户隔离，不会把 Steam 原本公开的库存变成私密。Steam OpenID 只验证身份，不授予读取私密库存或操作交易的权限。库存需在 Steam 侧公开；储存单元内部及其他非公开物品不保证可见。交易状态仅为读取时的 Steam 快照。

## 固定公开入口与反向代理

公开入口为 `https://ctrlversion.top/cases/`，现有反向代理剥离 `/cases/` 后转发到本地 FastAPI。认证回调、Cookie 路径与重定向使用固定公开入口，不从用户提交的 Host / X-Forwarded-Host 构造。

服务为 `steam-wallet-cases.service`，监听 `127.0.0.1:8765`。只有授权管理员才可发布或重启；不要改动 Nginx、系统依赖、代理环境或现有采集周期。本次发布沿用现有服务及其可信代理环境，两个 Steam HTTP 客户端使用 `trust_env=True`，不接受请求指定代理，固定 Steam HTTPS endpoint、禁止重定向，保留 TLS 验证。

## 自动化与隔离验证

- `.venv/bin/python -m unittest discover -q`：33 项通过。
- `node --test test_frontend.cjs test_account_frontend.cjs`：20 项通过。
- Python 编译、提交改动空白检查通过。
- 临时浏览器测试实例挂载 `/cases` 前缀并禁用 lifespan，未启动采集器；Chromium 在 1440×1000 与 390×844 打开未登录库存页均无横向溢出，测试实例已结束。
- 未登录不请求库存；已绑定只请求 `/api/inventory`，不发送 SteamID 选择参数。
- 登出使用 CSRF，清空已渲染数据，并丢弃迟到的旧账户库存响应。
- 名称按纯文本呈现，图片仅允许指定 Steam CDN；搜索及可交易/可上架/受限筛选已测试。
- 私密、限流、上游失败分别显示错误，不当作“库存为空”。
- 固定 `/730/2` 请求上下文允许 asset 缺少 contextid，继续拒绝显式错误 app/context。
- 独立安全复核未发现阻断性身份绕过、越权、CSRF、回放或前端注入问题；可信代理调整另经独立只读复核通过。

测试中的身份、库存与 OpenID 响应均为隔离 fixture，不得写入正式账户数据库或冒充真实用户库存。代理回归测试验证客户端配置，不等同真实代理路由测试。

## 发布记录与线上核验

2026-11-03 已由授权管理员要求发布。功能提交 `bf3a354` 与格式修正 `1032e5e` 已推送到 `Ctrey0226/steam-wallet-cases` 的 `main`，并通过 `git ls-remote` 核对远端提交一致。未提交运行中的行情数据、账户数据库、环境文件及无关启动脚本改动。

发布前等待正在进行的采集完成，确认 `is_collecting=false` 且 `last_error=null` 后，仅重启应用服务。systemd 启动时间为 2026-11-03 17:25:30 CST，状态 `active/running`。未修改 Nginx 或采集调度配置。

本机应用及真实公开 HTTPS 入口（保留 TLS 验证、不走出站代理）均实测：

- 行情首页、`/account`、`/inventory`、账户 JS/CSS 返回 200。
- `/api/account` 返回 200 且 `authenticated=false`。
- `/api/inventory` 未登录返回 401 / `authentication_required`。
- 两个个人 API 均为 `Cache-Control: private, no-store`。
- `/api/cases` 返回 200，包含 42 个武器箱。
- `/auth/steam` 返回 303，目标主机为 `steamcommunity.com`，return_to 为本站固定 HTTPS 回调，浏览器状态 Cookie 含 Secure 与 HttpOnly；未记录 Cookie、state 或完整认证跳转 URL。

上述登录入口探测只产生短期登录状态，未建立真实用户账户或用户会话。正式账户库不纳入 Git。当前环境经出站代理访问本站仍出现 TLS EOF，但同机直连本站公开 HTTPS 已通过，不能把代理探测失败当作站点故障。

## 尚待真实用户完成的验证

1. 用户自行在 Steam 官方页面完成登录，验证正式 HTTPS 回调与会话。
2. 通过实际公开库存验证身份归属、读取与分页。

服务与登录入口已上线，但未完成真实 Steam 身份认证及真实库存联调，不得声称已经读取用户库存。非阻断性后续改进：库存失败（含 429）尚无遵守 Retry-After 的冷却，反复点击或排队请求可能重复触达上游；成功缓存不代表失败路径限流。
