"""GitHub: what a deployment changed, as evidence for investigations.

Read-only. A project admin stores a fine-grained personal access token (encrypted) and maps
services to repositories. When an incident is investigated, the worker asks GitHub what
changed between a candidate deployment's commit and the previous one, and adds commit
messages and file names (never file contents) to the evidence.

- ``client``: the GitHub REST calls, with bounded results and one error type.
- ``models`` / ``service`` / ``router``: connecting, mapping repositories, disconnecting.
- ``evidence``: turns code changes into evidence items for the investigation worker.
"""
