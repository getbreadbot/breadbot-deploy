'use strict';

// ── Breadbot Welcome Tour (Driver.js v1) ──────────────────────────────────
// Fires once on first visit. Persists via localStorage + POST /api/tour-complete.
// All steps target sidebar nav items so no page-switching is required.

const TOUR_KEY = 'bb_tour_v2_complete';

const TourSystem = {
  driver: null,

  init() {
    if (typeof window.driver === 'undefined' || typeof window.driver.js === 'undefined' || typeof window.driver.js.driver !== 'function') {
      console.warn('[TourSystem] Driver.js not loaded — tour disabled.');
      return;
    }
    if (localStorage.getItem(TOUR_KEY)) return; // already seen
    // Small delay so the dashboard finishes rendering
    setTimeout(() => this._launch(), 3000);
  },

  _launch() {
    const { driver } = window.driver.js;

    this.driver = driver({
      animate: true,
      smoothScroll: true,
      allowClose: true,
      overlayOpacity: 0.85,
      stagePadding: 6,
      stageRadius: 6,
      popoverOffset: 10,
      showProgress: true,
      progressText: '{{current}} of {{total}}',

      nextBtnText: 'Next →',
      prevBtnText: '← Back',
      doneBtnText: 'Got it',

      onDestroyStarted: () => {
        this._markComplete();
        this.driver.destroy();
      },

      steps: [
        {
          // Step 1 — Overview
          element: '[data-page="overview"]',
          popover: {
            title: '👋 Welcome to Breadbot',
            description:
              'This is your command center. Overview shows real-time P&L, ' +
              'portfolio value, bot status, and today\'s scanner activity — ' +
              'all updating every 30 seconds.',
            side: 'right',
            align: 'start',
          },
        },
        {
          // Step 2 — Composite Signal card (Overview page)
          element: '[title="Click to open Signals page"]',
          popover: {
            title: '◉ Composite Signal',
            description:
              'This card shows the live composite signal score — a weighted ' +
              'reading from Fear & Greed, Kalshi prediction markets, and funding ' +
              'rates. When it drops below -50, trading pauses automatically. ' +
              'Click it any time to open the full Signals page.',
            side: 'bottom',
            align: 'start',
          },
        },
        {
          // Step 3 — Alert Feed
          element: '[data-page="alerts"]',
          popover: {
            title: '◈ Alert Feed',
            description:
              'Every token the scanner finds lands here with its security score, ' +
              'liquidity, and volume. Tap Buy or Skip — your decisions are logged ' +
              'automatically.',
            side: 'right',
            align: 'start',
          },
        },
        {
          // Step 4 — Yields
          element: '[data-page="yields"]',
          popover: {
            title: '◇ Yields',
            description:
              'Stablecoin APY across all connected platforms, checked hourly. ' +
              'You get an alert when any rate moves by more than 0.5%.',
            side: 'right',
            align: 'start',
          },
        },
        {
          // Step 5 — Flash Loans
          element: '[data-page="flashloans"]',
          popover: {
            title: '⚡ Flash Loans',
            description:
              'Monitor and manage flash loan arbitrage activity on Base. ' +
              'Tracks your deployed contract, successful executions, profit, ' +
              'and success rate in real time.',
            side: 'right',
            align: 'start',
          },
        },
        {
          // Step 6 — Analytics
          element: '[data-page="analytics"]',
          popover: {
            title: '📊 Analytics',
            description:
              'Historical performance charts — P&L over time, scanner alert ' +
              'volume, security score distribution, and yield trends. Use this ' +
              'to review how the bot is performing over days and weeks.',
            side: 'right',
            align: 'start',
          },
        },
        {
          // Step 7 — Signals
          element: '[data-page="signals"]',
          popover: {
            title: '◉ Signals',
            description:
              'Live alternative data — Fear & Greed index, Kalshi prediction market ' +
              'probabilities, perpetual funding rates, and a composite signal score ' +
              'that adjusts position sizing automatically.',
            side: 'right',
            align: 'start',
          },
        },
        {
          // Step 8 — Research
          element: '[data-page="research"]',
          popover: {
            title: '🔍 Research',
            description:
              'Paste any contract address for an instant on-demand rug check — ' +
              'honeypot detection, liquidity lock status, ownership, and top holders.',
            side: 'right',
            align: 'start',
          },
        },
        {
          // Step 9 — Controls
          element: '[data-page="controls"]',
          popover: {
            title: '⊕ Controls',
            description:
              'Pause or resume trading, adjust your risk settings, and manage ' +
              'your exchange API connections. The circuit breaker lives here — ' +
              'it auto-pauses if you hit your daily loss limit. Toggle auto-execution ' +
              'mode any time via Telegram: send /automode on or /automode off.',
            side: 'right',
            align: 'start',
          },
        },
      ],
    });

    this.driver.drive();
  },

  _markComplete() {
    localStorage.setItem(TOUR_KEY, '1');
    fetch('/api/tour-complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tour: 'welcome_v2', ts: Date.now() }),
    }).catch(() => {}); // non-blocking
  },

  // Allow replaying from the sidebar footer link
  replay() {
    if (this.driver) {
      try { this.driver.destroy(); } catch(e) {}
      this.driver = null;
    }
    localStorage.removeItem(TOUR_KEY);
    setTimeout(() => this._launch(), 200);
  },
};

window.TourSystem = TourSystem;

document.addEventListener('DOMContentLoaded', () => {
  TourSystem.init();
});
