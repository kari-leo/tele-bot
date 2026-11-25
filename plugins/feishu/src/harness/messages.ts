import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type {} from '@telebot/core'
import { FeishuMessages } from '../domain/messages.js'

function present(value: any): string {
  if (Array.isArray(value.items)) return value.items.length ? `**已接入的飞书会话**\n\n${value.items.map((row: any, index: number) => `${index + 1}. ${row.privateChat ? '私聊' : '群聊'}：${row.chatId}${row.threadId ? '\n   话题：' + row.threadId : ''}\n   飞书用户：${row.userId}`).join('\n')}\n\n第 ${value.page} 页，共 ${value.total} 个会话；仅列出已通过渠道接入校验的目标。` : '暂无已接入的飞书会话。请先在飞书向机器人发送一条消息，再重新查询目标。'
  const state = { queued: '已排队，尚未确认送达', sent: '已送达', failed: '发送失败，可查询原因后重试', unknown: '结果待核实，不自动重发' }[value.state as 'queued' | 'sent' | 'failed' | 'unknown']
  return `**飞书消息投递**\n\n投递编号：${value.id}\n目标会话：${value.chatId}${value.threadId ? '\n话题：' + value.threadId : ''}\n状态：${state}\n${value.messages.map((row: any) => row.messageId ? '飞书消息编号：' + row.messageId : row.error ? '投递提示：' + row.error : '').filter(Boolean).join('\n')}`
}

export function registerMessageTool(ctx: Context, service: FeishuMessages, available: () => boolean) {
  ctx.tools.register(defineTool({
    name: 'feishu_messages',
    description: 'Send a text/Markdown notification to Feishu NOW, without creating a schedule. First list known connected chats and threads; never guess chat IDs or silently choose among multiple targets. Full-access authenticated desktop owners may select any listed target; Feishu users may send only to their current chat/thread. Actions: list (page), send (chat_id, optional thread_id, text), status/retry (delivery_id). If exactly one target exists, send may omit chat_id. Repeated identical messages in the same user request reuse one durable receipt. A send receipt with queued means NOT delivered: use status; only sent with remote message IDs confirms delivery. retry only requeues definite failures, never unknown outcomes. Existing bot/application permissions still apply. No identity or credential arguments.',
    parameters: { action: { type: 'string', enum: ['list', 'send', 'status', 'retry'], required: true }, chat_id: { type: 'string' }, thread_id: { type: 'string' }, text: { type: 'string' }, delivery_id: { type: 'string' }, page: { type: 'number' } },
    output: { schema: { type: 'object', properties: { json: { type: 'string', required: true } }, additionalProperties: false }, render: (_args, value) => [{ type: 'text', text: present(JSON.parse(value.json)) }] },
    async execute(args, exec) {
      if (!exec.agent) throw new Error('trusted_agent_required')
      if (!available()) throw new Error('feishu_channel_not_ready')
      exec.signal.throwIfAborted()
      const actor = ctx.telebotCore.actor(exec.agent.session.id)
      let result
      if (args.action === 'list') result = service.list(actor, args.page)
      else if (args.action === 'send') result = await service.send(actor, { chatId: args.chat_id, threadId: args.thread_id, text: args.text ?? '' }, exec.signal)
      else if (args.action === 'status') result = service.status(actor, args.delivery_id ?? '')
      else if (args.action === 'retry') result = service.retry(actor, args.delivery_id ?? '')
      else throw new Error('unsupported_feishu_message_action')
      return { json: JSON.stringify(result) }
    },
  }))
}
