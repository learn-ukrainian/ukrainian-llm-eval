# Release and recovery

A merge is not a release. The first release is blocked until the milestone's required capabilities and evaluation evidence are complete.

1. Reconcile issues, source/data licenses, security findings and evidence archives. Record unresolved limitations explicitly; material acceptance gaps block release.
2. Require CI and an independent review for the exact source commit. Build wheel and source distribution from that commit using recorded dependency versions.
3. Install the wheel in a fresh environment outside the checkout. Exercise documented offline examples and verify the published evaluation against its immutable manifests. Run credentialed model canaries only under explicit experiment authorization.
4. Produce a changelog, source SHA, artifact SHA-256 checksums and a benchmark report listing all configured cells and failures/exclusions. Verify archive links and permissions; never publish raw secrets or grading keys accidentally.
5. Create a version tag on the reviewed main commit and a GitHub release with the tested artifacts and notes. Do not rebuild different artifacts after verification. PyPI publishing is not implied; it requires a separately configured publisher.
6. Download the public release artifacts and verify checksums and installation again. Close the release issue only after this public proof and merged-worktree/branch cleanup.

If a release is defective, publish a corrective patch release and mark the previous release's limitation in its notes. Never move an existing version tag or replace evidence to conceal a failure. For a secret leak, revoke credentials immediately and coordinate incident response before attempting history cleanup. An unavailable evidence host is a storage incident: restore/migrate the archive, verify its original checksum and update the manifest location without changing the evidence identity.
