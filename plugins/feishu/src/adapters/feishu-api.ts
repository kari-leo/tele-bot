import type { DeliveryPort, DeliveryResult, Outbound } from '../domain/types.js'
import { object, string } from '../domain/events.js'
import { readFile, stat } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import path from 'node:path'

/** Only official endpoints or an explicit loopback test server may receive application credentials. */
export function apiOrigin(value: string): string {
  const url = new URL(value)
  const loopback = ['127.0.0.1', '[::1]', 'localhost'].includes(url.hostname)
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/' ||
      !((url.protocol === 'https:' && ['open.feishu.cn', 'open.larksuite.com'].includes(url.hostname)) || (url.protocol === 'http:' && loopback))) {
    throw new Error('unsupported_feishu_api_origin')
  }
  return url.origin
}

/** Feishu writes have explicit confirmed, retryable-rejection, failed and uncertain outcomes. */
export class FeishuApi implements DeliveryPort {
  private cached: { value: string; expires: number } | undefined
  private pendingToken: Promise<string> | undefined
  constructor(private readonly options: { appId: string; secret(): Promise<string>; origin: string; timeoutMs: number }) {}
  private async token(): Promise<string> {
    if (this.cached && this.cached.expires > Date.now()) return this.cached.value
    if (!this.pendingToken) {
      this.pendingToken = (async () => {
        const response = await fetch(`${this.options.origin}/open-apis/auth/v3/tenant_access_token/internal`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, redirect: 'error', signal: AbortSignal.timeout(this.options.timeoutMs),
          body: JSON.stringify({ app_id: this.options.appId, app_secret: await this.options.secret() }),
        })
        if (!response.ok) throw new Error('feishu_auth_unavailable')
        const body = object(await response.json())
        const value = string(body.tenant_access_token)
        if (body.code !== 0 || !value || typeof body.expire !== 'number' || body.expire <= 0) throw new Error('feishu_auth_rejected')
        this.cached = { value, expires: Date.now() + Math.max(1, body.expire - 60) * 1000 }
        return value
      })().finally(() => { this.pendingToken = undefined })
    }
    return this.pendingToken
  }
  async tenantGrant() { const token = await this.token(); return { token, expiresAt: this.cached!.expires } }
  async send(message: Outbound,onUploaded?:(key:string)=>void): Promise<DeliveryResult> {
    let token: string
    try { token = await this.token() }
    catch { return { status: 'retry', code: 'feishu_auth_unavailable' } }
    const suffix = message.editMessageId ? `/open-apis/im/v1/messages/${encodeURIComponent(message.editMessageId)}` : message.replyTo ? `/open-apis/im/v1/messages/${encodeURIComponent(message.replyTo)}/reply` : '/open-apis/im/v1/messages?receive_id_type=chat_id'
    try {
      let content = message.content
      if (message.msgType === 'file') {
        const file = JSON.parse(message.content)
        if ((await stat(file.path)).size > 30 * 1024 * 1024) return { status: 'failed', code: 'file_too_large' }
        const bytes = await readFile(file.path)
        if (createHash('sha256').update(bytes).digest('hex') !== file.sha256) return { status: 'failed', code: 'artifact_changed' }
        let key=message.uploadedFileKey
        if(!key){
        const form = new FormData(); form.set('file_type', 'stream'); form.set('file_name', path.basename(file.path)); form.set('file', new Blob([bytes]), path.basename(file.path))
        const upload = await fetch(this.options.origin + '/open-apis/im/v1/files', { method: 'POST', redirect: 'error', headers: { Authorization: `Bearer ${token}` }, body: form, signal: AbortSignal.timeout(this.options.timeoutMs) })
        if (upload.status === 429) return { status: 'retry', code: 'feishu_upload_rate_limited' }
        if (upload.status >= 500) return { status: 'unknown', code: 'feishu_upload_unknown' }
        if (!upload.ok) return { status: 'failed', code: 'feishu_upload_rejected' }
        const receipt = object(await upload.json())
        if (receipt.code !== 0) return { status: 'failed', code: 'feishu_upload_rejected' }
        key = string(object(receipt.data).file_key)
        if (!key) return { status: 'unknown', code: 'feishu_upload_receipt_missing' }
        onUploaded?.(key)
        }
        content = JSON.stringify({ file_key: key })
      }
      const response = await fetch(this.options.origin + suffix, {
        method: message.editMessageId?'PUT':'POST', redirect: 'error', signal: AbortSignal.timeout(this.options.timeoutMs),
        headers: { 'Content-Type': 'application/json; charset=utf-8', Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          ...(!message.editMessageId&&!message.replyTo ? { receive_id: message.chatId } : {}),
          ...(!message.editMessageId&&message.replyTo && message.threadId ? { reply_in_thread: true } : {}),
          msg_type: message.msgType, content,...(!message.editMessageId?{uuid:message.id}:{}),
        }),
      })
      if (response.status === 429) return { status: 'retry', code: 'feishu_rate_limited' }
      if (response.status >= 500) return { status: 'unknown', code: `feishu_http_${response.status}` }
      if (!response.ok) return { status: 'failed', code: `feishu_http_${response.status}` }
      const result = object(await response.json())
      if (result.code === 99991663 || result.code === 99991668) {
        this.cached = undefined
        return { status: 'retry', code: 'feishu_token_expired' }
      }
      if (result.code !== 0) return { status: 'failed', code: `feishu_code_${typeof result.code === 'number' ? result.code : 'invalid'}` }
      const messageId = message.editMessageId??string(object(result.data).message_id)
      return messageId ? { status: 'sent', messageId } : { status: 'unknown', code: 'feishu_missing_receipt' }
    } catch { return { status: 'unknown', code: 'feishu_send_outcome_unknown' } }
  }
  async download(messageId: string, key: string, type: string) {
    if (!['file', 'image'].includes(type)) throw new Error('unsupported_attachment_type')
    const response = await fetch(`${this.options.origin}/open-apis/im/v1/messages/${encodeURIComponent(messageId)}/resources/${encodeURIComponent(key)}?type=${type}`, {
      headers: { Authorization: `Bearer ${await this.token()}` }, redirect: 'error', signal: AbortSignal.timeout(this.options.timeoutMs) })
    if (!response.ok || !response.body) throw new Error('attachment_download_failed')
    const chunks = []; let size = 0
    for await (const chunk of response.body) { size += chunk.length; if (size > (type === 'image' ? 20 : 30) * 1024 * 1024) { await response.body.cancel().catch(() => {}); throw new Error('attachment_size_limit') } chunks.push(chunk) }
    return { bytes: Buffer.concat(chunks), mediaType: response.headers.get('content-type')?.split(';')[0] ?? 'application/octet-stream' }
  }
}
