import { createRequire } from 'node:module'
import { realpath, rm } from 'node:fs/promises'
import path from 'node:path'
import { root, packages, fixtures, run } from './common.mjs'

const require = createRequire(import.meta.url)
const tsc = require.resolve('typescript/bin/tsc')
// Domain implementations are compiled/tested before their installable wiring exists.
// Do not put these projects in the release package list until that wiring is verified.
for (const directory of [...packages, ...fixtures]) {
  const project = path.resolve(root, directory), output = path.join(project, 'lib')
  const realRoot = await realpath(root)
  const realOutput = await realpath(output).catch(error => { if (error.code !== 'ENOENT') throw error; return undefined })
  if (!project.startsWith(path.resolve(root) + path.sep) || (realOutput && !realOutput.startsWith(realRoot + path.sep))) throw new Error('build_output_outside_workspace')
  // Removed sources must not survive as stale code in a newly packed plugin.
  await rm(output, { recursive: true, force: true })
  const result = await run(process.execPath, [tsc, '-p', path.join(root, directory, 'tsconfig.json')])
  process.stdout.write(result.stdout)
  console.log(`Built ${directory}`)
}
