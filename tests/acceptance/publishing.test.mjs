import test from 'node:test'
import assert from 'node:assert/strict'
import {mkdir,writeFile,readFile,symlink} from 'node:fs/promises'
import path from 'node:path'
import {createHash} from 'node:crypto'
import {execFile} from 'node:child_process'
import {promisify} from 'node:util'
import {PublishingService} from '../../plugins/publishing/lib/adapters/service.js'
import {temporaryDirectory,cleanup} from '../support/temp-dir.mjs'
test('P5 reports are new-only, scope bound and hash verified; source publication preserves body and freezes all files before one-use confirmation',async t=>{
  const directory=await temporaryDirectory(t),posts=path.join(directory,'posts');await mkdir(posts)
  const actor={userId:'alice',chatId:'chat',sessionId:'session',cwd:directory,origin:'feishu',privateChat:true},service=new PublishingService(path.join(directory,'publishing.sqlite'),{adminUserIds:['alice'],allowedRoots:[directory],postsDirectory:posts,repoRoot:'',remote:'origin',branch:'main',maxFileBytes:1600000},{run:async()=>{throw new Error('must_not_run_git')}});cleanup(t,()=>service.close())
  const result=await service.report(actor,{filename:'new.md',content:'合成研究报告'},'one');assert.equal(result.artifacts[0].sha256,createHash('sha256').update(await readFile(result.artifacts[0].path)).digest('hex'))
  await assert.rejects(service.report(actor,{filename:'new.md',content:'overwrite'},'two'),/no_overwrite/)
  await assert.rejects(service.report(actor,{filename:'../escape.md',content:'x'},'three'),/relative/)
  const body='第一行\r\n  leading and trailing  \r\n\n',source='---\r\nold: ignored\r\n---\r\n'+body;await writeFile(path.join(directory,'source.md'),source)
  const item={source:'source.md',slug:'exact-source',title:'合成标题',description:'说明',published:'2026-10-31'},signal=AbortSignal.timeout(5000),propose=async()=>JSON.parse((await service.command(actor,'publish '+JSON.stringify({items:[item]}),signal)).text)
  let p=await propose();await assert.rejects(service.command({...actor,userId:'mallory'},'confirm '+p.token,signal),/authorized/)
  await writeFile(path.join(directory,'source.md'),source+'changed');await assert.rejects(service.command(actor,'confirm '+p.token,signal),/source_changed/)
  await writeFile(path.join(directory,'source.md'),source);p=await propose();await service.command(actor,'confirm '+p.token,signal);assert((await readFile(path.join(posts,'exact-source.md'),'utf8')).endsWith(body))
  await service.command(actor,'confirm '+p.token,signal);assert.equal(service.pending(actor).some(row=>row.token===p.token),false)
  p=JSON.parse((await service.command(actor,'files '+JSON.stringify({action:'replace',source:'new.md',content:'new frozen content'}),signal)).text)
  assert.equal(p.mutation.content,'new frozen content');await service.command(actor,'confirm '+p.token,signal);assert.equal(await readFile(path.join(directory,'new.md'),'utf8'),'new frozen content')
  p=JSON.parse((await service.command(actor,'files '+JSON.stringify({action:'move',source:'new.md',destination:'moved.md'}),signal)).text);await service.command(actor,'confirm '+p.token,signal);assert.equal(await readFile(path.join(directory,'moved.md'),'utf8'),'new frozen content')
  p=JSON.parse((await service.command(actor,'files '+JSON.stringify({action:'quarantine',source:'moved.md'}),signal)).text);await service.command(actor,'confirm '+p.token,signal);assert.equal(await readFile(p.mutation.destination,'utf8'),'new frozen content');await assert.rejects(readFile(path.join(directory,'moved.md')),/ENOENT/)
})
test('P5 actual local Git commits/pushes only frozen specific files, rejects staging and stale hashes, and consumes a push grant once',async t=>{
  const directory=await temporaryDirectory(t),repo=path.join(directory,'repo'),bare=path.join(directory,'bare.git');await mkdir(repo)
  const exec=promisify(execFile),git=async(args,cwd=repo)=>(await exec('git',args,{cwd,windowsHide:true,maxBuffer:1048576})).stdout
  await git(['init','--initial-branch=main']);await git(['config','user.name','Telebot fixture']);await git(['config','user.email','test@example.invalid']);await git(['init','--bare',bare])
  await writeFile(path.join(repo,'chosen.md'),'initial');await writeFile(path.join(repo,'unrelated.md'),'untouched');await git(['add','.']);await git(['commit','-m','fixture']);await git(['remote','add','origin',bare])
  const actor={userId:'alice',chatId:'test',sessionId:'s',cwd:repo,privateChat:true,origin:'feishu'},service=new PublishingService(path.join(directory,'git.sqlite'),{adminUserIds:['alice'],allowedRoots:[directory],postsDirectory:'',repoRoot:repo,remote:'origin',branch:'main',maxFileBytes:1600000},{run:(args,cwd)=>git(args,cwd)});cleanup(t,()=>service.close());const signal=AbortSignal.timeout(10000),request='git '+JSON.stringify({files:['chosen.md'],message:'specific file'})
  await writeFile(path.join(repo,'chosen.md'),'frozen');await writeFile(path.join(repo,'unrelated.md'),'must remain uncommitted');await git(['add','unrelated.md']);await assert.rejects(service.command(actor,request,signal),/staging_conflict/);await git(['reset','--','unrelated.md'])
  let p=JSON.parse((await service.command(actor,request,signal)).text);await writeFile(path.join(repo,'chosen.md'),'changed');await assert.rejects(service.command(actor,'confirm '+p.token,signal),/file_changed/)
  p=JSON.parse((await service.command(actor,request,signal)).text);const result=await service.command(actor,'confirm '+p.token,signal);assert.equal(result.status,'completed');const head=(await git(['rev-parse','HEAD'])).trim();await service.command(actor,'confirm '+p.token,signal);assert.equal((await git(['rev-parse','HEAD'])).trim(),head)
  assert.equal((await git(['show','HEAD:unrelated.md'])).trim(),'untouched');assert.equal((await git(['--git-dir',bare,'rev-parse','refs/heads/main'],directory)).trim(),head);assert((await git(['status','--porcelain'])).includes('unrelated.md'))
})
