import { cleanupVerification } from '../../tooling/cleanup-verification.mjs'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { tmpdir } from 'node:os'
import { createServer } from 'node:net'
import { once } from 'node:events'
import { root, run } from '../../tooling/common.mjs'
import { officialHost, verificationEnvironment } from '../../tooling/official-host.mjs'
import { startAcp } from '../support/acp-client.mjs'
import { eventually } from '../support/temp-dir.mjs'

const host = await officialHost()
const tempRoot = process.env.TELEBOT_VERIFY_TEMP ?? tmpdir()
if (!path.isAbsolute(tempRoot)) throw new Error('TELEBOT_VERIFY_TEMP must be absolute')
await mkdir(tempRoot, { recursive: true })
const work = await mkdtemp(path.join(tempRoot, 'telebot-knowledge-'))
const home = path.join(work, 'home'), user = path.join(work, 'user'), profile = 'knowledge-check'
await mkdir(home); await mkdir(user)
const env = { ...verificationEnvironment(home), HOME: user, USERPROFILE: user, TELEBOT_DSH_INSTALL: host.installRoot }
const { knowledge } = JSON.parse(await readFile(new URL('../../tooling/external-plugins.json', import.meta.url), 'utf8'))
const report = { assessedAt: new Date().toISOString(), runtime: host.version, knowledge: knowledge.version, realHarness: true, paidModels: false, importedUserData: false, checks: [], status: 'running' }
const check = message => { report.checks.push(message); console.log('PASS ' + message) }
const command = args => run(process.execPath, [host.cli, ...args], { cwd: work, env })
const reservation = createServer(); reservation.listen(0, '127.0.0.1'); await once(reservation, 'listening')
const port = reservation.address().port; await new Promise(resolve => reservation.close(resolve))
const origin = 'http://127.0.0.1:' + port, patch = path.join(work, 'web-check.json')
await writeFile(patch, JSON.stringify([
  { id: 'webserver', config: { host: '127.0.0.1', port } },
  { id: 'web-runtime', config: { printUrl: false, openBrowser: false } },
  { id: 'session-title-llm', disabled: true },
  { insert: [{ id: 'acp-app-startup', name: '@deepseek-ai/dsh-acp-app' }, { id: 'acp', name: '@deepseek-ai/dsh-acp', inject: ['acpAppStartup'] }] },
]))
let acp
async function stop() { if (acp) { await acp.stop(); acp = undefined } }
async function api(route, method = 'GET', body) {
  const response = await fetch(origin + '/knowledge' + route, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(30000) })
  const result = await response.json()
  assert.equal(response.status, 200, JSON.stringify(result)); assert.equal(result.ok, true, JSON.stringify(result))
  return result.value
}
async function open(label) {
  acp = await startAcp({ cli: host.cli, profile, patches: [patch], cwd: work, env, log: path.join(work, label + '.log') })
  await eventually(async () => { try { return Array.isArray(await api('/bases')) } catch { return false } }, 30000)
}
try {
  await command(['--profile', profile, '--from-default-profile', 'web', '--dump-config'])
  await run(process.execPath, [path.join(root, 'tooling/install-knowledge.mjs'), '--home', home, '--profile', profile, '--report', path.join(work, 'installation.json'), ...(process.env.TELEBOT_RUNTIME_ROOT ? ['--runtime-root', process.env.TELEBOT_RUNTIME_ROOT] : [])], { cwd: work, env, timeout: 900000, stdio: 'inherit' })
  check('pinned upstream bundle installs and registers through the official manager in an isolated profile')
  await open('first')
  assert.deepEqual(await api('/bases'), [])
  assert.equal((await api('/config')).embeddingProvider, 'none')
  check('actual official Web composition loads knowledge routes with no embedding provider or paid model')
  const base = await api('/bases', 'POST', { name: 'Isolated acceptance only' })
  const doc = await api('/bases/' + base.id + '/documents', 'POST', { title: 'Acceptance note', content: 'The verification passphrase is windborne-nebula. Local lexical retrieval requires no remote model.' })
  assert(doc.id)
  check('a synthetic document is ingested and chunked through the upstream management API')
  const found = await api('/search', 'POST', { baseId: base.id, query: 'windborne-nebula', topK: 3 })
  assert(found.hits.some(hit => hit.docId === doc.id && hit.text.includes('windborne-nebula')))
  check('keyword search returns a real stored chunk from the requested knowledge base')
  await stop(); await open('restarted')
  assert((await api('/bases')).some(item => item.id === base.id))
  assert((await api('/search', 'POST', { baseId: base.id, query: 'windborne-nebula', topK: 3 })).hits.some(hit => hit.docId === doc.id))
  check('official host restart retains base metadata, SQLite chunks and searchable content')
  await stop()
  await command(['plugin', '--profile', profile, 'remove', knowledge.package])
  const manifest = JSON.parse(await readFile(path.join(home, 'profiles', profile, 'package.json'), 'utf8'))
  assert(!manifest.dependencies?.[knowledge.package]); assert(!manifest.dsh.profile.bundles.includes(knowledge.package))
  check('official uninstall removes the isolated bundle registration without changing the user profile')
  report.status = 'passed'
} catch (error) { report.status = 'failed'; report.error = error.message; throw error }
finally {
  await stop().catch(() => {})
  await mkdir(path.join(root, '.runtime'), { recursive: true })
  await writeFile(path.join(root, '.runtime/verify-knowledge-latest.json'), JSON.stringify(report, null, 2) + '\n')
  console.log('Knowledge evidence: ' + path.join(root, '.runtime/verify-knowledge-latest.json'))
  console.log('Isolated evidence directory: ' + work)
  await cleanupVerification(work, report.status === 'passed')
}
