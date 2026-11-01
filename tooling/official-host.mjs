import { createRequire } from 'node:module'
import { readFile, realpath } from 'node:fs/promises'
import path from 'node:path'
import { compatibility, root } from './common.mjs'

function inside(parent, child) {
  const relative = path.relative(parent, child)
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative))
}

/** Resolve an explicit, independently installed official release, never the repository's peers. */
export async function officialHost(install = process.env.TELEBOT_DSH_INSTALL) {
  if (!install || !path.isAbsolute(install)) {
    throw new Error('Set TELEBOT_DSH_INSTALL to the absolute external npm installation prefix. See docs/installation.md.')
  }
  const sourceRoot = await realpath(root)
  const installRoot = await realpath(install)
  if (inside(sourceRoot, installRoot)) throw new Error('The official Harness test installation must be outside this repository')
  const require = createRequire(path.join(installRoot, 'package.json'))
  const manifestPath = await realpath(require.resolve('@deepseek-ai/dsh/package.json'))
  if (!inside(installRoot, manifestPath) || inside(sourceRoot, manifestPath)) {
    throw new Error('The selected Harness resolves outside its independent installation')
  }
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'))
  if (manifest.version !== compatibility.candidate) {
    throw new Error(`Expected official Harness ${compatibility.candidate}, got ${manifest.version}`)
  }
  return { installRoot, version: manifest.version, cli: path.join(path.dirname(manifestPath), 'lib/bin.js') }
}

/** Keep real model credentials and developer Node resolution out of the test process. */
export function verificationEnvironment(home) {
  const environment = {}
  const allowed = new Set([
    'path', 'pathext', 'systemroot', 'windir', 'comspec', 'temp', 'tmp',
    'home', 'userprofile', 'appdata', 'localappdata',
    'http_proxy', 'https_proxy', 'no_proxy',
  ])
  for (const [key, value] of Object.entries(process.env)) {
    if (allowed.has(key.toLowerCase())) environment[key] = value
  }
  return { ...environment, DSH_HOME: home, TELEBOT_VERIFY_KEY: 'loopback-test-only' }
}
