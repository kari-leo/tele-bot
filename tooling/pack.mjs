import { createHash } from 'node:crypto'
import { cp, mkdir, mkdtemp, readFile, writeFile, rm } from 'node:fs/promises'
import path from 'node:path'
import { root, packages, fixtures, compatibility, run, npmCli } from './common.mjs'
import { checkArtifacts } from './check-artifacts.mjs'

const dist = path.join(root, 'dist')
await mkdir(dist, { recursive: true })
await mkdir(path.join(root, '.runtime'), { recursive: true })
const staging = await mkdtemp(path.join(root, '.runtime/pack-'))
const manifest = { harness: compatibility.candidate, packages: [] }
const selected = [...packages, ...(process.argv.includes('--fixtures') ? fixtures : [])]
try {
for (const directory of selected) {
  const source = path.join(root, directory)
  const original = JSON.parse(await readFile(path.join(source, 'package.json'), 'utf8'))
  const { devDependencies, scripts, packageManager, ...runtimeManifest } = original
  const destination = path.join(staging, original.name.replaceAll('/', '-'))
  await mkdir(destination)
  for (const entry of original.files) await cp(path.join(source, entry), path.join(destination, entry), { recursive: true })
  await writeFile(path.join(destination, 'package.json'), JSON.stringify(runtimeManifest, null, 2) + '\n')
  const packed = await run(process.execPath, [npmCli(), 'pack', destination, '--pack-destination', dist, '--ignore-scripts', '--json'])
  const [{ filename }] = JSON.parse(packed.stdout)
  const sha256 = createHash('sha256').update(await readFile(path.join(dist, filename))).digest('hex')
  manifest.packages.push({ name: original.name, version: original.version, filename, sha256, fixture: fixtures.includes(directory) })
}
await writeFile(path.join(dist, 'install-manifest.json'), JSON.stringify(manifest, null, 2) + '\n')
await writeFile(path.join(dist, 'INSTALL.txt'), [
  `Requires official Harness ${compatibility.candidate}. Choose the target profile and install packages in manifest order.`,
  'Use absolute tarball paths: dsh plugin --profile <profile> add file:<absolute-tarball-path>',
  'Entries marked fixture are test-only; omit them from production installations.',
  'Start the official profile with dsh --profile <profile>. No source checkout is needed.',
  '',
].join('\n'))
await checkArtifacts(dist)

} finally {
  if (path.dirname(staging) !== path.join(root, '.runtime') || !path.basename(staging).startsWith('pack-')) throw new Error('pack_cleanup_outside_workspace')
  await rm(staging, { recursive: true, force: true })
}
