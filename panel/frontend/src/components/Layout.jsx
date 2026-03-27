import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import { useState, useEffect } from 'react'
import { get, post } from '../lib/api.js'

const NAV_OPERATIONS = [
  { to: '/dashboard', label: 'Dashboard',  icon: '▦' },
  { to: '/alerts',    label: 'Alerts',     icon: '◎', alertBadge: true },
  { to: '/positions', label: 'Positions',  icon: '◈' },
  { to: '/yields',    label: 'Yields',     icon: '⟁' },
  { to: '/controls',  label: 'Controls',   icon: '◉' },
]

const NAV_STRATEGIES = [
  { to: '/grid',    label: 'Grid Trading', icon: '⊞' },
  { to: '/funding', label: 'Funding Arb',  icon: '⇄' },
]

const NAV_SYSTEM = [
  { to: '/performance', label: 'Performance', icon: '◈' },
  { to: '/settings',   label: 'Settings',    icon: '⚙' },
]

export default function Layout() {
  const navigate = useNavigate()
  const [botStatus, setBotStatus] = useState(null)
  const [pendingAlerts, setPendingAlerts] = useState(0)

  useEffect(() => {
    async function fetchStatus() {
      try {
        const s = await get('/bot/status')
        setBotStatus(s)
      } catch { setBotStatus(null) }
    }
    fetchStatus()
    const iv = setInterval(fetchStatus, 30000)
    return () => clearInterval(iv)
  }, [])

  // Listen for WebSocket alert count
  useEffect(() => {
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/ws/alerts`)
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (msg.type === 'alert') setPendingAlerts(n => n + 1)
        if (msg.type === 'decision_ack') setPendingAlerts(n => Math.max(0, n - 1))
      } catch {}
    }
    return () => ws.close()
  }, [])

  async function logout() {
    await post('/auth/logout')
    window.location.href = '/'
  }

  const statusDot = botStatus
    ? (botStatus.trading_active ? 'green' : 'amber')
    : 'red'
  const statusLabel = botStatus
    ? (botStatus.trading_active ? 'Active' : 'Paused')
    : 'Offline'

  return (
    <div className="layout">
      <header className="header">
        <span className="header-logo">⬡ BREADBOT</span>
        <span className="header-sep" />
        <div className="header-status">
          <div className={`dot ${statusDot}`} />
          <span>{statusLabel}</span>
        </div>
        <button
          className="btn btn-ghost btn-sm"
          style={{ marginLeft: 16 }}
          onClick={logout}
        >
          Sign out
        </button>
      </header>

      <nav className="nav">
        <div className="nav-section">Operations</div>
        {NAV_OPERATIONS.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
          >
            <span style={{ fontSize: 12, opacity: 0.7 }}>{item.icon}</span>
            {item.label}
            {item.alertBadge && pendingAlerts > 0 && (
              <span className="badge">{pendingAlerts}</span>
            )}
          </NavLink>
        ))}
        <div className="nav-section" style={{ marginTop: 10 }}>Strategies</div>
        {NAV_STRATEGIES.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
          >
            <span style={{ fontSize: 12, opacity: 0.7 }}>{item.icon}</span>
            {item.label}
          </NavLink>
        ))}
        <div className="nav-section" style={{ marginTop: 10 }}>System</div>
        {NAV_SYSTEM.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
          >
            <span style={{ fontSize: 12, opacity: 0.7 }}>{item.icon}</span>
            {item.label}
          </NavLink>
        ))}
      </nav>

      <main className="main">
        <Outlet />
      </main>

      <footer style={{
        gridArea: 'main',
        borderTop: '1px solid var(--border)',
        padding: '10px 20px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'flex-end',
        gap: 16,
        fontSize: 11,
        color: 'var(--text-3)',
      }}>
        <a
          href="/terms.html"
          target="_blank"
          rel="noopener noreferrer"
          style={{ color: 'var(--text-3)', textDecoration: 'none' }}
          onMouseOver={e => e.target.style.color = 'var(--amber)'}
          onMouseOut={e => e.target.style.color = 'var(--text-3)'}
        >
          Terms of Service
        </a>
        <span style={{ opacity: 0.4 }}>|</span>
        <span>Breadbot LLC &copy; 2026</span>
      </footer>
    </div>
  )
}
