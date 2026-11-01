import { spawn } from 'node:child_process'
import { Readable, Writable } from 'node:stream'
import { writeFile } from 'node:fs/promises'
import { appendFileSync } from 'node:fs'
import { client, methods, ndJsonStream, PROTOCOL_VERSION } from '@agentclientprotocol/sdk'

/** Bound individual protocol operations while preserving the official SDK's cancellation. */
export async function deadline(promise, label, timeout = 60000) {
  let timer
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`Timed out: ${label}`)), timeout) }),
    ])
  } finally { clearTimeout(timer) }
}

/** Drive the shipped ACP profile over stdio using the official stable-v1 SDK. */
export async function startAcp({ cli, profile, patches, cwd, env, log, args = [] }) {
  const child = spawn(process.execPath, [cli, '--profile', profile, ...patches.flatMap(file => ['--patch', file]), ...args], {
    cwd, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
  })
  let stderr = ''
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-65536); appendFileSync(log,chunk) })
  const exited = new Promise((resolve, reject) => {
    child.once('error', reject)
    child.once('exit', (code, signal) => resolve({ code, signal }))
  })
  const updates = []
  const permissions = []
  const app = client({ name: 'telebot-keyless-acceptance' })
    .onNotification(methods.client.session.update, ({ params }) => { updates.push(params) })
    .onRequest(methods.client.session.requestPermission, ({ params }) => {
      permissions.push(params)
      return { outcome: { outcome: 'cancelled' } }
    })
  const connection = app.connect(ndJsonStream(Writable.toWeb(child.stdin), Readable.toWeb(child.stdout)))
  const request = async (method, params, timeout = 60000) => {
    try { return await deadline(connection.agent.request(method, params, { signal: AbortSignal.timeout(timeout) }),method,timeout) }
    catch (error) {
      if (error.data?.details) error.message = `${method}: ${error.message}: ${error.data.details}`
      throw error
    }
  }
  const stop = async () => {
    child.stdin.end()
    let result
    try { result = await deadline(exited, 'ACP orderly shutdown', 15000) }
    finally {
      if (child.exitCode === null) child.kill()
      connection.close()
      await writeFile(log, stderr)
    }
    if (result.code !== 0) throw new Error(`ACP exited ${result.code ?? result.signal}: ${stderr.slice(-8000)}`)
  }
  try {
    const initialized = await request(methods.agent.initialize, {
      protocolVersion: PROTOCOL_VERSION,
      clientInfo: { name: 'telebot-keyless-acceptance', version: '0.1.0' },
      clientCapabilities: {},
    },120000)
    return {
      initialized, updates, permissions, request, stop,
      async crash() {
        child.kill('SIGKILL')
        await deadline(exited, 'ACP forced termination', 10000)
        connection.close()
        await writeFile(log, stderr)
      },
      notify: (method, params) => connection.agent.notify(method, params),
      prompt: (sessionId, text) => request(methods.agent.session.prompt, { sessionId, prompt: [{ type: 'text', text }] }),
    }
  } catch (error) {
    await stop().catch(() => {})
    throw new Error(`ACP initialization failed: ${error.message}\n${stderr.slice(-8000)}`, { cause: error })
  }
}
