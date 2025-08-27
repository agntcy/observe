import random
from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.utils import new_agent_text_message
from pydantic import BaseModel

from ioa_observe.sdk.decorators import agent


@agent(name="random_number_generator")
class RandomNumberAgent(BaseModel):
    """Generates a random number between 1 and 100"""

    async def invoke(self) -> str:
        number = random.randint(1, 100)
        print(f"Generated random number: {number}")
        return f"Random number generated: {number}"


class RandomNumberAgentExecutor(AgentExecutor):
    def __init__(self):
        self.agent = RandomNumberAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        result = await self.agent.invoke()
        await event_queue.enqueue_event(new_agent_text_message(result))

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        raise Exception("Cancel not supported")
