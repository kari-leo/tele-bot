/** Thin adapter to the official registry; approved names are a deployment compatibility list. */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent } from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-commands'
import type {} from '@deepseek-ai/dsh-permission-presets'
import type { CommandPort, ConversationSpec, ModelRoute } from '../contracts.js'
import { HarnessModels } from './models.js'

export class HarnessCommands implements CommandPort {
  constructor(private readonly ctx: Context, private readonly open: (spec: ConversationSpec) => Promise<Agent>,
    private readonly allowed: readonly string[] = ['compact'],private readonly selections=new HarnessModels()) {}
  private registry() {
    const registry = this.ctx.get('commands') as Context['commands'] | undefined
    if (!registry) throw new Error('harness_commands_unavailable')
    return registry
  }
  private async flush(agent: Agent): Promise<void> {
    const sessions = this.ctx.get('sessions') as Context['sessions'] | undefined
    if (!sessions) throw new Error('harness_session_store_missing')
    await sessions.flush(agent.session)
  }
  async prepare(spec: ConversationSpec): Promise<void> {
    const agent = await this.open(spec)
    if (agent.session.header.cwd !== spec.cwd) throw new Error('harness_workspace_mismatch')
    await this.flush(agent)
  }
  async permission(spec: ConversationSpec, preset?: string) {
    const service = this.ctx.get('permissionPresets') as Context['permissionPresets'] | undefined
    if (!service) throw new Error('harness_permission_presets_unavailable')
    const agent = await this.open(spec)
    if (preset) {
      if (!['read-only', 'workspace-write'].includes(preset) || !service.names.includes(preset)) throw new Error('permission_preset_not_allowed')
      const resolved = service.resolve(preset)
      if (resolved.sandbox === 'danger-full-access') throw new Error('permission_preset_not_allowed')
      service.set(agent.session, preset); await this.flush(agent)
    }
    const label=(value:string)=>({'read-only':'只读','workspace-write':'当前工作区可写'} as Record<string,string>)[value]??value
    return `**会话权限**\n\n当前：${label(service.current(agent.session))}\n可选：${service.names.filter(name => ['read-only', 'workspace-write'].includes(name)).map(label).join('、')}`
  }
  async list(spec: ConversationSpec) {
    return this.registry().list(await this.open(spec)).filter(row => this.allowed.includes(row.name))
      .map(({ name, description }) => ({ name, description }))
  }
  async execute(spec: ConversationSpec, line: string, signal: AbortSignal) {
    const match = /^\/([a-z][a-z0-9_-]*)(?=$|\s)/.exec(line)
    if (!match || !this.allowed.includes(match[1])) return { status: 'failed' as const, text: '未知或未获渠道许可的命令，请使用 /help。' }
    const registry = this.registry(), agent = await this.open(spec)
    if (agent.status !== 'idle') return { status: 'failed' as const, text: '会话正在执行，请先停止或等待。' }
    if (!registry.find(agent, match[1])) return { status: 'failed' as const, text: '当前宿主未挂载此命令。' }
    const execution = await registry.execute(agent, line, [], signal)
    await this.flush(agent)
    if (!execution) return { status: 'failed' as const, text: '命令已不可用，请重新查看 /help。' }
    return { status: execution.result.kind === 'success' ? 'completed' as const : 'failed' as const,
      text: execution.result.text || '命令已完成。' }
  }
  async models() {
    const llm = this.ctx.get('llm') as Context['llm'] | undefined
    if (!llm) throw new Error('harness_models_unavailable')
    const rows = await Promise.all(llm.listProviders().map(row => llm.listModels(row.id)))
    return rows.flat().map(row => ({ provider: row.provider, model: row.id }))
  }
  async selectModel(spec: ConversationSpec, route: ModelRoute): Promise<void> {
    const routes = await this.models()
    if (!routes.some(row => row.provider === route.provider && row.model === route.model)) throw new Error('model_not_in_harness_catalog')
    const agent = await this.open(spec)
    if (agent.status !== 'idle') throw new Error('conversation_busy')
    const previous = this.selections.current(agent)
    this.selections.select(agent,route)
    try { await this.flush(agent) }
    catch (error) { if (previous) this.selections.select(agent,previous); throw error }
  }
}
