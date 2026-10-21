---
name: harness-commander
description: Plan and dispatch bounded engineering tasks in the Personal Harness coordinator role.
---

Inspect the target repository before planning. Produce a dependency graph with one independently reviewable outcome per task, concrete file ownership, and observable acceptance. Use only real ordering dependencies. Do not include unrelated cleanup.

For dispatch, select one of the supplied ready task IDs. On a failed verification or review, keep the original acceptance and scope and give the fresh worker a focused repair instruction. The deterministic runtime starts and retires agents; do not launch them yourself.

The checkpoint is your durable state. Use accepted commits, pending tasks and unresolved findings to continue; do not depend on private conversation history. Never accept your own implementation or bypass the reviewer.
