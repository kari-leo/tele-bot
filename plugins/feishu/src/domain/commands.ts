import type { ConversationResult } from '@telebot/core/contracts'
import type { ChannelPorts, InboxEntry } from './types.js'

/** Authorization comes from durable channel admission, never command arguments. */
export async function channelCommand(ports: ChannelPorts, entry: InboxEntry): Promise<ConversationResult> {
  const match = /^\/([a-z][a-z0-9_-]*)(?:\s+([\s\S]*))?$/.exec(entry.text)
  if (!match) return { status: 'failed', text: '命令格式不正确，请使用 /help。' }
  const name = match[1], argument = (match[2] ?? '').trim()
  if (name === 'remind' && ports.reminders) return { status: 'completed', text: await ports.reminders.command(entry) }
  if (['codex', 'adviser', 'feishu', 'bitable', 'bases', 'schedule', 'publish', 'report'].includes(name) && ports.features) return ports.features.invoke(name, {
    actor: { userId: entry.actor.authorizedUserId ?? entry.actor.ids[0], chatId: entry.chatId, sessionId: entry.spec.id, cwd: entry.spec.cwd,
      privateChat: Boolean(entry.actor.privateChat), origin: 'feishu', sourceId: entry.id, threadId: entry.threadId }, text: argument, signal: AbortSignal.timeout(20 * 60 * 1000),
  })
  if (name === 'help') {
    let native = ''
    try { native = (await ports.commands?.list(entry.spec))?.map(row => `/${row.name}：${row.description}`).join('\n') ?? '' } catch {}
    return { status: 'completed', text: '/help /status /stop /model /cd /new /reset /permission /setting /answer /send /remind /feishu /bases /codex /adviser /schedule /publish\n/bases add|remove <链接>；/bases list。也可说“添加多维表格链接 <链接>”或“移除多维表格 <链接>”，移除不删除远端数据。\n/model <provider/model> 切换当前会话模型；/cd <绝对路径> 切换工作区。业务插件未安装或未启用时会明确提示。' + (native ? `\n${native}` : '') }
  }
  if (!ports.commands) return { status: 'failed', text: '当前部署未提供宿主命令适配。' }
  if (name === 'permission') return { status: 'completed', text: ports.commands.permission ? await ports.commands.permission(entry.spec, argument || undefined) : '当前宿主未提供权限预设。' }
  if (name === 'setting') return { status: 'completed', text: JSON.stringify({ ...ports.agent.status(entry.spec.id), cwd: entry.spec.cwd, globalSettingsMutableFromChannel: false }) }
  if (['new', 'reset'].includes(name)) {
    if (!ports.workspace || ports.repository.status(entry.sessionKey).pending) return { status: 'failed', text: '请先处理排队消息，再新建会话。' }
    const current = ports.agent.status(entry.spec.id)
    const spec = { ...entry.spec, id: ports.workspace.sessionId(), allowCreate: true, provider: current.provider ?? entry.spec.provider, model: current.model ?? entry.spec.model }
    await ports.commands.prepare(spec); ports.repository.updateMapping(entry, { ...spec, allowCreate: false })
    return { status: 'completed', text: '已在当前工作区创建新会话；旧历史与业务数据保留。' }
  }
  if (name === 'model') {
    if (!argument) {
      const routes = await ports.commands.models()
      const live = ports.agent.status(entry.spec.id)
      return { status: 'completed', text: `当前模型：${live.provider ?? entry.spec.provider ?? '宿主默认'}/${live.model ?? entry.spec.model ?? ''}\n` + routes.map(row => `${row.provider}/${row.model}`).join('\n') }
    }
    const slash = argument.indexOf('/')
    if (slash <= 0 || slash === argument.length - 1) return { status: 'failed', text: '请使用 /model <provider/model>。' }
    const route = { provider: argument.slice(0, slash), model: argument.slice(slash + 1) }
    await ports.commands.selectModel(entry.spec, route)
    ports.repository.updateMapping(entry, { ...entry.spec, ...route, allowCreate: false })
    return { status: 'completed', text: `当前会话模型已切换为 ${route.provider}/${route.model}，下一轮生效。` }
  }
  if (name === 'cd') {
    if (!argument) return { status: 'completed', text: `当前工作目录：${ports.agent.status(entry.spec.id).cwd ?? entry.spec.cwd}` }
    if (!ports.workspace) return { status: 'failed', text: '当前部署未配置工作区切换。' }
    if (ports.repository.status(entry.sessionKey).pending) return { status: 'failed', text: '会话仍有排队请求，请等待处理完毕再切换目录。' }
    const cwd = await ports.workspace.resolve(argument)
    const live = ports.agent.status(entry.spec.id)
    const spec = { ...entry.spec, id: ports.workspace.sessionId(), cwd, allowCreate: true,
      provider: live.provider ?? entry.spec.provider, model: live.model ?? entry.spec.model }
    await ports.commands.prepare(spec)
    ports.repository.updateMapping(entry, { ...spec, allowCreate: false })
    return { status: 'completed', text: `工作目录已切换为 ${cwd}。新工作区使用独立会话，旧会话历史保留。` }
  }
  return ports.commands.execute(entry.spec, entry.text, AbortSignal.timeout(120000))
}
