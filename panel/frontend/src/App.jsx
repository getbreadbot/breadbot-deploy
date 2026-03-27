import { useState, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout.jsx'
import Login from './pages/Login.jsx'
import Setup from './pages/Setup.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Alerts from './pages/Alerts.jsx'
import Positions from './pages/Positions.jsx'
import Yields from './pages/Yields.jsx'
import Grid from './pages/Grid.jsx'
import FundingArb from './pages/FundingArb.jsx'
import Performance from './pages/Performance.jsx'
import Controls from './pages/Controls.jsx'
import Settings from './pages/Settings.jsx'
import Channels from './pages/Channels.jsx'

export default function App() {
  const [authState, setAuthState] = useState('loading')

  useEffect(() => {
    async function check() {
      try {
        const status = await fetch('/api/auth/status').then(r => r.json())
        if (!status.configured) { setAuthState('setup'); return }
        const me = await fetch('/api/auth/me', { credentials: 'include' })
        if (me.ok) setAuthState('authed')
        else setAuthState('login')
      } catch { setAuthState('login') }
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
          <Route path="dashboard"   element={<Dashboard />} />
          <Route path="alerts"      element={<Alerts />} />
          <Route path="positions"   element={<Positions />} />
          <Route path="yields"      element={<Yields />} />
          <Route path="grid"        element={<Grid />} />
          <Route path="funding"     element={<FundingArb />} />
          <Route path="performance" element={<Performance />} />
          <Route path="controls"    element={<Controls />} />
          <Route path="settings"    element={<Settings />} />
          <Route path="channels"    element={<Channels />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
