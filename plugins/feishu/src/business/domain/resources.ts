import type { ApiGrant, ResourceKind, TrustedActor } from './authorization.js'
import { authorizeApi, collectPages } from './authorization.js'

export interface Resource { alias: string; kind: ResourceKind; id: string; tableId?: string; format?: 'docx' | 'wiki' | 'sheets' | 'drive'; ranges?: string[]; identity: 'tenant'; tokenRef: 'FEISHU_APP_TENANT'; expiresAt: number; capabilities: string[] }
export interface Operation { resource: string; action: 'inspect' | 'query' | 'create' | 'update' | 'delete'; params: Record<string, any>; frozenResource?: string }
export interface ApiPort { request(method: string, route: string, body?: unknown, signal?: AbortSignal, write?: boolean): Promise<Record<string, any>> }
export function validateResources(resources: unknown): asserts resources is Resource[] {
  if (!Array.isArray(resources) || resources.length > 500) throw new Error('resource_array_required')
  const keys = new Set(['alias','kind','id','tableId','format','ranges','identity','tokenRef','expiresAt','capabilities'])
  for (const row of resources) {
    if (!row || typeof row !== 'object' || Object.keys(row).some(key => !keys.has(key))) throw new Error('resource_metadata_only_no_literal_credentials')
    if (typeof row.alias !== 'string' || !row.alias.trim() || row.alias.length > 128 || !['documents','bitable','calendar','tasks'].includes(row.kind)) throw new Error('invalid_resource_configuration')
    if (row.identity !== 'tenant' || row.tokenRef !== 'FEISHU_APP_TENANT') throw new Error('application_identity_required')
    const resourceId = row.kind === 'calendar' ? /^[\w.@:-]{1,256}$/ : /^[\w.:-]{1,256}$/
    if (typeof row.id !== 'string' || !resourceId.test(row.id) || (row.tableId !== undefined && (typeof row.tableId !== 'string' || !/^[\w.:-]{1,256}$/.test(row.tableId)))) throw new Error('invalid_resource_identifier')
    if (!Number.isFinite(row.expiresAt) || !Array.isArray(row.capabilities) || row.capabilities.some((value: unknown) => typeof value !== 'string')) throw new Error('invalid_resource_grant')
    if (row.format !== undefined && !['docx','wiki','sheets','drive'].includes(row.format)) throw new Error('invalid_document_format')
    if (row.ranges !== undefined && (!Array.isArray(row.ranges) || row.ranges.some((value: unknown) => typeof value !== 'string'))) throw new Error('invalid_sheet_ranges')
  }
}
const id = (value: unknown) => { if (typeof value !== 'string' || !/^[\w.@:-]{1,256}$/.test(value)) throw new Error('invalid_resource_identifier'); return encodeURIComponent(value) }
export class Resources {
  constructor(readonly resources: readonly Resource[], private readonly admins: readonly string[], private readonly api: (resource: Resource) => ApiPort) {
    validateResources(resources)
    if (new Set(resources.map(row => row.alias)).size !== resources.length) throw new Error('ambiguous_resource_alias')
  }
  resource(actor: TrustedActor, operation: Operation): Resource {
    const resource = this.configured(operation.resource)
    const grant: ApiGrant = { kind: resource.identity, expiresAt: resource.expiresAt,
      capabilities: resource.capabilities, resources: [operation.resource] }
    authorizeApi(actor, this.admins, { kind: resource.kind, action: ['inspect', 'query'].includes(operation.action) ? 'read' : operation.action, resource: operation.resource }, grant, Date.now())
    return resource
  }
  configured(value: string): Resource {
    let reference = value, table: string | undefined
    if (/^https:\/\//.test(reference)) {
      const url = new URL(reference)
      if (!['feishu.cn', 'larksuite.com'].some(host => url.hostname === host || url.hostname.endsWith('.' + host))) throw new Error('untrusted_resource_link')
      reference = url.pathname.match(/\/(?:base|docx|wiki|sheets)\/([\w-]+)/)?.[1] ?? ''
      table = url.searchParams.get('table') ?? undefined
    }
    const matches = this.resources.filter(row => row.alias === reference || (row.id === reference && (!table || row.tableId === table)))
    if (matches.length !== 1) throw new Error(matches.length ? 'ambiguous_resource' : 'unknown_resource')
    return matches[0]
  }
  fingerprint(actor: TrustedActor, operation: Operation) {
    const row = this.resource(actor, operation)
    return JSON.stringify([row.alias, row.kind, row.id, row.tableId ?? null, row.identity, row.format ?? null, row.ranges ?? [], row.tokenRef])
  }
  async execute(actor: TrustedActor, operation: Operation, signal?: AbortSignal): Promise<Record<string, any>> {
    const resource = this.resource(actor, operation), client = this.api(resource), p = operation.params
    const query = operation.action === 'query', inspect = operation.action === 'inspect'
    const encode = encodeURIComponent
    const pages = async (base: string, method = 'GET', body?: unknown, itemKey = 'items') => collectPages(async cursor => {
      const value = await client.request(method, base + (base.includes('?') ? '&' : '?') + 'page_size=100' + (cursor ? '&page_token=' + encode(cursor) : ''), body, signal, false)
      const items = value[itemKey] ?? []
      if (!Array.isArray(items) || typeof (value.has_more ?? false) !== 'boolean') throw new Error('invalid_page_response')
      return { items, hasMore: value.has_more ?? false, next: value.page_token }
    })
    if (resource.kind === 'bitable') {
      if (!resource.tableId && inspect) return { tables: await pages(`/open-apis/bitable/v1/apps/${id(resource.id)}/tables`), known_resources_only: true }
      const base = `/open-apis/bitable/v1/apps/${id(resource.id)}/tables/${id(resource.tableId)}`
      if (inspect) return { fields: await pages(base + '/fields'), known_resources_only: true, resource: resource.alias }
      if (query) return pages(base + '/records/search', 'POST', { ...(p.filter ? { filter: p.filter } : {}), ...(p.field_names ? { field_names: p.field_names } : {}) })
      if (!Array.isArray(p.records) || !p.records.length || p.records.length > 500) throw new Error('invalid_record_batch')
      if (operation.action !== 'create') for (const record of p.records) id(record.record_id)
      const action = { create: 'batch_create', update: 'batch_update', delete: 'batch_delete' }[operation.action as 'create' | 'update' | 'delete']
      const body = operation.action === 'delete' ? { records: p.records.map((row: any) => row.record_id) } : { records: p.records }
      return client.request('POST', base + '/records/' + action, body, signal)
    }
    if (resource.kind === 'documents') {
      let documentId = resource.id, format = resource.format ?? 'docx'
      if (format === 'wiki') {
        const value = await client.request('GET', `/open-apis/wiki/v2/spaces/get_node?token=${id(resource.id)}`, undefined, signal)
        if (inspect) return { node: value.node, source: resource.alias, known_resources_only: true }
        documentId = value.node?.obj_token; format = value.node?.obj_type
        if (!['docx', 'sheets'].includes(format)) throw new Error('unsupported_wiki_object_type')
      }
      if (format === 'sheets') {
        if (inspect) return client.request('GET', `/open-apis/sheets/v3/spreadsheets/${id(documentId)}/sheets/query`, undefined, signal)
        if (!query) throw new Error('sheet_writes_not_in_document_append_scope')
        if (typeof p.range !== 'string' || !resource.ranges?.includes(p.range)) throw new Error('explicit_authorized_sheet_range_required')
        const value = await client.request('GET', `/open-apis/sheets/v2/spreadsheets/${id(documentId)}/values/${encode(p.range)}`, undefined, signal)
        if (!value.valueRange || !Array.isArray(value.valueRange.values)) throw new Error('sheet_range_receipt_missing')
        return { ...value, requestedRange: p.range, complete: true, coverage: 'configured_range_only' }
      }
      if (format === 'drive' && (query || inspect)) return { files: await pages(`/open-apis/drive/v1/files?folder_token=${id(resource.id)}`, 'GET', undefined, 'files'), known_resources_only: true }
      const base = `/open-apis/docx/v1/documents/${id(documentId)}`
      if (inspect) return client.request('GET', base, undefined, signal)
      if (query) return pages(base + '/blocks')
      if (operation.action === 'create') return client.request('POST', '/open-apis/docx/v1/documents', { title: String(p.title ?? ''), folder_token: resource.id }, signal)
      if (operation.action === 'update') {
        if (typeof p.text !== 'string' || !p.text.trim() || p.text.length > 120000) throw new Error('invalid_document_text')
        const children = p.text.split('\n').map((text: string) => ({ block_type: 2, text: { elements: [{ text_run: { content: text } }] } }))
        if (children.length > 50) throw new Error('document_batch_exceeds_50_blocks')
        return client.request('POST', base + `/blocks/${id(documentId)}/children`, { children, index: -1 }, signal)
      }
      return client.request('DELETE', `/open-apis/drive/v1/files/${id(resource.id)}?type=docx`, undefined, signal)
    }
    if (resource.kind === 'calendar') {
      const base = `/open-apis/calendar/v4/calendars/${id(resource.id)}/events`
      if (query) {
        if (!/^\d+$/.test(String(p.start_time)) || !/^\d+$/.test(String(p.end_time))) throw new Error('explicit_calendar_range_required')
        const start = Number(p.start_time), end = Number(p.end_time)
        if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start >= end) throw new Error('invalid_calendar_range')
        // This API forbids mixing a time range with page_token/sync_token.
        // Preserve an explicit partial receipt instead of inventing cursor coverage.
        const value = await client.request('GET', base + `?start_time=${start}&end_time=${end}`, undefined, signal)
        if (!Array.isArray(value.items)) throw new Error('calendar_range_receipt_missing')
        const complete = value.has_more === false && !value.page_token
        return { items: value.items, complete, coverage: complete ? 'requested_range' : 'possibly_truncated_range', requestedRange: { start_time: String(start), end_time: String(end) }, ...(complete ? {} : { warning: '时间范围接口不支持游标续读，请缩小时间范围；当前结果不能作为完整日历清单。' }) }
      }
      if (inspect) return p.event_id ? client.request('GET', base + '/' + id(p.event_id), undefined, signal) : { calendars: await pages('/open-apis/calendar/v4/calendars'), known_resources_only: false, coverage: 'accessible_calendars' }
      if (operation.action === 'delete') {
        if (!['single', 'all'].includes(p.recurrence_scope) || typeof p.notify !== 'boolean') throw new Error('explicit_delete_recurrence_scope_and_notification_required')
        if (p.recurrence_scope === 'single' && !p.event_id?.includes('_')) throw new Error('explicit_occurrence_event_id_required')
        return client.request('DELETE', base + '/' + id(p.event_id) + '?need_notification=' + p.notify, undefined, signal)
      }
      const data = p.event
      if (!data || !data.start_time?.timezone || !data.end_time?.timezone || typeof p.notify !== 'boolean') throw new Error('explicit_calendar_timezone_and_notification_required')
      new Intl.DateTimeFormat('en', { timeZone: data.start_time.timezone })
      new Intl.DateTimeFormat('en', { timeZone: data.end_time.timezone })
      if (data.recurrence && !['single', 'all'].includes(p.recurrence_scope)) throw new Error('explicit_recurrence_scope_required')
      if (p.recurrence_scope === 'single' && !p.event_id?.includes('_')) throw new Error('explicit_occurrence_event_id_required')
      if (operation.action === 'create') return client.request('POST', base, { ...data, need_notification: p.notify }, signal)
      return client.request('PATCH', base + '/' + id(p.event_id), { ...data, need_notification: p.notify }, signal)
    }
    const base = '/open-apis/task/v2/tasks'
    if (query) return pages(`/open-apis/task/v2/tasklists/${id(resource.id)}/tasks`, 'GET')
    if (inspect) return client.request('GET', base + '/' + id(p.task_guid), undefined, signal)
    if (operation.action === 'delete') return client.request('DELETE', base + '/' + id(p.task_guid), undefined, signal)
    const data = p.task
    if (!data || !Object.keys(data).length) throw new Error('explicit_task_update_required')
    if ((operation.action === 'create' || data.members !== undefined) && (!Array.isArray(data.members) || !data.members.length)) throw new Error('explicit_task_members_required')
    if ((operation.action === 'create' || data.due !== undefined) && (!data.due || !/^\d+$/.test(String(data.due.timestamp)))) throw new Error('explicit_task_due_required')
    if (data.completed_at !== undefined && !/^\d+$/.test(String(data.completed_at))) throw new Error('explicit_task_completion_timestamp_required')
    if (operation.action === 'create') return client.request('POST', base, { ...data, tasklists: [{ tasklist_guid: resource.id }] }, signal)
    return client.request('PATCH', base + '/' + id(p.task_guid), { task: data, update_fields: Object.keys(data) }, signal)
  }
  async snapshots(actor: TrustedActor, operation: Operation, signal?: AbortSignal): Promise<unknown> {
    const resource = this.resource(actor, operation)
    if (resource.kind !== 'bitable' || operation.action === 'create') return null
    if (!Array.isArray(operation.params.records)) throw new Error('records_required')
    const base = `/open-apis/bitable/v1/apps/${id(resource.id)}/tables/${id(resource.tableId)}/records/`
    const client = this.api(resource)
    return Promise.all(operation.params.records.map(async (row: any) => {
      const value = await client.request('GET', base + id(row.record_id), undefined, signal)
      if (!value.record?.fields) throw new Error('record_readback_missing')
      const keys = operation.action === 'delete' ? Object.keys(value.record.fields) : Object.keys(row.fields ?? {})
      return { id: row.record_id, fields: Object.fromEntries(keys.sort().map(key => [key, value.record.fields[key] ?? null])) }
    }))
  }
  async verify(actor: TrustedActor, operation: Operation, result: Record<string, any>, signal?: AbortSignal): Promise<unknown> {
    const resource = this.resource(actor, operation)
    if (resource.kind !== 'bitable') {
      if (operation.action === 'delete') return { apiDeletionAccepted: true, receipt: result, ...(resource.kind === 'documents' && result.task_id ? { asynchronous: true } : {}) }
      if (resource.kind === 'calendar') {
        const eventId = result.event?.event_id ?? operation.params.event_id
        const value = await this.api(resource).request('GET', `/open-apis/calendar/v4/calendars/${id(resource.id)}/events/${id(eventId)}`, undefined, signal)
        if (value.event?.event_id !== eventId) throw new Error('calendar_readback_missing')
        return value
      }
      if (resource.kind === 'tasks') {
        const guid = result.task?.guid ?? operation.params.task_guid
        const value = await this.api(resource).request('GET', `/open-apis/task/v2/tasks/${id(guid)}`, undefined, signal)
        if (value.task?.guid !== guid) throw new Error('task_readback_missing')
        return value
      }
      if (operation.action === 'create') {
        const documentId = result.document?.document_id
        const value = await this.api(resource).request('GET', `/open-apis/docx/v1/documents/${id(documentId)}`, undefined, signal)
        if (value.document?.document_id !== documentId) throw new Error('document_readback_missing')
        return value
      }
      if (!Array.isArray(result.children) || result.children.length !== operation.params.text.split('\n').length || result.children.some((row: any) => !row.block_id)) throw new Error('document_append_receipt_missing')
      return { appendedBlockIds: result.children.map((row: any) => row.block_id), receipt: result }
    }
    const base = `/open-apis/bitable/v1/apps/${id(resource.id)}/tables/${id(resource.tableId)}/records/`
    const client = this.api(resource), records = operation.action === 'create' ? result.records : operation.params.records
    if (!Array.isArray(records) || records.length !== operation.params.records.length) throw new Error('incomplete_batch_receipt')
    return Promise.all(records.map(async (record: any, index: number) => {
      if (operation.action === 'delete') {
        const confirmed = result.records?.find((row: any) => row.record_id === record.record_id)
        if (!confirmed?.deleted) throw new Error('delete_receipt_missing')
        return { id: record.record_id, deleted: true }
      }
      const actual = await client.request('GET', base + id(record.record_id), undefined, signal)
      const expected = operation.params.records[index].fields ?? {}
      for (const [key, value] of Object.entries(expected)) {
        const received = actual.record?.fields?.[key]
        if (value === null && received == null) continue
        if (JSON.stringify(received) !== JSON.stringify(value)) throw new Error('record_verification_mismatch')
      }
      return actual.record
    }))
  }
}
