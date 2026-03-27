import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import { useState, useEffect } from 'react'
import { get, post } from '../lib/api.js'

const NAV = [
  { to: '/dashboard',   label: 'Dashboard',    icon: '▦', section: 'Operations' },
  { to: '/alerts',      label: 'Alerts',       icon: '◎', section: null, alertBadge: true },
  { to: '/positions',   label: 'Positions',    icon: '◈', section: null },
  { to: '/yields',      label: 'Yields',       icon: '⟁', section: null },
  { to: '/grid',        label: 'Grid Trading', icon: '⊞', section: 'Strategies' },
  { to: '/funding',     label: 'Funding Arb',  icon: '⇌', section: null },
  { to: '/performance', label: 'Performance',  icon: '◱', section: null },
  { to: '/controls',    label: 'Controls',     icon: '◉', section: 'Config' },
  { to: '/settings',    label: 'Settings',     icon: '⚙', section: null },
  { to: '/channels',    label: 'Channels',     icon: '◫', section: null },
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
        {NAV.map(item => (
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
      </nav>

      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}
