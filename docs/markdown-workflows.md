# Markdown workflows

A Markdown workflow is the use-case-owned source for one large business path.
It contains exactly one fenced block named `yaml workflow`. Its `data` map
contains authoring sample values; explanations and recovered business
knowledge remain ordinary Markdown.

```text
large-path Markdown -> compile conditions/templates -> Playwright Authoring Run
                    -> parameterized TestAsset -> standalone pytest

production WebTool -> select published pytest -> pass plaintext input JSON
```

The supported schema is `web_e2e_workflow.v1`. Required keys are `name`,
`start_url`, `goal`, `data`, and `steps`. Optional `rules` use deliberately
small conditions: `data.field == value`, `!=`, `in`, and `not in`. A matching
rule can contribute `remember` context and additional ordered `steps`. Join
predicates with `and` when a stage depends on more than one form value.

Use `${data.field}` for authoring values. Studio converts those references to
`${INPUT:field}` in the recorded asset while resolving them to the sample value
during the authoring browser run. Exported pytest reads the actual run values
from `WEB_E2E_INPUT_PATH`. `${ENV:NAME}` remains available for authoring-only
host values when a use case needs it.

Common commands:

```powershell
web-e2e-studio workflow compile examples/basic_navigation.workflow.md
web-e2e-studio workflow update examples/basic_navigation.workflow.md --set expected_heading="Example Domain"
web-e2e-studio workflow run examples/basic_navigation.workflow.md --output-dir recordings
```

The Desktop bridge also exposes `list_workflows`, `load_workflow`, and
`update_workflow_data` for authoring tools. Production business pages consume
only the generated Python and never update a shared Markdown file.

Domain-specific templates are independent use-case assets. The generic runtime
does not import them or recognize their environment-variable names, business
types, or rules.
