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
function inspection(filename, query) {
  const db = new DatabaseSync(filename, { readOnly: true })
  try { return db.prepare(query).all() } finally { db.close() }
}
function fakeAgent(overrides = {}) {
  return { run: async () => ({ status: 'completed', text: 'done' }), recover: async () => ({ status: 'unknown', text: 'uncertain' }),
    cancel() {}, status: () => ({ active: false }), dispose: async () => {}, ...overrides }
}
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
