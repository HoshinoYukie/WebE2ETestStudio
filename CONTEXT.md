# Domain Language

**Observation**  
A serializable browser snapshot: URL, title, viewport, interactive elements,
page text, and an optional screenshot reference.

**LocatorResolution**  
A deterministic decision containing the selected Playwright locator and every
probed candidate with its source, score, match count, visibility, enabled
state, viability, and rejection reason.

**ActionSpec**  
The registered name, channel, description, and argument contract for one
deterministic capability. The same specification is shown to the VLM and
validated before execution.

**Action**  
A structured invocation of an `ActionSpec`. It is data, never free-form code.
It may carry an authoring/replay `timeout_seconds` deadline.

**CancellationToken**
A thread-safe cooperative interruption signal shared by one Authoring Run.
Every active Action receives the token and its own monotonic deadline.

**StepCandidate**  
A manual, user, imported, or VLM-proposed action that has not passed the
verification gate.

**AttemptRecord**  
The audit record for an executed candidate. Failed and rejected attempts remain
visible here but are never exported.

**RecordedStep**  
A candidate whose deterministic execution succeeded and whose result was
verified after a fresh observation. Only recorded steps enter a `TestAsset`.

**TestAsset**  
The versioned, model-independent intermediate representation used for replay
and export.

**ExternalTool**  
A project-scoped executable resource with a stable ID, explicit entry point,
argument vector, environment bindings, timeout, and portability metadata.

**SpreadsheetExtraction**  
A built-in, read-only Action that opens `.xlsx` or `.xlsm` containers without
launching Excel, finds a queried header or value, and returns structured sheet,
cell, value, and match-origin evidence. It is distinct from an arbitrary
ExternalTool and requires both `file_path` and `query`.

**Authoring Run**  
One bounded `observe → plan → execute → re-observe → verify → record/replan`
loop for a user instruction.

**RunCheckpoint**  
A secret-safe, workspace-scoped snapshot of an unfinished Desktop authoring
session. It references the portable `TestAsset` and stores recovery-only state:
last URL, active instruction, pending question, round, event history, error,
configuration, and lifecycle state.

**Exported Test**  
A plain pytest + Playwright file compiled from a `TestAsset`. It must not import
the Studio, Agent, OpenAI, or any model runtime.
