/** Thin official Harness plugin wiring; channel code depends only on local DTOs and ports. */
import type { Context } from '@deepseek-ai/cordis'
import { TelebotCore } from '@telebot/core'
import type {} from '@deepseek-ai/dsh-host-webserver'
import { credentialRef } from '@deepseek-ai/dsh-credentials'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import { chmod, mkdir, realpath, stat } from 'node:fs/promises'
import path from 'node:path'
import type { Config } from '../config.js'
import { FeishuChannel } from '../domain/channel.js'
import { SqliteChannelRepository } from '../adapters/sqlite.js'
import { FeishuApi, apiOrigin } from '../adapters/feishu-api.js'
import { webhookHandler } from '../adapters/webhook.js'
import { Workspaces } from '../adapters/workspaces.js'
import { FileTransfers } from '../adapters/files.js'
import { Reminders } from './reminders.js'

export async function startChannel(ctx: Context, config: Config, sharedDelivery?: FeishuApi, deferStart = false): Promise<{ busy(): boolean; hold(): () => void; start(): void }> {
  if (!(ctx.telebotCore instanceof TelebotCore)) throw new Error('telebot_core_identity_mismatch')
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(config.appId)) throw new Error('feishu_app_id_required')
  if (!/^\/[A-Za-z0-9_/-]+$/.test(config.webhookPath) || config.webhookPath.endsWith('/')) throw new Error('invalid_feishu_webhook_path')
  if (config.adminUserIds.some(id => !/^[A-Za-z0-9_-]{1,128}$/.test(id))) throw new Error('invalid_feishu_admin_ids')
  if (Boolean(config.provider) !== Boolean(config.model)) throw new Error('provider_and_model_must_be_configured_together')
  if (!path.isAbsolute(config.workspace)) throw new Error('absolute_feishu_workspace_required')
  const workspace = await realpath(config.workspace)
  if (!(await stat(workspace)).isDirectory()) throw new Error('feishu_workspace_not_directory')
  if (config.leaseMs < config.pollMs * 3) throw new Error('lease_must_exceed_three_poll_intervals')
  for (const field of ['apiTimeoutMs', 'maxBodyBytes', 'queueLimit', 'maxConcurrentSessions', 'pollMs', 'leaseMs', 'retryMs', 'maxDeliveryAttempts', 'actionTtlMs'] as const) {
    if (!Number.isSafeInteger(config[field])) throw new Error(`invalid_integer_${field}`)
  }
  const readSecret = async (ref: string) => (await ctx.credentials.resolve(credentialRef(ref)))?.value ?? ''
  const verificationToken = config.verificationTokenRef ? await readSecret(config.verificationTokenRef) : ''
  const encryptKey = config.encryptKeyRef ? await readSecret(config.encryptKeyRef) : ''
  if (!verificationToken && !encryptKey) throw new Error('feishu_webhook_source_authentication_required')
  if (!await readSecret(config.appSecretRef)) throw new Error('feishu_app_secret_required')
  const origin = apiOrigin(config.apiBaseUrl)
  const dataDirectory = config.dataDirectory || path.join(resolveDshHome(), 'plugins', 'telebot-feishu', config.appId)
  if (!path.isAbsolute(dataDirectory)) throw new Error('absolute_feishu_data_directory_required')
  await mkdir(dataDirectory, { recursive: true, mode: 0o700 })
  const filename = path.join(dataDirectory, 'channel.sqlite')
  const repository = new SqliteChannelRepository(filename, config.appId, config.leaseMs)
  const agent = ctx.telebotCore.conversations()
  const delivery = sharedDelivery ?? new FeishuApi({
    appId: config.appId, secret: () => readSecret(config.appSecretRef), origin, timeoutMs: config.apiTimeoutMs,
  })
  const files = new FileTransfers(dataDirectory, (messageId, key, type) => delivery.download(messageId, key, type))
  let reminders: Reminders | undefined
  ctx.inject(['schedule', 'agents', 'sessionPersistence', 'tools'], scope => {
    reminders = new Reminders(scope, path.join(dataDirectory, 'reminders.sqlite'), repository,entry=>entry.actor.ids.some(id=>config.adminUserIds.includes(id))&&(!config.allowedChatIds.length||config.allowedChatIds.includes(entry.chatId)))
    scope.effect(() => async () => { await reminders?.dispose(); reminders = undefined })
  })
  const channel = new FeishuChannel({ repository, agent, commands: agent.commands, features: ctx.telebotCore, files,
    reminders: { command: entry => { if (!reminders) throw new Error('official_schedule_required'); return reminders.command(entry) } },
    workspace: new Workspaces([workspace]), delivery },
  { ...config, workspace, preset: config.agentPreset || undefined, provider: config.provider || undefined, model: config.model || undefined },
  () => { ctx.logger.error('telebot-feishu worker stopped after a persistence or lease failure; webhook admission is unavailable') })
  const unregisterDelivery = ctx.telebotCore.registerDelivery('feishu', async request => {
    if (!config.adminUserIds.includes(request.actor.userId) || (config.allowedChatIds.length && !config.allowedChatIds.includes(request.actor.chatId))) throw new Error('delivery_actor_not_authorized')
    const source = repository.source(request.actor), outputs: { msgType: 'post' | 'file'; content: string }[] = []
    for (let offset = 0; offset < request.result.text.length; offset += 6000) outputs.push({ msgType:'post',content:JSON.stringify({zh_cn:{content:[[{tag:'text',text:request.result.text.slice(offset,offset+6000)}]]}}) })
    for (const artifact of request.result.artifacts ?? []) {
      const content = await files.outbound(source, artifact.path)
      if (JSON.parse(content).sha256 !== artifact.sha256) throw new Error('delivery_artifact_changed')
      outputs.push({ msgType:'file',content })
    }
    return repository.enqueueOnce(request.id, request.actor, outputs)
  },{status:(actor,receipt)=>repository.externalStatus(actor,receipt),retry:(actor,receipt)=>repository.retryExternal(actor,receipt)})
  const handler = webhookHandler({ appId: config.appId, secrets: { verificationToken, encryptKey }, maxBodyBytes: config.maxBodyBytes, accept: event => channel.accept(event) })
  let unregister: (() => void) | undefined
  try {
    await chmod(filename, 0o600)
    unregister = ctx.webServer.register({ kind: 'exact', path: config.webhookPath, handler })
    ctx.effect(() => async () => { unregisterDelivery(); unregister?.(); try { await channel.stop(); await reminders?.dispose() } finally { files.close(); repository.release() } })
    const release = deferStart ? channel.holdAdmission() : undefined
    let started = false
    const start = () => { if (!started) { channel.start(); started = true }; release?.() }
    if (!deferStart) start()
    return { busy: () => repository.busy(), hold: () => channel.holdAdmission(), start }
  } catch (error) {
    unregisterDelivery(); unregister?.()
    try { await channel.stop(); await reminders?.dispose() } finally { files.close(); repository.release() }
    throw error
  }
}
