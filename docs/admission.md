# Trusted live admission integrations

The admission modules implement a controller boundary for user-owned provider
probes. They are not yet wired into the research scheduler/CLI. Issue #3 must
finish that integration before primary live experiments. An echo callback or
constant-hash fixture is not a valid inaugural provider probe.

## Command contract

`admission_command` accepts only an explicitly supplied trusted command spec
with schema `ukrainian-llm-eval.admission-command.v1` and runtime
`python-script-v1`. Do not discover or execute commands from downloaded
benchmark data, manifests, or result archives.

The spec declares absolute executable/script paths, a runtime lock and optional
dependency files. Every declared file has a byte SHA-256. The implementation
opens and verifies the files, copies those verified bytes into a private
snapshot, and executes the copied Python interpreter and script directly.
Arguments are not shell commands. Only explicitly named environment variables
are passed; code-loading environment variables are rejected.

Each spec supplies time and stdin/stdout/stderr byte limits within defensive
protocol ceilings. Timeout and output overflow kill the process group. Failure
results contain normalized status, byte counts and hashes, with no raw streams.
Successful stdout is passed to the strict claims validator. The probe receives
safe requirements and identity information, not packet text, grading keys,
references, previous responses or reservation amounts.

Identity covers the declared files. It does not freeze shared libraries, the
standard library, the operating system or undeclared imports. The command is a
trusted integration, not a sandbox for hostile programs. Unsupported runtime
shapes fail closed. A probe must use real read-only provider sources to support
its claims; file hashes alone cannot prove those claims true.

## Fresh claims and authorization

`admission.build_admission_request` creates a fresh nonce and canonical request
hash. The response schema `ukrainian-llm-eval.admission-result.v1` binds that
nonce and hash, an observation time, and exactly three records: `pricing`,
`entitlement` and `capability`. Unknown or additional fields are rejected.

Each record separates frozen `state`/`state_sha256` from fresh `observed`
claims. Rates, account/route identity, entitlement validity and supported
limits stay bound to the plan. Quota or credit balances can change between
segments and are checked anew. Timestamps must be within the request's fresh
observation window; entitlement expiry is checked separately.

The controller independently computes conservative token/tool cost using
integer micro-USD and compares the observed quote against that arithmetic,
the frozen reservation and remaining ceiling. Existing credit must cover the
quoted provider charge while incremental spend remains zero. Unknown pricing,
unknown fit, unhealthy routes, insufficient credit, changed identity or limits,
expired entitlement and unauthorized paid cost all fail closed.

Operator authorization is separate from entitlement. Its strict record binds
the route, whether paid execution is allowed and a new-spend ceiling; the plan
must pin its canonical hash. An account subscription or balance is not itself
permission to spend. Integration must enforce the authorized totals across the
entire frozen schedule as well as each segment.

`invoke_validated_admission` retains the safe request and command result in a
private append-only evidence store. Accepted claims and their full response
hash are preserved. Rejected output is retained only as counts, hashes and a
normalized failure, avoiding arbitrary credential-bearing streams. Interrupted
probe attempts remain visible rather than being silently discarded.

## Remaining integration gates

The scheduler must bind the command/composite identity, operator authorization,
stable state hashes and fresh admission receipt to each segment's execution
receipt. The public run command must use this validated path. The inaugural
routes still require real pricing, entitlement, context/output and tool-control
probes; mock subprocess tests establish controller behavior only. Independent
review and installed end-to-end proof remain required before release.
