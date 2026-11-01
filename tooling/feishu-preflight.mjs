/** Readiness inspection only: no secret output, API traffic or production launch. */
import { readFile, stat } from 'node:fs/promises'
import path from 'node:path'
import { parseArgs } from 'node:util'
import { fileURLToPath } from 'node:url'
import { officialHost } from './official-host.mjs'
export async function inspectReference(filename) {
  const allowed = ['FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_ADMIN_USER_IDS', 'FEISHU_VERIFICATION_TOKEN', 'FEISHU_ENCRYPT_KEY']
  const presence = Object.fromEntries(allowed.map(key => [key, false]))
  for (const line of (await readFile(filename, 'utf8')).split(/\r?\n/)) {
    const match = /^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*?)\s*$/.exec(line)
    if (!match || !allowed.includes(match[1])) continue
    const value = match[2].replace(/^(?:"([\s\S]*)"|'([\s\S]*)')$/, '$1$2')
    presence[match[1]] = value.length > 0
  }
  return { presence, sourceAuthenticationConfigured: presence.FEISHU_VERIFICATION_TOKEN || presence.FEISHU_ENCRYPT_KEY,
    applicationConfigured: presence.FEISHU_APP_ID && presence.FEISHU_APP_SECRET && presence.FEISHU_ADMIN_USER_IDS }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { values } = parseArgs({ options: { reference: { type: 'string' }, workspace: { type: 'string' } } })
  if (!values.reference || !values.workspace || !path.isAbsolute(values.workspace)) throw new Error('Use --reference <external env file> --workspace <absolute directory>, and set TELEBOT_DSH_INSTALL')
  const host = await officialHost(), report = await inspectReference(values.reference)
  const workspaceReady = (await stat(values.workspace)).isDirectory()
  console.log(JSON.stringify({ harnessVersion: host.version, workspaceReady, ...report, liveApiCalled: false,
    remainingExternalInputs: ['dedicated test application/chat', 'HTTPS webhook route on existing port 3000', 'test resource allowlist and application API scope/resource ACL review'] }, null, 2))
  if (!workspaceReady || !report.sourceAuthenticationConfigured || !report.applicationConfigured) process.exitCode = 2
}
