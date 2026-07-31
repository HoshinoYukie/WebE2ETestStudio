# Development

## Quality checks

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\ruff.exe check src tests
```

In restricted environments, point pytest temporary files at a writable
directory with `--basetemp`.

## Browser checks

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.playwright-browsers"
.\.venv\Scripts\python.exe -m playwright install chromium
$env:RUN_BROWSER_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest -m browser
```

## Source layout

```text
src/web_e2e_test_studio/
  agent/       action schemas, VLM adapter, bounded authoring loop, verifier
  browser/     Playwright lifecycle, observation, locator and action execution
  desktop/     UI-independent engine, worker controller, native Tkinter shell
  exporters/   deterministic plain-pytest code generation
  recording/   manual event capture and verification-gated sessions
  storage/     versioned JSON asset persistence
  tools/       external tool registry, path policy and subprocess execution
  application.py
  cli.py
```

Behavior is tested at public seams. Add one failing behavior test before each
implementation slice, then make that slice pass without testing private helper
details.
