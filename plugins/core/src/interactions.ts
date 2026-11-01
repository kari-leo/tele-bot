import { randomUUID } from 'node:crypto'
import type { TrustedActor, HumanInputRoute } from './contracts.js'
export interface ChannelQuestion { token: string; question: string; choices: string[]; approval: boolean }
export type QuestionSink = ((question: ChannelQuestion) => Promise<void>) & { settled?:()=>void }
/** One-use in-memory answer authority. Restart deliberately invalidates all pending answers. */
export class Interactions {
  private readonly pending = new Map<string, { actor: TrustedActor; question:string; choices: string[]; approval: boolean; settle(answer: string): void }>()
  async ask(actor: TrustedActor, question: string, choices: string[], approval: boolean, sink: QuestionSink, signal?: AbortSignal): Promise<string> {
    if (this.pending.size >= 100) throw new Error('interaction_limit')
    const token = randomUUID(), controller = new AbortController()
    const lifetime = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal
    const timer = setTimeout(() => controller.abort(), 300000)
    let resolve: (answer: string) => void = () => {}
    const answer = new Promise<string>(done => { resolve = done })
    const abort = () => resolve('')
    this.pending.set(token, { actor, question, choices, approval, settle: resolve })
    lifetime.addEventListener('abort', abort, { once: true })
    try {
      lifetime.throwIfAborted()
      await sink({ token, question, choices, approval })
      return await answer
    } finally { clearTimeout(timer); lifetime.removeEventListener('abort', abort); this.pending.delete(token);sink.settled?.() }
  }
  private matching(actor:TrustedActor){return [...this.pending].filter(([,row])=>['origin','userId','chatId','cwd'].every(key=>row.actor[key as keyof TrustedActor]===actor[key as keyof TrustedActor])&&(row.actor.threadId??'')===(actor.threadId??'')&&(actor.origin==='feishu'||row.actor.sessionId===actor.sessionId))}
  waitingSessions(){return [...new Set([...this.pending.values()].map(row=>row.actor.sessionId))]}
  approvals(actor:TrustedActor){return this.matching(actor).filter(([,row])=>row.approval).map(([token,row])=>({token,label:row.question,sessionId:row.actor.sessionId}))}
  routeApproval(actor:TrustedActor,token:string,decision:'confirm'|'cancel'):HumanInputRoute|undefined{
    const row=this.matching(actor).find(([key,row])=>key===token&&row.approval)
    if(row)return this.routeAnswer(row,decision==='confirm'?'允许一次':'拒绝',true)
  }
  routeInput(actor:TrustedActor,text:string):HumanInputRoute|undefined{
    const pending=this.matching(actor),value=text.trim();if(!pending.length||!value)return
    const explicit=/^回答\s*(\d*)\s*[:：]\s*([\s\S]+)$/.exec(value)
    if(explicit){const row=explicit[1]?pending[Number(explicit[1])-1]:pending.length===1?pending[0]:undefined;if(!row)return{kind:'notice',text:'请回复“回答1：内容”等明确选择：\n'+pending.map(([,row],index)=>`${index+1}. ${row.question.slice(0,200)}`).join('\n')};return this.routeAnswer(row,explicit[2],true)}
    const matching=pending.filter(([,row])=>row.choices.includes(value)||row.approval&&Boolean(confirmationIntent(value))||!row.approval&&/^(?:选择|选)?\s*\d+$/.test(value)&&Boolean(row.choices[Number(value.replace(/\D/g,''))-1]))
    if(matching.length===1)return this.routeAnswer(matching[0],value,false)
    if(matching.length>1)return{kind:'notice',text:'有多个待回答问题，请回复“回答1：内容”等明确选择：\n'+pending.map(([,row],index)=>`${index+1}. ${row.question.slice(0,200)}`).join('\n')}
    // Ordinary messages are never silently consumed as an answer or a rejection.
    return
  }
  private routeAnswer([token,row]:[string,{actor:TrustedActor;choices:string[];approval:boolean}],value:string,_explicit:boolean):HumanInputRoute{
    let answer=value
    if(row.approval){
      const intent=confirmationIntent(value)
      const decision=intent?.decision
      answer=decision==='confirm'?'允许一次':decision==='cancel'?'拒绝':value
      if(!row.choices.includes(answer))return{kind:'notice',text:'请明确回复“允许一次”或“拒绝”；本次询问不会授予持续权限。'}
    }else if(row.choices.length){
      const index=/^(?:选择|选)?\s*(\d+)$/.exec(value)
      if(!row.choices.includes(value)&&index&&row.choices[Number(index[1])-1])answer=row.choices[Number(index[1])-1]
    }
    return{kind:'answer',text:'/answer '+token+' '+answer,sessionId:row.actor.sessionId}
  }
  cancelActor(actor:TrustedActor){for(const [token,row]of this.matching(actor)){this.pending.delete(token);row.settle('')}}
  answer(actor: TrustedActor, token: string, answer: string): boolean {
    const row = this.pending.get(token)
    if (!row || row.actor.origin!==actor.origin || row.actor.userId !== actor.userId || row.actor.chatId !== actor.chatId || (row.actor.threadId??'')!==(actor.threadId??'') || row.actor.sessionId !== actor.sessionId || row.actor.cwd !== actor.cwd || answer.length > 10000 || (row.approval && !row.choices.includes(answer))) return false
    this.pending.delete(token); row.settle(answer); return true
  }
  cancelSession(sessionId: string) { for (const [token, row] of this.pending) if (row.actor.sessionId === sessionId) { this.pending.delete(token); row.settle('') } }
}
export interface ConfirmationIntent { decision:'confirm'|'cancel' }
/** Parse complete consent clauses, never a positive substring followed by unchecked instructions.
 * Unsupported conditions remain ordinary chat so the Agent can produce a revised preview.
 */
export function confirmationIntent(text:string):ConfirmationIntent|undefined{
  const value=text.trim().replace(/[！!。\s]+$/,'')
  if(!value||value.length>500||/[?？“”"「」]/.test(value))return
  const clauses=value.split(/[，,。；;！!\n]+/).map(part=>part.trim()).filter(Boolean)
  let decision:ConfirmationIntent['decision']|undefined
  for(const clause of clauses){
    const normalized=clause.replace(/^(?:那就|那|请|麻烦你|麻烦)/,'').replace(/(?:即可|就行|吧|了)$/,'')
    let next:ConfirmationIntent['decision']|undefined
    if(/^(?:确认|确定|同意|同意执行|同意授权|确认授权|允许|允许一次|允许执行|执行|确认执行|确认修改|确认提交|确认按预览执行|confirm|我同意(?:这个|本次)?操作|我同意|可以执行|可以修改|按(?:这个|预览|你说的)(?:来|执行|做)|照预览执行|就这么办|就这样执行)$/i.test(normalized))next='confirm'
    else if(/^(?:取消|拒绝|不允许|不同意|不执行|不要执行|先不要执行|先别执行|先不改|这次先不改|不用改|别执行|取消本次执行|取消操作|取消修改|cancel)$/i.test(normalized))next='cancel'
    else if(/^(?:好|好的|可以|行|没问题|嗯)$/.test(normalized))continue
    // These descriptive clauses accompany consent; they do not add a record-count gate.
    else if(/^(?:只|仅|就)(?:改|修改|更新|处理)(?:[一二两三四五六七八九十]|[1-9]\d*)条(?:记录)?$/.test(normalized))continue
    else return
    if(decision&&decision!==next)return
    decision=next
  }
  // Acknowledgments alone are not authorization.
  if(decision)return{decision}
}
export function confirmationDecision(text:string):'confirm'|'cancel'|undefined{
  return confirmationIntent(text)?.decision
}
