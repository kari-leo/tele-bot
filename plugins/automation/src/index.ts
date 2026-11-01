import { renderPluginResult } from '@telebot/core/presentation'
import { desktopOwner, desktopFull, operationIdentity } from '@telebot/core/contracts'
import type { Context } from '@deepseek-ai/cordis'
import type { AgentHandle } from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-session-persistence'
import type {} from '@deepseek-ai/dsh-agent-default-model'
import type {} from '@deepseek-ai/dsh-fs'
import z from '@deepseek-ai/schemastery'
import { ScheduleId, type ScheduleCatalogEntry } from '@deepseek-ai/dsh-schedule'
import { SessionId } from '@deepseek-ai/dsh-session'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import { configuredPlugin } from '@telebot/core/configuration'
import { TelebotCore } from '@telebot/core'
import {receiptPreset} from '@telebot/core/presets'
import { scheduleToolInput } from './domain/tool-input.js'
import type {} from '@deepseek-ai/dsh-agent-preset-registry'
import { mkdir } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { AutomationService } from './adapters/service.js'
import type { Wake } from './domain/ports.js'
export const name='telebot-automation'
export const inject=['telebotCore','tools','schedule','agents','sessionPersistence']
// allowedRoots is a hidden legacy field, accepted for saved-config compatibility and ignored.
export interface Config {configDirectory:string;dataDirectory:string;adminUserIds:string[];allowedRoots:string[];timeZone:string;graceMs:number;executionTimeoutMs:number;provider:string;model:string}
export const Config=z.object({configDirectory:z.string().default(''),dataDirectory:z.string().default(''),adminUserIds:z.array(z.string()).default([]),allowedRoots:z.array(z.string()).default([]),timeZone:z.string().default('Asia/Shanghai'),graceMs:z.number().min(0).max(3600000).default(300000),executionTimeoutMs:z.number().min(1000).max(1200000).default(1200000),provider:z.string().default(''),model:z.string().default('')})
const wake=(row:ScheduleCatalogEntry):Wake=>{const {sessionId,status,lastDelivery,...record}=row;return{id:row.id,sessionId,scheduledAt:row.scheduledAt,status,fingerprint:JSON.stringify(Object.fromEntries(Object.entries(record).sort(([a],[b])=>a.localeCompare(b))))}}
async function start(ctx:Context,config:Config,setBusy:(busy:()=>boolean)=>void){
  if (!(ctx.telebotCore instanceof TelebotCore)) throw new Error('core_identity_mismatch')
  const directory=config.dataDirectory||path.join(resolveDshHome(),'plugins',name);await mkdir(directory,{recursive:true,mode:0o700})
  const handles=new Map<string,Promise<AgentHandle>>(),conversations=ctx.telebotCore.conversations(),lifetime=new AbortController()
  const presetId='telebot-automation-receipts'
  let service:AutomationService
  const receiptAgent=async(id:string)=>{
    const live=ctx.agents.get(SessionId(id));if(live&&handles.has(id))return live
    if(live)throw new Error('wake_session_owned_elsewhere')
    let handle=handles.get(id)
    if(!handle){handle=(async()=>{const existing=await ctx.sessionPersistence.stat(SessionId(id)),setup=async(scope:Context)=>{await (ctx.get('agentPresets') as Context['agentPresets']).mount(scope,presetId)},selection=(ctx.get('agentDefaultModel') as Context['agentDefaultModel']|undefined)?.currentSelection();const value=existing?await ctx.agents.resume({resumeSessionId:SessionId(id),setup}):await ctx.agents.create({sessionId:SessionId(id),meta:{agentPreset:presetId},agentOptions:{provider:config.provider||selection?.provider,model:config.model||selection?.model},setup});await (ctx.get('sessions') as Context['sessions']).flush(value.agent.session);return value})();handles.set(id,handle);handle.catch(()=>{handles.delete(id)})}
    return(await handle).agent
  }
  ctx.tools.register(defineTool({name:'telebot_task_receipt',description:'Acknowledge wake only; no business execution or external effects.',parameters:{},output:{schema:{type:'object',properties:{received:{type:'boolean',required:true}},additionalProperties:false},render:()=>[{type:'text',text:'Wake received.'}]},async execute(){return{received:true}}}))
  service=new AutomationService(path.join(directory,'automation.sqlite'),config,{
    catalog:async()=> (await ctx.schedule.catalog()).map(wake),
    async create(session,planned,title){await receiptAgent(session);const record=await ctx.schedule.create(SessionId(session),{title,prompt:'Acknowledge the wake only. Do not execute business actions.',...(Date.parse(planned)>Date.now()+1000?{at:planned}:{after_seconds:1})});return wake({...record,sessionId:SessionId(session),status:'active'})},
    async history(row){const history=await ctx.schedule.history({id:ScheduleId(row.id),sessionId:SessionId(row.sessionId),limit:100});if(!('records'in history))throw new Error('wake_history_unavailable');return history.records},
    async delete(row){await ctx.schedule.delete({id:ScheduleId(row.id),sessionId:SessionId(row.sessionId)})},
  },{available:name=>ctx.telebotCore.taskExecutorAvailable(name),execute:(name,input)=>ctx.telebotCore.executeTask(name,input),deliver:(id,actor,result)=>ctx.telebotCore.deliver({id,actor,result}),status:(actor,receipt)=>ctx.telebotCore.deliveryStatus(actor,receipt),retry:(actor,receipt)=>ctx.telebotCore.retryDelivery(actor,receipt)})
  setBusy(()=>service.busy())
  const unregister=ctx.telebotCore.registerFeature('schedule',request=>service.command(request.actor,request.text,AbortSignal.any([request.signal,lifetime.signal]),request.id))
  const confirm=ctx.telebotCore.registerConfirmations('schedule',{pending:actor=>service.pending(actor),confirm:(actor,token,signal)=>service.command(actor,'confirm '+token,signal),cancel:(actor,token)=>service.command(actor,'cancel '+token,lifetime.signal)})
  const disposePreset=await receiptPreset(ctx,presetId,['telebot_task_receipt'])
  const changed=ctx.on('schedule/changed',()=>{service.reconcile().catch(()=>{})})
  const delivery=ctx.telebotCore.onDeliveryReady(()=>{service.reconcile().catch(()=>{})})
  const taskActors=new Set<string>()
  const research=ctx.telebotCore.registerTaskExecutor('research',async request=>{
    const allowed=['web_search','web_fetch',...['telebot_task_read','telebot_task_list','write_report'].filter(tool=>ctx.tools.get(tool))]
    const id=request.actor.sessionId;taskActors.add(id)
    const abort=()=>conversations.cancel(id);request.signal.addEventListener('abort',abort,{once:true})
    try{return await conversations.run({id,cwd:request.actor.cwd,allowCreate:true,provider:config.provider||undefined,model:config.model||undefined,allowedTools:allowed},{id:randomUUID(),text:request.prompt,actor:request.actor})}
    finally{request.signal.removeEventListener('abort',abort);taskActors.delete(id)}
  })
  for(const kind of ['read','list'] as const)ctx.tools.register(defineTool({name:'telebot_task_'+kind,description:kind==='read'?'Read a bounded UTF-8 file only inside this task workspace.':'List direct non-sensitive entries only inside this task workspace.',parameters:{path:{type:'string',required:true},offset:{type:'number'},limit:{type:'number'}},output:{schema:{type:'object',properties:{json:{type:'string',required:true}},additionalProperties:false},render:(_a,v)=>[{type:'text',text:renderPluginResult(v.json)}]},async execute(args,exec){
    if(!exec.agent||!taskActors.has(exec.agent.session.id))throw new Error('restricted_task_actor_required')
    const actor=ctx.telebotCore.actor(exec.agent.session.id),fs=ctx.get('fs') as Context['fs']|undefined;if(!fs)throw new Error('official_filesystem_required')
    const root=await fs.resolve(actor.cwd),target=await fs.resolve(args.path,{cwd:actor.cwd,signal:exec.signal}),relative=path.relative(fs.processPath(root),fs.processPath(target))
    if(!fs.contains(root,target)||/(?:^|[\\/])(?:\.env(?:\..*)?|\.git|\.codex|\.dsh|\.ssh|credentials|\.aws)(?:[\\/]|$)/i.test(relative))throw new Error('task_file_outside_scope')
    if(kind==='list'){const entries=await fs.listDir(target,exec.signal),visible=entries.filter(row=>!/^\./.test(row.name)&&fs.contains(root,row.target));return{json:JSON.stringify({entries:visible.slice(0,1000).map(({name,type,size})=>({name,type,size})),complete:visible.length<=1000})}}
    const info=await fs.stat(target,exec.signal);if(!info||info.type!=='file'||info.size===undefined||info.size>31457280)throw new Error('task_read_size_limit')
    const text=await fs.readText(target,exec.signal),offset=args.offset??0,limit=args.limit??120000
    if(!Number.isSafeInteger(offset)||offset<0||!Number.isSafeInteger(limit)||limit<1||limit>120000)throw new Error('invalid_task_read_window')
    return{json:JSON.stringify({text:text.slice(offset,offset+limit),next:offset+limit<text.length?offset+limit:null,complete:offset+limit>=text.length})}
  }}))
  ctx.tools.register(defineTool({name:'telebot_schedule',description:'Prepare a task create/update/delete/batch proposal or explicit automatic-execution authorization. Tasks execute in separate sessions and do not occupy foreground chat. For authorize/delete use input_json={"task_id":"exact ID from telebot_task_list"}. Authorization only prepares a frozen task-specific grant; the trusted user must confirm it in chat. Native owner full access may apply immediately. Never confirm/execute a Feishu grant through a model tool. Weekdays use Monday=0. Default timezone: '+config.timeZone,parameters:{action:{type:'string',enum:['create','update','delete','batch','authorize'],required:true},input_json:{type:'string',required:true}},output:{schema:{type:'object',properties:{json:{type:'string',required:true}},additionalProperties:false},render:(_a,v)=>[{type:'text',text:renderPluginResult(v.json)}]},async execute(args,exec){if(!exec.agent)throw new Error('trusted_agent_required');return{json:JSON.stringify(await service.command(ctx.telebotCore.actor(exec.agent.session.id),args.action+' '+scheduleToolInput(args.action,args.input_json),exec.signal,operationIdentity(ctx.telebotCore.actor(exec.agent.session.id),String(exec.callId))))}}}))
  ctx.effect(()=>async()=>{unregister();confirm();changed();delivery();research();lifetime.abort();await service.close();await conversations.dispose();await Promise.allSettled([...handles.values()].map(async handle=>(await handle).dispose()));await disposePreset()})
  service.reconcile().catch(()=>{})
}
export async function apply(ctx:Context,bootstrap:Config){let busy=()=>false;await configuredPlugin(ctx,{name,title:'定时自动化配置',bootstrap,fields:[{key:'adminUserIds',label:'管理员 ID',type:'list'},{key:'timeZone',label:'默认时区'},{key:'graceMs',label:'迟到容忍（毫秒）',type:'number',min:0,max:3600000},{key:'executionTimeoutMs',label:'执行上限（毫秒）',type:'number',min:1000,max:1200000},{key:'provider',label:'任务模型 provider',help:'与 model 一起留空使用官方默认选择。任务不调用顾问账户。'},{key:'model',label:'任务模型'},{key:'dataDirectory',label:'授权与执行账本目录'}],busy:()=>busy(),missing:async value=>[...(!value.adminUserIds.length&&!ctx.telebotCore.hasActorProvider('web')?['管理员']:[])],validate:async value=>{Config(value);new Intl.DateTimeFormat('en',{timeZone:value.timeZone});if(Boolean(value.provider)!==Boolean(value.model)||![value.graceMs,value.executionTimeoutMs].every(Number.isSafeInteger))throw new Error('invalid_automation_configuration');if(value.dataDirectory&&!path.isAbsolute(value.dataDirectory))throw new Error('absolute_data_directory_required')},activate:async value=>{const fiber=ctx.plugin({name:'telebot-automation-runtime',apply:(scope:Context)=>start(scope,value,handler=>{busy=handler})});try{await fiber.await()}catch(error){await fiber.dispose();throw error}return async()=>{await fiber.dispose();busy=()=>false}}})}
