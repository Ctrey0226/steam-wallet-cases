/* No Steam credentials or session tokens are stored in browser storage. */
(() => {
  'use strict';
  const prefix = location.pathname.replace(/\/(?:account|inventory)\/?$/, '').replace(/\/$/, '');
  const url = path => `${prefix}${path}`;
  const el = id => document.getElementById(id);
  el('marketLink').href = `${prefix}/`;
  el('inventoryLink').href = url('/inventory');
  el('accountLink').href = url('/account');
  el(location.pathname.replace(/\/$/, '').endsWith('/account') ? 'accountLink' : 'inventoryLink').setAttribute('aria-current', 'page');
  let session = null;
  let inventory = [];
  const text = (tag, value, className) => {
    const node = document.createElement(tag);
    node.textContent = value;
    if (className) node.className = className;
    return node;
  };
  function renderInventory() {
    const query = el('inventorySearch').value.trim().toLowerCase();
    const filter = el('inventoryFilter').value;
    const visible = inventory.filter(item => {
      const matches = `${item.name || ''} ${item.market_hash_name || ''}`.toLowerCase().includes(query);
      return matches && (filter === 'all' || (filter === 'tradable' && item.tradable)
        || (filter === 'marketable' && item.marketable) || (filter === 'restricted' && !item.tradable));
    });
    el('inventoryCount').textContent = `${visible.length} / ${inventory.length} 条`;
    el('inventoryGrid').replaceChildren();
    for (const item of visible) {
      const card = document.createElement('article');
      card.className = 'inventory-item';
      const art = document.createElement('div');
      art.className = 'inventory-art';
      let icon = null;
      try {
        const imageUrl = new URL(item.icon_url);
        if (imageUrl.protocol === 'https:' && ['community.fastly.steamstatic.com', 'community.akamai.steamstatic.com', 'steamcommunity-a.akamaihd.net'].includes(imageUrl.hostname)) icon = imageUrl.href;
      } catch (_) { /* Missing or invalid image is not an inventory failure. */ }
      if (icon) {
        const img = document.createElement('img');
        img.src = icon; img.alt = ''; img.loading = 'lazy'; img.decoding = 'async';
        img.addEventListener('error', () => art.replaceChildren(text('span', '物品图片暂不可用')), { once: true });
        art.append(img);
      } else art.append(text('span', '暂无物品图片'));
      const content = document.createElement('div');
      content.className = 'inventory-item-content';
      content.append(text('h3', item.name || item.market_hash_name || '未命名物品'));
      content.append(text('p', item.market_hash_name || '', 'inventory-market-name'));
      const badges = document.createElement('div');
      badges.className = 'inventory-badges';
      badges.append(text('span', `数量 ${item.amount || 1}`));
      badges.append(text('span', item.tradable ? '可交易' : '交易受限', item.tradable ? 'available' : 'restricted'));
      badges.append(text('span', item.marketable ? '可上架市场' : '不可上架', item.marketable ? 'available' : 'restricted'));
      content.append(badges);
      content.append(text('p', `Asset ${item.assetid}`, 'inventory-item-details'));
      card.append(art, content);
      el('inventoryGrid').append(card);
    }
  }
  async function loadInventory() {
    if (!session?.authenticated) return;
    const requestedSession = session;
    el('loadInventory').disabled = true;
    el('inventoryGrid').setAttribute('aria-busy', 'true');
    el('inventoryMessage').hidden = false;
    el('inventoryMessage').dataset.state = 'loading';
    el('inventoryMessage').textContent = '正在从 Steam 读取可见库存，请稍候…';
    try {
      const response = await fetch(url('/api/inventory'), { credentials: 'same-origin', cache: 'no-store' });
      const data = await response.json();
      if (session !== requestedSession) return;
      if (!response.ok) throw new Error(data.detail?.message || '库存暂时无法读取，请稍后重试。');
      inventory = data.items || [];
      renderInventory();
      el('inventoryMeta').textContent = `${data.total_items} 件物品 · ${data.asset_count} 条记录 · ${new Date(data.fetched_at).toLocaleString('zh-CN')} 读取`;
      el('inventoryMessage').textContent = inventory.length ? '已读取当前 Steam 可见库存。本页不会执行交易操作。' : 'Steam 返回的可见库存为空。未公开或储存单元内的物品不一定包含在内。';
      el('inventoryMessage').dataset.state = 'success';
      el('loadInventory').textContent = '重新读取库存';
    } catch (error) {
      if (session !== requestedSession) return;
      inventory = [];
      el('inventoryGrid').replaceChildren();
      el('inventoryCount').textContent = '';
      el('inventoryMeta').textContent = '未能获取当前库存快照';
      el('inventoryMessage').textContent = error.message;
      el('inventoryMessage').dataset.state = 'error';
    } finally {
      el('loadInventory').disabled = false;
      el('inventoryGrid').setAttribute('aria-busy', 'false');
    }
  }
  el('loadInventory').addEventListener('click', loadInventory);
  el('inventorySearch').addEventListener('input', renderInventory);
  el('inventoryFilter').addEventListener('change', renderInventory);
  async function loadAccount() {
    el('retryAccount').hidden = true;
    try {
      const response = await fetch(url('/api/account'), { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) throw new Error('账户服务暂时无法连接，请稍后重试。');
      const account = await response.json();
      session = account;
      if (account.authenticated) el('accountIdentity').textContent = `已验证 SteamID · ${account.user.steamid}`;
      el('accountStatus').textContent = account.authenticated ? 'Steam 账户已绑定' : '尚未登录并绑定 Steam 账户';
      el('steamLogin').href = url('/auth/steam');
      el('steamLogin').hidden = !!account.authenticated;
      el('logoutButton').hidden = !account.authenticated;
      el('personalInventory').hidden = !account.authenticated;
      el('accountIdentity').hidden = !account.authenticated;
      if (account.authenticated && location.pathname.replace(/\/$/, '').endsWith('/inventory')) await loadInventory();
    } catch (error) {
      el('accountStatus').textContent = error.message;
      el('retryAccount').hidden = false;
    }
  }
  el('logoutButton').addEventListener('click', async () => {
    if (!session?.authenticated) return;
    el('logoutButton').disabled = true;
    try {
      const response = await fetch(url('/auth/logout'), {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'X-CSRF-Token': session.csrf_token }
      });
      if (!response.ok) throw new Error('退出失败，请重试；当前登录状态尚未改变。');
      session = null;
      inventory = [];
      el('inventoryGrid').replaceChildren();
      el('inventoryCount').textContent = '';
      el('personalInventory').hidden = true;
      el('accountIdentity').textContent = '';
      el('accountIdentity').hidden = true;
      el('logoutButton').hidden = true;
      el('steamLogin').hidden = false;
      el('accountStatus').textContent = '已退出本站账户。需要时可重新通过 Steam 登录。';
    } catch (error) {
      el('authFeedback').hidden = false;
      el('authFeedback').dataset.state = 'error';
      el('authFeedback').textContent = error.message;
    } finally {
      el('logoutButton').disabled = false;
    }
  });
  el('retryAccount').addEventListener('click', loadAccount);
  loadAccount();
})();
