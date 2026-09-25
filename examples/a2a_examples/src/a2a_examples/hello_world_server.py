import uvicorn

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)
from starlette.applications import Starlette
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.helpers import new_text_message
from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent
from ioa_observe.sdk.instrumentations.a2a import A2AInstrumentor


# --8<-- [start:HelloWorldAgent]
@agent(name="hello_world_generator")
class HelloWorldAgent:
    """Hello World Agent."""

    async def invoke(self) -> str:
        return "Hello World"


# --8<-- [start:HelloWorldAgentExecutor_init]
class HelloWorldAgentExecutor(AgentExecutor):
    """Test AgentProxy Implementation."""

    def __init__(self):
        self.agent = HelloWorldAgent()

    # --8<-- [end:HelloWorldAgentExecutor_init]
    # --8<-- [start:HelloWorldAgentExecutor_execute]
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        result = await self.agent.invoke()
        await event_queue.enqueue_event(new_text_message(result))

    # --8<-- [end:HelloWorldAgentExecutor_execute]

    # --8<-- [start:HelloWorldAgentExecutor_cancel]
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception("cancel not supported")


if __name__ == "__main__":
    Observe.init("helloworld_a2a_server", api_endpoint="http://localhost:4318")

    A2AInstrumentor().instrument()

    # --8<-- [start:AgentSkill]
    skill = AgentSkill(
        id="hello_world",
        name="Returns hello world",
        description="just returns hello world",
        tags=["hello world"],
        examples=["hi", "hello world"],
    )
    # --8<-- [end:AgentSkill]

    extended_skill = AgentSkill(
        id="super_hello_world",
        name="Returns a SUPER Hello World",
        description="A more enthusiastic greeting, only for authenticated users.",
        tags=["hello world", "super", "extended"],
        examples=["super hi", "give me a super hello"],
    )

    # --8<-- [start:AgentCard]
    # This will be the public-facing agent card
    public_agent_card = AgentCard(
        name="Hello World Agent",
        description="Just a hello world agent",
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="1.0",
                url="http://localhost:9999/a2a/jsonrpc",
            )
        ],
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True, extended_agent_card=True),
        skills=[skill],
    )
    # --8<-- [end:AgentCard]

    # This will be the authenticated extended agent card
    # It includes the additional 'extended_skill'
    specific_extended_agent_card = public_agent_card.model_copy(
        update={
            "name": "Hello World Agent - Extended Edition",  # Different name for clarity
            "description": "The full-featured hello world agent for authenticated users.",
            "version": "1.0.1",  # Could even be a different version
            # Capabilities and other fields like url, default_input_modes, default_output_modes,
            # supports_authenticated_extended_card are inherited from public_agent_card unless specified here.
            "skills": [
                skill,
                extended_skill,
            ],  # Both skills for the extended card
        }
    )

    request_handler = DefaultRequestHandler(
        agent_executor=HelloWorldAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=public_agent_card,
        extended_agent_card=specific_extended_agent_card,
    )

    app = Starlette(
        routes=[
            *create_agent_card_routes(public_agent_card),
            *create_jsonrpc_routes(request_handler, rpc_url="/a2a/jsonrpc"),
        ]
    )

    uvicorn.run(app, host="0.0.0.0", port=9999)
