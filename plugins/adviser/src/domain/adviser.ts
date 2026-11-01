import { createHash } from 'node:crypto'

export interface AdviserInput { question: string; context: string; excerpts: readonly string[] }
export interface AdviserPort { ask(input: AdviserInput, signal: AbortSignal): Promise<string> }
export type AdviserResult = { status: 'completed'; text: string; cached: boolean } | { status: 'unavailable'; text: string }

/** Pattern filtering is a baseline; explicitly configured secrets are also removed before hashing. */
export function redact(text: string, secrets: readonly string[]): string {
  let result = text.replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----/g, '[REDACTED KEY]')
    .replace(/\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{20,})\b/g, '[REDACTED]')
    .replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+=*/gi, 'Bearer [REDACTED]')
    .replace(/\b(api[_-]?key|token|password|secret)\s*[:=]\s*(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)/gi, '$1=[REDACTED]')
  for (const secret of [...secrets].filter(Boolean).sort((a, b) => b.length - a.length)) result = result.split(secret).join('[REDACTED]')
  return result
}

/** Bounded per-session LRU, no recursive tool dispatch, one in-flight call per session. */
export class Adviser {
  private readonly sessions = new Map<string, Map<string, string>>()
  private readonly active = new Set<string>()
  constructor(private readonly port: AdviserPort, private readonly secrets: readonly string[] = [],
    private readonly timeoutMs = 180000, private readonly capacity = 16, private readonly sessionCapacity = 64) {
    if (![timeoutMs, capacity, sessionCapacity].every(value => Number.isSafeInteger(value) && value > 0)) throw new Error('invalid_adviser_limits')
  }
  clear(session?: string): void { if (session) this.sessions.delete(session); else this.sessions.clear() }
  busy(): boolean { return this.active.size > 0 }
  async ask(session: string, input: AdviserInput, callerSignal?: AbortSignal): Promise<AdviserResult> {
    const unavailable: AdviserResult = { status: 'unavailable', text: '顾问暂不可用，主会话可继续。' }
    if (!session || this.active.has(session) || this.active.size >= this.sessionCapacity || callerSignal?.aborted) return unavailable
    const clean: AdviserInput = { question: redact(input.question, this.secrets), context: redact(input.context, this.secrets),
      excerpts: input.excerpts.map(text => redact(text, this.secrets)) }
    if (!clean.question.trim()) return unavailable
    const key = createHash('sha256').update(JSON.stringify(clean)).digest('hex')
    let cache = this.sessions.get(session)
    if (cache) { this.sessions.delete(session); this.sessions.set(session, cache) }
    const cached = cache?.get(key)
    if (cached !== undefined) {
      cache!.delete(key); cache!.set(key, cached)
      return { status: 'completed', text: cached, cached: true }
    }
    this.active.add(session)
    const controller = new AbortController()
    const signal = callerSignal ? AbortSignal.any([controller.signal, callerSignal]) : controller.signal
    // Retain the slot until the backend settles even when it ignores cancellation.
    const pending = Promise.resolve().then(() => this.port.ask(clean, signal)).finally(() => this.active.delete(session))
    let timer: ReturnType<typeof setTimeout> | undefined
    let abort: (() => void) | undefined
    try {
      const cancelled = new Promise<never>((_, reject) => {
        abort = () => reject(new Error('adviser_cancelled'))
        signal.addEventListener('abort', abort, { once: true })
        timer = setTimeout(() => controller.abort(), this.timeoutMs)
      })
      const text = redact(await Promise.race([pending, cancelled]), this.secrets)
      if (!text.trim() || signal.aborted) return unavailable
      cache ??= new Map<string, string>()
      cache.set(key, text)
      while (cache.size > this.capacity) cache.delete(cache.keys().next().value!)
      this.sessions.set(session, cache)
      while (this.sessions.size > this.sessionCapacity) this.sessions.delete(this.sessions.keys().next().value!)
      return { status: 'completed', text, cached: false }
    } catch { return unavailable }
    finally {
      if (timer) clearTimeout(timer)
      if (abort) signal.removeEventListener('abort', abort)
    }
  }
}
