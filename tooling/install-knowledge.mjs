import { readFile, writeFile, mkdir, copyFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { parseArgs } from 'node:util'
import { randomUUID } from 'node:crypto'
import { officialHost } from './official-host.mjs'
import { root, run } from './common.mjs'

// Install the upstream bundle through the official manager. No host/source fork or repackaging.
const { values } = parseArgs({ options: { home: { type: 'string' }, profile: { type: 'string' }, report: { type: 'string' }, 'runtime-root': { type: 'string' } } })
const { knowledge } = JSON.parse(await readFile(new URL('./external-plugins.json', import.meta.url), 'utf8'))
const host = await officialHost()
const home = values.home ?? process.env.DSH_HOME ?? path.join(homedir(), '.dsh')
const profile = values.profile ?? knowledge.defaultProfile
if (!path.isAbsolute(home) || !/^[a-zA-Z0-9_-]+$/.test(profile)) throw new Error('An absolute home and a simple profile name are required')
const directory = path.join(home, 'profiles', profile)
const env = { ...process.env, DSH_HOME: home }
const runtimeRoot = values['runtime-root'] ?? process.env.TELEBOT_RUNTIME_ROOT
if (runtimeRoot) {
  if (!path.isAbsolute(runtimeRoot)) throw new Error('runtime-root must be absolute')
  for (const name of ['tmp', 'cache/npm', 'cache/pnpm/store']) await mkdir(path.join(runtimeRoot, name), { recursive: true })
  env.TEMP = env.TMP = path.join(runtimeRoot, 'tmp')
  env.npm_config_cache = path.join(runtimeRoot, 'cache/npm')
  env.npm_config_store_dir = path.join(runtimeRoot, 'cache/pnpm/store')
}
const command = (args, options = {}) => run(process.execPath, [host.cli, ...args], { cwd: home, env, timeout: 900000, ...options })
const backup = path.join(home, 'backups', 'telebot-knowledge-' + randomUUID())
const report = { assessedAt: new Date().toISOString(), package: knowledge.package, version: knowledge.version, officialHarness: host.version, profile, status: 'running', backup, modelsDownloaded: false, userDataImported: false }
await mkdir(home, { recursive: true })
await mkdir(backup, { recursive: true, mode: 0o700 })
try {
  for (const name of ['package.json', 'pnpm-workspace.yaml', 'pnpm-lock.yaml', 'cordis.yml', 'cordis.patch.yml']) {
    try { await copyFile(path.join(directory, name), path.join(backup, name)) } catch (error) { if (error.code !== 'ENOENT') throw error }
  }
  try { await readFile(path.join(directory, 'package.json')) } catch (error) {
    if (error.code !== 'ENOENT') throw error
    // Capture the initialized config instead of echoing a potentially sensitive profile.
    await command(['--profile', profile, '--from-default-profile', 'web', '--dump-config'])
  }
  const require = createRequire(host.cli)
  const { parseDocument } = require('yaml')
  const file = path.join(directory, 'pnpm-workspace.yaml')
  const document = parseDocument(await readFile(file, 'utf8'))
  if (document.errors.length) throw new Error('Invalid existing profile workspace; backup kept and file not replaced')
  for (const [name, allowed] of Object.entries(knowledge.builds)) document.setIn(['allowBuilds', name], allowed)
  if (runtimeRoot) document.set('storeDir', path.join(runtimeRoot, 'cache/pnpm/store').replaceAll('\\', '/'))
  await writeFile(file, String(document))
  console.log(`Installing ${knowledge.package}@${knowledge.version} into official profile ${profile}; profile files backed up.`)
  await command(['plugin', '--profile', profile, 'add', `${knowledge.package}@${knowledge.version}`, '--save-exact'], { stdio: 'inherit' })
  const saved = JSON.parse(await readFile(path.join(directory, 'package.json'), 'utf8'))
  if (saved.dependencies?.[knowledge.package] !== knowledge.version) throw new Error('Official profile did not retain the exact requested version')
  const installed = JSON.parse(await readFile(path.join(directory, 'node_modules', knowledge.package, 'package.json'), 'utf8'))
  if (installed.version !== knowledge.version || !installed.dsh?.bundle?.patch) throw new Error('Installed knowledge bundle is incomplete or has a different version')
  if (!saved.dsh?.profile?.bundles?.includes(knowledge.package)) throw new Error('Bundle registration is missing from the official profile manifest')
  report.status = 'installed'
  console.log(`Installed and registered ${knowledge.package}@${installed.version}. Existing user libraries were not imported or changed.`)
} catch (error) {
  report.status = 'failed'
  report.error = error.message
  throw error
} finally {
  const target = values.report ? path.resolve(values.report) : path.join(root, '.runtime', 'knowledge-install-latest.json')
  await mkdir(path.dirname(target), { recursive: true })
  await writeFile(target, JSON.stringify(report, null, 2) + '\n')
}
