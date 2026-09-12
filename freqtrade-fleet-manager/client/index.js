/**
 * freqtrade-fleet-manager — browser half: the Settings → "Freqtrade Fleet"
 * section. It fetches `/freqtrade/api/fleet?ping=1` (served by the host entry
 * on the host web server) and renders the instance list with live `/ping`
 * health. The host entry owns the registry and all 54 `ft_*` tools; this half
 * is read-only presentation.
 *
 * @module freqtrade-fleet-manager/client
 */
import React from 'react'

/** Hard dependency: the slots service (settings.section is declared by the settings UI). */
export const inject = ['slots']

const CSS = [
  '.ftdash { font-size: 13px; line-height: 1.5; }',
  '.ftdash-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }',
  '.ftdash-head b { font-weight: 600; }',
  '.ftdash-btn { cursor: pointer; padding: 4px 10px; border-radius: 6px; border: 1px solid rgba(128,128,128,0.4); background: transparent; font-size: 12px; }',
  '.ftdash-table { width: 100%; border-collapse: collapse; margin-top: 4px; }',
  '.ftdash-table th, .ftdash-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid rgba(128,128,128,0.25); }',
  '.ftdash-empty { padding: 12px 0; opacity: 0.85; }',
  '.ftdash-err { padding: 12px 0; color: #c0392b; }',
  '.ftdash-foot { margin-top: 10px; opacity: 0.7; font-size: 12px; }',
].join('\n')

function FleetDashboard() {
  const [state, setState] = React.useState({ loading: true, data: null, error: null })

  function refresh() {
    setState((prev) => ({ loading: true, data: prev.data, error: null }))
    fetch('/freqtrade/api/fleet?ping=1')
      .then((r) => {
        if (!r.ok) throw new Error('HTTP ' + r.status)
        return r.json()
      })
      .then((data) => setState({ loading: false, data, error: null }))
      .catch((e) => setState({ loading: false, data: null, error: String((e && e.message) || e) }))
  }

  React.useEffect(() => { refresh() }, [])

  const data = state.data
  const rows = (data && data.instances) || []
  const health = (data && data.health) || []
  const hmap = {}
  for (const h of health) hmap[h.name] = h

  const th = (label) => React.createElement('th', null, label)
  const cells = rows.map((r) => {
    const h = hmap[r.name]
    let status = '\u2014'
    if (h) status = h.up ? 'up' : 'down'
    if (h && h.up && h.latency_ms !== null && h.latency_ms !== undefined) status += ' (' + h.latency_ms + 'ms)'
    return React.createElement('tr', { key: r.name },
      React.createElement('td', null, r.name),
      React.createElement('td', null, r.host === 'ssh' ? 'ssh' : 'local'),
      React.createElement('td', null, r.strategy || ''),
      React.createElement('td', null, r.dry_run === false ? 'live' : 'dry'),
      React.createElement('td', null, status)
    )
  })

  return React.createElement('div', { className: 'ftdash' },
    React.createElement('div', { className: 'ftdash-head' },
      React.createElement('b', null, 'Freqtrade Fleet'),
      React.createElement('button', { className: 'ftdash-btn', onClick: refresh }, state.loading ? 'Refreshing\u2026' : 'Refresh')
    ),
    state.error ? React.createElement('div', { className: 'ftdash-err' }, state.error) : null,
    state.loading && !data ? React.createElement('div', null, 'Loading fleet\u2026') : null,
    data && data.count === 0 ? React.createElement('div', { className: 'ftdash-empty' }, 'No instances registered. Ask the agent to run ft_instances_add.') : null,
    rows.length ? React.createElement('table', { className: 'ftdash-table' },
      React.createElement('thead', null, React.createElement('tr', null, th('Instance'), th('Host'), th('Strategy'), th('Mode'), th('Status'))),
      React.createElement('tbody', null, cells)
    ) : null,
    data ? React.createElement('div', { className: 'ftdash-foot' }, data.count + ' instance(s) \u00b7 registry: ' + (data.registryFile || 'in-memory')) : null
  )
}

/**
 * Client plugin body: inject the stylesheet, then register the settings
 * section once the settings UI declares the slot.
 * @param ctx - client root context.
 */
export function apply(ctx) {
  ctx.effect(() => {
    const tag = document.createElement('style')
    tag.setAttribute('data-plugin-css', 'freqtrade-fleet-manager')
    tag.textContent = CSS
    document.head.append(tag)
    return () => tag.remove()
  }, 'freqtrade-fleet-manager: dashboard styles')

  ctx.slots.inject('settings.section', () => ctx.slots.register(
    { name: 'settings.section', id: 'freqtrade-fleet', order: 50, label: 'Freqtrade Fleet' },
    FleetDashboard
  ))
}