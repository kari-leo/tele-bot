/** Shared service identity for independently installed Telebot bundles. */
import { Context, Service } from '@deepseek-ai/cordis'
import { configuredPlugin } from './harness/configuration.js'
import { ConfigurationPages } from './harness/configuration-ui.js'
import { hostCapabilities, type CapabilitySnapshot } from './contracts.js'
import { HarnessConversations, feishuTaskGuard } from './harness/conversations.js'
import type { FeatureHandler, FeatureRequest, TrustedActor, ActorProvider } from './contracts.js'
import type { TaskExecutor, TaskExecution, DeliveryRequest, ConfirmationProvider } from './contracts.js'
import type {} from '@deepseek-ai/dsh-user-approval'
import type {} from '@deepseek-ai/dsh-user-questions'
import { Interactions, confirmationIntent, type QuestionSink } from './interactions.js'
import { SessionId } from '@deepseek-ai/dsh-session/types'
import { presentResult } from './presentation.js'
import {randomUUID} from 'node:crypto'
import {interpretConfirmation} from './harness/confirmation-intent.js'

interface IntentCandidate {kind:'business'|'approval';token:string;label:string;sessionId:string;name?:string}

export type { CapabilitySnapshot, HostCapability } from './contracts.js'

declare module '@deepseek-ai/cordis' {
  interface Context { telebotCore: TelebotCore }
}

/** Discover public host services without loading another runtime or owning business data. */
export class TelebotCore extends Service {
  readonly interactions = new Interactions()
  private readonly questionSinks = new Map<string, QuestionSink>()
  private readonly actors = new Map<string, TrustedActor>()
  private readonly actorProviders = new Map<TrustedActor['origin'], ActorProvider>()
  private readonly features = new Map<string, FeatureHandler>()
  private readonly taskExecutors = new Map<string, TaskExecutor>()
  private readonly deliverySinks = new Map<string, (request: DeliveryRequest) => Promise<unknown>>()
  private readonly deliveryInspection = new Map<string, {status(actor:TrustedActor,receipt:unknown):unknown;retry(actor:TrustedActor,receipt:unknown):unknown}>()
  private readonly confirmations = new Map<string, ConfirmationProvider>()
  private readonly inputIntents=new Map<string,{actor:TrustedActor;reply:string;candidates:IntentCandidate[];expires:number}>()
  private readonly deliveryReady = new Set<() => void>()
  private readonly artifacts = new Map<string, NonNullable<import('./contracts.js').ConversationResult['artifacts']>[number][]>()
  constructor(ctx: Context) { super(ctx, 'telebotCore') }

  /** Create a channel-owned adapter; the caller must dispose its Agent handles. */
  conversations(): HarnessConversations { return new HarnessConversations(this.ctx, (actor, run, sink) => this.withActor(actor, run, sink), id => this.artifacts.get(id) ?? [],async(scope,id)=>{
    let actor:TrustedActor;try{actor=this.actor(id)}catch{return}
    if(actor.origin==='feishu')await scope.plugin({name:'telebot-feishu-isolated-tasks',inject:['tools'],apply:(child:Context)=>{
      child.tools.guard(feishuTaskGuard(id,()=>this.has('schedule')))
    }}).await()
  }) }
  recordArtifacts(session: string, artifacts: NonNullable<import('./contracts.js').ConversationResult['artifacts']>) {
    this.actor(session)
    const rows = this.artifacts.get(session) ?? []; rows.push(...artifacts); this.artifacts.set(session, rows)
  }
  takeArtifacts(session: string) { const rows=this.artifacts.get(session)??[];this.artifacts.delete(session);return rows }
  hasActiveActor(origin: TrustedActor['origin']) { return [...this.actors.values()].some(actor=>actor.origin===origin) }

  actor(sessionId: string): TrustedActor {
    const actor = this.actors.get(sessionId) ?? [...this.actorProviders.values()].map(provider => provider.resolve(sessionId)).find(Boolean)
    if (!actor) throw new Error('trusted_channel_actor_required')
    return Object.freeze({ ...actor })
  }
  hasActorProvider(origin: TrustedActor['origin']) { return this.actorProviders.has(origin) }
  registerActorProvider(origin: TrustedActor['origin'], provider: ActorProvider) {
    if (origin === 'feishu' || this.actorProviders.has(origin)) throw new Error('actor_provider_already_owned')
    this.actorProviders.set(origin, provider);this.ctx.emit('telebot/entry-policy-changed')
    return () => { if (this.actorProviders.get(origin) === provider) this.actorProviders.delete(origin);this.ctx.emit('telebot/entry-policy-changed') }
  }
  private validateActor(actor: TrustedActor) {
    if (actor.origin === 'web' && !this.actorProviders.get('web')?.validate(actor)) throw new Error('desktop_actor_not_authorized')
  }
  async withActor<T>(actor: TrustedActor, run: () => Promise<T>, sink?: QuestionSink): Promise<T> {
    this.validateActor(actor)
    if (this.actors.has(actor.sessionId)) throw new Error('actor_context_already_active')
    const immutable = Object.freeze({ ...actor })
    this.actors.set(actor.sessionId, immutable)
    if (sink) this.questionSinks.set(actor.sessionId, sink)
    try { return await run() } finally {
      this.interactions.cancelSession(actor.sessionId); this.questionSinks.delete(actor.sessionId); this.artifacts.delete(actor.sessionId)
      if (this.actors.get(actor.sessionId) === immutable) this.actors.delete(actor.sessionId)
    }
  }
  async question(sessionId: string, text: string, choices: string[], approval: boolean, signal?: AbortSignal) {
    const sink = this.questionSinks.get(sessionId)
    if (!sink) throw new Error('channel_question_sink_unavailable')
    const answer=await this.interactions.ask(this.actor(sessionId), text, choices, approval, sink, signal)
    if(!answer){(this.ctx.get('agents') as Context['agents']|undefined)?.get(SessionId(sessionId))?.cancel({kind:'user'});throw new Error('question_expired_or_cancelled')}
    return answer
  }
  registerFeature(name: string, handler: FeatureHandler): () => void {
    if (this.features.has(name)) throw new Error('feature_already_registered')
    this.features.set(name, handler)
    return () => { if (this.features.get(name) === handler) this.features.delete(name) }
  }
  has(name: string) { return this.features.has(name) }
  private routedConfirmations(actor:TrustedActor){
    const sessions=actor.origin==='feishu'?[...new Set([actor.sessionId,...actor.relatedSessionIds??[]])]:[actor.sessionId]
    return sessions.flatMap(sessionId=>[...this.confirmations].flatMap(([name,provider])=>provider.pending({...actor,sessionId}).map(row=>({...row,name,sessionId}))))
  }
  routeInput(actor:TrustedActor,text:string,semantic=false):import('./contracts.js').HumanInputRoute|undefined{
    this.validateActor(actor)
    const question=this.interactions.routeInput(actor,text)
    if(question){if(question.kind==='answer'&&confirmationIntent(text)&&this.routedConfirmations(actor).length)return{kind:'notice',text:'同时有询问和业务操作等待确认。回答卡片请用“回答1：允许一次”或“回答1：拒绝”；业务操作请用“确认第1项”或“取消第1项”。'};return question}
    const selected=/^(确认|同意|执行|取消|拒绝)(?:执行|授权)?第?([一二三四五六七八九十]|\d+)(?:个|项|项任务)?[。！!\s]*$/.exec(text.trim())
    if(selected){const ordinal=Number(selected[2])||'一二三四五六七八九十'.indexOf(selected[2])+1,rows=this.routedConfirmations(actor),row=rows[ordinal-1];if(row)return{kind:'confirmation',text:(['取消','拒绝'].includes(selected[1])?'cancel':'confirm')+' '+row.name+' '+row.token,sessionId:row.sessionId};return{kind:'notice',text:'待确认列表已变化，请重新查看后选择。'}}
    const intent=confirmationIntent(text)
    if(intent){
      const rows=this.routedConfirmations(actor)
      if(!rows.length)return{kind:'notice',text:'当前没有待确认操作，请先查看操作预览。'}
      if(rows.length>1)return{kind:'notice',text:'有多个待确认操作，请回复“确认第1项”或“取消第1项”等明确选择：\n'+rows.map((row,index)=>(index+1)+'. '+row.label).join('\n')}
      const row=rows[0]
      return{kind:'confirmation',text:intent.decision+' '+row.name+' '+row.token,sessionId:row.sessionId}
    }
    if(semantic&&text.length<=10000){
      const candidates:IntentCandidate[]=[...this.interactions.approvals(actor).map(row=>({...row,kind:'approval' as const})),...this.routedConfirmations(actor).map(row=>({...row,kind:'business' as const}))]
      if(!candidates.length)return
      for(const [id,row]of this.inputIntents)if(row.expires<=Date.now())this.inputIntents.delete(id)
      if(this.inputIntents.size>=100||candidates.length>32)return{kind:'notice',text:'待处理确认较多，请明确选择一个待确认事项。'}
      const ticket=randomUUID()
      this.inputIntents.set(ticket,{actor:Object.freeze({...actor}),reply:text,candidates,expires:Date.now()+120000})
      return{kind:'interpretation',text:ticket,sessionId:actor.sessionId}
    }
  }
  releaseInput(ticket:string){this.inputIntents.delete(ticket)}
  async resolveInput(actor:TrustedActor,ticket:string,spec:import('./contracts.js').ConversationSpec,signal:AbortSignal):Promise<import('./contracts.js').HumanInputRoute>{
    this.validateActor(actor)
    const request=this.inputIntents.get(ticket)
    if(!request||request.expires<=Date.now())return{kind:'notice',text:'这次确认解析已失效，没有执行操作。请重新回复你的意图。'}
    if(!['origin','userId','chatId','sessionId','cwd'].every(key=>request.actor[key as keyof TrustedActor]===actor[key as keyof TrustedActor])||(request.actor.threadId??'')!==(actor.threadId??''))return{kind:'notice',text:'这次确认不属于当前用户或会话。'}
    if(spec.id!==actor.sessionId||spec.cwd!==actor.cwd)return{kind:'notice',text:'这次确认的会话或目录已变化，没有执行操作。'}
    this.inputIntents.delete(ticket)
    let decision:Awaited<ReturnType<typeof interpretConfirmation>>
    try{decision=await interpretConfirmation(this.ctx,spec,request.reply,request.candidates,signal);signal.throwIfAborted()}catch{return{kind:'notice',text:'本次自然语言确认解析暂时未完成，操作尚未执行。可以重新表达你的意图，或使用预览中的备用命令。'}}
    if(decision.decision==='other'||decision.decision==='change')return{kind:'text',text:request.reply}
    if(decision.decision==='ambiguous')return{kind:'notice',text:'尚未执行：请说明你指的是哪一个待确认事项：\n'+request.candidates.map((row,index)=>`${index+1}. ${row.label}`).join('\n')}
    const row=request.candidates[decision.selection!-1]
    const original={...request.actor,sessionId:row.sessionId}
    if(row.kind==='approval')return this.interactions.routeApproval(original,row.token,decision.decision)??{kind:'notice',text:'这项权限询问已失效，没有执行操作。'}
    if(!this.confirmations.get(row.name!)?.pending(original).some(current=>current.token===row.token))return{kind:'notice',text:'这项操作预览已失效或已处理，没有自动确认其他操作。'}
    return{kind:'confirmation',text:decision.decision+' '+row.name+' '+row.token,sessionId:row.sessionId}
  }
  waitingSessions(){return this.interactions.waitingSessions()}
  registerTaskExecutor(name: string, handler: TaskExecutor) {
    if (this.taskExecutors.has(name)) throw new Error('task_executor_already_registered')
    this.taskExecutors.set(name, handler)
    return () => { if (this.taskExecutors.get(name) === handler) this.taskExecutors.delete(name) }
  }
  taskExecutorAvailable(name: string) { return this.taskExecutors.has(name) }
  async executeTask(name: string, request: TaskExecution) {
    this.validateActor(request.actor)
    const executor = this.taskExecutors.get(name)
    if (!executor) throw new Error('task_executor_unavailable')
    return presentResult(await executor(Object.freeze({ ...request, actor: Object.freeze({ ...request.actor }) })))
  }
  registerDelivery(origin: string, handler: (request: DeliveryRequest) => Promise<unknown>,inspection?:{status(actor:TrustedActor,receipt:unknown):unknown;retry(actor:TrustedActor,receipt:unknown):unknown}) {
    if (this.deliverySinks.has(origin)) throw new Error('delivery_sink_already_registered')
    this.deliverySinks.set(origin, handler)
    if(inspection)this.deliveryInspection.set(origin,inspection)
    for (const notify of this.deliveryReady) { try { notify() } catch {} }
    return () => { if (this.deliverySinks.get(origin) === handler) { this.deliverySinks.delete(origin);this.deliveryInspection.delete(origin) } }
  }
  deliveryStatus(actor:TrustedActor,receipt:unknown){return this.deliveryInspection.get(actor.origin)?.status(actor,receipt)??{available:false}}
  retryDelivery(actor:TrustedActor,receipt:unknown){const inspection=this.deliveryInspection.get(actor.origin);if(!inspection)throw new Error('delivery_sink_unavailable');return inspection.retry(actor,receipt)}
  onDeliveryReady(notify: () => void) { this.deliveryReady.add(notify); return () => { this.deliveryReady.delete(notify) } }
  deliver(request: DeliveryRequest) {
    this.validateActor(request.actor)
    const sink = this.deliverySinks.get(request.actor.origin)
    if (!sink) throw new Error('delivery_sink_unavailable')
    return sink(request)
  }
  registerConfirmations(name: string, provider: ConfirmationProvider) {
    if (this.confirmations.has(name)) throw new Error('confirmation_provider_already_registered')
    this.confirmations.set(name, provider)
    return () => { if (this.confirmations.get(name) === provider) this.confirmations.delete(name) }
  }
  async confirmLatest(actor: TrustedActor, cancel: boolean, signal: AbortSignal, selected?:{name:string;token:string}) {
    const all = [...this.confirmations].flatMap(([name, provider]) => provider.pending(actor).map(row => ({ ...row, name, provider }))),rows=selected?all.filter(row=>row.name===selected.name&&row.token===selected.token):all
    if (!rows.length) return { status: 'failed' as const, text: '' }
    if (rows.length !== 1) return { status: 'failed' as const, text: '有多个待确认操作，请回复“确认第1项”或“取消第1项”等明确选择：\n' + rows.map((row,index) => (index+1)+'. '+row.label).join('\n') }
    const row = rows[0]
    return presentResult(await (cancel ? row.provider.cancel(actor, row.token) : row.provider.confirm(actor, row.token, signal)))
  }
  async invoke(name: string, request: FeatureRequest) {
    this.validateActor(request.actor)
    const handler = this.features.get(name)
    if (!handler) return { status: 'failed' as const, text: '当前功能未安装或未启用。' }
    return presentResult(await handler(request))
  }

  capabilities(): CapabilitySnapshot {
    return Object.fromEntries(hostCapabilities.map(key => [key, this.ctx.get(key) !== undefined])) as CapabilitySnapshot
  }
}

export const name = 'telebot-core'

/** Register one service in the owning plugin fiber; Cordis removes it on unload. */
export async function apply(ctx: Context): Promise<void> {
  new ConfigurationPages(ctx)
  const core = new TelebotCore(ctx)
  core.registerFeature('confirm', async request => {const match=/^(confirm|cancel)(?:\s+(\S+)\s+(\S+))?$/.exec(request.text);if(!match)return{status:'failed',text:'请明确确认或取消当前待处理操作。'};return core.confirmLatest(request.actor,match[1]==='cancel',request.signal,match[2]?{name:match[2],token:match[3]}:undefined)})
  core.registerFeature('answer', async ({ actor, text }) => {
    const match = /^(\S+)\s+([\s\S]+)$/.exec(text)
    const accepted = Boolean(match && core.interactions.answer(actor, match[1], match[2]))
    return { status: accepted ? 'completed' : 'failed', text: accepted ? '回答已提交。' : '回答已失效或不属于当前用户/会话。' }
  })
  ctx.on('approval/request', async (request, next) => {
    try { if (core.actor(request.agent.session.id).origin !== 'feishu') return next() } catch { return next() }
    try { return await core.question(request.agent.session.id, `${request.toolName}\n${request.reason ?? ''}`, ['允许一次', '拒绝'], true, request.signal) === '允许一次' ? 'allowed-once' : 'rejected' }
    catch { return 'unavailable' }
  })
  ctx.on('user-questions/request', async (request, next) => {
    if (!request.agent) return next()
    try { if (core.actor(request.agent.session.id).origin !== 'feishu') return next() } catch { return next() }
    const answers = []
    for (const item of request.questions) {
      const choices = item.options?.map(option => option.label) ?? []
      const answer = await core.question(request.agent.session.id, item.question + (item.detail ? '\n' + item.detail : ''), choices, false, request.signal)
      answers.push({ id: item.id, selected: choices.includes(answer) ? [answer] : [], ...(!choices.includes(answer) ? { custom: answer } : {}) })
    }
    return { answers }
  })
  await configuredPlugin(ctx,{name,title:'核心服务与能力状态',bootstrap:{configDirectory:''},fields:[],missing:async()=>[],validate:async()=>{},busy:()=>false,activate:async()=>async()=>{},diagnostics:()=>Object.fromEntries(Object.entries(core.capabilities()).map(([key,value])=>[({agents:'会话执行',commands:'官方命令',tools:'工具服务',shell:'官方 Shell',schedule:'官方定时'} as Record<string,string>)[key]??key,value]))})
}
