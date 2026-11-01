import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, realpath, rm, symlink } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { root } from '../../tooling/common.mjs'
import { officialHost, verificationEnvironment } from '../../tooling/official-host.mjs'

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
