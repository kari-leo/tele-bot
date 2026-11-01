import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { root, compatibility, run } from './common.mjs'

/** Check the shipped manifest, including required peer ownership and unsafe dependency specs. */
export function validateManifest(manifest, files) {
  if (manifest.name === '@telebot/feishu') assert(!files.some(file => /(?:^|\/)oauth(?:[.-]|\/)/i.test(file)), 'Removed OAuth code must not ship')
  assert.equal(manifest.type, 'module', 'Plugin must be ESM')
  assert.equal(manifest.engines?.dsh, compatibility.candidate, 'Exact Harness version required')
  assert(manifest.dsh?.bundle?.patch, 'Bundle patch missing')
  for (const section of ['dependencies', 'optionalDependencies', 'peerDependencies']) {
    for (const [name, spec] of Object.entries(manifest[section] ?? {})) {
      assert(!/^(workspace:|link:|file:)|[\\/](vendor|plugins|internal)[\\/]/.test(spec), `Nonportable ${section}: ${name}`)
      if (name.startsWith('@deepseek-ai/')) assert(section === 'peerDependencies', `Host package must remain a peer: ${name}`)
      if (name === '@telebot/core') assert(section === 'peerDependencies', 'Core must have one runtime owner')
    }
  }
  assert.equal(manifest.devDependencies, undefined, 'Development dependencies must not ship')
  const targets = [manifest.main, manifest.types, manifest.dsh.bundle.patch]
  const visit = value => {
    if (typeof value === 'string') targets.push(value)
    else if (value && typeof value === 'object') Object.values(value).forEach(visit)
  }
  visit(manifest.exports)
  for (const target of targets) {
    assert(typeof target === 'string' && !target.includes('..'), `Invalid package target: ${target}`)
    assert(files.includes(`package/${target.replace(/^\.\//, '')}`), `Missing shipped target: ${target}`)
  }
  for (const file of files) {
    assert(!file.includes('\\') && !file.startsWith('/') && !file.split('/').includes('..'), `Invalid archive path: ${file}`)
    assert(!/(^|\/)(src|node_modules|data|vendor|\.git)(\/|$)|(^|\/)\.env(?:\.|$)|\.(?:sqlite|map)$/.test(file), `Forbidden archive content: ${file}`)
  }
}

/** Inspect actual archive contents without extracting or executing them. */
export async function inspectTarball(filename) {
  const listing = await run('tar', ['-tzf', filename])
  const files = listing.stdout.trim().split(/\r?\n/)
  const packed = await run('tar', ['-xOzf', filename, 'package/package.json'])
  const manifest = JSON.parse(packed.stdout)
  validateManifest(manifest, files)
  for (const file of files.filter(name => /\.(?:js|ts|yml|yaml)$/.test(name))) {
    const content = (await run('tar', ['-xOzf', filename, file])).stdout
    assert(!/vendor[\\/]deepseek-harness|(?:\.\.\/)+(?:plugins|internal|src)\/|["'](?:link:|workspace:)/.test(content), `Source dependency in ${file}`)
  }
  return { name: manifest.name, version: manifest.version, files: files.length }
}

export async function checkArtifacts(directory = path.join(root, 'dist')) {
  const manifest = JSON.parse(await readFile(path.join(directory, 'install-manifest.json'), 'utf8'))
  for (const entry of manifest.packages) {
    assert.equal(path.basename(entry.filename), entry.filename, 'Artifact filename must stay inside dist')
    const filename = path.join(directory, entry.filename)
    assert.equal(createHash('sha256').update(await readFile(filename)).digest('hex'), entry.sha256, `Checksum mismatch: ${entry.filename}`)
    const actual = await inspectTarball(filename)
    assert.equal(actual.name, entry.name)
    assert.equal(actual.version, entry.version)
    console.log(`Checked ${actual.name}: ${actual.files} shipped files`)
  }
  return manifest
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await checkArtifacts()
