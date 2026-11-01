import {desktopFull,desktopOwner} from '@telebot/core/contracts'
import { DatabaseSync } from 'node:sqlite'
import { createHash, randomUUID } from 'node:crypto'
import { realpath, stat } from 'node:fs/promises'
import path from 'node:path'
import type { ConversationResult, TrustedActor } from '@telebot/core/contracts'
import { dueOccurrences, executionDecision, validateTask, type TaskInput } from '../domain/rules.js'
import type { SchedulePort, ExecutionPort, Wake } from '../domain/ports.js'
export interface AutomationPolicy {adminUserIds:string[];allowedRoots:string[];graceMs:number;executionTimeoutMs:number}
type Row=Record<string,any>
type Mutation={action:'create'|'update'|'delete';id:string;version:number;input?:TaskInput}
const binding=(actor:TrustedActor)=>JSON.stringify([actor.userId,actor.chatId,actor.sessionId,actor.cwd])
const digest=(value:unknown)=>createHash('sha256').update(JSON.stringify(value)).digest('hex')
export class AutomationService {
  private readonly db:DatabaseSync
  private readonly owner=randomUUID()
  private chain:Promise<unknown>=Promise.resolve()
  private nativeOperation:{id:string;digest:string}|undefined
  private operations=0
  private worker:Promise<void>|undefined
  private readonly cancellations=new Map<string,AbortController>()
  private readonly stop=new AbortController()
  private closed=false
  constructor(filename:string,private readonly policy:AutomationPolicy,private readonly schedule:SchedulePort,private readonly execution:ExecutionPort,private readonly now:()=>number=Date.now){
    this.db=new DatabaseSync(filename)
    if(![0,1].includes(Number(this.db.prepare('PRAGMA user_version').get()?.user_version))){this.db.close();throw new Error('unsupported_automation_schema')}
    this.db.exec(`PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS writer(id INTEGER PRIMARY KEY,pid INTEGER,owner TEXT);
      CREATE TABLE IF NOT EXISTS metadata(id TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,actor TEXT NOT NULL,input TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,authorized INTEGER NOT NULL DEFAULT 0,consumed INTEGER NOT NULL DEFAULT 0,planned TEXT,arm TEXT);
      CREATE TABLE IF NOT EXISTS arms(session TEXT PRIMARY KEY,task TEXT NOT NULL,version INTEGER NOT NULL,planned TEXT NOT NULL,wake TEXT,state TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,task TEXT NOT NULL,version INTEGER NOT NULL,planned TEXT NOT NULL,state TEXT NOT NULL,actor TEXT NOT NULL,input TEXT NOT NULL,receipt TEXT NOT NULL,expires INTEGER NOT NULL,result TEXT);
      CREATE TABLE IF NOT EXISTS proposals(token TEXT PRIMARY KEY,binding TEXT NOT NULL,payload TEXT NOT NULL,expires INTEGER NOT NULL,state TEXT NOT NULL,result TEXT);
      CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,actor TEXT NOT NULL,result TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT);
      BEGIN IMMEDIATE;`)
    const writer=this.db.prepare('SELECT pid FROM writer WHERE id=1').get() as Row|undefined;let alive=false
    if(writer)try{process.kill(writer.pid,0);alive=true}catch(error){alive=(error as NodeJS.ErrnoException).code!=='ESRCH'}
    if(alive){this.db.exec('ROLLBACK');this.db.close();throw new Error('automation_single_dispatcher_required')}
    this.db.prepare('INSERT OR REPLACE INTO writer VALUES(1,?,?)').run(process.pid,this.owner)
    this.db.exec("UPDATE runs SET state='unknown' WHERE state='running';UPDATE proposals SET state='unknown' WHERE state='applying';")
    const policyHash=digest(policy),prior=this.db.prepare("SELECT value FROM metadata WHERE id='policy'").get() as Row|undefined
    if(prior&&prior.value!==policyHash)this.db.exec("UPDATE tasks SET state='paused',authorized=0 WHERE json_extract(actor,'$.origin')!='web' AND state NOT IN ('deleted','completed');UPDATE runs SET state='cancelled' WHERE json_extract(actor,'$.origin')!='web' AND state IN ('queued','awaiting_auth');UPDATE proposals SET state='cancelled' WHERE state='pending';")
    this.db.prepare("INSERT OR REPLACE INTO metadata VALUES('policy',?)").run(policyHash);this.db.exec('PRAGMA user_version=1;COMMIT;')
  }
  private transaction<T>(work:()=>T){this.db.exec('BEGIN IMMEDIATE');try{const value=work();this.db.exec('COMMIT');return value}catch(error){this.db.exec('ROLLBACK');throw error}}
  private serial<T>(work:()=>Promise<T>){if(this.closed)return Promise.reject(new Error('automation_closed'));this.operations++;const result=this.chain.then(async()=>{try{return await work()}finally{this.nativeOperation=undefined}});this.chain=result.catch(()=>{});return result.finally(()=>{this.operations--;this.startWorker()})}
  busy(){return this.operations>0||Boolean(this.worker)}
  ownsSession(session:string){return Boolean(this.db.prepare('SELECT 1 FROM arms WHERE session=?').get(session))}
  private async authorize(actor:TrustedActor){
    if(!desktopOwner(actor)&&!this.policy.adminUserIds.includes(actor.userId))throw new Error('automation_actor_not_authorized')
    const cwd=await realpath(actor.cwd);if(!(await stat(cwd)).isDirectory())throw new Error('automation_workspace_required')
    if(desktopFull(actor))return
    for(const root of desktopOwner(actor)?actor.allowedRoots??[]:this.policy.allowedRoots){const relative=path.relative(await realpath(root),cwd);if(relative===''||!relative.startsWith('..')&&!path.isAbsolute(relative))return}
    throw new Error('automation_workspace_not_allowed')
  }
  private owned(actor:TrustedActor,id:string):Row{
    const row=this.db.prepare('SELECT * FROM tasks WHERE id=?').get(id) as Row|undefined
    if(!row)throw new Error('task_not_found')
    const creator=JSON.parse(row.actor) as TrustedActor
    if(creator.userId!==actor.userId||creator.chatId!==actor.chatId||creator.origin!==actor.origin||(creator.threadId??'')!==(actor.threadId??''))throw new Error('task_not_owned')
    return row
  }
  private notify(id:string,actor:TrustedActor,result:ConversationResult){this.db.prepare("INSERT OR IGNORE INTO events VALUES(?,?,?,'pending',NULL)").run(id,JSON.stringify(actor),JSON.stringify(result))}
  pending(actor:TrustedActor){
    const changes=(this.db.prepare("SELECT token,payload FROM proposals WHERE binding=? AND state='pending' AND expires>?").all(binding(actor),this.now()) as Row[]).map(row=>({token:row.token,label:JSON.parse(row.payload).grant?'自动执行预授权':'定时任务变更'}))
    const runs=(this.db.prepare("SELECT * FROM runs WHERE state='awaiting_auth' AND expires>?").all(this.now()) as Row[]).filter(row=>{try{this.owned(actor,row.task);return binding(JSON.parse(row.actor))===binding(actor)}catch{return false}}).map(row=>({token:'run:'+row.id,label:'执行本次定时任务 '+row.planned}))
    return[...changes,...runs]
  }
  command(actor:TrustedActor,text:string,signal:AbortSignal,operationId?:string){return this.serial(()=>this.commandImpl(actor,text,signal,operationId))}
  private async commandImpl(actor:TrustedActor,text:string,signal:AbortSignal,operationId?:string):Promise<ConversationResult>{
    await this.authorize(actor);signal.throwIfAborted();const match=/^(\S+)\s*([\s\S]*)$/.exec(text.trim()),action=match?.[1]??'help',value=match?.[2]??''
    if(desktopFull(actor)&&['create','update','delete','batch','authorize'].includes(action)){if(!operationId)throw new Error('desktop_operation_id_required');this.nativeOperation={id:'desktop:'+digest([binding(actor),operationId]),digest:digest([action,value])};const old=this.db.prepare('SELECT payload FROM proposals WHERE token=?').get(this.nativeOperation.id) as Row|undefined;if(old){if(JSON.parse(old.payload).nativeRequestDigest!==this.nativeOperation.digest)throw new Error('desktop_operation_collision');return this.confirm(actor,this.nativeOperation.id,signal)}}
    if(action==='list'){const tasks=(this.db.prepare('SELECT * FROM tasks').all() as Row[]).filter(row=>{try{this.owned(actor,row.id);return true}catch{return false}});return{status:'completed',text:JSON.stringify(tasks.map(row=>({id:row.id,...JSON.parse(row.input),version:row.version,state:row.state,authorized:row.authorized===row.version,next:row.planned,consumed:row.consumed})))}}
    if(action==='get'||action==='history'){const row=this.owned(actor,value),runs=this.db.prepare('SELECT * FROM runs WHERE task=? ORDER BY planned').all(value) as Row[];return{status:'completed',text:JSON.stringify(action==='get'?row:runs.map(run=>({...run,deliveries:(this.db.prepare("SELECT id,state,receipt FROM events WHERE id LIKE ?").all(run.id+':%') as Row[]).map(event=>({...event,...(event.receipt&&this.execution.status?{items:this.execution.status(JSON.parse(run.actor),JSON.parse(event.receipt))}:{})}))})))}}
    if(action==='status'){const row=this.db.prepare('SELECT * FROM proposals WHERE token=? AND binding=?').get(value,binding(actor));if(!row)throw new Error('proposal_not_owned');return{status:'completed',text:JSON.stringify(row)}}
    if(action==='retry-delivery'){const task=this.owned(actor,value);await this.flushDeliveries();for(const event of this.db.prepare("SELECT e.receipt FROM events e JOIN runs r ON substr(e.id,1,length(r.id))=r.id WHERE r.task=? AND e.state='queued'").all(value) as Row[])if(event.receipt)this.execution.retry?.(JSON.parse(task.actor),JSON.parse(event.receipt));return{status:'completed',text:'已恢复待投递或已明确失败的出站；unknown 仍需对账，业务执行没有重跑。'}}
    if(action==='execute'||action==='reject'||action==='confirm'&&value.startsWith('run:')||action==='cancel'&&value.startsWith('run:')){
      const id=value.replace(/^run:/,''),row=this.db.prepare('SELECT * FROM runs WHERE id=?').get(id) as Row|undefined
      if(!row)throw new Error('execution_not_found');const task=this.owned(actor,row.task)
      if(binding(JSON.parse(row.actor))!==binding(actor)||row.expires<=this.now()||task.version!==row.version||task.state==='deleted')throw new Error('execution_authorization_mismatch')
      if(row.state!=='awaiting_auth')return{status:'completed',text:'该执行已消费或已失效，不会重复运行。'}
      this.db.prepare('UPDATE runs SET state=? WHERE id=? AND state=?').run(action==='reject'||action==='cancel'?'cancelled':'queued',id,'awaiting_auth');return{status:'completed',text:action==='reject'||action==='cancel'?'已拒绝本次执行。':'本次执行已授权并入队。'}
    }
    if(action==='cancel'){this.db.prepare("UPDATE proposals SET state='cancelled' WHERE token=? AND binding=? AND state='pending'").run(value,binding(actor));return{status:'completed',text:'任务变更已取消。'}}
    if(action==='confirm')return this.confirm(actor,value,signal)
    if(action==='stop'){
      const run=this.db.prepare('SELECT task FROM runs WHERE id=?').get(value) as Row|undefined;if(!run)throw new Error('execution_not_found');this.owned(actor,run.task)
      this.cancellations.get(value)?.abort();this.db.prepare("UPDATE runs SET state='cancelled' WHERE id=? AND state IN ('queued','awaiting_auth')").run(value)
      return{status:'completed',text:'已请求停止本次任务；停止不会撤销已经发生的外部变更。'}
    }
    if(action==='authorize'){
      const task=this.owned(actor,value),input=JSON.parse(task.input) as TaskInput
      if(task.state==='deleted'||input.mode!=='auto')throw new Error('auto_task_required')
      const proposed=this.proposal(actor,{grant:task.id,version:task.version,scope:{cwd:JSON.parse(task.actor).cwd,prompt:input.prompt,executor:input.executor,rule:input.rule}});return desktopFull(actor)?this.confirm(actor,JSON.parse(proposed.text).token,signal):proposed
    }
    if(['create','update','delete','batch'].includes(action)){
      const source=action==='delete'?{id:value}:JSON.parse(value),items=action==='batch'?source:[{...source,action}],mutations:Mutation[]=[]
      if(!Array.isArray(items)||!items.length||items.length>50)throw new Error('task_batch_limit')
      for(const item of items){if(!['create','update','delete'].includes(item.action))throw new Error('invalid_task_mutation');const old=item.action==='create'?undefined:this.owned(actor,item.id)
        if(old&&this.db.prepare("SELECT 1 FROM runs WHERE task=? AND state='running'").get(old.id))throw new Error('task_execution_busy')
        const input=item.action==='delete'?undefined:(item.input??(item.action==='create'?source:undefined)) as TaskInput
        if(item.action!=='delete'&&!input)throw new Error('task_input_required')
        if(input){validateTask(input);if(input.mode!=='remind'&&!this.execution.available(input.executor))throw new Error('task_executor_unavailable')}
        mutations.push({action:item.action,id:old?.id??randomUUID(),version:old?.version??0,...(input?{input}:{})})
      }
      if(new Set(mutations.map(item=>item.id)).size!==mutations.length)throw new Error('duplicate_task_mutation')
      const proposed=this.proposal(actor,{mutations});return desktopFull(actor)?this.confirm(actor,JSON.parse(proposed.text).token,signal):proposed
    }
    return{status:'completed',text:'/schedule create <任务 JSON>；update {"id":"…","input":{…}}；delete <id>；batch [{"action":"create","input":{…}}]；confirm|cancel <令牌>；authorize <id> 单独预授权 auto；execute|reject <执行ID>；list/get/history/status/retry-delivery。first 必须带时区，weekdays 沿用周一=0。'}
  }
  private proposal(actor:TrustedActor,payload:unknown){const token=this.nativeOperation?.id??randomUUID();if(this.nativeOperation)payload={...(payload as object),nativeRequestDigest:this.nativeOperation.digest};this.db.prepare("INSERT INTO proposals VALUES(?,?,?,?,'pending',NULL)").run(token,binding(actor),JSON.stringify(payload),this.now()+900000);return{status:'completed' as const,text:JSON.stringify({status:'confirmation_required',token,payload,confirm:'/schedule confirm '+token,expiresAt:this.now()+900000})}}
  private async confirm(actor:TrustedActor,token:string,signal:AbortSignal):Promise<ConversationResult>{
    const row=this.db.prepare('SELECT * FROM proposals WHERE token=? AND binding=?').get(token,binding(actor)) as Row|undefined
    if(!row||row.expires<=this.now())throw new Error('task_confirmation_mismatch')
    if(row.state!=='pending')return{status:row.state==='completed'?'completed':'unknown',text:row.result??'提案已消费，不会重复变更。'}
    const payload=JSON.parse(row.payload)
    for(const item of payload.mutations??[]){if(item.action!=='create'&&this.owned(actor,item.id).version!==item.version)throw new Error('task_version_changed');if(this.db.prepare("SELECT 1 FROM runs WHERE task=? AND state='running'").get(item.id))throw new Error('task_execution_busy')}
    this.db.prepare("UPDATE proposals SET state='applying' WHERE token=?").run(token);const completed:string[]=[]
    try{
      if(payload.grant){const task=this.owned(actor,payload.grant);if(task.version!==payload.version||task.state==='deleted')throw new Error('task_version_changed');this.db.prepare("UPDATE tasks SET authorized=version,state='active' WHERE id=?").run(task.id);completed.push(task.id)}
      for(const item of payload.mutations??[] as Mutation[]){
        signal.throwIfAborted()
        if(item.action!=='create'){
          const task=this.owned(actor,item.id)
          this.db.prepare("UPDATE tasks SET version=version+1,state='paused',authorized=0 WHERE id=?").run(item.id)
          this.db.prepare("UPDATE runs SET state='cancelled' WHERE task=? AND state IN ('queued','awaiting_auth')").run(item.id)
          if(task.arm){const arm=this.db.prepare('SELECT wake FROM arms WHERE session=?').get(task.arm) as Row|undefined;if(arm?.wake)await this.schedule.delete(JSON.parse(arm.wake));this.db.prepare("UPDATE arms SET state='cancelled' WHERE session=?").run(task.arm)}
        }
        if(item.action==='delete')this.db.prepare("UPDATE tasks SET state='deleted',planned=NULL,arm=NULL WHERE id=?").run(item.id)
        else{
          const input=item.input as TaskInput;validateTask(input);const state=input.mode==='auto'&&!desktopFull(actor)?'awaiting_authorization':'active',nextVersion=item.version+1
          this.db.prepare('INSERT INTO tasks(id,actor,input,version,state,authorized,consumed,planned,arm) VALUES(?,?,?,?,?,0,0,?,NULL) ON CONFLICT(id) DO UPDATE SET actor=excluded.actor,input=excluded.input,version=excluded.version,state=excluded.state,authorized=0,consumed=0,planned=excluded.planned,arm=NULL').run(item.id,JSON.stringify(actor),JSON.stringify(input),nextVersion,state,new Date(input.rule.first).toISOString());if(desktopFull(actor)&&input.mode==='auto')this.db.prepare('UPDATE tasks SET authorized=version WHERE id=?').run(item.id)
        }
        completed.push(item.id);this.db.prepare('UPDATE proposals SET result=? WHERE token=?').run(JSON.stringify({completed}),token)
      }
      await this.reconcileImpl();const result={status:'completed' as const,text:JSON.stringify({completed,autoExecution:desktopFull(actor)?'原生所有者完整权限：auto 已授权；显式 confirm 模式仍按次确认。':'创建/修改确认不会授权自动执行；auto 还需 /schedule authorize <id> 并确认范围。'})};this.db.prepare("UPDATE proposals SET state='completed',result=? WHERE token=?").run(JSON.stringify(result),token);return result
    }catch{this.db.prepare("UPDATE proposals SET state='unknown' WHERE token=?").run(token);return{status:'unknown',text:'任务变更部分结果未确认，已保留各任务及完成项。请 list/status 核对，不会自动重复整批。'}}
  }
  reconcile(){return this.serial(()=>this.reconcileImpl())}
  private async arm(task:Row,catalog:Wake[]){
    let session=task.arm as string|undefined
    if(!session){session=randomUUID();this.transaction(()=>{this.db.prepare("INSERT INTO arms VALUES(?,?,?,?,NULL,'arming')").run(session!,task.id,task.version,task.planned);this.db.prepare('UPDATE tasks SET arm=? WHERE id=?').run(session!,task.id)})}
    const arm=this.db.prepare('SELECT * FROM arms WHERE session=?').get(session) as Row,existing=catalog.filter(wake=>wake.sessionId===session)
    if(existing.length>1)throw new Error('ambiguous_official_wake')
    if(arm.wake){const saved=JSON.parse(arm.wake) as Wake,current=existing[0];if(!current||current.id!==saved.id||current.fingerprint!==saved.fingerprint)throw new Error('official_wake_changed_or_deleted');return current}
    const wake=existing[0]??await this.schedule.create(session,task.planned,JSON.parse(task.input).title)
    this.db.prepare("UPDATE arms SET wake=?,state='armed' WHERE session=?").run(JSON.stringify(wake),session)
    return wake
  }
  private async reconcileImpl(){
    if(this.stop.signal.aborted)return
    this.db.prepare("UPDATE runs SET state='expired' WHERE state='awaiting_auth' AND expires<=?").run(this.now())
    const catalog=await this.schedule.catalog()
    for(const task of this.db.prepare("SELECT * FROM tasks WHERE state='active' AND planned IS NOT NULL").all() as Row[]){
      try{
        const actor=JSON.parse(task.actor) as TrustedActor,input=JSON.parse(task.input) as TaskInput;await this.authorize(actor)
        if(input.mode==='auto'&&task.authorized!==task.version)throw new Error('task_execution_not_authorized')
        const wake=await this.arm(task,catalog),receipts=await this.schedule.history(wake),receipt=receipts.find(row=>row.scheduledAt===wake.scheduledAt&&row.messageId&&Number.isFinite(Date.parse(row.deliveredAt)))
        if(!receipt){if(wake.status==='inactive')throw new Error('official_wake_receipt_missing');continue}
        const now=this.now();if(Date.parse(receipt.scheduledAt)>now)throw new Error('future_wake_receipt')
        const due=dueOccurrences(input.rule,task.planned,task.consumed,now)
        if(!due.due.length)throw new Error('task_receipt_before_planned')
        const overlap=Boolean(this.db.prepare("SELECT 1 FROM runs WHERE task=? AND state IN ('running','queued')").get(task.id))
        this.transaction(()=>{
          for(const [index,planned]of due.due.entries()){
            const id=digest([task.id,task.version,planned]),state=index!==due.due.length-1||overlap?'skipped':executionDecision(input.mode,input.catchUp,Date.parse(planned),now,this.policy.graceMs)
            const inserted=this.db.prepare('INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?,?,?,?,NULL)').run(id,task.id,task.version,planned,state,task.actor,task.input,JSON.stringify({...receipt,derivedFrom:task.planned}),now+900000)
            if(inserted.changes&&state==='reminded')this.notify(id+':reminder',actor,{status:'completed',text:'定时提醒：'+input.prompt+'\n计划时间：'+planned})
            if(inserted.changes&&state==='queued')this.notify(id+':queued',actor,{status:'completed',text:'定时任务已入队：'+input.title+'\n执行 ID：'+id})
            if(inserted.changes&&state==='awaiting_auth')this.notify(id+':confirmation',actor,{status:'completed',text:'定时任务待本次确认：'+input.title+'\n目录：'+actor.cwd+'\n要求：'+input.prompt+'\n15 分钟内 /schedule execute '+id+'；拒绝 /schedule reject '+id})
          }
          this.db.prepare('UPDATE tasks SET consumed=?,planned=?,arm=NULL,state=? WHERE id=? AND version=?').run(due.consumed,due.next??null,due.next?'active':'completed',task.id,task.version)
          this.db.prepare("UPDATE arms SET state='consumed' WHERE session=?").run(wake.sessionId)
        })
        await this.schedule.delete(wake).catch(()=>{})
      }catch(error){this.db.prepare("UPDATE tasks SET state='paused',authorized=0 WHERE id=?").run(task.id);this.notify(task.id+':paused:'+task.version,JSON.parse(task.actor),{status:'failed',text:'任务已暂停，需要核对官方计时记录、权限或规则：'+task.id+'（'+(error instanceof Error?error.message:'reconcile_failed')+'）。没有自动重建已被外部修改/删除的计时任务。'})}
    }
    // Only arm the next future occurrence after committing the previous receipt.
    const fresh=await this.schedule.catalog()
    for(const task of this.db.prepare("SELECT * FROM tasks WHERE state='active' AND planned IS NOT NULL AND arm IS NULL").all() as Row[])try{await this.arm(task,fresh)}catch{this.db.prepare("UPDATE tasks SET state='paused',authorized=0 WHERE id=?").run(task.id)}
    await this.flushDeliveries()
  }
  private startWorker(){if(this.worker||this.closed||this.stop.signal.aborted)return;this.worker=this.drain().finally(()=>{this.worker=undefined;if(!this.closed&&this.db.prepare("SELECT 1 FROM runs WHERE state='queued' LIMIT 1").get())this.startWorker()});this.worker.catch(()=>{})}
  private async drain(){
    for(;;){if(this.stop.signal.aborted)return;const row=this.db.prepare("SELECT * FROM runs WHERE state='queued' ORDER BY planned,id LIMIT 1").get() as Row|undefined;if(!row)return
      const task=this.db.prepare('SELECT * FROM tasks WHERE id=?').get(row.task) as Row|undefined,input=JSON.parse(row.input) as TaskInput,actor=JSON.parse(row.actor) as TrustedActor
      if(!task||task.version!==row.version||['deleted','paused','awaiting_authorization'].includes(task.state)){this.db.prepare("UPDATE runs SET state='cancelled' WHERE id=?").run(row.id);continue}
      try{await this.authorize(actor);if(!this.execution.available(input.executor))throw new Error('executor_unavailable')}
      catch{this.db.prepare("UPDATE runs SET state='failed',result=? WHERE id=?").run(JSON.stringify({error:'authorization_or_executor_unavailable'}),row.id);continue}
      if(this.db.prepare("UPDATE runs SET state='running' WHERE id=? AND state='queued' AND EXISTS(SELECT 1 FROM tasks WHERE id=runs.task AND version=runs.version AND state NOT IN ('deleted','paused','awaiting_authorization'))").run(row.id).changes!==1)continue
      this.notify(row.id+':started',actor,{status:'completed',text:'定时任务开始：'+input.title+'\n执行 ID：'+row.id});await this.flushDeliveries()
      let result:ConversationResult;const cancellation=new AbortController();this.cancellations.set(row.id,cancellation)
      try{result=await this.execution.execute(input.executor,{id:row.id,taskId:row.task,version:row.version,scheduledAt:row.planned,actor:{...actor,sessionId:randomUUID()},prompt:input.prompt,signal:AbortSignal.any([this.stop.signal,cancellation.signal,AbortSignal.timeout(this.policy.executionTimeoutMs)])})}
      catch{result={status:'unknown',text:'执行结果未确认，没有自动重跑。'}}
      finally{this.cancellations.delete(row.id)}
      this.transaction(()=>{this.db.prepare('UPDATE runs SET state=?,result=? WHERE id=?').run(result.status,JSON.stringify(result),row.id);this.notify(row.id+':result',actor,{...result,text:'定时任务 '+input.title+'：'+result.text})});await this.flushDeliveries()
    }
  }
  private async flushDeliveries(){for(const row of this.db.prepare("SELECT * FROM events WHERE state='pending'").all() as Row[])try{const receipt=await this.execution.deliver(row.id,JSON.parse(row.actor),JSON.parse(row.result));this.db.prepare("UPDATE events SET state='queued',receipt=? WHERE id=?").run(JSON.stringify(receipt??null),row.id)}catch{/* Retry delivery only on a host event or explicit request. */}}
  async close(){this.closed=true;this.stop.abort();await this.chain;await this.worker?.catch(()=>{});this.db.prepare('DELETE FROM writer WHERE owner=?').run(this.owner);this.db.close()}
}
