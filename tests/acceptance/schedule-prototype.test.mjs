import test from 'node:test'
import assert from 'node:assert/strict'
import path from 'node:path'
import { nextWake, isoWeekday, missedDecision, PrototypeOccurrences } from '../fixtures/diagnostics/lib/schedule-policy.js'
import { temporaryDirectory, cleanup } from '../support/temp-dir.mjs'

test('P0 old Asia/Shanghai daily/weekly interval/count/until becomes an official one-shot wake, with explicit ISO weekdays', () => {
  const first = '2026-11-02T01:00:00.000Z' // Monday 09:00 Shanghai.
  assert.equal(isoWeekday(0), 1); assert.equal(isoWeekday(6), 7)
  assert.throws(() => isoWeekday(7))
  assert.deepEqual(nextWake(first, { kind: 'daily', interval: 2 }, 1), { at: '2026-11-04T01:00:00.000Z' })
  assert.deepEqual(nextWake(first, { kind: 'weekly', interval: 2, weekdays: [0] }, 1), { at: '2026-11-16T01:00:00.000Z' })
  assert.deepEqual(nextWake(first, { kind: 'weekly', interval: 2, weekdays: [0, 2] }, 1), { at: '2026-11-04T01:00:00.000Z' })
  assert.equal(nextWake(first, { kind: 'daily', interval: 1, count: 1 }, 1), undefined)
  assert.equal(nextWake(first, { kind: 'daily', interval: 1, until: first }, 1), undefined)
  assert.equal(nextWake(first, { kind: 'once', interval: 1 }, 1), undefined)
})

test('P0 grace/catch-up/confirmation decisions preserve skip/run_once/ask and overlap semantics', () => {
  const now = 1000000, old = now - 300001
  assert.equal(missedDecision('auto', 'skip', old, now), 'skipped')
  assert.equal(missedDecision('auto', 'run_once', old, now), 'queued')
  assert.equal(missedDecision('auto', 'ask', old, now), 'awaiting_auth')
  assert.equal(missedDecision('confirm', 'run_once', now, now), 'awaiting_auth')
  assert.equal(missedDecision('remind', 'run_once', old, now), 'reminded')
  assert.equal(missedDecision('auto', 'run_once', now, now, true), 'skipped')
})

test('P0 structured receipt and authorization fence one dispatcher, occurrence dedup, restart unknown and retained audit', async t => {
  const directory = await temporaryDirectory(t), filename = path.join(directory, 'occurrences.sqlite'), planned = '2026-11-02T01:00:00.000Z'
  let ledger = new PrototypeOccurrences(filename)
  assert.throws(() => new PrototypeOccurrences(filename), /single_dispatcher/)
  assert.throws(() => ledger.claim('task', 1, planned, undefined, true), /authorization/)
  assert.throws(() => ledger.claim('task', 1, planned, { scheduledAt: planned, messageId: 'message' }, false), /authorization/)
  const receipt = { scheduledAt: planned, messageId: 'message' }
  assert.equal(ledger.claim('task', 1, planned, receipt, true), true)
  assert.equal(ledger.claim('task', 1, planned, receipt, true), false)
  ledger.close(); ledger = new PrototypeOccurrences(filename); cleanup(t, () => ledger.close())
  assert.equal(ledger.history('task')[0].state, 'unknown')
  assert.equal(ledger.claim('task', 1, planned, receipt, true), false)
  ledger.deleteTask('task'); assert.equal(ledger.history('task').length, 1)
})
