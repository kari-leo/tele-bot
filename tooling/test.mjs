import { run, root } from './common.mjs'
import path from 'node:path'
import { readFile, readdir } from 'node:fs/promises'
import { parseArgs } from 'node:util'

const suites = JSON.parse(await readFile(new URL('./test-suites.json', import.meta.url), 'utf8'))
const catalog = Object.values(suites).flatMap(suite => suite.files)
const discovered = (await readdir(path.join(root, 'tests/acceptance'))).filter(file => file.endsWith('.test.mjs')).map(file => `tests/acceptance/${file}`)
if (new Set(catalog).size !== catalog.length || discovered.length !== catalog.length || discovered.some(file => !catalog.includes(file))) {
  throw new Error('Every acceptance test must appear in exactly one group in tooling/test-suites.json')
}
const { values } = parseArgs({ options: { suite: { type: 'string', multiple: true }, list: { type: 'boolean', default: false } } })
const selected = values.suite ?? Object.keys(suites)
for (const name of selected) if (!Object.hasOwn(suites, name)) throw new Error(`Unknown suite: ${name}. Available: ${Object.keys(suites).join(', ')}`)
if (values.list) {
  for (const name of selected) console.log(`${name}: ${suites[name].description}\n  ${suites[name].files.join('\n  ')}`)
} else {
  const files = [...new Set(selected.flatMap(name => suites[name].files))]

  await run(process.execPath, [path.join(root, 'tooling/build.mjs')], { stdio: 'inherit' })
  await run(process.execPath, ['--test', '--test-timeout=30000', ...files], { stdio: 'inherit' })
}
