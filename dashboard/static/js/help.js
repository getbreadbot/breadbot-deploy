'use strict';

const HELP_COPY = {
  security_score:
    'This score tells you how safe a token looks based on automated checks. ' +
    'It starts at 100 and drops when the bot detects red flags like hidden tax ' +
    'functions, unlocked liquidity, or concentrated ownership. ' +
    'Anything above 78 is worth reviewing manually before deciding.',

  daily_pnl:
    'Today\'s realized profit or loss in USD. ' +
    'It resets to zero at midnight and only counts positions that have been ' +
    'fully closed — open positions are not included until you exit them.',

  portfolio_value:
    'The total estimated value of your account. ' +
    'It combines your cash balance with the current market value of all open ' +
    'positions, so it will fluctuate as prices move.',

  position_size:
    'The dollar amount the risk manager recommends putting into this trade. ' +
    'It is calculated from your portfolio size and the token\'s security score — ' +
    'higher-scoring tokens get a larger allocation up to your configured maximum.',

  daily_loss_limit:
    'A circuit breaker that automatically pauses all new trades if your ' +
    'realized losses today reach this percentage of your portfolio. ' +
    'For example, a 5% limit on a $5,000 account stops trading after $250 in losses. ' +
    'You can resume manually from the Controls page.',

  yield_rate:
    'The current annual percentage yield (APY) on your stablecoin holdings ' +
    'at this platform. ' +
    'The bot checks all connected platforms hourly and alerts you when rates change ' +
    'by more than 0.5%.',

  portfolio_utilization:
    'What percentage of your total portfolio is currently tied up in open positions. ' +
    'A high utilization means less cash available for new trades. ' +
    'Most risk managers recommend keeping this below 60–70%.',

  stop_loss:
    'The price at which the bot will alert you to exit this position to cap your loss. ' +
    'It is set automatically at −6.5% from your entry price when a position is opened.',

  take_profit:
    'The price target at which you should consider taking partial profits. ' +
    'The first target is set at +15% above your entry price.',

  daily_loss_pct_used:
    'How much of today\'s loss limit you have already consumed. ' +
    'At 100%, trading pauses automatically. ' +
    'The counter resets every midnight.',

  open_positions:
    'The number of meme coin positions currently open and being tracked. ' +
    'Each position was opened by pressing Buy on an alert.',

  alerts_today:
    'How many scanner alerts passed all filters and were sent to Telegram today. ' +
    'This count includes both pending and decided alerts.',

  last_scan:
    'When the scanner last ran its check of DEXScreener for new token pairs. ' +
    'The scanner runs automatically every 5 minutes.',
};

const TIPPY_DEFAULTS = {
  trigger: 'click',
  placement: 'right',
  theme: 'breadbot',
  maxWidth: 280,
  interactive: false,
  arrow: true,
  animation: 'shift-away',
  hideOnClick: true,
  touch: ['hold', 300],
};

const HelpSystem = {
  instances: [],

  init() {
    if (typeof tippy === 'undefined') {
      console.warn('[HelpSystem] Tippy.js not loaded — tooltips disabled.');
      return;
    }
    this._destroyAll();
    const targets = document.querySelectorAll('[data-help]');
    if (!targets.length) return;
    targets.forEach(el => {
      const key     = el.dataset.help;
      const content = HELP_COPY[key] || el.dataset.helpContent || null;
      if (!content) return;
      const instance = tippy(el, { ...TIPPY_DEFAULTS, content });
      if (Array.isArray(instance)) {
        this.instances.push(...instance);
      } else {
        this.instances.push(instance);
      }
    });
  },

  refresh() { this.init(); },

  createHelpIcon(key) {
    const copy = HELP_COPY[key] || '';
    const btn = document.createElement('button');
    btn.className    = 'help-icon';
    btn.type         = 'button';
    btn.ariaLabel    = 'Help';
    btn.dataset.help = key;
    if (!copy) btn.dataset.helpContent = 'No help text available for this metric.';
    const inner      = document.createElement('span');
    inner.className  = 'help-icon-inner';
    inner.textContent = '?';
    inner.setAttribute('aria-hidden', 'true');
    btn.appendChild(inner);
    return btn;
  },

  _destroyAll() {
    this.instances.forEach(inst => { try { inst.destroy(); } catch(e) {} });
    this.instances = [];
  },
};

window.HelpSystem = HelpSystem;
window.HELP_COPY  = HELP_COPY;
