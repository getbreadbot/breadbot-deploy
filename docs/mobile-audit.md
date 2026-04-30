# Mobile Usability Audit — Breadbot Panel

*Written 2026-04-30 (S78). Audit scope: panel.breadbot.app, demo.breadbot.app — they share one codebase.*

---

## What this document is

A grounded list of mobile usability issues found by inspecting the live styles and components, paired with proposed fixes ranked by impact. Not a wishlist — every finding cites a real file and line.

This is intentionally a follow-up to the hamburger menu work shipped in S78 P3 (`4192fca`). That commit solved the navigation problem; what remains is the long tail of friction once a user is on a page.

---

## What S78 already shipped

| Commit | Fix |
|---|---|
| `4192fca` (S78 P3) | Hamburger + slide-out drawer at ≤768px. Sidebar gone on mobile, drawer with backdrop scrim, auto-close on route change, body scroll lock while open. Tap targets on nav links bumped from 8px→12px padding. Header tightened. iPhone SE (≤380px) extra tightening for the logo and Sign-out button. |
| `4192fca` (S78 P3) | Bonus catch: pre-existing WebSocket bug where `location.protocol/host` referenced the React Router location object (not `window.location`). The bug was masked because react-router used to alias `location` to the global; switched to explicit `window.location`. |
| `b4286f9` (S78 P2) | Token label format — alerts now say `The Chosen One ($Cayaha)` instead of just `Cayaha`. Recognizable name leads, ticker in parens. Applies to auto-buy, manual approval, pullback start, and SL/TP exit messages. |
| `1c90636` (S78 P4) | Native price chart on positions, expandable per row. Hand-rolled SVG (no recharts dependency added — bundle stayed under 270 kB). Overlay lines for entry/SL/TP25/TP50, hover crosshair with timestamp + percent vs entry. |

---

## Open findings, prioritized

### Tier 1 — fix next session

These are bugs masquerading as polish issues. Each one actively makes the app worse on a phone.

**1. Form inputs zoom-trigger on iOS Safari.** `styles.css:397` — `.input { font-size: 13px }`. Safari on iPhone auto-zooms the viewport whenever a focused text input has font-size below 16px. Every time you tap a password field or the Add Channel input, the page zooms in and stays zoomed until you manually pinch back out. The fix is one line: bump `.input` font-size to 16px, or add a media query that does so only on mobile. Cost: 10 minutes including a build. Affected pages: Login, Setup, Settings, Add Channel modal in Signal Channels, anything else with a form field.

**2. Modal overflows the screen on iPhone SE.** `styles.css:520` — `.modal { width: 420px }` with no `max-width`. The smallest iPhone in active use has a 375px viewport. The Close-position confirm modal currently overflows by 45px and gets cropped on the right. Fix: change to `width: 420px; max-width: calc(100vw - 32px)`. Cost: one line.

**3. Action buttons fail touch-target guidelines.** `styles.css:228` — `.btn-sm { padding: 5px 12px; font-size: 12px }`. The computed height is 22px. Apple's HIG requires 44px minimum tap targets, Google's Material requires 48px. The Close, Skip, BUY, and Chart buttons in the Positions table are all `.btn-sm`. They're hittable, but mistaps are common. Fix: a media query at ≤768px that bumps `.btn-sm` to `padding: 10px 14px`. Cost: 5 minutes.

### Tier 2 — fix when you next touch the affected page

These create real annoyances but aren't bugs. Worth handling lazily as you ship feature work on each page.

**4. Tables overflow horizontally with no indication.** Positions, Yields, Performance, Backtest, FundingArb, Dashboard all use `<table className="table">`. On a 375px viewport, an 8-column table renders at desktop width and forces side-scrolling. There's no visual cue that more content exists to the right. Two viable approaches:
- *Card stacking:* below 768px, transform table rows into stacked cards (each row becomes a card with label/value pairs). Most preserved data, biggest CSS lift.
- *Horizontal scroll with shadow:* wrap each table in a `<div style="overflow-x:auto">` with a CSS-driven gradient on the right edge that signals "scroll for more". Smaller change.

I'd suggest the shadow approach first; only escalate to card stacking on Positions if mobile usage data shows people get stuck.

**5. Notification format still busy on mobile.** S78 P2 fixed the token label, but the position-manager messages still lead with `🔔 *Position Manager*: TP25 exit — `. On a phone notification preview, the prefix eats the first 25 characters before the actual content starts. Quieter format: drop the emoji and the bold, prefix with just the action — `Exit TP25 · The Chosen One ($Cayaha) (#72)`. Same information, half the visual weight. Fix is in `position_manager.py` lines ~720 and ~737, tiny diff.

**6. Recharts vs SVG inconsistency.** `Performance.jsx` uses hand-rolled SVG; new `PriceChart.jsx` (S78 P4) also uses hand-rolled SVG. If a future feature adds a chart and reaches for recharts, the bundle jumps 70 kB and styling drifts. Worth adding a `components/Chart/` directory with a standardized primitive (axis labels, grid, tooltip helpers) so the next chart is 30 lines, not 200.

### Tier 3 — defer, track, revisit if data shows we should care

Real but speculative. Defer until usage shows the problem matters.

**7. Auto-refresh while scrolling jumps the page.** Positions polls every 20s (`Positions.jsx:42`); when state replaces, React reconciles and the scroll position can jolt if a row's height changes. Mitigation: virtualize, or skip refresh while the user is interacting (mousemove/touchmove debounce). Defer until a real customer complains.

**8. WebSocket reconnect on network change.** Phones swap between Wi-Fi and LTE constantly. The current alert WebSocket has no reconnect logic — if it drops, the badge counter goes stale silently. Fix: exponential-backoff reconnect plus a "stale" indicator on the badge if the socket has been down >30s. Worth doing eventually but not urgent.

**9. Login screen has no "remember device" toggle.** Every fresh app open prompts for the panel password. On a phone this is high-friction. A `localStorage` flag tied to a long-lived session cookie would solve it. Security tradeoff: the panel password is the only auth factor right now, so persistent login means anyone with the unlocked phone gets in. Decision deferred to Morgan.

---

## Test rig

Future mobile work should verify against these viewports:
- **iPhone SE (3rd gen):** 375 × 667 — smallest current iPhone
- **iPhone 15 Pro:** 393 × 852 — modal middle of the road
- **iPhone 15 Pro Max:** 430 × 932 — generous mobile
- **Samsung Galaxy S22:** 360 × 780 — narrowest common Android
- **iPad Mini:** 768 × 1024 — exactly at the breakpoint, worth checking the layout transition

Chrome DevTools device emulation works for layout testing. Real-device verification is needed for iOS Safari font-zoom (#1) and touch behavior (#3) since those don't reproduce in DevTools.

---

## What I'm explicitly *not* recommending

- **PWA / installable app.** The web panel works as a tab. Adding manifest + service worker is real engineering for marginal gain over "add to home screen." Defer indefinitely.
- **React Native rewrite.** Same logic — the panel is small enough that responsive web is the right answer. A native app would be 10× the maintenance for users who would mostly use it the same way.
- **Dark/light theme toggle.** The panel is dark-only and that's fine — meme-coin trading at 3am benefits from dark UI. Adding theming is a real lift; not worth it until customers ask twice.

---

## Tracking

When fixes ship, update this doc with the commit SHA and date so it stays a living record rather than rotting into a TODO list. The pattern from the "What S78 already shipped" table at the top is the format.
