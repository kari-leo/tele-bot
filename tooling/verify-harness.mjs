import { schedulingRows } from '../tests/support/scheduling-composition.mjs'
import { cleanupVerification } from './cleanup-verification.mjs'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { randomUUID } from 'node:crypto'
import { access, cp, mkdir, mkdtemp, readFile, realpath, rename, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { methods } from '@agentclientprotocol/sdk'
import { root, run } from './common.mjs'
import { checkArtifacts } from './check-artifacts.mjs'
import { officialHost, verificationEnvironment } from './official-host.mjs'
import { deadline, startAcp } from '../tests/support/acp-client.mjs'
import { startMockModel } from '../tests/support/mock-model.mjs'
import { verifyP0 } from '../tests/support/p0-probes.mjs'

const host = await officialHost()
const p0 = process.argv.includes('--p0')
const work = await mkdtemp(path.join(tmpdir(), 'telebot-acp-'))
const home = path.join(work, 'home')
const env = verificationEnvironment(home)
const profile = 'telebot-verify'
const profileDir = path.join(home, 'profiles', profile)
const evidence = { runtime: host.version, platform: process.platform, transport: 'official ACP v1 SDK / stdio', checks: [] }
const report = path.join(work, 'result.json')
const save = async () => {
  if (mock) evidence.modelRequests = mock.requests.map(({ model, names, receipt, instruction }) => ({
    model, toolCount: names.length, hasEcho: names.includes('telebot_echo'),
    hasReceipt: Boolean(receipt), instruction: instruction.slice(0, 200),
  }))
  await writeFile(report, JSON.stringify(evidence, null, 2) + '\n')
  await mkdir(path.join(root, '.runtime'), { recursive: true })
  await cp(report, path.join(root, p0 ? '.runtime/verify-p0-latest.json' : '.runtime/verify-latest.json'))
}
const check = description => { evidence.checks.push(description); console.log(`PASS ${description}`) }
let commandNumber = 0
async function command(args) {
  const label = String(++commandNumber).padStart(2, '0')
  try {
    const result = await run(process.execPath, [host.cli, ...args], { cwd: work, env, timeout: 180000 })
    await writeFile(path.join(work, `cli-${label}.log`), result.stdout + result.stderr)
    return result
  } catch (error) {
    await writeFile(path.join(work, `cli-${label}.log`), (error.stdout ?? '') + (error.stderr ?? error.message))
    throw error
  }
}

let mock
let acp
try {
  const manifest = await checkArtifacts(); evidence.artifacts = manifest.packages
  for (const name of ['@telebot/core', '@telebot/diagnostics']) {
    assert(manifest.packages.some(entry => entry.name === name), 'Pack with --fixtures before verification')
  }
  assert.equal((await command(['--version'])).stdout.trim(), host.version)
  await command(['--profile', profile, '--from-default-profile', 'acp', '--dump-config'])
  const artifacts = path.join(work, 'artifacts')
  await mkdir(artifacts)
  for (const entry of manifest.packages) {
    const destination = path.join(artifacts, entry.filename)
    await cp(path.join(root, 'dist', entry.filename), destination)
    await command(['plugin', '--profile', profile, 'add', `file:${destination.replaceAll('\\', '/')}`])
  }
  const resolver = createRequire(path.join(profileDir, 'package.json'))
  const installedCore = await realpath(resolver.resolve('@telebot/core'))
  const installedFixture = await realpath(resolver.resolve('@telebot/diagnostics'))
  assert(!installedCore.startsWith(await realpath(root)))
  assert(!installedFixture.startsWith(await realpath(root)))
  check('tarballs installed through the official plugin manager outside the repository')

  const incompatible = path.join(work, 'incompatible')
  await mkdir(incompatible)
  await writeFile(path.join(incompatible, 'package.json'), JSON.stringify({
    name: '@telebot/incompatible-check', version: '0.1.0', engines: { dsh: '99.0.0' },
    dsh: { bundle: { patch: './cordis.patch.yml' } },
  }))
  await writeFile(path.join(incompatible, 'cordis.patch.yml'), '[]\n')
  await assert.rejects(command(['plugin', '--profile', profile, 'add', `file:${incompatible.replaceAll('\\', '/')}`]), /99\.0\.0/)
  check('incompatible engines.dsh is rejected by the official CLI')

  mock = await startMockModel()
  const providerPatch = path.join(work, 'model.patch.json')
  const disabledPatch = path.join(work, 'disabled.patch.json')
  await writeFile(providerPatch, JSON.stringify([
    { id: 'llm-pi-ai', config: { providers: { 'telebot-verification': {
      api: 'openai-completions', apiKeyEnv: 'TELEBOT_VERIFY_KEY', baseURL: mock.url,
      retryPolicy: { mode: 'normal', maxRetries: 0 },
      models: ['telebot-mock-a', 'telebot-mock-b'].map(id => ({ id, contextWindow: 131072, maxTokens: 4096, input: ['text'] })),
    } } } },
    { id: 'acp', config: { provider: 'telebot-verification', model: 'telebot-mock-a' } },
    ...(p0 ? [
      { insert: [{id:'telebot-p0-web',name:'@deepseek-ai/dsh-host-webserver',config:{host:'127.0.0.1',port:0}},...schedulingRows] },
      { id: 'telebot-diagnostics', config: { label: 'telebot-p0', prototypes: true } },
    ] : []),
  ]))
  await writeFile(disabledPatch, JSON.stringify([{ id: 'telebot-diagnostics', disabled: true }]))
  const cwdA = path.join(work, 'workspace-a')
  const cwdB = path.join(work, 'workspace B 中文')
  await mkdir(cwdA)
  await mkdir(cwdB)
  const userHome = path.join(work, 'user-home')
  await mkdir(userHome)
  const open = async (label, disabled = false) => startAcp({
    cli: host.cli, profile, patches: [providerPatch, ...(disabled ? [disabledPatch] : [])],
    cwd: work, env: { ...env, HOME: userHome, USERPROFILE: userHome }, log: path.join(work, `${label}.log`),
  })

  await rename(artifacts, `${artifacts}.unavailable`)
  try {
    acp = await open('acp-enabled')
    assert.equal(acp.initialized.agentInfo.name, 'deepseek-harness-acp')
    const a = await acp.request(methods.agent.session.new, { cwd: cwdA, mcpServers: [] })
    const b = await acp.request(methods.agent.session.new, { cwd: cwdB, mcpServers: [] })
    assert.notEqual(a.sessionId, b.sessionId)
    evidence.sessionA = a.sessionId
    check('official ACP initializes and creates independent workspace sessions without source tarballs')

    const token = randomUUID()
    const result = await acp.prompt(a.sessionId, `TELEBOT_TEST:ECHO:${token}`)
    assert.equal(result.stopReason, 'end_turn')
    const receipt = mock.requests.find(entry => entry.receipt?.includes(token))
    assert(receipt, 'Actual tool output must reach the model')
    assert(JSON.parse(receipt.receipt).coreIdentity === true, 'Core service identity must match')
    assert(acp.updates.some(({ sessionId, update }) => sessionId === a.sessionId && update.sessionUpdate === 'tool_call_update' && update.status === 'completed'))
    check('installed fixture executes through the Agent, preserves shared core identity, and emits ACP tool events')

    const windows = process.platform === 'win32'
    const shellName = windows ? 'pwsh' : 'bash'
    const quote = value => windows ? `'${value.replaceAll("'", "''")}'` : `'${value.replaceAll("'", "'\\''")}'`
    const shellToken = `shell-${randomUUID()}`
    mock.toolCases.set('SHELL_READ', { name: shellName, arguments: {
      description: 'Verify native shell and the exact ACP session working directory',
      command: windows ? `Get-Location | Select-Object -ExpandProperty Path; Write-Output ${quote(shellToken)}` : `pwd; printf '%s\\n' ${quote(shellToken)}`,
      timeoutMs: 10000,
    } })
    const beforeShell = mock.requests.length
    await acp.prompt(b.sessionId, 'TELEBOT_TEST:SHELL_READ')
    const shellReceipt = mock.requests.slice(beforeShell).find(entry => entry.receipt)?.receipt
    assert(shellReceipt?.includes(shellToken) && shellReceipt.includes(cwdB), `Official ${shellName} must run in the session cwd: ${shellReceipt}`)
    check(`official ${shellName} executes in a workspace containing spaces and Unicode`)

    const shellDefinition = mock.requests[beforeShell].body.tools.find(tool => tool.function.name === shellName)
    const escalationModes = shellDefinition.function.parameters.properties.sandbox_permissions.enum
    assert(escalationModes.includes('danger-full-access'), 'Official shell must advertise its escalation modes')
    const forbiddenMarker = path.join(work, 'denied-escalation-marker.txt')
    mock.toolCases.set('SHELL_DENIED', { name: shellName, arguments: {
      description: 'Verify rejected escalation does not execute a write outside the workspace',
      command: windows ? `Set-Content -LiteralPath ${quote(forbiddenMarker)} -Value 'must-not-exist'` : `printf 'must-not-exist' > ${quote(forbiddenMarker)}`,
      timeoutMs: 10000, sandbox_permissions: 'danger-full-access', justification: 'Controlled acceptance test; client must reject.',
    } })
    const beforePermissions = acp.permissions.length
    await acp.prompt(b.sessionId, 'TELEBOT_TEST:SHELL_DENIED')
    assert(acp.permissions.length > beforePermissions, 'Native shell escalation must reach ACP permission control')
    await assert.rejects(access(forbiddenMarker), { code: 'ENOENT' })
    check('ACP rejection prevents the native shell escalation and its external write')

    if (p0) await verifyP0({ acp, a, b, mock, check, shellName, evidence })

    const modelOption = a.configOptions.find(option => option.id === 'model')
    assert(modelOption, 'ACP model configuration must be advertised')
    const choices = modelOption.options.flatMap(option => option.options ?? [option])
    const alternate = choices.find(option => option.name.includes('telebot-mock-b'))
    assert(alternate, 'Second mock model must appear in the official catalog')
    await acp.request(methods.agent.session.setConfigOption, { sessionId: a.sessionId, configId: 'model', value: alternate.value })
    const beforeSwitch = mock.requests.length
    await acp.prompt(a.sessionId, 'TELEBOT_TEST:MODEL_SWITCH_CHECK')
    assert.equal(mock.requests[beforeSwitch].model, 'telebot-mock-b')
    check('ACP model selection changes the next actual model request')

    const waitingToken = `WAIT_FOR_CANCEL:${randomUUID()}`
    const pending = acp.prompt(a.sessionId, `TELEBOT_TEST:${waitingToken}`).then(value => ({ value }), error => ({ error }))
    await deadline(mock.waitFor(entry => entry.instruction.includes(waitingToken)), 'model request before cancellation')
    assert.equal((await acp.prompt(b.sessionId, 'TELEBOT_TEST:INDEPENDENT_SESSION_CHECK')).stopReason, 'end_turn')
    await acp.notify(methods.agent.session.cancel, { sessionId: a.sessionId })
    const cancelled = await deadline(pending, 'cancelled prompt settlement')
    if (cancelled.error) throw cancelled.error
    assert.equal(cancelled.value.stopReason, 'cancelled')
    check('one session remains responsive while another is cancelled through ACP')

    await acp.request(methods.agent.session.close, { sessionId: a.sessionId })
    await acp.request(methods.agent.session.close, { sessionId: b.sessionId })
    await acp.stop()
    acp = undefined
    acp = await open('acp-restarted')
    const listed = await acp.request(methods.agent.session.list, { cwd: cwdA })
    assert(listed.sessions.some(session => session.sessionId === a.sessionId))
    const beforeResume = acp.updates.length
    await acp.request(methods.agent.session.resume, { sessionId: a.sessionId, cwd: cwdA, mcpServers: [] })
    assert(!acp.updates.slice(beforeResume).some(({ update }) => update.sessionUpdate === 'agent_message_chunk'), 'Resume must not replay conversation history')
    assert.equal((await acp.prompt(a.sessionId, 'TELEBOT_TEST:RESUMED_SESSION_CHECK')).stopReason, 'end_turn')
    const resumed = mock.requests.at(-1)
    assert.equal(resumed.model, 'telebot-mock-b')
    assert(JSON.stringify(resumed.body.messages).includes(token), 'Persisted tool history must survive restart')
    check('ACP close/restart/list/resume preserves history and model selection without replay')
    await acp.request(methods.agent.session.close, { sessionId: a.sessionId })
    await acp.stop()
    acp = undefined

    acp = await open('acp-disabled', true)
    const disabled = await acp.request(methods.agent.session.new, { cwd: cwdB, mcpServers: [] })
    const beforeDisabled = mock.requests.length
    await acp.prompt(disabled.sessionId, 'TELEBOT_TEST:DISABLED_PLUGIN_CHECK')
    assert(mock.requests.slice(beforeDisabled).every(entry => !entry.names.includes('telebot_echo')))
    check('disabled fixture disappears from the real model tool catalog')
    await acp.request(methods.agent.session.close, { sessionId: disabled.sessionId })
    await acp.stop()
    acp = undefined
  } finally {
    if (acp) { await acp.stop().catch(() => {}); acp = undefined }
    await rename(`${artifacts}.unavailable`, artifacts)
  }

  for (const entry of [...manifest.packages].reverse().filter(entry => !['@telebot/core', '@telebot/diagnostics'].includes(entry.name))) await command(['plugin', '--profile', profile, 'remove', entry.name])
  await command(['plugin', '--profile', profile, 'remove', '@telebot/diagnostics'])
  await command(['plugin', '--profile', profile, 'remove', '@telebot/core'])
  const afterRemoval = JSON.parse(await readFile(path.join(profileDir, 'package.json'), 'utf8'))
  assert(!afterRemoval.dependencies?.['@telebot/core'] && !afterRemoval.dependencies?.['@telebot/diagnostics'])
  check('both installed bundles are removable through the official plugin manager')
  evidence.status = 'passed'
  await save()
  console.log(`Evidence: ${report}`)
} catch (error) {
  evidence.status = 'failed'
  evidence.error = error.message
  await save()
  throw error
} finally {
  if (acp) await acp.stop().catch(() => {})
  if (mock) await mock.close()
  await cleanupVerification(work, evidence.status === 'passed')
}
