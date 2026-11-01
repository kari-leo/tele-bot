import { collectPages } from '../domain/authorization.js'
import type { ApiPort } from '../domain/resources.js'
/** Explicit human-supplied Base discovery, not tenant-wide resource enumeration. */
export async function discoverBase(value: string, api: ApiPort) {
  const url = new URL(value)
  if (url.protocol !== 'https:' || url.username || url.password || url.port || !['feishu.cn','larksuite.com'].some(domain => url.hostname === domain || url.hostname.endsWith('.' + domain))) throw new Error('trusted_base_link_required')
  const baseId = url.pathname.match(/^\/base\/([\w-]{1,128})\/?$/)?.[1]
  if (!baseId) throw new Error('specific_base_link_required')
  const tables = await collectPages(async token => {
    const result = await api.request('GET', `/open-apis/bitable/v1/apps/${baseId}/tables?page_size=100` + (token ? '&page_token=' + encodeURIComponent(token) : ''))
    if (!Array.isArray(result.items)) throw new Error('invalid_base_table_inventory')
    return { items: result.items.map((row: any) => ({ table_id: String(row.table_id), name: String(row.name ?? row.table_id) })), hasMore: result.has_more ?? false, next: result.page_token }
  })
  return { baseId, tables: tables.items, complete: tables.complete, preferredTableId: url.searchParams.get('table') ?? '' }
}
