import test from 'node:test'
import assert from 'node:assert/strict'
import { createCipheriv, createHash } from 'node:crypto'
import { DatabaseSync } from 'node:sqlite'
import { createServer } from 'node:http'
import { once } from 'node:events'
import path from 'node:path'
import {writeFile} from 'node:fs/promises'
import { decodeWebhook } from '../../plugins/feishu/lib/adapters/webhook.js'
import { parseEvent } from '../../plugins/feishu/lib/domain/events.js'
import { SqliteChannelRepository } from '../../plugins/feishu/lib/adapters/sqlite.js'
import { FeishuChannel } from '../../plugins/feishu/lib/domain/channel.js'
import { FeishuApi, apiOrigin } from '../../plugins/feishu/lib/adapters/feishu-api.js'
import { Interactions } from '../../plugins/core/lib/interactions.js'
import { TelebotCore } from '../../plugins/core/lib/index.js'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import { cleanup, eventually, temporaryDirectory } from '../support/temp-dir.mjs'

const settings = {
  appId: 'cli_test', adminUserIds: ['ou_alice', 'ou_bob'], workspace: process.cwd(),
  queueLimit: 20, maxConcurrentSessions: 2, pollMs: 25, leaseMs: 10000, retryMs: 50,
  maxDeliveryAttempts: 3, actionTtlMs: 10000,
}
const event = (id, extra = {}) => ({ eventId: `event-${id}`, messageId: `om_${id}`, chatId: 'oc_chat', threadId: '', actor: { ids: ['ou_alice'] }, kind: 'text', text: id, ...extra })
const payload = () => ({ schema: '2.0', header: { app_id: 'cli_test', event_id: 'event-1', event_type: 'im.message.receive_v1', token: 'test-token' },
  event: { sender: { sender_type: 'user', sender_id: { open_id: 'ou_alice' } }, message: { message_id: 'om_1', chat_id: 'oc_chat', message_type: 'text', content: JSON.stringify({ text: 'hello' }) } } })
const secrets = { verificationToken: 'test-token', encryptKey: '' }
async function repository(t, name = 'channel.sqlite') {
  const directory = await temporaryDirectory(t)
  const filename = path.join(directory, name)
  const store = new SqliteChannelRepository(filename, settings.appId, settings.leaseMs)
  cleanup(t, () => store.release())
  return { store, filename }
}

test('model messages use the existing outbox, authenticate targets and retain idempotent receipts across restart', async t => {
  const { FeishuMessages } = await import('../../plugins/feishu/lib/domain/messages.js')
  const {store,filename}=await repository(t), native={origin:'web',permissions:'full',userId:'desktop-owner',sessionId:'native-chat',chatId:'native-chat',cwd:process.cwd(),privateChat:true,nativeRequestId:'request-1'}
  store.accept(event('target',{actor:{ids:['ou_alice'],privateChat:true}}),settings)
  const source=store.claim([]);store.finish(source,'completed',[])
  const messages=new FeishuMessages(store,settings), signal=AbortSignal.timeout(5000)
  assert.equal(messages.list(native).items[0].chatId,'oc_chat')
  assert.throws(()=>messages.list({...native,permissions:'restricted'}),/not_authorized/)
  const actor={origin:'feishu',userId:'ou_alice',sessionId:source.spec.id,chatId:'oc_chat',threadId:'',cwd:process.cwd(),privateChat:true,sourceId:source.id}
  assert.throws(()=>messages.send(actor,{chatId:'other',text:'forbidden'},signal),/target_not_authorized/)
  assert.throws(()=>messages.send({...native,nativeRequestId:undefined},{text:'missing identity'},signal),/request_identity/)
  assert.throws(()=>messages.send(native,{text:'x'.repeat(120001)},signal),/message_text/)
  assert.throws(()=>messages.send(native,{text:'cancelled'},AbortSignal.abort()),/abort/i)
  const first=messages.send(native,{text:'主动通知 😀'},signal)
  assert.equal(first.state,'queued')
  assert.equal(messages.send(native,{text:'主动通知 😀'},signal).id,first.id)
  const outbound=store.claimDelivery(Date.now());assert.equal(outbound.chatId,'oc_chat');assert.equal(outbound.replyTo,'')
  store.settleDelivery(outbound,{status:'sent',messageId:'om_proactive'},Date.now(),3)
  assert.equal(messages.status(native,first.id).state,'sent')
  assert.equal(messages.status(native,first.id).messages[0].messageId,'om_proactive')
  assert.throws(()=>messages.status({...native,sessionId:'other-session'},first.id),/not_owned/)
  const uncertain=messages.send({...native,nativeRequestId:'request-2'},{text:'未知结果'},signal)
  store.claimDelivery(Date.now());store.recoverDeliveries()
  assert.equal(messages.retry(native,uncertain.id).state,'unknown')
  assert.equal(store.claimDelivery(Date.now()),undefined)
  const failed=messages.send({...native,nativeRequestId:'request-3'},{text:'明确失败'},signal)
  const rejected=store.claimDelivery(Date.now());assert(rejected,'unknown earlier request must not block a separate outgoing request')
  store.settleDelivery(rejected,{status:'failed',code:'feishu_http_403'},Date.now(),3)
  assert.equal(messages.retry(native,failed.id).state,'queued')
  const retried=store.claimDelivery(Date.now());assert.equal(retried.id,rejected.id)
  store.settleDelivery(retried,{status:'sent',messageId:'om_retry'},Date.now(),3)
  store.release();const restored=new SqliteChannelRepository(filename,settings.appId,settings.leaseMs);cleanup(t,()=>restored.release())
  assert.equal(new FeishuMessages(restored,settings).send(native,{text:'主动通知 😀'},signal).id,first.id)
  assert.equal(inspection(filename,"SELECT count(*) AS n FROM outbox")[0].n,3)
})

test('message targets remain unambiguous and current; topic messages preserve Unicode chunks and ordered delivery',async t=>{
  const { FeishuMessages }=await import('../../plugins/feishu/lib/domain/messages.js'),{store}=await repository(t)
  for(const incoming of [event('root'),event('topic',{threadId:'thread-a'}),event('bob',{chatId:'oc_bob',actor:{ids:['ou_bob']}})]){store.accept(incoming,settings);store.finish(store.claim([]),'completed',[])}
  const config={...settings},service=new FeishuMessages(store,config),native={origin:'web',permissions:'full',userId:'desktop',sessionId:'native',chatId:'native',cwd:process.cwd(),nativeRequestId:'req'},signal=AbortSignal.timeout(5000)
  assert.equal(service.list(native).total,3)
  assert.throws(()=>service.send(native,{text:'ambiguous'},signal),/target_required/)
  const alice={origin:'feishu',userId:'ou_alice',sessionId:'topic',chatId:'oc_chat',threadId:'thread-a',cwd:process.cwd(),sourceId:'trusted-source'}
  assert.equal(service.list(alice).total,1)
  const text='😀'.repeat(6001),receipt=service.send(alice,{text},signal);assert.equal(receipt.messages.length,2)
  const first=store.claimDelivery(Date.now());assert.equal(first.replyTo,'om_topic');assert.equal(first.threadId,'thread-a')
  const firstText=JSON.parse(first.content).zh_cn.content[0][0].text;assert.equal(Array.from(firstText).length,6000)
  store.settleDelivery(first,{status:'sent',messageId:'part-1'},Date.now(),3)
  const second=store.claimDelivery(Date.now()),secondText=JSON.parse(second.content).zh_cn.content[0][0].text
  assert.equal(firstText+secondText,text);store.settleDelivery(second,{status:'sent',messageId:'part-2'},Date.now(),3)
  assert.equal(service.status(alice,receipt.id).state,'sent')
  config.adminUserIds=['ou_bob'];assert.equal(service.list(native).total,1);assert.throws(()=>service.list(alice),/not_authorized/)
  config.allowedChatIds=['oc_missing'];assert.equal(service.list(native).total,0);assert.throws(()=>service.send(native,{chatId:'oc_bob',text:'revoked'},signal),/target_not_authorized/)
})
function inspection(filename, query) {
  const db = new DatabaseSync(filename, { readOnly: true })
  try { return db.prepare(query).all() } finally { db.close() }
}
function fakeAgent(overrides = {}) {
  return { run: async () => ({ status: 'completed', text: 'done' }), recover: async () => ({ status: 'unknown', text: 'uncertain' }),
    cancel() {}, status: () => ({ active: false }), dispose: async () => {}, ...overrides }
}

test('ordinary replies deliver local image and script links once, rewrite unavailable links and retain media receipts', async t => {
  const { FileTransfers } = await import('../../plugins/feishu/lib/adapters/files.js')
  const { startMockFeishu } = await import('../support/mock-feishu.mjs')
  const directory = await temporaryDirectory(t), image = path.join(directory, 'moon lake.png'), script = path.join(directory, 'draw_lake.py')
  await writeFile(image, Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aFz8AAAAASUVORK5CYII=', 'base64'))
  await writeFile(script, 'print("synthetic")')
  const {store,filename}=await repository(t), files=new FileTransfers(directory,async()=>{throw Error('No downloads')})
  cleanup(t,()=>files.close())
  const text=`![月夜](<${image}>)\n[脚本](<${script}>)\n[重复](<${script}>)\n[已删除](<${path.join(directory,'gone.log')}>)\n[网页](https://example.com)`
  const agent=fakeAgent({run:async()=>({status:'completed',text,presentedFiles:[{path:image}]})})
  const channel=new FeishuChannel({repository:store,agent,files,delivery:{}},settings)
  store.accept(event('local-files'),{...settings,workspace:directory});await channel.process(store.claim([]))
  const outputs=inspection(filename,'SELECT * FROM outbox ORDER BY seq')
  assert.deepEqual(outputs.map(row=>row.msg_type),['post','image','file'])
  assert(!outputs[0].content.includes(directory));assert.match(outputs[0].content,/附件/);assert.match(outputs[0].content,/不存在/);assert.match(outputs[0].content,/https:\/\/example.com/)
  const platform=await startMockFeishu();cleanup(t,()=>platform.close())
  const api=new FeishuApi({appId:'cli_test',secret:async()=>'loopback-app-secret',origin:platform.origin,timeoutMs:3000})
  for(let outgoing;(outgoing=store.claimDelivery(Date.now()));){const receipt=await api.send(outgoing,key=>store.uploaded(outgoing,key));assert.equal(receipt.status,'sent');store.settleDelivery(outgoing,receipt,0,3)}
  assert.equal(platform.imageUploads.length,1);assert.equal(platform.uploads.length,1)
  const media=inspection(filename,"SELECT msg_type,uploaded_key,state FROM outbox WHERE msg_type IN ('image','file')")
  assert(media.every(row=>row.uploaded_key&&row.state==='sent'))
})

test('local Markdown references support Windows paths, file URLs, relative/reference links and balanced names while code and web links stay inert',async()=>{
  const {fileReferences,rewriteFileReferences}=await import('../../plugins/feishu/lib/domain/file-references.js')
  const text='![图片](D:\\files_data\\moon.png) [脚本](<D:/files data/draw.py:12>) [括号](./a(b).log) [引用][log]\n[log]: <file:///D:/files%20data/run.log>\n`[示例](./secret.txt)`\n```md\n[代码](./ignored.txt)\n```\n[网页](https://example.com/file.py)\n<file:///D:/files_data/a.txt>\n普通路径 D:\\files_data\\mention.py'
  const references=fileReferences(text)
  assert.deepEqual(new Set(references.map(row=>row.path)),new Set(['D:\\files_data\\moon.png','D:/files data/draw.py','./a(b).log','D:/files data/run.log','D:/files_data/a.txt']))
  const rewritten=rewriteFileReferences(text,()=> '附件')
  assert(rewritten.includes('`[示例](./secret.txt)`'));assert(rewritten.includes('[代码](./ignored.txt)'));assert(rewritten.includes('https://example.com/file.py'));assert(rewritten.includes('普通路径 D:\\files_data\\mention.py'))
  assert.equal(fileReferences('[![嵌套](./image.png)](https://example.com)')[0].path,'./image.png')
  assert.equal(fileReferences('[网络文件](file://server/share/file.log)')[0].path,'//server/share/file.log')
})

test('reply composition enforces file scope and frozen hashes across aliases, deduplicates actual files and snapshots bytes for restart',async t=>{
  const {FileTransfers}=await import('../../plugins/feishu/lib/adapters/files.js'),{composeReply}=await import('../../plugins/feishu/lib/domain/replies.js'),{readFile,unlink,symlink}=await import('node:fs/promises')
  const directory=await temporaryDirectory(t),outside=await temporaryDirectory(t),file=path.join(directory,'report.py');await writeFile(file,'before')
  await writeFile(path.join(directory,'.env'),'secret');await writeFile(path.join(outside,'outside.log'),'outside');await symlink(outside,path.join(directory,'escape'),process.platform==='win32'?'junction':'dir')
  const files=new FileTransfers(directory,async()=>{throw Error('No download')});cleanup(t,()=>files.close())
  const entry={spec:{cwd:directory}},result=await composeReply(entry,{text:'[一](report.py) [二](./report.py) [秘密](.env) [越界](escape/outside.log) [目录](.) [缺失](missing.py)'},files)
  const concurrent=await Promise.all(Array.from({length:8},()=>files.outbound(entry,file,{reference:true})))
  assert.equal(new Set(concurrent).size,1)
  assert.equal(result.outputs.filter(row=>row.msgType==='file').length,1);assert.match(result.text,/不存在/);assert.match(result.text,/权限/)
  const descriptor=JSON.parse(result.outputs.find(row=>row.msgType==='file').content);await unlink(file);assert.equal(await readFile(descriptor.path,'utf8'),'before')
  await writeFile(file,'after');const changed=await composeReply(entry,{text:'[别名](./report.py)',artifacts:[{path:file,size:6,sha256:createHash('sha256').update('before').digest('hex')}]},files)
  assert.equal(changed.outputs.filter(row=>row.msgType==='file').length,0);assert.match(changed.text,/变化/)
  const image=path.join(directory,'large.png'),data=Buffer.alloc(10*1024*1024+1);Buffer.from([137,80,78,71,13,10,26,10]).copy(data);await writeFile(image,data)
  const fallback=await composeReply(entry,{text:'![大图](large.png)'},files);assert.equal(fallback.outputs.at(-1).msgType,'file')
})

test('recovered final file links enqueue attachments without rerunning the Agent; one failed media item does not block another',async t=>{
  const {FileTransfers}=await import('../../plugins/feishu/lib/adapters/files.js')
  const directory=await temporaryDirectory(t);await writeFile(path.join(directory,'first.log'),'one');await writeFile(path.join(directory,'second.py'),'two')
  const files=new FileTransfers(directory,async()=>{throw Error('No download')});cleanup(t,()=>files.close())
  const {store,filename}=await repository(t);store.accept(event('recover-files'),{...settings,workspace:directory});store.claim([])
  let calls=0;const channel=new FeishuChannel({repository:store,files,agent:fakeAgent({run:async()=>{calls++;throw Error('Must not rerun')},recover:async()=>({status:'completed',text:'[日志](first.log) [脚本](second.py)'})}),delivery:{}},settings)
  await channel.recover();assert.equal(calls,0)
  const first=store.claimDelivery(Date.now());store.settleDelivery(first,{status:'sent',messageId:'text'},0,3)
  const failed=store.claimDelivery(Date.now());store.settleDelivery(failed,{status:'failed',code:'feishu_upload_rejected'},0,3)
  const next=store.claimDelivery(Date.now());assert.equal(next.msgType,'file');store.settleDelivery(next,{status:'sent',messageId:'second'},0,3)
  assert.equal(inspection(filename,"SELECT count(*) AS n FROM outbox WHERE state='failed'")[0].n,1);assert.equal(store.status(inspection(filename,'SELECT key FROM sessions')[0].key).deliveryFailed,1)
})

test('proactive and delayed replies use common file composition and retain the first receipt after source deletion',async t=>{
  const {FileTransfers}=await import('../../plugins/feishu/lib/adapters/files.js'),{composeReply}=await import('../../plugins/feishu/lib/domain/replies.js'),{FeishuMessages}=await import('../../plugins/feishu/lib/domain/messages.js'),{unlink}=await import('node:fs/promises')
  const directory=await temporaryDirectory(t),file=path.join(directory,'code.py');await writeFile(file,'print(1)')
  const files=new FileTransfers(directory,async()=>{throw Error('No download')});cleanup(t,()=>files.close())
  const {store,filename}=await repository(t);store.accept(event('file-target',{actor:{ids:['ou_alice'],privateChat:true}}),{...settings,workspace:directory});const entry=store.claim([]);store.finish(entry,'completed',[])
  const actor={origin:'feishu',userId:'ou_alice',chatId:entry.chatId,threadId:'',sessionId:entry.spec.id,cwd:directory,sourceId:entry.id,privateChat:true},service=new FeishuMessages(store,settings,async(actor,target,text)=>(await composeReply(store.messageSource(actor,target),{text},files)).outputs)
  const text='[代码](code.py)',first=await service.send(actor,{text},AbortSignal.timeout(3000));assert.equal(first.messages.length,2)
  const result={status:'completed',text},reply=await composeReply(store.source(actor),result,files),ids=store.enqueueOnce('delayed-files',actor,reply.outputs,result)
  await unlink(file);assert.deepEqual(await service.send(actor,{text},AbortSignal.timeout(3000)),first);assert.deepEqual(store.queuedDelivery('delayed-files',actor,result),ids)
  const legacy=[{msgType:'post',content:JSON.stringify({zh_cn:{content:[[{tag:'md',text:'old reply'}]]}})}],legacyIds=store.enqueueOnce('old-delivery',actor,legacy)
  assert.deepEqual(store.queuedDelivery('old-delivery',actor,{status:'completed',text:'old reply'},legacy),legacyIds)
  assert.throws(()=>store.queuedDelivery('old-delivery',actor,{status:'completed',text:'changed'},[]),/collision/)
  assert.equal(inspection(filename,"SELECT count(*) AS n FROM outbox WHERE msg_type='file'")[0].n,2)
})

test('question cards and confirmation results convert their local review links into attachments',async t=>{
  const {FileTransfers}=await import('../../plugins/feishu/lib/adapters/files.js')
  const directory=await temporaryDirectory(t);await writeFile(path.join(directory,'review.log'),'review')
  const files=new FileTransfers(directory,async()=>{throw Error('No download')});cleanup(t,()=>files.close())
  const {store,filename}=await repository(t),channel=new FeishuChannel({repository:store,files,delivery:{},agent:fakeAgent({run:async(_spec,input)=>{await input.question({token:'review',question:'请看[日志](review.log)',choices:['继续'],approval:false});return{status:'completed',text:'done'}}}),features:{invoke:async()=>({status:'completed',text:'完成：[日志](review.log)'})}},settings)
  for(const [id,kind]of [['question-files','text'],['confirmation-files','confirmation']]){store.accept(event(id,{kind,text:'review'}),{...settings,workspace:directory});await channel.process(store.claim([]))}
  const cards=inspection(filename,"SELECT content FROM outbox WHERE msg_type='interactive'");assert.equal(cards.length,2);assert(cards.every(row=>!row.content.includes('](review.log)')&&row.content.includes('附件')))
  assert.equal(inspection(filename,"SELECT * FROM outbox WHERE msg_type='file'").length,2)
})

test('image upload keys survive restart and a failed send can retry after the local snapshot disappears; unknown images never resend',async t=>{
  const {FileTransfers}=await import('../../plugins/feishu/lib/adapters/files.js'),{startMockFeishu}=await import('../support/mock-feishu.mjs'),{unlink}=await import('node:fs/promises')
  const directory=await temporaryDirectory(t),file=path.join(directory,'image.png');await writeFile(file,Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aFz8AAAAASUVORK5CYII=','base64'))
  const files=new FileTransfers(directory,async()=>{throw Error('No download')});cleanup(t,()=>files.close())
  const filename=path.join(directory,'channel.sqlite');let store=new SqliteChannelRepository(filename,settings.appId,settings.leaseMs);cleanup(t,()=>store.release())
  store.accept(event('image-retry'),{...settings,workspace:directory});const entry=store.claim([]),actor={origin:'feishu',userId:'ou_alice',chatId:entry.chatId,threadId:'',sessionId:entry.spec.id,cwd:directory,sourceId:entry.id};store.finish(entry,'completed',[])
  const content=await files.outbound(entry,file,{reference:true}),ids=store.enqueueOnce('image-retry',actor,[{msgType:'image',content}]),platform=await startMockFeishu();cleanup(t,()=>platform.close())
  platform.behaviors.set(entry.messageId,'rate-limit-once');const api=new FeishuApi({appId:'cli_test',secret:async()=>'loopback-app-secret',origin:platform.origin,timeoutMs:3000})
  let message=store.claimDelivery(Date.now()),result=await api.send(message,key=>store.uploaded(message,key));assert.equal(result.status,'retry');store.settleDelivery(message,result,0,1)
  assert.equal(store.externalStatus(actor,ids)[0].upload,'uploaded');store.release();store=new SqliteChannelRepository(filename,settings.appId,settings.leaseMs)
  await unlink(JSON.parse(content).path);store.retryExternal(actor,ids);message=store.claimDelivery(Date.now());assert.equal(message.uploadedFileKey,'image-1')
  result=await api.send(message,key=>store.uploaded(message,key));assert.equal(result.status,'sent');store.settleDelivery(message,result,0,3);assert.equal(platform.imageUploads.length,1);assert.equal(JSON.parse(platform.sent[0].body.content).image_key,'image-1')
  const unknownIds=store.enqueueOnce('unknown-image',actor,[{msgType:'image',content:await files.outbound(entry,file,{reference:true})}]);platform.behaviors.set(entry.messageId,'disconnect')
  message=store.claimDelivery(Date.now());result=await api.send(message,key=>store.uploaded(message,key));assert.equal(result.status,'unknown');store.settleDelivery(message,result,0,3)
  store.release();store=new SqliteChannelRepository(filename,settings.appId,settings.leaseMs);store.recoverDeliveries();store.retryExternal(actor,unknownIds);assert.equal(store.claimDelivery(Date.now()),undefined);assert.equal(store.externalStatus(actor,unknownIds)[0].state,'unknown')
})

test('mixed batch confirmation produces rich receipts and queues the remaining dialogue exactly once',async t=>{
  const {store,filename}=await repository(t),require=createRequire(new URL('../../plugins/core/package.json',import.meta.url)),{Context}=await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')).href),ctx=new Context(),core=new TelebotCore(ctx),writes=[],runs=[]
  ctx.provide('llm',{async *stream(){yield{type:'text-delta',text:JSON.stringify({decision:'confirm',selections:[1,2],remainingText:'另外两点半发飞书提醒。'})};yield{type:'finish',reason:{kind:'stop'}}}})
  const pending=new Set(['create','update'])
  core.registerConfirmations('bitable',{pending:()=>[...pending].map(token=>({token,label:'**'+token+'预览**'})),confirm:async(_actor,token)=>{pending.delete(token);writes.push(token);return{status:'completed',text:'**已写入并核验**'}},cancel:async()=>{throw Error('Unexpected cancel')}})
  const agent=fakeAgent({run:async(_spec,input)=>{runs.push(input.text);return{status:'completed',text:'已继续处理提醒。'}}})
  const channel=new FeishuChannel({repository:store,agent,features:core,delivery:{send:async()=>({status:'sent',messageId:'sent'})}},{...settings,provider:'fixture',model:'fixture'})
  // Establish a mapped session before the first confirmation arrives.
  store.accept(event('seed'),{...settings,provider:'fixture',model:'fixture'});const seed=store.claim([]);store.finish(seed,'completed',[])
  channel.start();cleanup(t,()=>channel.stop())
  const reply='以上全部确认。另外两点半发飞书提醒。'
  channel.accept(event('mixed',{text:reply}));channel.accept(event('mixed',{text:reply}))
  await eventually(()=>runs.length===1)
  assert.deepEqual(writes,['create','update']);assert.match(runs[0],/另外两点半发飞书提醒/);assert.match(runs[0],/已写入并核验/)
  const originals=inspection(filename,"SELECT original_text FROM input_context c JOIN inbox i ON c.inbox_id=i.id WHERE event_id='event-mixed'")
  assert.equal(originals[0].original_text,reply)
  const outputs=inspection(filename,"SELECT msg_type,content FROM outbox o JOIN inbox i ON i.id=o.inbox_id WHERE event_id='event-mixed'")
  assert.equal(outputs[0].msg_type,'interactive');assert.match(JSON.parse(outputs[0].content).elements[0].content,/\*\*批量确认结果\*\*/)
})

test('interpretation failure preserves the whole mixed message and resumes dialogue without authorizing anything',async t=>{
  const {store,filename}=await repository(t),require=createRequire(new URL('../../plugins/core/package.json',import.meta.url)),{Context}=await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')).href),ctx=new Context(),core=new TelebotCore(ctx),runs=[]
  ctx.provide('llm',{async *stream(){yield{type:'text-delta',text:'not json'};yield{type:'finish',reason:{kind:'stop'}}}})
  core.registerConfirmations('bitable',{pending:()=>[{token:'frozen',label:'已有预览'}],confirm:async()=>{throw Error('Must not authorize')},cancel:async()=>{throw Error('Must not cancel')}})
  const config={...settings,provider:'fixture',model:'fixture'};store.accept(event('seed'),config);const seed=store.claim([]);store.finish(seed,'completed',[])
  const channel=new FeishuChannel({repository:store,features:core,agent:fakeAgent({run:async(_spec,input)=>{runs.push(input.text);return{status:'completed',text:'继续解释'}}}),delivery:{send:async()=>({status:'sent',messageId:'sent'})}},config)
  channel.start();cleanup(t,()=>channel.stop());const reply='全部确认，另外到点发飞书消息提醒。';channel.accept(event('parse-failure',{text:reply}))
  await eventually(()=>runs.length===1)
  assert.match(runs[0],new RegExp(reply));assert.match(runs[0],/确认尚未执行/)
  assert.equal(inspection(filename,"SELECT count(*) AS n FROM inbox WHERE event_id='event-parse-failure:continuation'")[0].n,1)
})

test('confirmation completion enters original and continued chat context without invoking another model turn',async t=>{
  const {store,filename}=await repository(t),contexts=[]
  store.accept(event('seed'),{...settings,provider:'fixture',model:'selected-model'})
  const original=store.claim([])
  store.markMapped(original.spec.id);store.finish(original,'completed',[])
  store.updateMapping(original,{...original.spec,id:'continued',allowCreate:false})
  let calls=0,runs=0
  const require=createRequire(new URL('../../plugins/core/package.json',import.meta.url)),{Context}=await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')).href),ctx=new Context(),core=new TelebotCore(ctx)
  const interpretations=[]
  ctx.provide('llm',{async *stream(options){interpretations.push(options);yield{type:'text-delta',index:0,text:JSON.stringify({decision:'confirm',selection:1})};yield{type:'finish',reason:{kind:'stop'}}}})
  core.registerConfirmations('bitable',{pending:actor=>actor.userId==='ou_alice'&&actor.sessionId===original.spec.id?[{token:'frozen',label:'修改备注'}]:[],confirm:async()=>{calls++;return{status:'completed',text:'已完成修改，并已回读核验。'}},cancel:async()=>({status:'cancelled',text:'已取消。'})})
  core.registerFeature('confirm',async({actor,text,signal})=>{assert.equal(text,'confirm bitable frozen');return core.confirmLatest(actor,false,signal,{name:'bitable',token:'frozen'})})
  const agent=fakeAgent({run:async()=>{runs++;return{status:'completed',text:'ordinary reply'}},context:async(spec,id,text)=>contexts.push({session:spec.id,id,text})})
  const channel=new FeishuChannel({repository:store,agent,delivery:{send:async()=>({status:'sent',messageId:'sent'})},features:core},{...settings,provider:'fixture',model:'selected-model'})
  channel.start();cleanup(t,()=>channel.stop())
  channel.accept(event('confirmed',{text:'好的，请修改把'}))
  channel.accept(event('confirmed',{text:'好的，请修改把'}))
  await eventually(()=>inspection(filename,"SELECT state FROM inbox WHERE message_id='om_confirmed'")[0]?.state==='completed')
  await eventually(()=>contexts.length>=1)
  assert.equal(calls,1);assert.equal(runs,0);assert(contexts.some(row=>row.session===original.spec.id&&row.text.includes('已完成修改')))
  assert.equal(interpretations.length,1)
  const current=store.currentSpec(inspection(filename,"SELECT session_key FROM inbox WHERE message_id='om_confirmed'")[0].session_key)
  assert(contexts.some(row=>row.session===current.id))
})

test('semantic replies answer an occupied approval in its original session without creating a chat fork',async t=>{
  const {store,filename}=await repository(t),require=createRequire(new URL('../../plugins/core/package.json',import.meta.url)),{Context}=await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')).href),ctx=new Context(),core=new TelebotCore(ctx)
  ctx.provide('llm',{async *stream(){yield{type:'text-delta',index:0,text:'{"decision":"confirm","selection":1}'};yield{type:'finish',reason:{kind:'stop'}}}})
  core.registerFeature('answer',async({actor,text})=>{const split=text.indexOf(' '),accepted=core.interactions.answer(actor,text.slice(0,split),text.slice(split+1));return{status:accepted?'completed':'failed',text:accepted?'回答已提交。':'回答已失效。'}})
  let waiting=false,runs=0,forks=0
  const agent=fakeAgent({prepare:async()=>{forks++},run:async(spec,input)=>{runs++;const answer=await core.interactions.ask(input.actor,'是否允许本次修改？',['允许一次','拒绝'],true,async question=>{waiting=true;await input.question(question)});return{status:'completed',text:answer}},cancel:id=>core.interactions.cancelSession(id)})
  const channel=new FeishuChannel({repository:store,agent,features:core,delivery:{send:async()=>({status:'sent',messageId:'sent'})}},{...settings,provider:'fixture',model:'selected-model',maxConcurrentSessions:1})
  channel.start();cleanup(t,()=>channel.stop())
  channel.accept(event('ask-approval'));await eventually(()=>waiting)
  channel.accept(event('semantic-answer',{text:'好的，请修改把'}))
  await eventually(()=>inspection(filename,"SELECT state FROM inbox WHERE message_id='om_ask-approval'")[0]?.state==='completed')
  assert.equal(runs,1);assert.equal(forks,0)
})

test('a semantic non-answer returns to the ordinary serial queue while an active request remains running',async t=>{
  const {store,filename}=await repository(t),require=createRequire(new URL('../../plugins/core/package.json',import.meta.url)),{Context}=await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')).href),ctx=new Context(),core=new TelebotCore(ctx),runs=[]
  ctx.provide('llm',{async *stream(){yield{type:'text-delta',index:0,text:'{"decision":"other","selection":null}'};yield{type:'finish',reason:{kind:'stop'}}}})
  let release
  const active=new Promise(resolve=>{release=resolve});cleanup(t,()=>release())
  const agent=fakeAgent({run:async(_spec,input)=>{runs.push(input.text);if(input.text==='long-request')await active;return{status:'completed',text:'done'}}})
  const channel=new FeishuChannel({repository:store,agent,features:core,delivery:{send:async()=>({status:'sent',messageId:'sent'})}},{...settings,provider:'fixture',model:'selected-model',maxConcurrentSessions:1})
  channel.start();cleanup(t,()=>channel.stop());channel.accept(event('long',{text:'long-request'}));await eventually(()=>runs.length===1)
  core.registerConfirmations('bitable',{pending:()=>[{token:'frozen',label:'修改备注'}],confirm:async()=>{throw Error('Must not confirm')},cancel:async()=>{throw Error('Must not cancel')}})
  channel.accept(event('new-topic',{text:'明天是什么天气'}))
  await eventually(()=>inspection(filename,"SELECT kind FROM inbox WHERE message_id='om_new-topic'")[0]?.kind==='text')
  assert.deepEqual(runs,['long-request'])
  release();await eventually(()=>runs.length===2)
  assert.deepEqual(runs,['long-request','明天是什么天气'])
})

test('restart invalidates interrupted semantic interpretation without replaying a model or confirming an operation',async t=>{
  const {store,filename}=await repository(t);store.accept(event('interrupted-interpretation',{kind:'interpretation',text:'expired-ticket'}),settings);store.claim([])
  let calls=0
  const channel=new FeishuChannel({repository:store,agent:fakeAgent(),features:{invoke:async()=>{throw Error('Must not invoke')},resolveInput:async()=>{calls++;return{kind:'confirmation',text:'confirm bitable frozen'}}},delivery:{send:async()=>({status:'sent',messageId:'sent'})}},settings)
  channel.start();cleanup(t,()=>channel.stop());await eventually(()=>inspection(filename,"SELECT state FROM inbox WHERE message_id='om_interrupted-interpretation'")[0]?.state==='cancelled')
  assert.equal(calls,0)
})
async function start(t, store, agent, delivery, config = settings) {
  const channel = new FeishuChannel({ repository: store, agent, delivery }, config)
  channel.start()
  cleanup(t, () => channel.stop())
  return channel
}

test('P4 verified command artifacts enter a separate durable delivery; changed artifacts keep execution receipt and never upload', async t => {
  const { store, filename } = await repository(t)
  let executions = 0
  const channel = new FeishuChannel({ repository: store, agent: fakeAgent(), delivery: { send: async () => ({ status: 'sent', messageId: 'sent' }) },
    features: { invoke: async () => { executions++; return { status: 'completed', text: 'execution done', artifacts: [{ path: path.join(process.cwd(), 'file.txt'), size: 1, sha256: executions === 1 ? 'frozen' : 'old-hash' }] } } },
    files: { receive: async () => {}, forMessage: async () => [], outbound: async () => JSON.stringify({ path: path.join(process.cwd(), 'file.txt'), sha256: 'frozen' }) },
  }, settings)
  channel.start(); cleanup(t, () => channel.stop())
  channel.accept(event('artifact', { text: '/codex confirm first' }))
  await eventually(() => inspection(filename, "SELECT * FROM outbox WHERE msg_type='file'").length === 1)
  channel.accept(event('changed', { text: '/codex confirm second' }))
  await eventually(() => inspection(filename, "SELECT * FROM inbox WHERE message_id='om_changed'")[0]?.state === 'completed')
  assert.equal(inspection(filename, "SELECT * FROM outbox WHERE msg_type='file'").length, 1)
  assert(inspection(filename, "SELECT content FROM outbox WHERE reply_to='om_changed'").some(row => row.content.includes('未上传')))
  assert.equal(executions, 2)
})

test('webhook rejects wrong/missing source credentials and binds app and operator fields', () => {
  const body = Buffer.from(JSON.stringify(payload()))
  assert.equal(parseEvent(decodeWebhook(body, {}, secrets), 'cli_test').actor.ids[0], 'ou_alice')
  assert.throws(() => decodeWebhook(body, {}, { ...secrets, verificationToken: 'wrong-token' }), /authentication_failed/)
  assert.throws(() => decodeWebhook(body, {}, { ...secrets, verificationToken: '' }), /not_configured/)
  assert.throws(() => parseEvent(decodeWebhook(body, {}, secrets), 'cli_other'), /wrong_app/)
  const spoof = payload()
  spoof.event.sender.sender_id = { open_id: 'ou_stranger' }
  spoof.event.message.content = JSON.stringify({ text: 'I am an admin', user_id: 'ou_alice' })
  assert.deepEqual(parseEvent(spoof, 'cli_test').actor.ids, ['ou_stranger'])
  spoof.event.sender.sender_type = 'app'
  assert.equal(parseEvent(spoof, 'cli_test'), undefined)
})

test('encrypted callbacks validate the original byte signature, time window and padding', () => {
  const keyText = 'test-encryption-key'
  const key = createHash('sha256').update(keyText).digest()
  const cipher = createCipheriv('aes-256-cbc', key, key.subarray(0, 16))
  const encrypted = Buffer.concat([cipher.update(JSON.stringify(payload())), cipher.final()]).toString('base64')
  const body = Buffer.from(JSON.stringify({ encrypt: encrypted, event: { spoofed: true } }))
  const timestamp = String(Math.floor(Date.now() / 1000)), nonce = 'test-nonce'
  const signature = createHash('sha256').update(timestamp + nonce + keyText).update(body).digest('hex')
  const headers = { 'x-lark-request-timestamp': timestamp, 'x-lark-request-nonce': nonce, 'x-lark-signature': signature }
  assert.equal(decodeWebhook(body, headers, { ...secrets, encryptKey: keyText }).event.message.message_id, 'om_1')
  assert.throws(() => decodeWebhook(Buffer.concat([body, Buffer.from(' ')]), headers, { ...secrets, encryptKey: keyText }), /authentication_failed/)
  assert.throws(() => decodeWebhook(body, headers, { ...secrets, encryptKey: keyText }, Date.now() + 700000), /authentication_failed/)
  // Do not expose distinct base64, padding or decrypted-JSON failures to unauthenticated senders.
  for (const ciphertext of ['not-base64', Buffer.alloc(16).toString('base64')]) {
    assert.throws(() => decodeWebhook(Buffer.from(JSON.stringify({ encrypt: ciphertext })), headers, { ...secrets, encryptKey: keyText }), /authentication_failed/)
  }
})

test('SQLite admission deduplicates both event and message IDs across restart; empty admin list denies', async t => {
  const { store, filename } = await repository(t)
  assert.equal(store.accept(event('one'), { ...settings, adminUserIds: [] }).rejected, true)
  assert.equal(store.accept(event('one'), settings).accepted, true)
  assert.equal(store.accept(event('two', { messageId: 'om_one' }), settings).duplicate, true)
  store.release()
  const restarted = new SqliteChannelRepository(filename, settings.appId, settings.leaseMs)
  cleanup(t, () => restarted.release())
  assert.equal(restarted.accept(event('one'), settings).duplicate, true)
  assert.equal(inspection(filename, 'SELECT * FROM inbox').length, 1)
})

test('database refuses competing workers, another app and unsupported schema', async t => {
  const { store, filename } = await repository(t)
  assert.throws(() => new SqliteChannelRepository(filename, settings.appId, settings.leaseMs), /already_active/)
  assert.throws(() => new SqliteChannelRepository(filename, 'cli_other', settings.leaseMs), /app_mismatch/)
  store.release()
  const db = new DatabaseSync(filename)
  db.exec('PRAGMA user_version=99'); db.close()
  assert.throws(() => new SqliteChannelRepository(filename, settings.appId, settings.leaseMs), /unsupported_channel_schema/)
})

test('queue saturation keeps duplicates and bounded local control admission available', async t => {
  const { store } = await repository(t)
  const config = { ...settings, queueLimit: 1 }
  assert.equal(store.accept(event('one'), config).accepted, true)
  assert.equal(store.accept(event('two'), config).full, true)
  assert.equal(store.accept(event('one'), config).duplicate, true)
  assert.equal(store.accept(event('stop', { text: '/stop' }), config).accepted, true)
})

test('a configured test-chat allowlist rejects even an administrator outside that chat before creating any session',async t=>{
  const {store,filename}=await repository(t),config={...settings,allowedChatIds:['oc_test_only']}
  assert.equal(store.accept(event('wrong_chat'),config).rejected,true)
  assert.equal(inspection(filename,'SELECT * FROM sessions').length,0)
  assert(store.accept(event('allowed_chat',{chatId:'oc_test_only'}),config).accepted)
  assert.equal(store.accept(event('wrong_control',{kind:'status',text:'/status'}),config).rejected,true)
})

test('configuration admission hold rejects new inputs without losing queued work and releases exactly once',async t=>{
  const {store}=await repository(t)
  const channel=new FeishuChannel({repository:store,agent:fakeAgent(),delivery:{send:async()=>({status:'sent',messageId:'sent'})}},settings)
  assert.equal(store.busy(),false)
  channel.accept(event('before_config'));assert.equal(store.busy(),true)
  const first=channel.holdAdmission(),second=channel.holdAdmission()
  assert.throws(()=>channel.accept(event('during_config')),/reconfiguring/)
  first();first();assert.throws(()=>channel.accept(event('still_during_config')),/reconfiguring/)
  second();assert(channel.accept(event('after_config')).accepted)
  assert.equal(store.claim([]).messageId,'om_before_config')
})

test('P2 persistent batching merges only the same actor and conversation, while control requests bypass the window', async t => {
  const { store } = await repository(t)
  const config = { ...settings, mergeMs: 40, maxMergeWaitMs: 100 }
  store.accept(event('first'), config); store.accept(event('second'), config)
  assert.equal(store.claim([]), undefined)
  store.accept(event('status', { kind: 'status', text: '/status' }), config)
  const status = store.claim([]); assert.equal(status.kind, 'status'); store.finish(status, 'completed', [])
  await new Promise(resolve => setTimeout(resolve, 50))
  const merged = store.claim([]); assert.equal(merged.text, 'first\nsecond')
  store.finish(merged, 'completed', [])
  store.accept(event('alice'), config); store.accept(event('bob', { actor: { ids: ['ou_bob'] } }), config)
  await new Promise(resolve => setTimeout(resolve, 50))
  assert.equal(store.claim([]).text, 'alice')
})

test('P2 an optional unknown progress publication never blocks the authoritative final reply', async t => {
  const { store } = await repository(t)
  store.accept(event('progress'), settings)
  const entry = store.claim([])
  store.enqueue(entry, [{ msgType: 'interactive', content: JSON.stringify({ header: { title: { content: '执行进度' } } }) }])
  store.finish(entry, 'completed', [{ msgType: 'post', content: '{}' }])
  const progress = store.claimDelivery(Date.now()); store.settleDelivery(progress, { status: 'unknown', code: 'lost_optional_progress' }, 0, 3)
  assert.equal(store.claimDelivery(Date.now()).msgType, 'post')
})

test('Feishu progress uses one editable Markdown post and replaces it with the final reply without duplicate phases',async t=>{
  const {store,filename}=await repository(t),sent=[]
  const agent=fakeAgent({run:async(_spec,input)=>{
    input.progress('正在处理，请稍候...');await eventually(()=>sent.length===1)
    input.progress('正在处理，请稍候...');input.progress('正在调用工具处理请求…')
    await eventually(()=>sent.length>=2)
    return {status:'completed',text:'**完成**\n\n|项目|结果|\n|---|---|\n|检查|通过|'}
  }})
  const channel=await start(t,store,agent,{send:async message=>{sent.push(message);return{status:'sent',messageId:message.editMessageId??'one-progress-message'}}},{...settings,streamProgress:true})
  channel.accept(event('editable-progress'))
  await eventually(()=>inspection(filename,"SELECT state FROM inbox WHERE message_id='om_editable-progress'")[0]?.state==='completed'&&sent.some(row=>row.content.includes('**完成**')))
  assert.equal(sent.filter(row=>!row.editMessageId).length,1)
  assert(sent.every(row=>row.msgType==='post'))
  assert(sent.slice(1).every(row=>row.editMessageId==='one-progress-message'))
  assert.equal(JSON.parse(sent.at(-1).content).zh_cn.content[0][0].tag,'md')
})

test('progress coalesces unsent phases, preserves retry payloads and lets a final reply supersede optional retries',async t=>{
  const {store}=await repository(t);store.accept(event('coalesce'),settings);const entry=store.claim([])
  store.progress(entry,'first');store.progress(entry,'latest')
  const initial=store.claimDelivery(Date.now());assert.equal(initial.content,'latest')
  store.settleDelivery(initial,{status:'retry',code:'limited'},Date.now()+60000,3)
  store.progress(entry,'new phase')
  store.finish(entry,'completed',[{msgType:'post',content:'final'}])
  const final=store.claimDelivery(Date.now());assert.equal(final.content,'final');assert(!final.editMessageId)
  store.settleDelivery(final,{status:'sent',messageId:'final'},0,3);assert.equal(store.claimDelivery(Date.now()+120000),undefined)
  store.accept(event('in-flight-final'),settings);const next=store.claim([])
  store.progress(next,'placeholder');const sending=store.claimDelivery(Date.now())
  store.progress(next,'phase');store.finish(next,'completed',[{msgType:'post',content:'final after in-flight create'}])
  assert.equal(store.claimDelivery(Date.now()),undefined)
  store.settleDelivery(sending,{status:'sent',messageId:'in-flight-placeholder'},0,3)
  assert.equal(store.claimDelivery(Date.now()).editMessageId,'in-flight-placeholder')
})

test('a lost progress edit receipt or process restart never replays it or allows a late phase to overwrite the final reply',async t=>{
  const {store}=await repository(t);store.accept(event('lost-edit'),settings);const entry=store.claim([])
  store.progress(entry,'first');const initial=store.claimDelivery(Date.now());store.settleDelivery(initial,{status:'sent',messageId:'placeholder'},0,3)
  store.progress(entry,'phase');const edit=store.claimDelivery(Date.now());assert.equal(edit.editMessageId,'placeholder')
  store.recoverDeliveries();store.progress(entry,'late phase')
  store.finish(entry,'completed',[{msgType:'post',content:'final'}]);const final=store.claimDelivery(Date.now())
  assert.equal(final.content,'final');assert(!final.editMessageId)
  assert.equal(store.status(entry.sessionKey).unknown,0)
})

test('Feishu edits the known Markdown post with PUT and accepts a success receipt without a new message ID',async t=>{
  const calls=[],server=createServer(async(req,res)=>{let raw='';for await(const chunk of req)raw+=chunk;calls.push({method:req.method,url:req.url,body:JSON.parse(raw)});res.setHeader('Content-Type','application/json');res.end(JSON.stringify(req.url.includes('/auth/')?{code:0,tenant_access_token:'fixture',expire:3600}:{code:0,data:{}}))})
  server.listen(0,'127.0.0.1');await once(server,'listening');cleanup(t,()=>new Promise(resolve=>{server.closeAllConnections();server.close(resolve)}))
  const api=new FeishuApi({appId:'fixture',secret:async()=>'fixture',origin:`http://127.0.0.1:${server.address().port}`,timeoutMs:1000})
  const result=await api.send({id:'update',chatId:'chat',replyTo:'input',threadId:'thread',msgType:'post',content:'{"zh_cn":{"content":[[{"tag":"md","text":"**完成**"}]]}}',attempts:1,editMessageId:'known-message'})
  assert.deepEqual(result,{status:'sent',messageId:'known-message'});assert.equal(calls[1].method,'PUT');assert.equal(calls[1].url,'/open-apis/im/v1/messages/known-message');assert(!('uuid'in calls[1].body));assert(!('reply_in_thread'in calls[1].body))
})

test('channel schema 1 upgrades non-destructively to pinned-session schema 3 while retaining queued messages',async t=>{
  const {store,filename}=await repository(t);store.accept(event('migration'),settings);store.release()
  const old=new DatabaseSync(filename);old.exec('ALTER TABLE outbox DROP COLUMN purpose; ALTER TABLE outbox DROP COLUMN edit_id; ALTER TABLE inbox DROP COLUMN pinned_spec; PRAGMA user_version=1');old.close()
  const restored=new SqliteChannelRepository(filename,settings.appId,settings.leaseMs);cleanup(t,()=>restored.release())
  assert.equal(restored.claim([]).messageId,'om_migration');assert.equal(inspection(filename,'PRAGMA user_version')[0].user_version,3)
})

test('a natural-language answer bypasses the occupied conversation and is consumed by its own pending question',async t=>{
  const {store,filename}=await repository(t),broker=new Interactions(),calls=[];let waiting=false,current
  const agent=fakeAgent({run:async(spec,input)=>{calls.push(input.text);current={userId:'ou_alice',chatId:'oc_chat',sessionId:spec.id,cwd:spec.cwd,origin:'feishu'};const answer=await broker.ask(current,'天气要查哪个城市？',[],false,async()=>{waiting=true});return{status:'completed',text:answer}},cancel(){if(current)broker.cancelSession(current.sessionId)}})
  const channel=new FeishuChannel({repository:store,agent,delivery:{send:async()=>({status:'sent',messageId:'sent'})},features:{routeInput:(actor,text)=>broker.routeInput(actor,text),cancelQuestions:actor=>broker.cancelActor(actor),invoke:async(_name,{actor,text})=>{const split=text.indexOf(' ');const accepted=broker.answer(actor,text.slice(0,split),text.slice(split+1));return{status:accepted?'completed':'failed',text:accepted?'回答已提交。':'回答已失效。'}}}},settings)
  channel.start();cleanup(t,()=>channel.stop());channel.accept(event('ask-city'));await eventually(()=>waiting)
  channel.accept(event('city-answer',{text:'回答：济南'}))
  await eventually(()=>inspection(filename,"SELECT state FROM inbox WHERE message_id='om_city-answer'")[0]?.state==='completed',500)
  assert.deepEqual(calls,['ask-city'])
})

test('restart never replays an interrupted natural confirmation against a later pending operation',async t=>{
  const {store,filename}=await repository(t);store.accept(event('interrupted-confirmation',{kind:'confirmation',text:'confirm schedule frozen-token'}),settings);store.claim([])
  let invoked=0
  const channel=new FeishuChannel({repository:store,agent:fakeAgent(),delivery:{send:async()=>({status:'sent',messageId:'sent'})},features:{invoke:async()=>{invoked++;return{status:'completed',text:'must-not-repeat'}}}},settings)
  channel.start();cleanup(t,()=>channel.stop());await eventually(()=>inspection(filename,"SELECT state FROM inbox WHERE message_id='om_interrupted-confirmation'")[0]?.state==='unknown')
  assert.equal(invoked,0)
})

test('unanswered cards keep their original session while plain new messages continue, and late answers return to the original request',async t=>{
  const {store,filename}=await repository(t),broker=new Interactions(),calls=[],prepared=[],contexts=[];let original,card
  const agent=fakeAgent({prepare:async(spec,actor)=>{assert.equal(actor.sessionId,spec.id);prepared.push(spec)},context:async(spec,id,text)=>contexts.push({spec,id,text}),
    run:async(spec,input)=>{calls.push({spec,text:input.text});if(input.text!=='ask-city')return{status:'completed',text:'new topic done'}
      original=input.actor;const answer=await broker.ask(original,'城市？',['济南','北京'],false,async question=>{card=question;await input.question(question)})
      return{status:'completed',text:'original answer: '+answer}},cancel:id=>broker.cancelSession(id)})
  const channel=new FeishuChannel({repository:store,agent,delivery:{send:async message=>({status:'sent',messageId:'sent-'+message.id})},features:{waitingSessions:()=>broker.waitingSessions(),routeInput:(actor,text)=>broker.routeInput(actor,text),invoke:async(_name,{actor,text})=>{const split=text.indexOf(' '),accepted=broker.answer(actor,text.slice(0,split),text.slice(split+1));return{status:accepted?'completed':'failed',text:accepted?'回答已提交。':'回答失效。'}}}}, {...settings,maxConcurrentSessions:1})
  channel.start();cleanup(t,()=>channel.stop());channel.accept(event('ask-city'));await eventually(()=>card&&inspection(filename,"SELECT state FROM inbox WHERE message_id='om_ask-city'")[0]?.state==='waiting')
  channel.accept(event('unprefixed',{text:'你好'}));await eventually(()=>inspection(filename,"SELECT state FROM inbox WHERE message_id='om_unprefixed'")[0]?.state==='completed')
  assert.equal(prepared.length,1);assert.equal(prepared[0].parentSessionId,original.sessionId)
  assert.equal(inspection(filename,"SELECT state FROM inbox WHERE message_id='om_ask-city'")[0].state,'waiting')
  assert.deepEqual(broker.waitingSessions(),[original.sessionId]);assert.notEqual(calls[1].spec.id,original.sessionId)
  channel.accept(event('another',{text:'随便再聊两句'}));await eventually(()=>calls.length===3);assert.equal(calls[2].spec.id,calls[1].spec.id)
  channel.accept(event('late-answer',{text:'济南'}));await eventually(()=>inspection(filename,"SELECT state FROM inbox WHERE message_id='om_ask-city'")[0]?.state==='completed')
  assert.equal(inspection(filename,"SELECT json_extract(pinned_spec,'$.id') AS id FROM inbox WHERE message_id='om_late-answer'")[0].id,original.sessionId)
  assert.equal(calls.length,3);await eventually(()=>contexts.length===1);assert.equal(contexts[0].spec.id,calls[1].spec.id);assert.match(contexts[0].text,/济南/)
})

test('restarting a waiting question invalidates it without replaying a tool or blocking the next ordinary request',async t=>{
  const {store,filename}=await repository(t);store.accept(event('waiting-question'),settings);const entry=store.claim([]);store.waiting(entry)
  let recovered=0,runs=0;const channel=new FeishuChannel({repository:store,agent:fakeAgent({recover:async()=>{recovered++;return{status:'completed',text:'must not run'}},run:async()=>{runs++;return{status:'completed',text:'new'}}}),delivery:{send:async()=>({status:'sent',messageId:'sent'})}},settings)
  channel.start();cleanup(t,()=>channel.stop());await eventually(()=>inspection(filename,"SELECT state FROM inbox WHERE message_id='om_waiting-question'")[0]?.state==='cancelled')
  channel.accept(event('after-restart'));await eventually(()=>runs===1);assert.equal(recovered,0)
})

test('a recovered execution uses its receipt without resubmitting; queued input remains processable', async t => {
  const { store, filename } = await repository(t)
  store.accept(event('previous'), settings)
  const previous = store.claim([])
  store.release()
  const restarted = new SqliteChannelRepository(filename, settings.appId, settings.leaseMs)
  cleanup(t, () => restarted.release())
  const submitted = [], recovered = [], sent = []
  const channel = await start(t, restarted, fakeAgent({
    run: async (_spec, input) => { submitted.push(input.id); return { status: 'completed', text: 'next' } },
    recover: async (_spec, input) => { recovered.push(input.id); return { status: 'completed', text: 'previous result' } },
  }), { send: async message => { sent.push(message); return { status: 'sent', messageId: 'sent_1' } } })
  channel.accept(event('next'))
  await eventually(() => sent.length === 2)
  assert.deepEqual(recovered, [previous.inputId])
  assert.equal(submitted.length, 1)
})

test('unknown execution blocks dependent model requests, while status remains usable', async t => {
  const { store, filename } = await repository(t)
  store.accept(event('previous'), settings); store.claim([])
  store.release()
  const restarted = new SqliteChannelRepository(filename, settings.appId, settings.leaseMs)
  cleanup(t, () => restarted.release())
  let calls = 0
  const sent = []
  const channel = await start(t, restarted, fakeAgent({ run: async () => { calls++; return { status: 'completed', text: 'must not run' } } }), {
    send: async message => { sent.push(message); return { status: 'sent', messageId: `sent_${sent.length}` } },
  })
  channel.accept(event('dependent'))
  channel.accept(event('status', { kind: 'status', text: '/status' }))
  await eventually(() => sent.some(message => message.msgType === 'interactive'))
  assert.equal(calls, 0)
  assert.equal(inspection(filename, "SELECT * FROM inbox WHERE state='unknown'").length, 1)
})

test('same-session work is serial and stop/status bypass a full active slot without admitting parallel work', async t => {
  const { store } = await repository(t)
  const calls = [], sent = []
  let finish
  const agent = fakeAgent({
    run: async (_spec, input) => {
      calls.push(input.text)
      if (input.text === 'hold') return new Promise(resolve => { finish = resolve })
      return { status: 'completed', text: 'after' }
    },
    cancel: () => finish?.({ status: 'cancelled', text: 'stopped' }), status: () => ({ active: Boolean(finish) }),
  })
  const channel = await start(t, store, agent, { send: async message => { sent.push(message); return { status: 'sent', messageId: `sent_${sent.length}` } } }, { ...settings, maxConcurrentSessions: 1 })
  channel.accept(event('hold'))
  await eventually(() => calls.length === 1)
  channel.accept(event('after'))
  channel.accept(event('status', { kind: 'status', text: '/status' }))
  await eventually(() => sent.some(message => message.msgType === 'interactive'))
  assert.deepEqual(calls, ['hold'])
  channel.accept(event('stop', { text: '/stop' }))
  await eventually(() => calls.length === 2)
  assert.deepEqual(calls, ['hold', 'after'])
})

test('rate-limited delivery retries its frozen UUID but an unknown delivery never reruns the Agent', async t => {
  const { store, filename } = await repository(t)
  let calls = 0
  const attempts = []
  const channel = await start(t, store, fakeAgent({ run: async () => { calls++; return { status: 'completed', text: 'result' } } }), {
    send: async message => { attempts.push(message.id); return attempts.length === 1 ? { status: 'retry', code: 'limited' } : { status: 'unknown', code: 'connection_lost' } },
  })
  channel.accept(event('run'))
  await eventually(() => inspection(filename, "SELECT * FROM outbox WHERE state='unknown'").length === 1)
  await new Promise(resolve => setTimeout(resolve, 80))
  assert.equal(calls, 1)
  assert.equal(attempts.length, 2)
  assert.equal(attempts[0], attempts[1])
})

test('status-card callback is bound to original operator, chat, message and expiry, and consumed once', async t => {
  const { store } = await repository(t)
  store.accept(event('status', { kind: 'status', text: '/status' }), settings)
  const entry = store.claim([])
  const card = JSON.parse(store.statusCard(entry, 'state', 10000))
  store.finish(entry, 'completed', [{ msgType: 'interactive', content: JSON.stringify(card) }])
  const outgoing = store.claimDelivery(Date.now())
  store.settleDelivery(outgoing, { status: 'sent', messageId: 'om_card' }, 0, 3)
  const callback = event('callback', { kind: 'status', text: '/status', messageId: 'om_card', actionToken: card.elements[1].actions[0].value.token })
  assert.equal(store.accept({ ...callback, actor: { ids: ['ou_bob'] } }, settings).rejected, true)
  assert.equal(store.accept({ ...callback, chatId: 'oc_other' }, settings).rejected, true)
  assert.equal(store.accept({ ...callback, messageId: 'om_wrong' }, settings).rejected, true)
  assert.equal(store.accept(callback, settings).accepted, true)
  assert.equal(store.accept(callback, settings).duplicate, true)
  const expired = JSON.parse(store.statusCard(entry, 'old state', 1))
  await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(store.accept({ ...callback, eventId: 'new', actionToken: expired.elements[1].actions[0].value.token }, settings).rejected, true)
})

test('crash during send becomes uncertain and is not silently reset to pending', async t => {
  const { store, filename } = await repository(t)
  store.accept(event('run'), settings)
  const entry = store.claim([])
  store.finish(entry, 'completed', [{ msgType: 'post', content: '{}' }])
  store.claimDelivery(Date.now()); store.release()
  const restarted = new SqliteChannelRepository(filename, settings.appId, settings.leaseMs)
  cleanup(t, () => restarted.release())
  restarted.recoverDeliveries()
  assert.equal(restarted.claimDelivery(Date.now()), undefined)
  assert.equal(inspection(filename, "SELECT * FROM outbox WHERE state='unknown'").length, 1)
})

test('Feishu API uses fixed origins, application token, thread routing and immutable delivery UUID', async t => {
  assert.throws(() => apiOrigin('https://attacker.example'))
  assert.throws(() => apiOrigin('https://open.feishu.cn@attacker.example'))
  const received = []
  let tokenCalls = 0
  const server = createServer(async (request, response) => {
    let raw = ''; for await (const chunk of request) raw += chunk
    const body = JSON.parse(raw)
    response.setHeader('Content-Type', 'application/json')
    if (request.url.includes('/auth/')) { tokenCalls++; response.end(JSON.stringify({ code: 0, tenant_access_token: 'test-access-token', expire: 7200 })); return }
    received.push({ url: request.url, body, auth: request.headers.authorization })
    if (received.length === 1) { response.writeHead(429).end('{}'); return }
    response.end(JSON.stringify({ code: 0, data: { message_id: 'om_sent' } }))
  })
  server.listen(0, '127.0.0.1'); await once(server, 'listening')
  cleanup(t, () => new Promise(resolve => { server.closeAllConnections(); server.close(resolve) }))
  const api = new FeishuApi({ appId: 'cli_test', secret: async () => 'test-secret', origin: apiOrigin(`http://127.0.0.1:${server.address().port}`), timeoutMs: 1000 })
  const message = { id: 'stable-uuid', chatId: 'oc_chat', replyTo: 'om_source', threadId: 'thread_1', msgType: 'post', content: '{}', attempts: 0 }
  assert.equal((await api.send(message)).status, 'retry')
  assert.equal((await api.send(message)).status, 'sent')
  assert.equal(tokenCalls, 1)
  assert.equal(received[0].body.uuid, received[1].body.uuid)
  assert.equal(received[1].body.reply_in_thread, true)
  assert.equal(received[1].auth, 'Bearer test-access-token')
})

test('Feishu API distinguishes confirmed rejection, retry and unknown receipt without blind write retry', async t => {
  let behavior = 'ok', sends = 0, tokens = 0
  const server = createServer(async (request, response) => {
    for await (const _chunk of request) { /* Drain the request without logging credentials. */ }
    response.setHeader('Content-Type', 'application/json')
    if (request.url.includes('/auth/')) { tokens++; response.end(JSON.stringify({ code: 0, tenant_access_token: 'loopback-token', expire: 7200 })); return }
    sends++
    if (behavior === 'disconnect') { response.destroy(); return }
    if (behavior === 'bad-json') { response.end('bad JSON'); return }
    if (behavior === 'http500') { response.writeHead(500).end('{}'); return }
    if (behavior === 'http403') { response.writeHead(403).end('{}'); return }
    if (behavior === 'http429') { response.writeHead(429).end('{}'); return }
    response.end(JSON.stringify(behavior === 'expired' ? { code: 99991663 } : behavior === 'denied' ? { code: 99991400 }
      : behavior === 'no-receipt' ? { code: 0, data: {} } : { code: 0, data: { message_id: 'sent' } }))
  })
  server.listen(0, '127.0.0.1'); await once(server, 'listening')
  cleanup(t, () => new Promise(resolve => { server.closeAllConnections(); server.close(resolve) }))
  const api = new FeishuApi({ appId: 'test', secret: async () => 'test-secret', origin: apiOrigin(`http://127.0.0.1:${server.address().port}`), timeoutMs: 500 })
  const message = { id: 'stable-id', chatId: 'chat', replyTo: '', threadId: '', msgType: 'post', content: '{}', attempts: 0 }
  for (const [scenario, status] of [['http403', 'failed'], ['denied', 'failed'], ['http429', 'retry'], ['http500', 'unknown'],
    ['bad-json', 'unknown'], ['no-receipt', 'unknown'], ['disconnect', 'unknown'], ['expired', 'retry'], ['ok', 'sent']]) {
    behavior = scenario; const before = sends
    assert.equal((await api.send(message)).status, status, scenario)
    assert.equal(sends, before + 1, 'One send attempt must never perform an automatic second write')
  }
  assert.equal(tokens, 2, 'An explicitly expired token must be refreshed on the next controlled attempt')
})

test('Feishu API coalesces concurrent token requests and does not send on authentication failure', async t => {
  let tokens = 0, sends = 0, rejectAuth = false
  const server = createServer(async (request, response) => {
    for await (const _chunk of request) {}
    response.setHeader('Content-Type', 'application/json')
    if (request.url.includes('/auth/')) {
      tokens++; await new Promise(resolve => setTimeout(resolve, 20))
      response.end(JSON.stringify(rejectAuth ? { code: 1 } : { code: 0, tenant_access_token: 'loopback-token', expire: 7200 })); return
    }
    sends++; response.end(JSON.stringify({ code: 0, data: { message_id: `sent-${sends}` } }))
  })
  server.listen(0, '127.0.0.1'); await once(server, 'listening')
  cleanup(t, () => new Promise(resolve => { server.closeAllConnections(); server.close(resolve) }))
  const options = { appId: 'test', secret: async () => 'test-secret', origin: apiOrigin(`http://127.0.0.1:${server.address().port}`), timeoutMs: 1000 }
  const api = new FeishuApi(options)
  const message = id => ({ id, chatId: 'chat', replyTo: '', threadId: '', msgType: 'post', content: '{}', attempts: 0 })
  assert((await Promise.all(['a', 'b', 'c'].map(id => api.send(message(id))))).every(row => row.status === 'sent'))
  assert.equal(tokens, 1); assert.equal(sends, 3)
  rejectAuth = true
  assert.equal((await new FeishuApi(options).send(message('denied'))).status, 'retry')
  assert.equal(sends, 3)
})

test('uploaded artifact receipts survive a confirmed send failure; retry restores only owned failed delivery and never resets unknown',async t=>{
  const directory=await temporaryDirectory(t),filename=path.join(directory,'channel.sqlite'),file=path.join(directory,'artifact.txt');await writeFile(file,'synthetic artifact')
  let store=new SqliteChannelRepository(filename,settings.appId,settings.leaseMs);cleanup(t,()=>store.release())
  store.accept(event('source'),{...settings,workspace:directory});const entry=store.claim([]),actor={userId:'ou_alice',chatId:entry.chatId,threadId:entry.threadId,sessionId:entry.spec.id,cwd:directory,origin:'feishu',privateChat:true,sourceId:entry.id};store.finish(entry,'completed',[])
  const content=JSON.stringify({path:file,sha256:createHash('sha256').update('synthetic artifact').digest('hex')}),ids=store.enqueueOnce('execution:artifact',actor,[{msgType:'file',content}])
  let uploads=0,sends=0
  const server=createServer(async(req,res)=>{for await(const _ of req){}res.setHeader('Content-Type','application/json');if(req.url.includes('/auth/'))return res.end(JSON.stringify({code:0,tenant_access_token:'test',expire:7200}));if(req.url.endsWith('/files')){uploads++;return res.end(JSON.stringify({code:0,data:{file_key:'uploaded-once'}}))}sends++;if(sends===1)return res.writeHead(429).end('{}');res.end(JSON.stringify({code:0,data:{message_id:'sent-file'}}))})
  server.listen(0,'127.0.0.1');await once(server,'listening');cleanup(t,()=>new Promise(resolve=>{server.closeAllConnections();server.close(resolve)}))
  const api=new FeishuApi({appId:'test',secret:async()=>'synthetic',origin:'http://127.0.0.1:'+server.address().port,timeoutMs:1000})
  let message=store.claimDelivery(Date.now()),result=await api.send(message,key=>store.uploaded(message,key));assert.equal(result.status,'retry');store.settleDelivery(message,result,0,1);assert.equal(store.externalStatus(actor,ids)[0].state,'failed')
  store.release();store=new SqliteChannelRepository(filename,settings.appId,settings.leaseMs)
  assert.throws(()=>store.retryExternal({...actor,userId:'ou_bob'},ids),/source_mismatch/);store.retryExternal(actor,ids);message=store.claimDelivery(Date.now());assert.equal(message.uploadedFileKey,'uploaded-once')
  result=await api.send(message,key=>store.uploaded(message,key));store.settleDelivery(message,result,0,3);assert.equal(uploads,1);assert.equal(sends,2);assert.equal(store.externalStatus(actor,ids)[0].messageId,'sent-file')
  const uncertain=store.enqueueOnce('execution:uncertain',actor,[{msgType:'post',content:'{}'}]);message=store.claimDelivery(Date.now());store.settleDelivery(message,{status:'unknown',code:'lost_receipt'},0,3);store.retryExternal(actor,uncertain);assert.equal(store.externalStatus(actor,uncertain)[0].state,'unknown')
})
