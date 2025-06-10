import os

from openai import OpenAI

from ioa_observe.sdk import Observe
from ioa_observe.sdk.decorators import agent, tool

Observe.init("agent_service_1", api_endpoint=os.getenv("OTLP_HTTP_ENDPOINT"))

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


@agent(name="joke_translation")
def translate_joke_to_pirate(joke: str):
    completion = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "user",
                "content": f"Translate the below joke to pirate-like english:\n\n{joke}",
            }
        ],
    )

    history_jokes_tool()

    return completion.choices[0].message.content


@tool(name="history_jokes")
def history_jokes_tool():
    completion = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "get some history jokes"}],
    )

    return completion.choices[0].message.content


print(translate_joke_to_pirate("Why did the chicken cross the road?"))
