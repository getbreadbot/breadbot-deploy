// ── Toast system ──────────────────────────────────────────────────────────────
const Toast = {
  show(msg, type='info', duration=3500) {
    const icon = type==='success'?'✓':type==='error'?'✗':'◈';
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `<span class="toast-icon">${icon}</span><span>${msg}</span>`;
    document.getElementById('toast-container').appendChild(el);
    setTimeout(() => { el.classList.add('toast-out'); setTimeout(()=>el.remove(), 300); }, duration);
  }
};

// ── Modal system ──────────────────────────────────────────────────────────────
const Modal = {
  _el: null,
  show(html) {
    this.close();
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'modal-overlay';
    overlay.innerHTML = html;
    overlay.addEventListener('click', e => { if(e.target===overlay) this.close(); });
    document.body.appendChild(overlay);
    this._el = overlay;
  },
  close() {
    const el = document.getElementById('modal-overlay');
    if(el) el.remove();
    this._el = null;
  }
};

// ── API wrapper ───────────────────────────────────────────────────────────────
const API = {
  async get(path) {
    const r = await fetch(path);
    if(!r.ok) throw new Error('API '+r.status+' '+path);
    return r.json();
  },
  async post(path, body={}) {
    const r = await fetch(path, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    const data = await r.json();
    if(!r.ok) throw new Error(data.detail || 'API '+r.status);
    return data;
  }
};


// ── Buy confirmation modal ────────────────────────────────────────────────────
const BuyModal = {
  show(alert) {
    const sym   = alert.symbol || alert.token_name || '?';
    const chain = (alert.chain||'?').toUpperCase();
    const size  = alert.position_size_usd || alert.size_usd || 0;
    const score = alert.rug_score || alert.security_score || 0;
    const price = alert.price_usd || 0;
    const flags = (alert.flags || alert.rug_flags || []);
    const flagStr = Array.isArray(flags) && flags.length
      ? flags.map(f=>`<span class="flag-item">${f}</span>`).join(' ')
      : '<span class="flag-item" style="color:var(--accent-green)">No flags</span>';
    const stop   = (price * 0.935).toFixed(8);
    const target = (price * 1.15).toFixed(8);

    Modal.show(`
      <div class="modal">
        <div class="modal-header">
          <span class="modal-title">CONFIRM BUY</span>
          <button class="modal-close" onclick="Modal.close()">✕</button>
        </div>
        <div class="modal-body">
          <div class="modal-token">${sym} <span style="font-size:14px;color:var(--text-muted)">${chain}</span></div>
          <div class="modal-row"><span class="modal-label">Position Size</span><span class="modal-val" style="color:var(--accent-green)">$${parseFloat(size).toFixed(2)}</span></div>
          <div class="modal-row"><span class="modal-label">Entry Price</span><span class="modal-val">${Fmt.price(price)}</span></div>
          <div class="modal-row"><span class="modal-label">Stop Loss</span><span class="modal-val" style="color:var(--accent-red)">$${stop} (−6.5%)</span></div>
          <div class="modal-row"><span class="modal-label">Take Profit</span><span class="modal-val" style="color:var(--accent-amber)">$${target} (+15%)</span></div>
          <div class="modal-row"><span class="modal-label">Security Score</span><span class="modal-val">${Fmt.scoreBadge(score)}</span></div>
          <div class="modal-row" style="flex-direction:column;gap:8px">
            <span class="modal-label">Flags</span>
            <div class="flags-list">${flagStr}</div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn btn-skip" onclick="Modal.close()">Cancel</button>
          <button class="btn btn-buy" id="confirm-buy-btn" onclick="BuyModal.confirm(${alert.id}, this)">
            ✓ Confirm Buy $${parseFloat(size).toFixed(2)}
          </button>
        </div>
      </div>
    `);
  },
  async confirm(alertId, btn) {
    btn.disabled = true; btn.textContent = 'Executing...';
    try {
      const res = await API.post('/api/action/decision', {alert_id: alertId, decision: 'buy'});
      Modal.close();
      if(res.ok) {
        Toast.show('Buy order placed — position opened', 'success');
        setTimeout(()=>App.renderPage(App.currentPage), 800);
      } else {
        Toast.show(res.msg || 'Could not place order', 'error');
      }
    } catch(e) {
      Toast.show('Error: '+e.message, 'error');
      btn.disabled = false; btn.textContent = 'Confirm Buy';
    }
  }
};

async function skipAlert(alertId) {
  try {
    const res = await API.post('/api/action/decision', {alert_id: alertId, decision: 'skip'});
    if(res.ok) { Toast.show('Alert skipped', 'info'); setTimeout(()=>App.renderPage('alerts'), 600); }
    else        Toast.show(res.msg||'Could not skip', 'error');
  } catch(e) { Toast.show('Error: '+e.message, 'error'); }
}

async function togglePause(isPaused) {
  try {
    const res = isPaused
      ? await API.post('/api/action/resume')
      : await API.post('/api/action/pause', {reason: 'Paused from dashboard'});
    Toast.show(res.paused ? 'Trading paused' : 'Trading resumed', res.paused ? 'info' : 'success');
    setTimeout(()=>App.renderPage(App.currentPage), 600);
  } catch(e) { Toast.show('Error: '+e.message, 'error'); }
}

async function closePosition(posId, sym) {
  if(!confirm(`Close position: ${sym}? This logs it as manually closed.`)) return;
  try {
    const res = await API.post('/api/action/close-position', {position_id: posId, reason: 'manual'});
    if(res.ok) { Toast.show(`${sym} closed`, 'success'); setTimeout(()=>App.renderPage('positions'), 600); }
    else        Toast.show(res.msg||'Could not close', 'error');
  } catch(e) { Toast.show('Error: '+e.message, 'error'); }
}

function countdownClose(posId, sym, btn) {
  if(btn.dataset.counting) return;
  btn.dataset.counting = '1';
  btn.classList.add('btn-countdown');
  let t = 2;
  btn.textContent = `Close (${t}s)`;
  const iv = setInterval(() => {
    t--;
    if(t > 0) { btn.textContent = `Close (${t}s)`; }
    else {
      clearInterval(iv);
      btn.classList.remove('btn-countdown');
      btn.textContent = '✕ Confirm?';
      btn.onclick = async () => {
        btn.disabled = true; btn.textContent = 'Closing...';
        try {
          const res = await API.post('/api/action/close-position', {position_id: posId, reason: 'manual'});
          if(res.ok) { Toast.show(`${sym} closed`, 'success'); setTimeout(()=>App.renderPage('positions'), 600); }
          else Toast.show(res.msg||'Could not close', 'error');
        } catch(e) { Toast.show('Error: '+e.message, 'error'); }
      };
      // Auto-reset after 5s if not clicked
      setTimeout(() => {
        if(!btn.disabled) {
          delete btn.dataset.counting;
          btn.textContent = '✕ Close';
          btn.onclick = () => countdownClose(posId, sym, btn);
        }
      }, 5000);
    }
  }, 1000);
}

async function forceScan() {
  try {
    const res = await API.post('/api/action/force-scan');
    Toast.show(res.msg || 'Scan queued', 'info');
  } catch(e) { Toast.show('Error: '+e.message, 'error'); }
}

async function resetDaily(btn) {
  if(!confirm('Reset all daily counters? This cannot be undone.')) return;
  btn.disabled = true; btn.textContent = 'Resetting...';
  try {
    const res = await API.post('/api/action/reset-daily');
    Toast.show(res.msg || 'Daily counters reset', 'success');
    setTimeout(()=>App.renderPage('controls'), 600);
  } catch(e) {
    Toast.show('Error: '+e.message, 'error');
    btn.disabled = false; btn.textContent = '↺ Reset Daily Counters';
  }
}


// ── App controller ────────────────────────────────────────────────────────────
const App = {
  currentPage: 'overview', refreshInterval: 30,
  refreshTimer: null, countdownTimer: null, countdown: 30,
  pages: ['overview','positions','alerts','yields','trades','flashloans','analytics','research','controls'],

  pendingAlertCount: 0,

  async init() {
    this.bindNav();
    this.bindKeyboard();
    const hash = window.location.hash.replace('#','') || 'overview';
    await this.navigate(hash);
    this.startRefreshLoop(); this.startCountdown();
    this.updateNotifyBar();
  },
  bindNav() {
    document.querySelectorAll('.nav-item').forEach(l => {
      l.addEventListener('click', async e => {
        e.preventDefault();
        const p = l.dataset.page;
        window.location.hash = p;
        await this.navigate(p);
      });
    });
    window.addEventListener('hashchange', async () => {
      const p = window.location.hash.replace('#','') || 'overview';
      await this.navigate(p);
    });
  },
  bindKeyboard() {
    document.addEventListener('keydown', e => {
      // Don't trigger if typing in an input/select
      if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT'||e.target.tagName==='TEXTAREA') return;
      // 1-6 navigate pages
      const num = parseInt(e.key);
      if(num >= 1 && num <= 9 && num <= this.pages.length) { e.preventDefault(); this.navigate(this.pages[num-1]); return; }
      // R = refresh
      if(e.key==='r'||e.key==='R') { e.preventDefault(); this.renderPage(this.currentPage); return; }
      // B/S on alert feed for hovered row
      if(this.currentPage==='alerts' && (e.key==='b'||e.key==='B'||e.key==='s'||e.key==='S')) {
        const hovered = document.querySelector('.alert-row:hover');
        if(!hovered) return;
        const id = parseInt(hovered.dataset.id);
        const alert = Pages._alertsData.find(a=>a.id===id);
        if(!alert || (alert.decision && alert.decision!=='pending')) return;
        e.preventDefault();
        if(e.key==='b'||e.key==='B') BuyModal.show(alert);
        else skipAlert(id);
      }
    });
  },

  async navigate(page) {
    if(!this.pages.includes(page)) page = 'overview';
    this.currentPage = page;
    document.querySelectorAll('.nav-item').forEach(l =>
      l.classList.toggle('active', l.dataset.page === page)
    );
    const c = document.getElementById('page-container');
    // Page transition fade
    c.classList.add('page-fade');
    await new Promise(r => setTimeout(r, 50));
    c.innerHTML = '<div class="loading-screen"><div class="loading-spinner"></div><div class="loading-text">LOADING</div></div>';
    c.classList.remove('page-fade');
    // Scroll to top
    document.querySelector('.main-content').scrollTo({top:0,behavior:'instant'});
    await this.renderPage(page);
  },
  async renderPage(page) {
    const c = document.getElementById('page-container');
    try {
      switch(page) {
        case 'overview':   await Pages.overview(c);   break;
        case 'positions':  await Pages.positions(c);  break;
        case 'alerts':     await Pages.alerts(c);     break;
        case 'yields':     await Pages.yields(c);     break;
        case 'trades':     await Pages.trades(c);     break;
        case 'flashloans': await Pages.flashloans(c); break;
        case 'analytics':  await Pages.analytics(c);  break;
        case 'research':   await Pages.research(c);   break;
        case 'controls':   await Pages.controls(c);   break;
      }
    } catch(e) {
      c.innerHTML = `<div class="empty-state"><div class="empty-state-icon">⚠</div><div class="empty-state-title">ERROR</div><div class="empty-state-msg">${e.message}</div></div>`;
    }
  },
  startRefreshLoop() {
    if(this.refreshTimer) clearInterval(this.refreshTimer);
    this.refreshTimer = setInterval(async () => {
      await this.renderPage(this.currentPage);
      this.resetCountdown();
      this.updateNotifyBar();
    }, this.refreshInterval * 1000);
  },
  startCountdown() {
    this.countdown = this.refreshInterval;
    if(this.countdownTimer) clearInterval(this.countdownTimer);
    this.countdownTimer = setInterval(() => {
      this.countdown--;
      const el = document.getElementById('refresh-countdown');
      if(el) el.textContent = this.countdown + 's';
      if(this.countdown <= 0) this.countdown = this.refreshInterval;
    }, 1000);
  },
  resetCountdown() {
    this.countdown = this.refreshInterval; this.startCountdown();
    const el = document.getElementById('last-updated');
    if(el) el.textContent = new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  },
  setStatusBadge(active) {
    const dot = document.getElementById('status-dot'), lbl = document.getElementById('status-label');
    if(!dot||!lbl) return;
    dot.className = 'status-dot ' + (active ? 'active' : 'paused');
    lbl.textContent = active ? 'ACTIVE' : 'PAUSED';
  },

  updateAlertBadge(count) {
    this.pendingAlertCount = count;
    const nav = document.querySelector('.nav-item[data-page="alerts"]');
    if(!nav) return;
    let badge = nav.querySelector('.nav-badge');
    if(count > 0) {
      if(!badge) { badge = document.createElement('span'); badge.className='nav-badge'; nav.appendChild(badge); }
      badge.textContent = count;
    } else if(badge) { badge.remove(); }
  },

  async updateNotifyBar() {
    try {
      const d = await API.get('/api/alerts?days=1&decision=pending');
      const pending = (d.alerts||[]).filter(a=>!a.decision||a.decision==='pending');
      this.updateAlertBadge(pending.length);
      const bar = document.getElementById('top-notify-bar');
      if(!bar) return;
      if(pending.length > 0) {
        const a = pending[0];
        const sym = a.token_name||a.symbol||'?';
        bar.innerHTML = `<div class="top-notify-bar">
          <span class="notify-icon">◈</span>
          <span class="notify-text">Pending alert: <span class="notify-token">${sym}</span> on ${(a.chain||'?').toUpperCase()} — Score ${a.rug_score||a.security_score||'?'}</span>
          <button class="btn btn-buy btn-sm" onclick='BuyModal.show(${JSON.stringify(a)})'>✓ Buy</button>
          <button class="notify-dismiss" onclick="document.getElementById('top-notify-bar').innerHTML=''">✕</button>
        </div>`;
      } else { bar.innerHTML = ''; }
    } catch(e) { /* silent */ }
  }
};


// ── Pages ─────────────────────────────────────────────────────────────────────
const Pages = {

  // ── Overview ─────────────────────────────────────────────────────────────
  async overview(c) {
    const [d, tradeData, balData, flData] = await Promise.all([
      API.get('/api/status'),
      API.get('/api/trades').catch(()=>({trades:[],cumulative_pnl:[]})),
      API.get('/api/balances').catch(()=>({})),
      API.get('/api/flashloans').catch(()=>({summary:{}})),
    ]);
    App.setStatusBadge(d.trading_active);
    App.updateNotifyBar();
    const pnl = Fmt.pnl(d.daily_pnl);
    const lp  = Math.min((d.daily_loss_pct_used||0)*100, 100);
    const pauseBtn = d.trading_paused
      ? `<button class="btn btn-amber btn-paused" onclick="togglePause(true)">▶ Resume Trading</button>`
      : `<button class="btn btn-ghost" onclick="togglePause(false)">⏸ Pause Trading</button>`;

    // Weekly P&L data for sparkline
    const cum = tradeData.cumulative_pnl||[];
    const weekPnl = cum.slice(-7);

    // Portfolio utilization: open positions cost basis / total portfolio
    const portUtil = d.portfolio_utilization != null ? d.portfolio_utilization : null;

    c.innerHTML = `
      <div class="page-header">
        <div class="page-header-left">
          <div class="page-title">OVERVIEW</div>
          <div class="page-subtitle">${new Date().toLocaleDateString('en-US',{weekday:'long',month:'long',day:'numeric',year:'numeric'})}</div>
        </div>
        <div class="page-header-right">
          ${pauseBtn}
          <button class="btn btn-ghost" onclick="forceScan()">⟳ Force Scan</button>
          <button class="btn btn-ghost btn-sm" onclick="App.renderPage('overview')">↻ Refresh</button>
        </div>
      </div>
      ${!d.db_exists ? '<div class="no-data-banner">Bot has not run yet. Start main.py to begin collecting data.</div>' : ''}
      ${d.trading_paused ? `<div class="no-data-banner" style="border-color:var(--accent-amber)">Trading is paused. ${d.pause_reason||''}</div>` : ''}
      <div class="stat-grid">
        <div class="stat-card"><div class="stat-label">BOT STATUS</div>
          <div class="stat-value ${d.trading_active?'positive':'amber'}">${d.trading_active?'ACTIVE':'PAUSED'}</div></div>
        <div class="stat-card"><div class="stat-label">TODAY P&L</div>
          <div class="stat-value ${pnl.cls}">${pnl.text}</div>
          <div class="stat-sparkline"><canvas id="pnl-sparkline" width="140" height="32"></canvas></div></div>
        <div class="stat-card"><div class="stat-label">DAILY LOSS LIMIT</div>
          <div class="stat-value ${lp>=80?'negative':''}">${Fmt.pct(lp)}</div>
          <div class="progress-bar-wrap"><div class="progress-bar-track"><div class="progress-bar-fill ${lp>=80?'danger':''}" style="width:${lp}%"></div></div></div></div>
        <div class="stat-card"><div class="stat-label">PORTFOLIO UTILIZATION</div>
          <div class="stat-value ${portUtil!=null&&portUtil>80?'amber':''}">${portUtil!=null?Fmt.pct(portUtil):'—'}</div>
          ${portUtil!=null?`<div class="progress-bar-wrap"><div class="progress-bar-track"><div class="progress-bar-fill" style="width:${Math.min(portUtil,100)}%"></div></div></div>`:''}</div>
        <div class="stat-card"><div class="stat-label">OPEN POSITIONS</div>
          <div class="stat-value">${d.open_positions??'—'}</div></div>
        <div class="stat-card"><div class="stat-label">ALERTS TODAY</div>
          <div class="stat-value">${d.alerts_today??'—'}</div></div>
        <div class="stat-card"><div class="stat-label">BUYS TODAY</div>
          <div class="stat-value positive">${d.buys_today??'—'}</div></div>
        <div class="stat-card"><div class="stat-label">LAST SCAN</div>
          <div class="stat-value" style="font-size:14px">${Fmt.ago(d.last_scan)}</div>
          <div class="stat-meta">${d.last_scan?Fmt.datetime(d.last_scan):'—'}</div></div>
      </div>
      ${balData.base || balData.solana ? `
      <div class="section-label">CHAIN BALANCES</div>
      <div class="stat-grid">
        <div class="stat-card"><div class="stat-label">BASE — ETH</div>
          <div class="stat-value">${balData.base?.eth ?? '—'}</div></div>
        <div class="stat-card"><div class="stat-label">BASE — USDC</div>
          <div class="stat-value positive">${Fmt.usd(balData.base?.usdc)}</div></div>
        <div class="stat-card"><div class="stat-label">SOLANA — SOL</div>
          <div class="stat-value">${balData.solana?.sol ?? '—'}</div></div>
        <div class="stat-card"><div class="stat-label">SOLANA — USDC</div>
          <div class="stat-value positive">${Fmt.usd(balData.solana?.usdc)}</div></div>
        <div class="stat-card"><div class="stat-label">TOTAL USDC</div>
          <div class="stat-value positive">${Fmt.usd(balData.total_usdc)}</div></div>
        <div class="stat-card"><div class="stat-label">FLASH LOAN CONTRACT</div>
          <div class="stat-value">${balData.base?.flash_loan_contract_eth ?? '—'} ETH</div></div>
      </div>` : ''}
      ${flData.summary?.total_attempts ? `
      <div class="section-label">FLASH LOAN ARBS</div>
      <div class="stat-grid">
        <div class="stat-card"><div class="stat-label">SUCCESSFUL / TOTAL</div>
          <div class="stat-value positive">${flData.summary.successful}/${flData.summary.total_attempts}</div></div>
        <div class="stat-card"><div class="stat-label">PROFIT</div>
          <div class="stat-value positive">${Fmt.usd(flData.summary.total_profit_usdc)}</div></div>
        <div class="stat-card"><div class="stat-label">SUCCESS RATE</div>
          <div class="stat-value">${Fmt.pct(flData.summary.success_rate)}</div></div>
      </div>` : ''}`;

    // Render weekly P&L sparkline
    if(weekPnl.length > 1) {
      Charts.sparkline('pnl-sparkline', weekPnl.map(d=>({apy:d.pnl,date:d.date})), (d.daily_pnl||0)>=0?'#22c55e':'#ef4444');
    }
  },


  // ── Positions ─────────────────────────────────────────────────────────────
  _priceRefreshTimer: null,

  async positions(c) {
    const d = await API.get('/api/positions');
    const open = d.open||[], closed = d.closed||[];

    // Fetch live prices for open positions
    let prices = {};
    try {
      const priceData = await API.get('/api/prices');
      prices = priceData.prices || {};
    } catch(e) {}
    Pages._positionPrices = prices;
    Pages._positionPriceFetchedAt = Date.now();

    const oRows = open.length ? open.map((p,i) => {
      const tokenAddr = p.token_address || '';
      const priceInfo = prices[tokenAddr] || null;
      const currentPrice = priceInfo ? priceInfo.price_usd : (p.last_price || p.current_price || null);
      const entry = p.entry_price || 0;
      const qty = entry > 0 ? (p.cost_basis||p.cost_basis_usd||0) / entry : 0;
      let unrealizedHtml = '<span class="td-muted">—</span>';
      if(currentPrice && entry > 0) {
        const unrealized = (currentPrice - entry) * qty;
        const pnl = Fmt.pnl(unrealized);
        unrealizedHtml = `<span class="${pnl.cls}">${pnl.text}</span>`;
      }
      const curPriceHtml = currentPrice ? Fmt.price(currentPrice) : '<span class="td-muted">—</span>';
      return `
      <tr style="animation-delay:${i*.04}s">
        <td class="td-mono">${p.symbol||'?'}</td>
        <td>${Fmt.chainBadge(p.chain)}</td>
        <td class="td-mono">${Fmt.price(p.entry_price)}</td>
        <td class="td-mono">${curPriceHtml}</td>
        <td class="td-mono">${Fmt.usd(p.cost_basis||p.cost_basis_usd)}</td>
        <td class="td-mono">${unrealizedHtml}</td>
        <td class="td-mono" style="color:var(--accent-red)">${Fmt.price(p.stop_loss||p.stop_loss_usd)}</td>
        <td class="td-mono" style="color:var(--accent-amber)">${Fmt.price(p.take_profit||p.take_profit_25)}</td>
        <td class="td-muted">${Fmt.ago(p.opened_at)}</td>
        <td class="action-cell">
          <button class="btn btn-danger btn-sm" id="close-btn-${p.id}" onclick="countdownClose(${p.id},'${(p.symbol||'?').replace(/'/g,"\\'")}',this)">✕ Close</button>
        </td>
      </tr>`;
    }).join('')
      : `<tr><td colspan="10"><div class="empty-state"><div class="empty-state-title">NO OPEN POSITIONS</div></div></td></tr>`;

    const cRows = closed.length ? closed.map((p,i) => `
      <tr style="animation-delay:${i*.03}s">
        <td class="td-mono">${p.symbol||'?'}</td>
        <td>${Fmt.chainBadge(p.chain)}</td>
        <td class="td-mono">${Fmt.price(p.entry_price)}</td>
        <td class="td-mono">${Fmt.usd(p.cost_basis||p.cost_basis_usd)}</td>
        <td class="td-mono" style="text-transform:uppercase;color:var(--text-secondary)">${p.status}</td>
        <td class="td-muted">${Fmt.datetime(p.closed_at)}</td>
      </tr>`).join('')
      : `<tr><td colspan="6"><div class="empty-state"><div class="empty-state-title">NO CLOSED POSITIONS</div></div></td></tr>`;

    c.innerHTML = `
      <div class="page-header">
        <div class="page-header-left">
          <div class="page-title">POSITIONS</div>
          <div class="page-subtitle">${open.length} open · ${closed.length} recently closed</div>
        </div>
        <div class="page-header-right">
          <button class="btn btn-ghost btn-sm" onclick="App.renderPage('positions')">↻ Refresh</button>
        </div>
      </div>
      <div class="section-label">OPEN POSITIONS</div>
      <div class="table-wrap"><table>
        <thead><tr><th>TOKEN</th><th>CHAIN</th><th>ENTRY</th><th>CURRENT</th><th>SIZE</th><th>UNREAL. P&L</th><th>STOP</th><th>TARGET</th><th>OPENED</th><th>ACTION</th></tr></thead>
        <tbody>${oRows}</tbody>
      </table></div>
      <div class="td-muted" style="margin-bottom:16px;font-family:var(--font-mono);font-size:10px" id="price-updated-msg">
        Prices last updated ${Pages._positionPriceFetchedAt ? Math.round((Date.now()-Pages._positionPriceFetchedAt)/1000)+'s ago' : '—'} · Auto-refreshes every 60s
      </div>
      <div class="section-label">RECENTLY CLOSED</div>
      <div class="table-wrap"><table>
        <thead><tr><th>TOKEN</th><th>CHAIN</th><th>ENTRY</th><th>SIZE</th><th>EXIT TYPE</th><th>CLOSED</th></tr></thead>
        <tbody>${cRows}</tbody>
      </table></div>`;

    // Auto-refresh prices every 60s
    if(Pages._priceRefreshTimer) clearInterval(Pages._priceRefreshTimer);
    Pages._priceRefreshTimer = setInterval(async () => {
      if(App.currentPage === 'positions') {
        try {
          const pd = await API.get('/api/prices');
          Pages._positionPrices = pd.prices || {};
          Pages._positionPriceFetchedAt = Date.now();
          const msg = document.getElementById('price-updated-msg');
          if(msg) msg.textContent = 'Prices last updated 0s ago · Auto-refreshes every 60s';
        } catch(e) {}
      } else { clearInterval(Pages._priceRefreshTimer); }
    }, 60000);
  },


  // ── Alert Feed ────────────────────────────────────────────────────────────
  async alerts(c) {
    // Render shell with filters first, then populate
    c.innerHTML = `
      <div class="page-header">
        <div class="page-header-left">
          <div class="page-title">ALERT FEED</div>
          <div class="page-subtitle" id="alert-subtitle">Loading...</div>
        </div>
        <div class="page-header-right">
          <button class="btn btn-ghost btn-sm" onclick="Pages._reloadAlerts()">↻ Refresh</button>
        </div>
      </div>
      <div class="filter-bar">
        <span class="filter-label">Filter:</span>
        <select class="filter-select" id="filter-days" onchange="Pages._reloadAlerts()">
          <option value="1">Last 24h</option>
          <option value="7" selected>Last 7 days</option>
          <option value="30">Last 30 days</option>
        </select>
        <select class="filter-select" id="filter-decision" onchange="Pages._reloadAlerts()">
          <option value="">All decisions</option>
          <option value="pending">Pending</option>
          <option value="buy">Bought</option>
          <option value="skip">Skipped</option>
        </select>
        <select class="filter-select" id="filter-chain" onchange="Pages._filterAlertsByChain()">
          <option value="">All chains</option>
          <option value="solana">Solana</option>
          <option value="base">Base</option>
        </select>
        <span class="filter-count" id="alert-count"></span>
      </div>
      <div id="alerts-table-wrap"></div>`;

    await Pages._reloadAlerts();
  },

  _alertsData: [],

  async _reloadAlerts() {
    const days     = document.getElementById('filter-days')?.value || 7;
    const decision = document.getElementById('filter-decision')?.value || '';
    const url      = `/api/alerts?days=${days}${decision?'&decision='+decision:''}`;
    try {
      const d = await API.get(url);
      Pages._alertsData = d.alerts || [];
      Pages._renderAlertsTable(Pages._alertsData);
      const sub = document.getElementById('alert-subtitle');
      if(sub) sub.textContent = `Last ${days} days · ${Pages._alertsData.length} alerts · Click row to expand`;
    } catch(e) {
      Toast.show('Could not load alerts: '+e.message, 'error');
    }
  },

  _filterAlertsByChain() {
    const chain = document.getElementById('filter-chain')?.value || '';
    const filtered = chain
      ? Pages._alertsData.filter(a => (a.chain||'').toLowerCase() === chain)
      : Pages._alertsData;
    Pages._renderAlertsTable(filtered);
  },

  _renderAlertsTable(alerts) {
    const wrap = document.getElementById('alerts-table-wrap');
    if(!wrap) return;
    const cnt = document.getElementById('alert-count');
    if(cnt) cnt.textContent = alerts.length + ' results';

    // Float pending alerts to top
    const sorted = [...alerts].sort((a,b) => {
      const ap = !a.decision || a.decision==='pending' ? 0 : 1;
      const bp = !b.decision || b.decision==='pending' ? 0 : 1;
      return ap - bp;
    });

    // Count pending for badge
    const pendingCount = sorted.filter(a=>!a.decision||a.decision==='pending').length;
    App.updateAlertBadge(pendingCount);

    const rows = sorted.length ? sorted.map((a, i) => {
      const flags = (a.flags || a.rug_flags || []);
      const flagArr = Array.isArray(flags) ? flags : (flags ? String(flags).split(',') : []);
      const flagHtml = flagArr.length
        ? flagArr.map(f=>`<span class="flag-item">${f.trim()}</span>`).join('')
        : '<span class="flag-item" style="color:var(--accent-green)">No flags</span>';
      const isPending = !a.decision || a.decision === 'pending';
      const actionBtns = isPending
        ? `<div class="action-cell">
            <button class="btn btn-buy btn-sm" onclick='BuyModal.show(${JSON.stringify(a)})'>✓ Buy</button>
            <button class="btn btn-skip btn-sm" onclick="skipAlert(${a.id})">✕ Skip</button>
           </div>`
        : Fmt.decisionBadge(a.decision);
      const contractAddr = a.contract_address || a.token_address || '';
      const contractHtml = contractAddr
        ? `<div style="margin-top:8px"><div style="font-family:var(--font-mono);font-size:10px;color:var(--text-muted);letter-spacing:.1em;margin-bottom:4px">CONTRACT</div><div style="font-family:var(--font-mono);font-size:11px;color:var(--text-secondary);word-break:break-all">${contractAddr}</div></div>`
        : '';
      return `
        <tr class="alert-row${isPending?' alert-pending':''}" data-id="${a.id}" style="animation-delay:${Math.min(i,20)*.03}s;cursor:pointer">
          <td class="td-muted">${Fmt.ago(a.created_at)}</td>
          <td class="td-mono" style="color:var(--text-primary)">${a.token_name||a.symbol||'?'} <span style="color:var(--text-muted)">${a.symbol||''}</span></td>
          <td>${Fmt.chainBadge(a.chain)}</td>
          <td>${Fmt.scoreBadge(a.rug_score||a.security_score)}</td>
          <td class="td-mono">${Fmt.usd(a.liquidity)}</td>
          <td class="td-mono">${Fmt.usd(a.volume_24h)}</td>
          <td>${actionBtns}</td>
          <td class="td-muted" style="cursor:pointer">▾</td>
        </tr>
        <tr class="row-detail" id="detail-${a.id}"><td colspan="8">
          <div style="margin-bottom:4px;font-family:var(--font-mono);font-size:10px;color:var(--text-muted);letter-spacing:.1em">RUG FLAGS</div>
          <div class="flags-list">${flagHtml}</div>
          ${contractHtml}
        </td></tr>`;
    }).join('') : `<tr><td colspan="8"><div class="empty-state"><div class="empty-state-icon">◈</div><div class="empty-state-title">NO ALERTS MATCH YOUR FILTERS</div></div></td></tr>`;

    wrap.innerHTML = `
      <div class="table-wrap"><table>
        <thead><tr><th>TIME</th><th>TOKEN</th><th>CHAIN</th><th>SCORE</th><th>LIQUIDITY</th><th>VOL 24H</th><th>DECISION / ACTION</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <div style="font-family:var(--font-mono);font-size:10px;color:var(--text-muted);margin-top:8px">Keyboard: <span class="kbd-hint">B</span> Buy · <span class="kbd-hint">S</span> Skip (hover a row)</div>`;

    wrap.querySelectorAll('.alert-row').forEach(r => {
      r.querySelector('td:last-child')?.addEventListener('click', () => {
        document.getElementById('detail-'+r.dataset.id)?.classList.toggle('open');
      });
    });
  },


  // ── Yields ────────────────────────────────────────────────────────────────
  async yields(c) {
    const d   = await API.get('/api/yields');
    const cur = d.current||[], hist = d.history||{};
    const best = cur.length ? Math.max(...cur.map(y=>y.apy)) : 0;
    const bestPlatform = cur.find(y=>y.apy===best);

    const cards = cur.length ? cur.map((y,i) => {
      const isBest = y.apy === best;
      const mo1k   = ((y.apy/100)*1000/12).toFixed(2);
      const mo20k  = ((y.apy/100)*20000/12).toFixed(0);
      const mo25k  = ((y.apy/100)*25000/12).toFixed(0);
      return `
        <div class="yield-card ${isBest?'yield-best':''}" style="animation-delay:${i*.06}s">
          <div class="yield-platform">${y.platform}${isBest?'<span class="best-badge">BEST</span>':''}</div>
          <div style="font-family:var(--font-mono);font-size:10px;color:var(--text-muted);margin-bottom:4px">${y.asset}</div>
          <div class="yield-apy">${Fmt.pct(y.apy,2)}</div>
          <div class="yield-monthly">~$${mo1k}/mo per $1,000</div>
          <div class="yield-monthly">~$${mo20k}/mo on $20,000</div>
          <div class="yield-monthly">~$${mo25k}/mo on $25,000</div>
          <div class="yield-meta" style="margin-top:8px">${y.notes||''}</div>
          <canvas id="spark-${i}" width="200" height="40" style="margin-top:12px"></canvas>
        </div>`;
    }).join('') : '<div class="no-data-banner">No yield data yet. Yield monitor runs every hour.</div>';

    // Recommendation box
    const recHtml = bestPlatform ? `
      <div class="recommendation-box">
        <div class="rec-title">◈ Best For Your Capital</div>
        <div class="rec-body">
          At <strong>${Fmt.pct(bestPlatform.apy,2)}</strong> APY, <strong>${bestPlatform.platform}</strong> (${bestPlatform.asset}) is currently the best rate.
          A $20,000 allocation earns ~<strong>$${((bestPlatform.apy/100)*20000/12).toFixed(0)}/mo</strong>.
        </div>
      </div>` : '';

    c.innerHTML = `
      <div class="page-header">
        <div class="page-header-left">
          <div class="page-title">YIELD MONITOR</div>
          <div class="page-subtitle">Stablecoin yields · Updated hourly</div>
        </div>
        <div class="page-header-right">
          <button class="btn btn-ghost btn-sm" onclick="App.renderPage('yields')">↻ Refresh</button>
        </div>
      </div>
      ${recHtml}
      <div class="yield-grid">${cards}</div>`;
    cur.forEach((y,i)=>{
      const h = hist[y.platform]||[];
      if(h.length > 1) Charts.sparkline(`spark-${i}`, h, y.apy===best?'#22c55e':'#f59e0b');
    });
  },

  // ── Trades ────────────────────────────────────────────────────────────────
  _tradesCache: [],

  async trades(c) {
    const d      = await API.get('/api/trades');
    const trades = d.trades||[], cum = d.cumulative_pnl||[];
    Pages._tradesCache = trades;

    const rows = trades.length ? trades.map((t,i) => {
      const pnl = t.pnl_usd != null ? Fmt.pnl(t.pnl_usd) : {text:'—', cls:''};
      const ac  = t.action==='buy'?'action-buy':t.action==='stop_loss'?'action-stop':'action-sell';
      const rowClass = t.pnl_usd != null ? (t.pnl_usd >= 0 ? 'trade-positive' : 'trade-negative') : '';
      return `<tr class="${rowClass}" style="animation-delay:${Math.min(i,20)*.03}s">
        <td class="td-muted">${Fmt.datetime(t.executed_at)}</td>
        <td class="${ac}">${(t.action||'').toUpperCase().replace('_',' ')}</td>
        <td class="td-mono">${t.symbol||'?'}</td>
        <td>${Fmt.chainBadge(t.chain)}</td>
        <td class="td-mono">${Fmt.price(t.price_usd)}</td>
        <td class="td-mono">${Fmt.usd(t.usd_value)}</td>
        <td class="td-mono ${pnl.cls}">${pnl.text}</td>
        <td class="td-muted">${t.exchange||'—'}</td>
      </tr>`;
    }).join('') : `<tr><td colspan="8"><div class="empty-state"><div class="empty-state-icon">▷</div><div class="empty-state-title">NO TRADES YET</div></div></td></tr>`;

    c.innerHTML = `
      <div class="page-header">
        <div class="page-header-left">
          <div class="page-title">TRADE LOG</div>
          <div class="page-subtitle">${trades.length} total trades</div>
        </div>
        <div class="page-header-right">
          ${trades.length ? `<button class="btn btn-ghost btn-sm" onclick="Pages.exportCSV()">⤓ Export CSV</button>` : ''}
          <button class="btn btn-ghost btn-sm" onclick="App.renderPage('trades')">↻ Refresh</button>
        </div>
      </div>
      ${cum.length > 1 ? `<div class="chart-wrap"><div class="chart-title">CUMULATIVE P&L</div><canvas id="pnl-chart" height="80"></canvas></div>` : ''}
      <div class="table-wrap"><table>
        <thead><tr><th>DATE</th><th>ACTION</th><th>TOKEN</th><th>CHAIN</th><th>PRICE</th><th>AMOUNT</th><th>P&L</th><th>EXCHANGE</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
    if(cum.length > 1) Charts.pnlLine('pnl-chart', cum);
  },

  exportCSV() {
    // Use server-side CSV export for proper tax format
    const a = document.createElement('a');
    a.href = '/api/export/trades-csv';
    a.download = '';
    document.body.appendChild(a); a.click(); a.remove();
    Toast.show('Downloading trades CSV...', 'success');
  },


  // ── Controls ──────────────────────────────────────────────────────────────
  async controls(c) {
    const [status, stats, cfg, execCfg] = await Promise.all([
      API.get('/api/status'),
      API.get('/api/stats'),
      API.get('/api/bot-config'),
      API.get('/api/execution-config').catch(()=>({})),
    ]);
    const isPaused = status.trading_paused || cfg.paused;

    c.innerHTML = `
      <div class="page-header">
        <div class="page-header-left">
          <div class="page-title">CONTROLS</div>
          <div class="page-subtitle">Bot management · Trade execution · Settings</div>
        </div>
        <div class="page-header-right">
          <button class="btn btn-ghost btn-sm" onclick="App.renderPage('controls')">↻ Refresh</button>
        </div>
      </div>

      <div class="control-grid">

        <!-- Trading toggle -->
        <div class="control-card">
          <div class="control-card-title">Trading State</div>
          <div class="control-status-val ${isPaused?'amber':'positive'}">${isPaused?'PAUSED':'ACTIVE'}</div>
          ${isPaused && cfg.pause_reason ? `<div class="control-card-desc">${cfg.pause_reason}</div>` : ''}
          <div class="btn-group">
            ${isPaused
              ? `<button class="btn btn-amber" onclick="togglePause(true)">▶ Resume Trading</button>`
              : `<button class="btn btn-ghost" onclick="togglePause(false)">⏸ Pause Trading</button>`}
          </div>
        </div>

        <!-- Scanner -->
        <div class="control-card">
          <div class="control-card-title">Scanner</div>
          <div class="control-card-desc">Last scan: ${Fmt.ago(status.last_scan)}<br>Runs automatically every 5 minutes.</div>
          <div class="btn-group">
            <button class="btn btn-ghost" onclick="forceScan()">⟳ Force Scan Now</button>
          </div>
        </div>

        <!-- All-time stats -->
        <div class="control-card">
          <div class="control-card-title">All-Time Stats</div>
          <div class="stat-row"><span class="stat-row-label">Total Trades</span><span class="stat-row-val">${stats.total_trades??'—'}</span></div>
          <div class="stat-row"><span class="stat-row-label">Win Rate</span><span class="stat-row-val ${(stats.win_rate||0)>=50?'positive':'negative'}">${stats.win_rate??'—'}%</span></div>
          <div class="stat-row"><span class="stat-row-label">Total P&L</span>
            <span class="stat-row-val ${(stats.total_pnl||0)>=0?'positive':'negative'}">${Fmt.pnl(stats.total_pnl).text}</span></div>
          <div class="stat-row"><span class="stat-row-label">Buy Rate</span><span class="stat-row-val">${stats.buy_rate??'—'}%</span></div>
          <div class="stat-row"><span class="stat-row-label">Open Positions</span><span class="stat-row-val">${stats.open_positions??'—'}</span></div>
        </div>

        <!-- Today stats -->
        <div class="control-card">
          <div class="control-card-title">Today</div>
          <div class="stat-row"><span class="stat-row-label">Alerts</span><span class="stat-row-val">${status.alerts_today??0}</span></div>
          <div class="stat-row"><span class="stat-row-label">Buys</span><span class="stat-row-val positive">${status.buys_today??0}</span></div>
          <div class="stat-row"><span class="stat-row-label">Skips</span><span class="stat-row-val">${status.skips_today??0}</span></div>
          <div class="stat-row">
            <span class="stat-row-label">P&L</span>
            <span class="stat-row-val ${(status.daily_pnl||0)>=0?'positive':'negative'}">${Fmt.pnl(status.daily_pnl).text}</span>
          </div>
          <div class="stat-row">
            <span class="stat-row-label">Loss Limit Used</span>
            <span class="stat-row-val ${(status.daily_loss_pct_used||0)>=80?'negative':''}">${Fmt.pct(status.daily_loss_pct_used||0)}</span>
          </div>
        </div>

        <!-- Yield summary -->
        <div class="control-card">
          <div class="control-card-title">Best Yield Available</div>
          <div class="control-status-val" style="color:var(--accent-green)">${stats.best_yield_apy??'—'}%</div>
          <div class="control-card-desc">On $25,000: ~$${stats.best_yield_apy ? ((stats.best_yield_apy/100)*25000/12).toFixed(0) : '—'}/month</div>
          <div class="btn-group">
            <button class="btn btn-ghost" onclick="App.navigate('yields')">→ View Yields</button>
          </div>
        </div>

        <!-- Reset daily counters -->
        <div class="control-card">
          <div class="control-card-title">Daily Reset</div>
          <div class="control-card-desc">Reset today's counters (alerts, buys, skips, daily P&L tracking). Does not affect open positions or trade history.</div>
          <div class="btn-group">
            <button class="btn btn-amber" onclick="resetDaily(this)">↺ Reset Daily Counters</button>
          </div>
        </div>

        <!-- Quick nav -->
        <div class="control-card">
          <div class="control-card-title">Quick Nav</div>
          <div class="btn-group" style="flex-direction:column;align-items:flex-start;gap:8px">
            <button class="btn btn-ghost" onclick="App.navigate('alerts')">◈ Alert Feed ${status.alerts_today?`<span style="color:var(--accent-amber)">(${status.alerts_today} today)</span>`:''}
            </button>
            <button class="btn btn-ghost" onclick="App.navigate('positions')">◎ Positions (${status.open_positions??0} open)</button>
            <button class="btn btn-ghost" onclick="App.navigate('trades')">▷ Trade Log</button>
          </div>
        </div>

        <!-- Bot info -->
        <div class="control-card">
          <div class="control-card-title">Bot Info</div>
          <div class="stat-row"><span class="stat-row-label">Version</span><span class="stat-row-val">1.0.0</span></div>
          <div class="stat-row"><span class="stat-row-label">Dashboard</span><span class="stat-row-val">Phase 4</span></div>
          <div class="stat-row"><span class="stat-row-label">Last Updated</span><span class="stat-row-val">${new Date().toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'})}</span></div>
          <div class="stat-row"><span class="stat-row-label">DB Path</span><span class="stat-row-val" style="font-size:10px">data/cryptobot.db</span></div>
        </div>

      </div>

      <!-- TASK 6: Risk Parameters -->
      <div class="section-label" style="margin-top:28px">RISK PARAMETERS</div>
      <div class="control-grid" id="risk-params-grid">
        <div style="padding:24px;background:var(--bg-secondary);grid-column:1/-1">Loading risk parameters...</div>
      </div>

      <div class="section-label" style="margin-top:28px">EXECUTION MODE</div>
      <div id="exec-config-wrap">
        <div style="padding:24px;background:var(--bg-secondary);border:1px solid var(--border)">Loading execution config...</div>
      </div>

      <div style="font-family:var(--font-mono);font-size:10px;color:var(--text-muted);margin-top:16px">
        Keyboard shortcuts: <span class="kbd-hint">1</span>-<span class="kbd-hint">9</span> Navigate · <span class="kbd-hint">R</span> Refresh
      </div>`;

    // Load risk config and execution config
    Pages._loadRiskConfig();
    Pages._loadExecutionConfig(execCfg);
  },

  // ── Execution Mode Config ──────────────────────────────────────────────────
  _loadExecutionConfig(cfg) {
    const wrap = document.getElementById('exec-config-wrap');
    if (!wrap) return;
    if (!cfg || !cfg.execution_mode) {
      wrap.innerHTML = '<div style="padding:24px;background:var(--bg-secondary);border:1px solid var(--border);color:var(--text-muted);font-family:var(--font-mono);font-size:11px">Execution config unavailable.</div>';
      return;
    }

    const mode       = cfg.execution_mode || 'manual';
    const strategy   = cfg.auto_strategy  || 'balanced';
    const maxTrades  = cfg.auto_max_trades_day || 5;
    const todayCount = cfg.auto_trades_today  || 0;
    const strategies = cfg.strategies || {};

    const modeBtn = (val, label) => {
      const active = mode === val;
      return `<button class="btn ${active?'btn-buy':'btn-ghost'}" style="${active?'':'opacity:.65'}"
        onclick="Pages._setExecMode('${val}')">${label}</button>`;
    };

    const stratCard = (key) => {
      const s = strategies[key] || {};
      const active = strategy === key;
      return `
        <div class="control-card" style="cursor:pointer;border-color:${active?'var(--accent-amber)':'var(--border)'};opacity:${mode==='auto'?'1':'.4'}"
          onclick="if(document.getElementById('exec-mode-val').value==='auto') Pages._setStrategy('${key}')">
          <div class="control-card-title" style="color:${active?'var(--accent-amber)':'var(--text-primary)'}">${s.label||key}${active?' ✓':''}</div>
          <div class="control-card-desc" style="margin-top:4px">${s.desc||''}</div>
          <div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:6px">
            <span class="flag-item">Score ≥ ${s.min_score}</span>
            <span class="flag-item">Mkt cap &lt; $${(s.max_market_cap/1e6).toFixed(0)}M</span>
            <span class="flag-item">${s.position_multiplier}× size</span>
          </div>
        </div>`;
    };

    wrap.innerHTML = `
      <input type="hidden" id="exec-mode-val" value="${mode}">
      <input type="hidden" id="exec-strategy-val" value="${strategy}">
      <div class="control-grid">
        <div class="control-card">
          <div class="control-card-title">Execution Mode</div>
          <div class="control-card-desc" style="margin-bottom:14px">
            Manual — every alert waits for your approval in Telegram or the dashboard.
            Auto — alerts that meet the strategy threshold execute immediately.
            Daily loss limit and pause state are always enforced in both modes.
          </div>
          <div class="btn-group">
            ${modeBtn('manual','⊘ Manual')}
            ${modeBtn('auto','▶ Auto')}
          </div>
          ${mode==='auto' ? `
          <div style="margin-top:14px">
            <div style="font-family:var(--font-mono);font-size:10px;color:var(--text-muted);letter-spacing:.1em;margin-bottom:6px">DAILY TRADE CAP</div>
            <div style="display:flex;align-items:center;gap:10px">
              <input type="number" id="exec-max-trades" class="filter-select" min="1" max="50" value="${maxTrades}" style="width:80px;padding:6px 10px">
              <span style="font-family:var(--font-mono);font-size:11px;color:var(--text-muted)">${todayCount}/${maxTrades} executed today</span>
            </div>
          </div>` : ''}
        </div>
        ${stratCard('conservative')}
        ${stratCard('balanced')}
        ${stratCard('aggressive')}
      </div>
      <div style="margin-top:12px;display:flex;gap:10px;align-items:center">
        <button class="btn btn-buy" onclick="Pages._saveExecutionConfig()">Save Execution Settings</button>
        <span id="exec-save-msg" style="font-family:var(--font-mono);font-size:11px;color:var(--text-muted)"></span>
      </div>`;
  },

  _setExecMode(mode) {
    const el = document.getElementById('exec-mode-val');
    if (el) el.value = mode;
    // Re-render with updated state by pulling current values and calling _loadExecutionConfig
    API.get('/api/execution-config').then(cfg => {
      cfg.execution_mode = mode; // optimistic override
      Pages._loadExecutionConfig(cfg);
    }).catch(()=>{});
  },

  _setStrategy(key) {
    const el = document.getElementById('exec-strategy-val');
    if (el) el.value = key;
    API.get('/api/execution-config').then(cfg => {
      cfg.auto_strategy    = key;
      cfg.execution_mode   = document.getElementById('exec-mode-val')?.value || cfg.execution_mode;
      Pages._loadExecutionConfig(cfg);
    }).catch(()=>{});
  },

  async _saveExecutionConfig() {
    const mode     = document.getElementById('exec-mode-val')?.value     || 'manual';
    const strategy = document.getElementById('exec-strategy-val')?.value || 'balanced';
    const maxEl    = document.getElementById('exec-max-trades');
    const maxTrades = maxEl ? parseInt(maxEl.value) || 5 : 5;
    const msg = document.getElementById('exec-save-msg');
    if (msg) msg.textContent = 'Saving...';
    try {
      const res = await API.post('/api/execution-config', {
        execution_mode: mode, auto_strategy: strategy, auto_max_trades_day: maxTrades
      });
      if (res.ok) {
        Toast.show(`Execution mode saved — ${mode.toUpperCase()}${mode==='auto'?' · '+strategy:''}`, 'success');
        if (msg) msg.textContent = '';
      } else {
        Toast.show('Could not save', 'error');
        if (msg) msg.textContent = 'Save failed.';
      }
    } catch(e) {
      Toast.show('Error: '+e.message, 'error');
      if (msg) msg.textContent = e.message;
    }
  },

    async _loadRiskConfig() {
    try {
      const rc = await API.get('/api/risk-config');
      const grid = document.getElementById('risk-params-grid');
      if(!grid) return;

      const fields = [
        {key:'max_position_size_pct', label:'Max Position Size %', hint:'0.001–0.10', step:'0.001'},
        {key:'daily_loss_limit_pct', label:'Daily Loss Limit %', hint:'0.01–0.20', step:'0.01'},
        {key:'min_liquidity_usd', label:'Min Liquidity USD', hint:'1,000–1,000,000', step:'1000'},
        {key:'min_volume_24h_usd', label:'Min Volume 24h USD', hint:'1,000–10,000,000', step:'1000'},
        {key:'min_rug_score', label:'Min Rug Score', hint:'0–100', step:'1'},
        {key:'portfolio_total_usd', label:'Portfolio Total USD', hint:'100–1,000,000', step:'100'},
      ];

      grid.innerHTML = fields.map(f => `
        <div class="control-card">
          <div class="control-card-title">${f.label}</div>
          <input type="number" class="filter-select" style="width:100%;padding:8px 12px;font-size:14px;margin-bottom:8px"
            id="risk-${f.key}" value="${rc[f.key] ?? ''}" step="${f.step}" />
          <div style="font-family:var(--font-mono);font-size:10px;color:var(--text-muted)">${f.hint} recommended</div>
        </div>
      `).join('') + `
        <div class="control-card" style="display:flex;align-items:flex-end">
          <button class="btn btn-buy" id="save-risk-btn" onclick="Pages._saveRiskConfig()" style="width:100%">Save Changes</button>
        </div>`;
    } catch(e) {
      const grid = document.getElementById('risk-params-grid');
      if(grid) grid.innerHTML = `<div style="padding:24px;background:var(--bg-secondary)">Could not load risk config.</div>`;
    }
  },

  async _saveRiskConfig() {
    const btn = document.getElementById('save-risk-btn');
    if(btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
    const body = {};
    ['max_position_size_pct','daily_loss_limit_pct','min_liquidity_usd','min_volume_24h_usd','min_rug_score','portfolio_total_usd'].forEach(k => {
      const el = document.getElementById('risk-'+k);
      if(el && el.value !== '') body[k] = parseFloat(el.value);
    });
    try {
      const res = await API.post('/api/risk-config', body);
      if(res.ok) Toast.show('Risk parameters saved', 'success');
      else Toast.show('Could not save', 'error');
    } catch(e) {
      Toast.show('Error: '+(e.message||'Save failed'), 'error');
    }
    if(btn) { btn.disabled = false; btn.textContent = 'Save Changes'; }
  },


  // ── Flash Loans page ──────────────────────────────────────────────────────
  async flashloans(c) {
    const d = await API.get('/api/flashloans').catch(()=>({summary:{},recent:[]}));
    const s = d.summary||{};
    const recent = d.recent||[];

    // Build daily buckets for the bar chart
    const dayMap = {};
    recent.forEach(tx => {
      const day = (tx.executed_at||'').slice(0,10);
      if (!day) return;
      if (!dayMap[day]) dayMap[day] = {success:0, reverted:0, profit:0};
      if (tx.status==='success') { dayMap[day].success++; dayMap[day].profit += (tx.profit_usdc||0); }
      else dayMap[day].reverted++;
    });
    const chartDays = Object.keys(dayMap).sort().slice(-14);
    const chartLabels = chartDays.map(d => {
      const dt = new Date(d + 'T00:00:00');
      return dt.toLocaleDateString('en-US', {month:'short', day:'numeric'});
    });

    // Success rate visual
    const srRate = s.success_rate ?? 0;
    const srColor = srRate >= 70 ? 'var(--accent-green)' : srRate >= 40 ? 'var(--accent-amber)' : 'var(--accent-red)';

    const rows = recent.length ? recent.map((tx, i) => {
      const isSuccess  = tx.status === 'success';
      const statusBadge = isSuccess
        ? '<span class="decision-badge buy">SUCCESS</span>'
        : '<span class="decision-badge skip">REVERTED</span>';
      const profitHtml = isSuccess
        ? `<span class="td-mono positive">${Fmt.usd(tx.profit_usdc)}</span>`
        : '<span class="td-muted">—</span>';
      const gasHtml = tx.gas_cost_eth
        ? `<span class="td-mono" style="color:var(--text-secondary)">${tx.gas_cost_eth.toFixed(6)}</span>`
        : '<span class="td-muted">—</span>';
      const hashLink = tx.tx_hash
        ? `<a href="https://basescan.org/tx/${tx.tx_hash}" target="_blank"
             style="color:var(--accent-blue);text-decoration:none;font-family:var(--font-mono);font-size:11px;letter-spacing:.02em">
             ${tx.tx_hash.slice(0,8)}…${tx.tx_hash.slice(-6)}</a>`
        : '—';
      return `<tr style="animation-delay:${Math.min(i,15)*.03}s">
        <td>${hashLink}</td>
        <td>${statusBadge}</td>
        <td>${profitHtml}</td>
        <td>${gasHtml} <span class="td-muted" style="font-size:10px">ETH</span></td>
        <td class="td-mono" style="color:var(--text-muted)">${tx.block_number ? '#'+tx.block_number.toLocaleString() : '—'}</td>
        <td class="td-muted">${Fmt.datetime(tx.executed_at)}</td>
      </tr>`;
    }).join('') : `<tr><td colspan="6">
      <div class="empty-state">
        <div class="empty-state-icon">◈</div>
        <div class="empty-state-title">NO FLASH LOAN DATA</div>
        <div class="empty-state-msg">Add BASESCAN_API_KEY to .env to sync live on-chain data.</div>
      </div></td></tr>`;

    c.innerHTML = `
      <div class="page-header">
        <div class="page-header-left">
          <div class="page-title">FLASH LOANS</div>
          <div class="page-subtitle">On-chain arb · Contract <a href="https://basescan.org/address/0x60b30eb32656dfDA6Aed6fd0c073fe872717d357" target="_blank"
            style="color:var(--accent-blue);text-decoration:none;font-family:var(--font-mono);font-size:10px">0x60b3…d357</a></div>
        </div>
        <div class="page-header-right">
          <button class="btn btn-ghost btn-sm" onclick="App.renderPage('flashloans')">↻ Refresh</button>
        </div>
      </div>

      <div class="stat-grid">
        <div class="stat-card">
          <div class="stat-label">TOTAL ATTEMPTS</div>
          <div class="stat-value">${s.total_attempts ?? '—'}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">SUCCESSFUL</div>
          <div class="stat-value positive">${s.successful ?? '—'}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">REVERTED</div>
          <div class="stat-value negative">${s.reverted ?? '—'}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">SUCCESS RATE</div>
          <div class="stat-value" style="color:${srColor}">${s.success_rate != null ? Fmt.pct(s.success_rate) : '—'}</div>
          ${s.success_rate != null ? `<div class="progress-bar-wrap"><div class="progress-bar-track">
            <div style="height:3px;background:${srColor};width:${Math.min(srRate,100)}%;transition:width .8s ease"></div>
          </div></div>` : ''}
        </div>
        <div class="stat-card">
          <div class="stat-label">TOTAL PROFIT</div>
          <div class="stat-value positive">${Fmt.usd(s.total_profit_usdc)}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">TOTAL GAS SPENT</div>
          <div class="stat-value">${s.total_gas_eth ? s.total_gas_eth.toFixed(6) : '—'}</div>
          <div class="stat-meta">ETH</div>
        </div>
      </div>

      ${chartDays.length > 0 ? `
      <div class="chart-wrap">
        <div class="chart-title">DAILY ATTEMPTS — SUCCESS vs REVERTED (LAST 14 DAYS)</div>
        <canvas id="fl-chart" height="90"></canvas>
      </div>` : ''}

      <div class="section-label">RECENT TRANSACTIONS</div>
      <div class="table-wrap"><table>
        <thead><tr>
          <th>TX HASH</th><th>STATUS</th><th>PROFIT (USDC)</th>
          <th>GAS COST</th><th>BLOCK</th><th>DATE</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;

    if (chartDays.length > 0) {
      Charts.flashLoanBar(
        'fl-chart',
        chartLabels,
        chartDays.map(d => dayMap[d].success),
        chartDays.map(d => dayMap[d].reverted),
        chartDays.map(d => dayMap[d].profit)
      );
    }
  },


  // ── Analytics page ─────────────────────────────────────────────────────────
  async analytics(c) {
    const d = await API.get('/api/analytics').catch(()=>({}));

    // Win rate by chain — rendered as labelled progress bars
    const chainBars = d.win_rate_by_chain
      ? Object.entries(d.win_rate_by_chain).map(([chain, rate]) => {
          const col = rate >= 50 ? 'var(--accent-green)' : 'var(--accent-red)';
          return `
            <div style="margin-bottom:14px">
              <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:5px">
                <span style="font-family:var(--font-mono);font-size:11px;color:var(--text-secondary);letter-spacing:.1em">${chain.toUpperCase()}</span>
                <span style="font-family:var(--font-mono);font-size:13px;font-weight:600;color:${col}">${Fmt.pct(rate)}</span>
              </div>
              <div class="progress-bar-track" style="height:5px">
                <div style="height:100%;background:${col};width:${Math.min(rate,100)}%;transition:width .8s ease"></div>
              </div>
            </div>`;
        }).join('')
      : '<div class="td-muted" style="font-family:var(--font-mono);font-size:11px">No data yet</div>';

    // Win rate by score band — same treatment
    const bandOrder = ['80-100', '60-79', 'below-60'];
    const bandLabel = {'80-100':'Score 80–100','60-79':'Score 60–79','below-60':'Score < 60'};
    const scoreBars = d.win_rate_by_score_band
      ? bandOrder.filter(k => k in d.win_rate_by_score_band).map(k => {
          const rate = d.win_rate_by_score_band[k];
          const col  = rate >= 50 ? 'var(--accent-green)' : 'var(--accent-red)';
          return `
            <div style="margin-bottom:14px">
              <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:5px">
                <span style="font-family:var(--font-mono);font-size:11px;color:var(--text-secondary);letter-spacing:.1em">${bandLabel[k]}</span>
                <span style="font-family:var(--font-mono);font-size:13px;font-weight:600;color:${col}">${Fmt.pct(rate)}</span>
              </div>
              <div class="progress-bar-track" style="height:5px">
                <div style="height:100%;background:${col};width:${Math.min(rate,100)}%;transition:width .8s ease"></div>
              </div>
            </div>`;
        }).join('')
      : '<div class="td-muted" style="font-family:var(--font-mono);font-size:11px">No data yet</div>';

    const bt = d.best_trade, wt = d.worst_trade;

    c.innerHTML = `
      <div class="page-header">
        <div class="page-header-left">
          <div class="page-title">ANALYTICS</div>
          <div class="page-subtitle">Performance breakdown · All-time</div>
        </div>
        <div class="page-header-right">
          <button class="btn btn-ghost btn-sm" onclick="App.renderPage('analytics')">↻ Refresh</button>
        </div>
      </div>

      <!-- KPI row -->
      <div class="stat-grid">
        <div class="stat-card">
          <div class="stat-label">WIN RATE</div>
          <div class="stat-value ${(d.win_rate_overall||0)>=50?'positive':'negative'}">${Fmt.pct(d.win_rate_overall)}</div>
          <div class="progress-bar-wrap"><div class="progress-bar-track"><div class="progress-bar-fill" style="width:${Math.min(d.win_rate_overall||0,100)}%"></div></div></div>
        </div>
        <div class="stat-card">
          <div class="stat-label">AVG HOLD TIME</div>
          <div class="stat-value">${d.avg_hold_time_hours ? d.avg_hold_time_hours.toFixed(1)+'h' : '—'}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">MAX DRAWDOWN</div>
          <div class="stat-value negative">${Fmt.usd(d.max_drawdown_usd)}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">TOTAL FEES</div>
          <div class="stat-value">${Fmt.usd(d.total_fees_usd)}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">AVG POSITION SIZE</div>
          <div class="stat-value">${Fmt.usd(d.avg_position_size_usd)}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">BEST TRADE</div>
          <div class="stat-value positive" style="font-size:16px">${bt ? bt.symbol : '—'}</div>
          <div class="stat-meta">${bt ? Fmt.pnl(bt.pnl_usd).text+' · '+Fmt.pct(bt.pct_gain)+' gain' : 'No trades yet'}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">WORST TRADE</div>
          <div class="stat-value negative" style="font-size:16px">${wt ? wt.symbol : '—'}</div>
          <div class="stat-meta">${wt ? Fmt.pnl(wt.pnl_usd).text+' · '+Fmt.pct(wt.pct_gain) : 'No trades yet'}</div>
        </div>
      </div>

      <!-- Breakdown cards -->
      <div class="control-grid" style="margin-bottom:28px">
        <div class="control-card">
          <div class="control-card-title">Win Rate by Chain</div>
          <div style="margin-top:8px">${chainBars}</div>
        </div>
        <div class="control-card">
          <div class="control-card-title">Win Rate by Security Score</div>
          <div style="margin-top:8px">${scoreBars}</div>
        </div>
      </div>

      <!-- Monthly P&L chart -->
      ${(d.monthly_pnl||[]).length > 0 ? `
      <div class="chart-wrap">
        <div class="chart-title">MONTHLY P&L</div>
        <canvas id="monthly-pnl-chart" height="90"></canvas>
      </div>` : `
      <div class="chart-wrap">
        <div class="chart-title">MONTHLY P&L</div>
        <div class="empty-state" style="padding:40px 20px">
          <div class="empty-state-icon">◈</div>
          <div class="empty-state-title">NO MONTHLY DATA YET</div>
          <div class="empty-state-msg">P&L chart will populate once trades are recorded.</div>
        </div>
      </div>`}`;

    const mp = d.monthly_pnl || [];
    if (mp.length > 0) {
      Charts.monthlyPnlBar('monthly-pnl-chart', mp.map(m=>m.month), mp.map(m=>m.pnl));
    }
  },


  // ── Research page ─────────────────────────────────────────────────────────
  _recentSearches: [],

  async research(c) {
    const chipsHtml = Pages._recentSearches.length
      ? Pages._recentSearches.map(addr =>
          `<span class="flag-item" style="cursor:pointer" onclick="Pages._doResearch('${addr}')">${addr.slice(0,6)}...${addr.slice(-4)}</span>`
        ).join('')
      : '';

    c.innerHTML = `
      <div class="page-header">
        <div class="page-header-left">
          <div class="page-title">TOKEN RESEARCH</div>
          <div class="page-subtitle">Paste a contract address to run security checks</div>
        </div>
      </div>
      <div style="display:flex;gap:8px;margin-bottom:16px">
        <input type="text" id="research-input" class="filter-select" style="flex:1;padding:12px 16px;font-size:14px"
          placeholder="Paste contract address..." onkeydown="if(event.key==='Enter')Pages._doResearch()"/>
        <button class="btn btn-buy" onclick="Pages._doResearch()">Analyze</button>
      </div>
      ${chipsHtml ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:20px">${chipsHtml}</div>` : ''}
      <div id="research-results"></div>`;
  },

  async _doResearch(addr) {
    const input = document.getElementById('research-input');
    const tokenAddr = addr || (input ? input.value.trim() : '');
    if (!tokenAddr) { Toast.show('Enter a contract address', 'error'); return; }
    if (input) input.value = tokenAddr;

    const results = document.getElementById('research-results');
    if (!results) return;
    results.innerHTML = `<div class="loading-screen" style="height:220px">
      <div class="loading-spinner"></div>
      <div class="loading-text">ANALYZING TOKEN</div>
    </div>`;

    try {
      const d = await API.get('/api/research/' + encodeURIComponent(tokenAddr));
      Pages._recentSearches = [tokenAddr, ...Pages._recentSearches.filter(a=>a!==tokenAddr)].slice(0,5);

      const score   = d.rug_score ?? 0;
      const scoreColor = score >= 80 ? 'var(--accent-green)' : score >= 60 ? 'var(--accent-amber)' : 'var(--accent-red)';
      const scoreTier  = score >= 80 ? 'LOW RISK' : score >= 60 ? 'ELEVATED RISK' : 'HIGH RISK';
      const dex        = d.dexscreener || {};
      const gp         = d.goplus || {};

      const flagsHtml = (d.flags||[]).length
        ? d.flags.map(f => `<span class="flag-item">${f}</span>`).join('')
        : `<span class="flag-item" style="color:var(--accent-green);border-color:var(--accent-green)">✓ No flags detected</span>`;

      const rugRisks = d.rugcheck?.risks || [];

      results.innerHTML = `
        <!-- Score hero — centered -->
        <div class="research-score-hero" style="border-color:${scoreColor}">
          <div class="research-score-number" style="color:${scoreColor}">${score}</div>
          <div class="research-score-label">/ 100</div>
          <div style="width:200px;margin-top:12px">
            <div class="progress-bar-track" style="height:6px">
              <div style="height:100%;background:${scoreColor};width:${score}%;transition:width 1s ease"></div>
            </div>
          </div>
          <div class="research-score-tier" style="color:${scoreColor}">${scoreTier}</div>
          <div class="research-badges">
            <span class="chain-badge">${(d.chain||'?').toUpperCase()}</span>
            ${dex.symbol ? `<span class="chain-badge" style="color:var(--accent-amber);border-color:var(--accent-amber)">${dex.symbol}</span>` : ''}
            ${gp.is_honeypot ? '<span class="decision-badge" style="color:var(--accent-red);border-color:var(--accent-red)">HONEYPOT</span>' : ''}
            ${gp.is_open_source===false ? '<span class="decision-badge">NOT VERIFIED</span>' : ''}
          </div>
        </div>

        <!-- Detail grid -->
        <div class="control-grid" style="margin-top:1px">

          <!-- DEXScreener: Token info -->
          <div class="control-card research-section">
            <div class="control-card-title">DEXScreener — Token Info</div>
            ${dex.name ? `<div class="stat-row"><span class="stat-row-label">Name</span><span class="stat-row-val">${dex.name}</span></div>` : ''}
            ${dex.symbol ? `<div class="stat-row"><span class="stat-row-label">Symbol</span><span class="stat-row-val">${dex.symbol}</span></div>` : ''}
            ${dex.price_usd ? `<div class="stat-row"><span class="stat-row-label">Price</span><span class="stat-row-val td-mono">${Fmt.price(dex.price_usd)}</span></div>` : ''}
            ${dex.liquidity ? `<div class="stat-row"><span class="stat-row-label">Liquidity</span><span class="stat-row-val td-mono">${Fmt.usd(dex.liquidity)}</span></div>` : ''}
            ${dex.volume_24h ? `<div class="stat-row"><span class="stat-row-label">Volume 24h</span><span class="stat-row-val td-mono">${Fmt.usd(dex.volume_24h)}</span></div>` : ''}
            ${dex.market_cap ? `<div class="stat-row"><span class="stat-row-label">Mkt Cap</span><span class="stat-row-val td-mono">${Fmt.usd(dex.market_cap)}</span></div>` : ''}
          </div>

          <!-- GoPlus: On-chain mechanics -->
          <div class="control-card research-section">
            <div class="control-card-title">GoPlus — On-Chain Mechanics</div>
            <div class="stat-row">
              <span class="stat-row-label">Honeypot</span>
              <span class="stat-row-val ${gp.is_honeypot ? 'negative' : 'positive'}">${gp.is_honeypot ? '✕ YES' : '✓ NO'}</span>
            </div>
            <div class="stat-row">
              <span class="stat-row-label">Buy Tax</span>
              <span class="stat-row-val td-mono ${(gp.buy_tax||0)>5?'negative':''}">${gp.buy_tax != null ? gp.buy_tax+'%' : '—'}</span>
            </div>
            <div class="stat-row">
              <span class="stat-row-label">Sell Tax</span>
              <span class="stat-row-val td-mono ${(gp.sell_tax||0)>5?'negative':''}">${gp.sell_tax != null ? gp.sell_tax+'%' : '—'}</span>
            </div>
            <div class="stat-row">
              <span class="stat-row-label">Ownership</span>
              <span class="stat-row-val ${gp.owner_address==='0x0000000000000000000000000000000000000000'?'positive':''}">${gp.owner_address === '0x0000000000000000000000000000000000000000' ? '✓ Renounced' : gp.owner_address ? gp.owner_address.slice(0,10)+'…' : '—'}</span>
            </div>
          </div>

          <!-- Risk flags full width -->
          <div class="control-card research-section" style="grid-column:1/-1">
            <div class="control-card-title">GoPlus — Risk Flags</div>
            <div class="flags-list" style="margin-top:8px">${flagsHtml}</div>
          </div>

          ${rugRisks.length ? `
          <div class="control-card research-section" style="grid-column:1/-1">
            <div class="control-card-title">RugCheck — Risk Factors</div>
            <div class="flags-list" style="margin-top:8px">
              ${rugRisks.map(r => `<span class="flag-item">${r}</span>`).join('')}
            </div>
          </div>` : ''}

        </div>`;
    } catch(e) {
      results.innerHTML = `<div class="empty-state">
        <div class="empty-state-icon">⚠</div>
        <div class="empty-state-title">RESEARCH FAILED</div>
        <div class="empty-state-msg">${e.message||'Unknown error'}</div>
      </div>`;
    }
  },
};

document.addEventListener('DOMContentLoaded', () => App.init());
