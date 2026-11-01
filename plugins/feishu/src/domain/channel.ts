import type { ChannelPorts, ChannelSettings, InboxEntry, InputEvent } from './types.js'
import { channelCommand } from './commands.js'
import { baseManagementCommand } from '../business/domain/base-links.js'

function posts(text: string) {
  const points = Array.from(text)
  const result: { msgType: 'post'; content: string }[] = []
  for (let offset = 0; offset < points.length; offset += 6000) {
    result.push({ msgType: 'post', content: JSON.stringify({ zh_cn: { content: [[{ tag: 'md', text: points.slice(offset, offset + 6000).join('') }]] } }) })
  }
  return result
}

/** Durable channel dispatch; Agent work is delegated to the official host, never reimplemented. */
export class FeishuChannel {
  private stopping = false
  private timer: ReturnType<typeof setInterval> | undefined
  private pumping = false
  private delivering = false
  private ready = false
  private readonly active = new Map<string, Promise<void>>()
  private readonly jobs = new Set<Promise<void>>()
  private readonly controlling = new Set<string>()
  private admissionHolds = 0

  constructor(private readonly ports: ChannelPorts, private readonly config: ChannelSettings, private readonly onFailure: () => void = () => {}) {}

  accept(event: InputEvent) {
    if (this.stopping) throw new Error('channel_stopping')
    if (this.admissionHolds) throw new Error('channel_reconfiguring')
    const management = event.kind === 'text' ? baseManagementCommand(event.text) : undefined
    const result = this.ports.repository.accept(management ? { ...event, text: management } : event, this.config)
    if (result.accepted && !result.duplicate) this.wake()
    return result
  }
  holdAdmission(): () => void {
    this.admissionHolds++
    let released = false
    return () => { if (!released) { released = true; this.admissionHolds-- } }
  }

  start(): void {
    this.ports.repository.recoverDeliveries()
    this.timer = setInterval(() => {
      if (this.stopping) return
      try {
        if (!this.ports.repository.heartbeat(this.config.leaseMs)) { this.fail(); return }
        this.wake()
      } catch { this.fail() }
    }, this.config.pollMs)
    const recovery = this.recover().then(() => { this.ready = true; this.wake() })
    this.jobs.add(recovery)
    recovery.catch(() => this.fail()).finally(() => this.jobs.delete(recovery))
  }

  private wake(): void {
    setImmediate(() => { this.pump().catch(() => this.fail()) })
  }
  private fail(): void {
    if (this.stopping) return
    this.stopping = true
    if (this.timer) clearInterval(this.timer)
    for (const sessionId of this.active.keys()) this.ports.agent.cancel(sessionId)
    this.onFailure()
  }
  private async recover(): Promise<void> {
    for (const entry of this.ports.repository.running()) {
      if (this.stopping) return
      if (entry.kind !== 'text' || ['/stop', '/help'].includes(entry.text)) { await this.process(entry); continue }
      if (entry.text.startsWith('/')) {
        this.ports.repository.finish(entry, 'unknown', posts('上次命令结果未确认，没有自动重复执行。请使用 /status 检查。'))
        continue
      }
      const job = (async () => {
        try {
          const result = await this.ports.agent.recover(entry.spec, { id: entry.inputId, text: entry.text })
          this.ports.repository.markMapped(entry.spec.id)
          this.ports.repository.finish(entry, result.status, posts(result.text))
        } catch {
          this.ports.repository.finish(entry, 'unknown', posts('上次请求的状态未确认，系统没有自动重复执行。请使用 /status 查看状态。'))
        }
      })()
      this.active.set(entry.spec.id, job)
      try { await job } finally { this.active.delete(entry.spec.id) }
    }
  }

  private async process(entry: InboxEntry): Promise<void> {
    try {
      if (entry.kind === 'status') {
        const status = this.ports.repository.status(entry.sessionKey)
        const live = this.ports.agent.status(entry.spec.id)
        const text = `会话${live.active ? '正在处理' : '当前空闲'}；待处理 ${status.pending} 条，状态待核实 ${status.unknown} 条。` +
          `\n模型：${live.provider ?? entry.spec.provider ?? '宿主默认'}/${live.model ?? entry.spec.model ?? ''}\n工作目录：${live.cwd ?? entry.spec.cwd}`
        const content = this.ports.repository.statusCard(entry, text, this.config.actionTtlMs)
        this.ports.repository.finish(entry, 'completed', [{ msgType: 'interactive', content }])
      } else if (entry.kind === 'notice') this.ports.repository.finish(entry, 'completed', posts(entry.text))
      else if (entry.kind === 'attachment') {
        if (!this.ports.files) throw new Error('file_transfers_unavailable')
        await this.ports.files.receive(entry)
        this.ports.repository.finish(entry, 'completed', posts('附件已按当前用户/会话暂存 24 小时。请发送处理要求；不会自动加入知识库。'))
      }
      else if (entry.kind === 'answer' || entry.text.startsWith('/answer ')) {
        const result = await this.ports.features?.invoke('answer', { actor: {
          userId: entry.actor.authorizedUserId ?? entry.actor.ids[0], chatId: entry.chatId, sessionId: entry.spec.id,
          cwd: entry.spec.cwd, privateChat: Boolean(entry.actor.privateChat), origin: 'feishu',
        }, text: entry.text.slice('/answer '.length), signal: AbortSignal.timeout(5000) })
        this.ports.repository.finish(entry, result?.status ?? 'failed', posts(result?.text ?? '回答服务不可用。'))
      }
      else if (entry.text === '/stop') {
        this.ports.agent.cancel(entry.spec.id)
        await this.ports.features?.invoke('codex', { actor: { userId: entry.actor.authorizedUserId ?? entry.actor.ids[0], chatId: entry.chatId,
          sessionId: entry.spec.id, cwd: entry.spec.cwd, privateChat: Boolean(entry.actor.privateChat), origin: 'feishu' }, text: 'stop', signal: AbortSignal.timeout(5000) })
        this.ports.repository.finish(entry, 'completed', posts('已请求停止当前执行；排队消息保留，已发生的外部操作不会撤销。'))
      } else if (entry.text.startsWith('/')) {
        if (!this.ready || this.active.has(entry.spec.id) || this.controlling.has(entry.spec.id) || this.ports.agent.status(entry.spec.id).active) {
          this.ports.repository.finish(entry, 'failed', posts('会话正在执行，请先停止或等待后再使用命令。'))
        } else {
          this.controlling.add(entry.spec.id)
          try {
            if (entry.text.startsWith('/send ') && this.ports.files) {
              const content = await this.ports.files.outbound(entry, entry.text.slice('/send '.length).trim())
              this.ports.repository.finish(entry, 'completed', [{ msgType: 'file', content }])
            } else {
              const result = await channelCommand(this.ports, entry)
              const outputs: { msgType: 'post' | 'file'; content: string }[] = posts(result.text)
              for (const artifact of result.artifacts ?? []) {
                try {
                  if (!this.ports.files) throw new Error('file_transfers_unavailable')
                  const content = await this.ports.files.outbound(entry, artifact.path)
                  if (JSON.parse(content).sha256 !== artifact.sha256) throw new Error('artifact_changed_after_execution')
                  outputs.push({ msgType: 'file', content })
                } catch { outputs.push(...posts('产物未通过目录、大小或哈希复核，未上传。业务执行不会因此重跑，请核对产物后显式发送。')) }
              }
              this.ports.repository.finish(entry, result.status, outputs)
            }
          } finally { this.controlling.delete(entry.spec.id) }
        }
      } else {
        if (this.ports.features && /^(?:确认|确定|确认执行|确认修改|确认提交|确认按预览执行|取消|取消操作|取消修改|confirm|cancel)[！!。\s]*$/i.test(entry.text)) {
          const cancel = /^(?:取消|cancel)/i.test(entry.text)
          const result = await this.ports.features.invoke(this.ports.features.has?.('confirm') ? 'confirm' : 'bitable', { actor: {
            userId: entry.actor.authorizedUserId ?? entry.actor.ids[0], chatId: entry.chatId, sessionId: entry.spec.id,
            cwd: entry.spec.cwd, privateChat: Boolean(entry.actor.privateChat), origin: 'feishu', sourceId: entry.id, threadId: entry.threadId,
          }, text: this.ports.features.has?.('confirm') ? (cancel ? 'cancel' : 'confirm') : (cancel ? 'cancel-latest' : 'confirm-latest'), signal: AbortSignal.timeout(120000) })
          if (result.text) { this.ports.repository.finish(entry, result.status, posts(result.text)); return }
        }
        const attachments = await this.ports.files?.forMessage(entry)
        const result = await this.ports.agent.run(entry.spec, { id: entry.inputId, text: entry.text, attachments,
          progress: this.config.streamProgress ? text => {
            this.ports.repository.enqueue?.(entry, [{ msgType: 'interactive', content: JSON.stringify({ header: { title: { tag: 'plain_text', content: '执行进度' } }, elements: [{ tag: 'div', text: { tag: 'lark_md', content: text } }] }) }]); this.wake()
          } : undefined, question: async question => {
          if (!this.ports.repository.question || !this.ports.repository.enqueue) throw new Error('question_delivery_unavailable')
          this.ports.repository.enqueue(entry, [{ msgType: 'interactive', content: this.ports.repository.question(entry, question) }]); this.wake()
        }, actor: {
          userId: entry.actor.ids.find(id => this.config.adminUserIds.includes(id))!, chatId: entry.chatId,
          sessionId: entry.spec.id, cwd: entry.spec.cwd, privateChat: Boolean(entry.actor.privateChat), origin: 'feishu', startedAt: Date.now(), sourceId: entry.id, threadId: entry.threadId,
        } })
        this.ports.repository.markMapped(entry.spec.id)
        this.ports.repository.finish(entry, result.status, posts(result.text))
      }
    } catch (error) {
      const rejected = error instanceof Error && [
        'absolute_workspace_required', 'workspace_not_directory', 'workspace_outside_allowed_roots',
        'workspace_has_pending_requests', 'model_not_in_harness_catalog', 'conversation_busy',
      ].includes(error.message)
      this.ports.repository.finish(entry, rejected ? 'failed' : 'unknown', posts(rejected
        ? '请求被拒绝：请检查模型目录、工作区允许范围及待处理请求。'
        : '本轮执行状态未确认，没有自动重复运行。请先使用 /status 检查。'))
    }
  }

  private async delivery(): Promise<void> {
    if (this.delivering || this.stopping) return
    this.delivering = true
    try {
      const message = this.ports.repository.claimDelivery(Date.now())
      if (!message) return
      let result
      try { result = await this.ports.delivery.send(message,key=>this.ports.repository.uploaded?.(message,key)) }
      catch { result = { status: 'unknown' as const, code: 'delivery_outcome_unknown' } }
      this.ports.repository.settleDelivery(message, result, Date.now() + this.config.retryMs, this.config.maxDeliveryAttempts)
    } finally { this.delivering = false }
  }

  private async pump(): Promise<void> {
    if (this.pumping || this.stopping) return
    this.pumping = true
    try {
      while (!this.stopping) {
        const entry = this.ports.repository.claim([...this.active.keys(), ...this.controlling], !this.ready || this.active.size + this.controlling.size >= this.config.maxConcurrentSessions)
        if (!entry) break
        const occupies = entry.kind === 'text' && !entry.text.startsWith('/')
        const job = this.process(entry).finally(() => {
          if (occupies) this.active.delete(entry.spec.id)
          this.jobs.delete(job)
          if (!this.stopping) this.wake()
        })
        // Status/stop entries bypass blocked/running conversations without claiming their Agent slot.
        if (occupies) this.active.set(entry.spec.id, job)
        this.jobs.add(job)
        job.catch(() => this.fail())
      }
      this.delivery().catch(() => this.fail())
    } finally { this.pumping = false }
  }

  async stop(): Promise<void> {
    this.stopping = true
    if (this.timer) clearInterval(this.timer)
    for (const sessionId of this.active.keys()) this.ports.agent.cancel(sessionId)
    await Promise.allSettled([...this.jobs])
    while (this.delivering) await new Promise(resolve => setTimeout(resolve, 10))
    await this.ports.agent.dispose()
  }
}
