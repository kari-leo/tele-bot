import { randomUUID } from 'node:crypto'
import type { TrustedActor } from './contracts.js'
export interface ChannelQuestion { token: string; question: string; choices: string[]; approval: boolean }
export type QuestionSink = (question: ChannelQuestion) => Promise<void>
/** One-use in-memory answer authority. Restart deliberately invalidates all pending answers. */
export class Interactions {
  private readonly pending = new Map<string, { actor: TrustedActor; choices: string[]; approval: boolean; settle(answer: string): void }>()
  async ask(actor: TrustedActor, question: string, choices: string[], approval: boolean, sink: QuestionSink, signal?: AbortSignal): Promise<string> {
    if (this.pending.size >= 100) throw new Error('interaction_limit')
    const token = randomUUID(), controller = new AbortController()
    const lifetime = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal
    const timer = setTimeout(() => controller.abort(), 300000)
    let resolve: (answer: string) => void = () => {}
    const answer = new Promise<string>(done => { resolve = done })
    const abort = () => resolve('')
    this.pending.set(token, { actor, choices, approval, settle: resolve })
    lifetime.addEventListener('abort', abort, { once: true })
    try {
      lifetime.throwIfAborted()
      await sink({ token, question, choices, approval })
      return await answer
    } finally { clearTimeout(timer); lifetime.removeEventListener('abort', abort); this.pending.delete(token) }
  }
  answer(actor: TrustedActor, token: string, answer: string): boolean {
    const row = this.pending.get(token)
    if (!row || row.actor.userId !== actor.userId || row.actor.chatId !== actor.chatId || row.actor.sessionId !== actor.sessionId || row.actor.cwd !== actor.cwd || answer.length > 10000 || (row.approval && !row.choices.includes(answer))) return false
    this.pending.delete(token); row.settle(answer); return true
  }
  cancelSession(sessionId: string) { for (const [token, row] of this.pending) if (row.actor.sessionId === sessionId) { this.pending.delete(token); row.settle('') } }
}
