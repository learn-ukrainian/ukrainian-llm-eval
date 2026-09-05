# Security

The first release is in development. No stable version is currently supported.

Report vulnerabilities privately through GitHub's **Security → Report a vulnerability** for this repository. Do not include credentials, private endpoint details or raw research transcripts in public issues.

Treat provider CLIs, models, tool outputs and datasets as untrusted inputs. The evaluator must separate candidate access from grading keys, constrain allowed tools, enforce budgets, reject identity/configuration drift and retain failures. Harness tool restrictions are not an operating-system sandbox against a malicious CLI or provider. Run untrusted executables on a separate host without grading keys.

Credentials belong in private environment variables or native credential stores. Use separate public-safe exports and permission-restricted raw evidence. Inspect raw archives before publication; deletion from a later commit does not erase a leaked secret.

GitHub workflows use minimal permissions and immutable action references. Public fork tests receive no provider credentials and never run scored provider calls. Main requires PRs, CI and independent review, and blocks force pushes and deletion.

CodeQL errors and high/critical security findings block main through its ruleset. Lower-severity findings require review and an explicit disposition.
