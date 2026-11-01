import { desktopFull, desktopOwner } from '@telebot/core/contracts'
import { DatabaseSync } from 'node:sqlite'
import { randomUUID, createHash } from 'node:crypto'
import { readFile, realpath, stat } from 'node:fs/promises'
import path from 'node:path'
import type { TrustedActor, ConversationResult } from '@telebot/core/contracts'
import { authorizeHistory, codingDigest, executeCoding, type CodingRequest } from '../domain/coding.js'
import { NativeCodex, type NativeResult } from './native.js'
import {RunSkills} from './skills.js'

export class CodingService {
  lastFailure: string | undefined
  private readonly db: DatabaseSync
  private readonly active = new Map<string, AbortController>()
  private readonly jobs = new Set<Promise<ConversationResult>>()
  private readonly writer = randomUUID()
  constructor(private readonly backend: NativeCodex, filename: string, private readonly admins: readonly string[], private readonly roots: readonly string[], private readonly historyOwner: string, private readonly executionTimeoutMs = 1200000, private readonly confirmationTtlMs = 900000,private readonly skills=new RunSkills()) {
    this.db = new DatabaseSync(filename)
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS coding(token TEXT PRIMARY KEY,binding TEXT NOT NULL,request TEXT NOT NULL,expires INTEGER NOT NULL,state TEXT NOT NULL,result TEXT);
      CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,owner TEXT NOT NULL,cwd TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS writer(id INTEGER PRIMARY KEY,pid INTEGER,owner TEXT);`)
    this.db.exec('BEGIN IMMEDIATE')
    const writer = this.db.prepare('SELECT * FROM writer WHERE id=1').get() as any
    let alive = false
    if (writer) { try { process.kill(writer.pid, 0); alive = true } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') alive = true } }
    if (alive) { this.db.exec('ROLLBACK'); this.db.close(); throw new Error('coding_database_already_active') }
    this.db.prepare('INSERT OR REPLACE INTO writer VALUES(1,?,?)').run(process.pid, this.writer)
    this.db.exec("UPDATE coding SET state='unknown' WHERE state='running'; COMMIT;")
  }
  private binding(actor: TrustedActor) { return JSON.stringify([actor.userId, actor.chatId, actor.sessionId, actor.cwd]) }
  busy() { return this.jobs.size > 0 || this.active.size > 0 }
  pending(actor: TrustedActor) {
    return (this.db.prepare("SELECT token,request FROM coding WHERE binding=? AND state='pending' AND expires>?").all(this.binding(actor), Date.now()) as {token:string;request:string}[]).filter(row => !row.token.startsWith('task:')).map(row => ({token:row.token,label:JSON.parse(row.request).prompt as string}))
  }
  confirm(actor: TrustedActor, token: string, signal: AbortSignal) {
    const row = this.db.prepare('SELECT request FROM coding WHERE token=? AND binding=?').get(token,this.binding(actor)) as any
    return this.command(actor,(row && JSON.parse(row.request).threadId ? 'resume-confirm ' : 'confirm ')+token,signal)
  }
  /** Called only by the trusted task dispatcher after its separate execution grant. */
  async scheduled(actor: TrustedActor, prompt: string, executionId: string, signal: AbortSignal) {
    if (!desktopOwner(actor) && !this.admins.includes(actor.userId)) throw new Error('coding_actor_not_authorized')
    await this.workspace(actor)
    if (!prompt.trim() || prompt.length > 120000 || !executionId || executionId.length > 300) throw new Error('invalid_task_execution')
    const payload = { userId:actor.userId,chatId:actor.chatId,sessionId:actor.sessionId,cwd:actor.cwd,mode:'apply' as const,prompt,...(desktopFull(actor)?{fullAccess:true}:{}),skills:await this.skills.snapshot() }, request = {...payload,digest:codingDigest(payload)}, token='task:'+executionId
    const existing = this.db.prepare('SELECT binding,request FROM coding WHERE token=?').get(token) as any
    if (existing && (existing.binding !== this.binding(actor) || JSON.parse(existing.request).digest !== request.digest)) throw new Error('task_execution_collision')
    if (!existing) this.db.prepare("INSERT INTO coding VALUES(?,?,?,?,?,NULL)").run(token,this.binding(actor),JSON.stringify(request),Date.now()+this.confirmationTtlMs,'pending')
    return this.command(actor,'confirm '+token,AbortSignal.any([signal,AbortSignal.timeout(this.executionTimeoutMs)]))
  }
  private async artifacts(actor: TrustedActor, result: NativeResult, startedAt: number) {
    const artifacts = []
    if (result.status === 'completed') for (const target of result.artifacts ?? []) {
      const file = await realpath(path.resolve(actor.cwd, target)), relative = path.relative(await realpath(actor.cwd), file)
      const info = await stat(file)
      if ((!desktopFull(actor)&&(relative.startsWith('..') || path.isAbsolute(relative))) || !info.isFile() || info.mtimeMs < startedAt - 1000 || info.size > 30 * 1024 * 1024) throw new Error('unverified_coding_artifact')
      artifacts.push({ path: file, size: info.size, sha256: createHash('sha256').update(await readFile(file)).digest('hex') })
    }
    return { ...result, artifacts }
  }
  private async workspace(actor: TrustedActor) {
    const actual = await realpath(actor.cwd)
    if (!(await stat(actual)).isDirectory()) throw new Error('workspace_not_directory')
    if(desktopFull(actor))return actual
    const roots = await Promise.all((desktopOwner(actor)?actor.allowedRoots??[]:this.roots).map(root => realpath(root)))
    if (!roots.some(root => { const r = path.relative(root, actual); return r === '' || (!r.startsWith('..') && !path.isAbsolute(r)) })) throw new Error('coding_workspace_not_allowed')
    return actual
  }
  command(actor: TrustedActor, text: string, signal: AbortSignal, operationId?:string): Promise<ConversationResult> {
    const job = this.commandImpl(actor, text, signal,operationId)
    this.jobs.add(job); job.finally(() => this.jobs.delete(job)).catch(() => {})
    return job
  }
  private async commandImpl(actor: TrustedActor, text: string, signal: AbortSignal,operationId?:string): Promise<ConversationResult> {
    if (!desktopOwner(actor) && !this.admins.includes(actor.userId)) throw new Error('coding_actor_not_authorized')
    await this.workspace(actor)
    const match = /^(\S+)(?:\s+([\s\S]*))?$/.exec(text.trim())
    const action = match?.[1] ?? '', value = match?.[2] ?? ''
    if (action === 'stop') { this.active.get(actor.sessionId)?.abort(); return { status: 'completed', text: '已请求停止 Codex。' } }
    if (action === 'apply' && /^[a-f0-9-]{36}$/.test(value) && this.db.prepare('SELECT 1 FROM coding WHERE token=?').get(value)) throw new Error('apply_token_requires_explicit_confirm')
    if (['history', 'read', 'resume', 'resume-confirm'].includes(action)) return this.history(actor, action, value, signal,operationId)
    if (action === 'cancel') { this.db.prepare("UPDATE coding SET state='cancelled' WHERE token=? AND binding=? AND state='pending'").run(value, this.binding(actor)); return { status: 'completed', text: '已取消待确认请求。' } }
    let request: CodingRequest, token: string | undefined
    if (action === 'confirm') {
      const row = this.db.prepare('SELECT * FROM coding WHERE token=?').get(value) as any
      if (!row || row.binding !== this.binding(actor) || row.expires <= Date.now()) throw new Error('coding_confirmation_mismatch')
      if (row.state !== 'pending') return { status: row.state === 'completed' ? 'completed' : 'unknown', text: row.result ?? '请求已消费，不会重复执行。' }
      request = JSON.parse(row.request); token = value
    } else {
      if (!['inspect', 'plan', 'apply'].includes(action) || !value.trim() || value.length > 120000) return { status: 'failed', text: '/codex inspect|plan|apply <要求>；/codex confirm|cancel <令牌>；history/read/resume/stop' }
      const payload = { userId: actor.userId, chatId: actor.chatId, sessionId: actor.sessionId, cwd: actor.cwd, mode: action as CodingRequest['mode'], prompt: value,...(desktopFull(actor)?{fullAccess:true}:{}),...(action==='apply'?{skills:await this.skills.snapshot()}:{}) }
      request = { ...payload, digest: codingDigest(payload) }
      if (action === 'apply') {
        if(desktopFull(actor)&&!operationId)throw new Error('desktop_operation_id_required')
        token = desktopFull(actor)?'desktop:'+createHash('sha256').update(this.binding(actor)+operationId).digest('hex'):randomUUID()
        const existing=this.db.prepare('SELECT request FROM coding WHERE token=?').get(token) as any
        if(existing){if(JSON.parse(existing.request).digest!==request.digest)throw new Error('desktop_operation_collision');return this.commandImpl(actor,'confirm '+token,signal)}
        this.db.prepare('INSERT INTO coding VALUES(?,?,?,?,?,NULL)').run(token, this.binding(actor), JSON.stringify(request), Date.now() + this.confirmationTtlMs, 'pending')
        if(desktopFull(actor))return this.commandImpl(actor,'confirm '+token,signal)
        return { status: 'completed', text: `写入计划：${request.prompt}\n目录：${actor.cwd}\n摘要：${request.digest}\n技能运行副本：${request.skills?.names.join('、')||'未选择'}\n在确认有效期内使用 /codex confirm ${token} 确认；只授权此用户/会话/目录/内容。` }
      }
    }
    if (this.active.has(actor.sessionId)) throw new Error('coding_session_busy')
    signal.throwIfAborted()
    if (token && this.db.prepare("UPDATE coding SET state='running' WHERE token=? AND state='pending'").run(token).changes !== 1) throw new Error('coding_already_claimed')
    const controller = new AbortController(); this.active.set(actor.sessionId, controller)
    const startedAt = Date.now()
    try {
      if(request.mode==='apply')await this.skills.prepare(actor.cwd,request.skills)
      const grant = request.mode === 'apply' ? { ...actor, digest: request.digest, expiresAt: Date.now() + this.confirmationTtlMs, consumed: false } : undefined
      const result = await executeCoding(this.backend, request, {...actor,fullAccess:desktopFull(actor)}, desktopOwner(actor)?[actor.userId]:this.admins, grant, AbortSignal.any([signal, controller.signal, AbortSignal.timeout(this.executionTimeoutMs)])) as NativeResult
      if (result.threadId) this.db.prepare('INSERT OR REPLACE INTO threads VALUES(?,?,?)').run(result.threadId, actor.userId, actor.cwd)
      const output = await this.artifacts(actor, result, startedAt)
      if (token) this.db.prepare('UPDATE coding SET state=?,result=? WHERE token=?').run(result.status, JSON.stringify(output), token)
      return { status: result.status, text: JSON.stringify(output), artifacts: output.artifacts }
    } catch (error) {
      this.lastFailure = error instanceof Error && /^[a-z_]+$/.test(error.message) ? error.message : (error as NodeJS.ErrnoException)?.code ?? 'coding_verification_failed'
      if (token) this.db.prepare("UPDATE coding SET state='unknown' WHERE token=?").run(token)
      return { status: 'unknown', text: 'Codex 执行或产物回执未确认，没有自动重试。' }
    } finally { this.active.delete(actor.sessionId) }
  }
  private async history(actor: TrustedActor, action: string, value: string, signal: AbortSignal,operationId?:string): Promise<ConversationResult> {
    if (!actor.privateChat || !desktopOwner(actor) && actor.userId !== this.historyOwner) throw new Error('private_owned_history_required')
    const client = await this.backend.connect(actor.cwd)
    try {
      if (action === 'resume-confirm') {
        const row = this.db.prepare('SELECT * FROM coding WHERE token=?').get(value) as any
        if (!row || row.binding !== this.binding(actor) || row.expires <= Date.now() || row.state !== 'pending') throw new Error('resume_confirmation_mismatch')
        const saved = JSON.parse(row.request)
        if(Boolean(saved.fullAccess)!==desktopFull(actor))throw new Error('resume_permission_changed')
        if (!saved.threadId || saved.cwd !== actor.cwd || saved.mode !== 'apply' || saved.policyVersion !== 1) throw new Error('resume_scope_mismatch_requires_new_confirmation')
        const details = await client.request('thread/read', { threadId: saved.threadId, includeTurns: false }, signal)
        if (details.thread.status?.type === 'active') throw new Error('native_thread_busy')
        if (details.thread.cwd !== actor.cwd || await this.archived(client, saved.threadId, signal)) throw new Error('history_resume_requires_fresh_workspace_authorization')
        if (this.active.has(actor.sessionId)) throw new Error('coding_session_busy')
        signal.throwIfAborted()
        if (this.db.prepare("UPDATE coding SET state='running' WHERE token=? AND state='pending'").run(value).changes !== 1) throw new Error('resume_already_claimed')
        const controller = new AbortController(); this.active.set(actor.sessionId, controller)
        const payload = { userId: actor.userId, chatId: actor.chatId, sessionId: actor.sessionId, cwd: actor.cwd, mode: 'apply' as const, prompt: saved.prompt,...(desktopFull(actor)?{fullAccess:true}:{}),skills:saved.skills }
        try {
          const startedAt = Date.now()
          await this.skills.prepare(actor.cwd,saved.skills)
          const result = await this.backend.execute({ ...payload, digest: codingDigest(payload) }, { sandbox: desktopFull(actor)?'danger-full-access':'workspace-write', signal: AbortSignal.any([signal, controller.signal, AbortSignal.timeout(this.executionTimeoutMs)]) }, saved.threadId)
          const output = await this.artifacts(actor, result, startedAt)
          this.db.prepare('UPDATE coding SET state=?,result=? WHERE token=?').run(result.status, JSON.stringify(output), value)
          return { status: result.status, text: JSON.stringify(output), artifacts: output.artifacts }
        } catch { this.db.prepare("UPDATE coding SET state='unknown' WHERE token=?").run(value); return { status: 'unknown', text: '续聊结果未确认，不会自动重复提交。' } }
        finally { this.active.delete(actor.sessionId) }
      }
      if (action === 'history') {
        const [cursor, archived] = value.split(/\s+/)
        const result = await client.request('thread/list', { limit: 20, ...(cursor ? { cursor } : {}), archived: archived === 'archived', modelProviders: [] }, signal)
        result.data=result.data.filter((thread:any)=>{const owner=this.db.prepare('SELECT owner FROM threads WHERE id=?').get(thread.id) as any;return !owner||owner.owner===actor.userId});return { status: 'completed', text: JSON.stringify(result) }
      }
      const [threadId, ...prompt] = value.split(/\s+/)
      const details = await client.request('thread/read', { threadId, includeTurns: true }, signal)
      if (action !== 'read' && details.thread.status?.type === 'active') throw new Error('native_thread_busy')
      const row = this.db.prepare('SELECT * FROM threads WHERE id=?').get(threadId) as any
      const archived = await this.archived(client, threadId, signal)
      const thread = { owner: row?.owner ?? (desktopOwner(actor)?actor.userId:this.historyOwner), cwd: details.thread.cwd, archived }
      authorizeHistory(actor, desktopOwner(actor)?[actor.userId]:this.admins, thread, action === 'read' ? 'read' : 'resume')
      if (action === 'read') return { status: 'completed', text: JSON.stringify(details) }
      if (!prompt.length) throw new Error('resume_prompt_required')
      // One-use resume grants use the same durable confirmation ledger.
      if (action === 'resume') {
        if(desktopFull(actor)&&!operationId)throw new Error('desktop_operation_id_required')
        const token = desktopFull(actor)?'desktop-resume:'+createHash('sha256').update(this.binding(actor)+operationId).digest('hex'):randomUUID()
        const prior=this.db.prepare('SELECT request,state,result FROM coding WHERE token=?').get(token) as any
        if(prior){const previous=JSON.parse(prior.request);if(previous.threadId!==threadId||previous.prompt!==prompt.join(' '))throw new Error('desktop_operation_collision');if(prior.state!=='pending')return{status:prior.state==='completed'?'completed':'unknown',text:prior.result??'结果未确认，不重复续聊。'};return this.history(actor,'resume-confirm',token,signal)}
        const request = { threadId, prompt: prompt.join(' '), cwd: actor.cwd, mode: 'apply', policyVersion: 1,...(desktopFull(actor)?{fullAccess:true}:{}),skills:await this.skills.snapshot() }
        this.db.prepare('INSERT INTO coding VALUES(?,?,?,?,?,NULL)').run(token, this.binding(actor), JSON.stringify(request), Date.now() + this.confirmationTtlMs, 'pending')
        if(desktopFull(actor))return this.history(actor,'resume-confirm',token,signal)
        return { status: 'completed', text: `续聊一轮：${threadId}\n目录：${actor.cwd}\n要求：${request.prompt}\n确认后仅允许写当前工作区；关闭 Shell 网络和临时目录默认写权限，拒绝沙箱外审批。\n确认：/codex resume-confirm ${token}` }
      }
      return { status: 'failed', text: '请使用专用续聊确认令牌。' }
    } finally { await this.backend.release(client) }
  }
  private async archived(client: import('./native.js').NativeClient, id: string, signal: AbortSignal) {
    let cursor: string | undefined
    const seen = new Set<string>()
    for (let page = 0; page < 100; page++) {
      const result = await client.request('thread/list', { archived: true, limit: 100, modelProviders: [], ...(cursor ? { cursor } : {}) }, signal)
      if (result.data.some((entry: any) => entry.id === id)) return true
      if (!result.nextCursor) return false
      if (seen.has(result.nextCursor)) throw new Error('native_history_cursor_cycle')
      cursor = result.nextCursor; seen.add(cursor!)
    }
    throw new Error('native_history_page_budget')
  }
  async dispose() { for (const controller of this.active.values()) controller.abort(); await this.backend.dispose(); await Promise.allSettled([...this.jobs]); this.db.prepare('DELETE FROM writer WHERE owner=?').run(this.writer); this.db.close() }
}
