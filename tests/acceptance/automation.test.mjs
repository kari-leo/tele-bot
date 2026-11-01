import test from 'node:test'
import assert from 'node:assert/strict'
import path from 'node:path'
import { AutomationService } from '../../plugins/automation/lib/adapters/service.js'
import { nextOccurrence, dueOccurrences, validateTask } from '../../plugins/automation/lib/domain/rules.js'
import {scheduleToolInput} from '../../plugins/automation/lib/domain/tool-input.js'
import { temporaryDirectory, cleanup, eventually } from '../support/temp-dir.mjs'

const first='2026-11-02T01:00:00.000Z'
test('task authorization tools prepare only an exact task identifier and cannot inject confirmation or change grant scope',()=>{
  assert.equal(scheduleToolInput('authorize','{"task_id":"task-1"}'),'task-1')
  assert.equal(scheduleToolInput('delete','{"id":"task-1"}'),'task-1')
  assert.throws(()=>scheduleToolInput('authorize','{"task_id":"task-1","allowAll":true}'),/exact_task_id/)
  assert.throws(()=>scheduleToolInput('authorize','{"task_id":"task-1 confirm other"}'),/exact_task_id/)
})
const input=(overrides={})=>({title:'合成任务',prompt:'Generate a synthetic report.',mode:'auto',executor:'research',catchUp:'skip',rule:{kind:'once',first,timeZone:'Asia/Shanghai',interval:1},...overrides})
async function fixture(t){
  const directory=await temporaryDirectory(t),actor={userId:'alice',chatId:'test',threadId:'topic',sessionId:'source',sourceId:'durable',cwd:directory,privateChat:true,origin:'feishu'},wakes=[],receipts=new Map(),delivered=new Map();let now=Date.parse(first)-1000,count=0,failDelivery=false,failExecution=false
  const schedule={async catalog(){return wakes.map(row=>({...row}))},async create(sessionId,planned){const row={id:'wake-'+wakes.length,sessionId,scheduledAt:planned,status:'active',fingerprint:'immutable-'+sessionId};wakes.push(row);return{...row}},async history(row){return receipts.get(row.id)??[]},async delete(row){const i=wakes.findIndex(item=>item.id===row.id);if(i>=0)wakes.splice(i,1)}}
  const port={available:()=>true,async execute(){count++;if(failExecution)throw new Error('simulated_process_receipt_loss');return{status:'completed',text:'verified result'}},async deliver(id,actor,result){if(failDelivery)throw new Error('offline');assert.equal(actor.userId,'alice');if(delivered.has(id))assert.deepEqual(delivered.get(id),result);delivered.set(id,result);return[id]}}
  const config={adminUserIds:['alice'],allowedRoots:[],graceMs:300000,executionTimeoutMs:1000},file=path.join(directory,'automation.sqlite')
  let service=new AutomationService(file,config,schedule,port,()=>now);cleanup(t,()=>service.close())
  const command=(text,who=actor)=>service.command(who,text,AbortSignal.timeout(5000))
  const create=async value=>{const proposal=JSON.parse((await command('create '+JSON.stringify(value))).text);await command('confirm '+proposal.token);return JSON.parse((await command('list')).text).at(-1).id}
  const grant=async id=>{const p=JSON.parse((await command('authorize '+id)).text);return command('confirm '+p.token)}
  const fire=async(at=Date.parse(first))=>{now=at;for(const row of wakes){row.status='inactive';receipts.set(row.id,[{scheduledAt:row.scheduledAt,deliveredAt:new Date(now).toISOString(),messageId:'structured-'+row.id}])}await service.reconcile()}
  return{actor,wakes,receipts,delivered,file,config,schedule,port,command,create,grant,fire,get service(){return service},get executions(){return count},set now(value){now=value},set failDelivery(value){failDelivery=value},set failExecution(value){failExecution=value},async restart(){await service.close();service=new AutomationService(file,config,schedule,port,()=>now)}}
}
test('P6 timezone rules preserve intervals/count/until, Monday=0 and DST gap/overlap semantics',()=>{
  const daily={kind:'daily',first,timeZone:'Asia/Shanghai',interval:2,count:3};validateTask(input({rule:daily}))
  assert.equal(nextOccurrence(daily,first,1),'2026-11-04T01:00:00.000Z');assert.equal(nextOccurrence(daily,first,3),undefined)
  const weekly={...daily,kind:'weekly',weekdays:[0,2],interval:2,count:undefined}
  assert.equal(nextOccurrence(weekly,first,1),'2026-11-04T01:00:00.000Z');assert.equal(nextOccurrence(weekly,'2026-11-04T01:00:00.000Z',2),'2026-11-16T01:00:00.000Z')
  assert.equal(nextOccurrence({...daily,until:first},first,1),undefined)
  assert.equal(nextOccurrence({kind:'daily',first:'2026-03-07T07:30:00Z',timeZone:'America/New_York',interval:1},'2026-03-07T07:30:00Z',1),'2026-03-09T06:30:00.000Z')
  assert.equal(nextOccurrence({kind:'daily',first:'2026-10-31T05:30:00Z',timeZone:'America/New_York',interval:1},'2026-10-31T05:30:00Z',1),'2026-11-01T05:30:00.000Z')
  const due=dueOccurrences({...daily,interval:1},first,0,Date.parse('2026-11-05T01:00:00Z'));assert.equal(due.due.length,3);assert.equal(due.next,undefined)
  assert.throws(()=>validateTask(input({rule:{...weekly,weekdays:[7]}})),/weekdays/)
})
test('P6 authorized Feishu users need no directory whitelist; legacy roots are ignored while identity and restricted native entry checks remain',async t=>{
  const f=await fixture(t),other=await temporaryDirectory(t)
  f.config.allowedRoots=['nonexistent-legacy-root']
  const preview=JSON.parse((await f.command('create '+JSON.stringify(input()),{...f.actor,cwd:other})).text);assert(preview.token)
  await assert.rejects(f.command('list',{...f.actor,userId:'stranger'}),/actor_not_authorized/)
  await assert.rejects(f.command('list',{...f.actor,cwd:path.join(other,'missing')}),/ENOENT/)
  await assert.rejects(f.command('list',{...f.actor,origin:'web',permissions:'restricted',allowedRoots:[]}),/workspace_not_allowed/)
})
test('P6 separate auto preauthorization, trusted receipt, cross-user denial and restart fencing permit exactly one execution',async t=>{
  const f=await fixture(t),id=await f.create(input());assert.equal(f.wakes.length,0)
  const p=JSON.parse((await f.command('authorize '+id)).text)
  await assert.rejects(f.command('confirm '+p.token,{...f.actor,userId:'mallory'}),/authorized/)
  await f.command('confirm '+p.token);assert.equal(f.wakes.length,1)
  assert.throws(()=>new AutomationService(f.file,f.config,f.schedule,f.port),/single_dispatcher/)
  await f.fire();await eventually(()=>f.executions===1);await eventually(()=>!f.service.busy())
  await f.service.reconcile();assert.equal(f.executions,1)
  await f.restart();await f.service.reconcile();assert.equal(f.executions,1)
  assert.equal(JSON.parse((await f.command('history '+id)).text)[0].state,'completed')
})
test('P6 confirm is one occurrence only, user/session bound and expires without execution',async t=>{
  const f=await fixture(t),id=await f.create(input({mode:'confirm'}));await f.fire()
  const [run]=JSON.parse((await f.command('history '+id)).text);assert.equal(run.state,'awaiting_auth');assert.equal(f.executions,0)
  await assert.rejects(f.command('execute '+run.id,{...f.actor,sessionId:'other'}),/authorization_mismatch/)
  await f.command('execute '+run.id);await f.command('execute '+run.id);await eventually(()=>f.executions===1);await eventually(()=>!f.service.busy())
  const later=await f.create(input({mode:'confirm',rule:{kind:'once',first:'2026-11-03T01:00:00Z',timeZone:'UTC',interval:1}}));await f.fire(Date.parse('2026-11-03T01:00:00Z'))
  const [expired]=JSON.parse((await f.command('history '+later)).text);f.now=Date.parse('2026-11-03T01:16:00Z');await f.service.reconcile();await assert.rejects(f.command('execute '+expired.id),/authorization_mismatch/);assert.equal(f.executions,1)
})
test('P6 missed history marks each occurrence but runs only the latest; outgoing failures never rerun business',async t=>{
  const f=await fixture(t),id=await f.create(input({catchUp:'run_once',rule:{kind:'daily',first,timeZone:'Asia/Shanghai',interval:1,count:3}}));await f.grant(id);f.failDelivery=true;await f.fire(Date.parse('2026-11-04T01:01:00Z'));await eventually(()=>f.executions===1);await eventually(()=>!f.service.busy())
  const rows=JSON.parse((await f.command('history '+id)).text);assert.deepEqual(rows.map(row=>row.state),['skipped','skipped','completed']);assert.equal(f.delivered.size,0)
  f.failDelivery=false;await f.command('retry-delivery '+id);assert(f.delivered.size>=3);await f.service.reconcile();assert.equal(f.executions,1)
})
test('P6 external official edit/delete pauses authority; unknown execution and deletion preserve audit',async t=>{
  const f=await fixture(t),id=await f.create(input());await f.grant(id);f.wakes[0].fingerprint='edited-in-web';await f.service.reconcile();assert.equal(JSON.parse((await f.command('get '+id)).text).state,'paused');assert.equal(f.executions,0)
  const second=await f.create(input());await f.grant(second);f.failExecution=true;await f.fire();await eventually(()=>f.executions===1);await eventually(()=>!f.service.busy());await f.restart();await f.service.reconcile();assert.equal(f.executions,1)
  const p=JSON.parse((await f.command('delete '+second)).text);await f.command('confirm '+p.token);assert.equal(JSON.parse((await f.command('history '+second)).text)[0].state,'unknown');assert.equal(JSON.parse((await f.command('get '+second)).text).state,'deleted')
})
