import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { writeFile, readFile } from 'node:fs/promises'
import path from 'node:path'
import { Interactions } from '../../plugins/core/lib/interactions.js'
import { Resources } from '../../plugins/feishu/lib/business/domain/resources.js'
import { OperationLedger } from '../../plugins/feishu/lib/business/adapters/ledger.js'
import { FeishuHttp } from '../../plugins/feishu/lib/business/adapters/http.js'
import { FeishuApi } from '../../plugins/feishu/lib/adapters/feishu-api.js'
import { FileTransfers } from '../../plugins/feishu/lib/adapters/files.js'
import { OpenCliAdviser } from '../../plugins/adviser/lib/adapters/opencli.js'
import { Adviser } from '../../plugins/adviser/lib/domain/adviser.js'
import { temporaryDirectory, cleanup } from '../support/temp-dir.mjs'

const actor = { userId: 'alice', chatId: 'chat', sessionId: 'session', cwd: '/workspace', privateChat: true, origin: 'feishu' }
const signal = () => AbortSignal.timeout(10000)
async function api(t, callback) {
  const server = createServer(callback); server.listen(0, '127.0.0.1'); await once(server, 'listening')
  cleanup(t, () => new Promise(resolve => { server.closeAllConnections(); server.close(resolve) }))
  return `http://127.0.0.1:${server.address().port}`
}

test('P3 real calendar identifiers with @ remain exact authorized resources and are encoded as one HTTP path segment', async t => {
  const requests = []
  const base = await api(t, (request, response) => {
    requests.push(request.url)
    response.setHeader('Content-Type', 'application/json')
    response.end(JSON.stringify({ code: 0, data: { items: [], has_more: false } }))
  })
  const calendar = { alias: 'calendar', kind: 'calendar', id: 'feishu.cn_fixture@group.calendar.feishu.cn', identity: 'tenant', expiresAt: Date.now() + 100000, capabilities: ['calendar.read'], tokenRef: 'FEISHU_APP_TENANT' }
  const resources = new Resources([calendar], ['alice'], () => new FeishuHttp(base, async () => 'loopback-only'))
  const result = await resources.execute(actor, { resource: 'calendar', action: 'query', params: { start_time: '1', end_time: '2' } }, signal())
  assert.equal(result.complete, true)
  assert.equal(requests.length, 1)
  assert(requests[0].startsWith('/open-apis/calendar/v4/calendars/feishu.cn_fixture%40group.calendar.feishu.cn/events?'))
  await assert.rejects(resources.execute({ ...actor, userId: 'bob' }, { resource: 'calendar', action: 'query', params: { start_time: '1', end_time: '2' } }), /not_authorized/)
  for (const value of ['../other', 'calendar/other', 'calendar%2Fother', 'calendar?other']) {
    assert.throws(() => new Resources([{ ...calendar, id: value }], ['alice'], () => null), /identifier/)
  }
  assert.throws(() => new Resources([{ ...calendar, kind: 'bitable' }], ['alice'], () => null), /identifier/)
  assert.equal(requests.length, 1)
})

test('P2 interaction answers bind actor/chat/session/cwd, bypass model work, consume once and expire on cancel', async () => {
  const broker = new Interactions(), controller = new AbortController()
  let question
  const pending = broker.ask(actor, 'write file?', ['yes', 'no'], true, async value => { question = value }, controller.signal)
  await new Promise(resolve => setImmediate(resolve))
  for (const changed of [{ ...actor,origin:'web' },{ ...actor, userId: 'bob' }, { ...actor, chatId: 'other' }, { ...actor, sessionId: 'other' }, { ...actor, cwd: '/other' }]) assert.equal(broker.answer(changed, question.token, 'yes'), false)
  assert.equal(broker.answer(actor, question.token, 'invented'), false)
  assert.equal(broker.answer(actor, question.token, 'yes'), true)
  assert.equal(broker.answer(actor, question.token, 'yes'), false)
  assert.equal(await pending, 'yes')
  const cancelled = broker.ask(actor, 'question', [], false, async value => { question = value }, controller.signal)
  controller.abort(); assert.equal(await cancelled, '')
  assert.equal(broker.answer(actor, question.token, 'late'), false)
  assert.equal(new Interactions().answer(actor, question.token, 'after restart'), false)
})

test('P2 pending cards preserve new messages and route only unambiguous answers to their original owner',async()=>{
  const broker=new Interactions();let card
  const pending=broker.ask(actor,'允许写文件吗？',['允许一次','拒绝'],true,async value=>{card=value});await new Promise(setImmediate)
  assert.equal(broker.routeInput(actor,'你好'),undefined);assert.equal(broker.routeInput(actor,'现在请查天气'),undefined)
  assert.equal(broker.routeInput({...actor,userId:'other'},'同意授权'),undefined)
  const yes=broker.routeInput(actor,'同意授权');assert.equal(yes.kind,'answer');assert(yes.text.endsWith(' 允许一次'))
  assert.equal(broker.answer(actor,card.token,'允许一次'),true);assert.equal(await pending,'允许一次')
  const city=broker.ask(actor,'城市？',[],false,async value=>{card=value});await new Promise(setImmediate)
  const continued={...actor,sessionId:'continued'}
  assert.equal(broker.routeInput({...continued,threadId:'other'},'回答：济南'),undefined)
  assert.equal(broker.routeInput(continued,'回答：济南').sessionId,actor.sessionId)
  assert.equal(broker.routeInput(actor,'你好'),undefined);assert.equal(broker.routeInput(actor,'今天几号？'),undefined)
  assert.equal(broker.routeInput(actor,'回答：济南').kind,'answer')
  broker.cancelActor({...actor,userId:'other'});assert.equal(broker.routeInput(actor,'回答：济南').kind,'answer')
  broker.cancelActor(actor);assert.equal(await city,'');assert.equal(broker.answer(actor,card.token,'late'),false)
})

test('P2 durable attachment staging enforces owner/reference/size, survives restart and freezes outgoing hashes', async t => {
  const directory = await temporaryDirectory(t), file = path.join(directory, 'output.txt')
  let downloads = 0
  const receive = async () => { downloads++; return { bytes: Buffer.from('uploaded text'), mediaType: 'text/plain' } }
  const entry = { id: 'input', messageId: 'message', chatId: 'chat', threadId: '', actor: { ids: ['alice'] }, spec: { cwd: directory }, text: JSON.stringify({ key: 'file-key', kind: 'file', name: '../../unsafe.txt' }) }
  let transfers = new FileTransfers(directory, receive)
  await transfers.receive(entry); await transfers.receive(entry); assert.equal(downloads, 1)
  transfers.close(); transfers = new FileTransfers(directory, receive); cleanup(t, () => transfers.close())
  const files = await transfers.forMessage(entry)
  assert.equal(files.length, 1); assert.equal(files[0].name, 'unsafe.txt'); assert.equal(Buffer.from(files[0].data).toString(), 'uploaded text')
  assert.equal((await transfers.forMessage({ ...entry, actor: { ids: ['bob'] } })).length, 0)
  await assert.rejects(transfers.forMessage({ ...entry, actor: { ids: ['bob'], referenceMessageId: 'message' } }), /unavailable/)
  await writeFile(file, 'artifact')
  const outgoing = JSON.parse(await transfers.outbound(entry, file)); assert.equal(outgoing.path, file); assert.equal(outgoing.sha256.length, 64)
  await writeFile(path.join(directory, '.env'), 'secret')
  await assert.rejects(transfers.outbound(entry, path.join(directory, '.env')), /scope/)
})

test('P3 HTTP resource pagination stays complete where supported and calendar time ranges never mix cursors or hide truncation', async t => {
  const requests = []
  const base = await api(t, async (request, response) => {
    let raw = ''; for await (const part of request) raw += part
    requests.push({ url: request.url, method: request.method, body: raw ? JSON.parse(raw) : null, auth: request.headers.authorization })
    response.setHeader('Content-Type', 'application/json')
    const next = new URL(request.url, 'http://localhost').searchParams.get('page_token')
    response.end(JSON.stringify({ code: 0, data: request.method === 'GET' || request.url.includes('/search') ? { items: [next ? 'second' : 'first'], has_more: !next, page_token: next ? undefined : 'next' } : { receipt: true } }))
  })
  const http = new FeishuHttp(base, async () => 'test-application-token')
  const resources = ['bitable', 'documents', 'calendar', 'tasks'].map(kind => ({ alias: kind, kind, id: 'resource', tableId: 'table', identity: 'tenant', tokenRef: 'FEISHU_APP_TENANT', expiresAt: Date.now() + 100000,
    capabilities: ['read', 'create', 'update', 'delete'].map(action => kind + '.' + action) }))
  const service = new Resources(resources, ['alice'], () => http)
  for (const row of resources) {
    const query = await service.execute(actor, { resource: row.alias, action: 'query', params: { start_time: '1', end_time: '2' } }, signal())
    if (row.kind === 'calendar') { assert.equal(query.complete,false);assert.deepEqual(query.items,['first']);assert.equal(query.coverage,'possibly_truncated_range');assert(requests.filter(request=>request.url.includes('/calendar/v4/')).every(request=>!request.url.includes('page_token'))) }
    else assert.deepEqual(query, { items: ['first', 'second'], complete: true })
    const params = { records: [{ record_id: 'record', fields: { name: 'value' } }], title: 'title', text: 'append', event_id: 'event', recurrence_scope: 'all', event: { start_time: { timezone: 'Asia/Shanghai' }, end_time: { timezone: 'Asia/Shanghai' } }, notify: false,
      task_guid: 'task', task: { summary: 'task', members: [{ id: 'alice', role: 'assignee' }], due: { timestamp: '1234567890000' } } }
    for (const action of ['create', 'update', 'delete']) await service.execute(actor, { resource: row.alias, action, params }, signal())
  }
  assert(requests.every(row => row.auth === 'Bearer test-application-token'))
  assert(requests.some(row => row.url.includes('/batch_update') && row.method === 'POST'))
  assert(requests.some(row => row.url.includes('/calendar/v4/') && row.method === 'PATCH'))
  assert(requests.some(row => row.url.includes('/task/v2/tasks/task') && row.method === 'PATCH'))
  assert(requests.some(row => row.url.includes('/docx/v1/documents/resource/blocks/resource/children')))
  await assert.rejects(service.execute({ ...actor, userId: 'bob' }, { resource: 'tasks', action: 'delete', params: { task_guid: 'task' } }), /authorized/)
})

test('P3 supplementary fields preserve resource, record and original expiry; retargeted config and consumed previews are rejected', async t => {
  const directory = await temporaryDirectory(t), ledger = new OperationLedger(path.join(directory, 'supplement.sqlite'))
  cleanup(t, () => ledger.close())
  const row = { alias: 'table', kind: 'bitable', id: 'base', tableId: 'table', identity: 'tenant', expiresAt: Date.now() + 100000, capabilities: ['bitable.update'], tokenRef: 'FEISHU_APP_TENANT' }
  const service = new Resources([row], ['alice'], () => ({ request: async () => ({ record: { fields: { name: 'before', count: 1 } } }) }))
  const preview = await ledger.preview(service, actor, { resource: 'table', action: 'update', params: { records: [{ record_id: 'record', fields: { name: 'after' } }] } }, signal())
  const originalExpiry = ledger.status(actor, preview.token).expiresAt
  await assert.rejects(ledger.supplement(service, { ...actor, sessionId: 'other' }, preview.token, { count: 2 }, signal()), /binding/)
  const updated = await ledger.supplement(service, actor, preview.token, { count: 2 }, signal())
  assert.equal(updated.expiresAt, originalExpiry)
  assert.deepEqual(updated.preview.params.records, [{ record_id: 'record', fields: { name: 'after', count: 2 } }])
  assert.equal(ledger.status(actor, preview.token).status, 'superseded')
  await assert.rejects(ledger.supplement(service, actor, preview.token, { name: 'again' }, signal()), /binding/)
  row.tableId = 'different-table'
  await assert.rejects(ledger.confirm(service, actor, updated.token, signal()), /frozen_resource_changed/)
})

test('P3 remote reconciliation retries verification only and task completion does not require resending assignees', async t => {
  const directory = await temporaryDirectory(t), ledger = new OperationLedger(path.join(directory, 'reconcile.sqlite'))
  cleanup(t, () => ledger.close())
  let writes = 0, failedRead = false, fields = { name: 'before' }
  const row = { alias: 'table', kind: 'bitable', id: 'base', tableId: 'table', identity: 'tenant', expiresAt: Date.now() + 100000, capabilities: ['bitable.update'], tokenRef: 'FEISHU_APP_TENANT' }
  const service = new Resources([row], ['alice'], () => ({ request: async (method, route, body) => {
    if (method === 'GET') { if (failedRead) throw new Error('network'); return { record: { fields } } }
    writes++; fields = body.records[0].fields; failedRead = true; return { records: [{ record_id: 'record' }] }
  } }))
  const preview = await ledger.preview(service, actor, { resource: 'table', action: 'update', params: { records: [{ record_id: 'record', fields: { name: 'after' } }] } }, signal())
  assert.equal((await ledger.confirm(service, actor, preview.token, signal())).status, 'unknown')
  failedRead = false
  assert.equal((await ledger.reconcile(service, actor, preview.token, signal())).status, 'completed'); assert.equal(writes, 1)
  let body
  const tasks = new Resources([{ ...row, kind: 'tasks', capabilities: ['tasks.update'] }], ['alice'], () => ({ request: async (_m, _r, value) => { body = value; return {} } }))
  await tasks.execute(actor, { resource: 'table', action: 'update', params: { task_guid: 'task', task: { completed_at: '1234567890000' } } }, signal())
  assert.deepEqual(body, { task: { completed_at: '1234567890000' }, update_fields: ['completed_at'] })
})

test('P3 frozen Bitable confirmation detects optimistic conflicts and never repeats a completed/unknown write after restart', async t => {
  const directory = await temporaryDirectory(t), filename = path.join(directory, 'ledger.sqlite')
  let writes = 0, value = 'before', loseReceipt = false
  const port = { request: async (method, route, body) => {
    if (method === 'GET') return { record: { record_id: 'record', fields: { name: value } } }
    writes++; if (loseReceipt) throw new Error('connection_lost'); value = body.records[0].fields.name
    return { records: [{ record_id: 'record' }] }
  } }
  const resources = new Resources([{ alias: 'table', kind: 'bitable', id: 'base', tableId: 'table', identity: 'tenant', expiresAt: Date.now() + 100000, capabilities: ['bitable.update'], tokenRef: 'FEISHU_APP_TENANT' }], ['alice'], () => port)
  let ledger = new OperationLedger(filename)
  assert.throws(() => new OperationLedger(filename), /already_active/)
  const operation = { resource: 'table', action: 'update', params: { records: [{ record_id: 'record', fields: { name: 'after' } }] } }
  const first = await ledger.preview(resources, actor, operation, signal())
  operation.params.records[0].fields.name = 'tampered after preview'
  value = 'conflict'
  await assert.rejects(ledger.confirm(resources, actor, first.token, signal()), /conflict/); assert.equal(writes, 0)
  value = 'before'
  await assert.rejects(ledger.confirm(resources, { ...actor, userId: 'bob' }, first.token, signal()), /binding/)
  assert.equal((await ledger.confirm(resources, actor, first.token, signal())).status, 'completed'); assert.equal(value, 'after')
  ledger.close(); ledger = new OperationLedger(filename); cleanup(t, () => ledger.close())
  assert.equal((await ledger.confirm(resources, actor, first.token, signal())).status, 'completed'); assert.equal(writes, 1)
  const uncertain = await ledger.preview(resources, actor, operation, signal()); loseReceipt = true
  assert.equal((await ledger.confirm(resources, actor, uncertain.token, signal())).status, 'unknown')
  assert.equal((await ledger.confirm(resources, actor, uncertain.token, signal())).status, 'unknown'); assert.equal(writes, 2)
})

test('P3 application token acquisition and reuse require no OAuth endpoint or user credential', async t => {
  const routes = []; let tokens = 0
  const base = await api(t, async (request, response) => {
    routes.push(request.url)
    let raw = ''; for await (const part of request) raw += part
    response.setHeader('Content-Type', 'application/json')
    if (request.url === '/open-apis/auth/v3/tenant_access_token/internal') {
      assert.deepEqual(JSON.parse(raw), { app_id: 'cli_fixture', app_secret: 'fixture-secret' })
      tokens++; response.end(JSON.stringify({code:0,tenant_access_token:'fixture-application-token',expire:7200}))
    } else {
      assert.equal(request.headers.authorization, 'Bearer fixture-application-token')
      response.end(JSON.stringify({code:0,data:{items:[],has_more:false}}))
    }
  })
  const app = new FeishuApi({appId:'cli_fixture',secret:async()=>'fixture-secret',origin:base,timeoutMs:1000})
  const grant = await app.tenantGrant()
  const calendar = {alias:'app-calendar',kind:'calendar',id:'fixture@group.calendar.feishu.cn',identity:'tenant',tokenRef:'FEISHU_APP_TENANT',expiresAt:grant.expiresAt,capabilities:['calendar.read']}
  const service = new Resources([calendar], ['alice'], () => new FeishuHttp(base, async()=>(await app.tenantGrant()).token))
  const operation = {resource:'app-calendar',action:'query',params:{start_time:'1',end_time:'2'}}
  await service.execute(actor, operation, signal()); await service.execute(actor, operation, signal())
  assert.equal(tokens,1); assert.equal(routes.length,3)
  assert(!routes.some(route=>route.includes('/authen/')||route.includes('/oauth/')))
  assert.throws(()=>new Resources([{...calendar,identity:'user'}],['alice'],()=>null),/application_identity/)
  assert.throws(()=>new Resources([{...calendar,tokenRef:'USER_CREDENTIAL'}],['alice'],()=>null),/application_identity/)
})

test('P4 OpenCLI adapter runs stateless arguments without a shell and kills a cooperative request on timeout', async t => {
  const directory = await temporaryDirectory(t), cli = path.join(directory, 'mock-opencli.js'), capture = path.join(directory, 'capture.json')
  await writeFile(cli, `const fs=require('fs');fs.writeFileSync(${JSON.stringify(capture)},JSON.stringify(process.argv.slice(2)));console.log('response: adviser answer')`)
  const provider = new OpenCliAdviser(cli)
  const adviser = new Adviser(provider, ['secret-value'], 1000)
  const result = await adviser.ask('session', { question: 'question secret-value', context: 'token=hidden', excerpts: [] })
  assert.equal(result.text, 'adviser answer')
  const args = JSON.parse(await readFile(capture, 'utf8'))
  assert.deepEqual(args.slice(0, 5), ['chatgpt', 'ask', '--new', '-f', 'plain']); assert(!args.at(-1).includes('secret-value'))
  await provider.dispose()
  await writeFile(cli, 'setInterval(()=>{},1000)')
  const slow = new OpenCliAdviser(cli), limited = new Adviser(slow, [], 30)
  assert.equal((await limited.ask('session', { question: 'question', context: '', excerpts: [] })).status, 'unavailable')
  await slow.dispose()
})
