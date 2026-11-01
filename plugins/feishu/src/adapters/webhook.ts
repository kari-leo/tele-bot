import { createDecipheriv, createHash, timingSafeEqual } from 'node:crypto'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { object, parseEvent, string } from '../domain/events.js'
import type { InputEvent } from '../domain/types.js'

export interface WebhookSecrets { verificationToken: string; encryptKey: string }
const equal = (a: string, b: string): boolean => {
  const left = Buffer.from(a), right = Buffer.from(b)
  return left.length === right.length && timingSafeEqual(left, right)
}

/** Feishu CBC decoding and request-source checks over the original wire bytes. */
export function decodeWebhook(raw: Buffer, headers: IncomingMessage['headers'], secrets: WebhookSecrets, now = Date.now()): Record<string, unknown> {
  const outer = object(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw)))
  let payload = outer
  const encrypted = string(outer.encrypt)
  if (encrypted) {
    if (!secrets.encryptKey) throw new Error('authentication_failed')
    try {
      const bytes = Buffer.from(encrypted, 'base64')
      if (bytes.toString('base64') !== encrypted) throw new Error('invalid_payload')
      const key = createHash('sha256').update(secrets.encryptKey).digest()
      const cipher = createDecipheriv('aes-256-cbc', key, key.subarray(0, 16))
      payload = object(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat([cipher.update(bytes), cipher.final()]))))
    } catch { throw new Error('authentication_failed') }
  }
  const header = payload.header ? object(payload.header) : undefined
  if (secrets.verificationToken && !equal(string(payload.token ?? header?.token), secrets.verificationToken)) throw new Error('authentication_failed')
  if (secrets.encryptKey && !(payload.type === 'url_verification' && encrypted)) {
    const timestamp = string(headers['x-lark-request-timestamp'])
    const nonce = string(headers['x-lark-request-nonce'])
    const signature = string(headers['x-lark-signature'])
    if (!/^\d{10}$/.test(timestamp) || Math.abs(now - Number(timestamp) * 1000) > 600000 || !nonce || nonce.length > 256 || !/^[a-f0-9]{64}$/.test(signature)) throw new Error('authentication_failed')
    const expected = createHash('sha256').update(timestamp + nonce + secrets.encryptKey).update(raw).digest('hex')
    if (!equal(signature, expected)) throw new Error('authentication_failed')
  }
  if (!secrets.verificationToken && !secrets.encryptKey) throw new Error('webhook_authentication_not_configured')
  return payload
}

/** Register with the official HTTP carrier; persistence completes before the 200 acknowledgment. */
export function webhookHandler(options: {
  appId: string; secrets: WebhookSecrets; maxBodyBytes: number;
  accept(event: InputEvent): { accepted: boolean; duplicate?: boolean; rejected?: boolean; full?: boolean }
}) {
  return async (request: IncomingMessage, response: ServerResponse): Promise<void> => {
    const reply = (status: number, value: object) => {
      response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' })
      response.end(JSON.stringify(value))
    }
    if (request.method !== 'POST') { response.setHeader('Allow', 'POST'); reply(405, { error: 'method_not_allowed' }); return }
    try {
      const chunks: Buffer[] = []
      let size = 0
      for await (const value of request) {
        const chunk = Buffer.from(value); size += chunk.length
        if (size > options.maxBodyBytes) { reply(413, { error: 'body_too_large' }); return }
        chunks.push(chunk)
      }
      const payload = decodeWebhook(Buffer.concat(chunks), request.headers, options.secrets)
      if (payload.type === 'url_verification') {
        const challenge = string(payload.challenge)
        if (!challenge || challenge.length > 4096) { reply(400, { error: 'invalid_challenge' }); return }
        reply(200, { challenge }); return
      }
      const event = parseEvent(payload, options.appId)
      if (!event) { reply(200, { ignored: true }); return }
      let result
      try { result = options.accept(event) } catch { reply(503, { error: 'persistence_unavailable' }); return }
      if (result.rejected) { reply(403, { error: 'actor_or_action_rejected' }); return }
      if (result.full) { reply(503, { error: 'queue_full' }); return }
      reply(200, event.actionToken
        ? { toast: { type: 'info', content: result.duplicate ? '此操作已处理' : '已收到，正在更新状态' } }
        : { accepted: true, duplicate: Boolean(result.duplicate) })
    } catch (error) {
      const code = error instanceof Error ? error.message : ''
      if (code === 'authentication_failed' || code === 'webhook_authentication_not_configured' || code === 'wrong_app') reply(403, { error: 'source_rejected' })
      else if (code === 'channel_stopping' || code.includes('SQLITE') || code.includes('database')) reply(503, { error: 'persistence_unavailable' })
      else reply(400, { error: 'invalid_event' })
    }
  }
}
