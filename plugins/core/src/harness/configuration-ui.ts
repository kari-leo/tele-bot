import { Context, Service } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-client-connection'

export interface SettingField {
  key: string; label: string; type?: 'text' | 'list' | 'number' | 'boolean' | 'json' | 'password'
  help?: string; min?: number; max?: number; section?: string; secret?: boolean
}
export interface ConfigurationPage {
  name: string; title: string; fields: SettingField[]
  describe(): Promise<Record<string, any>>
  save(body: any): Promise<unknown>
}
declare module '@deepseek-ai/cordis' { interface Context { telebotConfiguration: ConfigurationPages } }

/** One registry owned by core; the official Connection owns identity, transport and Origin checks. */
export class ConfigurationPages extends Service {
  private readonly pages = new Map<string, ConfigurationPage>()
  constructor(ctx: Context) {
    super(ctx, 'telebotConfiguration')
    ctx.inject(['connection'], scope => {
      scope.effect(() => scope.connection.fetch.register({
        path: '/api/telebot/configuration', methods: ['POST'], requestBody: 'streaming',
        fetch: request => this.handle(request),
      }))
    })
  }
  register(page: ConfigurationPage) {
    if (this.pages.has(page.name)) throw new Error('configuration_page_already_registered')
    this.pages.set(page.name, page)
    return () => { if (this.pages.get(page.name) === page) this.pages.delete(page.name) }
  }
  async handle(request: Request): Promise<Response> {
    const send = (status: number, value: unknown) => Response.json(value, { status, headers: { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } })
    if (['forwarded', 'x-forwarded-for', 'x-forwarded-host', 'x-real-ip', 'cf-connecting-ip'].some(key => request.headers.has(key))) return send(403, { code: 'direct_owner_configuration_required' })
    try {
      if (request.method !== 'POST' || request.headers.get('content-type')?.split(';')[0] !== 'application/json') return send(400, { code: 'configuration_json_required' })
      const reader=request.body?.getReader(),chunks:Uint8Array[]=[];let size=0
      try {
        while(reader){const part=await reader.read();if(part.done)break;size+=part.value.byteLength
          if(size>262144){await reader.cancel();return send(413,{code:'configuration_too_large'})}chunks.push(part.value)}
      } finally {reader?.releaseLock()}
      const data=Buffer.concat(chunks).toString('utf8')
      const body = JSON.parse(data)
      if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('invalid_configuration_request')
      if (body.action === 'list') {
        const pages = await Promise.all([...this.pages.values()].map(async page => {
          const status = await page.describe()
          return { name: page.name, title: page.title, active: status.active, missing: status.missing, busy: status.busy }
        }))
        return send(200, { pages })
      }
      if(typeof body.plugin!=='string')throw new Error('invalid_configuration_request')
      const page = this.pages.get(body.plugin)
      if (!page) return send(404, { code: 'configuration_plugin_not_enabled' })
      if (body.action === 'get') return send(200, { ...await page.describe(), name: page.name, title: page.title, fields: page.fields })
      if (body.action !== 'save' || !body.body || typeof body.body !== 'object' || Array.isArray(body.body)) throw new Error('invalid_configuration_request')
      return send(200, await page.save(body.body))
    } catch (error) {
      const code = error instanceof Error && /^[a-z_]{1,100}$/.test(error.message) ? error.message : 'configuration_request_rejected'
      return send(code === 'configuration_busy' || code === 'configuration_revision_conflict' ? 409 : 400, { code })
    }
  }
}

export function registerConfigurationPage(ctx: Context, page: ConfigurationPage) {
  // Registration follows the caller's fiber so disabled/uninstalled modules leave no stale page.
  ctx.inject(['telebotConfiguration'], scope => { scope.effect(() => scope.telebotConfiguration.register(page)) })
}
