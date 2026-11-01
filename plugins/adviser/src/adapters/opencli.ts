import { spawn } from 'node:child_process'
import path from 'node:path'
import type { AdviserInput, AdviserPort } from '../domain/adviser.js'

/** One stateless OpenCLI request, not an Agent or a generic shell executor. */
export class OpenCliAdviser implements AdviserPort {
  private readonly processes = new Set<ReturnType<typeof spawn>>()
  constructor(private readonly cli: string) { if (!path.isAbsolute(cli)) throw new Error('absolute_opencli_path_required') }
  ask(input: AdviserInput, signal: AbortSignal): Promise<string> {
    signal.throwIfAborted()
    const prompt = ['Act as a concise second-opinion adviser. Give a recommendation and tradeoff; do not execute actions.',
      input.context, ...input.excerpts, 'Question:', input.question].join('\n\n')
    if (Buffer.byteLength(prompt) > 100000) throw new Error('adviser_input_budget_exceeded')
    const args = ['chatgpt', 'ask', '--new', '-f', 'plain', prompt]
    const child = /\.[cm]?js$/.test(this.cli) ? spawn(process.execPath, [this.cli, ...args], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
      : spawn(this.cli, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    this.processes.add(child)
    let output = '', overflow = false
    const abort = () => child.kill()
    signal.addEventListener('abort', abort, { once: true })
    child.stderr?.resume()
    child.stdout?.on('data', chunk => { if (Buffer.byteLength(output) + chunk.length > 120000) { overflow = true; child.kill() } else output += chunk })
    return new Promise((resolve, reject) => {
      child.once('error', () => reject(new Error('adviser_provider_unavailable')))
      child.once('close', code => {
        this.processes.delete(child); signal.removeEventListener('abort', abort)
        if (code !== 0 || signal.aborted || overflow) { reject(new Error('adviser_provider_failed')); return }
        const match = /^response:\s*([\s\S]*)/im.exec(output)
        const result = (match?.[1] ?? output).trim()
        if (result) resolve(result); else reject(new Error('adviser_empty_response'))
      })
    })
  }
  async dispose() {
    await Promise.allSettled([...this.processes].map(child => new Promise<void>(resolve => {
      child.once('close', () => resolve()); child.kill(); setTimeout(resolve, 2000).unref()
    })))
  }
}
