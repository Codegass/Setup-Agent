# Project-user image layer

`Dockerfile` adds one thin layer on the pinned benchmark image
`sag-advisor-five-cold:20260926`
(`sha256:a2d28950852af85d9b64d0e4ee015d989e88e83d9beb71c8ac5a166c41b8e1cc`) and is
tagged `sag-advisor-five-cold-user:20261021`.

## Why

In the 52-project run, commons-io, commons-vfs and xmlgraphics-batik failed only
because their tests rely on `File.setReadOnly()` or `chmod`, and the container
ran everything as root, where file permissions are not enforced. Official CI
runs as an ordinary user. With `SAG_PROJECT_USER=sag`, SAG runs the project's own
commands (Maven, Gradle, pip, pytest and the bash tool) as `sag` and keeps root
for its own control work (apt, alternatives, receipts, probes). Unset, SAG runs
everything as root exactly as before, and this layer changes nothing it uses.

## What the layer adds

- The account `sag`, home `/home/sag`. Uid 1000 if it and gid 1000 are free;
  otherwise the next uid whose user and group numbers are both free (Ubuntu
  images usually already have `ubuntu` at 1000).
- `sudo` with a passwordless rule for `sag` (`/etc/sudoers.d/sag`), so the
  model's shell can still install system packages.
- `/usr/bin/setpriv` (util-linux; installed only if the base lacks it), which
  the orchestrator uses to drop to `sag`.
- `/opt/sag-maven-observations`, mode 1777: the Maven witness writes its
  observations there from inside the project's Maven process.
- `git config --system safe.directory '*'`, so root's harness git calls can read
  a checkout `sag` owns.

The witness files (`/etc/mavenrc`, `/opt/sag-maven-witness/*`) are not touched;
the build script checks that every `policy.json` is byte-identical to the base.

## Build

```sh
scripts/build_project_user_image.sh            # tag sag-advisor-five-cold-user:20261021
```

The script refuses to build while any container is running or while the
campaign status file (`SAG_CAMPAIGN_STATUS`) says `running`, checks that the base
tag still resolves to the pinned id, builds for `linux/amd64`, then checks inside
the new image: the `sag` account, `setpriv`, `sudo -n true` as `sag`, the
observation directory mode, `safe.directory`, that `sag` can read the witness
files, and the `policy.json` hashes against the base.

## Recorded build

Not built yet.
