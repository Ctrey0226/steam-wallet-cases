"""Steam identity and read-only personal inventory (no trading credentials).

Integration: app.include_router(create_account_router(BASE_DIR / 'data' / 'accounts.db')).
The proxy strips /cases; route paths deliberately do not include that prefix.
The fixed public_base_url supplies OpenID return_to, realm, redirects and cookie
scope independently of untrusted Host / forwarded headers. Database initialization
is lazy. http_transport accepts httpx.MockTransport for isolated, offline tests.
All API failures use FastAPI's {detail: {code, message}} JSON envelope.
"""
import asyncio
import hashlib
import hmac
import re
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

OPENID_ENDPOINT = 'https://steamcommunity.com/openid/login'
OPENID_NS = 'http://specs.openid.net/auth/2.0'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class AccountStore:
    """Connections are request-local; only token hashes are persisted."""
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS login_states (
                    token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, steamid TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    csrf_token TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS openid_nonces (
                    nonce_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
            ''')
            with db:
                yield db
        finally:
            db.close()

    def session(self, token):
        if not token or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return None
        with self.connect() as db:
            return db.execute('''SELECT u.id, u.steamid, s.csrf_token FROM sessions s
                JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires > ?''',
                (digest(token), time.time())).fetchone()



def error(status, code, message):
    return HTTPException(status, detail={'code': code, 'message': message},
                         headers={'Cache-Control': 'private, no-store'})


def validate_assertion(q, expected_return_to):
    """Bind every security field to Steam's signed assertion, never Host."""
    required = {'op_endpoint', 'return_to', 'response_nonce', 'identity', 'claimed_id', 'assoc_handle'}
    signed = q.get('openid.signed', '').split(',')
    identity = q.get('openid.claimed_id', '')
    match = re.fullmatch(r'https?://steamcommunity\.com/openid/id/([0-9]{17})', identity)
    nonce = q.get('openid.response_nonce', '')
    valid = (q.get('openid.ns') == OPENID_NS and q.get('openid.mode') == 'id_res'
             and q.get('openid.op_endpoint') == OPENID_ENDPOINT
             and q.get('openid.return_to') == expected_return_to
             and match and q.get('openid.identity') == identity
             and required.issubset(signed) and len(signed) == len(set(signed))
             and all(q.get('openid.' + field) for field in required)
             and bool(q.get('openid.sig')) and 20 < len(nonce) <= 255)
    try:
        issued = datetime.strptime(nonce[:20], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
        valid = valid and 0 <= time.time() - issued <= 300
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        raise error(400, 'invalid_openid', 'Steam 身份验证参数无效。')
    return match[1]


def parse_verification(response):
    if response.status_code != 200 or len(response.content) > 16384:
        return False
    fields = {}
    for line in response.text.splitlines():
        if ':' not in line:
            return False
        key, value = line.split(':', 1)
        if not key or key in fields:
            return False
        fields[key] = value
    return fields.get('is_valid') == 'true'


def decimal_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,30}', value):
        raise ValueError('invalid inventory identifier')
    return value


def text_field(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('invalid inventory text')
    return value


def binary_flag(value):
    if type(value) not in (int, bool) or value not in (0, 1):
        raise ValueError('invalid inventory flag')
    return bool(value)


def normalize_item(asset, desc):
    # The fixed /730/2 endpoint establishes context; assets may omit contextid.
    if asset.get('appid') != 730 or asset.get('contextid', '2') != '2':
        raise ValueError('wrong inventory context')
    amount = asset['amount']
    if not isinstance(amount, str) or not re.fullmatch(r'[0-9]{1,10}', amount) or int(amount) <= 0:
        raise ValueError('invalid inventory amount')
    tags = desc.get('tags', [])
    if not isinstance(tags, list) or len(tags) > 100:
        raise ValueError('invalid tags')
    clean_tags = []
    for tag in tags:
        if not isinstance(tag, dict):
            raise ValueError('invalid tag')
        clean_tags.append({key: text_field(tag.get(key, '')) for key in ('category', 'internal_name', 'localized_tag_name')})
    icon = desc.get('icon_url', '')
    icon_url = ''
    if isinstance(icon, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,2048}', icon):
        icon_url = 'https://community.fastly.steamstatic.com/economy/image/' + icon
    return {'assetid': decimal_id(asset['assetid']), 'classid': decimal_id(asset['classid']),
            'instanceid': decimal_id(asset['instanceid']), 'amount': int(amount),
            'name': text_field(desc['name']), 'market_hash_name': text_field(desc.get('market_hash_name', '')),
            'tradable': binary_flag(desc.get('tradable', 0)), 'marketable': binary_flag(desc.get('marketable', 0)),
            'icon_url': icon_url, 'type': text_field(desc.get('type', '')), 'tags': clean_tags}


def parse_inventory_page(page):
    if not isinstance(page, dict) or type(page.get('success')) is not int or page['success'] != 1:
        raise ValueError('unsuccessful inventory')
    total = page['total_inventory_count']
    if type(total) is not int or not 0 <= total <= 20000:
        raise ValueError('invalid inventory count')
    assets = page.get('assets', [] if total == 0 else None)
    descriptions = page.get('descriptions', [] if total == 0 else None)
    if not isinstance(assets, list) or not isinstance(descriptions, list) or len(assets) > 1000:
        raise ValueError('invalid inventory arrays')
    desc_map = {}
    for desc in descriptions:
        key = (decimal_id(desc['classid']), decimal_id(desc['instanceid']))
        if key in desc_map and desc_map[key] != desc:
            raise ValueError('conflicting descriptions')
        desc_map[key] = desc
    items = [normalize_item(asset, desc_map[(asset['classid'], asset['instanceid'])]) for asset in assets]
    return items, total


class InventoryService:
    """Per-identity, process-local cache; only complete snapshots are cached."""
    def __init__(self, http_transport=None):
        self.http_transport = http_transport
        self.cache = {}
        self.locks = {}

    async def fetch(self, steamid):
        items, seen_assets, seen_cursors = [], set(), set()
        params = {'l': 'schinese', 'count': 1000}
        expected_total = None
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, transport=self.http_transport, trust_env=True) as client:
            for _ in range(20):
                response = await client.get('https://steamcommunity.com/inventory/' + steamid + '/730/2', params=params)
                if response.status_code == 403:
                    raise error(403, 'inventory_private', 'Steam 库存不可见，请将库存隐私设为公开。')
                if response.status_code == 429:
                    raise error(429, 'rate_limited', 'Steam 请求过于频繁，请稍后重试。')
                if response.status_code != 200:
                    raise error(502, 'steam_unavailable', 'Steam 库存服务暂时不可用。')
                page = response.json()
                batch, total = parse_inventory_page(page)
                if expected_total is None:
                    expected_total = total
                if total != expected_total:
                    raise ValueError('inventory changed during pagination')
                for item in batch:
                    if item['assetid'] in seen_assets:
                        raise ValueError('duplicate asset')
                    seen_assets.add(item['assetid'])
                    items.append(item)
                if len(items) > total:
                    raise ValueError('too many assets')
                more = binary_flag(page.get('more_items', 0))
                if not more:
                    if len(items) != total:
                        raise ValueError('incomplete inventory')
                    return items
                cursor = decimal_id(page['last_assetid'])
                if not batch or cursor in seen_cursors or len(items) >= total:
                    raise ValueError('pagination made no progress')
                seen_cursors.add(cursor)
                params['start_assetid'] = cursor
        raise ValueError('inventory page cap exceeded')

    async def get(self, steamid):
        lock = self.locks.setdefault(steamid, asyncio.Lock())
        async with lock:
            cached = self.cache.get(steamid)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            try:
                items = await self.fetch(steamid)
            except httpx.HTTPError:
                raise error(502, 'steam_unavailable', '无法连接 Steam 库存服务。') from None
            except (ValueError, KeyError, TypeError, AttributeError):
                raise error(502, 'inventory_malformed', 'Steam 返回的库存不完整或格式错误。') from None
            data = {'steamid': steamid, 'items': items, 'total_items': sum(i['amount'] for i in items),
                    'asset_count': len(items), 'fetched_at': datetime.now(timezone.utc).isoformat()}
            self.cache[steamid] = (time.monotonic() + 120, data)
            return data


def create_account_router(db_path, public_base_url='https://ctrlversion.top/cases', *, http_transport=None):
    router = APIRouter()
    base = public_base_url.rstrip('/')
    parts = urlsplit(base)
    if parts.scheme != 'https' or not parts.netloc or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError('public_base_url must be a fixed HTTPS URL')
    cookie_path = parts.path.rstrip('/') + '/'
    store = AccountStore(db_path)
    inventories = InventoryService(http_transport)
    login_url = base + '/auth/steam'

    @router.get('/auth/steam')
    async def login():
        state = secrets.token_urlsafe(32)
        with store.connect() as db:
            db.execute('DELETE FROM login_states WHERE expires <= ?', (time.time(),))
            db.execute('INSERT INTO login_states VALUES (?, ?)', (digest(state), time.time() + 600))
        return_to = base + '/auth/steam/callback?' + urlencode({'state': state})
        params = {'openid.ns': OPENID_NS, 'openid.mode': 'checkid_setup',
                  'openid.return_to': return_to, 'openid.realm': base + '/',
                  'openid.identity': OPENID_NS + '/identifier_select',
                  'openid.claimed_id': OPENID_NS + '/identifier_select'}
        response = RedirectResponse(OPENID_ENDPOINT + '?' + urlencode(params), status_code=303)
        response.headers['Cache-Control'] = 'private, no-store'
        response.set_cookie('steam_login_state', state, max_age=600, path=cookie_path,
                            secure=True, httponly=True, samesite='lax')
        return response

    @router.get('/auth/steam/callback')
    async def callback(request: Request):
        q = dict(request.query_params)
        if len(q) != len(request.query_params.multi_items()) or len(request.scope.get('query_string', b'')) > 16384:
            raise error(400, 'invalid_openid', 'Steam 身份验证参数重复或过长。')
        state = q.get('state', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', state):
            raise error(400, 'invalid_openid', 'Steam 登录状态无效。')
        expected_return_to = base + '/auth/steam/callback?' + urlencode({'state': state})
        steamid = validate_assertion(q, expected_return_to)
        with store.connect() as db:
            row = db.execute('SELECT expires FROM login_states WHERE token_hash = ?', (digest(state),)).fetchone()
        browser_state = request.cookies.get('steam_login_state', '')
        if (not row or row['expires'] <= time.time() or not browser_state.isascii()
                or not hmac.compare_digest(state, browser_state)):
            raise error(400, 'invalid_openid', 'Steam 登录状态无效，请重试。')
        payload = {k: v for k, v in q.items() if k.startswith('openid.')}
        payload['openid.mode'] = 'check_authentication'
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, transport=http_transport, trust_env=True) as client:
                verified = await client.post(OPENID_ENDPOINT, data=payload)
            valid = parse_verification(verified)
        except (httpx.HTTPError, ValueError):
            valid = False
        if not valid:
            raise error(400, 'invalid_openid', 'Steam 身份验证失败。')
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with store.connect() as db:
            consumed = db.execute('DELETE FROM login_states WHERE token_hash = ? AND expires > ?', (digest(state), time.time()))
            if consumed.rowcount != 1:
                raise error(400, 'invalid_openid', 'Steam 登录状态已使用。')
            db.execute('DELETE FROM openid_nonces WHERE expires <= ?', (time.time(),))
            try:
                db.execute('INSERT INTO openid_nonces VALUES (?, ?)', (digest(q['openid.response_nonce']), time.time() + 600))
            except sqlite3.IntegrityError:
                raise error(400, 'invalid_openid', 'Steam 验证响应已使用。')
            db.execute('INSERT OR IGNORE INTO users (steamid) VALUES (?)', (steamid,))
            user = db.execute('SELECT id FROM users WHERE steamid = ?', (steamid,)).fetchone()
            db.execute('DELETE FROM sessions WHERE token_hash = ? OR expires <= ?',
                       (digest(request.cookies.get('steam_session', '')), time.time()))
            db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)', (digest(token), user['id'], csrf, time.time() + 604800))
        response = RedirectResponse(base + '/', status_code=303)
        response.headers['Cache-Control'] = 'private, no-store'
        response.set_cookie('steam_session', token, max_age=604800, path=cookie_path,
                            secure=True, httponly=True, samesite='lax')
        response.delete_cookie('steam_login_state', path=cookie_path, secure=True, httponly=True, samesite='lax')
        return response

    @router.get('/api/account')
    async def account(request: Request, response: Response):
        response.headers['Cache-Control'] = 'private, no-store'
        user = store.session(request.cookies.get('steam_session'))
        if not user:
            return {'authenticated': False, 'login_url': login_url}
        return {'authenticated': True, 'login_url': login_url,
                'user': {'id': user['id'], 'steamid': user['steamid'],
                         'profile_url': 'https://steamcommunity.com/profiles/' + user['steamid']},
                'csrf_token': user['csrf_token']}

    @router.post('/auth/logout')
    async def logout(request: Request, response: Response):
        token = request.cookies.get('steam_session')
        user = store.session(token)
        if not user:
            raise error(401, 'authentication_required', '请先绑定 Steam 账户。')
        csrf = request.headers.get('x-csrf-token', '')
        origin = request.headers.get('origin')
        expected_origin = parts.scheme + '://' + parts.netloc
        if (not csrf.isascii() or not hmac.compare_digest(csrf, user['csrf_token'])
                or (origin is not None and origin != expected_origin)):
            raise error(403, 'csrf_failed', '请求校验失败，请刷新页面后重试。')
        with store.connect() as db:
            db.execute('DELETE FROM sessions WHERE token_hash = ?', (digest(token),))
        inventories.cache.pop(user['steamid'], None)
        response.headers['Cache-Control'] = 'private, no-store'
        response.delete_cookie('steam_session', path=cookie_path, secure=True, httponly=True, samesite='lax')
        return {'authenticated': False}

    @router.get('/api/inventory')
    async def inventory(request: Request, response: Response):
        user = store.session(request.cookies.get('steam_session'))
        if not user:
            raise error(401, 'authentication_required', '请先绑定 Steam 账户。')
        if request.query_params:
            raise error(400, 'invalid_inventory_selector', '只能查看当前账户库存，不接受账户选择参数。')
        response.headers['Cache-Control'] = 'private, no-store'
        return await inventories.get(user['steamid'])

    return router
