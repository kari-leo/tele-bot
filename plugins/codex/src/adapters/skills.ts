import {createHash,randomUUID} from 'node:crypto'
import {readFile,realpath,readdir,lstat,mkdir,open,rename,rm} from 'node:fs/promises'
import path from 'node:path'
import type {SkillSnapshot} from '../domain/coding.js'
type Entry={relative:string;bytes:Buffer;hash:string}
/** Explicit selected skills only. Never edits the original .codex or copies account files. */
export class RunSkills {
  constructor(private readonly directory='',private readonly names:readonly string[]=[]){}
  private async inventory(){
    if(!this.names.length)return
    if(!path.isAbsolute(this.directory)||this.names.length>20||this.names.some(name=>!/^[a-z0-9_-]{1,80}$/i.test(name)))throw new Error('invalid_selected_skills')
    const root=await realpath(this.directory),entries:Entry[]=[];let bytes=0
    const walk=async(relative:string)=>{
      const file=path.join(root,relative),info=await lstat(file)
      if(info.isSymbolicLink())throw new Error('skill_links_not_copied')
      if(info.isDirectory()){for(const row of(await readdir(file)).sort())await walk(path.join(relative,row));return}
      if(!info.isFile()||/(?:^|[\\/])(?:\.env(?:\..*)?|auth\.json|credentials|node_modules|\.git)(?:[\\/]|$)/i.test(relative))throw new Error('skill_account_or_dependency_file_refused')
      if(info.size>12*1024*1024||entries.length>=5000||(bytes+=info.size)>64*1024*1024)throw new Error('selected_skills_size_limit')
      const content=await readFile(file);entries.push({relative,bytes:content,hash:createHash('sha256').update(content).digest('hex')})
    }
    for(const name of [...new Set(this.names)].sort()){await walk(name);if(!entries.some(entry=>entry.relative===path.join(name,'SKILL.md')))throw new Error('selected_skill_manifest_required')}
    const digest=createHash('sha256').update(JSON.stringify(entries.map(({relative,hash})=>[relative.replaceAll('\\','/'),hash]))).digest('hex')
    return{snapshot:{source:root,names:[...new Set(this.names)].sort(),digest} satisfies SkillSnapshot,entries}
  }
  async snapshot(){return(await this.inventory())?.snapshot}
  async prepare(cwd:string,expected:SkillSnapshot|undefined){
    const value=await this.inventory()
    if(JSON.stringify(value?.snapshot)!==JSON.stringify(expected))throw new Error('selected_skills_changed_new_confirmation_required')
    if(!value)return
    const root=await realpath(cwd),container=path.join(root,'.telebot-skills')
    await mkdir(container,{recursive:true,mode:0o700});if(await realpath(container)!==container)throw new Error('skill_copy_container_redirected')
    const destination=path.join(container,value.snapshot.digest)
    try{
      const saved=JSON.parse(await readFile(path.join(destination,'manifest.json'),'utf8'));if(saved.digest!==value.snapshot.digest)throw new Error('skill_copy_manifest_mismatch')
      for(const entry of value.entries){const file=path.join(destination,entry.relative);if(await realpath(file)!==file||createHash('sha256').update(await readFile(file)).digest('hex')!==entry.hash)throw new Error('skill_copy_program_changed')}
      return destination
    }catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error}
    const temporary=path.join(container,'.preparing-'+randomUUID());await mkdir(temporary)
    try{
    for(const entry of value.entries){const file=path.join(temporary,entry.relative);await mkdir(path.dirname(file),{recursive:true});const handle=await open(file,'wx',0o600);try{await handle.writeFile(entry.bytes)}finally{await handle.close()}}
    const manifest=await open(path.join(temporary,'manifest.json'),'wx',0o600);try{await manifest.writeFile(JSON.stringify(value.snapshot));await manifest.sync()}finally{await manifest.close()}
    await rename(temporary,destination);return destination
    }finally{if(path.dirname(temporary)!==container||!path.basename(temporary).startsWith('.preparing-'))throw new Error('invalid_skill_staging_cleanup');await rm(temporary,{recursive:true,force:true})}
  }
}
