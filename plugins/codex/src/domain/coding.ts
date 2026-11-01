/** Local coding policy. An executor port is not another Agent or process loop. */
import { createHash } from 'node:crypto'
export interface CodingBinding { userId: string; chatId: string; sessionId: string; cwd: string }
export interface SkillSnapshot { source:string;names:string[];digest:string }
export interface CodingRequest extends CodingBinding { mode: 'inspect' | 'plan' | 'apply'; prompt: string; digest: string; skills?:SkillSnapshot; fullAccess?:boolean }
export interface CodingExecutorPort {
  readonly enforcesReadOnly: boolean
  execute(request: CodingRequest, options: { sandbox: 'read-only' | 'workspace-write' | 'danger-full-access'; signal: AbortSignal }): Promise<{ status: 'completed' | 'failed' | 'unknown'; text: string }>
}
export interface ApplyGrant extends CodingBinding { digest: string; expiresAt: number; consumed: boolean }
export function codingDigest(request: Omit<CodingRequest, 'digest'>): string {
  const payload:unknown[]=[request.userId,request.chatId,request.sessionId,request.cwd,request.mode,request.prompt]
  if(request.fullAccess)payload.push({fullAccess:true})
  if(request.skills)payload.push(request.skills)
  return createHash('sha256').update(JSON.stringify(payload)).digest('hex')
}
function matches(left: CodingBinding, right: CodingBinding) {
  return left.userId === right.userId && left.chatId === right.chatId && left.sessionId === right.sessionId && left.cwd === right.cwd
}
/** Grants are issued by trusted user confirmation, never by an executor/model response. */
export async function executeCoding(executor: CodingExecutorPort, request: CodingRequest,
  trusted: CodingBinding & {fullAccess?:boolean}, admins: readonly string[], grant?: ApplyGrant, signal?: AbortSignal) {
  if (!admins.includes(trusted.userId) || !matches(request, trusted)) throw new Error('coding_actor_or_scope_mismatch')
  if (!['inspect', 'plan', 'apply'].includes(request.mode) || !request.prompt.trim() || request.digest !== codingDigest(request)) throw new Error('invalid_coding_request')
  if (request.fullAccess && !trusted.fullAccess) throw new Error('coding_full_access_not_authorized')
  signal?.throwIfAborted()
  if (request.mode !== 'apply' && !executor.enforcesReadOnly) throw new Error('backend_cannot_enforce_read_only')
  if (request.mode === 'apply') {
    if (!grant || grant.consumed || !Number.isFinite(grant.expiresAt) || grant.expiresAt <= Date.now() || !matches(grant, trusted) || grant.digest !== request.digest) throw new Error('apply_confirmation_required')
    // Consume before dispatch. Failed/unknown execution needs a new explicit confirmation.
    grant.consumed = true
  }
  const deadline = AbortSignal.timeout(20 * 60 * 1000)
  return executor.execute(request, { sandbox: request.mode === 'apply' ? request.fullAccess ? 'danger-full-access' : 'workspace-write' : 'read-only',
    signal: signal ? AbortSignal.any([signal, deadline]) : deadline })
}
export function authorizeHistory(trusted: CodingBinding & { privateChat: boolean }, admins: readonly string[],
  thread: { owner: string; cwd: string; archived: boolean }, action: 'read' | 'resume') {
  if (!trusted.privateChat || !admins.includes(trusted.userId) || trusted.userId !== thread.owner) throw new Error('private_owned_history_required')
  if (action === 'resume' && (thread.archived || thread.cwd !== trusted.cwd)) throw new Error('history_resume_requires_fresh_workspace_authorization')
}
