import type { InputEvent } from './types.js'

export function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid_object')
  return value as Record<string, unknown>
}
export const string = (value: unknown): string => typeof value === 'string' ? value : ''
const identifier = (value: unknown): string => {
  const result = string(value)
  if (!/^[A-Za-z0-9_.:-]{1,256}$/.test(result)) throw new Error('invalid_identifier')
  return result
}
function ids(value: unknown): string[] {
  const source = object(value)
  const result = [source.user_id, source.open_id].filter(item => typeof item === 'string' && item.length > 0).map(identifier)
  if (!result.length) throw new Error('missing_actor')
  return [...new Set(result)]
}

/** Normalize only fields actually asserted by the authenticated platform envelope. */
export function parseEvent(payload: Record<string, unknown>, appId: string): InputEvent | undefined {
  const header = payload.header ? object(payload.header) : undefined
  if (header && header.app_id !== appId) throw new Error('wrong_app')
  if (!header && payload.app_id && payload.app_id !== appId) throw new Error('wrong_app')
  if (header?.event_type === 'card.action.trigger' || (!header && payload.action)) {
    const event = header ? object(payload.event) : payload
    const operator = object(event.operator ?? { user_id: payload.user_id, open_id: payload.open_id })
    const context = object(event.context ?? payload)
    const action = object(event.action)
    const value = object(action.value)
    if (!['telebot.status', 'telebot.answer'].includes(string(value.action))) return undefined
    const messageId = identifier(context.open_message_id)
    const actionToken = identifier(value.token)
    return {
      eventId: header ? identifier(header.event_id) : `card:${actionToken}`,
      messageId, chatId: identifier(context.open_chat_id), threadId: '',
      actor: { ids: ids(operator.operator_id ?? operator) }, kind: value.action === 'telebot.answer' ? 'answer' : 'status',
      text: value.action === 'telebot.answer' ? `/answer ${actionToken} ${string(value.answer)}` : '/status', actionToken,
    }
  }
  if (header?.event_type !== 'im.message.receive_v1') return undefined
  const event = object(payload.event)
  const sender = object(event.sender)
  if (sender.sender_type !== 'user') return undefined
  const message = object(event.message)
  const base = {
    eventId: identifier(header.event_id), messageId: identifier(message.message_id),
    chatId: identifier(message.chat_id), threadId: message.thread_id ? identifier(message.thread_id) : '',
    actor: { ids: ids(sender.sender_id), privateChat: message.chat_type === 'p2p', referenceMessageId: message.parent_id ? identifier(message.parent_id) : undefined },
  }
  const content = object(JSON.parse(string(message.content)))
  if (['file', 'image'].includes(string(message.message_type))) return { ...base, kind: 'attachment', text: JSON.stringify({
    kind: message.message_type, key: identifier(content.file_key ?? content.image_key), name: string(content.file_name) || 'image',
  }) }
  let text = ''
  if (message.message_type === 'text') text = string(content.text).trim()
  else if (message.message_type === 'post') {
    const post = content.content ? content : object(content.zh_cn ?? content.en_us)
    const lines = [string(post.title)]
    if (Array.isArray(post.content)) for (const row of post.content) {
      if (!Array.isArray(row)) continue
      lines.push(row.map(value => {
        const element = object(value)
        return ['text', 'md', 'a', 'code_block'].includes(string(element.tag)) ? string(element.text ?? element.href) : ''
      }).join(''))
    }
    text = lines.filter(Boolean).join('\n').trim()
  } else {
    return { ...base, kind: 'notice', text: '暂不支持此消息类型，请发送文字、文件或图片。' }
  }
  if (Array.isArray(message.mentions)) for (const mention of message.mentions) {
    const key = string(object(mention).key)
    if (key && text.startsWith(key)) { text = text.slice(key.length).trim(); break }
  }
  if (!text) return undefined
  if (text.length > 40000) throw new Error('message_too_long')
  return { ...base, kind: text === '/status' ? 'status' : 'text', text }
}
