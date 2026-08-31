# Architecture

## Runtime flow

The authoring runtime follows the deep-module seams used by Midscene Core:

```text
UIContext + user goal + previous ExecutionDump
                         |
                         v
               StructuredVLMPlanner
                         |
       PlanningResult(actions | ask | complete)
                         |
                         v
                   PlanCompiler
       validates the complete batch before effects
                         |
                         v
                 ExecutionRunner
          +--------------+--------------+
          |              |              |
       browser          HTTP       external tool
          +--------------+--------------+
                         |
          execute -> observe -> verify -> record
                         |
                         v
                  ExecutionDump
          +--------------+--------------+
          |                             |
   Studio event stream             TestAsset
                                         |
                                         v
                              deterministic Pytest
```

Markdown workflows add an upstream authoring and publication seam:

```text
large-path *.workflow.md -> condition/template compiler
          -> Playwright Authoring Run -> parameterized TestAsset
          -> standalone deterministic Pytest
```

Domain-specific knowledge lives in use-case-owned Markdown templates. The
compiler understands only the generic workflow schema, `${INPUT:path}`
bindings, and registered Action contracts. A consuming WebTool passes runtime
data directly to the exported Python; it does not call Studio.

Before a browser action is performed, `LocatorEngine` expands the requested
locator and its fallbacks with candidates derived from the current
`Observation`. It probes every candidate without side effects, rejects missing
or ambiguous matches, ranks stable strategies (`test_id`, accessible role,
label, placeholder, text), and returns both the selected locator and its
evidence. A verified step persists that selected locator while retaining the
original locator as an ordered fallback.

Manual browser events enter through `ExecutionRunner.ingest()`. They are
already performed, but use the same verification, recording, sanitization, and
dump persistence seam as Agent, HTTP, and external-tool actions.

## Strict planner protocol

`PlanningResult` has exactly three outcomes:

- `actions`: one or more ordered `PlannedAction` values;
- `ask`: one blocking user question and no actions;
- `complete`: a terminal summary and no actions.

The old free-form `decision`, `execute`, `wait`, and `done` values are rejected.
A wait is an ordinary registered browser action inside an `actions` plan.

The planner never invokes an adapter. `PlanCompiler` resolves every action
against `ActionRegistry` and validates channel, locator requirements, required
arguments, allowed arguments, and argument types before creating any
`ExecutionTask`.

## Execution and audit model

`ExecutionRunner` is device-independent. Its observer is optional, so API-only
and tool-only workflows receive an empty `Observation` without starting a
browser. A failed task stops the batch and every remaining task is emitted as
`cancelled`.

Every attempted task produces an `ExecutionRecord` containing:

- task identity, intent, and deterministic action;
- before and after observations;
- execution and verification results;
- status, timestamps, and committed step identity.

Each Action may define `timeout_seconds`. `ExecutionRunner` creates a
monotonic deadline and passes it together with the run-scoped
`CancellationToken` through `ActionRegistry`. Timeout and cancellation are
terminal, auditable outcomes: the active candidate is not committed and the
remaining batch is cancelled. Desktop cancellation bypasses the serialized
worker queue so it can signal an Action while that worker is blocked.

Adapters implement interruption at their deterministic boundary:

- Playwright clamps native operation timeouts and checks long waits in slices;
- HTTP closes the active response when cancellation is signalled;
- external tools use `Popen` and terminate/kill the child process;
- generated PyTest preserves Action deadlines using native timeout arguments.

Records form an `ExecutionDump`. The in-memory dump retains full diagnostic
data. The persisted form redacts common credential fields and truncates large
values before appending to `TestAsset.execution_history`.

Only verified actions become `RecordedStep` values and reach deterministic
Pytest export.

## Recoverable Desktop sessions

`TestAsset` remains the portable source of truth. Desktop process state is
stored separately in an atomic `<name>.run.json` `RunCheckpoint`. A checkpoint
is updated at session start, after every Agent event, after every save, on a
blocking question, on failure, and when the application suspends the session.
It contains no API key and redacts secret-shaped event fields.

On restart, the Desktop discovers workspace-scoped checkpoints. Resume loads
the existing asset, restores the last URL without recording another navigation,
retains the original instruction and pending question, and continues adding
verified steps. Discard removes only recovery state; generated assets and
PyTest files are preserved.

## Module seams

- `domain.py`: versioned model-independent test asset.
- `context.py`: navigation-safe UI capture, purpose-aware caching,
  freeze/unfreeze, tab/frame metadata, fingerprints, screenshots, and
  sensitive-content filtering.
- `browser/locator.py`: model-independent candidate generation, live probing,
  ranking, selection, and auditable locator evidence.
- `agent/planning.py`: strict plan types and whole-batch compiler.
- `agent/actions.py`: dynamic action contracts and executor registry.
- `agent/execution.py`: execution, verification, audit, cancellation, and
  sanitized dump persistence.
- `agent/loop.py`: bounded plan/execute/replan orchestration and structured
  progress events.
- `agent/planner.py`: the only model-facing core module.
- `capabilities.py`: built-in and entry-point capability adapters.
- `browser/`, `api/`, `tools/`: deterministic channel adapters.
- `files/`: built-in, read-only local-file extraction capabilities with
  explicit argument contracts and structured results.
- `recording/`: verification gate and manual-event ingestion.
- `exporters/`: one-way compilation to model-free Pytest.
- `desktop/`: UI-independent engine, serialized worker controller, and WebView
  shell.
- `desktop/recovery.py`: atomic checkpoint persistence, discovery, validation,
  redaction, resume metadata, and safe discard.

## Safety decisions

- Model output cannot invoke an unregistered action.
- The complete action batch is validated before its first side effect.
- Arbitrary executable source is not part of the action IR.
- External tools use argument vectors with `shell=False`.
- Event-rendering failures cannot interrupt deterministic execution.
- Persisted execution diagnostics redact common secrets and are size bounded.
- Exported Pytest contains no authoring runtime or model call.
