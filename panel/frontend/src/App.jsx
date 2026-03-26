import { useState, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout.jsx'
import Login from './pages/Login.jsx'
import Setup from './pages/Setup.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Alerts from './pages/Alerts.jsx'
import Positions from './pages/Positions.jsx'
import Yields from './pages/Yields.jsx'
import Controls from './pages/Controls.jsx'
import Settings from './pages/Settings.jsx'
import Channels from './pages/Channels.jsx'
import { get } from './lib/api.js'

export default function App() {
  const [authState, setAuthState] = useState('loading') // loading | setup | login | authed

  useEffect(() => {
    async function check() {
      try {
        // Check if panel is configured (password has been set)
        const status = await fetch('/api/auth/status').then(r => r.json())
        if (!status.configured) { setAuthState('setup'); return }

        // Check if current session is valid
        const me = await fetch('/api/auth/me', { credentials: 'include' })
        if (me.ok) { setAuthState('authed') }
        else { setAuthState('login') }
      } catch {
        setAuthState('login')
      }
    }
    check()
  }, [])

  if (authState === 'loading') return (
    <div className="auth-wrap">
      <div className="loading"><div className="spinner" /><span>Loading...</span></div>
    </div>
  )

  if (authState === 'setup') return <Setup onComplete={() => setAuthState('authed')} />
  if (authState === 'login') return <Login onLogin={() => setAuthState('authed')} />

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard"  element={<Dashboard />} />
          <Route path="alerts"     element={<Alerts />} />
          <Route path="positions"  element={<Positions />} />
          <Route path="yields"     element={<Yields />} />
          <Route path="controls"   element={<Controls />} />
          <Route path="settings"   element={<Settings />} />
          <Route path="channels"   element={<Channels />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
