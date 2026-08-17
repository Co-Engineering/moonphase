import { useState } from 'react'
import * as api from '../lib/api'
import { useResource } from '../lib/useResource'

/**
 * What the agents have spent, and how much of your limit is left.
 *
 * The first version of this screen answered the wrong question. It showed a
 * trailing five-hour sum, which is not what a subscription limit is: the
 * window opens with your first message and resets at a fixed time, and "used
 * 4.7M tokens" tells you nothing without knowing when it comes back. The
 * numbers people actually want are how full the window is and what time it
 * clears — the same two facts the harness itself reports.
 *
 * The allowance is the one thing that cannot be derived. It is not published,
 * and reading it from the provider would mean using a session's own OAuth
 * token on the person's behalf. So it is asked for once, and until it is given
 * the bar is absent rather than drawn against a number nobody supplied.
 */

const SPANS: { label: string; hours: number }[] = [
  { label: '24 hours', hours: 24 },
  { label: '7 days', hours: 24 * 7 },
  { label: '30 days', hours: 24 * 30 },
]

export function compact(tokens: number): string {
  if (tokens >= 1_000_000_000) return `${(tokens / 1_000_000_000).toFixed(1)}B`
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(0)}K`
  return String(tokens)
}

export function money(value: number | null): string {
  if (value === null) return '—'
  if (value > 0 && value < 0.01) return '<$0.01'
  return `$${value.toFixed(2)}`
}

/** "Resets 2:29pm" for today, with the date once it is not today. */
export function resetLabel(iso: string | null, now = new Date()): string {
  if (!iso) return ''
  const at = new Date(iso)
  const time = at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  const sameDay = at.toDateString() === now.toDateString()
  if (sameDay) return `Resets ${time}`
  return `Resets ${at.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}, ${time}`
}

/** How long until it clears, for the cases where a clock time is not enough. */
export function untilLabel(iso: string | null, now = new Date()): string {
  if (!iso) return ''
  const ms = new Date(iso).getTime() - now.getTime()
  if (ms <= 0) return 'now'
  const minutes = Math.round(ms / 60000)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ${minutes % 60}m`
  return `${Math.floor(hours / 24)}d ${hours % 24}h`
}

function shortModel(model: string): string {
  return model.replace(/^claude-/, '').replace(/-\d{8}$/, '')
}

/**
 * A limit window as a bar, or as an honest absence of one.
 *
 * The bar turns amber past three quarters and red past ninety percent, because
 * the only reason to look at this is to find out whether you are about to run
 * out.
 */
function WindowBar({
  window: w,
  metered,
  onSetLimit,
}: {
  window: api.UsageWindow
  metered: boolean
  onSetLimit: () => void
}) {
  const percent = w.percent
  const tone = percent === null ? '' : percent >= 90 ? 'hot' : percent >= 75 ? 'warm' : ''

  return (
    <div className="window">
      <div className="window-head">
        <span className="window-label">
          {w.label} <span className="window-span">({w.hours}h)</span>
        </span>
        <span className="window-used">
          {percent !== null ? (
            <strong>{percent}% used</strong>
          ) : (
            <strong>{compact(w.tokens)} tokens</strong>
          )}
          {metered && w.cost !== null && <span className="window-cost">{money(w.cost)}</span>}
        </span>
      </div>

      {percent !== null ? (
        <div className={`bar ${tone}`}>
          <span style={{ width: `${Math.max(1, percent)}%` }} />
        </div>
      ) : (
        // No allowance given, so there is nothing to fill. Saying so and
        // offering to fix it beats drawing a bar that means nothing. Only
        // worth offering while a window is actually open — there is nothing to
        // be a percentage of otherwise.
        w.started_at && (
          <button className="bar-empty" onClick={onSetLimit}>
            Set your plan limit to see how much is left
          </button>
        )
      )}

      <div className="window-foot">
        {w.started_at ? (
          <>
            <span>{resetLabel(w.resets_at)}</span>
            <span className="muted">in {untilLabel(w.resets_at)}</span>
            {percent !== null && (
              <span className="muted">
                {compact(w.tokens)} of {compact(w.limit_tokens ?? 0)}
              </span>
            )}
          </>
        ) : (
          <span className="muted">
            Nothing running. The window opens with your next message.
          </span>
        )}
      </div>
    </div>
  )
}

interface Props {
  onClose: () => void
}

export function Usage({ onClose }: Props) {
  const [hours, setHours] = useState(24 * 7)
  const usage = useResource(() => api.usage(hours), [hours], { pollMs: 30000 })
  const [editing, setEditing] = useState<'limits' | 'prices' | null>(null)

  const data = usage.data
  const metered = data?.billing === 'api_key'
  const unpriced = (data?.models ?? []).filter((m) => !m.priced)

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal modal--wide" onClick={(event) => event.stopPropagation()}>
        <div className="row-between">
          <h2>Usage</h2>
          <button className="ghost" onClick={onClose}>
            Close
          </button>
        </div>

        {usage.error && <div className="error">{usage.error}</div>}
        {!data ? (
          <p className="hint">Reading transcripts…</p>
        ) : (
          <>
            {/* Limits first. On a subscription this is the whole question, and
                on an API key the bill is right beside it. */}
            <div className="windows">
              <WindowBar
                window={data.session_window}
                metered={metered}
                onSetLimit={() => setEditing('limits')}
              />
              <WindowBar
                window={data.week_window}
                metered={metered}
                onSetLimit={() => setEditing('limits')}
              />
            </div>

            {metered && (
              <div className="spend-line">
                <strong>{money(data.cost)}</strong>
                <span className="muted">spent in the last {spanLabel(hours)}</span>
                {unpriced.length > 0 && (
                  <button className="link" onClick={() => setEditing('prices')}>
                    {unpriced.length} model{unpriced.length > 1 ? 's' : ''} unpriced
                  </button>
                )}
              </div>
            )}

            <div className="row-between usage-controls">
              <div className="spans">
                {SPANS.map((span) => (
                  <button
                    key={span.hours}
                    className={hours === span.hours ? 'active' : ''}
                    onClick={() => setHours(span.hours)}
                  >
                    {span.label}
                  </button>
                ))}
              </div>
              <span className="muted small">{compact(data.tokens)} tokens</span>
            </div>

            <Sparkline series={data.series} />

            <h3>By model</h3>
            <table className="usage-table">
              <thead>
                <tr>
                  <th>Model</th>
                  <th className="num">Tokens</th>
                  <th className="num">In</th>
                  <th className="num">Out</th>
                  <th className="num">Cache read</th>
                  <th className="num">Cost</th>
                </tr>
              </thead>
              <tbody>
                {data.models.map((slice) => (
                  <tr key={slice.model}>
                    <td title={slice.model}>{shortModel(slice.model)}</td>
                    <td className="num">{compact(slice.tokens)}</td>
                    <td className="num">{compact(slice.input_tokens)}</td>
                    <td className="num">{compact(slice.output_tokens)}</td>
                    <td className="num">{compact(slice.cache_read_tokens)}</td>
                    <td className="num">
                      {slice.priced ? (
                        money(slice.cost)
                      ) : (
                        // The reason this is blank is actionable, so it is
                        // offered as the action.
                        <button className="link" onClick={() => setEditing('prices')}>
                          set rate
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {data.models.length === 0 && (
                  <tr>
                    <td colSpan={6} className="muted">
                      Nothing recorded in this period.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>

            {data.projects.length > 0 && (
              <>
                <h3>By project</h3>
                <div className="usage-bars">
                  {data.projects.map((project) => (
                    <div key={project.project_name} className="usage-bar">
                      <span className="usage-bar-name">{project.project_name}</span>
                      <span className="usage-bar-track">
                        <span
                          className="usage-bar-fill"
                          style={{
                            width: `${share(project.tokens, data.projects[0].tokens)}%`,
                          }}
                        />
                      </span>
                      <span className="usage-bar-value">
                        {metered && project.cost !== null
                          ? money(project.cost)
                          : compact(project.tokens)}
                      </span>
                    </div>
                  ))}
                </div>
              </>
            )}

            <div className="row-between usage-foot">
              <span>
                <button className="ghost small" onClick={() => setEditing('limits')}>
                  Plan limits
                </button>
                <button className="ghost small" onClick={() => setEditing('prices')}>
                  Model rates
                </button>
              </span>
              <span className="muted small">
                Counted from each session's own transcript.
              </span>
            </div>
          </>
        )}

        {editing === 'limits' && (
          <Limits
            onClose={() => {
              setEditing(null)
              usage.reload()
            }}
          />
        )}
        {editing === 'prices' && (
          <Prices
            initial={unpriced[0]?.model ?? ''}
            onClose={() => {
              setEditing(null)
              usage.reload()
            }}
          />
        )}
      </div>
    </div>
  )
}

function spanLabel(hours: number): string {
  return SPANS.find((item) => item.hours === hours)?.label ?? `${hours}h`
}

function share(value: number, top: number): number {
  if (top <= 0) return 0
  return Math.max(2, Math.round((value / top) * 100))
}

/**
 * Consumption over time, drawn as bars.
 *
 * Deliberately not a charting library: it is one series of non-negative
 * integers, and the dependency would be larger than the whole app.
 */
function Sparkline({ series }: { series: { at: string; tokens: number }[] }) {
  if (series.length < 2) return null
  const peak = Math.max(...series.map((point) => point.tokens), 1)
  return (
    <div className="sparkline" aria-hidden="true">
      {series.map((point) => (
        <span
          key={point.at}
          className="sparkline-bar"
          style={{ height: `${Math.max(2, (point.tokens / peak) * 100)}%` }}
          title={`${new Date(point.at).toLocaleString()} — ${compact(point.tokens)}`}
        />
      ))}
    </div>
  )
}

/**
 * What your plan allows.
 *
 * Entered rather than detected, and the screen says why: the allowance is not
 * published, and Moonphase will not use a session's credentials to go and ask
 * on your behalf. Whatever the harness reports as 100% is the number to put
 * here.
 */
function Limits({ onClose }: { onClose: () => void }) {
  const limits = useResource(() => api.usageLimits(), [])
  const [session, setSession] = useState<string | null>(null)
  const [week, setWeek] = useState<string | null>(null)
  const [alert, setAlert] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const current = limits.data
  const sessionValue = session ?? (current?.session_tokens?.toString() || '')
  const weekValue = week ?? (current?.weekly_tokens?.toString() || '')
  const alertValue = alert ?? (current?.alert_percent?.toString() || '')

  async function save() {
    setBusy(true)
    setError(null)
    try {
      await api.setUsageLimits({
        session_tokens: sessionValue ? Number(sessionValue) : null,
        weekly_tokens: weekValue ? Number(weekValue) : null,
        alert_percent: alertValue ? Number(alertValue) : null,
      })
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal" onClick={(event) => event.stopPropagation()}>
        <div className="row-between">
          <h2>Plan limits</h2>
          <button className="ghost" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="hint">
          Anthropic does not publish an allowance per plan, and Moonphase will not
          use a session's own credentials to go and ask on your behalf. Put in the
          token count your plan allows and the bars become percentages. Leave a
          field empty to go back to showing raw tokens.
        </p>

        <label>
          <span>Tokens per 5-hour window</span>
          <input
            type="number"
            min="1"
            value={sessionValue}
            onChange={(event) => setSession(event.target.value)}
            placeholder="e.g. 8000000"
          />
        </label>
        <label>
          <span>Tokens per week</span>
          <input
            type="number"
            min="1"
            value={weekValue}
            onChange={(event) => setWeek(event.target.value)}
            placeholder="e.g. 120000000"
          />
        </label>

        <label>
          <span>Warn me when a window reaches (%)</span>
          <input
            type="number"
            min="1"
            max="100"
            value={alertValue}
            onChange={(event) => setAlert(event.target.value)}
            placeholder="80"
          />
        </label>
        <p className="hint">
          Pushed to your devices once per window, not once per check — a threshold
          crossed early in a window stays crossed, and you should hear about it once.
          Needs a limit above and notifications turned on in Settings.
        </p>

        {error && <div className="error">{error}</div>}
        <div className="actions">
          <button className="primary" disabled={busy} onClick={() => void save()}>
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

/**
 * What a model costs, when Moonphase does not already know.
 *
 * Only two numbers are asked for. Cache read and cache write rates are fixed
 * multiples of the input rate and are the same for every model, so asking for
 * them would be three more chances to mistype a bill.
 */
function Prices({ initial, onClose }: { initial: string; onClose: () => void }) {
  const prices = useResource(() => api.modelPrices(), [])
  const [model, setModel] = useState(initial)
  const [input, setInput] = useState('')
  const [output, setOutput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function save() {
    setBusy(true)
    setError(null)
    try {
      await api.setModelPrice({
        model: model.trim(),
        input_per_m: Number(input),
        output_per_m: Number(output),
      })
      setModel('')
      setInput('')
      setOutput('')
      prices.reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const valid = Boolean(model.trim() && input && output && Number(input) >= 0 && Number(output) >= 0)

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="card modal" onClick={(event) => event.stopPropagation()}>
        <div className="row-between">
          <h2>Model rates</h2>
          <button className="ghost" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="hint">
          Dollars per million tokens. A rate set here overrides the built-in one and
          applies to every model whose name starts with it, so <code>claude-sonnet-5</code>{' '}
          covers every dated release of it.
        </p>

        <div className="price-form">
          <label>
            <span>Model</span>
            <input
              value={model}
              onChange={(event) => setModel(event.target.value)}
              placeholder="claude-sonnet-5"
            />
          </label>
          <label>
            <span>Input $/M</span>
            <input
              type="number"
              min="0"
              step="0.01"
              value={input}
              onChange={(event) => setInput(event.target.value)}
            />
          </label>
          <label>
            <span>Output $/M</span>
            <input
              type="number"
              min="0"
              step="0.01"
              value={output}
              onChange={(event) => setOutput(event.target.value)}
            />
          </label>
          <button className="primary" disabled={!valid || busy} onClick={() => void save()}>
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>

        {error && <div className="error">{error}</div>}

        <table className="usage-table">
          <thead>
            <tr>
              <th>Model</th>
              <th className="num">Input $/M</th>
              <th className="num">Output $/M</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(prices.data ?? []).map((price) => (
              <tr key={price.model}>
                <td>
                  {price.model}
                  {price.builtin && <span className="tag">built in</span>}
                </td>
                <td className="num">{price.input_per_m.toFixed(2)}</td>
                <td className="num">{price.output_per_m.toFixed(2)}</td>
                <td className="num">
                  {!price.builtin && (
                    <button
                      className="link"
                      onClick={async () => {
                        await api.clearModelPrice(price.model)
                        prices.reload()
                      }}
                    >
                      remove
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/**
 * The home-screen line: how full the window is, at a glance.
 *
 * Small on purpose, and absent when there is nothing to say. The screen it
 * sits on exists to tell you what needs an answer, not to be a dashboard.
 */
export function UsageStrip({ onOpen }: { onOpen: () => void }) {
  const usage = useResource(() => api.usage(24), [], { pollMs: 60000 })
  const data = usage.data
  const w = data?.session_window
  // Nothing has opened a window and nothing has been spent: the honest render
  // is no strip at all, rather than a confident "0".
  if (!data || !w?.started_at) return null

  const metered = data.billing === 'api_key'
  const tone = w.percent === null ? '' : w.percent >= 90 ? 'hot' : w.percent >= 75 ? 'warm' : ''

  return (
    <button className="usage-strip" onClick={onOpen}>
      <span className={`strip-bar ${tone}`}>
        <span style={{ width: `${w.percent ?? 100}%`, opacity: w.percent === null ? 0.25 : 1 }} />
      </span>
      <span className="strip-text">
        {w.percent !== null ? (
          <>
            <strong>{w.percent}%</strong> of this window used
          </>
        ) : metered ? (
          <>
            <strong>{money(data.cost)}</strong> in the last 24 hours
          </>
        ) : (
          <>
            <strong>{compact(w.tokens)}</strong> tokens this window
          </>
        )}
      </span>
      <span className="muted">{resetLabel(w.resets_at)}</span>
      <span className="usage-strip-more">Usage →</span>
    </button>
  )
}
