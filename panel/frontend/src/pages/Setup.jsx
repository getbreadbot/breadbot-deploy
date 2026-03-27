import { useState } from 'react'
import { post } from '../lib/api.js'

export default function Setup({ onComplete }) {
  const [licenseKey, setLicenseKey] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [tosAccepted, setTosAccepted] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    if (!tosAccepted) { setError('You must accept the Terms of Service to continue'); return }
    if (password.length < 8) { setError('Password must be at least 8 characters'); return }
    if (password !== confirm) { setError('Passwords do not match'); return }
    setLoading(true)
    try {
      await post('/auth/setup', { license_key: licenseKey, password })
      onComplete()
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="auth-wrap">
      <form className="auth-box" onSubmit={handleSubmit}>
        <div className="auth-logo">⬡ BREADBOT</div>
        <div className="auth-sub">First time setup — confirm your license and set a panel password</div>
        {error && <div className="auth-error">{error}</div>}

        <div className="field">
          <label className="field-label">Whop license key</label>
          <input
            type="text"
            className="input mono"
            value={licenseKey}
            onChange={e => setLicenseKey(e.target.value)}
            placeholder="Your Whop license key"
            autoFocus
          />
          <div className="field-desc">Found in your Whop account under Purchases</div>
        </div>

        <div className="field">
          <label className="field-label">Panel password</label>
          <input
            type="password"
            className="input"
            value={password}
            onChange={e => setPassword(e.target.value)}
            placeholder="Minimum 8 characters"
          />
        </div>

        <div className="field">
          <label className="field-label">Confirm password</label>
          <input
            type="password"
            className="input"
            value={confirm}
            onChange={e => setConfirm(e.target.value)}
            placeholder="Repeat password"
          />
          <div className="field-desc">This password is stored in your Railway environment. You can reset it there if needed.</div>
        </div>

        <div className="field" style={{ marginBottom: 16 }}>
          <label style={{ display: 'flex', alignItems: 'flex-start', gap: 10, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={tosAccepted}
              onChange={e => setTosAccepted(e.target.checked)}
              style={{ marginTop: 3, accentColor: 'var(--amber)', flexShrink: 0 }}
            />
            <span style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.5 }}>
              I confirm that I have the legal right to use each exchange I connect in my jurisdiction,
              and I have read and agree to the{' '}
              <a href="/terms.html" target="_blank" rel="noopener noreferrer"
                 style={{ color: 'var(--amber)', textDecoration: 'underline' }}>
                Terms of Service
              </a>.
            </span>
          </label>
        </div>

        <button type="submit" className="btn btn-amber" style={{ width: '100%' }} disabled={loading || !tosAccepted}>
          {loading ? 'Verifying...' : 'Set up panel'}
        </button>
      </form>
    </div>
  )
}
