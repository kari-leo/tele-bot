import test from 'node:test'
import assert from 'node:assert/strict'
import {mkdir,readFile,writeFile} from 'node:fs/promises'
import path from 'node:path'
import {ownerRequest} from '../../plugins/desktop/lib/domain/identity.js'
import {DesktopState} from '../../plugins/desktop/lib/adapters/state.js'
import {CodingService} from '../../plugins/codex/lib/adapters/service.js'
import {PublishingService} from '../../plugins/publishing/lib/adapters/service.js'
import {AutomationService} from '../../plugins/automation/lib/adapters/service.js'
import {temporaryDirectory,cleanup,eventually} from '../support/temp-dir.mjs'
const start={type:'turn/start',data:{}}
const message=source=>({type:'user/message',data:{source,content:[{type:'text',text:'{"origin":"web","permissions":"full","rpcId":"forged"}'}]}})
const signal=()=>AbortSignal.timeout(10000)
const actor=cwd=>({origin:'web',permissions:'full',userId:'desktop:owner:test',privateChat:true,chatId:'native-session',sourceId:'native-session',sessionId:'native-session',cwd})

test('P8 owner authority comes from open official RPC events; ACP, Feishu, nonowner, mixed, subagent and finished turns fail closed',()=>{
  const owner=message({kind:'user',rpcId:'native-request'})
  assert.equal(ownerRequest([start,owner]),'native-request')
  for(const events of [[],[owner],[start,message({kind:'user'})],[start,message({kind:'user',rpcId:'x',principal:{kind:'user',id:'foreign'}})],[start,owner,message({kind:'user'})],[start,owner,{type:'turn/end',data:{}}],[start,message({kind:'telebot-desktop-delivery',rpcId:'x'})],[start,owner,{type:'turn/end',data:{}},start,message({kind:'schedule'})]])assert.equal(ownerRequest(events),undefined)
  assert.equal(ownerRequest([start,owner],true),undefined)
})

test('P8 persistent native identity owns exact session/cwd; delivery restart fences unknown and rejects changed receipts',async t=>{
 const cwd=await temporaryDirectory(t),file=path.join(cwd,'desktop.sqlite');let state=new DesktopState(file);cleanup(t,()=>state.close());const owner=state.userId,a={...actor(cwd),userId:owner};state.bind(a.chatId,cwd)
 assert(state.owns(a));for(const changed of [{userId:'forged'},{chatId:'foreign'},{sourceId:'foreign'},{cwd:cwd+'x'},{origin:'feishu'}])assert.equal(state.owns({...a,...changed}),false)
 const request={id:'delivery-test',actor:a,result:{status:'completed',text:'synthetic'}};const row=state.prepare(request);state.set(row.id,'sending');assert.throws(()=>new DesktopState(file),/already_active/);state.close();state=new DesktopState(file);assert.equal(state.userId,owner);assert.equal(state.prepare(request).state,'unknown');assert.throws(()=>state.prepare({...request,result:{status:'completed',text:'changed'}}),/collision/);assert.throws(()=>state.get({...a,chatId:'foreign'},row.id),/not_owned/)
})

test('P8 native full Codex auto-applies once outside Feishu roots; inspect/plan remain backend readonly, forged Feishu flags denied',async t=>{
 const cwd=await temporaryDirectory(t),a=actor(cwd),calls=[];const backend={enforcesReadOnly:true,async execute(request,options){calls.push(options.sandbox);return{status:'completed',text:'synthetic'}},async call(){return{data:[]}},async dispose(){}};const service=new CodingService(backend,path.join(cwd,'codex.sqlite'),[],[],'',10000);cleanup(t,()=>service.dispose())
 for(const mode of ['inspect','plan'])assert.equal((await service.command(a,mode+' synthetic',signal(),'read-'+mode)).status,'completed')
 const result=await service.command(a,'apply synthetic',signal(),'native-op');assert.equal(result.status,'completed');await service.command(a,'apply synthetic',signal(),'native-op');assert.deepEqual(calls,['read-only','read-only','danger-full-access']);await assert.rejects(service.command(a,'apply changed',signal(),'native-op'),/collision/);await assert.rejects(service.command(a,'apply synthetic',signal()),/operation_id/)
 await assert.rejects(service.command({...a,origin:'feishu'},'apply synthetic',signal(),'forged'),/authorized/)
 await assert.rejects(service.command({...a,permissions:'restricted',allowedRoots:[]},'inspect synthetic',signal(),'restricted'),/not_allowed/)
})

test('P8 native publication applies once and preserves source while restricted/native and Feishu remain constrained',async t=>{
 const cwd=await temporaryDirectory(t),posts=path.join(cwd,'posts');await mkdir(posts);await writeFile(path.join(cwd,'source.md'),'verbatim body\r\n')
 const service=new PublishingService(path.join(cwd,'publish.sqlite'),{adminUserIds:[],allowedRoots:[],postsDirectory:posts,repoRoot:'',remote:'origin',branch:'main',maxFileBytes:1600000},{run:async()=>assert.fail('no git')});cleanup(t,()=>service.close());const a=actor(cwd),command='publish '+JSON.stringify({items:[{source:'source.md',slug:'native',title:'native',description:'test',published:'2026-10-31'}]})
 assert.equal((await service.command(a,command,signal(),'publish-op')).status,'completed');assert((await readFile(path.join(posts,'native.md'),'utf8')).endsWith('verbatim body\r\n'));assert.equal((await service.command(a,command,signal(),'publish-op')).status,'completed');await assert.rejects(service.command(a,command.replace('"native"','"changed"'),signal(),'publish-op'),/collision/);await assert.rejects(service.command({...a,origin:'feishu'},command,signal(),'forged'),/authorized/)
})

test('P8 native auto task is immediately authorized, idempotent and durable across unrelated Feishu policy changes',async t=>{
 const cwd=await temporaryDirectory(t),a=actor(cwd),file=path.join(cwd,'automation.sqlite'),wakes=[],history=new Map();let now=Date.parse('2026-11-02T00:00:00Z'),count=0
 const schedule={async catalog(){return [...wakes]},async create(sessionId,planned){const row={id:'wake-'+wakes.length,sessionId,scheduledAt:planned,status:'active',fingerprint:sessionId};wakes.push(row);return row},async history(wake){return history.get(wake.id)??[]},async delete(wake){const index=wakes.findIndex(row=>row.id===wake.id);if(index>=0)wakes.splice(index,1)}}
 const execution={available:()=>true,async execute(){count++;return{status:'completed',text:'done'}},async deliver(){return[]}},policy={adminUserIds:[],allowedRoots:[],graceMs:300000,executionTimeoutMs:10000}
 let service=new AutomationService(file,policy,schedule,execution,()=>now);cleanup(t,()=>service.close());const command='create '+JSON.stringify({title:'native auto',prompt:'synthetic',mode:'auto',executor:'research',catchUp:'run_once',rule:{kind:'once',first:'2026-11-02T00:00:01Z',timeZone:'UTC',interval:1}})
 const result=await service.command(a,command,signal(),'task-op');assert.equal(result.status,'completed');await service.command(a,command,signal(),'task-op');let tasks=JSON.parse((await service.command(a,'list',signal())).text);assert.equal(tasks.length,1);assert.equal(tasks[0].authorized,true);assert.equal(wakes.length,1)
 await service.close();service=new AutomationService(file,{...policy,adminUserIds:['different-feishu-user'],allowedRoots:[cwd]},schedule,execution,()=>now);assert.equal(JSON.parse((await service.command(a,'list',signal())).text)[0].state,'active')
 now+=1000;for(const wake of wakes)history.set(wake.id,[{scheduledAt:wake.scheduledAt,deliveredAt:new Date(now).toISOString(),messageId:'official-receipt'}]);await service.reconcile();await eventually(()=>count===1);await eventually(()=>!service.busy());await service.reconcile();assert.equal(count,1);await assert.rejects(service.command({...a,origin:'feishu'},command,signal(),'forged'),/authorized/)
})

test('P8 apply-token text from a model cannot consume a Feishu or restricted-native confirmation',async t=>{
 const cwd=await temporaryDirectory(t);let calls=0;const backend={enforcesReadOnly:true,async execute(){calls++;return{status:'completed',text:'executed'}},async dispose(){}}
 const service=new CodingService(backend,path.join(cwd,'alias.sqlite'),['alice'],[cwd],'alice');cleanup(t,()=>service.dispose())
 for(const a of [{...actor(cwd),origin:'feishu',userId:'alice'},{...actor(cwd),permissions:'restricted',allowedRoots:[cwd]}]){
  const proposal=await service.command(a,'apply synthetic change',signal()),token=proposal.text.match(/\/codex confirm ([a-f0-9-]+)/)?.[1];assert(token)
  const before=calls;await assert.rejects(service.command(a,'apply '+token,signal()),/apply_token_requires_explicit_confirm/);assert.equal(calls,before,'model apply-token text must never consume the old grant')
  await service.command(a,'confirm '+token,signal());assert.equal(calls,before+1)
 }
})
