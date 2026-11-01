/** Dedicated official Codex JSONL connector; Codex owns its Agent loop and threads. */
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { createInterface } from 'node:readline'
import path from 'node:path'
import type { CodingExecutorPort, CodingRequest } from '../domain/coding.js'

export class NativeClient {
  private readonly child: ChildProcessWithoutNullStreams
  private sequence = 0
  private readonly pending = new Map<number, { resolve(value: any): void; reject(reason: Error): void }>()
  private readonly observers = new Set<(message: any) => void>()
  private closed = false
  constructor(cli: string, cwd: string, home?: string, environment: NodeJS.ProcessEnv = process.env) {
    if (!path.isAbsolute(cli) || (home && !path.isAbsolute(home))) throw new Error('absolute_codex_paths_required')
    this.child = /\.[cm]?js$/.test(cli) ? spawn(process.execPath, [cli, 'app-server', '--listen', 'stdio://'], { cwd, windowsHide: true,
      env: { ...environment, ...(home ? { CODEX_HOME: home } : {}) }, stdio: ['pipe', 'pipe', 'pipe'] }) : spawn(cli, ['app-server', '--listen', 'stdio://'], { cwd, windowsHide: true,
      env: { ...environment, ...(home ? { CODEX_HOME: home } : {}) }, stdio: ['pipe', 'pipe', 'pipe'] })
    this.child.stderr.resume() // Do not copy provider output or credentials into business reports.
    const lines = createInterface({ input: this.child.stdout })
    lines.on('line', line => {
      let message: any
      try { message = JSON.parse(line) } catch { this.fail(new Error('invalid_codex_protocol')); return }
      if (message.id !== undefined && !message.method) {
        const waiter = this.pending.get(message.id); this.pending.delete(message.id)
        if (message.error) waiter?.reject(new Error('codex_request_rejected'))
        else waiter?.resolve(message.result)
      } else if (message.id !== undefined) {
        // The connector never approves an escalation or asks a model for consent.
        this.send({ id: message.id, error: { code: -32601, message: 'Telebot refuses unconfirmed server requests' } })
      } else for (const observer of this.observers) observer(message)
    })
    this.child.on('error', () => this.fail(new Error('codex_process_unavailable')))
    this.child.on('exit', () => { lines.close(); this.fail(new Error('codex_process_exited')) })
  }
  private fail(error: Error) { for (const row of this.pending.values()) row.reject(error); this.pending.clear() }
  private send(message: unknown) { if (!this.closed) this.child.stdin.write(JSON.stringify(message) + '\n') }
  async initialize() {
    const result = await this.request('initialize', { clientInfo: { name: 'telebot-codex', title: 'Telebot Codex', version: '0.1.0' }, capabilities: { experimentalApi: true } })
    if (!/\b0\.160\.0\b/.test(String(result.userAgent))) throw new Error('codex_version_not_tested')
    this.send({ method: 'initialized', params: {} })
  }
  request(method: string, params: unknown, signal?: AbortSignal): Promise<any> {
    signal?.throwIfAborted()
    const id = ++this.sequence
    return new Promise((resolve, reject) => {
      const abort = () => { const row = this.pending.get(id); this.pending.delete(id); row?.reject(new Error('codex_request_cancelled')) }
      const timer = setTimeout(() => { const row = this.pending.get(id); this.pending.delete(id); row?.reject(new Error('codex_request_timeout')) }, 30000)
      this.pending.set(id, { resolve: value => { clearTimeout(timer); signal?.removeEventListener('abort', abort); resolve(value) }, reject: error => { clearTimeout(timer); signal?.removeEventListener('abort', abort); reject(error) } })
      signal?.addEventListener('abort', abort, { once: true })
      this.send({ id, method, params })
    })
  }
  observe(callback: (message: any) => void) { this.observers.add(callback); return () => this.observers.delete(callback) }
  async close() {
    if (this.closed) return
    this.closed = true
    this.fail(new Error('codex_connection_closed')); this.child.stdin.end()
    if (this.child.exitCode !== null) return
    await new Promise<void>(resolve => {
      const timer = setTimeout(() => { this.child.kill(); resolve() }, 2000)
      this.child.once('exit', () => { clearTimeout(timer); resolve() })
    })
  }
}
export interface NativeResult { status: 'completed' | 'failed' | 'unknown'; text: string; threadId?: string; artifacts?: string[]; diagnostic?: string }
export class NativeCodex implements CodingExecutorPort {
  readonly enforcesReadOnly = true
  lastPermission: unknown
  lastTurnFailure: unknown
  private readonly clients = new Set<NativeClient>()
  constructor(private readonly cli: string, private readonly home?: string, private readonly environment?: NodeJS.ProcessEnv) {}
  async connect(cwd: string) { const client = new NativeClient(this.cli, cwd, this.home, this.environment); this.clients.add(client); try { await client.initialize(); return client } catch (error) { await this.release(client); throw error } }
  async release(client: NativeClient) { this.clients.delete(client); await client.close() }
  async execute(request: CodingRequest, options: { sandbox: 'read-only' | 'workspace-write' | 'danger-full-access'; signal: AbortSignal }, resumeThread?: string): Promise<NativeResult> {
    const client = await this.connect(request.cwd)
    let threadId: string | undefined, turnId: string | undefined
    const signal = options.signal
    const abort = () => {
      if (threadId && turnId) client.request('turn/interrupt', { threadId, turnId }).catch(() => {})
      // A noncooperative native turn still loses its dedicated process.
      client.close().catch(() => {})
    }
    signal.addEventListener('abort', abort, { once: true })
    try {
      signal.throwIfAborted()
      const sandboxPolicy = options.sandbox === 'danger-full-access' ? {type:'dangerFullAccess'} : options.sandbox === 'read-only' ? { type: 'readOnly', networkAccess: false }
        : { type: 'workspaceWrite', writableRoots: [request.cwd], networkAccess: false, excludeSlashTmp: true, excludeTmpdirEnvVar: true }
      const selection = { cwd: request.cwd, sandbox: options.sandbox, approvalPolicy: 'never', approvalsReviewer: 'user',
        config: { sandbox_workspace_write: { writable_roots: [request.cwd], network_access: false, exclude_slash_tmp: true, exclude_tmpdir_env_var: true } } }
      const started = await client.request(resumeThread ? 'thread/resume' : 'thread/start', { ...selection, ...(resumeThread ? { threadId: resumeThread } : { ephemeral: false }) }, signal)
      this.lastPermission = { requested: sandboxPolicy, active: started.activePermissionProfile ?? null, projection: started.sandbox }
      // CLI 0.160 may retain the home default in thread/start's legacy projection.
      // Start must never be broader than requested; the exact policy is set again
      // on turn/start, where the protocol defines the authoritative override.
      const admissible = options.sandbox === 'danger-full-access' ? ['dangerFullAccess'] : options.sandbox === 'read-only' ? ['readOnly'] : ['readOnly', 'workspaceWrite']
      if (!admissible.includes(started.sandbox.type)) throw new Error('codex_sandbox_mismatch_' + String(started.sandbox.type))
      threadId = started.thread.id
      const messages: string[] = []
      let settle: ((value: any) => void) | undefined
      const done = new Promise<any>(resolve => { settle = resolve })
      const remove = client.observe(message => {
        if (message.params?.threadId !== threadId) return
        if (message.method === 'item/completed' && message.params.item?.type === 'agentMessage') messages.push(message.params.item.text)
        if (message.method === 'turn/completed') settle?.(message.params.turn)
      })
      const cancelled = () => settle?.({ status: 'interrupted' })
      signal.addEventListener('abort', cancelled, { once: true })
      try {
        const turn = await client.request('turn/start', { threadId, cwd: request.cwd, approvalPolicy: 'never', approvalsReviewer: 'user', sandboxPolicy,
          input: [{ type: 'text', text: request.prompt+(request.skills?'\nUse only the selected Skill run copies in '+path.join(request.cwd,'.telebot-skills',request.skills.digest)+'. Selected names: '+request.skills.names.join(', ')+'. Keep deduplication state in those run copies. Never modify the original .codex skills.':''), text_elements: [] }], outputSchema: {
          type: 'object', properties: { summary: { type: 'string' }, artifacts: { type: 'array', items: { type: 'string' } } }, required: ['summary', 'artifacts'], additionalProperties: false,
        } }, signal)
        turnId = turn.turn.id
        const finished = await done
        this.lastTurnFailure = finished.error
        if (signal.aborted || finished.status !== 'completed') return { status: 'failed', text: 'Codex 执行已停止或失败。', threadId }
        let value: { summary: string; artifacts: string[] }
        try { value = JSON.parse(messages.at(-1) ?? '') } catch { return { status: 'unknown', text: 'Codex 结构化结果未确认。', threadId } }
        if (typeof value.summary !== 'string' || !Array.isArray(value.artifacts) || value.artifacts.some(item => typeof item !== 'string')) return { status: 'unknown', text: 'Codex 结构化结果不符合约定。', threadId }
        return { status: 'completed', text: value.summary, artifacts: value.artifacts, threadId }
      } finally { remove(); signal.removeEventListener('abort', cancelled) }
    } catch (error) { return { status: signal.aborted ? 'failed' : 'unknown', text: signal.aborted ? 'Codex 已取消。' : 'Codex 结果未确认，没有自动重试。', threadId,
      diagnostic: error instanceof Error && /^codex_[a-zA-Z_]+$/.test(error.message) ? error.message : 'codex_connector_failure' } }
    finally { signal.removeEventListener('abort', abort); await this.release(client) }
  }
  async dispose() { await Promise.allSettled([...this.clients].map(client => this.release(client))) }
}
