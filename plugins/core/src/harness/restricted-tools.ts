import type {Context} from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-tools'
import type {} from '@deepseek-ai/dsh-agent'
export const name='telebot-receipt-policy'
export const inject=['tools']
/** Durable receipt presets stay inert even when the official controller resumes them cold. */
export function apply(ctx:Context,config:{allow:string[]}){
  if(!Array.isArray(config.allow)||!config.allow.length||config.allow.some(name=>typeof name!=='string'))throw new Error('receipt_tool_allowlist_required')
  ctx.tools.restrict({allow:config.allow})
  ctx.on('agent/pre-step',async()=>({kind:'reject' as const}))
}
