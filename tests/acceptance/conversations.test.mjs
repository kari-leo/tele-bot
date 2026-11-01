import test from 'node:test'
import assert from 'node:assert/strict'
import { HarnessConversations,feishuTaskGuard } from '../../plugins/core/lib/harness/conversations.js'
import {TelebotCore} from '../../plugins/core/lib/index.js'
import {createRequire} from 'node:module'
import {pathToFileURL} from 'node:url'
import {renderPluginResult} from '../../plugins/core/lib/presentation.js'
import {Interactions} from '../../plugins/core/lib/interactions.js'

test('semantic confirmation uses a single tool-free official model request bound to admission candidates',async()=>{
  const require=createRequire(new URL('../../plugins/core/package.json',import.meta.url)),{Context}=await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')).href),ctx=new Context(),requests=[]
  let result={decision:'confirm',selection:1},finish='stop',present=true
  ctx.provide('llm',{async *stream(options){requests.push(options);yield{type:'text-delta',index:0,text:JSON.stringify(result)};yield{type:'finish',reason:{kind:finish}}}})
  const core=new TelebotCore(ctx),actor={origin:'feishu',userId:'alice',chatId:'chat',sessionId:'continued',relatedSessionIds:['original'],cwd:process.cwd()},spec={id:actor.sessionId,cwd:actor.cwd,provider:'fixture',model:'selected-model'}
  core.registerConfirmations('bitable',{pending:a=>present&&a.userId==='alice'&&a.sessionId==='original'?[{token:'frozen',label:'将两条任务的详情改为已预览文本'}]:[],confirm:async()=>{throw Error('Routing must not execute')},cancel:async()=>{throw Error('Routing must not execute')}})
  for(const text of ['好的，请修改把','好的，请修改吧','你就照刚才那个方案帮我处理一下吧']){
    const ticket=core.routeInput(actor,text,true);assert.equal(ticket?.kind,'interpretation',text)
    const before=requests.length
    const route=await core.resolveInput(actor,ticket.text,spec,AbortSignal.timeout(1000))
    assert.equal(route.kind,'confirmation');assert.equal(route.text,'confirm bitable frozen');assert.equal(route.sessionId,'original');assert.equal(requests.length,before+1)
    const request=requests.at(-1);assert.equal(request.provider,'fixture');assert.equal(request.model,'selected-model');assert.deepEqual(request.tools,[])
    const input=JSON.parse(request.messages[0].content[0].text);assert.equal(input.reply,text);assert.equal(input.candidates.length,1);assert(!JSON.stringify(input).includes('frozen'));assert(!JSON.stringify(input).includes('original'))
    assert.equal((await core.resolveInput(actor,ticket.text,spec,AbortSignal.timeout(1000))).kind,'notice')
  }
  const ticket=core.routeInput(actor,'麻烦处理一下吧',true)
  assert.equal((await core.resolveInput({...actor,userId:'bob'},ticket.text,spec,AbortSignal.timeout(1000))).kind,'notice')
  present=false
  assert.equal((await core.resolveInput(actor,ticket.text,spec,AbortSignal.timeout(1000))).kind,'notice','Never retarget an expired candidate')
  present=true
  for(const decision of ['other','change','ambiguous']){
    result={decision,selection:null};const pending=core.routeInput(actor,'这个内容我还想讨论一下',true),route=await core.resolveInput(actor,pending.text,spec,AbortSignal.timeout(1000))
    assert.equal(route.kind,decision==='ambiguous'?'notice':'text')
    if(route.kind==='text')assert.equal(route.text,'这个内容我还想讨论一下')
  }
  result={decision:'confirm',selection:99}
  let pending=core.routeInput(actor,'请按刚才的方案修改',true)
  assert.equal((await core.resolveInput(actor,pending.text,spec,AbortSignal.timeout(1000))).kind,'notice')
  result={decision:'confirm',selection:1};finish='max-tokens';pending=core.routeInput(actor,'请按刚才的方案修改',true)
  assert.equal((await core.resolveInput(actor,pending.text,spec,AbortSignal.timeout(1000))).kind,'notice','Incomplete generation never grants permission')
})

test('resumed Feishu conversations receive current channel guidance above stale exact-word instructions',async()=>{
  const injected=[],events=[],adapter=new HarnessConversations({on:()=>()=>{}})
  const agent={session:{id:'resumed',snapshotEvents:()=>events},whenIdle:async()=>{},inject:message=>injected.push(message),followup:message=>{
    events.push({type:'turn/start',data:{turn:1}},{type:'user/message',data:{id:message.id}},{type:'assistant/message',data:{turn:1,message:{content:[{type:'text',text:'新预览'}]}}},{type:'turn/end',data:{turn:1,reason:{kind:'completed'}}})
  }}
  adapter.open=async()=>agent;adapter.flush=async()=>{}
  const spec={id:'resumed',cwd:process.cwd(),allowCreate:false},actor={origin:'feishu',userId:'alice',chatId:'chat',sessionId:spec.id,cwd:spec.cwd}
  for(const id of ['first','second'])await adapter.run(spec,{id,text:'生成预览',actor})
  assert.equal(injected.length,1);assert.equal(injected[0].source.kind,'telebot-channel-policy')
  const policy=injected[0].content[0].text;assert.match(policy,/自然语言/);assert.match(policy,/已经过时/);assert.match(policy,/命令只是可选备用入口/)
})

test('spoken approval stays one-use and bound to the original user',async t=>{
  const broker=new Interactions(),actor={origin:'feishu',userId:'alice',chatId:'chat',sessionId:'original',cwd:process.cwd()}
  t.after(()=>broker.cancelActor(actor))
  let card
  const waiting=broker.ask(actor,'允许本次操作？',['允许一次','拒绝'],true,async value=>{card=value})
  await new Promise(setImmediate)
  assert.equal(broker.routeInput({...actor,userId:'bob'},'可以，执行吧'),undefined)
  assert.equal(broker.routeInput(actor,'确认，但日期改成明天'),undefined)
  const route=broker.routeInput(actor,'好的，可以执行了')
  assert.equal(route?.text,'/answer '+card.token+' 允许一次')
  assert.equal(broker.answer(actor,card.token,'允许一次'),true)
  assert.equal(broker.answer(actor,card.token,'允许一次'),false)
  assert.equal(await waiting,'允许一次')
})

test('confirmation intent accepts the users complete reply in its original pending session',async()=>{
  const require=createRequire(new URL('../../plugins/core/package.json',import.meta.url)),{Context}=await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')).href)
  const core=new TelebotCore(new Context()),actor={origin:'feishu',userId:'alice',chatId:'chat',sessionId:'original',cwd:process.cwd()},continued={...actor,sessionId:'continued',relatedSessionIds:['original']}
  let calls=0
  core.registerConfirmations('bitable',{pending:a=>a.sessionId==='original'&&a.userId==='alice'?[{token:'frozen',label:'修改备注'}]:[],confirm:async()=>{calls++;return{status:'completed',text:'done'}},cancel:async()=>({status:'cancelled',text:'cancelled'})})
  for(const text of ['确认，只改一条即可','好的，按预览执行吧','可以，执行吧','我同意这个操作','没问题，就这么办']){
    const route=core.routeInput(continued,text)
    assert.equal(route?.kind,'confirmation',text);assert.equal(route.text,'confirm bitable frozen');assert.equal(route.sessionId,'original')
  }
  for(const text of ['先不要执行了','不用改了','取消，这次先不改了','好的，先别执行'])assert.equal(core.routeInput(continued,text)?.text,'cancel bitable frozen',text)
  for(const text of ['确认是什么意思？','如果确认会怎样','确认，但日期改到明天','确认，只改第一条','确认，只改一条即可，其他两条也改','确认，不要执行','确认。接下来删除其他记录','“确认”','他说可以执行了','你好','好的'])assert.notEqual(core.routeInput(continued,text)?.kind,'confirmation',text)
  assert.equal(calls,0,'Routing never executes business operations')
})

test('plugin previews cover dates, deletion, task grants and publication without raw JSON or false success',()=>{
  const preview={status:'confirmation_required',token:'frozen-token',expiresInSeconds:900,preview:{resource:'待办',action:'update',frozenResource:'internal-only',params:{records:[{record_id:'one',fields:{截止日期:1793894400000,状态:'完成'}}]}},baseline:[{id:'one',fields:{截止日期:1786204800000,状态:'进行中'}}]}
  const text=renderPluginResult(JSON.stringify(preview))
  for(const word of ['尚未执行','确认','取消','2026-08-09','2026-11-06','进行中 → 完成','/bitable confirm frozen-token'])assert(text.includes(word),text)
  assert(!text.includes('internal-only'));assert(!text.includes('confirmation_required'))
  assert(!renderPluginResult({status:'completed',text:JSON.stringify(preview)}).startsWith('已完成'))
  const deletion=renderPluginResult({...preview,preview:{...preview.preview,action:'delete',params:{records:[{record_id:'one'}]}}})
  assert.match(deletion,/将删除/);assert.match(deletion,/2026-08-09/)
  for(const payload of [{status:'confirmation_required',token:'task-token',payload:{grant:'task-one',scope:{cwd:'D:/work',prompt:'研究后输出报告',executor:'research'}},confirm:'/schedule confirm task-token'},
    {status:'confirmation_required',token:'publish-token',action:'git',files:[{path:'post.md',sha256:'hash'}],remote:'origin',branch:'main',confirm:'/publish confirm publish-token'},
    {status:'completed',text:JSON.stringify({status:'completed',text:JSON.stringify({summary:'代码已完成',threadId:'thread',artifacts:[]})})},
    {status:'cancelled'}, {status:'expired'}, {status:'unknown',receipt:{records:[]}}]){
    const shown=renderPluginResult(JSON.stringify(payload));assert(!shown.includes('"status"'),shown);assert(!shown.trim().startsWith('{'),shown)
  }
  assert(!renderPluginResult({status:'unknown',receipt:{records:[]}}).includes('已完成操作'))
  assert.equal(renderPluginResult('普通 Markdown **答案**'),'普通 Markdown **答案**')
})

test('plugin feature and confirmation receipts show readable text instead of nested JSON',async()=>{
  const require=createRequire(new URL('../../plugins/core/package.json',import.meta.url)),{Context}=await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')).href)
  const core=new TelebotCore(new Context()),actor={origin:'feishu',userId:'alice',chatId:'chat',sessionId:'session',cwd:process.cwd()}
  const raw={status:'completed',receipt:{records:[{record_id:'record-one',fields:{截止日期:1793894400000}}]},verified:[{record_id:'record-one',fields:{截止日期:1793894400000}}]}
  const reply=async()=>({status:'completed',text:JSON.stringify(raw)})
  core.registerFeature('bitable',reply)
  core.registerConfirmations('bitable',{pending:()=>[{token:'frozen',label:'修改日期'}],confirm:reply,cancel:reply})
  const delivery={id:'existing-durable-delivery',actor,result:await reply()}
  core.registerDelivery('feishu',async request=>{assert.equal(request,delivery,'Display changes must preserve durable delivery hashes');return 'queued'})
  assert.equal(await core.deliver(delivery),'queued')
  for(const result of [await core.invoke('bitable',{actor,text:'confirm frozen',signal:AbortSignal.timeout(1000)}),await core.confirmLatest(actor,false,AbortSignal.timeout(1000))]){
    assert(!result.text.trim().startsWith('{'),result.text)
    assert.match(result.text,/已完成/);assert.match(result.text,/核验/);assert.match(result.text,/2026-11-06/)
    assert(!result.text.includes('"receipt"'))
  }
})

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
