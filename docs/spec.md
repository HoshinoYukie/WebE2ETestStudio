# VLM-assisted Web E2E authoring — implementation specification

## Goal

Build a Python authoring framework in this repository that uses pytest as the
test format and Playwright as the browser driver. During authoring it may use a
VLM Agent and may execute explicitly registered external tools. Its primary
output is deterministic pytest code that runs without any model.

## Required capabilities

1. Start a Playwright browser and capture user browser operations as structured
   action candidates.
2. Give a VLM the current screenshot, DOM-oriented observation, action schemas,
   and prior execution feedback; accept only structured registered actions.
3. Execute browser and external-tool actions through deterministic adapters.
4. Re-observe after execution and keep only verified actions in the portable
   test asset. Failed probes and retries remain audit events, not test steps.
5. Persist and reload a versioned, human-inspectable asset snapshot.
6. Export a plain pytest file based on the Playwright `page` fixture. The file
   must not import this project, OpenAI, or another model SDK.
7. Preserve external-tool operations in exported tests as explicit
   `subprocess.run(..., shell=False)` calls with bounded timeouts.
8. Never persist recorded password values. Export environment-variable
   references for secrets.
9. Provide CLI entry points for recording/authoring, asset export, inspection,
   and replay or validation where applicable.

## Acceptance criteria

- A rejected or failed candidate never appears in `TestAsset.steps`.
- The Agent can use execution feedback to replan and commit a later verified
  action.
- Manual click/input/change/navigation events compile to Playwright-oriented
  actions; repeated input noise is coalesced.
- External paths outside the workspace require explicit authorization and
  registered commands never use a shell string.
- Exported Python parses with `ast.parse`, is collectable by pytest, and has no
  model or Studio dependency.
- Unit tests cover the four public seams: authoring Agent, recording/storage,
  external tools, and pytest export.
- A browser smoke test demonstrates that a generated test can run against a
  local page without a model, when Chromium is available.

## Non-goals for the first release

- Recording arbitrary internal clicks inside native desktop applications.
  External GUI programs are represented as coarse-grained registered tool
  operations unless a future desktop adapter is installed.
- Letting a model execute arbitrary Python, JavaScript, PowerShell, or shell
  text.
- Treating the Studio UI or a YAML file as the product's source of truth.

