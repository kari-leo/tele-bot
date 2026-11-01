import test from 'node:test'
import assert from 'node:assert/strict'
import path from 'node:path'
import {mkdir,writeFile,readFile,symlink} from 'node:fs/promises'
import {RunSkills} from '../../plugins/codex/lib/adapters/skills.js'
import {CodingService} from '../../plugins/codex/lib/adapters/service.js'
import {temporaryDirectory,cleanup} from '../support/temp-dir.mjs'
test('selected Skills freeze source hashes, run copies retain state, changed source/program is refused and original config stays intact',async t=>{
  const root=await temporaryDirectory(t),source=path.join(root,'original-skills'),cwd=path.join(root,'workspace')
  await mkdir(path.join(source,'report'),{recursive:true});await mkdir(cwd);await writeFile(path.join(source,'report/SKILL.md'),'# synthetic skill\nUse only this local run copy.')
  const skills=new RunSkills(source,['report']),snapshot=await skills.snapshot(),copy=await skills.prepare(cwd,snapshot);assert(copy.startsWith(cwd));assert.equal(await readFile(path.join(copy,'report/SKILL.md'),'utf8'),await readFile(path.join(source,'report/SKILL.md'),'utf8'))
  await writeFile(path.join(copy,'report/dedup-state.json'),'{"seen":["synthetic"]}');assert.equal(await skills.prepare(cwd,snapshot),copy);assert((await readFile(path.join(copy,'report/dedup-state.json'),'utf8')).includes('synthetic'))
  await writeFile(path.join(source,'report/SKILL.md'),'changed source');await assert.rejects(skills.prepare(cwd,snapshot),/changed_new_confirmation/)
  assert.equal(await readFile(path.join(copy,'report/SKILL.md'),'utf8'),'# synthetic skill\nUse only this local run copy.')
  await assert.rejects(new RunSkills(source,['../outside']).snapshot(),/invalid_selected/)
})
test('Codex UI execution limit reaches the actual executor and scheduled dispatch keys cannot execute twice',async t=>{
  const cwd=await temporaryDirectory(t),actor={userId:'alice',chatId:'chat',sessionId:'s',cwd,privateChat:true,origin:'feishu'};let calls=0
  const backend={enforcesReadOnly:true,async execute(_request,options){calls++;await new Promise(resolve=>options.signal.addEventListener('abort',resolve,{once:true}));return{status:'failed',text:'stopped by configured deadline',artifacts:[]}},async dispose(){}}
  const service=new CodingService(backend,path.join(cwd,'coding.sqlite'),['alice'],[cwd],'alice',30,900000);cleanup(t,()=>service.dispose())
  const started=Date.now(),timer=setTimeout(()=>{},2000);try{assert.equal((await service.command(actor,'inspect synthetic slow task',AbortSignal.timeout(5000))).status,'failed');assert(Date.now()-started<1500)}finally{clearTimeout(timer)}
  const keepAlive=setTimeout(()=>{},2000);try{await service.scheduled(actor,'synthetic bounded task','same-execution',AbortSignal.timeout(5000));const count=calls;await service.scheduled(actor,'synthetic bounded task','same-execution',AbortSignal.timeout(5000));assert.equal(calls,count)}finally{clearTimeout(keepAlive)}
})
