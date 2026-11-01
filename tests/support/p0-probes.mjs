import assert from 'node:assert/strict'
import { setTimeout as delay } from 'node:timers/promises'

/** Real installed public-service probes, driven by the normal official ACP entry. */
export async function verifyP0({ acp, a, b, mock, check, shellName, evidence }) {
  let sequence = 0
  async function tool(sessionId, name, args) {
    const key = `P0_${++sequence}`
    mock.toolCases.set(key, { name, arguments: args })
    const before = mock.requests.length
    const result = await acp.prompt(sessionId, `TELEBOT_TEST:${key}`)
    assert.equal(result.stopReason, 'end_turn')
    const receipt = mock.requests.slice(before).find(row => row.instruction === key && row.receipt)?.receipt
    assert(receipt, `Missing actual ${name} receipt for ${key}`)
    return receipt
  }
  async function probe(action, extra = {}, sessionId = a.sessionId) {
    const raw = await tool(sessionId, 'telebot_p0_probe', { action, ...extra })
    const envelope = JSON.parse(raw)
    assert.equal(typeof envelope.json, 'string', raw)
    return JSON.parse(envelope.json)
  }

  const commands = await probe('commands')
  assert.deepEqual(commands, { admitted: 1, rejected: 2, nativeLifecycle: true })
  check('P0 native command discovery/execution/lifecycle works without a model command loop; spoofed actors and unknown names are rejected')
  const scope = await probe('scope')
  assert.equal(scope.deniedDirectEntrypoints.length, 6)
  check('P0 restricted Agent tools reject direct Shell/subagent/schedule dispatch while the normal Agent retains its tools')

  const windows = process.platform === 'win32'
  const background = await tool(b.sessionId, shellName, {
    description: 'P0 official timeout promotion and scoped background cancellation',
    command: windows ? "Write-Output 'p0-start'; Start-Sleep -Seconds 30; Write-Output 'p0-finish'" : "printf 'p0-start\\n'; sleep 30; printf 'p0-finish\\n'",
    timeoutMs: 1000,
  })
  const jobId = background.match(/background job ([A-Za-z0-9_-]+)/)?.[1]
  assert(jobId, `A wait timeout must expose a background job rather than claim success: ${background}`)
  const foreign = await tool(a.sessionId, 'job_kill', { job_id: jobId })
  assert(/unknown|foreign|not found|not owned|another session|access/i.test(foreign), foreign)
  const killed = await tool(b.sessionId, 'job_kill', { job_id: jobId })
  assert(!/unknown|not found/i.test(killed), killed)
  const output = await tool(b.sessionId, 'job_output', { job_id: jobId, wait: true })
  assert(/killed|cancel|signal|exit code: 1/i.test(output), output)
  check('P0 native Shell wait timeout promotes a live job; a foreign session cannot kill it and its owner can cancel and collect it')

  const beforeCold=mock.requests.length,cold=await probe('cold-receipt')
  assert(cold.coldRestored&&cold.inert);assert.equal(mock.requests.length-beforeCold,2,'Cold receipt must make no model request')
  check('P0 official cold SessionController restoration reapplies the persisted receipt preset, blocks direct tools and never calls a model')
  const created = await probe('schedule-create', { delay: 2 })
  let snapshot
  const deadline = Date.now() + 15000
  do {
    await delay(300)
    snapshot = await probe('schedule-read', { id: created.record.id })
  } while (!snapshot.history.records?.length && Date.now() < deadline)
  assert.equal(snapshot.task.status, 'inactive')
  assert.equal(snapshot.history.records.length, 1)
  const receipt = snapshot.history.records[0]
  assert.equal(receipt.scheduledAt, created.record.scheduledAt)
  assert(JSON.stringify(snapshot.scheduleEvents).includes(receipt.messageId))
  evidence.p0 = { schedule: { taskId: created.record.id, plannedAt: receipt.scheduledAt, inboxReceipt: true } }
  check('P0 official schedule timing exposes structured task/occurrence/message correlation after durable inbox admission')
  const deleted = await probe('schedule-delete', { id: created.record.id })
  assert.equal(deleted.history.code, 'schedule_not_found')
  check('P0 deletion removes native delivery history: independent business audit retention is required')

  const faulted = await probe('schedule-fault', { delay: 1 })
  await delay(1800)
  const faultSnapshot = await probe('schedule-read', { id: faulted.record.id }, b.sessionId)
  assert.equal(faultSnapshot.task.status, 'active')
  assert.equal(faultSnapshot.history.records.length, 0)
  // The log still proves admission. Never infer execution authorization from
  // this prompt: catalog/history has no committed task receipt for it.
  assert(JSON.stringify(faultSnapshot.scheduleEvents).includes('P0 checkpoint fault'))
  await probe('schedule-delete', { id: faulted.record.id }, b.sessionId)
  evidence.p0.schedule.checkpointFailure = 'inbox admitted, native task still active without receipt; automated execution must fail closed'
  check('P0 injected checkpoint failure demonstrates the public schedule inbox/task commit gap; absent receipts cannot authorize business execution')
}
