import { run, root } from './common.mjs'
import path from 'node:path'
import { readdir } from 'node:fs/promises'
import { parseArgs } from 'node:util'
import { modules, selectModules, selectTests } from './verification-catalog.mjs'

const catalog = selectTests().map(row=>row.file)
const discovered = (await readdir(path.join(root, 'tests/acceptance'))).filter(file => file.endsWith('.test.mjs')).map(file => `tests/acceptance/${file}`)
if (discovered.length !== catalog.length || discovered.some(file => !catalog.includes(file))) {
  throw new Error('Every acceptance test must appear in tooling/verification-catalog.mjs')
}
const { values } = parseArgs({ options: { module: {type:'string',multiple:true},suite: { type: 'string', multiple: true }, list: { type: 'boolean', default: false }, 'no-build':{type:'boolean',default:false} } })
const selected = selectModules(values.module??values.suite)
if (values.list) {
  for (const name of selected) console.log(`${name}: ${modules[name].description}\n  ${modules[name].tests.map(row=>row.file+(row.pattern?' ['+row.pattern+']':'')).join('\n  ')}`)
} else {
  if(!values['no-build'])await run(process.execPath, [path.join(root, 'tooling/build.mjs'),...selected.flatMap(name=>['--module',name])], { stdio: 'inherit' })
  const groups=new Map()
  for(const spec of selectTests(selected)){const key=spec.pattern??'';groups.set(key,[...(groups.get(key)??[]),spec.file])}
  for(const [pattern,files]of groups)await run(process.execPath, ['--test', '--test-timeout=30000',...(pattern?['--test-name-pattern',pattern]:[]), ...files], { stdio: 'inherit' })
}
