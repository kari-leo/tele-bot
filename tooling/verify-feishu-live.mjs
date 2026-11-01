/** Opt-in outbound acceptance for one human-authorized chat. Never enumerate/read unrelated history. */
import { parseArgs } from 'node:util'
import { mkdtemp, mkdir, writeFile, readFile, rename } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { randomUUID, createHash } from 'node:crypto'
import assert from 'node:assert/strict'
import { root } from './common.mjs'
import { readReference } from '../plugins/feishu/lib/adapters/reference-env.js'
import { FeishuApi, apiOrigin } from '../plugins/feishu/lib/adapters/feishu-api.js'
import { FeishuHttp } from '../plugins/feishu/lib/business/adapters/http.js'
const { values } = parseArgs({ options: { reference: {type:'string'}, chat: {type:'string'}, send: {type:'boolean',default:false}, callback: {type:'string'} } })
assert(values.reference && /^oc_[A-Za-z0-9]+$/.test(values.chat ?? ''), 'Use --reference <external env> --chat <explicit authorized chat_id> [--send] [--callback <public URL>]')
const reference = await readReference(values.reference), origin = apiOrigin(reference.FEISHU_API_BASE_URL || 'https://open.feishu.cn')
assert.equal(origin,'https://open.feishu.cn')
const report={date:new Date().toISOString(),realFeishu:true,paidModels:false,scope:'one-explicit-chat-own-messages-only',chatId:values.chat,sendAuthorized:values.send,checks:[],messages:[],status:'running',messageCleanup:'retained_as_visible_test_evidence',incomingPlatformDeliveryVerified:false}
const reportFile=path.join(root,'.runtime/feishu-live-chat-latest.json')
await mkdir(path.dirname(reportFile),{recursive:true})
try{const previous=JSON.parse(await readFile(reportFile,'utf8'));if(previous.chatId===values.chat&&previous.sendAuthorized){report.messages=previous.messages??[];report.messageAttempts=previous.messageAttempts??[];report.fixtureNonce=previous.fixtureNonce}}catch(error){if(error.code!=='ENOENT')throw error}
report.messageAttempts??=[];report.fixtureNonce??=randomUUID().slice(0,8)
async function persist(){const temporary=reportFile+'.'+randomUUID()+'.tmp';await writeFile(temporary,JSON.stringify(report,null,2)+'\n',{mode:0o600});await rename(temporary,reportFile)}
const check=name=>{report.checks.push(name);console.log('PASS '+name)}
const app=new FeishuApi({appId:reference.FEISHU_APP_ID,secret:async()=>reference.FEISHU_APP_SECRET,origin,timeoutMs:15000})
const http=new FeishuHttp(origin,async()=>(await app.tenantGrant()).token,15000)
async function send(msgType,content){
  assert(values.send,'send_requires_explicit_flag')
  const existing=report.messages.find(row=>row.type===msgType)
  if(existing)return existing.messageId
  let attempt=report.messageAttempts.find(row=>row.type===msgType)
  if(attempt&&['sending','unknown'].includes(attempt.state))throw new Error('previous_send_unknown_requires_reconciliation')
  if(attempt?.state==='sent') {
    if(!attempt.messageId)throw new Error('previous_send_receipt_requires_reconciliation')
    report.messages.push({type:msgType,messageId:attempt.messageId});await persist();return attempt.messageId
  }
  if(!attempt){attempt={type:msgType,uuid:randomUUID(),state:'pending'};report.messageAttempts.push(attempt)}
  attempt.state='sending';await persist()
  const result=await app.send({id:attempt.uuid,chatId:values.chat,replyTo:'',threadId:'',msgType,content,attempts:0})
  attempt.state=result.status
  if(result.status==='sent'){attempt.messageId=result.messageId;report.messages.push({type:msgType,messageId:result.messageId})}
  await persist()
  if(result.status!=='sent')throw new Error(result.code||'outcome_unconfirmed_no_retry')
  return result.messageId
}
async function readOwned(id){
  assert(report.messages.some(row=>row.messageId===id),'owned_message_only')
  const result=await http.request('GET','/open-apis/im/v1/messages/'+encodeURIComponent(id))
  const entry=(result.items??[result]).find(row=>row.message_id===id)
  assert(entry&&entry.chat_id===values.chat,'readback_scope_or_receipt_mismatch')
  return entry
}
try{
  try{await http.request('GET','/open-apis/im/v1/chats/'+encodeURIComponent(values.chat));check('application can access the explicitly supplied test chat')}
  catch(error){report.chatMetadata={status:'unavailable',code:error.code??'metadata_unavailable'};if(!values.send)throw error}
  if(values.send){
    const nonce=report.fixtureNonce
    const post=await send('post',JSON.stringify({zh_cn:{title:'Telebot 联调测试',content:[[{tag:'md',text:'**测试消息** · '+nonce+'\n仅验证此测试群中的富文本发送；不触发业务写入。'}]]}}))
    assert.equal((await readOwned(post)).msg_type,'post');check('production rich-text sender obtains an actual message receipt and reads back only that message')
    const card=await send('interactive',JSON.stringify({config:{wide_screen_mode:true},header:{title:{tag:'plain_text',content:'Telebot 卡片联调测试'}},elements:[{tag:'div',text:{tag:'lark_md',content:'卡片发送测试已到达。编号：'+nonce+'\n本卡片没有执行按钮，不会产生业务变更。'}}]}))
    assert.equal((await readOwned(card)).msg_type,'interactive');check('production card sender obtains an actual receipt in the same test chat')
    const work=await mkdtemp(path.join(tmpdir(),'telebot-live-file-')),filename=path.join(work,'telebot-test-'+nonce+'.txt'),bytes=Buffer.from('Telebot isolated file transport probe '+nonce+'\n','utf8'),digest=createHash('sha256').update(bytes).digest('hex')
    await writeFile(filename,bytes)
    const file=await send('file',JSON.stringify({path:filename,sha256:digest}))
    const message=await readOwned(file),content=JSON.parse(message.body.content)
    assert.equal(message.msg_type,'file');assert(content.file_key)
    const downloaded=await app.download(file,content.file_key,'file')
    assert.equal(createHash('sha256').update(downloaded.bytes).digest('hex'),digest)
    check('uploaded tiny fixture file is delivered, downloaded from its own message and verified by SHA-256')
  }
  if(values.callback){
    const url=new URL(values.callback);assert(url.protocol==='https:'&&!url.username&&!url.password&&!url.search&&!url.hash)
    try{const result=await fetch(url,{redirect:'error',signal:AbortSignal.timeout(10000)});report.publicCallback={httpStatus:result.status,method:'GET',scope:'reachability_only_not_plugin_ingress'};await result.body?.cancel()}
    catch{report.publicCallback={status:'unreachable',scope:'reachability_only_not_plugin_ingress'}}
  }
  report.status='passed'
}catch(error){report.status='failed';report.error=error instanceof Error&&/^[a-z_\d]+$/i.test(error.message)?error.message:'live_chat_verification_failed'}
finally{await persist();console.log(JSON.stringify({status:report.status,checks:report.checks.length,sentMessages:report.messages.length,error:report.error,chatMetadata:report.chatMetadata,publicCallback:report.publicCallback,report:reportFile}))}
if(report.status!=='passed')process.exitCode=1
