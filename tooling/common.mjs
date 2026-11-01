import { spawn } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import path from 'node:path'

export const root = fileURLToPath(new URL('../', import.meta.url))
export const compatibility = JSON.parse(await readFile(new URL('./compatibility.json', import.meta.url), 'utf8'))
export const packages = ['plugins/core', 'plugins/desktop', 'plugins/feishu', 'plugins/codex', 'plugins/adviser', 'plugins/publishing', 'plugins/automation']
export const fixtures = ['tests/fixtures/diagnostics']

/** Resolve npm without passing paths or package specs through a command shell. */
export function npmCli() {
  const require = createRequire(import.meta.url)
  try { return require.resolve('npm/bin/npm-cli.js') } catch {
    return path.join(path.dirname(process.execPath), 'node_modules/npm/bin/npm-cli.js')
  }
}

export function run(command, args, { cwd = root, env = process.env, timeout = 180000, ...options } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], ...options })
    let stdout = '', stderr = ''
    const timer = setTimeout(() => { child.kill(); reject(new Error(`Command timed out: ${command}`)) }, timeout)
    child.stdout?.on('data', data => { stdout += data })
    child.stderr?.on('data', data => { stderr += data })
    child.once('error', error => { clearTimeout(timer); reject(error) })
    child.once('exit', (code, signal) => {
      clearTimeout(timer)
      if (code === 0) resolve({ code, stdout, stderr })
      else reject(Object.assign(new Error(`${command} exited ${code ?? signal}\n${stderr}\n${stdout}`), { code, stdout, stderr }))
    })
  })
}
