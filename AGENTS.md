# Agent working contract

- This repository evaluates Ukrainian language only. Keep LU fleet/service dependencies out of the public package.
- Follow CONTRIBUTING.md and issue acceptance. One accountable lead; bounded workers own disjoint paths and preserve others' changes.
- Use `.worktrees/dispatch/<agent>/<task>/` and issue-linked branches for edits. Keep the primary checkout clean. Use `.venv/bin/python` for project commands after environment creation.
- Every commit includes `X-Agent: <agent>/<task>` attribution. Do not copy credentials, private infrastructure settings, grading keys or raw execution evidence into Git.
- Paid calls need an explicit authorized route and bounded budget. Never silently substitute a model, effort, provider or tool surface.
- Tests and exact-head independent cross-family review precede merging. The lead owns merge and release; workers do not merge or arm auto-merge.
- Preserve all evaluation attempts. Unknown effective effort remains unknown. Live MCP results disclose drift and contamination limitations.
- Done means documented, implemented, tested, independently reviewed, merged and cleaned. Release requires published artifacts and clean-install proof. Blocked work retains evidence, owner and next action.
