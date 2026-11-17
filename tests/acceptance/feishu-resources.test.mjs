import test from 'node:test'
import assert from 'node:assert/strict'
import { Resources } from '../../plugins/feishu/lib/business/domain/resources.js'
import { OperationLedger } from '../../plugins/feishu/lib/business/adapters/ledger.js'
import { temporaryDirectory, cleanup } from '../support/temp-dir.mjs'
import path from 'node:path'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { FeishuHttp } from '../../plugins/feishu/lib/business/adapters/http.js'
import { bitableOperation } from '../../plugins/feishu/lib/business/domain/tool-input.js'
import { bitableParameters } from '../../plugins/feishu/lib/business/harness/tool-schema.js'
const row=(alias,tableId,name)=>({alias,tableId,name,kind:'bitable',id:'base-fixture',identity:'tenant',tokenRef:'FEISHU_APP_TENANT',expiresAt:0,capabilities:['bitable.read']})

async function boundary(t,handler){const server=createServer(handler);server.listen(0,'127.0.0.1');await once(server,'listening');cleanup(t,()=>new Promise(done=>{server.closeAllConnections();server.close(done)}));return 'http://127.0.0.1:'+server.address().port}
test('Bitable tool schemas expose record batches and fields to the model',()=>{
  const schema=bitableParameters('update')
  assert.equal(schema.params.properties.records.required,true)
  assert.equal(schema.params.properties.records.items.properties.record_id.required,true)
  assert.equal(schema.params.properties.records.items.properties.fields.additionalProperties,true)
})
test('Bitable tool inputs expose records and bounded queries without guessing JSON or silently changing writes',()=>{
  const args={resource:'todo',params:{records:[{record_id:'r1',fields:{状态:'完成'}}]}}
  assert.deepEqual(bitableOperation('update',args),{resource:'todo',action:'update',params:args.params})
  assert.equal(bitableOperation('query',{resource:'todo'}).params.page_size,20)
  assert.throws(()=>bitableOperation('update',{resource:'todo',params_json:'{"record_id":"r1","fields":{"状态":"完成"}}'}),/records.*record_id/)
  assert.throws(()=>bitableOperation('update',{...args,params_json:'{}'}),/one_payload/)
  assert.throws(()=>bitableOperation('query',{resource:'todo',params:{page_size:10000}}),/page_size/)
})
test('model Bitable queries return one small page and preserve filters, fields and explicit continuation',async()=>{
  const calls=[],resource={...row('todo','table1','Todo'),expiresAt:Date.now()+60000},actor={origin:'feishu',userId:'alice',chatId:'chat'}
  const resources=new Resources([resource],['alice'],()=>({async request(method,route,body){calls.push({method,route,body});return{items:[{record_id:'r1',fields:{name:'match'}}],has_more:calls.length===1,page_token:calls.length===1?'cursor-next':undefined}}}))
  const filter={conjunction:'and',conditions:[{field_name:'name',operator:'contains',value:['match']}]}
  const result=await resources.execute(actor,bitableOperation('query',{resource:'todo',params:{filter,field_names:['name']}}))
  assert.equal(calls.length,1);assert.match(calls[0].route,/page_size=20/);assert.deepEqual(calls[0].body,{filter,field_names:['name']})
  assert.equal(result.complete,false);assert.equal(result.next_page_token,'cursor-next');assert.match(result.instruction,/page_token/)
  await resources.execute(actor,bitableOperation('query',{resource:'todo',params:{page_token:result.next_page_token,page_size:5}}))
  assert.match(calls[1].route,/page_size=5.*page_token=cursor-next/)
})
test('a timed-out pre-write snapshot retries only the read and the ledger writes and verifies once',async t=>{
  let reads=0,writes=0,fields={name:'before'}
  const base=await boundary(t,async(req,res)=>{if(req.method==='GET'&&++reads===1)return;let raw='';for await(const chunk of req)raw+=chunk;let data;if(req.method==='POST'){writes++;fields=JSON.parse(raw).records[0].fields;data={records:[{record_id:'rec_one',fields}]}}else data={record:{record_id:'rec_one',fields}};res.end(JSON.stringify({code:0,data}))})
  const http=new FeishuHttp(base,async()=>'loopback-only',100,{readAttempts:2,retryDelayMs:0}),resource={...row('todo','table1','Todo'),expiresAt:Date.now()+60000,capabilities:['bitable.read','bitable.update']},actor={origin:'feishu',userId:'alice',chatId:'chat',sessionId:'s',cwd:process.cwd()},directory=await temporaryDirectory(t),ledger=new OperationLedger(path.join(directory,'retry.sqlite'));cleanup(t,()=>ledger.close())
  const resources=new Resources([resource],['alice'],()=>http),operation=bitableOperation('update',{resource:'todo',params:{records:[{record_id:'rec_one',fields:{name:'after'}}]}})
  assert.equal((await ledger.execute(resources,actor,operation,AbortSignal.timeout(2000),true,'one-request')).status,'completed')
  assert.equal(writes,1);assert.equal(reads,4)
})
test('read timeouts are bounded and cancellation, malformed responses and uncertain writes never blindly retry',async t=>{
  let calls=0
  const base=await boundary(t,(req,res)=>{calls++;if(req.url.endsWith('/invalid'))res.end('not-json secret=must-not-leak')})
  const http=new FeishuHttp(base,async()=>'loopback-only',20,{readAttempts:2,retryDelayMs:0})
  await assert.rejects(http.request('GET','/open-apis/read'),e=>e.code==='read_timeout'&&e.diagnostic.attempts===2&&e.diagnostic.failure==='timeout')
  assert.equal(calls,2)
  await assert.rejects(http.request('POST','/open-apis/write',{name:'x'}),e=>e.code==='write_receipt_unknown'&&e.outcome==='unknown')
  assert.equal(calls,3)
  await assert.rejects(http.request('GET','/open-apis/invalid'),e=>e.code==='read_invalid_response'&&!e.message.includes('must-not-leak'))
  assert.equal(calls,4)
  await assert.rejects(http.request('GET','/open-apis/read',undefined,AbortSignal.abort()),e=>e.code==='read_cancelled')
  assert.equal(calls,4)
})
test('read-only POST searches retry transient HTTP errors but permission failures do not retry',async t=>{
  let calls=0
  const base=await boundary(t,(req,res)=>{calls++;res.writeHead(req.url.endsWith('/forbidden')?403:calls===1?503:200);res.end(JSON.stringify({code:0,data:{items:[]}}))})
  const http=new FeishuHttp(base,async()=>'loopback-only',100,{readAttempts:2,retryDelayMs:0})
  await http.request('POST','/open-apis/search',{},undefined,false);assert.equal(calls,2)
  await assert.rejects(http.request('GET','/open-apis/forbidden'),e=>e.code==='http_403');assert.equal(calls,3)
})
test('transport diagnostics distinguish DNS, TLS and system permission failures without exposing raw errors',async t=>{
  let calls=0,code='ENOTFOUND',recover=true
  t.mock.method(globalThis,'fetch',async()=>{calls++;if(recover&&calls===2)return new Response(JSON.stringify({code:0,data:{ok:true}}));throw new TypeError('secret=do-not-disclose',{cause:{code}})})
  const http=new FeishuHttp('http://127.0.0.1',async()=>'loopback-only',100,{readAttempts:2,retryDelayMs:0})
  assert.deepEqual(await http.request('GET','/open-apis/check'),{ok:true});assert.equal(calls,2)
  recover=false
  for(const [transport,kind]of [['CERT_HAS_EXPIRED','tls'],['EACCES','permission']]){
    code=transport;const before=calls
    await assert.rejects(http.request('GET','/open-apis/check?token=private'),error=>error.code==='read_'+kind&&error.diagnostic.transportCode===transport&&!error.message.includes('private')&&!error.message.includes('do-not-disclose'))
    assert.equal(calls,before+1)
  }
})
test('trusted desktop full permission reaches configured Feishu resources without impersonating a Feishu admin',()=>{
  const table={...row('todo','table1','Todo'),expiresAt:Date.now()+60000},resources=new Resources([table],['ou_admin'],()=>{throw Error('no network')})
  const actor={origin:'web',permissions:'full',userId:'desktop-owner',chatId:'native-session'}
  assert.equal(resources.resource(actor,{resource:'todo',action:'inspect',params:{}}),table)
  assert.throws(()=>resources.resource({...actor,permissions:'restricted'},{resource:'todo',action:'inspect',params:{}}),/actor_not_authorized/)
  assert.throws(()=>resources.resource({...actor,origin:'feishu'},{resource:'todo',action:'inspect',params:{}}),/actor_not_authorized/)
  assert.throws(()=>resources.resource(actor,{resource:'todo',action:'create',params:{}}),/capability_missing/)
})
test('native automatic authorization keeps one frozen resource write per request and preserves replay after restart',async t=>{
  const directory=await temporaryDirectory(t),file=path.join(directory,'operations.sqlite'),actor={origin:'web',permissions:'full',userId:'desktop-owner',chatId:'native',sessionId:'native',cwd:directory},signal=AbortSignal.timeout(5000)
  let writes=0,ledger=new OperationLedger(file);cleanup(t,()=>ledger.close())
  const records=new Map(),resource={...row('todo','table1','Todo'),expiresAt:Date.now()+60000,capabilities:['bitable.read','bitable.create']}
  const resources=new Resources([resource],[],()=>({async request(method,route,body){if(method==='POST'){writes++;records.set('rec_one',body.records[0].fields);return{records:[{record_id:'rec_one'}]}}return{record:{record_id:'rec_one',fields:records.get('rec_one')}}}}))
  const operation={resource:'todo',action:'create',params:{records:[{fields:{name:'native'}}]}}
  const previews=await Promise.all([ledger.preview(resources,actor,operation,signal,'owner-request-call'),ledger.preview(resources,actor,operation,signal,'owner-request-call')]);assert.equal(previews[0].token,previews[1].token)
  assert.equal((await ledger.confirm(resources,actor,previews[0].token,signal)).status,'completed');assert.equal(writes,1)
  assert.equal((await ledger.preview(resources,actor,operation,signal,'owner-request-call')).status,'completed');assert.equal(writes,1)
  await assert.rejects(ledger.preview(resources,actor,{...operation,params:{records:[{fields:{name:'changed'}}]}},signal,'owner-request-call'),/collision/)
  ledger.close();ledger=new OperationLedger(file)
  assert.equal((await ledger.preview(resources,actor,operation,signal,'owner-request-call')).status,'completed');assert.equal(writes,1)
})

test('automatic multi-record Bitable create/update verifies once, survives restart and keeps deletion behind confirmation',async t=>{
  const directory=await temporaryDirectory(t),file=path.join(directory,'direct.sqlite'),actor={origin:'feishu',userId:'alice',chatId:'chat',sessionId:'session',cwd:directory,sourceId:'message-one'},signal=AbortSignal.timeout(5000)
  let ledger=new OperationLedger(file),writes=0;cleanup(t,()=>ledger.close())
  const records=new Map(),resource={...row('todo','table1','Todo'),expiresAt:Date.now()+60000,capabilities:['bitable.read','bitable.create','bitable.update','bitable.delete']}
  const resources=new Resources([resource],['alice'],()=>({async request(method,url,body){
    if(url.endsWith('/batch_create')){writes++;return{records:body.records.map(r=>{const id='rec_'+records.size;records.set(id,r.fields);return{record_id:id}})}}
    if(url.endsWith('/batch_update')){writes++;for(const r of body.records)records.set(r.record_id,r.fields);return{records:body.records}}
    if(url.endsWith('/batch_delete')){writes++;return{records:body.records.map(id=>({record_id:id,deleted:records.delete(id)}))}}
    const id=url.split('/').at(-1);return{record:{record_id:id,fields:records.get(id)}}
  }}))
  const create={resource:'todo',action:'create',params:{records:[{fields:{name:'one'}},{fields:{name:'two'}}]}}
  assert.equal((await ledger.execute(resources,actor,create,signal,true,'message-one:call')).status,'completed');assert.equal(records.size,2);assert.equal(writes,1)
  ledger.close();ledger=new OperationLedger(file)
  assert.equal((await ledger.execute(resources,actor,create,signal,true,'message-one:call')).status,'completed');assert.equal(writes,1)
  const update={resource:'todo',action:'update',params:{records:[...records.keys()].map(record_id=>({record_id,fields:{name:'updated'}}))}}
  assert.equal((await ledger.execute(resources,actor,update,signal,true,'message-two:call')).status,'completed');assert.equal(writes,2);assert([...records.values()].every(r=>r.name==='updated'))
  const deletion={resource:'todo',action:'delete',params:{records:[...records.keys()].map(record_id=>({record_id}))}},preview=await ledger.execute(resources,actor,deletion,signal,false)
  assert.equal(preview.status,'confirmation_required');assert.equal(writes,2)
  assert.equal((await ledger.confirm(resources,actor,preview.token,signal)).status,'completed');assert.equal(writes,3);assert.equal(records.size,0)
})
test('configured table display names resolve exactly, with ambiguity rejected and no permission expansion',()=>{
  const todo=row('base-table1','table1','Todo'),resources=new Resources([todo],['alice'],()=>{throw Error('no network')})
  assert.equal(resources.configured('todo'),todo);assert.equal(resources.configured('  TODO  '),todo)
  assert.equal(resources.configured('base-table1'),todo)
  assert.throws(()=>resources.configured('unregistered'),/unknown_resource/)
  const duplicate=new Resources([todo,row('base-table2','table2','Todo')],['alice'],()=>{throw Error('no network')})
  assert.throws(()=>duplicate.configured('todo'),/ambiguous_resource/)
  const namedBase=new Resources([{...todo,name:'个人任务看板',baseName:'Todo'}],['alice'],()=>{throw Error('no network')})
  assert.equal(namedBase.configured('todo').tableId,'table1')
  const ambiguousBase=new Resources([{...todo,name:'任务',baseName:'Todo'},{...row('other','table2','说明'),baseName:'Todo'}],['alice'],()=>{throw Error('no network')})
  assert.throws(()=>ambiguousBase.configured('todo'),/ambiguous_resource/)
})

