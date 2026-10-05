"""AI-assisted investigation of an incident.

The model never fetches data and never acts. Code collects a bounded set of evidence, the
model reasons over it and must cite it, and code checks every citation before anything is
shown. Each run and each step is recorded.

- ``evidence``: collects and snapshots the evidence (no LLM).
- ``code_changes``: the interface for "what did a deployment change?" (GitHub implements it).
- ``llm``: the provider interface and the Gemini implementation.
- ``prompts``: what the model is told. Evidence is passed as clearly delimited data.
- ``schemas`` / ``validation``: the report format and the checks applied to model output.
- ``orchestrator``: the fixed sequence of steps for one investigation.
- ``service`` / ``worker``: queueing investigations and running them outside API requests.
"""
