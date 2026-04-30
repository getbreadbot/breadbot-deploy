import { useEffect, useState, useMemo } from 'react'
import { get } from '../lib/api.js'

/**
 * Inline OHLCV chart for a single position.
 * Hand-rolled SVG (matches Performance.jsx style, no recharts dependency).
 *
 * Renders:
 * - Price line (close-of-candle)
 * - Entry price horizontal line (amber)
 * - Stop loss horizontal line (red)
 * - TP25 / TP50 horizontal lines (green, dashed)
 * - Hover crosshair with timestamp + price tooltip
 */
export default function PriceChart({ positionId, height = 220 }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [hover, setHover] = useState(null)  // { x, y, candle }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    get(`/bot/positions/${positionId}/ohlcv`)
      .then(d => {
        if (cancelled) return
        if (!d || (d.error && (!d.candles || d.candles.length === 0))) {
          setError(d?.error || 'No data')
        } else if (!d.candles || d.candles.length === 0) {
          setError('No price history available')
        } else {
          setData(d)
        }
        setLoading(false)
      })
      .catch(err => {
        if (cancelled) return
        setError(err.message || 'Failed to load chart')
        setLoading(false)
      })
    return () => { cancelled = true }
  }, [positionId])

  // Compute chart geometry
  const chart = useMemo(() => {
    if (!data || !data.candles?.length) return null

    const candles = data.candles
    const W = 900            // viewBox width — SVG scales to container
    const H = height
    const PAD_L = 56
    const PAD_R = 16
    const PAD_T = 12
    const PAD_B = 28

    // Y range: include all candle highs/lows AND the overlay lines
    const overlayValues = [
      data.entry_price, data.stop_loss,
      data.take_profit_25, data.take_profit_50,
    ].filter(v => v != null && Number.isFinite(v) && v > 0)
    const allHighs = candles.map(c => c.h)
    const allLows  = candles.map(c => c.l)
    const yMin = Math.min(...allLows,  ...overlayValues) * 0.98
    const yMax = Math.max(...allHighs, ...overlayValues) * 1.02
    const xMin = candles[0].t
    const xMax = candles[candles.length - 1].t || (xMin + 1)

    const xToPx = t => PAD_L + ((t - xMin) / (xMax - xMin || 1)) * (W - PAD_L - PAD_R)
    const yToPx = p => PAD_T + (1 - (p - yMin) / (yMax - yMin || 1)) * (H - PAD_T - PAD_B)

    // Price line points
    const linePts = candles.map(c => `${xToPx(c.t).toFixed(1)},${yToPx(c.c).toFixed(1)}`).join(' ')

    // Y-axis labels (4 ticks)
    const ticks = []
    for (let i = 0; i <= 3; i++) {
      const v = yMin + ((yMax - yMin) * i) / 3
      ticks.push({ y: yToPx(v), label: v < 0.001 ? v.toExponential(2) : v.toFixed(v < 1 ? 6 : 2) })
    }

    return {
      W, H, PAD_L, PAD_R, PAD_T, PAD_B,
      candles, linePts, ticks, xToPx, yToPx,
      xMin, xMax, yMin, yMax,
    }
  }, [data, height])

  function handleMouseMove(e) {
    if (!chart) return
    const rect = e.currentTarget.getBoundingClientRect()
    const px = e.clientX - rect.left
    const ratio = chart.W / rect.width
    const xVB = px * ratio
    if (xVB < chart.PAD_L || xVB > chart.W - chart.PAD_R) {
      setHover(null)
      return
    }
    // Find nearest candle
    const tApprox = chart.xMin + ((xVB - chart.PAD_L) / (chart.W - chart.PAD_L - chart.PAD_R)) * (chart.xMax - chart.xMin)
    let nearest = chart.candles[0]
    let bestDt = Math.abs(nearest.t - tApprox)
    for (const c of chart.candles) {
      const dt = Math.abs(c.t - tApprox)
      if (dt < bestDt) { nearest = c; bestDt = dt }
    }
    setHover({
      x: chart.xToPx(nearest.t),
      y: chart.yToPx(nearest.c),
      candle: nearest,
    })
  }

  if (loading) return (
    <div style={{ padding: 24, color: 'var(--text-3)', textAlign: 'center', fontSize: 12 }}>
      Loading chart…
    </div>
  )
  if (error) return (
    <div style={{ padding: 24, color: 'var(--text-3)', textAlign: 'center', fontSize: 12 }}>
      {error}
    </div>
  )
  if (!chart) return null

  const { W, H, PAD_L, PAD_R, PAD_T, PAD_B, ticks, xToPx, yToPx, xMin, xMax } = chart
  const entry = data.entry_price
  const sl    = data.stop_loss
  const tp25  = data.take_profit_25
  const tp50  = data.take_profit_50

  function fmtTime(t) {
    const d = new Date(t * 1000)
    return d.toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    })
  }
  function fmtPrice(v) {
    if (v == null) return '—'
    if (v < 0.001) return v.toExponential(3)
    if (v < 1) return v.toFixed(6)
    return v.toFixed(2)
  }

  return (
    <div style={{ position: 'relative' }}>
      <svg
        width="100%"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        style={{ display: 'block', cursor: 'crosshair' }}
        onMouseMove={handleMouseMove}
        onMouseLeave={() => setHover(null)}
      >
        {/* Background grid lines + Y axis labels */}
        {ticks.map((tk, i) => (
          <g key={i}>
            <line
              x1={PAD_L} x2={W - PAD_R} y1={tk.y} y2={tk.y}
              stroke="var(--border)" strokeWidth="1" strokeDasharray="2,3"
            />
            <text
              x={PAD_L - 6} y={tk.y + 4}
              textAnchor="end" fontSize="10"
              fill="var(--text-3)" fontFamily="var(--mono)"
            >
              {tk.label}
            </text>
          </g>
        ))}

        {/* X axis time labels — start, middle, end */}
        {[0, Math.floor(chart.candles.length / 2), chart.candles.length - 1].map((idx, i) => {
          const c = chart.candles[idx]
          if (!c) return null
          const x = xToPx(c.t)
          const d = new Date(c.t * 1000)
          const lbl = d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric' })
          return (
            <text
              key={i} x={x} y={H - 8}
              textAnchor={i === 0 ? 'start' : i === 2 ? 'end' : 'middle'}
              fontSize="10" fill="var(--text-3)" fontFamily="var(--mono)"
            >
              {lbl}
            </text>
          )
        })}

        {/* Price line */}
        <polyline
          points={chart.linePts}
          fill="none" stroke="var(--text)" strokeWidth="1.6"
          strokeLinecap="round" strokeLinejoin="round"
        />

        {/* Entry price line (amber, solid) */}
        {entry != null && entry > 0 && (
          <g>
            <line
              x1={PAD_L} x2={W - PAD_R} y1={yToPx(entry)} y2={yToPx(entry)}
              stroke="var(--amber)" strokeWidth="1.2" strokeDasharray="6,3"
            />
            <text
              x={W - PAD_R - 4} y={yToPx(entry) - 4}
              textAnchor="end" fontSize="10"
              fill="var(--amber)" fontFamily="var(--mono)"
            >
              entry {fmtPrice(entry)}
            </text>
          </g>
        )}

        {/* Stop loss line (red, dashed) */}
        {sl != null && sl > 0 && (
          <g>
            <line
              x1={PAD_L} x2={W - PAD_R} y1={yToPx(sl)} y2={yToPx(sl)}
              stroke="var(--red)" strokeWidth="1" strokeDasharray="3,3"
            />
            <text
              x={W - PAD_R - 4} y={yToPx(sl) - 4}
              textAnchor="end" fontSize="10"
              fill="var(--red)" fontFamily="var(--mono)"
            >
              SL {fmtPrice(sl)}
            </text>
          </g>
        )}

        {/* TP25 line (green, dashed) */}
        {tp25 != null && tp25 > 0 && (
          <g>
            <line
              x1={PAD_L} x2={W - PAD_R} y1={yToPx(tp25)} y2={yToPx(tp25)}
              stroke="var(--green)" strokeWidth="1" strokeDasharray="3,3"
              opacity="0.7"
            />
            <text
              x={W - PAD_R - 4} y={yToPx(tp25) - 4}
              textAnchor="end" fontSize="10"
              fill="var(--green)" fontFamily="var(--mono)"
              opacity="0.8"
            >
              TP25 {fmtPrice(tp25)}
            </text>
          </g>
        )}

        {/* TP50 line (green, lighter dashed) */}
        {tp50 != null && tp50 > 0 && (
          <g>
            <line
              x1={PAD_L} x2={W - PAD_R} y1={yToPx(tp50)} y2={yToPx(tp50)}
              stroke="var(--green)" strokeWidth="1" strokeDasharray="6,4"
              opacity="0.5"
            />
            <text
              x={W - PAD_R - 4} y={yToPx(tp50) - 4}
              textAnchor="end" fontSize="10"
              fill="var(--green)" fontFamily="var(--mono)"
              opacity="0.6"
            >
              TP50 {fmtPrice(tp50)}
            </text>
          </g>
        )}

        {/* Hover crosshair */}
        {hover && (
          <g pointerEvents="none">
            <line
              x1={hover.x} x2={hover.x} y1={PAD_T} y2={H - PAD_B}
              stroke="var(--text-2)" strokeWidth="0.8" strokeDasharray="2,2"
            />
            <circle cx={hover.x} cy={hover.y} r="3.5" fill="var(--amber)" stroke="var(--bg)" strokeWidth="1.5" />
          </g>
        )}
      </svg>

      {/* Hover tooltip — positioned relative to container */}
      {hover && (
        <div style={{
          position: 'absolute',
          top: 8,
          left: 12,
          background: 'var(--bg-2)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius)',
          padding: '6px 10px',
          fontSize: 11,
          fontFamily: 'var(--mono)',
          color: 'var(--text-2)',
          pointerEvents: 'none',
          whiteSpace: 'nowrap',
        }}>
          <div style={{ color: 'var(--text-3)', fontSize: 10, marginBottom: 2 }}>
            {fmtTime(hover.candle.t)}
          </div>
          <div>
            <span style={{ color: 'var(--text)' }}>${fmtPrice(hover.candle.c)}</span>
            {entry > 0 && (
              <span style={{
                marginLeft: 8,
                color: hover.candle.c >= entry ? 'var(--green)' : 'var(--red)',
              }}>
                ({hover.candle.c >= entry ? '+' : ''}{((hover.candle.c - entry) / entry * 100).toFixed(1)}%)
              </span>
            )}
          </div>
        </div>
      )}

      {/* Footer: data source + freshness */}
      <div style={{
        marginTop: 4, fontSize: 10, color: 'var(--text-3)',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
      }}>
        <span>15-min OHLCV via GeckoTerminal · {data.candles.length} candles</span>
        {data.stale_seconds > 0 && (
          <span>cached {data.stale_seconds}s ago</span>
        )}
      </div>
    </div>
  )
}
