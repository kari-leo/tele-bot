import type { TrustedActor } from '@telebot/core/contracts'
import type { ChannelSettings } from './types.js'
import { createHash } from 'node:crypto'

export interface MessageTarget { sourceId: string; chatId: string; threadId: string; userId: string; privateChat: boolean; lastSeen: number }
export interface MessageReceipt {
  id: string; chatId: string; threadId: string; state: 'queued' | 'sent' | 'failed' | 'unknown'
  messages: { id: string; state: string; messageId?: string; error?: string }[]
}
export interface MessageRepository {
  messageTargets(): MessageTarget[]
  queueMessage(actor: TrustedActor, key: string, target: MessageTarget, text: string): MessageReceipt
  messageStatus(actor: TrustedActor, id: string, retry?: boolean): MessageReceipt
}

/** Message authority is derived from a trusted entry, never a model-supplied user ID. */
export class FeishuMessages {
  constructor(private readonly repository: MessageRepository, private readonly settings: Pick<ChannelSettings, 'adminUserIds' | 'allowedChatIds'>) {}
  private authorize(actor: TrustedActor) {
    if (actor.origin === 'web') {
      if (actor.permissions !== 'full') throw new Error('feishu_message_actor_not_authorized')
    } else if (!this.settings.adminUserIds.includes(actor.userId) || (this.settings.allowedChatIds?.length && !this.settings.allowedChatIds.includes(actor.chatId))) throw new Error('feishu_message_actor_not_authorized')
  }
  private targets(actor: TrustedActor) {
    this.authorize(actor)
    return this.repository.messageTargets().filter(target => this.settings.adminUserIds.includes(target.userId)
      && (!this.settings.allowedChatIds?.length || this.settings.allowedChatIds.includes(target.chatId))
      && (actor.origin === 'web' || (target.chatId === actor.chatId && target.threadId === (actor.threadId ?? ''))))
  }
  list(actor: TrustedActor, page = 1) {
    if (!Number.isSafeInteger(page) || page < 1) throw new Error('invalid_page')
    const targets = this.targets(actor)
    return { items: targets.slice((page - 1) * 50, page * 50).map(({ sourceId, ...target }) => target), page, total: targets.length, knownConversationsOnly: true }
  }
  send(actor: TrustedActor, input: { chatId?: string; threadId?: string; text: string }, signal: AbortSignal) {
    const targets = this.targets(actor)
    if (typeof input.text !== 'string' || !input.text.trim() || Array.from(input.text).length > 120000 || Buffer.byteLength(input.text) > 480000) throw new Error('invalid_message_text')
    const matches = input.chatId ? targets.filter(row => row.chatId === input.chatId && row.threadId === (input.threadId ?? '')) : targets.filter(row => input.threadId === undefined || row.threadId === input.threadId)
    if (matches.length !== 1) throw new Error(input.chatId ? 'feishu_message_target_not_authorized' : 'feishu_message_target_required')
    const request = actor.origin === 'web' ? actor.nativeRequestId : actor.sourceId
    if (!request) throw new Error('feishu_message_request_identity_required')
    const target = matches[0]
    // Different model call IDs in the same authenticated request cannot duplicate the same message.
    const key = createHash('sha256').update(JSON.stringify([actor.origin, actor.userId, actor.sessionId, actor.cwd, request, target.chatId, target.threadId, input.text])).digest('hex')
    signal.throwIfAborted()
    return this.repository.queueMessage(actor, key, target, input.text)
  }
  status(actor: TrustedActor, id: string) { this.authorize(actor); return this.repository.messageStatus(actor, id) }
  retry(actor: TrustedActor, id: string) {
    const receipt = this.status(actor, id)
    if (!this.targets(actor).some(target => target.chatId === receipt.chatId && target.threadId === receipt.threadId)) throw new Error('feishu_message_target_not_authorized')
    return this.repository.messageStatus(actor, id, true)
  }
}
