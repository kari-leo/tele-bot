/** HTTP transport: fixed official origin, no redirects, no identity fallback. */
export type Json = Record<string, any>
export class ApiFailure extends Error {
  constructor(readonly outcome: 'failed' | 'unknown', readonly code: string) { super(code) }
}
export function origin(value: string): string {
  const url = new URL(value)
  if (url.pathname !== '/' || url.search || url.hash || url.username || url.password ||
    !((url.protocol === 'https:' && ['open.feishu.cn', 'open.larksuite.com'].includes(url.hostname)) ||
      (url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)))) throw new Error('unsupported_feishu_origin')
  return url.origin
}
export class FeishuHttp {
  constructor(private readonly base: string, private readonly token: () => Promise<string>, private readonly timeoutMs = 10000) { origin(base) }
  async request(method: string, route: string, body?: unknown, signal?: AbortSignal, write = method !== 'GET'): Promise<Json> {
    if (!route.startsWith('/open-apis/') || route.includes('..') || route.includes('://')) throw new Error('invalid_api_route')
    const token = await this.token()
    if (!token) throw new ApiFailure('failed', 'credential_missing')
    try {
      const response = await fetch(this.base + route, { method, redirect: 'error',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(this.timeoutMs)]) : AbortSignal.timeout(this.timeoutMs) })
      if (response.status >= 500) throw new ApiFailure(write ? 'unknown' : 'failed', `http_${response.status}`)
      if (!response.ok) throw new ApiFailure('failed', `http_${response.status}`)
      const value = await response.json() as Json
      if (value.code !== 0) throw new ApiFailure('failed', `feishu_${value.code ?? 'invalid'}`)
      if (value.data === undefined || value.data === null) return {}
      return value.data
    } catch (error) {
      if (error instanceof ApiFailure) throw error
      throw new ApiFailure(write ? 'unknown' : 'failed', write ? 'write_receipt_unknown' : 'read_failed')
    }
  }
}
