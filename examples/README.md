# 🧠 IOA Observe Examples

This repository contains example scripts showcasing how to use agents, tools, and workflows with:

- [LlamaIndex](https://github.com/jerryjliu/llama_index)
- [LangGraph](https://github.com/langchain-ai/langgraph)
- [`ioa_observe` SDK](https://pypi.org/project/ioa-observe-sdk/) for observability and tracing

These examples are designed to help you understand how to build, trace, and debug multi-agent systems.

---

## 📦 Installation

Make sure you have Python 3.8+ installed. Then:

```bash
pip install -r requirements.txt.txt
```

> **Note:** Some examples may require additional dependencies — check the import statements in each script.

---

## 🔧 Environment Variables

Set the following environment variables before running the examples:

```bash
export OPENAI_API_KEY=<your_openai_api_key>
export OTLP_HTTP_ENDPOINT=<your_otel_exporter_endpoint>
export TAVILY_API_KEY=<your_tavily_api_key>  # Optional, for web search tool used in langgraph example
```

---

### 🚀 How to Run

After installing the dependencies and setting environment variables, run an example with:

```bash
python <filename.py>
```

---

### 🗂️ Example Files

<details>
<summary><strong>agent.py</strong></summary>

**Highlight:**  
Shows how to create a simple agent using the OpenAI API and the `ioa_observe` SDK.

**Speciality:**
- Demonstrates agent and tool decorators  
- Translates a joke to "pirate" English and fetches history jokes  
- Tracing is enabled for observability

</details>

---

<details>
<summary><strong>llama-index-example.py</strong></summary>

**Highlight:**  
Demonstrates a math agent using LlamaIndex's agent workflow and tools.

**Speciality:**
- Uses LlamaIndex's `AgentWorkflow` with custom tools for addition and multiplication  
- Shows how to set up callback managers for debugging and tracing  
- Tracing is enabled for observability

</details>

---

<details>
<summary><strong>llama-index-multi-agent-example.py</strong></summary>

**Highlight:**  
Shows a multi-agent workflow using LlamaIndex, with explicit agent handoff.

**Speciality:**
- Defines two agents, each with their own tool and role  
- Demonstrates agent-to-agent handoff and multi-step processing  
- Tracing is enabled for observability

</details>

---

<details>
<summary><strong>langgraph_multi_agent.py</strong></summary>

**Highlight:**  
Implements a multi-agent workflow using LangGraph, with a supervisor agent managing researcher and coder agents.

**Speciality:**
- Uses LangGraph's stateful graph to coordinate agents  
- Researcher agent uses a search tool; coder agent can execute Python code  
- Supervisor agent decides workflow progression  
- Tracing is enabled for observability

</details>

---

<details>
<summary><strong>test.py</strong></summary>

**Highlight:**  
Shows a simple workflow using LlamaIndex to generate and critique a joke.

**Speciality:**
- Demonstrates step-based workflow with LlamaIndex  
- Uses an LLM to generate a joke and then critique it  
- Tracing is enabled for observability

</details>

---

Check out the `remote_agent_slim` and `remote_agent_http` directories for examples of remote agents using SLIM and HTTP protocols, respectively.
