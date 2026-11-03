// Isolated fixtures only. No requests or writes to Steam or the production API.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('/opt/hermes-agent/node_modules/jsdom');
const root = __dirname;
const cases = JSON.parse(fs.readFileSync(path.join(root, 'catalog.json'))).items.map((item, i) => ({
  ...item, image: '/static/images/Fracture_Case.png', lowest_sell_price: 10 + i,
  highest_buy_price: 9 + i, est_net_income: Math.round((10 + i) * 86) / 100,
  sell_order_count: 100 + i, observed_at: '2026-11-03T06:00:00+00:00'
}));
async function page(options = {}) {
  let html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
  html = html.replace(/<script[^>]+src=[^>]+><\/script>/g, '');
  const calls = [], charts = [], alerts = [], timers = [], errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', err => errors.push(err));
  const dom = new JSDOM(html, {
    url: 'http://localhost:8765/cases', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole,
    beforeParse(w) {
      w.HTMLCanvasElement.prototype.getContext = () => ({});
      w.Chart = class { constructor(ctx, config) { charts.push(config); } destroy() {} };
      w.alert = message => alerts.push(message);
      w.setInterval = callback => { timers.push(callback); return timers.length; };
      w.clearInterval = () => {};
      w.fetch = async (url, init) => {
        calls.push({ url, init });
        if (options.fail && String(url).endsWith('/api/cases')) throw new Error('test offline');
        return { ok: true, json: async () => String(url).includes('/api/history/')
          ? { history: options.history || [] }
          : String(url).includes('/api/refresh/status') ? { collector: { is_collecting: false } }
          : String(url).endsWith('/api/refresh') ? { message: 'test queued' }
          : { total: cases.length, cases, scheduler: { enabled: options.enabled ?? true, interval_seconds: 300 }, is_collecting: false } };
      };
    }
  });
  await new Promise(resolve => setTimeout(resolve, 20));
  return { dom, w: dom.window, d: dom.window.document, calls, charts, alerts, timers, errors };
}
const tick = () => new Promise(resolve => setTimeout(resolve, 10));
test('cards can be opened with Enter and expose a named dialog', async () => {
  const { dom, w, d, errors } = await page();
  try {
    const card = d.querySelector('.case-card');
    card.focus();
    card.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    assert.ok(d.querySelector('#calcModal').classList.contains('active'));
    assert.equal(d.querySelector('#calcModal').getAttribute('role'), 'dialog');
    assert.equal(d.querySelector('#calcModal').getAttribute('aria-labelledby'), 'modalCnName');
    assert.equal(d.activeElement.id, 'platformBuyPrice');
    assert.ok(d.body.classList.contains('modal-open'));
    assert.equal(errors.length, 0);
  } finally { dom.window.close(); }
});
test('renders every catalog case and keeps current defaults and registration IDs', async () => {
  const { dom, d, errors } = await page();
  try {
    assert.equal(d.querySelectorAll('.case-card').length, cases.length);
    assert.equal(d.querySelector('#resultCount').textContent, `${cases.length} / ${cases.length} 种`);
    assert.equal(d.querySelector('#globalTargetBalance').value, '298');
    assert.equal(d.querySelector('#globalTargetRatio').value, '0.78');
    assert.equal(d.querySelector('#coeffInput').value, '0.86');
    assert.match(d.body.textContent, /蜀ICP备2026036807号-1/);
    assert.match(d.body.textContent, /川公网安备51160202511983号/);
    assert.equal(d.querySelector('#casesGrid').getAttribute('aria-busy'), 'false');
    const ids = [...d.querySelectorAll('[id]')].map(n => n.id);
    assert.equal(new Set(ids).size, ids.length);
    assert.equal(errors.length, 0);
  } finally { dom.window.close(); }
});
test('search supports English and Chinese, with a helpful empty state', async () => {
  const { dom, w, d } = await page();
  try {
    for (const query of [cases[0].market_hash_name, cases[0].cn_name]) {
      d.querySelector('#searchInput').value = query;
      w.renderCases();
      assert.ok(d.querySelector('.case-card'));
      assert.equal(d.querySelector('.case-card').dataset.caseName, cases[0].market_hash_name);
    }
    d.querySelector('#searchInput').value = 'nonexistent-case-xyz';
    w.renderCases();
    assert.equal(d.querySelectorAll('.case-card').length, 0);
    assert.match(d.querySelector('.empty-state').textContent, /没有匹配/);
    assert.equal(d.querySelector('#resultCount').textContent, `0 / ${cases.length} 种`);
  } finally { dom.window.close(); }
});
test('all five existing sort modes remain wired', async () => {
  const { dom, w, d } = await page();
  try {
    for (const mode of ['price-asc', 'price-desc', 'buy-desc', 'sell-count-desc', 'count-asc']) {
      d.querySelector('#sortSelect').value = mode;
      w.renderCases();
      const names = [...d.querySelectorAll('.case-card')].map(c => c.dataset.caseName);
      const expected = [...cases].sort((a,b) => mode === 'count-asc'
        ? Math.ceil(298/a.est_net_income) - Math.ceil(298/b.est_net_income)
        : mode === 'price-asc' ? a.lowest_sell_price - b.lowest_sell_price
        : mode === 'price-desc' ? b.lowest_sell_price - a.lowest_sell_price
        : mode === 'buy-desc' ? b.highest_buy_price - a.highest_buy_price
        : b.sell_order_count - a.sell_order_count).map(c => c.market_hash_name);
      assert.deepEqual(names, expected, mode);
    }
  } finally { dom.window.close(); }
});
test('quick targets and target ratio update both card quantity and procurement cap', async () => {
  const { dom, w, d } = await page();
  try {
    w.updateTargetBalance(500);
    w.updateTargetRatio(0.8);
    const card = d.querySelector('.case-card');
    const item = cases.find(c => c.market_hash_name === card.dataset.caseName);
    assert.equal(d.querySelector('#globalTargetBalance').value, '500');
    assert.equal(d.querySelector('#globalTargetRatio').value, '0.8');
    assert.equal(card.querySelector('.count-highlight-val').textContent.trim(), `${Math.ceil(500/item.est_net_income)}个`);
    assert.equal(card.querySelector('.procurement-row .price-val').textContent, `¥${(Math.floor(item.est_net_income*0.8*100)/100).toFixed(2)}`);
  } finally { dom.window.close(); }
});
test('calculator formulas and fill-suggested-price preserve rounding semantics', async () => {
  const { dom, w, d } = await page();
  try {
    w.openModal(cases[0].market_hash_name);
    const input = d.querySelector('#platformBuyPrice');
    input.value = '7.00';
    w.recalc();
    const net = Math.round(cases[0].lowest_sell_price*0.86*100)/100;
    assert.equal(d.querySelector('#resDiscountText').innerText, (7/net*10).toFixed(2));
    assert.equal(d.querySelector('#resCost100').innerText, `¥${(7/net*100).toFixed(2)}`);
    assert.equal(d.querySelector('#planTotalCost').innerText, `¥${(Math.ceil(298/net)*7).toFixed(2)}`);
    d.querySelector('#suggestedPriceBtn').click();
    assert.equal(input.value, (Math.floor(net*0.78*100)/100).toFixed(2));
    assert.equal(d.activeElement, input);
  } finally { dom.window.close(); }
});
test('Escape closes, restores focus, and Tab stays inside the dialog', async () => {
  const { dom, w, d } = await page();
  try {
    const card = d.querySelector('.case-card');
    card.focus(); card.click();
    const close = d.querySelector('.close-btn');
    const last = d.querySelector('.history-drawer summary');
    last.focus();
    last.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
    assert.equal(d.activeElement, close);
    close.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }));
    assert.equal(d.activeElement, last);
    w.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape' }));
    assert.ok(!d.querySelector('#calcModal').classList.contains('active'));
    assert.ok(!d.body.classList.contains('modal-open'));
    assert.equal(d.activeElement, card);
  } finally { dom.window.close(); }
});
test('history controls preserve hour/day/month routes and pressed states', async () => {
  const { dom, w, d, calls, errors } = await page();
  try {
    w.openModal(cases[0].market_hash_name);
    for (const [id, view] of [['tabHour','hour'], ['tabDay','day'], ['tabMonth','month']]) {
      d.getElementById(id).click();
      assert.equal(d.getElementById(id).getAttribute('aria-pressed'), 'true');
      assert.ok(calls.some(c => c.url.endsWith(`?view=${view}`)));
    }
    await tick();
    assert.equal(errors.length, 0);
    assert.match(d.querySelector('#historyTableBody').textContent, /暂无历史记录/);
  } finally { dom.window.close(); }
});
test('one observed snapshot remains one plotted point, without invented history', async () => {
  const { dom, w, charts } = await page({ history: [{ observed_at: '2026-11-03T06:00:00Z', lowest_sell_price: 10, highest_buy_price: 9 }] });
  try {
    w.openModal(cases[0].market_hash_name); await tick();
    assert.equal(charts.at(-1).data.labels.length, 1);
    assert.deepEqual([...charts.at(-1).data.datasets[0].data], [10]);
  } finally { dom.window.close(); }
});
test('network failure shows a retry action instead of a blank page', async () => {
  const { dom, d } = await page({ fail: true });
  try {
    assert.match(d.querySelector('.empty-state').textContent, /暂时无法读取行情/);
    assert.equal(d.querySelector('.empty-state button').textContent, '重新加载行情');
    assert.equal(d.querySelector('#monitorStatus').dataset.state, 'error');
    assert.equal(d.querySelector('#casesGrid').getAttribute('aria-busy'), 'false');
  } finally { dom.window.close(); }
});
test('monitor status reflects API configuration rather than a hardcoded active badge', async () => {
  const { dom, d } = await page({ enabled: false });
  try {
    assert.equal(d.querySelector('#monitorText').textContent, '定时监控未开启');
    assert.notEqual(d.querySelector('#monitorStatus').dataset.state, 'enabled');
  } finally { dom.window.close(); }
});
test('refresh wiring remains POST then status polling (mocked only)', async () => {
  const { dom, w, d, calls, timers } = await page();
  try {
    await w.triggerRefresh();
    assert.ok(calls.some(c => c.url === '/cases/api/refresh' && c.init.method === 'POST'));
    assert.ok(d.querySelector('#refreshBtn').disabled);
    await timers[0]();
    await tick();
    assert.ok(!d.querySelector('#refreshBtn').disabled);
    assert.ok(d.querySelector('#refreshIcon svg'));
  } finally { dom.window.close(); }
});
