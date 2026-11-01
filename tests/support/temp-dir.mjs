import assert from 'node:assert/strict'
import { mkdtemp, realpath, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'

const resources = new WeakMap()
export function cleanup(test, operation) {
  let stack = resources.get(test)
  if (!stack) {
    stack = []
    resources.set(test, stack)
    test.after(async () => {
      const failures = []
      for (const dispose of stack.toReversed()) {
        try { await dispose() } catch (error) { failures.push(error) }
      }
      if (failures.length) throw new AggregateError(failures, 'Test cleanup failed')
    })
  }
  stack.push(operation)
}

export async function temporaryDirectory(test, prefix = 'telebot-test-') {
  const parent = await realpath(tmpdir())
  const directory = await mkdtemp(path.join(parent, prefix))
  cleanup(test, async () => {
    assert.equal(path.dirname(directory), parent)
    await rm(directory, { recursive: true, force: true })
  })
  return directory
}
export async function eventually(predicate, timeout = 4000) {
  const deadline = Date.now() + timeout
  while (!await predicate()) {
    if (Date.now() >= deadline) throw new Error('Condition did not settle before test deadline')
    await new Promise(resolve => setTimeout(resolve, 10))
  }
}
