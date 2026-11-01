import test from 'node:test'
import assert from 'node:assert/strict'
import path from 'node:path'
import { mkdir, symlink } from 'node:fs/promises'
import { DatabaseSync } from 'node:sqlite'
import { HarnessCommands } from '../../plugins/core/lib/harness/commands.js'
import { FeishuChannel } from '../../plugins/feishu/lib/domain/channel.js'
import { SqliteChannelRepository } from '../../plugins/feishu/lib/adapters/sqlite.js'
import { Workspaces } from '../../plugins/feishu/lib/adapters/workspaces.js'
import { cleanup, eventually, temporaryDirectory } from '../support/temp-dir.mjs'

const event = (id, text, extra = {}) => ({ eventId: id, messageId: `om_${id}`, chatId: 'chat', threadId: '', actor: { ids: ['alice'] }, kind: 'text', text, ...extra })
async function setup(t, overrides = {}) {
  const root = await temporaryDirectory(t), child = path.join(root, 'child 中文')
  await mkdir(child)
  const filename = path.join(root, 'channel.sqlite')
  const settings = { appId: 'test', adminUserIds: ['alice'], workspace: root, provider: 'mock', model: 'a', queueLimit: 20,
    maxConcurrentSessions: 2, pollMs: 25, leaseMs: 10000, retryMs: 50, maxDeliveryAttempts: 3, actionTtlMs: 10000 }
  const repository = new SqliteChannelRepository(filename, 'test', 10000)
  cleanup(t, () => repository.release())
  const sent = [], calls = [], selections = [], prepared = []
  const agent = { run: async (spec, input) => { calls.push({ spec, input }); return { status: 'completed', text: 'done' } },
    recover: async () => { throw new Error('must not replay command') }, status: () => ({ active: false }), cancel() {}, dispose: async () => {}, ...overrides }
  const commands = { list: async () => [{ name: 'compact', description: 'native compact' }],
    execute: async () => ({ status: 'failed', text: 'not available' }), models: async () => [{ provider: 'mock', model: 'a' }, { provider: 'mock', model: 'b' }],
    selectModel: async (_spec, route) => { if (route.provider !== 'mock' || route.model !== 'b') throw new Error('model_not_in_harness_catalog'); selections.push(route) },
    prepare: async spec => { prepared.push(spec) } }
  const ports = { repository, agent, commands, workspace: new Workspaces([root]), delivery: { send: async message => { sent.push(message); return { status: 'sent', messageId: 'sent_' + sent.length } } } }
  const channel = new FeishuChannel(ports, settings)
  channel.start(); cleanup(t, () => channel.stop())
  const rows = query => { const db = new DatabaseSync(filename, { readOnly: true }); try { return db.prepare(query).all() } finally { db.close() } }
  const settled = async id => { await eventually(() => sent.some(row => row.replyTo === `om_${id}`)); return sent.find(row => row.replyTo === `om_${id}`) }
  return { root, child, filename, repository, settings, sent, calls, selections, prepared, commands, ports, channel, rows, settled }
}

test('P2 native CommandAdapter filters discovery and dispatch, preserving exact input and flushing host lifecycle', async () => {
  let dispatches = 0, flushes = 0, line
  const agent = { status: 'idle', session: { header: { cwd: '/workspace' } }, model: { current: () => ({ provider: 'mock', model: 'a' }), select() {} } }
  const registry = { list: () => [{ name: 'compact', description: 'compact' }, { name: 'dangerous', description: 'not reviewed' }],
    find: () => ({}), execute: async (_agent, exact) => { dispatches++; line = exact; return { result: { kind: 'success', text: 'native result' } } } }
  const sessions = { flush: async () => { flushes++ } }
  const ctx = { get: key => key === 'commands' ? registry : key === 'sessions' ? sessions : undefined,
    get sessions() { throw new Error('cannot get property "sessions" without inject') } }
  const adapter = new HarnessCommands(ctx, async () => agent)
  const spec = { id: 'id', cwd: '/workspace', allowCreate: true }
  assert.deepEqual(await adapter.list(spec), [{ name: 'compact', description: 'compact' }])
  assert.equal((await adapter.execute(spec, '/dangerous', AbortSignal.timeout(1000))).status, 'failed')
  assert.equal(dispatches, 0)
  assert.deepEqual(await adapter.execute(spec, '/compact  exact input', AbortSignal.timeout(1000)), { status: 'completed', text: 'native result' })
  assert.equal(line, '/compact  exact input'); assert.equal(dispatches, 1); assert.equal(flushes, 1)
})

test('P2 model selection changes only current mapping and persists across repository restart', async t => {
  const s = await setup(t)
  assert.equal(s.channel.accept(event('spoof', '/model mock/b', { actor: { ids: ['stranger'] } })).rejected, true)
  s.channel.accept(event('model', '/model mock/b')); await s.settled('model')
  s.channel.accept(event('next', 'hello')); await s.settled('next')
  s.channel.accept(event('other', 'other chat', { chatId: 'other' })); await s.settled('other')
  assert.equal(s.calls.find(row => row.input.text === 'hello').spec.model, 'b')
  assert.equal(s.calls.find(row => row.input.text === 'other chat').spec.model, 'a')
  assert.equal(s.selections.length, 1)
  await s.channel.stop(); s.repository.release()
  const restarted = new SqliteChannelRepository(s.filename, 'test', 10000); cleanup(t, () => restarted.release())
  restarted.accept(event('restart', 'after restart'), s.settings)
  assert.equal(restarted.claim([]).spec.model, 'b')
})

test('P2 directory switch prepares a new session before mapping commit and refuses traversal and junction escape', async t => {
  const s = await setup(t)
  s.channel.accept(event('initial', 'initial')); await s.settled('initial')
  const original = s.calls[0].spec
  s.channel.accept(event('cd', '/cd ' + s.child)); await s.settled('cd')
  s.channel.accept(event('after', 'after')); await s.settled('after')
  const actual = s.calls.at(-1).spec
  assert.notEqual(actual.id, original.id); assert.equal(actual.cwd, s.child); assert.equal(s.prepared[0].id, actual.id)
  const outside = await temporaryDirectory(t), link = path.join(s.root, 'escape')
  await symlink(outside, link, process.platform === 'win32' ? 'junction' : 'dir')
  for (const [id, target] of [['outside', outside], ['junction', link], ['relative', '../outside']]) {
    s.channel.accept(event(id, '/cd ' + target)); await s.settled(id)
    assert.equal(s.rows(`SELECT state FROM inbox WHERE event_id='${id}'`)[0].state, 'failed')
  }
  s.channel.accept(event('still', 'still')); await s.settled('still')
  assert.equal(s.calls.at(-1).spec.id, actual.id)
})

test('P2 failed preparation and busy commands retain the previous mapping; stop remains usable', async t => {
  let held, active = false
  const s = await setup(t, { run: async () => { active = true; return new Promise(resolve => { held = resolve }) },
    status: () => ({ active }), cancel: () => { active = false; held({ status: 'cancelled', text: 'stopped' }) } })
  s.commands.prepare = async () => { throw new Error('persistence_failure') }
  s.channel.accept(event('badcd', '/cd ' + s.child)); await s.settled('badcd')
  assert.equal(JSON.parse(s.rows('SELECT spec FROM sessions')[0].spec).cwd, s.root)
  // A genuinely uncertain preparation blocks ordinary input; use another chat for busy behavior.
  s.channel.accept(event('hold', 'hold', { chatId: 'busy' })); await eventually(() => active)
  s.channel.accept(event('busy_model', '/model mock/b', { chatId: 'busy' })); await s.settled('busy_model')
  assert.equal(s.selections.length, 0)
  s.channel.accept(event('stop', '/stop', { chatId: 'busy' })); await s.settled('stop'); await s.settled('hold')
})

test('P2 interrupted mutating command is marked unknown on restart without dispatching it again', async t => {
  const s = await setup(t)
  await s.channel.stop()
  s.repository.accept(event('old_command', '/model mock/b'), s.settings); s.repository.claim([])
  s.repository.release()
  const repository = new SqliteChannelRepository(s.filename, 'test', 10000); cleanup(t, () => repository.release())
  const channel = new FeishuChannel({ ...s.ports, repository }, s.settings)
  channel.start(); cleanup(t, () => channel.stop())
  await eventually(() => s.rows("SELECT state FROM inbox WHERE event_id='old_command'")[0].state === 'unknown')
  assert.equal(s.selections.length, 0)
})
