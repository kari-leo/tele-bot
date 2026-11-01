import test from 'node:test'
import assert from 'node:assert/strict'
import { HarnessConversations,feishuTaskGuard } from '../../plugins/core/lib/harness/conversations.js'
import {TelebotCore} from '../../plugins/core/lib/index.js'
import {createRequire} from 'node:module'
import {pathToFileURL} from 'node:url'

test('continued chat freezes business confirmation in its original session and never confuses a pending approval with that confirmation',async t=>{
  const require=createRequire(new URL('../../plugins/core/package.json',import.meta.url)),{Context}=await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')).href)
  const core=new TelebotCore(new Context()),actor={userId:'alice',chatId:'chat',threadId:'thread',sessionId:'old',cwd:process.cwd(),privateChat:true,origin:'feishu'},continued={...actor,sessionId:'new',relatedSessionIds:['old']}
  const confirmed=[];core.registerConfirmations('schedule',{pending:a=>a.sessionId==='old'&&a.userId==='alice'?[{token:'frozen',label:'原任务'}]:[],confirm:async a=>{confirmed.push(a.sessionId);return{status:'completed',text:'done'}},cancel:async()=>({status:'cancelled',text:'cancelled'})})
  const routed=core.routeInput(continued,'同意');assert.equal(routed.kind,'confirmation');assert.equal(routed.sessionId,'old');assert.equal(routed.text,'confirm schedule frozen')
  let card;const pending=core.interactions.ask(actor,'permission?',['允许一次','拒绝'],true,async value=>{card=value});t.after(()=>core.interactions.cancelActor(actor));await new Promise(setImmediate)
  assert.equal(core.routeInput(continued,'同意').kind,'notice');assert.equal(core.routeInput(continued,'你好'),undefined)
  const explicit=core.routeInput(continued,'回答1：允许一次');assert.equal(explicit.sessionId,'old')
  assert.equal(core.routeInput(continued,'确认第1项').sessionId,'old')
  assert.equal(core.routeInput({...continued,userId:'bob'},'同意').kind,'notice')
  assert.equal(core.interactions.answer(continued,card.token,'允许一次'),false);assert.equal(core.interactions.answer(actor,card.token,'允许一次'),true);assert.equal(await pending,'允许一次')
  await core.confirmLatest({...continued,sessionId:routed.sessionId},false,AbortSignal.timeout(1000),{name:'schedule',token:'frozen'});assert.deepEqual(confirmed,['old'])
})

test('task isolation follows late plugin activation and never restricts another native session',()=>{
  let active=false;const guard=feishuTaskGuard('feishu-session',()=>active),call={name:'schedule_create',agent:{session:{id:'feishu-session'}}}
  assert.equal(guard(call),undefined);active=true;assert.match(guard(call),/requires_isolated_task/)
  assert.equal(guard({...call,agent:{session:{id:'native-session'}}}),undefined)
  assert.equal(guard({...call,name:'web_fetch'}),undefined);active=false;assert.equal(guard(call),undefined)
})

test('reasoning-only and tool streams publish bounded status without exposing reasoning, arguments or another session', async t => {
  let clock=10000;t.mock.method(Date,'now',()=>clock)
  const observers=new Set(),progress=[],events=[]
  const context={on(name,listener){assert.equal(name,'agent/assistant-stream');observers.add(listener);return()=>observers.delete(listener)}}
  const adapter=new HarnessConversations(context)
  const agent={session:{id:'owned',snapshotEvents:()=>events},whenIdle:async()=>{},followup(message){
    for(const listener of observers){
      listener({agent:{session:{id:'other'}},frame:{type:'chunk',chunk:{type:'text-delta',text:'foreign text'}}})
      for(let i=0;i<30;i++){clock+=1100;listener({agent,frame:{type:'chunk',chunk:{type:'reasoning-delta',text:'private reasoning'}}})}
      listener({agent,frame:{type:'chunk',chunk:{type:'tool-call-delta',arguments:'secret tool input'}}})
    }
    events.push({type:'turn/start',data:{turn:1}},{type:'user/message',data:{id:message.id}},
      {type:'assistant/message',data:{turn:1,message:{content:[{type:'text',text:'final reply'}]}}},
      {type:'turn/end',data:{turn:1,reason:{kind:'completed'}}})
  }}
  adapter.open=async()=>agent;adapter.flush=async()=>{}
  const result=await adapter.run({id:'owned',cwd:process.cwd(),allowCreate:true},{id:'input',text:'hello',progress:text=>progress.push(text)})
  assert.equal(result.text,'final reply');assert.equal(progress[0],'正在处理，请稍候...')
  assert.equal(progress.filter(text=>text==='正在思考，请稍候…').length,1)
  assert(progress.length<=5);assert(!progress.join('').match(/private reasoning|secret tool input|foreign text/));assert.equal(observers.size,0)
})
