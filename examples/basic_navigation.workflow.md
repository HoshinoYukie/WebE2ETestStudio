# Basic navigation workflow

This product-generic example demonstrates the Markdown workflow boundary. It
contains no domain-specific rules or credentials.

```yaml workflow
schema: web_e2e_workflow.v1
name: basic_navigation
start_url: https://example.com
goal: Confirm that the requested page content is visible.
data:
  expected_heading: Example Domain
steps:
  - Open the start page.
  - Confirm that the heading ${data.expected_heading} is visible.
rules: []
```
