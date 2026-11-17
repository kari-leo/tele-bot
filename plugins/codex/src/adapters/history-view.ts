import type { TrustedActor } from '@telebot/core/contracts'

export interface HistoryThread {
  id: string
  cwd: string
  name?: string | null
  preview?: string
  updatedAt?: number
  archived?: boolean
  path?: string | null
  model?: string | null
  modelProvider?: string | null
  status?: { type: string }
}

interface HistoryItem {
  type: string
  text?: string
  content?: { type: string; text?: string }[]
}

export interface HistoryTurnPage {
  data: { items: HistoryItem[] }[]
  nextCursor?: string | null
}

export const historyThreadId = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i
const timestamp = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })

export function archivedThread(thread: HistoryThread) {
  return Boolean(thread.archived || thread.path?.split(/[\\/]/).includes('archived_sessions'))
}

export function historyScope(actor: TrustedActor) {
  return JSON.stringify([actor.userId, actor.chatId, actor.threadId ?? ''])
}

function markdown(text: string) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/[\\`*_\[\]~]/g, '\\$&')
}

function title(thread: HistoryThread) {
  return markdown((thread.name || thread.preview || '未命名会话').split(/\r?\n/, 1)[0].slice(0, 90))
}

export function historyList(threads: readonly HistoryThread[]) {
  const rows = threads.map((thread, index) => `${index + 1}. **${title(thread)}**\n${thread.updatedAt ? timestamp.format(thread.updatedAt * 1000) : '时间未知'} · ${markdown(thread.cwd.slice(0, 180))}`)
  return [rows.join('\n\n') || '没有找到匹配的会话。', '时间为北京时间。'].join('\n\n')
}

export function historyTranscript(page: HistoryTurnPage) {
  const turns = page.data.map(turn => turn.items.flatMap(item => {
    if (item.type === 'userMessage') return ['你：\n' + (item.content ?? []).map(part => part.type === 'text' ? part.text ?? '' : '[非文字输入]').join('\n')]
    if (item.type === 'agentMessage' && item.text) return ['Codex：\n' + item.text]
    return []
  }).join('\n\n')).filter(Boolean)
  const text = turns.join('\n\n── 更早一轮 ──\n\n')
  return markdown(text.length > 100000 ? text.slice(0, 100000) + '\n（本批文字超过 10 万字，其余内容请在 Codex 中查看。）' : text || '此页没有可展示的文字对话。')
}

export function historyDetail(thread: HistoryThread, text: string) {
  return [`**${title(thread)}**\n${markdown(thread.cwd.slice(0, 300))}\n会话 ID：\`${thread.id}\``, '按轮次由新到旧展示用户与助手文字。', text,
    ...(archivedThread(thread) ? ['此会话已归档，只能查看。'] : [])].join('\n\n')
}

export function historyResumePrompt(thread: HistoryThread) {
  return `**${title(thread)}**\n工作目录：${markdown(thread.cwd.slice(0, 300))}\n\n请在 10 分钟内**直接发送一条文字任务**，也可以回复本卡片。提交时会检查原工作目录授权。发送「取消」可结束，发送 \`/new\` 可另开会话。`
}
