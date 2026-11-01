import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-agent'
import type {} from '@deepseek-ai/dsh-client-connection'
import type {} from '@deepseek-ai/dsh-api-session-controller'
import type {} from '@deepseek-ai/dsh-session-persistence'
import type {} from '@deepseek-ai/dsh-attachment'
import type {} from '@deepseek-ai/dsh-user-questions'
import z from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { freezeMessage, MessageId, type ContentBlock } from '@deepseek-ai/dsh-llm'
import { SessionId } from '@deepseek-ai/dsh-session'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import {isLoopbackHost} from '@deepseek-ai/dsh-host-webserver'
import {operationIdentity} from '@telebot/core/contracts'
import { configuredPlugin } from '@telebot/core/configuration'
import type {} from '@telebot/core'
import type { TrustedActor, DeliveryRequest, ConversationResult } from '@telebot/core/contracts'
import { mkdir, stat, readFile, realpath } from 'node:fs/promises'
import { realpathSync, statSync } from 'node:fs'
import { createHash } from 'node:crypto'
import path from 'node:path'
import { ownerRequest } from './domain/identity.js'
import { DesktopState } from './adapters/state.js'

declare module '@deepseek-ai/dsh-llm' { interface MessageSourceMap { 'telebot-desktop-delivery': {kind:'telebot-desktop-delivery'; deliveryId:string} } }
export const name='telebot-desktop'
export const inject=['telebotCore','tools','agents','sessionController','sessions','attachments','connection','userQuestions','webServer']
export interface Config {configDirectory:string;dataDirectory:string;fullAccess:boolean;allowedRoots:string[]}
export const Config=z.object({configDirectory:z.string().default(''),dataDirectory:z.string().default(''),fullAccess:z.boolean().default(true),allowedRoots:z.array(z.string()).default([])})

async function start(ctx:Context,config:Config,setBusy:(handler:()=>boolean)=>void) {
  if(ctx.connection.allowsRemoteAuthorities||!isLoopbackHost(new URL(ctx.webServer.getOrigin()).hostname.replace(/[\[\]]/g,'')))throw new Error('desktop_entry_requires_loopback_connection')
  const directory=config.dataDirectory||path.join(resolveDshHome(),'plugins',name)
  await mkdir(directory,{recursive:true,mode:0o700})
  const state=new DesktopState(path.join(directory,'desktop.sqlite'))
  let stopping=false,deliveries:Promise<unknown>=Promise.resolve()
  const policy=(actor:TrustedActor)=>state.owns(actor)&&actor.permissions===(config.fullAccess?'full':'restricted')&&JSON.stringify(actor.allowedRoots??[])===JSON.stringify(config.fullAccess?[]:config.allowedRoots)
  const resolve=(id:string):TrustedActor|undefined=>{
    if(stopping)return
    const agent=ctx.agents.get(SessionId(id))
    if(!agent||agent.status==='idle')return
    const nativeRequestId=ownerRequest(agent.session.snapshotEvents(),agent.session.header.origin==='subagent');if(!nativeRequestId)return
    const cwd=realpathSync(agent.session.header.cwd??'')
    if(!path.isAbsolute(cwd)||!statSync(cwd).isDirectory())return
    state.bind(id,cwd)
    return {userId:state.userId,sessionId:id,chatId:id,sourceId:id,cwd,privateChat:true,origin:'web',nativeRequestId,permissions:config.fullAccess?'full':'restricted',...(config.fullAccess?{}:{allowedRoots:[...config.allowedRoots]})}
  }
  const unbind=ctx.telebotCore.registerActorProvider('web',{resolve,validate:policy})
  setBusy(()=>ctx.telebotCore.hasActiveActor('web')||ctx.agents.list().some(agent=>agent.status!=='idle'&&Boolean(resolve(agent.session.id))))
  async function attachments(result:ConversationResult):Promise<ContentBlock[]> {
    const blocks:ContentBlock[]=[]
    for(const artifact of result.artifacts??[]) {
      const target=await realpath(artifact.path),info=await stat(target)
      if(!info.isFile()||info.size!==artifact.size||info.size>31457280)throw new Error('desktop_artifact_changed')
      const bytes=await readFile(target)
      if(createHash('sha256').update(bytes).digest('hex')!==artifact.sha256)throw new Error('desktop_artifact_changed')
      blocks.push({type:'file',attachment:await ctx.attachments.saveFile({data:bytes,name:path.basename(target)})})
    }
    return blocks
  }
  function receipt(row:{id:string;message:string;state:string}) {return {deliveryId:row.id,messageId:row.message,state:row.state,kind:'native-session'}}
  const deliver=(request:DeliveryRequest):Promise<unknown>=>{
    const job=deliveries.then(async()=>{
      if(stopping||!policy(request.actor))throw new Error('desktop_delivery_not_authorized')
      const row=state.prepare(request)
      if(row.state==='queued')return receipt(row)
      const restored=await ctx.sessionController.resolveAgent(SessionId(request.actor.chatId))
      if('error'in restored)throw new Error('desktop_delivery_session_unavailable')
      const agent=restored.agent,events=agent.session.snapshotEvents()
      const accepted=events.some(event=>event.type==='user/message'&&event.data.id===row.message||event.type==='agent/inbox/spliced'&&event.data.inserted.some(message=>message.id===row.message))
      if(accepted){state.set(row.id,'queued');return receipt({...row,state:'queued'})}
      if(row.state==='unknown'||row.state==='sending')return receipt({...row,state:'unknown'})
      let blocks:ContentBlock[]
      try{blocks=await attachments(request.result)}catch{state.set(row.id,'failed');throw new Error('desktop_artifact_presentation_failed')}
      state.set(row.id,'sending')
      try{
        agent.followup(freezeMessage({id:MessageId(row.message),role:'user',source:{kind:'telebot-desktop-delivery',deliveryId:row.id},content:[{type:'text',text:'已完成的任务回执（只呈现结果，不执行工具或重新创建任务）：\n'+request.result.text},...blocks]}))
        await ctx.sessions.flush(agent.session);state.set(row.id,'queued')
        return receipt({...row,state:'queued'})
      }catch{state.set(row.id,'unknown');return receipt({...row,state:'unknown'})}
    })
    deliveries=job.catch(()=>{});return job
  }
  const inspect=(actor:TrustedActor,value:any)=>{if(!policy(actor)||typeof value?.deliveryId!=='string')throw new Error('desktop_delivery_not_owned');return receipt(state.get(actor,value.deliveryId))}
  const delivery=ctx.telebotCore.registerDelivery('web',deliver,{status:inspect,retry(actor,value:any){const row=state.get(actor,value?.deliveryId);if(!policy(actor))throw new Error('desktop_delivery_not_owned');if(row.state==='failed'||row.state==='pending'){state.set(row.id,'pending');deliver(JSON.parse(row.request)).catch(()=>{})}return inspect(actor,value)}})

  // A completed background receipt carries no authority to start new work.
  ctx.tools.guard(exec=>{
    if(!exec.agent)return
    const events=exec.agent.session.snapshotEvents(),start=events.findLastIndex(event=>event.type==='turn/start')
    if(events.slice(start+1).some(event=>event.type==='user/message'&&event.data.source.kind==='telebot-desktop-delivery')&&!ownerRequest(events))return 'desktop_delivery_is_not_execution_authority'
  })
  ctx.on('tools/post-execute',async(exec,result,next)=>{
    const decision=await next()
    if(!exec.agent||result.isError||decision.kind!=='accept'||!resolve(exec.agent.session.id)||!['telebot_codex','write_report','publish_source','telebot_desktop'].includes(exec.name))return decision
    ctx.telebotCore.takeArtifacts(exec.agent.session.id)
    let value:ConversationResult
    try{value=JSON.parse((result.value as any).json)}catch{return decision}
    if(!Array.isArray(value.artifacts)||!value.artifacts.length)return decision
    try{return {kind:'accept' as const,content:[...('content'in decision&&decision.content?decision.content:result.content),...await attachments(value)]}}
    catch{return {kind:'accept' as const,content:[...result.content,{type:'text' as const,text:'业务回执已保留；附件呈现失败，勿重复执行业务。请核对原路径/哈希。'}]}}
  })
  ctx.tools.register(defineTool({name:'telebot_desktop',description:'Manage local Codex history/read/resume/stop, publishing status and schedule list/get/history/stop/execute. Uses authenticated native owner only. Desktop defaults to full access; normal business tools apply/publish/schedule need no repeated grant. Explicit inspect/plan remain read-only. No Feishu identity parameters.',parameters:{feature:{type:'string',enum:['codex','publish','schedule','delivery'],required:true},command:{type:'string',required:true}},output:{schema:{type:'object',properties:{json:{type:'string',required:true}},additionalProperties:false},render:(_a,value)=>[{type:'text',text:value.json}]},async execute(args,exec){
    if(!exec.agent)throw new Error('native_agent_required')
    const actor=resolve(exec.agent.session.id);if(!actor)throw new Error('native_owner_request_required')
    if(args.feature==='delivery') {
      const [action,id]=args.command.trim().split(/\s+/),row=state.get(actor,id)
      if(action==='retry'&&(row.state==='failed'||row.state==='pending')){state.set(id,'pending');await deliver(JSON.parse(row.request))}
      return {json:JSON.stringify(receipt(state.get(actor,id)))}
    }
    if(!config.fullAccess&&/^(?:confirm|resume-confirm|execute|authorize)\b/.test(args.command.trim())) {
      const answer=await ctx.userQuestions.ask({agent:exec.agent,signal:exec.signal,questions:[{id:'desktop-operation',question:'执行这次受限桌面操作？',detail:args.feature+' '+args.command,options:[{label:'允许一次'},{label:'拒绝'}]}]})
      if(!answer.answers.some(item=>item.id==='desktop-operation'&&item.selected.includes('允许一次')))throw new Error('desktop_confirmation_rejected')
    }
    return {json:JSON.stringify(await ctx.telebotCore.invoke(args.feature,{id:operationIdentity(actor,String(exec.callId)),actor,text:args.command,signal:exec.signal}))}
  }}))
  ctx.effect(()=>async()=>{stopping=true;unbind();delivery();await deliveries;state.close()})
}

export async function apply(ctx:Context,bootstrap:Config) {
  let busy=()=>false
  await configuredPlugin(ctx,{name,title:'原生桌面对话入口',bootstrap,fields:[{key:'fullAccess',label:'默认完整权限',type:'boolean',help:'仅经官方原生 RPC 认证的宿主所有者可用；无需插件逐次授权，不改变飞书规则或宿主强制策略。inspect/plan 保持只读。'},{key:'allowedRoots',label:'受限模式的允许目录',type:'list',help:'完整权限时不作为写根限制；关闭完整权限后必填。'},{key:'dataDirectory',label:'身份与投递账本目录'}],missing:async()=>[],busy:()=>busy(),validate:async value=>{
    Config(value)
    if(!value.fullAccess&&!value.allowedRoots.length)throw new Error('restricted_desktop_roots_required')
    for(const root of value.allowedRoots)if(!path.isAbsolute(root)||!(await stat(root)).isDirectory())throw new Error('existing_absolute_directory_required')
    if(value.dataDirectory&&!path.isAbsolute(value.dataDirectory))throw new Error('absolute_data_directory_required')
  },activate:async value=>{const fiber=ctx.plugin({name:'telebot-native-entry-runtime',inject,apply:(scope:Context)=>start(scope,value,handler=>{busy=handler})});try{await fiber.await()}catch(error){await fiber.dispose();throw error}return async()=>{await fiber.dispose();busy=()=>false}}})
}
