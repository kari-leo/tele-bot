/** Public Harness API conversion; no transport, business queue, or model loop lives here. */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent, AgentHandle } from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-agent-preset-registry'
import type {} from '@deepseek-ai/dsh-agent-default-model'
import type {} from '@deepseek-ai/dsh-session-persistence'
import { freezeMessage, MessageId } from '@deepseek-ai/dsh-llm'
import { SessionId, type SessionEvent } from '@deepseek-ai/dsh-session'
import type { ConversationInput, ConversationPort, ConversationResult, ConversationSpec, TrustedActor } from '../contracts.js'
import { HarnessCommands } from './commands.js'
import type { QuestionSink } from '../interactions.js'
import type {} from '@deepseek-ai/dsh-attachment'
import type {} from '@deepseek-ai/dsh-tools'
import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import { HarnessModels } from './models.js'

/** Correlate an identified user message to its own committed turn, never another turn's success. */
function settled(events: readonly SessionEvent[], inputId: string): ConversationResult {
  let turn: number | undefined, currentTurn: number | undefined
  let text = '', reason: string | undefined
  for (const event of events) {
    if (event.type === 'turn/start') currentTurn = event.data.turn
    if (event.type === 'user/message' && event.data.id === inputId) turn = currentTurn
    if (turn !== undefined && event.type === 'assistant/message' && event.data.turn === turn) {
      const candidate = event.data.message.content.filter(block => block.type === 'text').map(block => block.text).join('')
      if (candidate.trim()) text = candidate
    }
    if (turn !== undefined && event.type === 'turn/end' && event.data.turn === turn) reason = event.data.reason.kind
  }
  if (reason === 'completed' && text.trim()) return { status: 'completed', text }
  if (reason === 'interrupted' || reason === 'aborted') return { status: 'cancelled', text: '本轮已停止；停止不代表已发生的外部操作被撤销。' }
  if (reason) return { status: 'failed', text: '本轮未完成，请检查宿主模型或工具状态。' + (text ? `\n执行结束前的输出：\n${text}` : '') }
  return { status: 'unknown', text: '本轮结果尚未确认，系统没有自动重复执行。请先检查会话状态。' }
}

export class HarnessConversations implements ConversationPort {
  readonly commands: HarnessCommands
  private readonly handles = new Map<string, Promise<AgentHandle>>()
  private readonly inFlight = new Map<string, { cancelled: boolean }>()
  private closed = false
  private readonly selections=new HarnessModels()
  constructor(private readonly ctx: Context, private readonly actorScope?: <T>(actor: TrustedActor, run: () => Promise<T>, sink?: QuestionSink) => Promise<T>, private readonly artifacts?: (id: string) => NonNullable<ConversationResult['artifacts']>) { this.commands = new HarnessCommands(ctx, spec => this.open(spec),['compact'],this.selections) }

  private async flush(agent: Agent): Promise<void> {
    const sessions = this.ctx.get('sessions') as Context['sessions'] | undefined
    if (!sessions) throw new Error('harness_session_store_missing')
    await sessions.flush(agent.session)
  }

  private async open(spec: ConversationSpec): Promise<Agent> {
    if (this.closed) throw new Error('conversation_adapter_closed')
    const agents = this.ctx.get('agents') as Context['agents'] | undefined
    const persistence = this.ctx.get('sessionPersistence') as Context['sessionPersistence'] | undefined
    if (!agents || !persistence) throw new Error('missing_harness_agent_or_persistence')
    const live = agents.get(SessionId(spec.id))
    if (live) {
      if (spec.allowedTools && !this.handles.has(spec.id)) throw new Error('restricted_session_owned_elsewhere')
      return live
    }
    let pending = this.handles.get(spec.id)
    if (!pending) {
      pending = (async () => {
        const snapshot = await persistence.stat(SessionId(spec.id))
        if (!snapshot && !spec.allowCreate) throw new Error('mapped_session_missing')
        const presets = this.ctx.get('agentPresets') as Context['agentPresets'] | undefined
        if (spec.preset && !presets) throw new Error('requested_agent_preset_unavailable')
        const preset = snapshot?.header.agentPreset ?? spec.preset
        const fallback = (this.ctx.get('agentDefaultModel') as Context['agentDefaultModel'] | undefined)?.currentSelection()
        const selection = {
          provider: spec.provider ?? fallback?.provider,
          model: spec.model ?? fallback?.model,
        }
        if (!selection.provider || !selection.model) throw new Error('harness_default_model_missing')
        const setup = async (scope: Context,agent:Agent) => {
          await this.selections.mount(scope,agent,{provider:selection.provider!,model:selection.model!})
          // Dedicated tasks must not inherit scoped tools contributed by a broad preset.
          if (presets && !spec.allowedTools) await presets.mount(scope, preset)
          if (spec.allowedTools?.some(name => name === 'web_search' || name === 'web_fetch')) {
            const webTools = await import('@deepseek-ai/dsh-tool-web')
            await scope.plugin(webTools, { search: spec.allowedTools.includes('web_search'), fetch: spec.allowedTools.includes('web_fetch') }).await()
          }
          if (spec.allowedTools) {
            const allowed = new Set(spec.allowedTools)
            const tools = this.ctx.get('tools') as Context['tools'] | undefined
            if (!tools) throw new Error('harness_tools_required')
            const inherited = spec.allowedTools.filter(name => tools.get(name))
            // rc.5 restrict() masks inherited tools and rejects scope-local
            // names. Web tools above are our own registrations. Await policy
            // activation so a rejected restriction can never fail open.
            await scope.plugin({ name: 'telebot-dedicated-task-policy', inject: ['tools'], apply(child: Context) {
              child.tools.restrict({ allow: [...inherited] })
              child.tools.guard(call => allowed.has(call.name) ? undefined : 'dedicated_task_tool_not_allowed')
            } }).await()
          }
        }
        return snapshot
          ? agents.resume({ resumeSessionId: SessionId(spec.id), setup })
          : agents.create({ sessionId: SessionId(spec.id), meta: { cwd: spec.cwd, ...(preset ? { agentPreset: preset } : {}) }, agentOptions: selection, setup })
      })()
      this.handles.set(spec.id, pending)
      pending.catch(() => { if (this.handles.get(spec.id) === pending) this.handles.delete(spec.id) })
    }
    return (await pending).agent
  }

  async run(spec: ConversationSpec, input: ConversationInput): Promise<ConversationResult> {
    if (input.actor && this.actorScope) return this.actorScope(input.actor, () => this.run(spec, { ...input, actor: undefined }), input.question)
    const activity = { cancelled: false }
    let progress = '', lastProgress = Date.now(), publications = 0
    const stopObserving = input.progress ? this.ctx.on('agent/assistant-stream', ({ agent, frame }) => {
      if (agent.session.id !== spec.id || frame.type !== 'chunk' || frame.chunk.type !== 'text-delta') return
      progress = (progress + frame.chunk.text).slice(-6000)
      if (Date.now() - lastProgress < 1000 || publications >= 5) return
      lastProgress = Date.now(); publications++
      try { input.progress?.(progress) } catch { /* The final turn receipt remains authoritative. */ }
    }) : () => {}
    this.inFlight.set(spec.id, activity)
    try {
      const agent = await this.open(spec)
      await agent.whenIdle()
      if (activity.cancelled || this.closed) return { status: 'cancelled', text: '本轮已停止。' }
      const events = agent.session.snapshotEvents()
      const alreadyAccepted = events.some(event =>
        (event.type === 'user/message' && event.data.id === input.id) ||
        (event.type === 'agent/inbox/spliced' && event.data.inserted.some(message => message.id === input.id)))
      if (!alreadyAccepted) {
        const content: ContentBlock[] = [{ type: 'text', text: input.text }]
        if (input.attachments?.length) {
          const attachments = this.ctx.get('attachments') as Context['attachments'] | undefined
          if (!attachments) throw new Error('harness_attachments_unavailable')
          if (input.attachments.filter(file => file.kind === 'image').length > 8 || input.attachments.reduce((n, file) => n + file.data.byteLength, 0) > 30 * 1024 * 1024) throw new Error('attachment_batch_limit')
          for (const file of input.attachments) {
            if (file.kind === 'image') {
              if (file.data.byteLength > 20 * 1024 * 1024) throw new Error('image_limit')
              const image = await attachments.saveImage({ data: file.data, mediaType: file.mediaType as 'image/png', name: file.name })
              content.push({ type: 'image', attachment: image })
            } else content.push({ type: 'file', attachment: await attachments.saveFile({ data: file.data, name: file.name }) })
          }
        }
        agent.followup(freezeMessage({
          id: MessageId(input.id), role: 'user', source: { kind: 'user' },
          content,
        }))
        await this.flush(agent)
      }
      await agent.whenIdle()
      await this.flush(agent)
      return { ...settled(agent.session.snapshotEvents(), input.id), ...(this.artifacts ? { artifacts: this.artifacts(spec.id) } : {}) }
    } finally { stopObserving(); if (this.inFlight.get(spec.id) === activity) this.inFlight.delete(spec.id) }
  }

  async recover(spec: ConversationSpec, input: ConversationInput): Promise<ConversationResult> {
    const agent = await this.open({ ...spec, allowCreate: false })
    await agent.whenIdle()
    await this.flush(agent)
    return settled(agent.session.snapshotEvents(), input.id)
  }

  cancel(id: string): void {
    const activity = this.inFlight.get(id)
    if (activity) activity.cancelled = true
    const agents = this.ctx.get('agents') as Context['agents'] | undefined
    agents?.get(SessionId(id))?.cancel({ kind: 'user' })
  }
  status(id: string) {
    const agent = (this.ctx.get('agents') as Context['agents'] | undefined)?.get(SessionId(id))
    const selection = agent?this.selections.current(agent):undefined
    return { active: Boolean(agent && agent.status !== 'idle'), cwd: agent?.session.header.cwd, model: selection?.model, provider: selection?.provider }
  }
  async dispose(): Promise<void> {
    this.closed = true
    const results = await Promise.allSettled([...this.handles.values()].map(async pending => { await (await pending).dispose() }))
    this.handles.clear()
    const failures = results.filter((result): result is PromiseRejectedResult => result.status === 'rejected')
    if (failures.length) throw new AggregateError(failures.map(result => result.reason), 'harness_agent_disposal_failed')
  }
}
