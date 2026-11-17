/** HTTP transport: fixed official origin, no redirects, no identity fallback. */
import { setTimeout as delay } from 'node:timers/promises'
export type Json = Record<string, any>
export interface ApiDiagnostic { method:string; route:string; failure:string; attempts:number; elapsedMs:number; status?:number; transportCode?:string }
export class ApiFailure extends Error {
  constructor(readonly outcome: 'failed' | 'unknown', readonly code: string,readonly diagnostic?:ApiDiagnostic,readonly retryable=false) {
    super(code+(diagnostic?`: ${diagnostic.method} ${diagnostic.route}; ${diagnostic.failure}; attempts=${diagnostic.attempts}; elapsedMs=${diagnostic.elapsedMs}`+(diagnostic.transportCode?`; transport=${diagnostic.transportCode}`:'')+(outcome==='unknown'?'; verify remote state before any new write':'') : ''))
  }
}
export function origin(value: string): string {
  const url = new URL(value)
  if (url.pathname !== '/' || url.search || url.hash || url.username || url.password ||
    !((url.protocol === 'https:' && ['open.feishu.cn', 'open.larksuite.com'].includes(url.hostname)) ||
      (url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)))) throw new Error('unsupported_feishu_origin')
  return url.origin
}
export class FeishuHttp {
  private readonly readAttempts:number
  private readonly retryDelayMs:number
  constructor(private readonly base: string, private readonly token: () => Promise<string>, private readonly timeoutMs = 10000,options:{readAttempts?:number;retryDelayMs?:number}={}) {
    origin(base);this.readAttempts=options.readAttempts??2;this.retryDelayMs=options.retryDelayMs??250
    if(!Number.isSafeInteger(timeoutMs)||timeoutMs<1||!Number.isSafeInteger(this.readAttempts)||this.readAttempts<1||this.readAttempts>3||!Number.isSafeInteger(this.retryDelayMs)||this.retryDelayMs<0||this.retryDelayMs>5000)throw Error('invalid_feishu_http_policy')
  }
  async request(method: string, route: string, body?: unknown, signal?: AbortSignal, write = method !== 'GET'): Promise<Json> {
    if (!route.startsWith('/open-apis/') || route.includes('..') || route.includes('://')) throw new Error('invalid_api_route')
    const started=Date.now(),safeRoute=route.split('?')[0].replace(/\/(apps|tables|records|calendars|events|tasks|documents|files|spaces)\/[^/]+/g,'/$1/:id')
    const diagnostic=(failure:string,attempts:number,extra:Partial<ApiDiagnostic>={}):ApiDiagnostic=>({method,route:safeRoute,failure,attempts,elapsedMs:Date.now()-started,...extra})
    const cancelled=(attempts:number)=>new ApiFailure('failed',write?'write_cancelled':'read_cancelled',diagnostic('cancelled',attempts))
    if(signal?.aborted)throw cancelled(0)
    const token = await this.token()
    if (!token) throw new ApiFailure('failed', 'credential_missing')
    for(let attempt=1;attempt<=(write?1:this.readAttempts);attempt++){
      if(signal?.aborted)throw cancelled(attempt-1)
      const timeout=AbortSignal.timeout(this.timeoutMs)
      let decoding=false
      try {
        const response = await fetch(this.base + route, { method, redirect: 'error',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout })
        if(!response.ok){
          await response.body?.cancel().catch(()=>{})
          throw new ApiFailure(write&&response.status>=500?'unknown':'failed',`http_${response.status}`,diagnostic('http',attempt,{status:response.status}),response.status===429||response.status>=500)
        }
        decoding=true
        const value = await response.json() as Json
        if(!value||typeof value!=='object'||Array.isArray(value)||!Number.isSafeInteger(value.code))throw new SyntaxError('invalid_response')
        if (value.code !== 0) throw new ApiFailure('failed', `feishu_${value.code}`,diagnostic('api',attempt))
        if (value.data === undefined || value.data === null) return {}
        return value.data
      } catch (error) {
        const rawCode=(error as {cause?:{code?:unknown}})?.cause?.code
        const transportCode=typeof rawCode==='string'&&/^(?:E[A-Z0-9_]+|UND_ERR_[A-Z0-9_]+|CERT_[A-Z0-9_]+|DEPTH_ZERO_SELF_SIGNED_CERT|UNABLE_TO_[A-Z0-9_]+)$/.test(rawCode)?rawCode:undefined
        const failure=signal?.aborted?'cancelled':timeout.aborted||transportCode&&/TIMEOUT|ETIMEDOUT/.test(transportCode)?'timeout':decoding?'invalid_response':transportCode==='ENOTFOUND'||transportCode==='EAI_AGAIN'?'dns':transportCode==='EACCES'||transportCode==='EPERM'?'permission':transportCode&&/CERT|TLS|SSL|UNABLE_TO_VERIFY|DEPTH_ZERO/.test(transportCode)?'tls':'connection'
        const issue=error instanceof ApiFailure?error:new ApiFailure(write?'unknown':'failed',write?'write_receipt_unknown':'read_'+failure,diagnostic(failure,attempt,{...(transportCode?{transportCode}:{})}),!['cancelled','invalid_response','tls','permission'].includes(failure))
        if(write||signal?.aborted||!issue.retryable||attempt>=this.readAttempts)throw issue
        try{await delay(this.retryDelayMs*attempt,undefined,{signal})}catch{throw cancelled(attempt)}
      }
    }
    throw Error('unreachable_feishu_attempt')
  }
}
