// All account/inventory responses below are isolated test fixtures, never live credentials.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('/opt/hermes-agent/node_modules/jsdom');
const root = __dirname;
async function openPage({ authenticated = false, route = 'inventory', failure = null, items = null } = {}) {
  const htmlPath = path.join(root, 'web/account.html');
  assert.ok(fs.existsSync(htmlPath), 'Account and inventory page must exist');
  const html = fs.readFileSync(htmlPath, 'utf8').replace(/<script[^>]+src=[^>]+><\/script>/g, '');
  const errors = [], calls = [];
  const console = new VirtualConsole();
  console.on('jsdomError', e => errors.push(e));
  const dom = new JSDOM(html, {
    url: `https://ctrlversion.top/cases/${route}`, runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole: console
  });
  dom.window.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith('/api/account')) return { ok: true, status: 200, json: async () => authenticated
      ? { authenticated: true, login_url: '/cases/auth/steam', user: { id: 'fixture-user', steamid: '76561198000000001', profile_url: 'https://steamcommunity.com/profiles/76561198000000001' }, csrf_token: 'fixture-csrf' }
      : { authenticated: false, login_url: '/cases/auth/steam' } };
    if (url.endsWith('/api/inventory')) {
      if (failure) return { ok: false, status: failure.status, json: async () => ({ detail: { code: failure.code, message: failure.message } }) };
      return { ok: true, status: 200, json: async () => ({ steamid: '76561198000000001', total_items: 2, asset_count: 2, fetched_at: '2026-11-03T06:00:00+00:00', items: items || [
        { assetid: '1', name: '裂空武器箱', market_hash_name: 'Fracture Case', amount: 1, tradable: true, marketable: true, icon_url: null, type: '武器箱', tags: [] },
        { assetid: '2', name: '测试饰品', market_hash_name: 'Test Item', amount: 1, tradable: false, marketable: false, icon_url: null, type: '武器', tags: [] }
      ] }) };
    }
    if (url.endsWith('/auth/logout')) return { ok: true, status: 200, json: async () => ({ authenticated: false }) };
    throw new Error(`Unexpected fetch: ${url}`);
  };
  dom.window.eval(fs.readFileSync(path.join(root, 'web/static/account.js'), 'utf8'));
  await new Promise(r => setTimeout(r, 30));
  return { dom, w: dom.window, d: dom.window.document, errors, calls };
}
test('bound user requests only own inventory and renders account and item states', async () => {
  const { dom, d, calls, errors } = await openPage({ authenticated: true });
  try {
    assert.match(d.querySelector('#accountIdentity').textContent, /76561198000000001/);
    assert.equal(d.querySelector('#steamLogin').hidden, true);
    assert.equal(d.querySelectorAll('.inventory-item').length, 2);
    assert.equal(calls.filter(c => c.url.endsWith('/api/inventory')).length, 1);
    assert.ok(calls.every(c => !c.url.includes('steamid=')));
    assert.equal(d.querySelectorAll('.inventory-badges .restricted').length, 2);
    assert.equal(errors.length, 0);
  } finally { dom.window.close(); }
});
test('logout uses session CSRF and immediately clears personal inventory', async () => {
  const { dom, d, calls } = await openPage({ authenticated: true });
  try {
    d.querySelector('#logoutButton').click();
    await new Promise(r => setTimeout(r, 15));
    const logout = calls.find(c => c.url.endsWith('/auth/logout'));
    assert.ok(logout, 'logout must call server');
    assert.equal(logout.options.method, 'POST');
    assert.equal(logout.options.headers['X-CSRF-Token'], 'fixture-csrf');
    assert.equal(d.querySelector('#personalInventory').hidden, true);
    assert.equal(d.querySelectorAll('.inventory-item').length, 0);
    assert.equal(d.querySelector('#steamLogin').hidden, false);
  } finally { dom.window.close(); }
});
test('logout cannot be followed by a late inventory response exposing prior account data', async () => {
  const { dom, w, d } = await openPage({ authenticated: true });
  try {
    const original = w.fetch;
    let release;
    w.fetch = (url, options) => url.endsWith('/api/inventory')
      ? new Promise(resolve => { release = resolve; }) : original(url, options);
    d.querySelector('#loadInventory').click();
    d.querySelector('#logoutButton').click();
    await new Promise(r => setTimeout(r, 10));
    release({ ok: true, status: 200, json: async () => ({ total_items: 1, asset_count: 1, fetched_at: '2026-11-03T06:00:00Z', items: [{ assetid: 'secret', name: 'Previous user private item', amount: 1 }] }) });
    await new Promise(r => setTimeout(r, 10));
    assert.equal(d.querySelectorAll('.inventory-item').length, 0);
    assert.ok(!d.body.textContent.includes('Previous user private item'));
  } finally { dom.window.close(); }
});
test('private, rate-limited, and unavailable inventories remain explicit errors, not empty success', async () => {
  for (const failure of [
    { status: 403, code: 'inventory_private', message: '库存为私密，请在 Steam 设置为公开后重试。' },
    { status: 429, code: 'rate_limited', message: 'Steam 请求过于频繁，请稍后重试。' },
    { status: 502, code: 'steam_unavailable', message: 'Steam 暂时不可用。' }
  ]) {
    const { dom, d } = await openPage({ authenticated: true, failure });
    try {
      assert.equal(d.querySelector('#inventoryMessage').dataset.state, 'error');
      assert.equal(d.querySelector('#inventoryMessage').textContent, failure.message);
      assert.equal(d.querySelectorAll('.inventory-item').length, 0);
      assert.equal(d.querySelector('#loadInventory').disabled, false);
    } finally { dom.window.close(); }
  }
});
test('item names render as text and untrusted image origins are rejected', async () => {
  const { dom, d } = await openPage({ authenticated: true, items: [{
    assetid: '1', name: '<img src=x onerror=alert(1)>', market_hash_name: '<script>bad()</script>',
    amount: 1, tradable: false, marketable: false, icon_url: 'https://evil.example/image'
  }] });
  try {
    assert.equal(d.querySelector('.inventory-item h3').textContent, '<img src=x onerror=alert(1)>');
    assert.equal(d.querySelectorAll('.inventory-item img').length, 0);
    assert.equal(d.querySelectorAll('.inventory-item script').length, 0);
  } finally { dom.window.close(); }
});
test('inventory search and status filter apply together', async () => {
  const { dom, w, d } = await openPage({ authenticated: true });
  try {
    const search = d.querySelector('#inventorySearch');
    search.value = 'Fracture'; search.dispatchEvent(new w.Event('input'));
    assert.equal(d.querySelectorAll('.inventory-item').length, 1);
    const filter = d.querySelector('#inventoryFilter');
    filter.value = 'restricted'; filter.dispatchEvent(new w.Event('change'));
    assert.equal(d.querySelectorAll('.inventory-item').length, 0);
    search.value = ''; search.dispatchEvent(new w.Event('input'));
    assert.equal(d.querySelectorAll('.inventory-item').length, 1);
    assert.match(d.querySelector('.inventory-item h3').textContent, /测试饰品/);
  } finally { dom.window.close(); }
});
test('account settings do not fetch inventory until user requests it', async () => {
  const { dom, calls } = await openPage({ authenticated: true, route: 'account' });
  try { assert.equal(calls.filter(c => c.url.endsWith('/api/inventory')).length, 0); }
  finally { dom.window.close(); }
});
test('unbound user sees Steam binding action without any inventory request', async () => {
  const { dom, d, calls, errors } = await openPage();
  try {
    assert.equal(calls.filter(c => c.url.endsWith('/api/inventory')).length, 0);
    const link = d.querySelector('#steamLogin');
    assert.ok(!link.hidden);
    assert.equal(new URL(link.href).pathname, '/cases/auth/steam');
    assert.match(d.querySelector('#accountStatus').textContent, /未绑定|未登录/);
    assert.equal(d.querySelectorAll('.inventory-item').length, 0);
    assert.equal(errors.length, 0);
  } finally { dom.window.close(); }
});
