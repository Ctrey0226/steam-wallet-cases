from contextlib import closing
import pathlib
import tempfile
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / 'accounts.db'

    def client(self, **kwargs):
        from steam_accounts import create_account_router
        app = FastAPI()
        app.include_router(create_account_router(self.db, **kwargs))
        client = TestClient(app, base_url='https://ctrlversion.top')
        self.addCleanup(client.close)
        return client

    def assertion(self, c, steamid='76561198000000001', **changes):
        from urllib.parse import urlsplit, parse_qs
        from datetime import datetime, timezone
        import secrets
        r = c.get('/auth/steam', follow_redirects=False)
        rt = parse_qs(urlsplit(r.headers['location']).query)['openid.return_to'][0]
        state = parse_qs(urlsplit(rt).query)['state'][0]
        # TestClient sees stripped proxy paths, so explicitly forward cookies
        # scoped to /cases/ when exercising upstream /api/... paths.
        c.headers['cookie'] = 'steam_login_state=' + state
        identity = 'https://steamcommunity.com/openid/id/' + steamid
        q = {'state': state, 'openid.ns': 'http://specs.openid.net/auth/2.0',
             'openid.mode': 'id_res', 'openid.op_endpoint': 'https://steamcommunity.com/openid/login',
             'openid.return_to': rt, 'openid.identity': identity, 'openid.claimed_id': identity,
             'openid.response_nonce': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') + secrets.token_hex(8),
             'openid.assoc_handle': 'test-handle', 'openid.sig': 'mock-signature',
             'openid.signed': 'op_endpoint,return_to,response_nonce,identity,claimed_id,assoc_handle'}
        q.update(changes)
        return q

    def login(self, c, steamid='76561198000000001'):
        r = c.get('/auth/steam/callback', params=self.assertion(c, steamid), follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        c.headers['cookie'] = 'steam_session=' + r.cookies['steam_session']
        return r.cookies['steam_session']

    def test_verified_callback_creates_hashed_rotating_session(self):
        import httpx
        import sqlite3
        import hashlib
        calls = []
        def handler(request):
            from urllib.parse import parse_qs
            calls.append(request)
            self.assertEqual(str(request.url), 'https://steamcommunity.com/openid/login')
            self.assertEqual(request.method, 'POST')
            self.assertEqual(parse_qs(request.content.decode())['openid.mode'], ['check_authentication'])
            return httpx.Response(200, text='ns:http://specs.openid.net/auth/2.0\nis_valid:true\n')
        c = self.client(http_transport=httpx.MockTransport(handler))
        token = self.login(c)
        data = c.get('/api/account').json()
        self.assertTrue(data['authenticated'])
        self.assertEqual(data['user']['steamid'], '76561198000000001')
        self.assertEqual(data['user']['profile_url'], 'https://steamcommunity.com/profiles/76561198000000001')
        self.assertGreaterEqual(len(data['csrf_token']), 32)
        self.assertEqual(data['login_url'], 'https://ctrlversion.top/cases/auth/steam')
        with closing(sqlite3.connect(self.db)) as db, db:
            dump = '\n'.join(db.iterdump())
            self.assertNotIn(token, dump)
            self.assertIn(hashlib.sha256(token.encode()).hexdigest(), dump)
        old = token
        # Retain the old session alongside the new browser state.
        q = self.assertion(c)
        c.headers['cookie'] += '; steam_session=' + old
        r = c.get('/auth/steam/callback', params=q, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertNotEqual(r.cookies['steam_session'], old)
        c.headers['cookie'] = 'steam_session=' + old
        self.assertFalse(c.get('/api/account').json()['authenticated'])
        c.headers['cookie'] = 'steam_session=' + r.cookies['steam_session']
        self.assertEqual(c.get('/api/account').json()['user']['id'], data['user']['id'])
        self.assertEqual(len(calls), 2)

    def test_callback_rejects_untrusted_fields_before_verification(self):
        import httpx
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, text='is_valid:true\n')
        c = self.client(http_transport=httpx.MockTransport(handler))
        bad = [
            {'openid.ns': 'wrong'}, {'openid.mode': 'cancel'},
            {'openid.op_endpoint': 'https://attacker.invalid/openid'},
            {'openid.return_to': 'https://attacker.invalid/callback'},
            {'openid.identity': 'https://steamcommunity.com/openid/id/76561198000000002'},
            {'openid.claimed_id': 'https://attacker.invalid/openid/id/76561198000000001'},
            {'openid.response_nonce': '2000-01-01T00:00:00Zold'},
            {'openid.response_nonce': '2999-01-01T00:00:00Zfuture'},
            {'openid.response_nonce': 'broken'}, {'openid.sig': ''},
            {'openid.signed': 'return_to,identity,claimed_id'},
        ]
        for changes in bad:
            with self.subTest(changes=changes):
                q = self.assertion(c, **changes)
                before = len(calls)
                r = c.get('/auth/steam/callback', params=q, follow_redirects=False)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()['detail']['code'], 'invalid_openid')
                self.assertEqual(len(calls), before)
                self.assertNotIn('steam_session', r.cookies)
        q = self.assertion(c)
        for key in ('state', 'openid.identity', 'openid.return_to'):
            r = c.get('/auth/steam/callback', params=list(q.items()) + [(key, q[key])], follow_redirects=False)
            self.assertEqual(r.status_code, 400)
        c.headers['cookie'] = 'steam_login_state=wrong'
        self.assertEqual(c.get('/auth/steam/callback', params=q).status_code, 400)
        self.assertEqual(calls, [])

    def test_callback_nonce_replay_expiry_and_verification_fail_closed(self):
        import httpx
        import sqlite3
        mode = ['valid']
        def handler(request):
            if mode[0] == 'network':
                raise httpx.ConnectError('offline', request=request)
            body = {'valid': 'is_valid:true\n', 'false': 'is_valid:false\n',
                    'duplicate': 'is_valid:false\nis_valid:true\n',
                    'malformed': 'garbage\nis_valid:true\n',
                    'redirect': 'is_valid:true\n'}[mode[0]]
            return httpx.Response(302 if mode[0] == 'redirect' else 200, text=body)
        c = self.client(http_transport=httpx.MockTransport(handler))
        q = self.assertion(c)
        self.assertEqual(c.get('/auth/steam/callback', params=q, follow_redirects=False).status_code, 303)
        self.assertEqual(c.get('/auth/steam/callback', params=q).status_code, 400)
        replay = self.assertion(c, **{'openid.response_nonce': q['openid.response_nonce']})
        self.assertEqual(c.get('/auth/steam/callback', params=replay, follow_redirects=False).status_code, 400)
        q = self.assertion(c)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE login_states SET expires = 0')
        self.assertEqual(c.get('/auth/steam/callback', params=q).status_code, 400)
        for value in ('false', 'duplicate', 'malformed', 'redirect', 'network'):
            with self.subTest(mode=value):
                mode[0] = value
                q = self.assertion(c)
                r = c.get('/auth/steam/callback', params=q, follow_redirects=False)
                self.assertEqual(r.status_code, 400)
                self.assertNotIn('steam_session', r.cookies)

    def page(self, assetid='100', amount='2'):
        return {'success': 1, 'total_inventory_count': 1, 'more_items': 0,
                'assets': [{'appid': 730, 'contextid': '2', 'assetid': assetid, 'classid': '10', 'instanceid': '0', 'amount': amount}],
                'descriptions': [{'classid': '10', 'instanceid': '0', 'name': '测试箱',
                    'market_hash_name': 'Test Case', 'tradable': 1, 'marketable': 0,
                    'icon_url': 'abc-DEF_123', 'type': 'Container',
                    'tags': [{'category': 'Type', 'internal_name': 'CSGO_Type_WeaponCase', 'localized_tag_name': '武器箱'}],
                    'descriptions': [{'value': '<script>evil</script>'}]}]}

    def test_personal_inventory_ownership_normalization_and_cache(self):
        import httpx
        calls = []
        def handler(request):
            if request.method == 'POST':
                return httpx.Response(200, text='is_valid:true\n')
            calls.append(str(request.url))
            self.assertEqual(request.url.host, 'steamcommunity.com')
            self.assertEqual(request.url.params['l'], 'schinese')
            self.assertEqual(request.url.params['count'], '1000')
            self.assertNotIn('cookie', request.headers)
            return httpx.Response(200, json=self.page())
        transport = httpx.MockTransport(handler)
        a = self.client(http_transport=transport)
        b = TestClient(a.app, base_url='https://ctrlversion.top')
        self.addCleanup(b.close)
        self.login(a)
        self.login(b, '76561198000000002')
        r = a.get('/api/inventory')
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data['steamid'], '76561198000000001')
        self.assertEqual(data['total_items'], 2)
        self.assertEqual(data['asset_count'], 1)
        self.assertTrue(data['fetched_at'].endswith('+00:00'))
        item = data['items'][0]
        self.assertEqual(item, {'assetid': '100', 'classid': '10', 'instanceid': '0', 'name': '测试箱',
            'market_hash_name': 'Test Case', 'amount': 2, 'tradable': True, 'marketable': False,
            'icon_url': 'https://community.fastly.steamstatic.com/economy/image/abc-DEF_123',
            'type': 'Container', 'tags': [{'category': 'Type', 'internal_name': 'CSGO_Type_WeaponCase', 'localized_tag_name': '武器箱'}]})
        self.assertEqual(r.headers['cache-control'], 'private, no-store')
        self.assertEqual(a.get('/api/inventory').json(), data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(b.get('/api/inventory').json()['steamid'], '76561198000000002')
        self.assertIn('/76561198000000001/730/2', calls[0])
        self.assertIn('/76561198000000002/730/2', calls[1])
        for key in ('steamid', 'userid', 'user_id', 'steam_id', 'SteamID'):
            self.assertEqual(a.get('/api/inventory', params={key: '76561198000000002'}).status_code, 400)
        self.assertEqual(len(calls), 2)

    def test_inventory_upstream_errors_are_not_cached(self):
        import httpx
        mode = ['private']
        calls = []
        def handler(request):
            if request.method == 'POST':
                return httpx.Response(200, text='is_valid:true\n')
            calls.append(mode[0])
            if mode[0] == 'network':
                raise httpx.ReadTimeout('offline', request=request)
            if mode[0] == 'bad_json':
                return httpx.Response(200, text='not json')
            if mode[0] == 'private':
                return httpx.Response(403, json={'success': 0})
            if mode[0] == 'rate':
                return httpx.Response(429)
            if mode[0] == 'redirect':
                return httpx.Response(302, headers={'Location': 'https://evil.invalid'})
            return httpx.Response(200, json={})
        c = self.client(http_transport=httpx.MockTransport(handler))
        self.login(c)
        for name, status, code in [('private', 403, 'inventory_private'), ('rate', 429, 'rate_limited'),
            ('network', 502, 'steam_unavailable'), ('bad_json', 502, 'inventory_malformed'),
            ('redirect', 502, 'steam_unavailable'), ('malformed', 502, 'inventory_malformed')]:
            with self.subTest(name=name):
                mode[0] = name
                for _ in range(2):
                    before = len(calls)
                    r = c.get('/api/inventory')
                    self.assertEqual(r.status_code, status, r.text)
                    self.assertEqual(r.json()['detail']['code'], code)
                    self.assertEqual(r.headers['cache-control'], 'private, no-store')
                    self.assertEqual(len(calls), before + 1)

    def test_inventory_malformed_data_not_silently_empty_or_unsafe(self):
        import httpx
        current = [self.page()]
        def handler(request):
            if request.method == 'POST':
                return httpx.Response(200, text='is_valid:true\n')
            return httpx.Response(200, json=current[0])
        c = self.client(http_transport=httpx.MockTransport(handler))
        self.login(c)
        changes = [lambda p: p.update(success=0), lambda p: p.pop('assets'),
            lambda p: p.update(assets=[]), lambda p: p.update(descriptions=[]),
            lambda p: p.update(total_inventory_count=2),
            lambda p: p['assets'][0].update(amount='0'), lambda p: p['assets'][0].update(amount='oops'),
            lambda p: p['assets'][0].update(assetid='../../bad'),
            lambda p: p['assets'][0].update(appid=440),
            lambda p: p['descriptions'][0].update(tradable='false'),
            lambda p: p['descriptions'][0].update(tags='invalid')]
        for change in changes:
            current[0] = self.page()
            change(current[0])
            r = c.get('/api/inventory')
            self.assertEqual(r.status_code, 502, r.text)
            self.assertEqual(r.json()['detail']['code'], 'inventory_malformed')
        for icon in ('https://evil.invalid/x', '../x', 'foo/bar', '//evil', 'abc?x=y'):
            current[0] = self.page()
            current[0]['descriptions'][0]['icon_url'] = icon
            # Different router has an independent cache.
            other = self.client(http_transport=httpx.MockTransport(handler))
            self.login(other)
            r = other.get('/api/inventory')
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()['items'][0]['icon_url'], '')
        current[0] = {'success': 1, 'total_inventory_count': 0}
        other = self.client(http_transport=httpx.MockTransport(handler))
        self.login(other)
        self.assertEqual(other.get('/api/inventory').json()['items'], [])

    def test_inventory_context_is_derived_from_fixed_endpoint_when_asset_omits_it(self):
        import httpx
        page = self.page()
        page['assets'][0].pop('contextid')
        def handler(request):
            if request.method == 'POST':
                return httpx.Response(200, text='is_valid:true\n')
            return httpx.Response(200, json=page)
        c = self.client(http_transport=httpx.MockTransport(handler))
        self.login(c)
        response = c.get('/api/inventory')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['items'][0]['assetid'], '100')

    def test_inventory_pagination_complete_snapshot(self):
        import httpx
        requests = []
        def handler(request):
            if request.method == 'POST':
                return httpx.Response(200, text='is_valid:true\n')
            requests.append(request)
            cursor = request.url.params.get('start_assetid')
            page = self.page('101' if cursor else '100', '3' if cursor else '2')
            page['total_inventory_count'] = 2
            if not cursor:
                page.update(more_items=1, last_assetid='100')
            else:
                self.assertEqual(cursor, '100')
            return httpx.Response(200, json=page)
        c = self.client(http_transport=httpx.MockTransport(handler))
        self.login(c)
        r = c.get('/api/inventory')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['total_items'], 5)
        self.assertEqual(r.json()['asset_count'], 2)
        self.assertEqual(len(requests), 2)

    def test_inventory_pagination_rejects_duplicates_and_incomplete_pages(self):
        import httpx
        for scenario in ('duplicate', 'cursor_loop', 'missing_cursor', 'bad_cursor', 'missing_more', 'bad_more',
                         'changed_total', 'empty_more', 'cap', 'late_failure'):
            calls = []
            def handler(request):
                if request.method == 'POST':
                    return httpx.Response(200, text='is_valid:true\n')
                calls.append(request)
                index = len(calls)
                page = self.page(str(index))
                page.update(total_inventory_count=100, more_items=1, last_assetid=str(index))
                if scenario == 'duplicate':
                    page['assets'][0]['assetid'] = '1'
                elif scenario == 'cursor_loop':
                    page['last_assetid'] = '1'
                elif scenario == 'missing_cursor':
                    page.pop('last_assetid')
                elif scenario == 'bad_cursor':
                    page['last_assetid'] = 'https://evil.invalid'
                elif scenario == 'missing_more':
                    page.pop('more_items')
                elif scenario == 'bad_more':
                    page['more_items'] = 'false'
                elif scenario == 'changed_total' and index == 2:
                    page['total_inventory_count'] = 101
                elif scenario == 'empty_more':
                    page['assets'] = []
                elif scenario == 'late_failure' and index == 2:
                    return httpx.Response(429)
                return httpx.Response(200, json=page)
            c = self.client(http_transport=httpx.MockTransport(handler))
            self.login(c)
            with self.subTest(scenario=scenario):
                r = c.get('/api/inventory')
                self.assertEqual(r.status_code, 429 if scenario == 'late_failure' else 502, r.text)
                self.assertNotIn('items', r.json())
                self.assertLessEqual(len(calls), 20)
                before = len(calls)
                c.get('/api/inventory')
                self.assertGreater(len(calls), before)

    def test_logout_requires_session_csrf_and_matching_origin(self):
        import httpx
        import sqlite3
        c = self.client(http_transport=httpx.MockTransport(lambda req: httpx.Response(200, text='is_valid:true\n')))
        self.assertEqual(c.post('/auth/logout').status_code, 401)
        self.login(c)
        csrf = c.get('/api/account').json()['csrf_token']
        for headers in ({}, {'X-CSRF-Token': 'wrong'},
                        {'X-CSRF-Token': csrf, 'Origin': 'https://evil.invalid'},
                        {'X-CSRF-Token': csrf, 'Origin': 'null'},
                        {'X-CSRF-Token': csrf, 'Origin': 'https://ctrlversion.top.evil.invalid'}):
            self.assertEqual(c.post('/auth/logout', headers=headers).status_code, 403)
            self.assertTrue(c.get('/api/account').json()['authenticated'])
        r = c.post('/auth/logout', headers={'X-CSRF-Token': csrf, 'Origin': 'https://ctrlversion.top'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {'authenticated': False})
        for flag in ('Path=/cases/', 'Max-Age=0', 'Secure', 'HttpOnly', 'SameSite=lax'):
            self.assertIn(flag, r.headers['set-cookie'])
        self.assertFalse(c.get('/api/account').json()['authenticated'])
        self.assertEqual(c.get('/api/inventory').status_code, 401)
        with closing(sqlite3.connect(self.db)) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
        self.login(c)
        csrf = c.get('/api/account').json()['csrf_token']
        # Non-browser callers may omit Origin, never the unpredictable CSRF token.
        self.assertEqual(c.post('/auth/logout', headers={'X-CSRF-Token': csrf}).status_code, 200)
        self.login(c)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE sessions SET expires = 0')
        self.assertFalse(c.get('/api/account').json()['authenticated'])
        self.assertEqual(c.post('/auth/logout', headers={'X-CSRF-Token': csrf}).status_code, 401)

    def test_inventory_cache_expires_and_coalesces_concurrent_fetches(self):
        import asyncio
        import httpx
        from steam_accounts import InventoryService
        calls = []
        async def handler(request):
            calls.append(request)
            await asyncio.sleep(0.01)
            return httpx.Response(200, json=self.page())
        async def scenario():
            service = InventoryService(httpx.MockTransport(handler))
            results = await asyncio.gather(*(service.get('76561198000000001') for _ in range(5)))
            self.assertEqual(len(calls), 1)
            self.assertTrue(all(result == results[0] for result in results))
            import time
            expiry, cached = service.cache['76561198000000001']
            self.assertTrue(119 < expiry - time.monotonic() <= 120)
            service.cache['76561198000000001'] = (0, cached)
            await service.get('76561198000000001')
            self.assertEqual(len(calls), 2)
        asyncio.run(scenario())

    def test_unknown_and_duplicate_inventory_query_rejected(self):
        import httpx
        c = self.client(http_transport=httpx.MockTransport(lambda req: httpx.Response(200, text='is_valid:true\n')))
        self.login(c)
        for params in ({'anything': 'value'}, [('steamid', '1'), ('steamid', '2')]):
            r = c.get('/api/inventory', params=params)
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()['detail']['code'], 'invalid_inventory_selector')

    def test_non_ascii_browser_secrets_fail_closed(self):
        import httpx
        c = self.client(http_transport=httpx.MockTransport(lambda req: httpx.Response(200, text='is_valid:true\n')))
        q = self.assertion(c)
        # Latin-1 bytes are allowed by the transport; hmac string comparison
        # must not turn an attacker-controlled cookie/header into a 500.
        r = c.get('/auth/steam/callback', params=q, headers={'cookie': b'steam_login_state=\xff'})
        self.assertEqual(r.status_code, 400)
        self.login(c)
        r = c.post('/auth/logout', headers={'X-CSRF-Token': b'\xff'})
        self.assertEqual(r.status_code, 403)

    def test_login_redirect_and_state_cookie(self):
        from urllib.parse import urlsplit, parse_qs
        import hashlib
        import sqlite3
        c = self.client()
        r = c.get('/auth/steam', follow_redirects=False, headers={'host': 'evil.example'})
        self.assertEqual(r.status_code, 303)
        url = urlsplit(r.headers['location'])
        self.assertEqual(url.scheme + '://' + url.netloc + url.path, 'https://steamcommunity.com/openid/login')
        q = parse_qs(url.query)
        self.assertTrue(q['openid.return_to'][0].startswith('https://ctrlversion.top/cases/auth/steam/callback?state='))
        self.assertEqual(q['openid.realm'], ['https://ctrlversion.top/cases/'])
        cookie = r.headers['set-cookie']
        for flag in ('Secure', 'HttpOnly', 'SameSite=lax', 'Path=/cases/'):
            self.assertIn(flag, cookie)
        token = r.cookies['steam_login_state']
        with closing(sqlite3.connect(self.db)) as db, db:
            row = db.execute('SELECT token_hash FROM login_states').fetchone()
            self.assertEqual(row[0], hashlib.sha256(token.encode()).hexdigest())
            self.assertNotIn(token, '\n'.join(db.iterdump()))

    def test_account_requests_respect_trusted_service_proxy_environment(self):
        import httpx
        from unittest.mock import patch
        transport = httpx.MockTransport(lambda request: httpx.Response(200,
            text='is_valid:true\n') if request.method == 'POST' else httpx.Response(200, json=self.page()))
        original = httpx.AsyncClient
        with patch('steam_accounts.httpx.AsyncClient', wraps=original) as clients:
            c = self.client(http_transport=transport)
            self.login(c)
            self.assertEqual(c.get('/api/inventory').status_code, 200)
        self.assertEqual(len(clients.call_args_list), 2)
        for call in clients.call_args_list:
            self.assertTrue(call.kwargs.get('trust_env', True),
                            'production requests must honor existing administrator proxy settings')

    def test_anonymous_account_and_inventory(self):
        c = self.client()
        self.assertFalse(self.db.exists())
        r = c.get('/api/account')
        self.assertEqual(r.json(), {'authenticated': False, 'login_url': 'https://ctrlversion.top/cases/auth/steam'})
        self.assertEqual(r.headers['cache-control'], 'private, no-store')
        r = c.get('/api/inventory')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['detail']['code'], 'authentication_required')


if __name__ == '__main__':
    unittest.main()
