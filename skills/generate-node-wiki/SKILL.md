---
name: generate-node-wiki
version: 6
workflow: workflow://wiki-node-production@6
input_schema: wiki-production-request-v1
policy: wiki-production-v3
---

Thin entry point: validate node IDs and request a persistent `wiki-node-production` job. It never asks an Agent to interpret this Markdown, choose scripts, poll a process, or decide retries. Version 6 routes executable actions and runtime profiles through the Workflow; every long stage is owned by the non-polling stage supervisor, which enforces the 100-model-call/zero-compaction budget and writes a CAS checkpoint on every terminal outcome. Narrative composition and quantitative table collection remain separate evidence streams. Table values require a frozen reference configuration, verified source identity, an explicit-gap policy, Table Population Gate, and hash-locked apply.
