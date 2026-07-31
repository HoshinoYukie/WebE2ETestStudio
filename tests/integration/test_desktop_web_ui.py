from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.mark.browser
def test_desktop_ui_starts_session_and_renders_bridge_event() -> None:
    if os.environ.get("RUN_BROWSER_TESTS") != "1":
        pytest.skip("set RUN_BROWSER_TESTS=1 after installing Playwright Chromium")

    from playwright.sync_api import sync_playwright

    page_path = (
        Path(__file__).parents[2]
        / "src"
        / "web_e2e_test_studio"
        / "desktop"
        / "web"
        / "index.html"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.add_init_script(
            """
            window.__studioCalls = [];
            window.__eventSent = false;
            window.__modelConfigured = false;
            window.pywebview = { api: {
              bootstrap: async () => ({
                workspace: "C:\\\\projects\\\\sample",
                model: {
                  configured: false, key_hint: "", model: "gpt-4o",
                  base_url: "", source: "", ignore_proxy: false
                },
                capabilities: [],
                recoverable_sessions: [{
                  name: "saved-flow", state: "suspended", round: 2,
                  checkpoint_path: "C:\\\\out\\\\saved-flow.run.json",
                  pending_question: "", last_url: "https://example.test",
                  goal: "Resume"
                }]
              }),
              get_model_settings: async () => ({
                configured: false, key_hint: "", model: "gpt-4o",
                base_url: "", source: "", ignore_proxy: false
              }),
              check_model: async () => window.__modelConfigured
                ? {available: true, model: "vision-model", message: "verified"}
                : {available: false, model: "gpt-4o", message: "not configured"},
              configure_model: async (config) => ({
                configured: true, key_hint: "desk…cret",
                model: (window.__modelConfigured = true, config.model),
                base_url: config.base_url,
                source: "Desktop session settings",
                ignore_proxy: config.ignore_proxy
              }),
              start_session: async (config) => {
                window.__studioCalls.push(["start_session", config]);
                return {accepted: true};
              },
              poll_events: async () => {
                if (!window.__studioCalls.length || window.__eventSent) return [];
                window.__eventSent = true;
                return [{
                  kind: "snapshot",
                  message: "Recording started",
                  details: [],
                  snapshot: {
                    running: true,
                    step_count: 0,
                    asset_path: "C:\\\\out\\\\flow.json",
                    output_path: "C:\\\\out\\\\test_flow.py",
                    active_page_id: "page_2",
                    pages: [
                      {
                        id: "page_1", title: "Devices", url: "https://example.test/devices",
                        active: false, opener_id: ""
                      },
                      {
                        id: "page_2", title: "Export", url: "https://example.test/export",
                        active: true, opener_id: "page_1"
                      }
                    ],
                    observation: {
                      url: "", title: "", screenshot_path: "C:\\\\out\\\\shot.png",
                      screenshot_data_url: "data:image/png;base64,iVBORw0KGgo=",
                      viewport: {}, interactive_elements: [], page_text: ""
                    },
                    steps: []
                  }
                }];
              },
              send_message: async () => ({accepted: true}),
              sync_manual: async () => ({accepted: true}),
              activate_page: async (pageId) => {
                window.__studioCalls.push(["activate_page", pageId]);
                return {accepted: true};
              },
              save: async () => ({accepted: true}),
              register_tool: async () => ({accepted: true}),
              run_tool: async () => ({accepted: true})
              ,resume_session: async (path) => {
                window.__studioCalls.push(["resume_session", path]);
                return {accepted: true};
              }
              ,discard_session: async () => ({discarded: true})
            }};
            """
        )
        page.goto(page_path.as_uri())
        page.evaluate(
            "window.dispatchEvent(new Event('pywebviewready'))"
        )
        page.locator("#status").get_by_text("Model unavailable").wait_for()
        assert page.locator("#sendButton").is_disabled()
        assert page.locator("#sessions").get_by_text("saved-flow").is_visible()
        page.get_by_role("button", name="＋ New test").click()
        assert page.locator("#sessionDialog").evaluate("dialog => dialog.open")
        page.locator("input[name=name]").fill("flow")
        page.locator("textarea[name=goal]").fill("API-only flow")
        page.locator("input[name=url]").fill("")
        page.locator("input[name=output_dir]").fill("C:\\out")
        page.get_by_role("button", name="Start Agent").click()

        page.locator("#status").get_by_text("Running").wait_for()
        assert page.locator("#assetArtifact small").inner_text() == "C:\\out\\flow.json"
        assert page.locator("#runPanel .event-card").count() == 1
        assert page.locator("#browserFrame img").get_attribute("src").startswith(
            "data:image/png;base64,"
        )
        assert page.locator("#browserTabs .browser-tab").count() == 2
        assert page.locator("#browserTabs .browser-tab.active").inner_text() == "Export"
        page.get_by_role("button", name="Devices").click()
        assert ["activate_page", "page_1"] in page.evaluate(
            "window.__studioCalls"
        )

        page.get_by_role("button", name="Model settings").click()
        assert page.locator("#modelDialog").evaluate("dialog => dialog.open")
        page.locator("#modelForm input[name=api_key]").fill("desktop-secret")
        page.locator("#modelForm input[name=model]").fill("vision-model")
        page.get_by_role("button", name="Save and verify").click()
        page.locator("#status").get_by_text("Model ready").wait_for()
        assert page.locator("#sendButton").is_enabled()
        assert "vision-model" in page.locator("#modelCurrent").inner_text()
        browser.close()
