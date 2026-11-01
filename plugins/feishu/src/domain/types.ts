import type { ChannelQuestion, CommandPort, ConversationPort, ConversationSpec, FeaturePort } from '@telebot/core/contracts'

export interface Actor { ids: string[]; privateChat?: boolean; authorizedUserId?: string; referenceMessageId?: string }
export interface InputEvent {
  eventId: string
  messageId: string
  chatId: string
  threadId: string
  actor: Actor
  kind: 'text' | 'status' | 'notice' | 'answer' | 'attachment' | 'confirmation'
  text: string
  actionToken?: string
  /** Set only by the trusted interaction router, never decoded from webhook text. */
  targetSessionId?: string
}
export interface InboxEntry extends InputEvent {
  id: string
  inputId: string
  sessionKey: string
  state: string
  spec: ConversationSpec
}
export interface Outbound {
  id: string
  chatId: string
  replyTo: string
  threadId: string
  msgType: 'post' | 'interactive' | 'file'
  content: string
  attempts: number
  uploadedFileKey?: string
  editMessageId?: string
}
export interface ChannelSettings {
  appId: string
  adminUserIds: string[]
  allowedChatIds?: string[]
  workspace: string
  provider?: string
  model?: string
  preset?: string
  queueLimit: number
  maxConcurrentSessions: number
  pollMs: number
  leaseMs: number
  retryMs: number
  maxDeliveryAttempts: number
  actionTtlMs: number
  mergeMs?: number
  maxMergeWaitMs?: number
  streamProgress?: boolean
}
export type DeliveryResult =
  | { status: 'sent'; messageId: string }
  | { status: 'retry'; code: string }
  | { status: 'failed' | 'unknown'; code: string }
export interface DeliveryPort { send(message: Outbound,onUploaded?:(key:string)=>void): Promise<DeliveryResult> }
export interface AcceptResult { accepted: boolean; duplicate?: boolean; rejected?: boolean; full?: boolean }
export interface ChannelRepository {
  actorFor?(event:InputEvent,settings:ChannelSettings):import('@telebot/core/contracts').TrustedActor|undefined
  accept(event: InputEvent, settings: ChannelSettings): AcceptResult
  heartbeat(leaseMs: number): boolean
  running(): InboxEntry[]
  claim(excludedSessions: readonly string[], controlsOnly?: boolean, waitingSessions?:readonly string[]): InboxEntry | undefined
  waiting?(entry:InboxEntry):void
  resumeQuestion?(entry:InboxEntry):void
  continuation?(entry:InboxEntry):ConversationSpec
  currentSpec?(sessionKey:string):ConversationSpec
  commitContinuation?(entry:InboxEntry,spec:ConversationSpec):void
  finish(entry: InboxEntry, state: string, outputs: readonly Pick<Outbound, 'msgType' | 'content'>[]): void
  markMapped(id: string): void
  updateMapping(entry: InboxEntry, spec: ConversationSpec): void
  claimDelivery(now: number): Outbound | undefined
  settleDelivery(message: Outbound, result: DeliveryResult, retryAt: number, maxAttempts: number): void
  recoverDeliveries(): void
  uploaded?(message:Outbound,key:string):void
  status(sessionKey: string): { pending: number; unknown: number }
  statusCard(entry: InboxEntry, text: string, ttlMs: number): string
  question?(entry: InboxEntry, question: ChannelQuestion): string
  enqueue?(entry: InboxEntry, outputs: readonly Pick<Outbound, 'msgType' | 'content'>[]): string[] | void
  progress?(entry: InboxEntry, content: string): void
  deliveries?(ids: string[]): { id: string; state: string }[]
  release(): void
}
export interface WorkspacePort { resolve(target: string): Promise<string>; sessionId(): string }
export interface ChannelPorts {
  agent: ConversationPort; repository: ChannelRepository; delivery: DeliveryPort
  commands?: CommandPort; workspace?: WorkspacePort
  features?: FeaturePort
  files?: { receive(entry: InboxEntry): Promise<void>; forMessage(entry: InboxEntry): Promise<import('@telebot/core/contracts').IncomingFile[]>; outbound(entry: InboxEntry, filename: string): Promise<string> }
  reminders?: { command(entry: InboxEntry): Promise<string> }
}
