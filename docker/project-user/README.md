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

## Recorded build (2026-10-21)

Built with `scripts/build_project_user_image.sh` on Docker 29.8.2 (arm64 host,
`linux/amd64` image), base tag verified as the pinned id.

- Tag: `sag-advisor-five-cold-user:20261021`
- Image id: `sha256:8e99d20a3736c28cb98fbf54696fa780deb6bb88f04d485f4cd6a02b38e128d6`
- Account: `sag`, uid 1001, gid 1001, home `/home/sag` (uid/gid 1000 is the
  base image's `ubuntu` user).
- `sudo` 1.9.15p5-3ubuntu5.24.04.4 installed; `sudo -n true` as `sag` succeeds.
  `/usr/bin/setpriv` was already present, so util-linux was not reinstalled.
- `/opt/sag-maven-observations` mode 1777; `git config --system --get-all
  safe.directory` prints `*`; `sag` can read `/etc/mavenrc` and every file in
  `/opt/sag-maven-witness`.

Witness files, sha256, identical in the base and the new image:

| File | sha256 |
| --- | --- |
| `/opt/sag-maven-witness/policy.json` (the only `policy.json` under `/opt` and `/etc`) | `b28689f27be99ca995b43e303b437f39b48a125a21eedbe7b0041819fb96e7e5` |
| `/etc/mavenrc` | `c2dac373e1481265665f063eacc26f8144af9f344255354e45b3740d12ddc702` |
| `/opt/sag-maven-witness/launch.py` | `981fa89bd0a5963cb9b8c7f8b0bf27ceec989a119767886a57fdacf5eb525b89` |
| `/opt/sag-maven-witness/witness.jar` | `260ec37d2aa1f016c2441f7e946c8abffad71c234ed2129e8ec9098530ff4f35` |

Rootfs layers: the base image's 17 layers, unchanged and in order (last base
layer `sha256:42deea506ee9461dcf9cea8b4671cbe0648418ccb1a9b719522f9c88cd6d2c04`),
followed by four new ones:

1. `sha256:200a96fd506ee84144a8ea91c8f3c24d0bd109b265ee1e4f8926536009dbf358` (account)
2. `sha256:2ceb473ba5463a70aad0082395497592a5b0ccee576919d63511bb5fc3bf711a` (sudo, sudoers rule)
3. `sha256:5e4bbb1ec303ef4d40f085ca35c73b14f937af366aaa33f51f5cfc2810f4ad0c` (observation directory)
4. `sha256:20547ecf7c67c5d64b69526b3605bbba3658e9c3fe1f9078646370e37391cb08` (`safe.directory`)

Note: the sudo layer also refreshed the apt package lists (`apt-get update`).
SAG runs `apt-get update` in every new container anyway.
