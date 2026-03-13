// wizard.js - Breadbot first-run setup wizard

// On every page load calls /api/setup/status. If setup is not complete,
// injects a full-screen overlay and walks the user through five configuration
// steps before revealing the main dashboard.
// 
// Design contract: runs before app.js. Overlay sits at z-index 9999 covering
// the dashboard until the user completes setup. The main app loads beneath it.
// All window.Wiz_* functions are global so onclick attributes can call them.
// 
'use strict';

(function WizardScope() {

  const STEPS = ['welcome','telegram','coinbase','kraken','wallet','risk','launch'];
  let currentStep = 0;
  let overlayEl   = null;

  document.addEventListener('DOMContentLoaded', async () => {
    try {
      const res  = await fetch('/api/setup/status');
      const data = await res.json();
      if (!data.setup_complete) {
        const startStep = Math.min(data.current_step || 0, 5);
        init(startStep);
      }
    } catch (_) {}
  });

  function init(startStep) {
    overlayEl = document.createElement('div');
    overlayEl.id = 'wiz-overlay';
    overlayEl.innerHTML = buildShell();
    document.body.appendChild(overlayEl);
    goToStep(startStep);
  }

  function buildShell() {
    return '<div class="wiz-container">' +
      '<div class="wiz-header">' +
        '<div class="wiz-header-left">' +
          '<svg width="36" height="36" viewBox="0 0 32 36" fill="none"><line x1="8" y1="5" x2="8" y2="31" stroke="#f59e0b" stroke-width="1.5" stroke-linecap="round"/><polyline points="8,5 18,5 24,9 24,14 16,18 8,18" fill="none" stroke="#f59e0b" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><polyline points="8,18 17,18 25,22 25,27 17,31 8,31" fill="none" stroke="#f59e0b" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
          '<span class="wiz-brand">BREADBOT</span>' +
        '</div>' +
        '<div class="wiz-dots" id="wiz-dots"></div>' +
      '</div>' +
      '<div class="wiz-body" id="wiz-body"></div>' +
      '<div class="wiz-footer" id="wiz-footer"></div>' +
    '</div>';
  }

  function updateDots() {
    const el = document.getElementById('wiz-dots');
    if (!el) return;
    if (currentStep === 0 || currentStep === 6) { el.innerHTML = ''; return; }
    el.innerHTML = Array.from({length: 5}, (_, i) => {
      const idx = i + 1;
      const cls = idx < currentStep ? 'done' : idx === currentStep ? 'active' : '';
      return '<div class="wiz-dot ' + cls + '"></div>';
    }).join('');
  }

  function goToStep(step) {
    currentStep = step;
    const renderers = [renderWelcome, renderTelegram, renderCoinbase,
                       renderKraken, renderWallet, renderRisk, renderLaunch];
    const body   = document.getElementById('wiz-body');
    const footer = document.getElementById('wiz-footer');
    if (!body) return;
    body.innerHTML   = renderers[step]();
    footer.innerHTML = renderFooter(step);
    updateDots();
    if (step === 6) setTimeout(runLaunch, 150);
  }

  function renderFooter(step) {
    if (step === 0 || step === 6) return '';
    const isFirst   = step === 1;
    const isPreLast = step === 5;
    return '<div class="wiz-footer-inner">' +
      (!isFirst ? '<button class="wiz-btn-ghost" onclick="Wiz_back()">Back</button>' : '<div></div>') +
      '<button class="wiz-btn-primary" id="wiz-next-btn" onclick="Wiz_next()">' +
        (isPreLast ? 'Review and Launch' : 'Next') +
      '</button>' +
    '</div>';
  }

  function renderWelcome() {
    return '<div class="wiz-step wiz-welcome">' +
      '<div class="wiz-flash-icon">&#9889;</div>' +
      '<h1 class="wiz-title">Welcome to Breadbot</h1>' +
      '<p class="wiz-subtitle">This wizard gets you live in about 5 minutes. Before you start, have these ready:</p>' +
      '<ul class="wiz-checklist">' +
        '<li>A Telegram bot token - free from @BotFather</li>' +
        '<li>Coinbase Advanced Trade API keys (View + Trade only)</li>' +
        '<li>Kraken API keys (Query Funds + Trade + WebSockets only)</li>' +
        '<li>Your Base wallet private key</li>' +
      '</ul>' +
      '<button class="wiz-btn-primary wiz-btn-large" onclick="Wiz_next()">Start Setup</button>' +
    '</div>';
  }

  function renderTelegram() {
    return '<div class="wiz-step">' +
      '<div class="wiz-step-label">Step 1 of 5</div>' +
      '<h2 class="wiz-title">Telegram</h2>' +
      '<p class="wiz-subtitle">Every alert, trade decision, and status update flows through Telegram.</p>' +
      '<div class="wiz-field">' +
        '<label class="wiz-label">Bot Token</label>' +
        '<p class="wiz-hint">Open Telegram, message @BotFather, send /newbot, follow prompts, copy the token.</p>' +
        '<input id="wiz_tg_token" type="password" class="wiz-input" placeholder="7123456789:AAFxyz..." autocomplete="off" />' +
      '</div>' +
      '<div class="wiz-field">' +
        '<label class="wiz-label">Your Chat ID</label>' +
        '<p class="wiz-hint">Message @userinfobot anything - it replies with your numeric chat ID.</p>' +
        '<input id="wiz_tg_chat" type="text" class="wiz-input" placeholder="123456789" autocomplete="off" />' +
      '</div>' +
      '<div class="wiz-test-row">' +
        '<button class="wiz-btn-ghost" id="wiz-tg-test-btn" onclick="Wiz_testTelegram()">Send Test Message</button>' +
        '<span id="wiz-tg-result"></span>' +
      '</div>' +
    '</div>';
  }

  function renderCoinbase() {
    return '<div class="wiz-step">' +
      '<div class="wiz-step-label">Step 2 of 5</div>' +
      '<h2 class="wiz-title">Coinbase</h2>' +
      '<p class="wiz-subtitle">Used for stablecoin yield tracking and executing trades on Base.</p>' +
      '<div class="wiz-callout">Enable <strong>View</strong> and <strong>Trade</strong> only. Never enable Transfer or Withdraw.</div>' +
      '<div class="wiz-field">' +
        '<label class="wiz-label">API Key</label>' +
        '<p class="wiz-hint">Coinbase - Settings - Developer Platform - Create new API key.</p>' +
        '<input id="wiz_cb_key" type="password" class="wiz-input" placeholder="organizations/..." autocomplete="off" />' +
      '</div>' +
      '<div class="wiz-field">' +
        '<label class="wiz-label">API Secret</label>' +
        '<p class="wiz-hint">Shown only once when you create the key. Copy it before closing that page.</p>' +
        '<input id="wiz_cb_secret" type="password" class="wiz-input" placeholder="-----BEGIN EC PRIVATE KEY-----" autocomplete="off" />' +
      '</div>' +
    '</div>';
  }

  function renderKraken() {
    return '<div class="wiz-step">' +
      '<div class="wiz-step-label">Step 3 of 5</div>' +
      '<h2 class="wiz-title">Kraken</h2>' +
      '<p class="wiz-subtitle">Used for yield tracking and spot trading on Kraken.</p>' +
      '<div class="wiz-callout">Enable <strong>Query Funds</strong>, <strong>Trade</strong>, and <strong>WebSockets</strong>. Withdraw stays off always.</div>' +
      '<div class="wiz-field">' +
        '<label class="wiz-label">API Key</label>' +
        '<p class="wiz-hint">Kraken - Security - API - Add Key.</p>' +
        '<input id="wiz_kr_key" type="password" class="wiz-input" placeholder="Kraken API key..." autocomplete="off" />' +
      '</div>' +
      '<div class="wiz-field">' +
        '<label class="wiz-label">API Secret</label>' +
        '<input id="wiz_kr_secret" type="password" class="wiz-input" placeholder="Kraken private key..." autocomplete="off" />' +
      '</div>' +
    '</div>';
  }

  function renderWallet() {
    return '<div class="wiz-step">' +
      '<div class="wiz-step-label">Step 4 of 5</div>' +
      '<h2 class="wiz-title">Wallet and RPC</h2>' +
      '<p class="wiz-subtitle">Your Base wallet is used for meme coin trades and flash loan arb.</p>' +
      '<div class="wiz-callout wiz-callout-warn">Your private key is stored in the encrypted database on your server only. It never leaves your infrastructure.</div>' +
      '<div class="wiz-field">' +
        '<label class="wiz-label">Base Wallet Private Key</label>' +
        '<p class="wiz-hint">The private key for your Base/EVM wallet. Starts with 0x.</p>' +
        '<input id="wiz_pk" type="password" class="wiz-input" placeholder="0x..." autocomplete="off" />' +
      '</div>' +
      '<div class="wiz-field">' +
        '<label class="wiz-label">RPC URL <span class="wiz-optional">optional</span></label>' +
        '<p class="wiz-hint">Leave blank for the free public Base RPC. Add Alchemy or Infura for higher reliability.</p>' +
        '<input id="wiz_rpc" type="text" class="wiz-input" placeholder="https://mainnet.base.org" autocomplete="off" />' +
      '</div>' +
      '<details class="wiz-advanced">' +
        '<summary>Optional API Keys</summary>' +
        '<div class="wiz-field wiz-advanced-field">' +
          '<label class="wiz-label">GoPlus API Key <span class="wiz-optional">optional</span></label>' +
          '<p class="wiz-hint">Free tier works. Register at gopluslabs.io for higher scanner rate limits.</p>' +
          '<input id="wiz_goplus" type="password" class="wiz-input" placeholder="GoPlus key..." autocomplete="off" />' +
        '</div>' +
        '<div class="wiz-field wiz-advanced-field">' +
          '<label class="wiz-label">BaseScan API Key <span class="wiz-optional">optional</span></label>' +
          '<p class="wiz-hint">Enables real flash loan transaction history from BaseScan.</p>' +
          '<input id="wiz_basescan" type="password" class="wiz-input" placeholder="BaseScan key..." autocomplete="off" />' +
        '</div>' +
      '</details>' +
    '</div>';
  }

  function renderRisk() {
    return '<div class="wiz-step">' +
      '<div class="wiz-step-label">Step 5 of 5</div>' +
      '<h2 class="wiz-title">Risk Settings</h2>' +
      '<p class="wiz-subtitle">Controls how aggressively the bot trades. Defaults are conservative. Adjustable anytime from the Controls page.</p>' +
      '<div class="wiz-field">' +
        '<label class="wiz-label">Total Portfolio Value (USD)</label>' +
        '<p class="wiz-hint">Your account size. Position sizes are calculated as a percentage of this.</p>' +
        '<input id="wiz_portfolio" type="number" class="wiz-input" value="5000" step="500" min="100" />' +
      '</div>' +
      '<div class="wiz-two-col">' +
        '<div class="wiz-field">' +
          '<label class="wiz-label">Max Position Size</label>' +
          '<p class="wiz-hint">% of portfolio per trade. 0.02 = 2% = 100 USD on a 5k account.</p>' +
          '<input id="wiz_max_pos" type="number" class="wiz-input" value="0.02" step="0.005" min="0.005" max="0.1" />' +
        '</div>' +
        '<div class="wiz-field">' +
          '<label class="wiz-label">Daily Loss Limit</label>' +
          '<p class="wiz-hint">Bot pauses when realized losses hit this. 0.05 = 5% = 250 USD on a 5k account.</p>' +
          '<input id="wiz_loss" type="number" class="wiz-input" value="0.05" step="0.01" min="0.01" max="0.5" />' +
        '</div>' +
      '</div>' +
      '<div class="wiz-two-col">' +
        '<div class="wiz-field">' +
          '<label class="wiz-label">Min Liquidity (USD)</label>' +
          '<p class="wiz-hint">Tokens below this liquidity are filtered out by the scanner.</p>' +
          '<input id="wiz_liq" type="number" class="wiz-input" value="15000" step="1000" min="1000" />' +
        '</div>' +
        '<div class="wiz-field">' +
          '<label class="wiz-label">Min 24h Volume (USD)</label>' +
          '<p class="wiz-hint">Tokens below this daily volume are filtered out.</p>' +
          '<input id="wiz_vol" type="number" class="wiz-input" value="40000" step="1000" min="1000" />' +
        '</div>' +
      '</div>' +
    '</div>';
  }

  function renderLaunch() {
    return '<div class="wiz-step wiz-launch">' +
      '<h2 class="wiz-title">Almost There</h2>' +
      '<p class="wiz-subtitle" id="wiz-launch-status">Running connection checks...</p>' +
      '<div id="wiz-checks" class="wiz-checks"></div>' +
      '<div id="wiz-launch-cta"></div>' +
    '</div>';
  }

  window.Wiz_next = async function() {
    if (currentStep === 0) { goToStep(1); return; }
    const values = collectStep(currentStep);
    if (values === null) return;
    const ok = await saveValues(values);
    if (!ok) return;
    goToStep(currentStep + 1);
  };

  window.Wiz_back = function() {
    if (currentStep > 1) goToStep(currentStep - 1);
  };

  function collectStep(step) {
    clearStepError();
    switch (step) {
      case 1: return collectTelegram();
      case 2: return collectCoinbase();
      case 3: return collectKraken();
      case 4: return collectWallet();
      case 5: return collectRisk();
      default: return {};
    }
  }

  function val(id)  { return (document.getElementById(id) && document.getElementById(id).value || '').trim(); }
  function req(id, label) {
    const v = val(id);
    if (!v) { showStepError(label + ' is required'); return null; }
    return v;
  }

  function collectTelegram() {
    const token  = req('wiz_tg_token', 'Bot token');   if (token  === null) return null;
    const chatId = req('wiz_tg_chat',  'Chat ID');     if (chatId === null) return null;
    return {telegram_bot_token: token, telegram_chat_id: chatId};
  }

  function collectCoinbase() {
    const key    = req('wiz_cb_key',    'API key');    if (key    === null) return null;
    const secret = req('wiz_cb_secret', 'API secret'); if (secret === null) return null;
    return {coinbase_api_key: key, coinbase_api_secret: secret};
  }

  function collectKraken() {
    const key    = req('wiz_kr_key',    'API key');    if (key    === null) return null;
    const secret = req('wiz_kr_secret', 'API secret'); if (secret === null) return null;
    return {kraken_api_key: key, kraken_api_secret: secret};
  }

  function collectWallet() {
    const pk = req('wiz_pk', 'Private key'); if (pk === null) return null;
    const rpc      = val('wiz_rpc')      || 'https://mainnet.base.org';
    const goplus   = val('wiz_goplus');
    const basescan = val('wiz_basescan');
    const values   = {base_private_key: pk, base_rpc_url: rpc};
    if (goplus)   values.goplus_api_key   = goplus;
    if (basescan) values.basescan_api_key = basescan;
    return values;
  }

  function collectRisk() {
    return {
      portfolio_total_usd:   val('wiz_portfolio') || '5000',
      max_position_size_pct: val('wiz_max_pos')   || '0.02',
      daily_loss_limit_pct:  val('wiz_loss')      || '0.05',
      min_liquidity_usd:     val('wiz_liq')       || '15000',
      min_volume_24h_usd:    val('wiz_vol')       || '40000',
    };
  }

  async function saveValues(values) {
    try {
      const res  = await fetch('/api/setup/save', {
        method:  'POST',
        headers: {'Content-Type': 'application/json'},
        body:    JSON.stringify({step: STEPS[currentStep], values: values}),
      });
      const data = await res.json();
      if (!data.ok) { showStepError(data.error || 'Save failed - try again'); return false; }
      return true;
    } catch (_) {
      showStepError('Connection error - is the server still running?');
      return false;
    }
  }

  window.Wiz_testTelegram = async function() {
    const token  = val('wiz_tg_token');
    const chatId = val('wiz_tg_chat');
    const result = document.getElementById('wiz-tg-result');
    const btn    = document.getElementById('wiz-tg-test-btn');
    if (!token || !chatId) {
      result.innerHTML = '<span class="wiz-inline-error">Enter token and chat ID first</span>';
      return;
    }
    btn.disabled = true; btn.textContent = 'Sending...'; result.innerHTML = '';
    try {
      const res  = await fetch('/api/setup/test-telegram', {
        method:  'POST',
        headers: {'Content-Type': 'application/json'},
        body:    JSON.stringify({token: token, chat_id: chatId}),
      });
      const data = await res.json();
      result.innerHTML = data.ok
        ? '<span class="wiz-inline-ok">&#10003; Message sent - check Telegram</span>'
        : '<span class="wiz-inline-error">&#10007; ' + data.error + '</span>';
    } catch (_) {
      result.innerHTML = '<span class="wiz-inline-error">&#10007; Connection error</span>';
    }
    btn.disabled = false; btn.textContent = 'Send Test Message';
  };

  async function runLaunch() {
    const checksEl = document.getElementById('wiz-checks');
    const ctaEl    = document.getElementById('wiz-launch-cta');
    if (!checksEl) return;

    const checks = [
      { label: 'Configuration saved to database', fn: checkConfigSaved },
      { label: 'Database ready',                  fn: checkDatabase },
      { label: 'Telegram credentials on file',    fn: checkTelegramOnFile },
    ];

    let allPassed = true;
    for (const check of checks) {
      const row = document.createElement('div');
      row.className = 'wiz-check-row';
      row.innerHTML = '<span class="wiz-check-spin">...</span><span>' + check.label + '</span>';
      checksEl.appendChild(row);
      try {
        const r = await check.fn();
        row.innerHTML = r.ok
          ? '<span class="wiz-check-ok">&#10003;</span><span>' + check.label + '</span>'
          : '<span class="wiz-check-fail">&#10007;</span><span>' + check.label + ' - ' + (r.error || 'failed') + '</span>';
        if (!r.ok) allPassed = false;
      } catch (e) {
        row.innerHTML = '<span class="wiz-check-fail">&#10007;</span><span>' + check.label + ' - ' + e.message + '</span>';
        allPassed = false;
      }
    }

    const statusEl = document.getElementById('wiz-launch-status');
    if (statusEl) statusEl.textContent = allPassed
      ? 'All checks passed. Breadbot is configured.'
      : 'Setup complete. Fix any issues above from the Controls page.';

    if (ctaEl) ctaEl.innerHTML = '<button class="wiz-btn-primary wiz-btn-large wiz-launch-btn" onclick="Wiz_complete()">Open Dashboard</button>';
  }

  async function checkConfigSaved() {
    const r = await fetch('/api/setup/status'); const d = await r.json();
    return {ok: !!d.credentials_saved};
  }
  async function checkDatabase() {
    const r = await fetch('/api/status'); const d = await r.json();
    return {ok: !!d.db_exists};
  }
  async function checkTelegramOnFile() {
    const r = await fetch('/api/setup/status'); const d = await r.json();
    return {ok: !!d.telegram_configured, error: 'Token or chat ID not set'};
  }

  window.Wiz_complete = async function() {
    await fetch('/api/setup/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({step: 'complete', values: {setup_complete: '1'}}),
    }).catch(function() {});
    if (overlayEl) {
      overlayEl.style.transition = 'opacity 0.4s ease';
      overlayEl.style.opacity    = '0';
      var el = overlayEl;
      setTimeout(function() { if (el) el.remove(); }, 420);
    }
  };

  function showStepError(msg) {
    clearStepError();
    var el = document.createElement('div');
    el.className = 'wiz-step-error'; el.id = 'wiz-step-error'; el.textContent = msg;
    var body = document.getElementById('wiz-body');
    if (body) body.appendChild(el);
  }
  function clearStepError() {
    var el = document.getElementById('wiz-step-error');
    if (el) el.remove();
  }

})();
