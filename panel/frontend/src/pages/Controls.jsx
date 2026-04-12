import { useState, useEffect } from 'react'
import { get, post } from '../lib/api.js'

function ConfirmModal({ action, onConfirm, onCancel }) {
  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-title">
          {action === 'pause' ? 'Pause trading?' : 'Resume trading?'}
        </div>
        <div className="modal-body">
          {action === 'pause'
            ? 'The scanner will keep running but no new trade alerts will be sent. Existing positions are unaffected.'
            : 'Trading will resume on the next scanner cycle (within 5 minutes).'}
        </div>
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button
            className={`btn ${action === 'pause' ? 'btn-amber' : 'btn-green'}`}
            onClick={onConfirm}
          >
            {action === 'pause' ? 'Pause trading' : 'Resume trading'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function Controls() {
  const [status, setStatus] = useState(null)
  const [settings, setSettings] = useState({})
  const [loading, setLoading] = useState(true)
  const [confirming, setConfirming] = useState(null) // 'pause' | 'resume'
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  async function load() {
    try {
      const [s, set] = await Promise.all([get('/bot/status'), get('/settings/basic')])
      setStatus(s)
      setSettings(set)
    } catch {}
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  async function toggleTrading(action) {
    try {
      await post(`/bot/${action}`)
      await load()
    } catch (err) {
      alert(err.message)
    } finally {
      setConfirming(null)
    }
  }

  async function saveAutoExecute(val) {
    const newSettings = { ...settings, AUTO_EXECUTE: val ? 'auto' : 'manual' }
    setSettings(newSettings)
    setSaving(true)
    try {
      await post('/settings/basic', { settings: { AUTO_EXECUTE: val ? 'auto' : 'manual' } })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (err) {
      alert(err.message)
    } finally {
      setSaving(false)
    }
  }

  async function saveAlertChannel(val) {
    const newSettings = { ...settings, ALERT_CHANNEL: val }
    setSettings(newSettings)
    try {
      await post('/settings/basic', { settings: { ALERT_CHANNEL: val } })
    } catch (err) {
      alert(err.message)
    }
  }

  if (loading) return <div className="loading"><div className="spinner" />Loading controls...</div>

  const trading = status?.trading_active ?? false
  const autoExecute = settings?.AUTO_EXECUTE === 'auto'
  const alertChannel = settings?.ALERT_CHANNEL || 'both'

  return (
    <div>
      <div className="page-title">Controls</div>
      <div className="page-sub">Trading state and execution preferences</div>

      {/* Trading on/off */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">Trading</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 500, marginBottom: 4 }}>
              Trading is currently{' '}
              <span style={{ color: trading ? 'var(--green)' : 'var(--amber)' }}>
                {trading ? 'active' : 'paused'}
              </span>
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
              {trading
                ? 'The scanner is running and alerts will be sent on new opportunities.'
                : 'The scanner continues to run but no new alerts or trades will fire.'}
            </div>
          </div>
          {trading ? (
            <button className="btn btn-amber" onClick={() => setConfirming('pause')}>
              Pause trading
            </button>
          ) : (
            <button className="btn btn-green" onClick={() => setConfirming('resume')}>
              Resume trading
            </button>
          )}
        </div>
      </div>

      {/* Auto-execute */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">Auto-execution</div>
        <div className="toggle-row" style={{ marginBottom: 10 }}>
          <label className="toggle">
            <input
              type="checkbox"
              checked={autoExecute}
              onChange={e => saveAutoExecute(e.target.checked)}
              disabled={saving}
            />
            <span className="toggle-slider" />
          </label>
          <div>
            <div className="toggle-label">
              Auto-execute trades
              {saving && <span style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 8 }}>Saving...</span>}
              {saved && <span style={{ fontSize: 11, color: 'var(--green)', marginLeft: 8 }}>Saved</span>}
            </div>
            <div className="toggle-desc">
              When on, qualifying alerts are executed automatically without requiring a Buy press.
            </div>
          </div>
        </div>
        {autoExecute && (
          <div style={{
            background: 'rgba(245,166,35,0.06)',
            border: '1px solid var(--amber-dim)',
            borderRadius: 'var(--radius)',
            padding: '10px 14px',
            fontSize: 12,
            color: 'var(--text-2)',
          }}>
            Auto-execute is active. Alerts above score {settings?.AUTO_EXECUTE_MIN_SCORE ?? 75} will trade automatically.
            You will receive a confirmation notification after each trade. Risk limits still apply.
          </div>
        )}
      </div>

      {/* Alert channel */}
      <div className="card">
        <div className="card-title">Alert delivery</div>
        <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 16 }}>
          Choose where you receive trade alerts and bot notifications.
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {[
            { val: 'telegram', label: 'Telegram only', desc: 'All alerts sent to your Telegram bot. Panel shows history.' },
            { val: 'panel', label: 'Panel only', desc: 'Alerts appear here and as browser push notifications. Telegram receives nothing.' },
            { val: 'both', label: 'Both', desc: 'Alerts fire on Telegram and in the panel simultaneously. Actioning one marks the other done.' },
          ].map(opt => (
            <label
              key={opt.val}
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 12,
                padding: '12px 14px',
                borderRadius: 'var(--radius)',
                border: `1px solid ${alertChannel === opt.val ? 'var(--amber-dim)' : 'var(--border)'}`,
                background: alertChannel === opt.val ? 'var(--amber-glow)' : 'transparent',
                cursor: 'pointer',
              }}
            >
              <input
                type="radio"
                name="alert_channel"
                value={opt.val}
                checked={alertChannel === opt.val}
                onChange={() => saveAlertChannel(opt.val)}
                style={{ marginTop: 2, accentColor: 'var(--amber)' }}
              />
              <div>
                <div style={{ fontWeight: 500, fontSize: 13 }}>{opt.label}</div>
                <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 2 }}>{opt.desc}</div>
              </div>
            </label>
          ))}
        </div>
      </div>

      {confirming && (
        <ConfirmModal
          action={confirming}
          onConfirm={() => toggleTrading(confirming)}
          onCancel={() => setConfirming(null)}
        />
      )}
    </div>
  )
}
