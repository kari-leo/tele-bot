import type { Resource } from './resources.js'

export interface BaseLink { baseId: string; url: string }
export function baseLink(value: string): BaseLink {
  const url = new URL(value)
  if (url.protocol !== 'https:' || url.username || url.password || url.port || !['feishu.cn', 'larksuite.com'].some(domain => url.hostname === domain || url.hostname.endsWith('.' + domain))) throw new Error('trusted_base_link_required')
  const baseId = url.pathname.match(/^\/base\/([\w-]{1,128})\/?$/)?.[1]
  if (!baseId) throw new Error('specific_base_link_required')
  url.search = ''; url.hash = ''; url.pathname = '/base/' + baseId
  return { baseId, url: url.href }
}
export function validateBaseLinks(value: unknown): asserts value is BaseLink[] {
  if (!Array.isArray(value) || value.length > 500) throw new Error('base_link_array_required')
  const seen = new Set<string>()
  for (const row of value) {
    if (!row || Object.keys(row).some(key => !['baseId', 'url'].includes(key)) || typeof row.url !== 'string' || row.baseId !== baseLink(row.url).baseId || seen.has(row.baseId)) throw new Error('invalid_or_duplicate_base_link')
    seen.add(row.baseId)
  }
}
export function linkedBases(resources: readonly Resource[], links: readonly BaseLink[]) {
  const ids = [...new Set([...links.map(row => row.baseId), ...resources.filter(row => row.kind === 'bitable').map(row => row.id)])]
  return ids.map(baseId => ({ baseId, url: links.find(row => row.baseId === baseId)?.url ?? 'https://feishu.cn/base/' + baseId,
    tables: resources.filter(row => row.kind === 'bitable' && row.id === baseId).map(row => ({ id: row.tableId, alias: row.alias, ...(row.name?{name:row.name}:{}) })) }))
}
/** Only an explicit human management request is admitted as a direct command. */
export function baseManagementCommand(text: string): string | undefined {
  const value = text.trim()
  if (/^(?:查看|列出|显示)(?:已接入的?|已添加的?|当前的?)?多维表格(?:链接|列表)?[。！!\s]*$/.test(value)) return '/bases list'
  const match = /^(?:请)?(添加|增加|接入|绑定|移除|删除|解绑)(?:这个|一个|该)?(?:飞书)?多维表格(?:链接)?\s*[:：]?\s*(https:\/\/\S+?)[。！!\s]*$/.exec(value)
  if (!match) return undefined
  try { baseLink(match[2]) } catch { return undefined }
  return '/bases ' + (['移除','删除','解绑'].includes(match[1]) ? 'remove' : 'add') + ' ' + match[2]
}
