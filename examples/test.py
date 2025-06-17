# Copyright AGNTCY Contributors (https://github.com/agntcy)
# SPDX-License-Identifier: Apache-2.0

from llama_index.core.workflow import StopEvent, Workflow, StartEvent, step, Event
from llama_index.llms.openai import OpenAI

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, graph
from ioa_observe.sdk.tracing import session_start

Observe.init("joke_workflow", api_endpoint="http://localhost:4318")


class JokeEvent(Event):
    joke: str


@graph(name="joke_workflow")
@agent(name="joke_agent")
class JokeFlow(Workflow):
    llm = OpenAI(api_key="")

    @step
    async def generate_joke(self, ev: StartEvent) -> JokeEvent:
        topic = ev.topic

        prompt = f"Write your best joke about {topic}."
        response = await self.llm.acomplete(prompt)
        return JokeEvent(joke=str(response))

    @step
    async def critique_joke(self, ev: JokeEvent) -> StopEvent:
        joke = ev.joke

        prompt = f"Give a thorough analysis and critique of the following joke: {joke}"
        response = await self.llm.acomplete(prompt)
        return StopEvent(result=str(response))


async def main():
    workflow = JokeFlow(timeout=60, verbose=False)
    session_start() # Start a new session for tracing
    result = await workflow.run(topic="pirates")
    print(str(result))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
