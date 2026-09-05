# Contributing

Start with an issue describing the user-visible outcome, scope, acceptance evidence and owner. Keep one outcome per PR. Epics coordinate dependencies and retain links to every child issue; an unchecked task is not complete because a PR exists.

## Working loop

1. Reconcile the issue, open PRs and worktrees. Pick one dependency-ready issue and assign ownership.
2. Use a short-lived issue-linked branch and a dispatch worktree. Keep the main checkout clean.
3. Implement and document behavior, including limitations and migration implications. Preserve unrelated changes.
4. Run meaningful affected tests, lint and build checks. Verify installation and CLI behavior from outside the source checkout when relevant.
5. Obtain independent review of the exact head. AI-authored changes require review from a different model family, with model identity, head SHA, findings and adjudication recorded. A planning discussion is not a code review.
6. Wait for CI Gate and Independent review to pass on that same head, then squash-merge. A changed head requires renewed review. Never bypass protection or manufacture a success status.
7. Verify the merge and issue acceptance, remove the clean merged worktree, delete its branch, prune remote refs and update the epic. Preserve uncommitted work; do not force-remove dirty worktrees.

A task is done only after documentation, implementation, validation, independent review, merge and cleanup are verified. Release tasks additionally require published artifacts and public installation proof.

## Evidence and reproducibility

Commit source code, safe configuration examples, concise reports and evidence manifests. Store large evidence in versioned downloadable archives, recording hashes and durable links. Preserve failed attempts and separate retries; never tune against the scored set, silently drop questions or select only successful runs. Raw evidence must be checked for secrets and redistribution rights before publication. Never put grading keys in the candidate-accessible MCP corpus.

## Review status

An accountable maintainer publishes the `Independent review` commit status only after inspecting a recorded independent verdict for the exact commit and resolving material findings. The status links to that evidence. This is a maintainer trust boundary, not cryptographic proof of reviewer independence: write-access maintainers can publish commit statuses. CI does not approve its own changes. A separate GitHub reviewer identity may additionally submit a native PR review.

## Maintenance

Every open issue and PR needs an owner and next action. Reconcile the milestone at each delivery-loop boundary; do not use inactivity bots to close unresolved work. Dependency updates go through normal CI and review. Report vulnerabilities privately using the repository Security tab.
