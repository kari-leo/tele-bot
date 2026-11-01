/** Logical capabilities; platform API scopes are configured on the application. */
export const permissionMatrix = Object.freeze({
  documents: ['read', 'create', 'update', 'delete'],
  bitable: ['read', 'create', 'update', 'delete'],
  calendar: ['read', 'create', 'update', 'delete'],
  tasks: ['read', 'create', 'update', 'delete'],
} as const)
export type ResourceKind = keyof typeof permissionMatrix
export interface TrustedActor { origin: 'feishu' | 'web'; userId?: string; chatId?: string }
export interface ApiGrant {
  kind: 'tenant'
  expiresAt: number
  capabilities: readonly string[]
  resources: readonly string[]
}
export interface ApiOperation { kind: ResourceKind; action: string; resource: string }

/** Trusted configuration and credential owner must be supplied separately from model arguments. */
export function authorizeApi(actor: TrustedActor, admins: readonly string[], operation: ApiOperation,
  grant: ApiGrant | undefined, now: number): void {
  if (!actor.userId || !admins.includes(actor.userId)) throw new Error('actor_not_authorized')
  if (actor.origin === 'feishu' && !actor.chatId) throw new Error('feishu_chat_required')
  const actions: readonly string[] | undefined = Object.hasOwn(permissionMatrix, operation.kind) ? permissionMatrix[operation.kind] : undefined
  if (!actions?.includes(operation.action)) throw new Error('unsupported_operation')
  if (!grant || !Number.isFinite(grant.expiresAt) || !Number.isFinite(now) || grant.expiresAt <= now) throw new Error('credential_missing_or_expired')
  if (grant.kind !== 'tenant') throw new Error('application_identity_required')
  if (!grant.capabilities.includes(`${operation.kind}.${operation.action}`)) throw new Error('capability_missing')
  if (!operation.resource || !grant.resources.includes(operation.resource)) throw new Error('resource_not_authorized')
}

export interface Page<T> { items: readonly T[]; hasMore: boolean; next?: string }
/** Never report a truncated or cycling enumeration as a complete resource inventory. */
export async function collectPages<T>(fetchPage: (token?: string) => Promise<Page<T>>, maxPages: number = 100) {
  if (!Number.isSafeInteger(maxPages) || maxPages < 1) throw new Error('invalid_page_budget')
  const items: T[] = [], seen = new Set<string>()
  let token: string | undefined
  for (let page = 0; page < maxPages; page++) {
    const result = await fetchPage(token)
    items.push(...result.items)
    if (!result.hasMore) return { items, complete: true as const }
    if (!result.next || seen.has(result.next)) throw new Error('invalid_pagination_cursor')
    seen.add(result.next); token = result.next
  }
  throw new Error('pagination_budget_exceeded')
}
