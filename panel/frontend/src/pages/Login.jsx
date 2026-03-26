// Login.jsx
import { useState } from 'react'
import { post } from '../lib/api.js'

export function Login({ onLogin }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      await post('/auth/login', { password })
      onLogin()
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
        <div className="auth-sub">Control panel — sign in to continue</div>
        {error && <div className="auth-error">{error}</div>}
        <div className="field">
          <label className="field-label">Panel password</label>
          <input
            type="password"
            className="input"
            value={password}
            onChange={e => setPassword(e.target.value)}
            placeholder="Enter your panel password"
            autoFocus
          />
        </div>
        <button type="submit" className="btn btn-amber" style={{ width: '100%' }} disabled={loading}>
          {loading ? 'Signing in...' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}

export default Login
