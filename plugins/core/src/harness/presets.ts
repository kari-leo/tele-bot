import type {Context} from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-agent-preset-registry'
/** Use the official persistent preset binding instead of a newer schedule resolver event. */
export async function receiptPreset(ctx:Context,id:string,allow:string[]){
  const registry=ctx.get('agentPresets') as Context['agentPresets']|undefined
  if(!registry)throw new Error('official_agent_presets_required')
  return registry.register({id,name:'Telebot 唤醒回执（不执行业务）',description:'Only durable timing receipts; no model or business actions.',plugins:[{name:'@telebot/core/restricted-tools',config:{allow}}]})
}
