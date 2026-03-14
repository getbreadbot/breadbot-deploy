'use strict';

// ── Breadbot Welcome Tour (Driver.js v1) ──────────────────────────────────
// Fires once on first visit. Persists via localStorage + POST /api/tour-complete.
// All steps target sidebar nav items so no page-switching is required.

const TOUR_KEY = 'bb_tour_v1_complete';

const TourSystem = {
  driver: null,

  init() {
    if (typeof window.driver === 'undefined') {
      console.warn('[TourSystem] Driver.js not loaded — tour disabled.');
      return;
    }
    if (localStorage.getItem(TOUR_KEY)) return; // already seen
    // Small delay so the dashboard finishes rendering
    setTimeout(() => this._launch(), 1200);
  },

  _launch() {
    const { driver } = window;

    this.driver = driver({
      animate: true,
      smoothScroll: true,
      allowClose: true,
      overlayOpacity: 0.55,
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
          // Step 2 — Alert Feed
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
          // Step 3 — Yields
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
          // Step 4 — Research
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
          // Step 5 — Controls
          element: '[data-page="controls"]',
          popover: {
            title: '⊕ Controls',
            description:
              'Pause or resume trading, adjust your risk settings, and manage ' +
              'your exchange API connections. The circuit breaker lives here too — ' +
              'it auto-pauses if you hit your daily loss limit.',
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
      body: JSON.stringify({ tour: 'welcome_v1', ts: Date.now() }),
    }).catch(() => {}); // non-blocking
  },

  // Allow replaying from the sidebar footer link
  replay() {
    localStorage.removeItem(TOUR_KEY);
    this._launch();
  },
};

window.TourSystem = TourSystem;

document.addEventListener('DOMContentLoaded', () => {
  TourSystem.init();
});
