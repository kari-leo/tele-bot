import {desktopFull, desktopOwner} from '@telebot/core/contracts'
import { DatabaseSync } from 'node:sqlite'
import { createHash, randomUUID } from 'node:crypto'
import { readFile, realpath, stat, open, mkdir, rename, copyFile } from 'node:fs/promises'
import path from 'node:path'
import type { TrustedActor, ConversationResult } from '@telebot/core/contracts'
import { publicationContent, safeRelative, type Publication } from '../domain/policy.js'

export interface GitPort { run(args: string[], cwd: string, signal: AbortSignal, fullAccess?:boolean): Promise<string> }
export interface PublishingConfig { adminUserIds: string[]; allowedRoots: string[]; postsDirectory: string; repoRoot: string; remote: string; branch: string; maxFileBytes: number }
type FrozenFile={path:string;sha256:string;bytes?:string;source?:string;sourceHash?:string}
type Plan={kind:'publish'|'git'|'files';files:FrozenFile[];message?:string;head?:string;branch?:string;remoteUrl?:string;mutation?:{action:'replace'|'move'|'quarantine';destination:string;content?:string}}
const hash=(bytes:Uint8Array|string)=>createHash('sha256').update(bytes).digest('hex')
const binding=(actor:TrustedActor)=>JSON.stringify([actor.userId,actor.chatId,actor.sessionId,actor.cwd])
export class PublishingService {
  private readonly db:DatabaseSync
  private running=false
  private chain:Promise<unknown>=Promise.resolve()
  private jobs=0
  private actor:TrustedActor|undefined
  private readonly owner=randomUUID()
  constructor(filename:string,private readonly config:PublishingConfig,private readonly git:GitPort) {
    this.db=new DatabaseSync(filename)
    if(![0,1].includes(Number(this.db.prepare('PRAGMA user_version').get()?.user_version))){this.db.close();throw new Error('unsupported_publishing_schema')}
    this.db.exec(`PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;CREATE TABLE IF NOT EXISTS operations(token TEXT PRIMARY KEY,binding TEXT NOT NULL,plan TEXT NOT NULL,expires INTEGER NOT NULL,state TEXT NOT NULL,result TEXT);CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);CREATE TABLE IF NOT EXISTS writer(id INTEGER PRIMARY KEY,pid INTEGER,owner TEXT);CREATE TABLE IF NOT EXISTS metadata(id TEXT PRIMARY KEY,value TEXT NOT NULL);BEGIN IMMEDIATE;`)
    const writer=this.db.prepare('SELECT pid FROM writer WHERE id=1').get() as any;let alive=false
    if(writer)try{process.kill(writer.pid,0);alive=true}catch(error){alive=(error as NodeJS.ErrnoException).code!=='ESRCH'}
    if(alive){this.db.exec('ROLLBACK');this.db.close();throw new Error('publishing_database_already_active')}
    this.db.prepare('INSERT OR REPLACE INTO writer VALUES(1,?,?)').run(process.pid,this.owner)
    const policy=hash(JSON.stringify(config)),prior=this.db.prepare("SELECT value FROM metadata WHERE id='policy'").get() as any
    if(prior&&prior.value!==policy)this.db.exec("UPDATE operations SET state='cancelled' WHERE state='pending';")
    this.db.prepare("INSERT OR REPLACE INTO metadata VALUES('policy',?)").run(policy)
    this.db.exec('PRAGMA user_version=1;')
    this.db.exec("UPDATE operations SET state='unknown' WHERE state='running';COMMIT;")
  }
  busy(){return this.jobs>0}
  private serial<T>(work:()=>Promise<T>):Promise<T>{this.jobs++;const job=this.chain.then(async()=>{try{return await work()}finally{this.actor=undefined}});this.chain=job.catch(()=>{});return job.finally(()=>{this.jobs--})}
  private gitRun(args:string[],cwd:string,signal:AbortSignal){return this.git.run(args,cwd,signal,Boolean(this.actor&&desktopFull(this.actor)))}
  private async authorize(actor:TrustedActor) {
    if(!desktopOwner(actor)&&!this.config.adminUserIds.includes(actor.userId))throw new Error('publishing_actor_not_authorized')
    this.actor=actor
    const cwd=await realpath(actor.cwd)
    if(!(await stat(cwd)).isDirectory()||!await this.allowed(cwd))throw new Error('publishing_workspace_not_allowed')
    return cwd
  }
  private async allowed(file:string) {if(this.actor&&desktopFull(this.actor))return true;for(const root of this.actor&&desktopOwner(this.actor)?this.actor.allowedRoots??[]:this.config.allowedRoots){const relative=path.relative(await realpath(root),file);if(relative===''||(!relative.startsWith('..')&&!path.isAbsolute(relative)))return true}return false}
  private async existing(file:string){const actual=await realpath(file);if(!await this.allowed(actual)||(await stat(actual)).size>this.config.maxFileBytes||!(await stat(actual)).isFile())throw new Error('publishing_file_not_allowed');return actual}
  private async destination(root:string,relative:string){safeRelative(relative);const base=await realpath(root),file=path.resolve(base,relative),parent=await realpath(path.dirname(file)),r=path.relative(base,parent);if(r.startsWith('..')||path.isAbsolute(r)||!await this.allowed(parent))throw new Error('publishing_destination_not_allowed');try{await stat(file);throw new Error('publishing_no_overwrite')}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error}return file}
  private artifact(file:string,bytes:Uint8Array){return{path:file,size:bytes.length,sha256:hash(bytes)}}
  report(actor:TrustedActor,input:{filename:string;content:string},id:string){return this.serial(()=>this.reportImpl(actor,input,id))}
  private async reportImpl(actor:TrustedActor,input:{filename:string;content:string},id:string):Promise<ConversationResult>{
    await this.authorize(actor)
    const bytes=new TextEncoder().encode(input.content)
    if(!input.content?.trim()||bytes.length>this.config.maxFileBytes)throw new Error('report_size_or_content_invalid')
    const digest=hash(JSON.stringify([binding(actor),input])),old=this.db.prepare('SELECT * FROM reports WHERE id=?').get(id) as any
    if(old){if(old.digest!==digest)throw new Error('report_request_collision');return JSON.parse(old.result)}
    const file=await this.destination(actor.cwd,input.filename),handle=await open(file,'wx',0o600)
    try{await handle.writeFile(bytes);await handle.sync()}finally{await handle.close()}
    const verified=await readFile(await this.existing(file));if(hash(verified)!==hash(bytes))throw new Error('report_readback_mismatch')
    const result={status:'completed' as const,text:'报告已新建：'+file,artifacts:[this.artifact(file,bytes)]}
    this.db.prepare('INSERT INTO reports VALUES(?,?,?)').run(id,digest,JSON.stringify(result));return result
  }
  pending(actor:TrustedActor){return(this.db.prepare("SELECT token,plan FROM operations WHERE binding=? AND state='pending' AND expires>?").all(binding(actor),Date.now()) as any[]).map(row=>({token:row.token,label:({git:'提交并推送指定文件',publish:'原文发布',files:'本地文件变更'} as Record<string,string>)[JSON.parse(row.plan).kind]}))}
  async propose(actor:TrustedActor,kind:'publish'|'git'|'files',input:any,signal:AbortSignal,operationId?:string){
    const cwd=await this.authorize(actor);let plan:Plan
    if(kind==='publish'){
      if(!this.config.postsDirectory)throw new Error('posts_directory_not_configured')
      if(!Array.isArray(input.items)||!input.items.length||input.items.length>50)throw new Error('publication_batch_required')
      const files:FrozenFile[]=[]
      for(const item of input.items as Publication[]){const source=await this.existing(path.resolve(cwd,safeRelative(item.source))),bytes=await readFile(source),output=publicationContent(item,bytes);if(output.length>this.config.maxFileBytes)throw new Error('publication_size_limit');const file=await this.destination(this.config.postsDirectory,item.slug+'.md');files.push({path:file,sha256:hash(output),bytes:Buffer.from(output).toString('base64'),source,sourceHash:hash(bytes)})}
      if(new Set(files.map(file=>file.path.toLowerCase())).size!==files.length)throw new Error('duplicate_publication_destination')
      plan={kind,files}
    }else if(kind==='files'){
      if(!['replace','move','quarantine'].includes(input.action))throw new Error('invalid_file_mutation')
      const source=await this.existing(path.join(cwd,safeRelative(input.source)))
      if(input.action==='replace'&&(typeof input.content!=='string'||Buffer.byteLength(input.content)>this.config.maxFileBytes))throw new Error('replacement_content_size_limit')
      const destination=input.action==='move'?await this.destination(cwd,input.destination):input.action==='replace'?source:path.join(cwd,'.telebot-quarantine',randomUUID()+'-'+path.basename(source))
      plan={kind,files:[{path:source,sha256:hash(await readFile(source))}],mutation:{action:input.action,destination,...(input.action==='replace'?{content:input.content}:{})}}
    }else{
      if(!this.config.repoRoot||!this.config.remote||!this.config.branch)throw new Error('git_target_not_configured')
      if(!Array.isArray(input.files)||!input.files.length||input.files.length>50||typeof input.message!=='string'||!input.message.trim()||input.message.length>1000)throw new Error('git_files_and_message_required')
      const repo=await realpath(this.config.repoRoot)
      if(!await this.allowed(repo))throw new Error('git_repository_not_allowed')
      const files:FrozenFile[]=[]
      for(const relative of input.files){safeRelative(relative);const file=await this.existing(path.join(repo,relative));if(path.relative(repo,file).startsWith('..'))throw new Error('git_file_outside_repository');files.push({path:file,sha256:hash(await readFile(file))})}
      const head=(await this.gitRun(['rev-parse','HEAD'],repo,signal)).trim(),branch=(await this.gitRun(['branch','--show-current'],repo,signal)).trim(),remoteUrl=hash((await this.gitRun(['remote','get-url',this.config.remote],repo,signal)).trim())
      if(branch!==this.config.branch||!head||!remoteUrl||(await this.gitRun(['diff','--cached','--name-only'],repo,signal)).trim())throw new Error('git_branch_or_staging_conflict')
      plan={kind,files,message:input.message,head,branch,remoteUrl}
    }
    signal.throwIfAborted();const token=desktopFull(actor)?'desktop:'+hash(binding(actor)+(operationId??'')):randomUUID();if(desktopFull(actor)&&!operationId)throw new Error('desktop_operation_id_required');if(desktopFull(actor))(plan as any).nativeRequestDigest=hash(kind+' '+JSON.stringify(input));this.db.prepare('INSERT INTO operations VALUES(?,?,?,?,?,NULL)').run(token,binding(actor),JSON.stringify(plan),Date.now()+900000,'pending')
    return{status:'completed' as const,text:JSON.stringify({status:'confirmation_required',token,action:kind,files:plan.files.map(({path,sha256})=>({path,sha256})),...(plan.mutation?{mutation:{...plan.mutation,...(plan.mutation.content!==undefined?{content:plan.mutation.content.slice(0,4000),contentSha256:hash(plan.mutation.content),previewTruncated:plan.mutation.content.length>4000}:{})}}:{}),...(kind==='git'?{remote:this.config.remote,branch:plan.branch,message:plan.message}:{}),confirm:'/publish confirm '+token})}
  }
  command(actor:TrustedActor,text:string,signal:AbortSignal,operationId?:string){return this.serial(()=>this.commandImpl(actor,text,signal,operationId))}
  private async commandImpl(actor:TrustedActor,text:string,signal:AbortSignal,operationId?:string):Promise<ConversationResult>{
    await this.authorize(actor);const match=/^(\S+)\s*([\s\S]*)$/.exec(text),action=match?.[1],value=match?.[2]??''
    if(action==='status'){const row=this.db.prepare('SELECT state,result FROM operations WHERE token=? AND binding=?').get(value,binding(actor));if(!row)throw new Error('publication_not_owned');return{status:'completed',text:JSON.stringify(row)}}
    if(action==='cancel'){this.db.prepare("UPDATE operations SET state='cancelled' WHERE token=? AND binding=? AND state='pending'").run(value,binding(actor));return{status:'completed',text:'已取消待确认发布。'}}
    if(action==='publish'||action==='git'||action==='files'){if(desktopFull(actor)){if(!operationId)throw new Error('desktop_operation_id_required');const token='desktop:'+hash(binding(actor)+operationId),old=this.db.prepare('SELECT binding,plan FROM operations WHERE token=?').get(token) as any;if(old){if(old.binding!==binding(actor)||JSON.parse(old.plan).nativeRequestDigest!==hash(action+' '+JSON.stringify(JSON.parse(value))))throw new Error('desktop_operation_collision');return this.commandImpl(actor,'confirm '+token,signal)}}const planned=await this.propose(actor,action,JSON.parse(value),signal,operationId);return desktopFull(actor)?this.commandImpl(actor,'confirm '+JSON.parse(planned.text).token,signal):planned}
    if(action!=='confirm')return{status:'completed',text:'请说明要发布的原文文件，或要提交并推送的具体文件及提交说明，我会先给出预览。原文发布与 Git 推送分别确认。也可使用 /publish confirm/cancel/status <令牌> 确认、取消或查看状态。'}
    if(this.running)throw new Error('publishing_busy')
    const row=this.db.prepare('SELECT * FROM operations WHERE token=? AND binding=?').get(value,binding(actor)) as any
    if(!row||row.expires<=Date.now())throw new Error('publication_confirmation_mismatch')
    if(row.state!=='pending')return{status:row.state==='completed'?'completed':'unknown',text:row.result??'请求已消费，不会重复执行。'}
    const plan=JSON.parse(row.plan) as Plan
    for(const file of plan.files){if(file.source&&hash(await readFile(await this.existing(file.source)))!==file.sourceHash)throw new Error('publication_source_changed');if(plan.kind==='publish'){if(await this.destination(this.config.postsDirectory,path.basename(file.path))!==file.path)throw new Error('publication_destination_changed')}else if(hash(await readFile(await this.existing(file.path)))!==file.sha256)throw new Error('git_file_changed')}
    signal.throwIfAborted();this.db.prepare("UPDATE operations SET state='running' WHERE token=? AND state='pending'").run(value);this.running=true
    const completed:string[]=[]
    try{
      if(plan.kind==='publish')for(const file of plan.files){const handle=await open(file.path,'wx',0o600);try{await handle.writeFile(Buffer.from(file.bytes!,'base64'));await handle.sync()}finally{await handle.close()}if(hash(await readFile(file.path))!==file.sha256)throw new Error('publication_readback_mismatch');completed.push(file.path);this.db.prepare('UPDATE operations SET result=? WHERE token=?').run(JSON.stringify({completed}),value)}
      else if(plan.kind==='files'){
        const file=plan.files[0],mutation=plan.mutation!,cwd=await realpath(actor.cwd),quarantine=path.join(cwd,'.telebot-quarantine')
        await mkdir(quarantine,{recursive:true,mode:0o700});if(await realpath(quarantine)!==quarantine)throw new Error('quarantine_directory_redirected')
        const backup=path.join(quarantine,value+'-'+path.basename(file.path));await copyFile(file.path,backup,1)
        this.db.prepare('UPDATE operations SET result=? WHERE token=?').run(JSON.stringify({backup,source:file.path}),value)
        if(mutation.action==='replace'){
          const temporary=path.join(path.dirname(file.path),'.telebot-replace-'+value),handle=await open(temporary,'wx',0o600);try{await handle.writeFile(mutation.content!);await handle.sync()}finally{await handle.close()}
          if(hash(await readFile(file.path))!==file.sha256)throw new Error('file_changed_during_confirmation')
          await rename(temporary,file.path);if(hash(await readFile(file.path))!==hash(mutation.content!))throw new Error('replacement_readback_mismatch');completed.push(file.path,backup)
        }else{
          const relative=path.relative(cwd,mutation.destination);if(await this.destination(cwd,relative)!==mutation.destination)throw new Error('file_destination_changed')
          if(hash(await readFile(file.path))!==file.sha256)throw new Error('file_changed_during_confirmation')
          await rename(file.path,mutation.destination);if(hash(await readFile(mutation.destination))!==file.sha256)throw new Error('move_readback_mismatch');completed.push(mutation.destination,backup)
        }
      }else{
        const repo=await realpath(this.config.repoRoot)
        if((await this.gitRun(['rev-parse','HEAD'],repo,signal)).trim()!==plan.head||(await this.gitRun(['branch','--show-current'],repo,signal)).trim()!==plan.branch||hash((await this.gitRun(['remote','get-url',this.config.remote],repo,signal)).trim())!==plan.remoteUrl||(await this.gitRun(['diff','--cached','--name-only'],repo,signal)).trim())throw new Error('git_target_changed')
        const files=plan.files.map(file=>path.relative(repo,file.path).replaceAll('\\','/'))
        await this.gitRun(['add','--',...files],repo,signal)
        const staged=(await this.gitRun(['diff','--cached','--name-only','-z'],repo,signal)).split('\0').filter(Boolean)
        if(!staged.length||staged.some(file=>!files.includes(file)))throw new Error('git_staged_scope_mismatch')
        await this.gitRun(['-c','core.hooksPath=/dev/null','commit','--only','-m',plan.message!,'--',...files],repo,signal)
        const commit=(await this.gitRun(['rev-parse','HEAD'],repo,signal)).trim();this.db.prepare('UPDATE operations SET result=? WHERE token=?').run(JSON.stringify({commit,committed:true,pushed:false}),value)
        await this.gitRun(['push',this.config.remote,'HEAD:refs/heads/'+plan.branch],repo,signal)
        completed.push(commit)
      }
      const result={status:'completed' as const,text:JSON.stringify({action:plan.kind,completed})};this.db.prepare("UPDATE operations SET state='completed',result=? WHERE token=?").run(JSON.stringify(result),value);return result
    }catch(error){const prior=this.db.prepare('SELECT result FROM operations WHERE token=?').get(value) as any,code=error instanceof Error&&/^[a-z0-9_-]{1,100}$/.test(error.message)?error.message:'publication_receipt_unconfirmed';this.db.prepare("UPDATE operations SET state='unknown',result=? WHERE token=?").run(JSON.stringify({prior:prior.result?JSON.parse(prior.result):null,error:code}),value);return{status:'unknown',text:'发布结果未确认；保留已完成文件/提交回执，不自动重发或重新提交。请先 /publish status '+value}}
    finally{this.running=false}
  }
  async close(){await this.chain;this.db.prepare('DELETE FROM writer WHERE owner=?').run(this.owner);this.db.close()}
}
