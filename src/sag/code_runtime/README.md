# Optional code runtime

`SAG_CODE_MODE=true` adds a JavaScript orchestration tool. It is off by default.
The current implementation keeps native tools available. The proposed three-way
routing experiment is a separate design, not an implemented feature.

Scripts run in a QuickJS sandbox with the tool capabilities supplied by SAG.
Child calls use the existing Python dispatcher, validation, execution receipts
and shared call budget. Script output cannot certify build or test success.
Phase, report, advisor and context-control tools cannot be called inside scripts.

## Container prerequisites

The Python wheel includes the worker and pinned source, but does not install Node
or the npm dependency in a project container. Prepare a custom SAG image with:

- Node at `/opt/sag-code-mode/node/bin/node`. The experiment used Node **22.23.3**;
  the pinned source requires Node 22.19 or later with native TypeScript support.
- This directory's contents at `/opt/sag-code-mode/runtime/`.
- Dependencies installed there with `npm ci --ignore-scripts`.

Do not enable code mode with an image that lacks these files. The stock image is
not provisioned by this flag. The worker receives no provider credentials.

## Local regression tests

With the same Node version on `PATH`, run from the repository root:

```sh
npm ci --ignore-scripts --prefix src/sag/code_runtime
PYTHONPATH=. uv run python -m pytest -q tests/test_code_mode.py
```

These tests run the real worker and Python dispatch with fake project I/O. They
do not call a model or prove that a model will choose code mode. CI installs the
pinned Node version and executes these tests as part of the Python suite.

## Provenance

`pinned-source/source-lock.json` records the Pi repository commit and source
hashes. Its MIT license is retained in `pinned-source/LICENSE`.
`package-lock.json` pins `quickjs-wasi` and its integrity hash. Keep generated
`node_modules` out of version control.
