import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, realpath, rm, symlink, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { root } from '../../tooling/common.mjs'
import { officialHost, verificationEnvironment } from '../../tooling/official-host.mjs'
import {modules, selectModules, selectTests} from '../../tooling/verification-catalog.mjs'
import ts from 'typescript'

test('module selection covers every local assertion while unrelated modules remain excluded and shared files run once',async()=>{
  const all=selectTests()
  assert.equal(new Set(all.map(row=>row.file)).size,all.length)
  for(const spec of all){const source=await readFile(path.join(root,spec.file),'utf8'),ast=ts.createSourceFile(spec.file,source,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS),names=ast.statements.filter(node=>ts.isExpressionStatement(node)&&ts.isCallExpression(node.expression)&&node.expression.expression.getText(ast)==='test').map(node=>node.expression.arguments[0].text);assert(names.length,'No assertions: '+spec.file);for(const name of names)assert(!spec.pattern||new RegExp(spec.pattern).test(name),'Uncovered assertion: '+name)}
  const feishu=selectTests(['feishu','feishu-business'])
  assert(!feishu.some(row=>/publishing|automation|desktop\.test|skills-and-limits/.test(row.file)))
  const shared=feishu.find(row=>row.file.endsWith('production-business.test.mjs'))
  assert(new RegExp(shared.pattern).test('P2 attachment'));assert(new RegExp(shared.pattern).test('P3 resource'));assert(!new RegExp(shared.pattern).test('P4 OpenCLI'))
  assert.throws(()=>selectModules(['typo']),/Unknown module/)
  assert(modules.settings.tests.some(row=>row.file.endsWith('native-configuration-ui.test.mjs')))
})

test('official acceptance cannot silently use a source workspace, including a junction alias', async () => {
  await assert.rejects(officialHost(root), /outside this repository/)
  const temporaryParent = await realpath(tmpdir())
  const temporary = await mkdtemp(path.join(temporaryParent, 'telebot-host-alias-'))
  try {
    const alias = path.join(temporary, 'host')
    await symlink(root, alias, process.platform === 'win32' ? 'junction' : 'dir')
    await assert.rejects(officialHost(alias), /outside this repository/)
  } finally {
    assert.equal(path.dirname(temporary), temporaryParent, 'Cleanup must remain inside the test temporary directory')
    await rm(temporary, { recursive: true, force: true })
  }
})

test('verification child receives neither model credentials nor developer module injection', () => {
  const keys = ['DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'NODE_PATH', 'NODE_OPTIONS']
  const previous = keys.map(key => process.env[key])
  try {
    for (const key of keys) process.env[key] = 'must-not-reach-the-test-host'
    const actual = verificationEnvironment('/temporary-test-home')
    for (const key of keys) assert.equal(actual[key], undefined)
    assert.equal(actual.DSH_HOME, '/temporary-test-home')
    assert.equal(actual.TELEBOT_VERIFY_KEY, 'loopback-test-only')
  } finally {
    keys.forEach((key, index) => {
      if (previous[index] === undefined) delete process.env[key]
      else process.env[key] = previous[index]
    })
  }
})
