// dsh-pin-gate — PIN login for the dsh-ffm web UI.
//
// Serves a minimal PIN gate at /pin. /pin is a NAMED webServer route, so it
// answers before the SPA fallback that enforces the dsh token/401 index
// gate. A correct PIN redirects the browser to /?token=<launch-token>,
// minting the ordinary dsh browser-auth cookie; the app then loads normally.
//
// The PIN is read from the container env FFM_PIN (injected by the deploy
// workflow from a repo secret). Requests are rate-limited per client address
// (fixed window). The underlying launch-token auth is left intact — this gate
// is a convenience entrance, not a removal of the token.

const PIN =
  typeof process !== 'undefined' && process.env && process.env.FFM_PIN
    ? String(process.env.FFM_PIN).trim()
    : ''

const MAX_ATTEMPTS = 5
const WINDOW_MS = 60 * 1000
const attempts = new Map()

function page(message) {
  const err = message ? '<div class="err">' + message + '</div>' : ''
  return [
    '<!doctype html>',
    '<html lang="en"><head><meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    '<title>dsh-ffm access</title>',
    '<style>',
    'body{font-family:system-ui,-apple-system,sans-serif;background:#0f0f0f;color:#eee;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}',
    'form{background:#1b1b1b;padding:2rem 2.4rem;border-radius:14px;text-align:center;min-width:280px;box-shadow:0 8px 40px rgba(0,0,0,.5)}',
    '.t{font-weight:600;font-size:1.1rem}',
    'input{font-size:1.5rem;letter-spacing:.5em;text-align:center;width:6.5em;padding:.45rem;margin:1rem 0;background:#000;color:#eee;border:1px solid #444;border-radius:8px}',
    'button{font-size:.95rem;padding:.55rem 1.4rem;border-radius:8px;border:0;background:#3b82f6;color:#fff;cursor:pointer}',
    'button:hover{background:#2563eb}',
    '.err{color:#f87171;font-size:.85rem;margin-top:.5rem}',
    '</style></head><body>',
    '<form method="post" action="/pin">',
    '<div class="t">dsh-ffm</div>',
    '<div><input name="pin" type="password" inputmode="numeric" maxlength="4" autocomplete="off" autofocus></div>',
    '<div><button type="submit">Unlock</button></div>',
    err,
    '</form></body></html>'
  ].join('\n')
}

function readBody(req) {
  return new Promise((resolve) => {
    const parts = []
    let size = 0
    req.on('data', (c) => {
      const s = c.toString('utf8')
      size += s.length
      if (size <= 64 * 1024) parts.push(s)
    })
    req.on('end', () => resolve(parts.join('')))
    req.on('error', () => resolve(''))
  })
}

function formPin(body) {
  for (const pair of body.split('&')) {
    const i = pair.indexOf('=')
    if (i < 0) continue
    const k = decodeURIComponent(pair.slice(0, i).replace(/\+/g, ' '))
    if (k === 'pin') return decodeURIComponent(pair.slice(i + 1).replace(/\+/g, ' ')).trim()
  }
  return ''
}

export default {
  name: 'dsh-pin-gate',
  apply(ctx) {
    if (!PIN) return

    const webServer = ctx.get('webServer')
    const connection = ctx.get('connection')
    if (webServer === undefined || connection === undefined) return

    const handler = async (req, res) => {
      const addr = (req.socket && req.socket.remoteAddress) || 'unknown'
      const now = Date.now()
      let rec = attempts.get(addr)
      if (rec === undefined || now - rec.since > WINDOW_MS) {
        rec = { count: 0, since: now }
      }
      attempts.set(addr, rec)

      if (req.method === 'GET') {
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
        res.end(page(''))
        return
      }

      if (req.method !== 'POST') {
        res.writeHead(405, { 'content-type': 'text/plain; charset=utf-8' })
        res.end('method not allowed\n')
        return
      }

      if (rec.count >= MAX_ATTEMPTS) {
        res.writeHead(429, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
        res.end(page('Too many attempts — wait a minute and retry'))
        return
      }
      rec.count += 1

      const body = await readBody(req)
      if (formPin(body) !== PIN) {
        res.writeHead(401, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
        res.end(page('Wrong PIN'))
        return
      }

      // success: hand the browser the launch-token URL so the ordinary
      // browser-auth cookie is minted on the public authority.
      const token = new URL(connection.authenticatedUrl('http://127.0.0.1:3081/')).searchParams.get('token')
      if (!token) {
        res.writeHead(500, { 'content-type': 'text/plain; charset=utf-8' })
        res.end('no launch token available\n')
        return
      }
      res.writeHead(303, { location: '/?token=' + token, 'cache-control': 'no-store' })
      res.end()
    }

    ctx.effect(() => webServer.register({ kind: 'prefix', path: '/pin', handler }))
  }
}