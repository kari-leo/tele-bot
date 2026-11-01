import { createServer } from 'node:http'
import { once } from 'node:events'

/** Loopback platform boundary: no real tenant, credentials, messages or API calls. */
export async function startMockFeishu({ appId = 'cli_test', appSecret = 'loopback-app-secret', tables=[{table_id:'test-table',name:'Isolated table'}] } = {}) {
  const attempts = [], sent = [], edits=[], messages=new Map(), behaviors = new Map(), resources = new Map(), uploads = [], records = new Map(), calendarEvents = new Map()
  let tokenRequests = 0
  const business=[]
  const server = createServer(async (request, response) => {
    if (request.method === 'GET' && request.url.includes('/resources/')) {
      const key = decodeURIComponent(request.url.match(/\/resources\/([^?]+)/)?.[1] ?? '')
      const file = resources.get(key)
      if (!file) { response.writeHead(404).end(); return }
      response.writeHead(200, { 'Content-Type': file.mediaType }).end(file.data); return
    }
    let raw = ''
    for await (const chunk of request) raw += chunk
    if (request.url === '/open-apis/im/v1/files') {
      uploads.push(raw); response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ code: 0, data: { file_key: 'uploaded-' + uploads.length } })); return
    }
    const body = raw ? JSON.parse(raw) : {}
    response.setHeader('Content-Type', 'application/json')
    if (request.url === '/open-apis/auth/v3/tenant_access_token/internal') {
      tokenRequests++
      if (body.app_id !== appId || body.app_secret !== appSecret) {
        response.writeHead(403).end('{}'); return
      }
      response.end(JSON.stringify({ code: 0, tenant_access_token: 'loopback-access', expire: 7200 })); return
    }
    if (request.headers.authorization !== 'Bearer loopback-access') { response.writeHead(403).end('{}'); return }
    if (request.url.startsWith('/open-apis/calendar/v4/calendars/')) {
      const eventId = request.url.match(/\/events\/([^?]+)/)?.[1]
      let data
      if (request.method === 'POST') { const id = 'event-' + (calendarEvents.size + 1); const event = { ...body, event_id: id }; calendarEvents.set(id, event); data = { event } }
      else if (request.method === 'PATCH') { const event = { ...calendarEvents.get(eventId), ...body, event_id: eventId }; calendarEvents.set(eventId, event); data = { event } }
      else if (request.method === 'DELETE') { calendarEvents.delete(eventId); data = {} }
      else data = eventId ? { event: calendarEvents.get(eventId) } : { items: [...calendarEvents.values()], has_more: false }
      response.end(JSON.stringify({ code: 0, data })); return
    }
    if (request.url.includes('/bitable/')) {
      business.push({method:request.method,url:request.url})
      let data
      if (/\/apps\/[^/?]+$/.test(request.url)) data={app:{name:'Todo'}}
      else if (/\/tables\?/.test(request.url)) data = { items: tables, has_more: false }
      else if (request.url.includes('/fields')) data = { items: [{ field_name: 'name', type: 1 }], has_more: false }
      else if (request.url.includes('/search')) data = { items: [...records].map(([record_id, fields]) => ({ record_id, fields })), has_more: false }
      else if (request.url.includes('/batch_create')) data = { records: body.records.map((row, i) => { const record_id = 'rec_' + (records.size + 1); records.set(record_id, row.fields); return { record_id, fields: row.fields } }) }
      else if (request.url.includes('/batch_update')) data = { records: body.records.map(row => { records.set(row.record_id, row.fields); return row }) }
      else if (request.url.includes('/batch_delete')) data = { records: body.records.map(record_id => ({ record_id, deleted: records.delete(record_id) })) }
      else { const record_id = request.url.split('/').at(-1); data = { record: { record_id, fields: records.get(record_id) } } }
      response.end(JSON.stringify({ code: 0, data })); return
    }
    const replyTo = request.url.match(/\/messages\/([^/]+)\/reply$/)?.[1]
    const editing=request.method==='PUT',editId=editing?request.url.match(/\/messages\/([^/?]+)$/)?.[1]:undefined
    const target=editId?messages.get(editId):undefined
    const entry = { method:request.method,url: request.url, replyTo:target?.replyTo??replyTo, body, messageId:editId??'om_reply_' + (attempts.length + 1) }
    attempts.push(entry)
    const optionalProgress=body.msg_type==='interactive'&&JSON.parse(body.content).header?.title?.content==='执行进度'
    const behavior = optionalProgress ? undefined : behaviors.get(replyTo)
    if (behavior === 'rate-limit-once') { behaviors.delete(replyTo); response.writeHead(429).end('{}'); return }
    if (behavior === 'disconnect') { request.socket.destroy(); return }
    if (behavior === 'hold') { return }
    if(editing){if(!target){response.writeHead(404).end(JSON.stringify({code:230001}));return}target.body=structuredClone(body);edits.push(entry);response.end(JSON.stringify({code:0,data:{}}));return}
    const published={...entry,body:structuredClone(body)};sent.push(published);messages.set(entry.messageId,published)
    response.end(JSON.stringify({ code: 0, data: { message_id: entry.messageId } }))
  })
  server.listen(0, '127.0.0.1'); await once(server, 'listening')
  return {
    attempts, sent, edits, behaviors, resources, uploads, records, calendarEvents, business, get tokenRequests() { return tokenRequests },
    origin: 'http://127.0.0.1:' + server.address().port,
    async close() { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) },
  }
}
