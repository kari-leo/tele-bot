import test from 'node:test'
import assert from 'node:assert/strict'
import { authorizeApi, collectPages, permissionMatrix } from '../../plugins/feishu/lib/business/domain/authorization.js'
import { executeCoding, authorizeHistory, codingDigest } from '../../plugins/codex/lib/domain/coding.js'
import { Adviser } from '../../plugins/adviser/lib/domain/adviser.js'

const actor = { origin: 'feishu', userId: 'alice', chatId: 'chat' }
const binding = { userId: 'alice', chatId: 'chat', sessionId: 'session', cwd: '/workspace' }
const request = mode => { const value = { ...binding, mode, prompt: 'inspect project' }; return { ...value, digest: codingDigest(value) } }

test('P3 all four resource families require actor, exact capability, resource and unexpired application identity', () => {
  for (const [kind, actions] of Object.entries(permissionMatrix)) for (const action of actions) {
    const operation = { kind, action, resource: 'test-resource' }
    const grant = { kind: 'tenant', expiresAt: 1001, capabilities: [`${kind}.${action}`], resources: ['test-resource'] }
    assert.doesNotThrow(() => authorizeApi(actor, ['alice'], operation, grant, 1000))
    for (const [who, credential] of [
      [{ ...actor, userId: 'bob' }, grant], [actor, { ...grant, kind: 'user' }],
      [actor, { ...grant, expiresAt: 1000 }], [actor, { ...grant, capabilities: [] }],
      [actor, { ...grant, resources: [] }], [actor, undefined],
      [{ origin: 'web' }, grant], [{ ...actor, chatId: undefined }, grant],
    ]) assert.throws(() => authorizeApi(who, ['alice'], operation, credential, 1000))
    assert.throws(() => authorizeApi(actor, ['alice'], { ...operation, action: 'arbitrary' }, grant, 1000), /unsupported/)
  }
})

test('P3 pagination verifies all pages and refuses missing, cycling and budget-exhausted cursors', async () => {
  const tokens = []
  const result = await collectPages(async token => { tokens.push(token); return token ? { items: [2], hasMore: false } : { items: [1], hasMore: true, next: 'next' } })
  assert.deepEqual(result, { items: [1, 2], complete: true }); assert.deepEqual(tokens, [undefined, 'next'])
  await assert.rejects(collectPages(async () => ({ items: [], hasMore: true })), /cursor/)
  await assert.rejects(collectPages(async () => ({ items: [], hasMore: true, next: 'cycle' })), /cursor/)
  await assert.rejects(collectPages(async () => ({ items: [], hasMore: true, next: 'next' }), 1), /budget/)
})

test('P4 inspect and plan require backend-enforced read-only; no forged actor or prompt reaches executor', async () => {
  const calls = [], executor = { enforcesReadOnly: true, execute: async (input, options) => { calls.push({ input, options }); return { status: 'completed', text: 'done' } } }
  for (const mode of ['inspect', 'plan']) {
    await executeCoding(executor, request(mode), binding, ['alice'])
    assert.equal(calls.at(-1).options.sandbox, 'read-only')
    await assert.rejects(executeCoding({ ...executor, enforcesReadOnly: false }, request(mode), binding, ['alice']), /read_only/)
  }
  await assert.rejects(executeCoding(executor, { ...request('inspect'), userId: 'bob' }, binding, ['alice']), /mismatch/)
  await assert.rejects(executeCoding(executor, { ...request('inspect'), prompt: 'modified' }, binding, ['alice']), /invalid/)
  assert.equal(calls.length, 2)
})

test('P4 apply confirmation binds scope and frozen content, consumes before unknown dispatch and never retries', async () => {
  let calls = 0
  const executor = { enforcesReadOnly: true, execute: async (_request, options) => { calls++; assert.equal(options.sandbox, 'workspace-write'); return { status: 'unknown', text: 'receipt lost' } } }
  const input = request('apply'), grant = { ...binding, digest: input.digest, expiresAt: Date.now() + 10000, consumed: false }
  for (const changed of [{ ...grant, cwd: '/other' }, { ...grant, digest: 'wrong' }, { ...grant, expiresAt: 0 }]) {
    await assert.rejects(executeCoding(executor, input, binding, ['alice'], changed), /confirmation/)
  }
  assert.equal((await executeCoding(executor, input, binding, ['alice'], grant)).status, 'unknown')
  await assert.rejects(executeCoding(executor, input, binding, ['alice'], grant), /confirmation/)
  assert.equal(calls, 1); assert.equal(grant.consumed, true)
})

test('P4 cancelled request preserves unused grant; history requires private owner and fresh resume scope', async () => {
  const input = request('apply'), grant = { ...binding, digest: input.digest, expiresAt: Date.now() + 10000, consumed: false }
  await assert.rejects(executeCoding({ enforcesReadOnly: true, execute() { assert.fail('cancelled request dispatched') } }, input, binding, ['alice'], grant, AbortSignal.abort()))
  assert.equal(grant.consumed, false)
  const trusted = { ...binding, privateChat: true }, thread = { owner: 'alice', cwd: binding.cwd, archived: false }
  assert.doesNotThrow(() => authorizeHistory(trusted, ['alice'], thread, 'resume'))
  assert.doesNotThrow(() => authorizeHistory(trusted, ['alice'], { ...thread, archived: true }, 'read'))
  for (const [who, record] of [[{ ...trusted, privateChat: false }, thread], [trusted, { ...thread, owner: 'bob' }],
    [trusted, { ...thread, archived: true }], [trusted, { ...thread, cwd: '/other' }]]) {
    assert.throws(() => authorizeHistory(who, ['alice'], record, 'resume'))
  }
})

test('P4 adviser redacts question/context/excerpts before request, caches per session and evicts LRU', async () => {
  const calls = [], adviser = new Adviser({ ask: async (input) => { calls.push(input); return 'advice secret-value' } }, ['secret-value'], 1000, 2, 2)
  const input = { question: 'question secret-value token=abc', context: 'Bearer abcd', excerpts: ['password="sensitive text"', 'secret-value'] }
  const first = await adviser.ask('s1', input)
  assert.equal(first.text, 'advice [REDACTED]'); assert.equal(first.cached, false)
  assert(!JSON.stringify(calls[0]).includes('secret-value')); assert(!JSON.stringify(calls[0]).includes('sensitive text')); assert(!JSON.stringify(calls[0]).includes('abcd'))
  assert.equal((await adviser.ask('s1', input)).cached, true)
  await adviser.ask('s2', input); assert.equal(calls.length, 2)
  const second = { ...input, question: 'second' }, third = { ...input, question: 'third' }
  await adviser.ask('s1', second); await adviser.ask('s1', input); await adviser.ask('s1', third)
  assert.equal((await adviser.ask('s1', second)).cached, false)
  await adviser.ask('s3', input)
  assert.equal((await adviser.ask('s2', input)).cached, false)
  adviser.clear('s2'); assert.equal((await adviser.ask('s2', input)).cached, false)
})

test('P4 adviser degrades on timeout/cancel/provider failure, retains concurrency bound for uncooperative backend', async () => {
  let resolve, calls = 0
  const input = { question: 'question', context: '', excerpts: [] }
  const adviser = new Adviser({ ask: async () => { calls++; return new Promise(done => { resolve = done }) } }, [], 20, 16, 1)
  const first = adviser.ask('s1', input)
  assert.equal((await adviser.ask('s1', input)).status, 'unavailable')
  assert.equal((await first).status, 'unavailable')
  assert.equal((await adviser.ask('s2', input)).status, 'unavailable'); assert.equal(calls, 1)
  resolve('late'); await new Promise(done => setImmediate(done))
  assert.equal((await adviser.ask('s1', input, AbortSignal.abort())).status, 'unavailable')
  const failing = new Adviser({ ask: async () => { throw new Error('secret provider failure') } })
  const result = await failing.ask('s1', input)
  assert.equal(result.status, 'unavailable'); assert(!result.text.includes('secret'))
})
