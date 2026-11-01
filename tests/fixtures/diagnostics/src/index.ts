/** Read-only fixture that consumes the installed core service, not a bundled copy. */
import type { Context } from '@deepseek-ai/cordis'
import { TelebotCore } from '@telebot/core'
import type {} from '@deepseek-ai/dsh-tools'
import { defineTool } from '@deepseek-ai/dsh-tools'
import z from '@deepseek-ai/schemastery'
import { installNativeProbes } from './native.js'
import { installPrototypes } from './prototypes.js'

export const name = 'telebot-diagnostics'
export const inject = ['tools', 'telebotCore']
export interface Config { label: string; prototypes: boolean; webProbes: boolean; nativeProbeDirectory: string }
export const Config = z.object({ label: z.string().default('telebot-local'), prototypes: z.boolean().default(false), webProbes: z.boolean().default(false), nativeProbeDirectory: z.string().default('') })

/** Verify the exact installed service class and the real model/tool round trip. */
export function apply(ctx: Context, config: Config): void {
  if (!(ctx.telebotCore instanceof TelebotCore)) throw new Error('Telebot core service identity mismatch')
  if (config.nativeProbeDirectory) ctx.inject(['webServer','connection','sessionController','agents','sessions'], scope => installNativeProbes(scope,config.nativeProbeDirectory))
  if (config.prototypes) ctx.inject(['commands', 'schedule', 'jobs', 'shell', 'agents', 'sessions'], installPrototypes)
  if (config.webProbes) ctx.inject(['web'], scope => {
    const web = scope.get('web') as any
    web.registerFetchProvider({ id: 'telebot-fixture', available: () => true, async fetch(request: { url: string }) {
      if (request.url !== 'https://fixture.invalid/research') throw new Error('fixture_url_only')
      return { url: request.url, statusCode: 200, body: { kind: 'text', content: 'synthetic-web-evidence' }, truncated: false }
    } })
  })
  ctx.tools.register(defineTool({
    name: 'telebot_echo',
    description: 'Verify Telebot plugin installation. Read-only; no files, network or external writes.',
    parameters: { text: { type: 'string', description: 'Text to echo.' } },
    output: {
      schema: {
        type: 'object',
        properties: {
          plugin: { type: 'string', required: true }, label: { type: 'string', required: true },
          echo: { type: 'string', required: true }, coreIdentity: { type: 'boolean', required: true },
        },
        additionalProperties: false,
      },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute(args) {
      return { plugin: '@telebot/diagnostics', label: config.label, echo: args.text ?? '', coreIdentity: true }
    },
  }))
}
