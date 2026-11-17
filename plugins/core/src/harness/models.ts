/** Keep model choices in the official Agent's durable selection projection. */
import type { Context } from '@deepseek-ai/cordis'
import type { Agent, ModelSelection } from '@deepseek-ai/dsh-agent'
export class HarnessModels {
  private readonly owned=new WeakSet<Agent>()
  async mount(ctx:Context,agent:Agent,initial:ModelSelection){
    const {installModelSelection}=await import('@deepseek-ai/dsh-agent')
    if(!agent.model.current())agent.model.select(initial)
    this.owned.add(agent)
    const dispose=installModelSelection(ctx);ctx.effect(()=>dispose)
  }
  current(agent:Agent):ModelSelection|undefined{
    return agent.model.current()
  }
  select(agent:Agent,value:ModelSelection){
    if(!this.owned.has(agent))throw new Error('conversation_model_owned_elsewhere')
    agent.model.select(value)
  }
}
