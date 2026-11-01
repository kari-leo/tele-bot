/** Explicitly opt-in real API test. Only the supplied Base and this run's own rows may be written. */
import { parseArgs } from 'node:util'
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import assert from 'node:assert/strict'
import { root } from './common.mjs'
import { readReference } from '../plugins/feishu/lib/adapters/reference-env.js'
import { FeishuApi, apiOrigin } from '../plugins/feishu/lib/adapters/feishu-api.js'
import { FeishuHttp } from '../plugins/feishu/lib/business/adapters/http.js'
import { Resources } from '../plugins/feishu/lib/business/domain/resources.js'
import { OperationLedger } from '../plugins/feishu/lib/business/adapters/ledger.js'

const { values } = parseArgs({ options: { reference: { type: 'string' }, base: { type: 'string' }, table: { type: 'string' }, write: { type: 'boolean', default: false } } })
assert(values.reference && values.base, 'Use --reference <external env> --base <human-authorized Base URL> [--table id] [--write]')
const url = new URL(values.base)
assert(url.protocol === 'https:' && !url.username && !url.password && (url.hostname === 'feishu.cn' || url.hostname.endsWith('.feishu.cn')))
const base = url.pathname.match(/^\/base\/([\w-]+)\/?$/)?.[1]
assert(base, 'A specific Base URL is required; tenant-wide discovery is not supported')
const reference = await readReference(values.reference), origin = apiOrigin(reference.FEISHU_API_BASE_URL || 'https://open.feishu.cn')
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
