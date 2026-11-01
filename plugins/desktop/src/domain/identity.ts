/** Public Session facts, without importing host service identities. */
export interface EntryEvent { type: string; data: any }
export function ownerRequest(events: readonly EntryEvent[], subagent = false): string | undefined {
  if (subagent) return
  const start = events.findLastIndex(event => event.type === 'turn/start')
  if (start < 0) return
  let request: string | undefined
  for (const event of events.slice(start + 1)) {
    if (event.type === 'turn/end') return
    if (event.type !== 'user/message' || event.data.source?.kind !== 'user') continue
    const source = event.data.source
    // rc.5 SessionController constructs these facts AFTER authenticated RPC
    // admission. Feishu, ACP and model-supplied JSON do not mint this source.
    if (typeof source.rpcId !== 'string' || !source.rpcId || source.rpcId.length > 512 || Object.hasOwn(source, 'principal')) return
    request = source.rpcId
  }
  return request
}
