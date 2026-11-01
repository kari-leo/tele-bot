/** Explicit live-service checks only. No default run or automatic credential import. */
import {mkdtemp,mkdir,writeFile,readFile,rename,stat} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import path from 'node:path'
import {randomUUID,createHash} from 'node:crypto'
import assert from 'node:assert/strict'
import {root} from '../../tooling/common.mjs'
import {officialHost} from '../../tooling/official-host.mjs'
import {readReference} from '../../plugins/feishu/lib/adapters/reference-env.js'
import {FeishuApi,apiOrigin} from '../../plugins/feishu/lib/adapters/feishu-api.js'
import {FeishuHttp} from '../../plugins/feishu/lib/business/adapters/http.js'
import {Resources} from '../../plugins/feishu/lib/business/domain/resources.js'
import {OperationLedger} from '../../plugins/feishu/lib/business/adapters/ledger.js'
async function liveAccess(filename){const reference=await readReference(filename),origin=apiOrigin(reference.FEISHU_API_BASE_URL||'https://open.feishu.cn');assert.equal(origin,'https://open.feishu.cn');return{reference,origin}}
export async function inspectReference(filename) {
  const allowed = ['FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_ADMIN_USER_IDS', 'FEISHU_VERIFICATION_TOKEN', 'FEISHU_ENCRYPT_KEY']
  const presence = Object.fromEntries(allowed.map(key => [key, false]))
  for (const line of (await readFile(filename, 'utf8')).split(/\r?\n/)) {
    const match = /^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*?)\s*$/.exec(line)
    if (!match || !allowed.includes(match[1])) continue
    const value = match[2].replace(/^(?:"([\s\S]*)"|'([\s\S]*)')$/, '$1$2')
    presence[match[1]] = value.length > 0
  }
  return { presence, sourceAuthenticationConfigured: presence.FEISHU_VERIFICATION_TOKEN || presence.FEISHU_ENCRYPT_KEY,
    applicationConfigured: presence.FEISHU_APP_ID && presence.FEISHU_APP_SECRET && presence.FEISHU_ADMIN_USER_IDS }
}
async function bitable(values){
assert(values.reference && values.base, 'Use --reference <external env> --base <human-authorized Base URL> [--table id] [--write]')
const url = new URL(values.base)
assert(url.protocol === 'https:' && !url.username && !url.password && (url.hostname === 'feishu.cn' || url.hostname.endsWith('.feishu.cn')))
const base = url.pathname.match(/^\/base\/([\w-]+)\/?$/)?.[1]
assert(base, 'A specific Base URL is required; tenant-wide discovery is not supported')
const {reference,origin}=await liveAccess(values.reference)
assert.equal(origin, 'https://open.feishu.cn', 'Real test credentials only go to the official Feishu API')
assert(reference.FEISHU_APP_ID && reference.FEISHU_APP_SECRET)
const work = await mkdtemp(path.join(tmpdir(), 'telebot-bitable-live-'))
const actor = { userId: reference.FEISHU_ADMIN_USER_IDS?.split(/[,;\s]+/).find(Boolean), chatId: 'explicit-local-live-test', sessionId: randomUUID(), cwd: work, privateChat: true, origin: 'web' }
assert(actor.userId, 'A configured administrator is required')
const marker = 'telebot-harness-live-' + randomUUID(), owned = new Set(), checks = []
const report = { date: new Date().toISOString(), realFeishu: true, realMessagesSent: false, paidModels: false, mode: values.write ? 'write-own-test-records' : 'inspect-only', base, status: 'running', checks, remainingTestRecords: [], cleanup: 'not_needed', recordContentsLogged: false, limits: ['No real chat/card/OAuth user tests', 'No table/schema changes', 'No bulk 500-row load or forced platform rate-limit'] }
const check = (name, details = {}) => { checks.push({ name, ...details }); console.log('PASS ' + name) }
const app = new FeishuApi({ appId: reference.FEISHU_APP_ID, secret: async () => reference.FEISHU_APP_SECRET, origin, timeoutMs: 15000 })
const http = new FeishuHttp(origin, async () => (await app.tenantGrant()).token, 15000)
let tableId, fieldName, ledger, transportWrites = 0
const prefix = `/open-apis/bitable/v1/apps/${base}/tables`
const port = { request: async (method, route, body, signal, write = method !== 'GET') => {
  assert(route.startsWith(prefix + '/') || route.startsWith(prefix + '?'), 'test_scope_violation')
  if (write) {
    assert(values.write && tableId && route.startsWith(prefix + '/' + tableId + '/records/'), 'write_scope_violation')
    assert(Array.isArray(body?.records) && body.records.length === 1, 'single_test_record_only')
    if (route.endsWith('/batch_create')) assert(String(body.records[0].fields?.[fieldName] ?? '').startsWith(marker), 'unique_marker_required')
    else for (const record of body.records) assert(owned.has(typeof record === 'string' ? record : record.record_id), 'preexisting_record_write_denied')
    transportWrites++
  }
  const data = await http.request(method, route, body, signal, write)
  if (write && route.endsWith('/batch_create')) for (const record of data.records ?? []) if (record.record_id) owned.add(record.record_id)
  if (write && route.endsWith('/batch_delete')) for (const record of data.records ?? []) if (record.deleted) owned.delete(record.record_id)
  return data
} }
const signal = () => AbortSignal.timeout(30000)
try {
  const grant = await app.tenantGrant()
  const resource = { alias: 'human-authorized-live-base', kind: 'bitable', id: base, identity: 'tenant', tokenRef: 'FEISHU_APP_TENANT', expiresAt: grant.expiresAt, capabilities: ['bitable.read','bitable.create','bitable.update','bitable.delete'] }
  const service = new Resources([resource], [actor.userId], () => port)
  const inventory = await service.execute(actor, { resource: resource.alias, action: 'inspect', params: {} }, signal())
  assert(inventory.tables.complete && inventory.tables.items.length)
  tableId = values.table || url.searchParams.get('table') || inventory.tables.items[0].table_id
  assert(inventory.tables.items.some(row => row.table_id === tableId), 'table_not_in_authorized_base')
  resource.tableId = tableId
  const structure = await service.execute(actor, { resource: resource.alias, action: 'inspect', params: {} }, signal())
  assert(structure.fields.complete)
  fieldName = structure.fields.items.find(row => row.type === 1 && !row.is_computed)?.field_name
  check('official app identity can inspect only the authorized Base and enumerate its tables/fields', { tableCount: inventory.tables.items.length, fieldCount: structure.fields.items.length })
  report.tableId = tableId
  if (values.write) {
    assert(fieldName, 'test_table_needs_existing_text_field_no_schema_changes_permitted')
    ledger = new OperationLedger(path.join(work, 'operations.sqlite'))
    const write = async (action, records) => {
      const operation = { resource: resource.alias, action, params: { records } }
      const before = transportWrites, plan = await ledger.preview(service, actor, operation, signal())
      assert.equal(transportWrites, before, 'preview_must_not_write')
      const result = await ledger.confirm(service, actor, plan.token, signal())
      assert.equal(result.status, 'completed', 'write_or_verification_not_confirmed')
      return { plan, result }
    }
    const created = await write('create', [{ fields: { [fieldName]: marker } }]), recordId = created.result.receipt.records[0].record_id
    assert(owned.has(recordId)); check('create freezes a preview and verifies a uniquely marked new test record')
    const beforeReplay = transportWrites
    assert.equal((await ledger.confirm(service, actor, created.plan.token, signal())).status, 'completed')
    assert.equal(transportWrites, beforeReplay); check('repeated confirmation makes no second API write')
    const found = await service.execute(actor, { resource: resource.alias, action: 'query', params: { filter: { conjunction: 'and', conditions: [{ field_name: fieldName, operator: 'is', value: [marker] }] }, field_names: [fieldName] } }, signal())
    assert(found.complete && found.items.some(row => row.record_id === recordId)); check('search query returns the owned test record with a complete filtered result')
    await write('update', [{ record_id: recordId, fields: { [fieldName]: marker + '-updated' } }]); check('update verifies actual remote readback')
    await write('update', [{ record_id: recordId, fields: { [fieldName]: null } }]); check('clear verifies the platform empty-field representation')
    const pending = await ledger.preview(service, actor, { resource: resource.alias, action: 'update', params: { records: [{ record_id: recordId, fields: { [fieldName]: marker + '-pending' } }] } }, signal())
    await assert.rejects(ledger.confirm(service, { ...actor, userId: 'not-authorized-test-user' }, pending.token, signal()), /binding/)
    const expiry = ledger.status(actor, pending.token).expiresAt
    const supplement = await ledger.supplement(service, actor, pending.token, { [fieldName]: marker + '-supplemented' }, signal())
    assert.equal(supplement.expiresAt, expiry); assert.equal(ledger.status(actor, pending.token).status, 'superseded')
    assert.equal((await ledger.confirm(service, actor, supplement.token, signal())).status, 'completed'); check('actor binding and supplementary fields preserve the frozen scope and original expiry')
    const conflict = await ledger.preview(service, actor, { resource: resource.alias, action: 'update', params: { records: [{ record_id: recordId, fields: { [fieldName]: marker + '-stale' } }] } }, signal())
    await port.request('POST', prefix + '/' + tableId + '/records/batch_update', { records: [{ record_id: recordId, fields: { [fieldName]: marker + '-concurrent' } }] }, signal())
    const beforeConflict = transportWrites
    await assert.rejects(ledger.confirm(service, actor, conflict.token, signal()), /conflict/)
    assert.equal(transportWrites, beforeConflict); assert(ledger.cancel(actor, conflict.token)); check('optimistic conflict rejects the stale preview without another write; cancellation consumes it')
    await write('delete', [{ record_id: recordId }]); assert(!owned.has(recordId)); check('delete requires confirmation and the API confirms deletion of only the owned test record')
    report.cleanup = 'completed'
  }
  report.status = 'passed'
} catch (error) {
  report.status = 'failed'
  report.error = error instanceof Error && /^[a-z_\d]+$/i.test(error.message) ? error.message : 'live_verification_failed'
} finally {
  if (owned.size) {
    report.cleanup = 'incomplete'
    for (const recordId of [...owned]) {
      try {
        // A fresh frozen delete checks the current row before cleanup; never touch unowned rows.
        const currentGrant = await app.tenantGrant()
        const cleanupResource = { alias: 'cleanup-owned-row', kind: 'bitable', id: base, tableId, identity: 'tenant', tokenRef: 'FEISHU_APP_TENANT', expiresAt: currentGrant.expiresAt, capabilities: ['bitable.delete'] }
        const cleanupService = new Resources([cleanupResource], [actor.userId], () => port)
        const plan = await ledger.preview(cleanupService, actor, { resource: cleanupResource.alias, action: 'delete', params: { records: [{ record_id: recordId }] } }, signal())
        await ledger.confirm(cleanupService, actor, plan.token, signal())
      } catch {}
    }
    report.cleanup = owned.size ? 'incomplete' : 'completed_after_failure'
  }
  report.remainingTestRecords = [...owned]
  report.transportWrites = transportWrites
  if (owned.size) report.status = 'cleanup_failed'
  ledger?.close()
  await mkdir(path.join(root, '.runtime'), { recursive: true })
  const filename = path.join(root, '.runtime/feishu-live-bitable-latest.json')
  await writeFile(filename, JSON.stringify(report, null, 2) + '\n')
  console.log(JSON.stringify({ status: report.status, checks: checks.length, cleanup: report.cleanup, remainingTestRecords: report.remainingTestRecords, error: report.error, report: filename }))
}
if (report.status !== 'passed') process.exitCode = 1

}
async function chat(values){
assert(values.reference && /^oc_[A-Za-z0-9]+$/.test(values.chat ?? ''), 'Use --reference <external env> --chat <explicit authorized chat_id> [--send] [--callback <public URL>]')
const {reference,origin}=await liveAccess(values.reference)
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

}
export async function verifyLive(values){
  if(!['preflight','bitable','chat'].includes(values.live))throw Error('Use --live preflight|bitable|chat')
  if(!values.reference)throw Error('An external --reference path is required; never paste credentials')
  if(values.live!=='chat'&&(values.send||values.chat||values.callback))throw Error('Chat/send options only apply to --live chat')
  if(values.live!=='bitable'&&(values.write||values.base||values.table))throw Error('Base/write options only apply to --live bitable')
  if(values.live==='bitable')return bitable(values)
  if(values.live==='chat')return chat(values)
  assert(values.workspace&&path.isAbsolute(values.workspace),'Absolute --workspace required')
  const host=await officialHost(),report=await inspectReference(values.reference),workspaceReady=(await stat(values.workspace)).isDirectory()
  console.log(JSON.stringify({harnessVersion:host.version,workspaceReady,...report,liveApiCalled:false},null,2))
  if(!workspaceReady||!report.sourceAuthenticationConfigured||!report.applicationConfigured)process.exitCode=2
}
