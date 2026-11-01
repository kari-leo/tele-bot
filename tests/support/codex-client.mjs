/** Minimal test-only JSONL client for the official Codex app-server protocol. */
import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { deadline } from './acp-client.mjs'

export async function startCodex({ cli, cwd, env }) {
  const child = spawn(process.execPath, [cli, 'app-server', '--listen', 'stdio://'], {
    cwd, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
  })
  let counter = 0, stderr = ''
  const pending = new Map(), notifications = [], listeners = new Set()
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-16000) })
  const exited = new Promise(resolve => child.once('exit', resolve))
  const lines = createInterface({ input: child.stdout })
  const send = value => child.stdin.write(JSON.stringify(value) + '\n')
  lines.on('line', line => {
    const message = JSON.parse(line)
    if (message.id !== undefined && !message.method) {
      const target = pending.get(message.id)
      pending.delete(message.id)
      if (message.error) target?.reject(new Error(message.error.message))
      else target?.resolve(message.result)
    } else if (message.id !== undefined) {
      // No test may approve an external write, login, or capability escalation.
      send({ id: message.id, error: { code: -32601, message: 'Test client refuses server-side approval requests' } })
    } else {
      notifications.push(message)
      for (const listener of [...listeners]) if (listener.test(message)) { listeners.delete(listener); listener.resolve(message) }
    }
  })
  child.on('error', error => { for (const target of pending.values()) target.reject(error) })
  child.on('exit', () => { for (const target of pending.values()) target.reject(new Error(`Codex exited: ${stderr}`)) })
  const request = (method, params) => {
    const id = ++counter
    const result = new Promise((resolve, reject) => pending.set(id, { resolve, reject }))
    send({ id, method, params })
    return deadline(result, `Codex ${method}`, 30000).finally(() => pending.delete(id))
  }
  const stop = async () => {
    child.stdin.end()
    try { await deadline(exited, 'Codex exit', 5000) }
    finally { if (child.exitCode === null) child.kill(); lines.close() }
  }
  try {
    const initialized = await request('initialize', { clientInfo: { name: 'telebot-p0', title: 'Telebot P0', version: '0.1.0' } })
    send({ method: 'initialized', params: {} })
    return { request, notifications, initialized, stop, waitFor(test) {
      const existing = notifications.find(test)
      if (existing) return Promise.resolve(existing)
      return deadline(new Promise(resolve => listeners.add({ test, resolve })), 'Codex notification', 30000)
    } }
  } catch (error) { await stop(); throw new Error(`${error.message}\n${stderr}`, { cause: error }) }
}
