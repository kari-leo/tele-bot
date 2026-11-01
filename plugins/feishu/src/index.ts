/** One installable Feishu plugin: configuration UI, webhook and all business capabilities. */
import type { Context, Fiber } from '@deepseek-ai/cordis'
import WebServer from '@deepseek-ai/dsh-host-webserver'
import { credentialRef } from '@deepseek-ai/dsh-credentials'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import path from 'node:path'
import { Config, validateCallbacks } from './config.js'
import { Configuration, type Credentials } from './adapters/configuration.js'
import { registerConfigurationPage } from '@telebot/core/configuration'
import { feishuFields } from './setup/fields.js'
import { apply as startBusiness } from './business/index.js'
import { startChannel } from './harness/runtime.js'
import { FeishuApi, apiOrigin } from './adapters/feishu-api.js'
import { discoverBase } from './business/adapters/discovery.js'
import { FeishuHttp } from './business/adapters/http.js'
import { baseLink, linkedBases, type BaseLink } from './business/domain/base-links.js'
import { Resources, type Resource } from './business/domain/resources.js'

export { Config }
export const name = 'telebot-feishu'
export const inject = ['telebotCore', 'webServer', 'credentials', 'tools']
export async function apply(ctx: Context, bootstrap: Config) {
  const credentials: Credentials = {
    read: async ref => ref ? (await ctx.credentials.resolve(credentialRef(ref)))?.value ?? '' : '',
    write: async (ref, value) => ctx.credentials.set(credentialRef(ref), value),
    remove: async ref => ctx.credentials.unset(credentialRef(ref)),
  }
  const directory = bootstrap.configDirectory || path.join(resolveDshHome(), 'plugins', 'telebot-feishu', 'config')
  const store = await Configuration.open(directory, bootstrap, credentials)
  // Desktop owns an ephemeral root listener. Isolate only the official HTTP
  // service; Agent, models, tools, credentials and scheduling remain shared.
  if (store.current.dedicatedIngress) {
    const isolated = ctx.isolate('webServer')
    try {
      await isolated.plugin(WebServer, { host: '127.0.0.1', port: store.current.deploymentPort }).await()
      // A child injection resolves the isolated service; the parent fiber's
      // original injection correctly remains bound to Desktop's listener.
      await isolated.plugin({ name: 'telebot-feishu-ingress', inject,
        apply: (scope: Context) => configure(scope, store, directory, credentials, true) }).await()
    } catch (error) { await store.close(); throw error }
    return
  }
  await configure(ctx, store, directory, credentials, false)
}
async function configure(ctx: Context, store: Configuration, directory: string, credentials: Credentials, dedicatedIngress: boolean) {
  let runtime: Fiber | undefined, busy = () => false, hold = () => () => {}, start = () => {}, active = false, stopping = false, failure = ''
  let business: Awaited<ReturnType<typeof startBusiness>> | undefined
  let initializing = true, finishInitialization!: () => void
  let queue: Promise<unknown> = new Promise<void>(resolve => { finishInitialization = resolve })
  async function activate(config: Config) {
    validateCallbacks(config)
    if (runtime) { const old = runtime; runtime = undefined; active = false; await old.dispose() }
    if (stopping) throw new Error('configuration_stopping')
    const candidate = ctx.plugin({ name: 'telebot-feishu-runtime', inject: ['telebotCore','webServer','credentials','tools'], async apply(scope: Context) {
      const delivery = new FeishuApi({ appId: config.appId, secret: () => credentials.read(config.appSecretRef), origin: apiOrigin(config.apiBaseUrl), timeoutMs: config.apiTimeoutMs })
      business = undefined
      if (config.businessEnabled) business = await startBusiness(scope, { ...config.business, adminUserIds: config.adminUserIds, apiBaseUrl: config.apiBaseUrl,
        dataDirectory: config.business.dataDirectory || path.join(config.dataDirectory || path.join(resolveDshHome(), 'plugins', 'telebot-feishu', config.appId), 'business') }, delivery)
      const handle = await startChannel(scope, config, delivery, true); busy = handle.busy; hold = handle.hold; start = handle.start
    } })
    try { await candidate.await(); runtime = candidate; failure = '' }
    catch { await candidate.dispose().catch(() => {}); busy = () => false; throw new Error('runtime_configuration_rejected') }
  }
  async function commit(body: any) {
    if (stopping) throw new Error('configuration_stopping')
    if (body.revision !== store.revision) throw new Error('configuration_revision_conflict')
    if (body.operation === 'discover-bitable') {
      const config = store.current
      const app = new FeishuApi({ appId: config.appId, secret: () => credentials.read(config.appSecretRef), origin: apiOrigin(config.apiBaseUrl), timeoutMs: config.apiTimeoutMs })
      return discoverBase(body.baseUrl, new FeishuHttp(config.apiBaseUrl, async () => (await app.tenantGrant()).token, config.apiTimeoutMs))
    }
    if (body.operation) throw new Error('unsupported_setup_operation')
    if (active && busy()) throw new Error('configuration_busy')
    const value = body.config, secrets = body.secrets ?? {}
    const { candidate, pending } = await store.prepare(value, secrets), previous = store.current, wasActive = active
    const releasePrevious = active ? hold() : () => {}
    if (active && busy()) { releasePrevious(); throw new Error('configuration_busy') }
    try {
      if (business && candidate.business.resourcesJson !== previous.business.resourcesJson) business.invalidatePending()
      for (const row of pending) await credentials.write(row.ref, row.value)
      await activate(candidate)
      await store.save(candidate)
      start(); active = true
      return { saved: true, active, revision: store.revision }
    } catch (error) {
      if (runtime) { await runtime.dispose().catch(() => {}); runtime = undefined; active = false }
      if (wasActive && !stopping) await activate(previous).then(() => { start(); active = true }).catch(() => { failure = 'runtime_configuration_rejected' })
      for (const row of pending) await credentials.remove(row.ref).catch(() => {})
      throw error
    } finally { releasePrevious() }
  }
  const unregisterBases = ctx.telebotCore.registerFeature('bases', async request => {
    const task = queue.then(async () => {
      const config = store.current
      if (!config.adminUserIds.includes(request.actor.userId)) throw new Error('actor_not_authorized')
      if (!config.businessEnabled || !business) return { status: 'failed' as const, text: '飞书业务未启用。' }
      const resources = JSON.parse(config.business.resourcesJson) as Resource[], links = JSON.parse(config.business.baseLinksJson) as BaseLink[]
      const [action, ...parts] = request.text.trim().split(/\s+/), value = parts.join(' ')
      if (action === 'list' || !action) return { status: 'completed' as const, text: JSON.stringify(linkedBases(resources, links)) }
      if (!['add', 'remove'].includes(action)) return { status: 'failed' as const, text: '/bases add|remove <多维表格链接>；/bases list。移除仅解除接入，不删除飞书数据。' }
      const link = baseLink(value)
      let next = resources.filter(row => action !== 'remove' || row.kind !== 'bitable' || row.id !== link.baseId)
      let nextLinks = links.filter(row => row.baseId !== link.baseId)
      if (action === 'add') {
        const app = new FeishuApi({ appId: config.appId, secret: () => credentials.read(config.appSecretRef), origin: apiOrigin(config.apiBaseUrl), timeoutMs: config.apiTimeoutMs })
        const discovered = await discoverBase(value, new FeishuHttp(config.apiBaseUrl, async () => (await app.tenantGrant()).token, config.apiTimeoutMs))
        for (const table of discovered.tables) {
          const existing=next.find(row=>row.kind==='bitable'&&row.id===link.baseId&&row.tableId===table.table_id)
          if(existing)existing.name=table.name
          else next.push({ alias: 'base-' + table.table_id, name:table.name, kind: 'bitable', id: link.baseId, tableId: table.table_id, identity: 'tenant', tokenRef: 'FEISHU_APP_TENANT', expiresAt: 0, capabilities: ['bitable.read', 'bitable.create', 'bitable.update', 'bitable.delete'] })
        }
        nextLinks.push(link)
      }
      new Resources(next, config.adminUserIds, () => { throw new Error('validation_only') })
      request.signal.throwIfAborted()
      const release = business.holdResources()
      try {
        business.invalidatePending()
        await store.save({ ...config, business: { ...config.business, resourcesJson: JSON.stringify(next), baseLinksJson: JSON.stringify(nextLinks) } })
        business.replaceResources(next)
      } finally { release() }
      return { status: 'completed' as const, text: action === 'add' ? '已接入多维表格：' + link.url + '。写入仍需本人确认。' : '已移除多维表格接入：' + link.url + '。飞书中的表格及记录未删除。' }
    })
    queue = task.catch(() => {})
    return task
  })
  registerConfigurationPage(ctx,{
    name,title:'飞书配置',fields:feishuFields,
    describe:async()=>({...await store.describe(),active,initializing,busy:active?busy():false,failure,
      deployment:{effectivePort:ctx.webServer.port,configuredPort:store.current.deploymentPort,dedicatedIngress,
        restartRequired:ctx.webServer.port!==store.current.deploymentPort||dedicatedIngress!==store.current.dedicatedIngress}}),
    save:body=>{const job=queue.then(()=>commit(body));queue=job.catch(()=>{});return job},
  })
  ctx.effect(() => async () => { stopping = true; unregisterBases(); await queue; try { await runtime?.dispose() } finally { await store.close() } })
  try {
    const missing = await store.missing()
    if (!missing.length) {
      try { await activate(store.current); if (!store.revision) await store.save(store.current); start(); active = true }
      catch { failure = 'runtime_configuration_rejected' }
    }
    if (!active) ctx.logger.warn('Feishu configuration required (%s). Settings → Telebot 插件 → 飞书配置', missing.join(', ') || failure)
  } finally { initializing = false; finishInitialization() }
}
