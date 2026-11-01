import { mkdir, readFile, open, rename, unlink, realpath, stat, chmod } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { Config, validateCallbacks } from '../config.js'
import { apiOrigin } from './feishu-api.js'
import { validateResources } from '../business/domain/resources.js'
import { validateBaseLinks } from '../business/domain/base-links.js'

export interface Credentials { read(ref: string): Promise<string>; write(ref: string, value: string): Promise<void>; remove(ref: string): Promise<void> }
const stringify = (value: unknown) => JSON.stringify(value, null, 2) + '\n'
const secretFields = { appSecret: 'appSecretRef', verificationToken: 'verificationTokenRef', encryptKey: 'encryptKeyRef' } as const
function validated(value: any): Config {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('configuration_object_required')
  if (Object.keys(value).some(key => !Object.hasOwn(Config.dict!, key))) throw new Error('unknown_configuration_field')
  if (value.business && Object.keys(value.business).some(key => !Object.hasOwn(Config.dict!.business.dict!, key))) throw new Error('unknown_business_configuration_field')
  const result = Config(value)
  validateResources(JSON.parse(result.business.resourcesJson))
  validateBaseLinks(JSON.parse(result.business.baseLinksJson))
  validateCallbacks(result)
  return result
}
function knownFields(value: any, schema: typeof Config | any) {
  return Object.fromEntries(Object.keys(schema.dict ?? {}).filter(key => Object.hasOwn(value ?? {}, key)).map(key => [key, value[key]]))
}
/** Atomic, versioned, non-secret configuration; credentials are owned by the official provider. */
export class Configuration {
  private lockOwner = randomUUID()
  private closed = false
  private readonly lock: string
  revision = ''
  current: Config
  loadError = ''
  private constructor(readonly directory: string, initial: Config, private readonly credentials: Credentials) { this.current = initial; this.lock = path.join(directory, 'writer.lock') }
  static async open(directory: string, initial: Config, credentials: Credentials): Promise<Configuration> {
    if (!path.isAbsolute(directory)) throw new Error('absolute_configuration_directory_required')
    await mkdir(directory, { recursive: true, mode: 0o700 })
    const selected = knownFields(initial, Config)
    if (initial.business) selected.business = knownFields(initial.business, Config.dict!.business)
    const safeInitial: Config = Config(selected)
    let bootstrapError = ''
    try { validateResources(JSON.parse(safeInitial.business.resourcesJson)); validateBaseLinks(JSON.parse(safeInitial.business.baseLinksJson)) }
    catch { safeInitial.business.resourcesJson = '[]'; safeInitial.business.baseLinksJson = '[]'; bootstrapError = 'bootstrap_resource_configuration_invalid' }
    try { validateCallbacks(safeInitial) } catch { bootstrapError ||= 'bootstrap_callback_configuration_invalid' }
    const store = new Configuration(await realpath(directory), safeInitial, credentials)
    store.loadError = bootstrapError
    try {
      const lock = await open(store.lock, 'wx', 0o600)
      try { await lock.writeFile(stringify({ pid: process.pid, owner: store.lockOwner })); await lock.sync() } finally { await lock.close() }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
      const previous = JSON.parse(await readFile(store.lock, 'utf8'))
      let live = true
      try { process.kill(previous.pid, 0) } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ESRCH') live = false }
      if (live) throw new Error('configuration_directory_already_active')
      await unlink(store.lock)
      return Configuration.open(directory, initial, credentials)
    }
    try {
      const filename = path.join(directory, 'config.json')
      try {
        if ((await stat(filename)).size > 262144) throw new Error('config_file_too_large')
        const saved = JSON.parse(await readFile(filename, 'utf8'))
        if (saved.version !== 1 || typeof saved.revision !== 'string') throw new Error('config_version_not_supported')
        const value = saved.config
        if (Array.isArray(value.business?.resources)) { value.business.resourcesJson = JSON.stringify(value.business.resources); delete value.business.resources }
        if (Array.isArray(value.business?.baseLinks)) { value.business.baseLinksJson = JSON.stringify(value.business.baseLinks); delete value.business.baseLinks }
        store.current = validated({ ...safeInitial, ...value, configDirectory: directory })
        store.revision = saved.revision
        store.loadError = ''
      } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') store.loadError = 'config_file_invalid' }
      for (const obsolete of ['setup.token','endpoint.json']) await unlink(path.join(directory,obsolete)).catch(error=>{if(error.code!=='ENOENT')throw error})
      // An editable, harmless template exists even before the first successful setup.
      await store.template('config.example.json', stringify({ version: 1, revision: 'example', config: { appId: '', adminUserIds: [], workspace: '', appSecretRef: 'FEISHU_APP_SECRET', verificationTokenRef: 'FEISHU_VERIFICATION_TOKEN', businessEnabled: true, business: { resources: [], confirmAll: true } } }))
      await store.atomic('README.txt', 'Configure in Harness Settings > Telebot 插件 > 飞书配置. Edit config.json only while stopped. Secrets are credential references, not values. Uninstall preserves this directory.\n')
      if (!store.revision && !store.loadError) await store.save({ ...safeInitial, configDirectory: store.directory })
      return store
    } catch (error) { await store.close(); throw error }
  }
  async missing(config = this.current) {
    const missing: string[] = []
    if (this.loadError) missing.push(this.loadError)
    if (!config.appId) missing.push('appId')
    if (!config.adminUserIds.length) missing.push('adminUserIds')
    if (!config.workspace) missing.push('workspace')
    if (!await this.credentials.read(config.appSecretRef)) missing.push('appSecret')
    if (!await this.credentials.read(config.verificationTokenRef) && !await this.credentials.read(config.encryptKeyRef)) missing.push('verificationToken_or_encryptKey')
    return missing
  }
  async describe() {
    return { revision: this.revision, config: this.current, missing: await this.missing(), directory: this.directory,
      credentials: Object.fromEntries(await Promise.all(Object.entries(secretFields).map(async ([field, ref]) => [field, Boolean(await this.credentials.read(this.current[ref]))]))) }
  }
  async prepare(value: unknown, secrets: Record<string, unknown> = {}) {
    const candidate = validated({ ...this.current, ...(value as object), configDirectory: this.directory })
    if (!Number.isSafeInteger(candidate.deploymentPort)) throw new Error('deployment_port_integer_required')
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(candidate.appId) || !candidate.adminUserIds.length || candidate.adminUserIds.some(id => !/^[\w-]{1,128}$/.test(id))) throw new Error('valid_app_and_administrators_required')
    if (candidate.allowedChatIds.some(id => !/^[\w-]{1,256}$/.test(id))) throw new Error('valid_chat_ids_required')
    if (!path.isAbsolute(candidate.workspace) || !(await stat(await realpath(candidate.workspace))).isDirectory()) throw new Error('existing_absolute_workspace_required')
    if (!/^\/[A-Za-z0-9_/-]+$/.test(candidate.webhookPath) || candidate.webhookPath.endsWith('/')) throw new Error('invalid_webhook_path')
    if (Boolean(candidate.provider) !== Boolean(candidate.model)) throw new Error('provider_and_model_required_together')
    if (candidate.leaseMs < candidate.pollMs * 3) throw new Error('lease_must_exceed_three_poll_intervals')
    apiOrigin(candidate.apiBaseUrl); apiOrigin(candidate.business.apiBaseUrl)
    const resources = JSON.parse(candidate.business.resourcesJson)
    if (!Array.isArray(resources) || resources.length > 500) throw new Error('resource_array_required')
    const pending: { ref: string; value: string }[] = []
    for (const [field, refField] of Object.entries(secretFields)) {
      const secret = secrets[field]
      if (secret !== undefined && (typeof secret !== 'string' || secret.length > 16384 || secret.includes('\0'))) throw new Error('invalid_credential_input')
      if (typeof secret === 'string' && secret) {
        const ref = 'TELEBOT_FEISHU_' + field.replace(/[A-Z]/g, letter => '_' + letter).toUpperCase() + '_' + randomUUID().replaceAll('-', '').toUpperCase()
        candidate[refField as typeof secretFields[keyof typeof secretFields]] = ref
        pending.push({ ref, value: secret })
      }
    }
    const available = async (ref: string) => pending.find(row => row.ref === ref)?.value ?? await this.credentials.read(ref)
    if (!await available(candidate.appSecretRef) || (!await available(candidate.verificationTokenRef) && !await available(candidate.encryptKeyRef))) throw new Error('application_secret_and_source_authentication_required')
    return { candidate, pending }
  }
  async save(config: Config) {
    if (this.closed) throw new Error('configuration_closed')
    const revision = randomUUID(), persisted = JSON.parse(JSON.stringify(config))
    persisted.business.resources = JSON.parse(persisted.business.resourcesJson); delete persisted.business.resourcesJson
    persisted.business.baseLinks = JSON.parse(persisted.business.baseLinksJson); delete persisted.business.baseLinksJson
    await this.atomic('config.json', stringify({ version: 1, revision, config: persisted }))
    this.current = config; this.revision = revision; this.loadError = ''
  }
  private async atomic(name: string, content: string) {
    const filename = path.join(this.directory, name), temporary = filename + '.' + randomUUID() + '.tmp'
    const handle = await open(temporary, 'wx', 0o600)
    try { await handle.writeFile(content); await handle.sync() } finally { await handle.close() }
    try { await chmod(temporary, 0o600); await rename(temporary, filename) } finally { await unlink(temporary).catch(() => {}) }
  }
  private async template(name: string, content: string) { try { const file = await open(path.join(this.directory, name), 'wx', 0o600); try { await file.writeFile(content) } finally { await file.close() } } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error } }
  async close() { if (this.closed) return; this.closed = true; try { const lock = JSON.parse(await readFile(this.lock, 'utf8')); if (lock.owner === this.lockOwner) await unlink(this.lock) } catch {} }
}
