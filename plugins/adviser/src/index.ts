import { renderPluginResult } from '@telebot/core/presentation'
import { desktopOwner, desktopFull } from '@telebot/core/contracts'
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { credentialRef } from '@deepseek-ai/dsh-credentials'
import { TelebotCore } from '@telebot/core'
import { configuredPlugin } from '@telebot/core/configuration'
import { stat } from 'node:fs/promises'
import path from 'node:path'
import { Adviser } from './domain/adviser.js'
import { OpenCliAdviser } from './adapters/opencli.js'

export const name = 'telebot-adviser'
export const inject = ['telebotCore', 'tools', 'credentials']
export interface Config { configDirectory: string; cli: string; adminUserIds: string[]; secretRefs: string[]; timeoutMs: number; cacheEntries: number }
export const Config = z.object({ configDirectory: z.string().default(''), cli: z.string().default(''), adminUserIds: z.array(z.string()).default([]), secretRefs: z.array(z.string()).default([]), timeoutMs: z.number().min(100).max(600000).default(180000), cacheEntries: z.number().min(1).max(64).default(16) })
async function start(ctx: Context, config: Config, onBusy: (busy: () => boolean) => void) {
  if (!(ctx.telebotCore instanceof TelebotCore)) throw new Error('core_identity_mismatch')
  const provider = new OpenCliAdviser(config.cli)
  const secrets = await Promise.all(config.secretRefs.map(async ref => (await ctx.credentials.resolve(credentialRef(ref)))?.value ?? ''))
  const adviser = new Adviser(provider, secrets, config.timeoutMs, config.cacheEntries)
  onBusy(() => adviser.busy())
  const unregister = ctx.telebotCore.registerFeature('adviser', async ({ actor, text, signal }) => {
    if (!desktopOwner(actor) && !config.adminUserIds.includes(actor.userId)) throw new Error('adviser_actor_not_authorized')
    const result = await adviser.ask(actor.sessionId, { question: text, context: '', excerpts: [] }, signal)
    return { status: result.status === 'completed' ? 'completed' : 'failed', text: result.text }
  })
  ctx.tools.register(defineTool({
    name: 'ask_adviser', description: 'Get a stateless second opinion. Unavailable means continue independently; do not claim advice was obtained or repeat internal errors.',
    parameters: { question: { type: 'string', required: true }, context: { type: 'string' }, excerpts: { type: 'array', items: { type: 'string' } } },
    output: { schema: { type: 'object', properties: { json: { type: 'string', required: true } }, additionalProperties: false }, render: (_args, value) => [{ type: 'text', text: renderPluginResult(value.json) }] },
    async execute(args, exec) {
      if (!exec.agent) throw new Error('trusted_agent_required')
      const actor = ctx.telebotCore.actor(exec.agent.session.id)
      if (!desktopOwner(actor) && !config.adminUserIds.includes(actor.userId)) throw new Error('adviser_actor_not_authorized')
      const result = await adviser.ask(actor.sessionId, { question: args.question, context: args.context ?? '', excerpts: args.excerpts ?? [] }, exec.signal)
      return { json: JSON.stringify(result.status === 'completed' ? result : { status: 'unavailable' }) }
    },
  }))
  ctx.effect(() => async () => { unregister(); adviser.clear(); await provider.dispose() })
}

export async function apply(ctx:Context,bootstrap:Config){let busy=()=>false;await configuredPlugin(ctx,{name,title:'顾问插件配置',bootstrap,fields:[
 {key:'cli',label:'OpenCLI 路径',help:'独立可选顾问，不影响本机 Codex。'}, {key:'adminUserIds',label:'管理员 ID',type:'list'},
 {key:'secretRefs',label:'需要脱敏的凭据引用',type:'list',help:'填写官方 credentials 中的引用名称，不填写秘密值。'},
 {key:'timeoutMs',label:'等待上限（毫秒）',type:'number',min:100,max:600000}, {key:'cacheEntries',label:'每会话缓存条数',type:'number',min:1,max:64},
 ],busy:()=>busy(),missing:async value=>[...(!value.cli?['OpenCLI 路径']:[]),...(!value.adminUserIds.length&&!ctx.telebotCore.hasActorProvider('web')?['管理员']:[])],validate:async value=>{Config(value);if(value.cli&&(!path.isAbsolute(value.cli)||!(await stat(value.cli)).isFile()))throw new Error('absolute_adviser_cli_required');if(![value.timeoutMs,value.cacheEntries].every(Number.isSafeInteger))throw new Error('integer_limits_required')},
 activate:async value=>{const fiber=ctx.plugin({name:'telebot-adviser-runtime',apply:(scope:Context)=>start(scope,value,handler=>{busy=handler})});try{await fiber.await()}catch(error){await fiber.dispose();throw error}return async()=>{await fiber.dispose();busy=()=>false}},})}
