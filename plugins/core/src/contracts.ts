/** Host capability names used without leaking Cordis types into business code. */
export const hostCapabilities = ['agents', 'commands', 'tools', 'shell', 'schedule'] as const

export type HostCapability = typeof hostCapabilities[number]
export type { ChannelQuestion } from './interactions.js'
export type CapabilitySnapshot = Readonly<Record<HostCapability, boolean>>

/** Local session identity and immutable execution location owned by the channel mapping. */
export interface ConversationSpec {
  id: string
  cwd: string
  allowCreate: boolean
  provider?: string
  model?: string
  preset?: string
  /** Only trusted dedicated tasks set this restriction, never model parameters. */
  allowedTools?: readonly string[]
  /** Trusted channel continuation through an unanswered tool, using the official fork seed. */
  parentSessionId?: string
}
export interface TrustedActor { userId: string; chatId: string; sessionId: string; cwd: string; privateChat: boolean; origin: 'feishu' | 'web'; permissions?: 'full' | 'restricted'; nativeRequestId?: string; allowedRoots?: readonly string[]; startedAt?: number; sourceId?: string; threadId?: string; relatedSessionIds?: readonly string[] }
export function desktopOwner(actor: TrustedActor): boolean { return actor.origin === 'web' && (actor.permissions === 'full' || actor.permissions === 'restricted') }
/** Only trusted entry adapters supply this DTO; tool schemas never accept it. */
export function desktopFull(actor: TrustedActor): boolean { return actor.origin === 'web' && actor.permissions === 'full' }
/** Scope model call IDs by the authenticated native user request, since providers may reuse call IDs across turns. */
export function operationIdentity(actor: TrustedActor, callId: string): string { return actor.origin === 'web' ? JSON.stringify([actor.nativeRequestId,callId]) : callId }
export interface ActorProvider { resolve(sessionId: string): TrustedActor | undefined; validate(actor: TrustedActor): boolean }
export interface TaskExecution { id: string; taskId: string; version: number; scheduledAt: string; actor: TrustedActor; prompt: string; signal: AbortSignal }
export type TaskExecutor = (execution: TaskExecution) => Promise<ConversationResult>
export interface DeliveryRequest { id: string; actor: TrustedActor; result: ConversationResult }
export interface PendingConfirmation { token: string; label: string }
export interface ConfirmationProvider { pending(actor: TrustedActor): PendingConfirmation[]; confirm(actor: TrustedActor, token: string, signal: AbortSignal): Promise<ConversationResult>; cancel(actor: TrustedActor, token: string): Promise<ConversationResult> }
export interface FeatureRequest { id?: string; actor: TrustedActor; text: string; signal: AbortSignal }
export type FeatureHandler = (request: FeatureRequest) => Promise<ConversationResult>
export interface HumanInputRoute { kind: 'text'|'answer'|'notice'|'confirmation'|'interpretation'; text:string; sessionId?:string }
export interface FeaturePort { invoke(name: string, request: FeatureRequest): Promise<ConversationResult>; has?(name: string): boolean; routeInput?(actor:TrustedActor,text:string,semantic?:boolean):HumanInputRoute|undefined; releaseInput?(ticket:string):void; resolveInput?(actor:TrustedActor,ticket:string,spec:ConversationSpec,signal:AbortSignal):Promise<HumanInputRoute>; waitingSessions?():readonly string[] }
export interface IncomingFile { name: string; kind: 'file' | 'image'; data: Uint8Array; mediaType?: string }
export interface ConversationInput { id: string; text: string; actor?: TrustedActor; question?: import('./interactions.js').QuestionSink; attachments?: IncomingFile[]; progress?: (text: string) => void }
export interface ConversationResult {
  status: 'completed' | 'failed' | 'cancelled' | 'unknown'
  text: string
  artifacts?: readonly { path: string; size: number; sha256: string }[]
}
export interface ConversationStatus { active: boolean; cwd?: string; model?: string; provider?: string }
export interface ModelRoute { provider: string; model: string }
/** Trusted channels own authorization; this port never interprets model text as commands. */
export interface CommandPort {
  list(spec: ConversationSpec): Promise<readonly { name: string; description: string }[]>
  execute(spec: ConversationSpec, line: string, signal: AbortSignal): Promise<ConversationResult>
  models(): Promise<readonly ModelRoute[]>
  selectModel(spec: ConversationSpec, route: ModelRoute): Promise<void>
  prepare(spec: ConversationSpec): Promise<void>
  permission?(spec: ConversationSpec, preset?: string): Promise<string>
}
export interface ConversationPort {
  prepare?(spec:ConversationSpec,actor:TrustedActor):Promise<void>
  context?(spec:ConversationSpec,id:string,text:string):Promise<void>
  run(spec: ConversationSpec, input: ConversationInput): Promise<ConversationResult>
  recover(spec: ConversationSpec, input: ConversationInput): Promise<ConversationResult>
  cancel(id: string): void
  status(id: string): ConversationStatus
  dispose(): Promise<void>
}
