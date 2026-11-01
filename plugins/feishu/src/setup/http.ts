import type { IncomingMessage, ServerResponse } from 'node:http'
import { randomBytes } from 'node:crypto'
import { setupPage } from './page.js'
export const setupPath = '/telebot/feishu/setup'
export interface SetupPort { authorized(token: string): boolean; describe(): Promise<unknown>; save(body: any): Promise<unknown> }
/** Local capability access, strict authority/origin, no forwarded/proxied setup requests. */
export function setupHandler(origin: string, port: SetupPort) {
  const authority = new URL(origin).host
  return async (request: IncomingMessage, response: ServerResponse) => {
    response.setHeader('Cache-Control', 'no-store'); response.setHeader('X-Content-Type-Options', 'nosniff'); response.setHeader('Referrer-Policy', 'no-referrer')
    const json = (status: number, value: unknown) => { response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' }).end(JSON.stringify(value)) }
    const remote = request.socket.remoteAddress ?? ''
    if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(remote) || request.headers.host !== authority || ['forwarded','x-forwarded-for','x-forwarded-host','x-real-ip','cf-connecting-ip'].some(key => request.headers[key]) || (request.headers.origin && request.headers.origin !== origin)) return json(403, { code: 'local_setup_only' })
    const pathname = new URL(request.url ?? '/', origin).pathname
    if (pathname === setupPath && request.method === 'GET') {
      const nonce = randomBytes(18).toString('base64')
      response.setHeader('Content-Security-Policy', `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`)
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }).end(setupPage(nonce)); return
    }
    if (pathname !== setupPath + '/api') return json(404, { code: 'not_found' })
    if (!port.authorized((request.headers.authorization ?? '').replace(/^Bearer /, ''))) return json(403, { code: 'setup_access_denied' })
    try {
      if (request.method === 'GET') return json(200, await port.describe())
      if (request.method !== 'POST') return json(405, { code: 'method_not_allowed' })
      if (request.headers.origin !== origin || request.headers['content-type']?.split(';')[0] !== 'application/json') return json(403, { code: 'same_origin_json_required' })
      const chunks: Buffer[] = []; let length = 0
      for await (const part of request) { length += part.length; if (length > 262144) return json(413, { code: 'configuration_too_large' }); chunks.push(part) }
      return json(200, await port.save(JSON.parse(Buffer.concat(chunks).toString('utf8'))))
    } catch (error) {
      const code = error instanceof Error && /^[a-z_]{1,100}$/.test(error.message) ? error.message : 'configuration_request_rejected'
      return json(code === 'configuration_busy' || code === 'configuration_revision_conflict' ? 409 : 400, { code })
    }
  }
}
