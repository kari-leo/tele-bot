import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import { TelebotCore } from '@telebot/core'
import type { TrustedActor } from '@telebot/core/contracts'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'
import { FeishuHttp, origin } from './adapters/http.js'
import { OperationLedger } from './adapters/ledger.js'
import { Resources, validateResources, type Operation, type Resource } from './domain/resources.js'
import { Config } from './config.js'
export { Config }

export const name = 'telebot-feishu-business'
export const inject = ['telebotCore', 'tools']

export async function apply(ctx: Context, config: Config, access: { tenantGrant(): Promise<{ token: string; expiresAt: number }> }) {
  if (!(ctx.telebotCore instanceof TelebotCore)) throw new Error('core_identity_mismatch')
  const base = origin(config.apiBaseUrl), resources = JSON.parse(config.resourcesJson) as Resource[]
  if (!Array.isArray(resources)) throw new Error('resources_array_required')
  const directory = config.dataDirectory || path.join(resolveDshHome(), 'plugins', 'telebot-feishu-tools')
  if (!path.isAbsolute(directory)) throw new Error('absolute_data_directory_required')
  await mkdir(directory, { recursive: true, mode: 0o700 })
  const service = new Resources(resources, config.adminUserIds, row => new FeishuHttp(base, async () => {
    const grant = await access.tenantGrant(); row.expiresAt = grant.expiresAt; return grant.token
  }))
  let resourceHold = false
  const ledger = new OperationLedger(path.join(directory, 'operations.sqlite'))
  const authorize = (actor: TrustedActor) => { if (!config.adminUserIds.includes(actor.userId)) throw new Error('actor_not_authorized') }
  const prepare = async (actor: TrustedActor, operation: Operation) => {
    if (resourceHold) throw new Error('resource_configuration_busy')
    authorize(actor)
    const resource = service.configured(operation.resource)
    resource.expiresAt = (await access.tenantGrant()).expiresAt
  }
  const invoke = async (actor: TrustedActor, operation: Operation, signal: AbortSignal) => { await prepare(actor, operation); return ledger.preview(service, actor, operation, signal) }
  const lifetime = new AbortController(), jobs = new Set<Promise<any>>()
  const track = <T>(work: () => Promise<T>) => { const job = work(); jobs.add(job); job.finally(() => jobs.delete(job)).catch(() => {}); return job }
  const refresh = async (actor: TrustedActor) => {
    if (resourceHold) throw new Error('resource_configuration_busy')
    authorize(actor)
    const grant = await access.tenantGrant()
    for (const row of resources) row.expiresAt = grant.expiresAt
  }
  const handler: import('@telebot/core/contracts').FeatureHandler = request => track(async () => {
    const { actor, text } = request, signal = AbortSignal.any([request.signal, lifetime.signal])
    signal.throwIfAborted()
    authorize(actor)
    const [action, value = ''] = text.split(/\s+/, 2)
    let result: unknown
    if (['confirm-latest', 'cancel-latest'].includes(action)) {
      const pending = ledger.pending(actor)
      if (!pending.length) return { status: 'failed', text: '' }
      if (pending.length !== 1) return { status: 'failed', text: '存在多个待确认操作，请使用 /bitable confirm|cancel <令牌> 明确选择。' }
      if (action === 'confirm-latest') await refresh(actor)
      result = action === 'confirm-latest' ? await ledger.confirm(service, actor, pending[0].token, signal) : { cancelled: ledger.cancel(actor, pending[0].token) }
    } else if (action === 'whoami') result = { actor: actor.userId, origin: actor.origin }
    else if (action === 'list') {
      const page = value ? Number(value) : 1
      if (!Number.isSafeInteger(page) || page < 1) throw new Error('invalid_page')
      const visible = resources.filter(row => row.kind === 'bitable')
      result = { items: visible.slice((page - 1) * 5, page * 5).map(({ alias, id, tableId }) => ({ alias, id, tableId })), page, total: visible.length, known_resources_only: true }
    } else if (action === 'status') result = ledger.status(actor, value)
    else if (action === 'supplement') {
      await refresh(actor)
      const json = text.slice(text.indexOf(value) + value.length).trim()
      result = await ledger.supplement(service, actor, value, JSON.parse(json), signal)
    } else if (action === 'reconcile') { await refresh(actor); result = await ledger.reconcile(service, actor, value, signal) }
    else if (action === 'confirm') {
      // Refresh the application grant before rechecking the user-bound frozen plan.
      await refresh(actor)
      result = await ledger.confirm(service, actor, value, signal)
    } else if (action === 'cancel') result = { cancelled: ledger.cancel(actor, value) }
    else {
      const operation = JSON.parse(text) as Operation
      result = await invoke(actor, operation, signal)
      if (!config.confirmAll && ['create', 'update'].includes(operation.action) && operation.params.records?.length === 1 && result && typeof result === 'object' && 'token' in result) result = await ledger.confirm(service, actor, String(result.token), signal)
    }
    return { status: 'completed', text: JSON.stringify(result) }
  })
  const unregister = ctx.telebotCore.registerFeature('feishu', handler)
  const unregisterBitable = ctx.telebotCore.registerFeature('bitable', handler)
  const unregisterConfirm = ctx.telebotCore.registerConfirmations('bitable', { pending: actor => ledger.pending(actor).map(row => ({ token: row.token, label: row.operation })), confirm: (actor, token, signal) => handler({ actor, text: 'confirm ' + token, signal }), cancel: (actor, token) => handler({ actor, text: 'cancel ' + token, signal: lifetime.signal }) })
  ctx.effect(() => async () => { unregister(); unregisterBitable(); unregisterConfirm(); lifetime.abort(); await Promise.allSettled([...jobs]); ledger.close() })
  const tool = (toolName: string, fixedAction?: Operation['action'], kind?: Resource['kind']) => ctx.tools.register(defineTool({
    name: toolName, description: 'Feishu resource operation. Writes produce a frozen preview; only a trusted user command may confirm. Application identity only.',
    parameters: { resource: { type: 'string', required: true }, action: { type: 'string', enum: ['inspect', 'query', 'create', 'update', 'delete'] }, params_json: { type: 'string', required: true } },
    output: { schema: { type: 'object', properties: { json: { type: 'string', required: true } }, additionalProperties: false }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    async execute(args, exec) { return track(async () => {
      if (!exec.agent) throw new Error('trusted_agent_required')
      const actor = ctx.telebotCore.actor(exec.agent.session.id)
      const operation: Operation = { resource: args.resource, action: fixedAction ?? args.action as Operation['action'], params: JSON.parse(args.params_json) }
      await prepare(actor, operation)
      if (kind && service.resource(actor, operation).kind !== kind) throw new Error('resource_kind_mismatch')
      return { json: JSON.stringify(await invoke(actor, operation, AbortSignal.any([exec.signal, lifetime.signal]))) }
    })
    },
  }))
  for (const action of ['inspect', 'query', 'create', 'update', 'delete'] as const) tool('feishu_bitable_' + action, action, 'bitable')
  for (const [old, action] of [['bitable_inspect', 'inspect'], ['bitable_query', 'query'], ['bitable_create_records', 'create'], ['bitable_update_records', 'update'], ['bitable_delete_records', 'delete']] as const) tool(old, action, 'bitable')
  for (const kind of ['documents', 'calendar', 'tasks'] as const) tool('feishu_' + kind, undefined, kind)
  return {
    invalidatePending: () => ledger.invalidatePending(),
    holdResources() {
      if (resourceHold || jobs.size) throw new Error('resource_configuration_busy')
      resourceHold = true
      return () => { resourceHold = false }
    },
    replaceResources(next: Resource[]) {
      validateResources(next)
      new Resources(next, config.adminUserIds, () => { throw new Error('validation_only') })
      resources.splice(0, resources.length, ...next)
    },
  }
}
