import { renderPluginResult } from '@telebot/core/presentation'
import { desktopOwner, desktopFull, operationIdentity } from '@telebot/core/contracts'
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import { TelebotCore } from '@telebot/core'
import { configuredPlugin } from '@telebot/core/configuration'
import { mkdir, stat } from 'node:fs/promises'
import path from 'node:path'
import { NativeCodex } from './adapters/native.js'
import { CodingService } from './adapters/service.js'
import {RunSkills} from './adapters/skills.js'

export const name = 'telebot-codex'
export const inject = ['telebotCore', 'tools']
export interface Config { skillSourceDirectory:string;skillNames:string[]; configDirectory: string; executionTimeoutMs: number; confirmationTtlMs: number; cli: string; codexHome: string; dataDirectory: string; adminUserIds: string[]; allowedRoots: string[]; historyOwnerUserId: string }
export const Config = z.object({ skillSourceDirectory:z.string().default(''),skillNames:z.array(z.string()).default([]),configDirectory: z.string().default(''), executionTimeoutMs: z.number().min(1000).max(1200000).default(1200000), confirmationTtlMs: z.number().min(1000).max(900000).default(900000), cli: z.string().default(''), codexHome: z.string().default(''), dataDirectory: z.string().default(''), adminUserIds: z.array(z.string()).default([]), allowedRoots: z.array(z.string()).default([]), historyOwnerUserId: z.string().default('') })
async function start(ctx: Context, config: Config, onBusy: (busy: () => boolean) => void) {
  if (!(ctx.telebotCore instanceof TelebotCore)) throw new Error('core_identity_mismatch')
  const directory = config.dataDirectory || path.join(resolveDshHome(), 'plugins', 'telebot-codex')
  if (!path.isAbsolute(directory)) throw new Error('absolute_data_directory_required')
  await mkdir(directory, { recursive: true, mode: 0o700 })
  const backend = new NativeCodex(config.cli, config.codexHome || undefined)
  const probe=await backend.connect(config.allowedRoots[0]);await backend.release(probe)
  const service = new CodingService(backend, path.join(directory, 'coding.sqlite'), config.adminUserIds, config.allowedRoots, config.historyOwnerUserId, config.executionTimeoutMs, config.confirmationTtlMs,new RunSkills(config.skillSourceDirectory,config.skillNames))
  onBusy(() => service.busy())
  const unregister = ctx.telebotCore.registerFeature('codex', request => service.command(request.actor, request.text, request.signal,request.id))
  const unregisterTask = ctx.telebotCore.registerTaskExecutor('codex_apply', request => service.scheduled(request.actor, request.prompt, request.id, request.signal))
  const unregisterConfirm = ctx.telebotCore.registerConfirmations('codex', { pending: actor => service.pending(actor), confirm: (actor, token, signal) => service.confirm(actor,token,signal), cancel: (actor,token) => service.command(actor,'cancel '+token,AbortSignal.timeout(10000)) })
  ctx.tools.register(defineTool({
    name: 'telebot_codex', description: 'Request Codex inspect/plan under enforced read-only, or apply changes. Native owner full access applies immediately; Feishu/restricted native returns a frozen proposal for trusted confirmation. This tool cannot consume confirmation tokens.',
    parameters: { mode: { type: 'string', enum: ['inspect', 'plan', 'apply'], required: true }, prompt: { type: 'string', required: true } },
    output: { schema: { type: 'object', properties: { json: { type: 'string', required: true } }, additionalProperties: false }, render: (_args, value) => [{ type: 'text', text: renderPluginResult(value.json) }] },
    async execute(args, exec) {
      if (!exec.agent) throw new Error('trusted_agent_required')
      const actor = ctx.telebotCore.actor(exec.agent.session.id)
      return { json: JSON.stringify(await service.command(actor, args.mode + ' ' + args.prompt, exec.signal,operationIdentity(ctx.telebotCore.actor(exec.agent.session.id),String(exec.callId)))) }
    },
  }))
  // Deny global alternative Codex entrypoints only for Telebot-owned calls.
  ctx.tools.guard(exec => {
    if (!exec.agent || !['subagent_codex', 'codex'].includes(exec.name)) return undefined
    try { ctx.telebotCore.actor(exec.agent.session.id); return 'Use the authorized Telebot Codex entrypoint' } catch { return undefined }
  })
  ctx.effect(() => async () => { unregister(); unregisterTask(); unregisterConfirm(); await service.dispose() })
}

export async function apply(ctx: Context, bootstrap: Config) {
  let busy=()=>false
  await configuredPlugin(ctx,{name,title:'Codex 插件配置',bootstrap,fields:[
    {key:'cli',label:'本机 Codex CLI 路径',help:'选择已安装 CLI 的 codex.js 或可执行文件；固定兼容 0.160.0，启用时校验协议，复用既有配置和登录。'},
    {key:'codexHome',label:'Codex 配置目录',help:'留空使用本机现有默认目录，不创建新账户。'},
    {key:'allowedRoots',label:'允许的工作目录',type:'list',help:'每行一个已存在的绝对目录。'},
    {key:'adminUserIds',label:'管理员 ID',type:'list'}, {key:'historyOwnerUserId',label:'原生历史所属管理员 ID'},
    {key:'executionTimeoutMs',label:'执行时间上限（毫秒）',type:'number',min:1000,max:1200000},
    {key:'confirmationTtlMs',label:'确认有效期（毫秒）',type:'number',min:1000,max:900000},
    {key:'skillSourceDirectory',label:'可选 Skill 原目录',help:'选择本机已有技能目录；原目录只读。'}, {key:'skillNames',label:'选用的 Skill 名称',type:'list',help:'仅复制明确选中的技能；副本与去重状态保存在工作区 .telebot-skills 中。留空不复制。'},
    {key:'dataDirectory',label:'业务数据目录',help:'留空使用插件独立数据目录。'},
  ],busy:()=>busy(),missing:async value=>[...(!value.cli?['本机 CLI 路径']:[]),...(!value.adminUserIds.length&&!ctx.telebotCore.hasActorProvider('web')?['管理员']:[]),...(!value.allowedRoots.length&&!ctx.telebotCore.hasActorProvider('web')?['允许工作目录']:[])],
  validate:async value=>{Config(value);await new RunSkills(value.skillSourceDirectory,value.skillNames).snapshot();for(const key of ['cli','codexHome','dataDirectory','skillSourceDirectory'])if(value[key as keyof Config]&&!path.isAbsolute(String(value[key as keyof Config])))throw new Error('absolute_codex_paths_required');if(value.cli&&!(await stat(value.cli)).isFile())throw new Error('codex_cli_file_required');for(const root of value.allowedRoots)if(!path.isAbsolute(root)||!(await stat(root)).isDirectory())throw new Error('existing_absolute_workspace_required');if(![value.executionTimeoutMs,value.confirmationTtlMs].every(Number.isSafeInteger))throw new Error('integer_limits_required')},
  activate:async value=>{const fiber=ctx.plugin({name:'telebot-codex-runtime',apply:(scope:Context)=>start(scope,value,handler=>{busy=handler})});try{await fiber.await()}catch(error){await fiber.dispose();throw error}return async()=>{await fiber.dispose();busy=()=>false}},})
}
