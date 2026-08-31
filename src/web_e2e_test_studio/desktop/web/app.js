const $ = (s) => document.querySelector(s);
const api = () => window.pywebview.api;
let state = {
  running: false,
  workspace: "",
  snapshot: null,
  modelAvailable: false,
  agentBusy: false,
  validating: false,
  validationSteps: {},
  polling: false,
  selectedHistory: null
};

window.addEventListener("pywebviewready", async () => {
  const boot = await api().bootstrap();
  state.workspace = boot.workspace;
  $("#workspace").textContent = boot.workspace;
  $("#projectName").textContent = boot.workspace.split(/[\\/]/).pop() || "Workspace";
  $("#outputDir").value = `${boot.workspace}\\recordings`;
  applyModelSettings(boot.model);
  renderEnvironment(
    boot.environment || {ready: false, checks: []},
    boot.environment_settings || {variables: []}
  );
  renderRecoverableSessions(boot.recoverable_sessions || []);
  renderWorkflows(boot.workflows || []);
  setInterval(pollEvents, 250);
  await checkModel();
});

function renderWorkflows(workflows) {
  const select = $("#workflowPath");
  select.innerHTML = `<option value="">Choose a *.workflow.md template</option>` +
    workflows.map(workflow => `<option value="${escapeHtml(workflow.path)}">${escapeHtml(workflow.name || workflow.path)}</option>`).join("");
}

$("#loadWorkflow").onclick = async () => {
  const path = $("#workflowPath").value;
  if (!path) return setStatus("Choose a Markdown workflow", true);
  try {
    const document = await api().load_workflow(path);
    $("#workflowData").value = JSON.stringify(document.data, null, 2);
    setStatus(`Loaded workflow · ${document.name}`);
  } catch (error) {
    setStatus(`Workflow load failed · ${error}`, true);
  }
};

$("#saveWorkflowData").onclick = async () => {
  const path = $("#workflowPath").value;
  if (!path) return setStatus("Choose a Markdown workflow", true);
  try {
    const data = JSON.parse($("#workflowData").value);
    if (!data || Array.isArray(data) || typeof data !== "object") {
      throw new Error("Workflow data must be a JSON object");
    }
    const document = await api().update_workflow_data(JSON.stringify({path, data}));
    $("#workflowData").value = JSON.stringify(document.data, null, 2);
    setStatus(`Saved workflow data · ${document.name}`);
  } catch (error) {
    setStatus(`Workflow save failed · ${error}`, true);
  }
};

$("#previewWorkflow").onclick = async () => {
  const path = $("#workflowPath").value;
  if (!path) return setStatus("Choose a Markdown workflow", true);
  try {
    const compiled = await api().compile_workflow(path);
    addAgentEvent({
      kind: "workflow_preview",
      message: `Workflow ready · ${compiled.steps.length} business stages`,
      payload: {goal: compiled.goal, stages: compiled.steps}
    });
    setStatus(`Workflow valid · ${compiled.steps.length} stages`);
  } catch (error) {
    setStatus(`Workflow validation failed · ${error}`, true);
  }
};

$("#runWorkflow").onclick = async () => {
  const path = $("#workflowPath").value;
  if (!path) return setStatus("Choose a Markdown workflow", true);
  try {
    const result = await api().run_workflow(JSON.stringify({
      path,
      output_dir: `${state.workspace}\\recordings`,
      headless: false
    }));
    if (!result?.accepted) throw new Error("Workflow was not accepted");
    setStatus("Markdown workflow running with Playwright");
  } catch (error) {
    setStatus(`Workflow start failed · ${error}`, true);
  }
};

function renderRecoverableSessions(sessions) {
  const container = $("#sessions");
  const current = `
    <button class="draft active">
      <strong>Current test</strong><span>0 verified steps</span>
    </button>`;
  const recoverable = sessions.map(session => `
    <div class="draft recovery-draft">
      <button class="recovery-open" data-checkpoint="${escapeHtml(session.checkpoint_path)}">
        <strong>${escapeHtml(session.name)}</strong>
        <span>${escapeHtml(session.state)} · round ${session.round}</span>
        <small>${escapeHtml(session.pending_question || session.last_url || session.goal)}</small>
      </button>
      <button class="recovery-discard" title="Discard recovery checkpoint"
        data-checkpoint="${escapeHtml(session.checkpoint_path)}">×</button>
    </div>`).join("");
  container.innerHTML = current + recoverable;
  container.querySelectorAll(".recovery-open").forEach(button => {
    button.onclick = async () => {
      setStatus("Opening project details");
      const session = await api().inspect_session(button.dataset.checkpoint);
      showHistoryDetails(session);
      setStatus("Historical project opened · no actions started");
    };
  });
  container.querySelectorAll(".recovery-discard").forEach(button => {
    button.onclick = async () => {
      await api().discard_session(button.dataset.checkpoint);
      button.closest(".recovery-draft").remove();
      setStatus("Recovery checkpoint discarded");
    };
  });
}

function showHistoryDetails(session) {
  state.selectedHistory = session;
  $("#historyName").textContent = session.name || "Historical project";
  $("#historyState").textContent = session.state || "unknown";
  $("#historyRound").textContent = String(session.round ?? 0);
  $("#historyGoal").textContent = session.goal || "—";
  $("#historyUrl").textContent = session.last_url || "—";
  $("#historyQuestion").textContent = session.pending_question || "—";
  $("#historyQuestionRow").hidden = !session.pending_question;
  $("#historyError").textContent = session.last_error || "—";
  $("#historyErrorRow").hidden = !session.last_error;
  $("#historyUpdated").textContent = session.updated_at || "—";
  const steps = session.steps || [];
  $("#historySteps").innerHTML = steps.length ? steps.map(step => `
    <button type="button" class="history-step" data-step-id="${escapeHtml(step.id)}">
      <span>${step.index}</span><div><strong>${escapeHtml(step.action)}</strong>
      <small>${escapeHtml(step.intent || step.channel)}</small></div>
    </button>`).join("") : `<div class="empty-row">No verified steps</div>`;
  $("#historySteps").querySelectorAll(".history-step").forEach(button => {
    button.onclick = () => openHistoricalStep(button.dataset.stepId);
  });
  $("#historyDialog").showModal();
}

function openHistoricalStep(stepId) {
  const steps = state.selectedHistory?.steps || [];
  $("#historyDialog").close();
  renderTimeline(steps, {historical: true});
  document.querySelector('.tabs button[data-tab="steps"]').click();
  selectStep(stepId);
  setStatus("Historical step opened · read-only");
}

$("#continueHistory").onclick = async () => {
  const session = state.selectedHistory;
  if (!session?.checkpoint_path) return;
  $("#continueHistory").disabled = true;
  setStatus("Resuming session");
  try {
    $("#historyDialog").close();
    await api().resume_session(session.checkpoint_path);
  } finally {
    $("#continueHistory").disabled = false;
  }
};

function setCommandAvailability(available) {
  state.modelAvailable = available;
  $("#prompt").disabled = !available || state.agentBusy;
  $("#sendButton").disabled = !available;
  document.querySelectorAll(".suggestions button").forEach(
    button => button.disabled = !available
  );
  $("#prompt").placeholder = available
    ? "Describe a test goal, continue the run, or answer the agent…"
    : "Waiting for model connection verification…";
}

function setAgentBusy(busy) {
  state.agentBusy = busy;
  $("#cancelButton").hidden = !busy;
  $("#sendButton").hidden = busy;
  $("#prompt").disabled = busy || !state.modelAvailable;
}

async function checkModel() {
  setCommandAvailability(false);
  setStatus("Checking model connection");
  try {
    const result = await api().check_model();
    setCommandAvailability(Boolean(result.available));
    setStatus(
      result.available
        ? `Model ready · ${result.model}`
        : `Model unavailable · ${result.message}`,
      !result.available
    );
    return result;
  } catch (error) {
    setCommandAvailability(false);
    setStatus(`Model check failed · ${error}`, true);
    return {available: false};
  }
}

function applyModelSettings(settings) {
  const current = $("#modelCurrent");
  current.textContent = settings.configured
    ? `Configured · ${settings.key_hint} · ${settings.model} · ${settings.source || "environment"}`
    : `No API key configured · ${settings.model}`;
  const form = $("#modelForm");
  form.elements.model.value = settings.model || "gpt-4o";
  form.elements.base_url.value = settings.base_url || "";
  form.elements.api_key.value = "";
  form.elements.clear_api_key.checked = false;
  form.elements.ignore_proxy.checked = Boolean(settings.ignore_proxy);
}

function renderEnvironment(report, settings) {
  const checks = report?.checks || [];
  $("#environmentChecks").innerHTML = checks.length
    ? checks.map(check => `
      <div class="environment-item ${escapeHtml(check.status)}">
        <div><strong>${escapeHtml(check.label)}</strong><small>${escapeHtml(check.message)}</small></div>
        <span>${escapeHtml(check.status)}</span>
      </div>`).join("")
    : `<div class="environment-empty">No runtime issues detected</div>`;
  $("#installChromium").hidden = !checks.some(
    check => check.id === "chromium" && check.repairable && check.status !== "ready"
  );

  const variables = settings?.variables || [];
  $("#environmentPath").textContent = settings?.path || "";
  $("#environmentVariables").innerHTML = variables.length
    ? variables.map(variable => `
      <div class="environment-item variable">
        <div><strong>${escapeHtml(variable.name)}</strong><small>${variable.masked ? "Configured (hidden)" : escapeHtml(variable.value)} · ${escapeHtml(variable.source)}</small></div>
        <button type="button" class="quiet environment-remove" data-name="${escapeHtml(variable.name)}">Remove</button>
      </div>`).join("")
    : `<div class="environment-empty">No persisted variables</div>`;
}

async function pollEvents() {
  if (state.polling) return;
  state.polling = true;
  try {
  const events = await api().poll_events();
  for (const event of events) {
    if (!["browser_preview", "validation_step"].includes(event.kind)) addAgentEvent(event);
    if (event.kind === "validation_step") applyValidationStep(event.payload);
    if (event.kind === "historical_step_executed") {
      const passed = event.payload?.status === "succeeded";
      setStatus(
        passed
          ? `Step passed: ${event.payload.action}`
          : `Step failed: ${event.payload?.verification?.reason || event.payload?.action || "unknown"}`,
        !passed
      );
    }
    if (event.message?.startsWith("VLM run finished")) setAgentBusy(false);
    if (event.kind === "error" && event.payload?.command === "validate_export") {
      setValidationBusy(false);
    }
    if (event.kind === "validation_passed" || event.kind === "validation_failed") {
      setValidationBusy(false);
      setStatus(
        event.kind === "validation_passed"
          ? "Model-free validation passed"
          : "Model-free validation failed",
        event.kind === "validation_failed"
      );
    }
    if (event.snapshot) applySnapshot(event.snapshot);
    if (event.kind === "validation_step" || event.kind === "validation_passed" || event.kind === "validation_failed") {
      continue;
    }
    if (event.kind === "browser_preview") {
      if (event.payload?.screenshot_data_url) applyStreamFrame(event.payload);
      continue;
    }
    const statusText = event.kind === "progress"
      ? event.message
      : (event.kind === "error" ? `Error: ${event.message}` : (event.snapshot?.running ? "Running" : "Ready"));
    setStatus(statusText, event.kind === "error");
  }
  } finally {
    state.polling = false;
  }
}

function applyStreamFrame(frame) {
  const container = $("#browserFrame");
  let image = container.querySelector("img");
  if (!image) {
    container.innerHTML = '<img alt="Live browser frame">';
    image = container.querySelector("img");
  }
  image.src = frame.screenshot_data_url;
  $("#pageMeta").textContent = `${frame.title || "Untitled"} · ${frame.url || "No URL"}`;
}

function applySnapshot(snapshot) {
  state.snapshot = snapshot;
  state.running = snapshot.running;
  $("#validateButton").disabled = !snapshot.running || state.validating;
  $("#stepCount").textContent = `${snapshot.step_count} verified`;
  document.querySelector(".draft span").textContent = `${snapshot.step_count} verified steps`;
  $("#startButton").textContent = snapshot.running ? "运行中" : "启动会话";
  $("#assetArtifact small").textContent = snapshot.asset_path;
  $("#pytestArtifact small").textContent = snapshot.output_path;
  const obs = snapshot.observation;
  renderBrowserTabs(snapshot.pages || []);
  renderVariables(snapshot.variables || []);
  $("#pageMeta").textContent = `${obs.title || "Untitled"} · ${obs.url || "No URL"}`;
  const frame = $("#browserFrame");
  frame.innerHTML = obs.screenshot_data_url
    ? `<img src="${obs.screenshot_data_url}" alt="Latest browser observation">`
    : `<div class="browser-empty">当前观察没有截图</div>`;
  $("#timeline").innerHTML = snapshot.steps.length ? snapshot.steps.map((s, i) => `
    <div class="step" data-step-id="${escapeHtml(s.id)}"><span class="step-index">${i + 1}</span><div>
      <strong>${escapeHtml(s.action)} <small>· ${escapeHtml(s.channel)}</small></strong>
      <small>${escapeHtml(s.intent)}</small>
    </div></div>`).join("") : `<div class="empty-row">尚无执行步骤</div>`;
  renderTimeline(snapshot.steps || []);
}

function renderTimeline(steps, {historical = false} = {}) {
  $("#timeline").innerHTML = steps.length ? steps.map((step, index) => `
    <button type="button" class="step" data-step-id="${escapeHtml(step.id)}">
      <span class="step-index">${step.index || index + 1}</span><div>
        <strong>${escapeHtml(step.action)} <small>· ${escapeHtml(step.channel)}</small></strong>
        <small>${escapeHtml(step.intent || "")}</small>
      </div>${historical ? '<span class="step-readonly">History</span>' : ""}
    </button>`).join("") : `<div class="empty-row">No verified steps yet.</div>`;
  $("#timeline").querySelectorAll(".step").forEach(button => {
    button.onclick = () => selectStep(button.dataset.stepId);
  });
  $("#timeline").dataset.steps = JSON.stringify(steps);
  $("#timeline").dataset.historical = String(historical);
  $("#stepInspector").hidden = true;
}

function selectStep(stepId) {
  const steps = JSON.parse($("#timeline").dataset.steps || "[]");
  const step = steps.find(item => item.id === stepId);
  if (!step) return;
  $("#timeline").querySelectorAll(".step").forEach(item =>
    item.classList.toggle("selected", item.dataset.stepId === stepId)
  );
  const selected = $("#timeline").querySelector(
    `.step[data-step-id="${CSS.escape(stepId)}"]`
  );
  selected?.scrollIntoView({block: "center", behavior: "smooth"});
  const inspector = $("#stepInspector");
  const details = {
    Action: `${step.channel || ""} · ${step.action || ""}`,
    Intent: step.intent || "",
    "Expected result": step.expected_result || "",
    Locator: step.locator || null,
    Arguments: step.arguments || {},
    Verification: step.verification || null,
    Source: step.source || ""
  };
  inspector.innerHTML = Object.entries(details)
    .filter(([, value]) => value !== "" && value !== null)
    .map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(formatEventValue(value))}</strong></div>`)
    .join("");
  if ($("#timeline").dataset.historical === "true") {
    const actions = document.createElement("div");
    actions.className = "step-inspector-actions";
    actions.innerHTML = '<button type="button" id="runHistoricalStep">Run this step</button>';
    inspector.appendChild(actions);
    $("#runHistoricalStep").onclick = () => runHistoricalStep(step);
  }
  inspector.hidden = false;
}

async function runHistoricalStep(step) {
  const session = state.selectedHistory;
  if (!session?.checkpoint_path) return;
  const needsConfirmation = ["api", "external"].includes(step.channel);
  if (needsConfirmation && !window.confirm(
    `This ${step.channel} step may have external side effects. Run it once?`
  )) return;
  const button = $("#runHistoricalStep");
  button.disabled = true;
  setStatus(`Preparing step: ${step.action}`);
  try {
    await api().run_historical_step(JSON.stringify({
      checkpoint_path: session.checkpoint_path,
      step_id: step.id,
      confirmed: needsConfirmation
    }));
  } catch (error) {
    setStatus(`Step could not be started: ${error}`, true);
    button.disabled = false;
  }
}

function renderBrowserTabs(pages) {
  const container = $("#browserTabs");
  container.hidden = pages.length === 0;
  container.innerHTML = pages.map((page, index) => `
    <button class="browser-tab ${page.active ? "active" : ""}"
      data-page-id="${escapeHtml(page.id)}"
      title="${escapeHtml(page.url || page.title)}">
      ${page.opener_id ? '<span class="popup-mark" title="Popup">↳</span>' : ""}
      ${escapeHtml(page.title || `Tab ${index + 1}`)}
    </button>
  `).join("");
  container.querySelectorAll(".browser-tab").forEach(button => {
    button.onclick = async () => {
      if (button.classList.contains("active")) return;
      setStatus("Switching browser tab");
      await api().activate_page(button.dataset.pageId);
    };
  });
}

function renderVariables(variables) {
  const markup = variables.map(variable => `
    <button class="variable-item" draggable="true"
      data-reference="${escapeHtml(variable.reference)}"
      title="Drag ${escapeHtml(variable.reference)} into an API field or prompt">
      <strong>${escapeHtml(variable.name)}</strong>
      <code>${escapeHtml(variable.value)}</code>
      <small>${escapeHtml(variable.reference)}</small>
    </button>
  `).join("");
  $("#variableList").innerHTML = markup;
  $("#variableQuickList").innerHTML = markup;
  $("#variableQuickPanel").hidden = variables.length === 0;
  document.querySelectorAll(".variable-item").forEach(button => {
    button.onclick = () => {
      const prompt = $("#prompt");
      const separator = prompt.value && !prompt.value.endsWith(" ") ? " " : "";
      insertReference(prompt, separator + button.dataset.reference);
    };
    button.ondragstart = event => {
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData("text/plain", button.dataset.reference);
      event.dataTransfer.setData("application/x-studio-variable", button.dataset.reference);
      document.body.classList.add("dragging-variable");
    };
    button.ondragend = () => document.body.classList.remove("dragging-variable");
  });
}

function insertReference(target, reference) {
  const editor = target.__variableTokenEditor;
  if (!editor) return;
  editor.focus();
  const selection = window.getSelection();
  const range = selection?.rangeCount ? selection.getRangeAt(0) : null;
  const insertion = range && editor.contains(range.commonAncestorContainer)
    ? range : document.createRange();
  if (!range || !editor.contains(range.commonAncestorContainer)) {
    insertion.selectNodeContents(editor);
    insertion.collapse(false);
  }
  insertion.deleteContents();
  const tokenReference = reference.trimStart();
  const leadingText = reference.slice(0, reference.length - tokenReference.length);
  const token = createVariableToken(tokenReference);
  const fragment = document.createDocumentFragment();
  if (leadingText) fragment.append(document.createTextNode(leadingText));
  fragment.append(token);
  insertion.insertNode(fragment);
  insertion.setStartAfter(token);
  insertion.collapse(true);
  selection.removeAllRanges();
  selection.addRange(insertion);
  syncVariableTokenEditor(target);
}

function createVariableToken(reference) {
  const token = document.createElement("span");
  token.className = "variable-token-chip";
  token.contentEditable = "false";
  token.dataset.reference = reference;
  token.title = `${reference} · click to select, then Backspace/Delete`;
  const label = document.createElement("span");
  label.textContent = reference.replace("${VAR:", "").replace("}", "");
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "variable-token-remove";
  remove.setAttribute("aria-label", `Remove ${label.textContent}`);
  remove.textContent = "×";
  remove.onpointerdown = event => event.preventDefault();
  remove.onclick = event => {
    event.stopPropagation();
    const editor = token.closest(".variable-token-editor");
    const source = editor?.__variableTokenSource;
    token.remove();
    if (source) syncVariableTokenEditor(source);
  };
  token.append(label, remove);
  token.onclick = event => {
    event.stopPropagation();
    document.querySelectorAll(".variable-token-chip.selected")
      .forEach(item => item.classList.remove("selected"));
    token.classList.add("selected");
    token.closest(".variable-token-editor")?.focus();
  };
  return token;
}

function serializeTokenEditor(editor) {
  const serialize = node => {
    if (node.nodeType === Node.TEXT_NODE) return node.nodeValue;
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    if (node.classList.contains("variable-token-chip")) return node.dataset.reference;
    if (node.tagName === "BR") return "\n";
    const content = Array.from(node.childNodes).map(serialize).join("");
    return ["DIV", "P"].includes(node.tagName) ? `${content}\n` : content;
  };
  return Array.from(editor.childNodes).map(serialize).join("").replace(/\n$/, "");
}

function syncVariableTokenEditor(source) {
  source.value = serializeTokenEditor(source.__variableTokenEditor);
  source.dispatchEvent(new Event("input", {bubbles: true}));
}

function renderTokenEditor(source) {
  const editor = source.__variableTokenEditor;
  editor.replaceChildren();
  const expression = /\$\{VAR:[A-Za-z_][A-Za-z0-9_]*\}/g;
  let cursor = 0;
  for (const match of source.value.matchAll(expression)) {
    if (match.index > cursor) {
      editor.append(document.createTextNode(source.value.slice(cursor, match.index)));
    }
    editor.append(createVariableToken(match[0]));
    cursor = match.index + match[0].length;
  }
  if (cursor < source.value.length) {
    editor.append(document.createTextNode(source.value.slice(cursor)));
  }
}

function setupVariableTokenEditor(source) {
  source.classList.add("variable-token-source");
  source.setAttribute("aria-hidden", "true");
  source.tabIndex = -1;
  const editor = document.createElement("div");
  editor.className = `variable-token-editor token-editor-${source.id}`;
  editor.contentEditable = "true";
  editor.setAttribute("role", "textbox");
  editor.setAttribute("aria-label", source.getAttribute("aria-label") || source.placeholder || source.id);
  editor.dataset.placeholder = source.placeholder || "";
  source.insertAdjacentElement("afterend", editor);
  source.__variableTokenEditor = editor;
  editor.__variableTokenSource = source;
  renderTokenEditor(source);
  editor.oninput = () => syncVariableTokenEditor(source);
  editor.onblur = () => {
    syncVariableTokenEditor(source);
    renderTokenEditor(source);
  };
  editor.onclick = event => {
    if (!event.target.closest(".variable-token-chip")) {
      editor.querySelectorAll(".variable-token-chip.selected")
        .forEach(item => item.classList.remove("selected"));
    }
  };
  editor.onkeydown = event => {
    const selected = editor.querySelector(".variable-token-chip.selected");
    if (selected && ["Backspace", "Delete"].includes(event.key)) {
      event.preventDefault();
      selected.remove();
      syncVariableTokenEditor(source);
      return;
    }
    if (source.id === "prompt" && event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendPrompt();
    }
  };
  return editor;
}

const variableDropTargets = ["#prompt", "#httpUrl", "#httpHeaders", "#httpBody"];
variableDropTargets.forEach(selector => {
  const source = $(selector);
  const target = setupVariableTokenEditor(source);
  target.classList.add("variable-drop-target");
  target.ondragover = event => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    target.classList.add("variable-drop-active");
  };
  target.ondragleave = () => target.classList.remove("variable-drop-active");
  target.ondrop = event => {
    event.preventDefault();
    target.classList.remove("variable-drop-active");
    document.body.classList.remove("dragging-variable");
    const reference = event.dataTransfer.getData("application/x-studio-variable")
      || event.dataTransfer.getData("text/plain");
    if (reference.startsWith("${VAR:")) insertReference(source, reference);
  };
});

const variablePanel = $("#variableQuickPanel");
const variablePanelHandle = $("#variablePanelHandle");
let variablePanelDrag = null;
variablePanelHandle.onpointerdown = event => {
  if (event.target.closest("button")) return;
  const bounds = variablePanel.getBoundingClientRect();
  variablePanelDrag = {
    offsetX: event.clientX - bounds.left,
    offsetY: event.clientY - bounds.top
  };
  variablePanelHandle.setPointerCapture(event.pointerId);
};
variablePanelHandle.onpointermove = event => {
  if (!variablePanelDrag) return;
  const left = Math.max(8, Math.min(
    event.clientX - variablePanelDrag.offsetX,
    window.innerWidth - variablePanel.offsetWidth - 8
  ));
  const top = Math.max(8, Math.min(
    event.clientY - variablePanelDrag.offsetY,
    window.innerHeight - variablePanel.offsetHeight - 8
  ));
  variablePanel.style.left = `${left}px`;
  variablePanel.style.top = `${top}px`;
  variablePanel.style.right = "auto";
};
variablePanelHandle.onpointerup = event => {
  variablePanelDrag = null;
  variablePanelHandle.releasePointerCapture(event.pointerId);
};
$("#collapseVariablePanel").onclick = () => {
  variablePanel.classList.toggle("collapsed");
  const collapsed = variablePanel.classList.contains("collapsed");
  $("#collapseVariablePanel").textContent = collapsed ? "+" : "−";
  $("#collapseVariablePanel").setAttribute("aria-expanded", String(!collapsed));
};

function applyValidationStep(step) {
  if (!step?.step_id) return;
  state.validationSteps[step.step_id] = step;
  const selector = `.step[data-step-id="${CSS.escape(step.step_id)}"]`;
  const row = document.querySelector(selector);
  if (!row) return;
  let status = row.querySelector(".step-validation");
  if (!status) {
    status = document.createElement("span");
    row.append(status);
  }
  status.className = `step-validation ${step.status}`;
  status.textContent = step.status;
}

function formatEventValue(value) {
  if (typeof value !== "object" || value === null) return String(value);
  try {
    const formatted = JSON.stringify(value, null, 2);
    return formatted.length > 4000 ? `${formatted.slice(0, 4000)}\n…` : formatted;
  } catch (error) {
    return `Unable to display structured value: ${error?.message || error}`;
  }
}

function addAgentEvent(event) {
  $(".welcome")?.remove();
  const feed = $("#runPanel");
  const payload = event.payload || {};
  const rows = Object.entries(payload)
    .filter(([, value]) => value !== "" && value !== null && value !== undefined)
    .map(([key, value]) => `<div class="event-row"><span>${escapeHtml(key.replaceAll("_", " "))}</span><strong>${escapeHtml(formatEventValue(value))}</strong></div>`)
    .join("");
  const legacyDetails = event.details?.length
    ? event.details.map(escapeHtml).join("\n") : "";
  const details = rows || legacyDetails
    ? `<div class="event-details">${rows || legacyDetails}</div>` : "";
  feed.insertAdjacentHTML("beforeend", `
    <article class="message">
      <div class="event-card"><div class="event-head"><span class="event-icon">✦</span>
      <span>${escapeHtml(event.message)}</span></div>${details}</div>
    </article>`);
  feed.scrollTop = feed.scrollHeight;
}

function addUserMessage(text) {
  $(".welcome")?.remove();
  const feed = $("#runPanel");
  feed.insertAdjacentHTML("beforeend", `<article class="message user"><div class="bubble">${escapeHtml(text)}</div></article>`);
  feed.scrollTop = feed.scrollHeight;
}

function setValidationBusy(busy) {
  state.validating = busy;
  if (busy) {
    state.validationSteps = {};
    document.querySelectorAll(".step-validation").forEach(status => status.remove());
  }
  const button = $("#validateButton");
  button.disabled = busy || !state.running;
  button.textContent = busy ? "Validating…" : "Validate without model";
}

async function sendPrompt() {
  const input = $("#prompt");
  const text = input.value.trim();
  if (!text) return;
  if (!state.modelAvailable) {
    setStatus("Verify the model connection before sending commands", true);
    return;
  }
  if (!state.running) { $("#sessionDialog").showModal(); return; }
  addUserMessage(text);
  input.value = "";
  renderTokenEditor(input);
  setStatus("Agent working");
  setAgentBusy(true);
  await api().send_message(text);
}

$("#sendButton").onclick = sendPrompt;
$("#cancelButton").onclick = async () => {
  const result = await api().cancel();
  setStatus(
    result.accepted ? "Cancelling active action" : "No active action"
  );
};
$("#prompt").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendPrompt(); }
});
document.querySelectorAll(".suggestions button").forEach(b => b.onclick = () => { $("#prompt").value = b.textContent; sendPrompt(); });
$("#newSession").onclick = $("#startButton").onclick = () => $("#sessionDialog").showModal();
$("#chooseOutputDir").onclick = async () => {
  try {
    const result = await api().choose_output_directory();
    if (result?.path) $("#outputDir").value = result.path;
  } catch (error) {
    setStatus(`Directory selection failed · ${error?.message || error}`, true);
  }
};
$("#environmentSettings").onclick = async () => {
  try {
    const [report, settings] = await Promise.all([
      api().check_environment(),
      api().get_environment_settings()
    ]);
    renderEnvironment(report, settings);
    $("#environmentDialog").showModal();
  } catch (error) {
    setStatus(`Environment check failed · ${error?.message || error}`, true);
  }
};
$("#closeEnvironment").onclick = $("#doneEnvironment").onclick = () => $("#environmentDialog").close();
$("#installChromium").onclick = async () => {
  const button = $("#installChromium");
  button.disabled = true;
  setStatus("Installing Playwright Chromium");
  try {
    const result = await api().install_environment_component("chromium");
    renderEnvironment(result.environment, await api().get_environment_settings());
    setStatus("Playwright Chromium installed");
  } catch (error) {
    setStatus(`Chromium installation failed · ${error?.message || error}`, true);
  } finally {
    button.disabled = false;
  }
};
$("#environmentVariableForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.target;
  try {
    const data = Object.fromEntries(new FormData(form));
    const settings = await api().configure_environment_variable({
      name: data.variable_name,
      value: data.value
    });
    renderEnvironment(await api().check_environment(), settings);
    applyModelSettings(await api().get_model_settings());
    form.reset();
    setStatus("Environment variable saved");
  } catch (error) {
    setStatus(`Environment variable failed · ${error?.message || error}`, true);
  }
});
$("#environmentVariables").addEventListener("click", async event => {
  const button = event.target.closest(".environment-remove");
  if (!button) return;
  try {
    const settings = await api().configure_environment_variable({
      name: button.dataset.name,
      remove: true
    });
    renderEnvironment(await api().check_environment(), settings);
    applyModelSettings(await api().get_model_settings());
    setStatus(`Removed ${button.dataset.name}`);
  } catch (error) {
    setStatus(`Environment variable removal failed · ${error?.message || error}`, true);
  }
});
$("#modelSettings").onclick = async () => {
  applyModelSettings(await api().get_model_settings());
  $("#modelDialog").showModal();
};
$("#modelForm").addEventListener("submit", async e => {
  if (e.submitter?.value !== "save") return;
  e.preventDefault();
  const form = e.target;
  const data = Object.fromEntries(new FormData(form));
  data.clear_api_key = new FormData(form).has("clear_api_key");
  data.ignore_proxy = new FormData(form).has("ignore_proxy");
  try {
    const settings = await api().configure_model(data);
    applyModelSettings(settings);
    const validation = await checkModel();
    settings.configured = Boolean(validation.available);
    $("#modelDialog").close();
    setStatus(settings.configured ? `Model ready · ${settings.model}` : "Model not configured", !settings.configured);
  } catch (error) {
    setStatus(`Model settings failed · ${error}`, true);
  }
});
$("#checkModel").onclick = async event => {
  event.preventDefault();
  await checkModel();
};
$("#sessionForm").addEventListener("submit", async e => {
  if (e.submitter?.value !== "start") return;
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  data.headless = new FormData(e.target).has("headless");
  $("#sessionDialog").close();
  $("#sessionTitle").textContent = data.goal;
  setStatus("Starting");
  await api().start_session(data);
});
$("#syncButton").onclick = async () => { setStatus("Syncing"); await api().sync_manual(); };
$("#validateButton").onclick = async () => {
  if (!state.running || state.validating) return;
  setValidationBusy(true);
  setStatus("Running model-free validation");
  try {
    await api().validate_export();
  } catch (error) {
    setValidationBusy(false);
    setStatus(`Validation could not start · ${error}`, true);
  }
};
$("#registerTool").onclick = async () => {
  const path = $("#toolPath").value.trim();
  if (path) {
    await api().register_tool(path);
    const filename = path.split(/[\\/]/).pop() || "";
    $("#toolRef").value = filename.replace(/\.[^.]+$/, "");
  }
};
$("#defineVariable").onclick = async () => {
  if (!state.running) {
    setStatus("Start a recording session before defining variables", true);
    return;
  }
  const name = $("#variableName").value.trim();
  const description = $("#variableFormat").value.trim();
  if (!name || !description) {
    setStatus("Variable name and format are required", true);
    return;
  }
  try {
    setStatus(`Generating variable ${name}`);
    await api().define_variable(JSON.stringify({name, description}));
  } catch (error) {
    setStatus(`Variable generation failed · ${error}`, true);
  }
};
$("#variableSource").onchange = () => {
  $("#variableFileRow").hidden = $("#variableSource").value !== "spreadsheet";
};
$("#chooseVariableFile").onclick = async () => {
  const selected = await api().choose_spreadsheet();
  if (selected.path) $("#variableFilePath").value = selected.path;
};
$("#extractVariable").onclick = async () => {
  if (!state.running) {
    setStatus("Start a recording session before extracting variables", true);
    return;
  }
  const name = $("#variableName").value.trim();
  const description = $("#variableExtraction").value.trim();
  const source = $("#variableSource").value;
  const filePath = $("#variableFilePath").value.trim();
  if (!name || !description) {
    setStatus("Variable name and extraction description are required", true);
    return;
  }
  if (source === "spreadsheet" && !filePath) {
    setStatus("Choose an Excel workbook before extracting", true);
    return;
  }
  try {
    setStatus(`Extracting variable ${name}`);
    await api().extract_variable(JSON.stringify({
      name, description, source, file_path: filePath
    }));
  } catch (error) {
    setStatus(`Variable extraction failed · ${error}`, true);
  }
};
$("#runTool").onclick = async () => {
  const ref = $("#toolRef").value.trim();
  if (!ref) return;
  const args = $("#toolArgs").value.match(/(?:[^\s"]+|"[^"]*")+/g)?.map(value =>
    value.startsWith('"') && value.endsWith('"') ? value.slice(1, -1) : value
  ) || [];
  setStatus("Running tool");
  await api().run_tool(ref, args);
};
const addHttpAssertion = () => {
  const row = document.createElement("div");
  row.className = "http-assertion";
  row.innerHTML = `
    <input class="assertion-path" placeholder="JSON path, e.g. data.id">
    <select class="assertion-operator" aria-label="Assertion operator">
      <option value="equals">equals</option>
      <option value="not_equals">not equals</option>
      <option value="contains">contains</option>
      <option value="exists">exists</option>
      <option value="not_exists">does not exist</option>
    </select>
    <select class="assertion-type" aria-label="Expected value type">
      <option value="string">string</option><option value="number">number</option>
      <option value="boolean">boolean</option><option value="null">null</option>
      <option value="json">JSON</option>
    </select>
    <input class="assertion-expected" placeholder="Expected value or \${VAR:name}">
    <button class="assertion-remove quiet" type="button" aria-label="Remove assertion">×</button>`;
  const operator = row.querySelector(".assertion-operator");
  const type = row.querySelector(".assertion-type");
  const expected = row.querySelector(".assertion-expected");
  const update = () => {
    const noExpected = operator.value === "exists" || operator.value === "not_exists";
    type.disabled = noExpected;
    expected.disabled = noExpected || type.value === "null";
    expected.placeholder = type.value === "boolean" ? "true or false" : "Expected value or \${VAR:name}";
  };
  operator.onchange = update;
  type.onchange = update;
  row.querySelector(".assertion-remove").onclick = () => row.remove();
  $("#httpAssertions").append(row);
  update();
};
const collectHttpAssertions = () => [...document.querySelectorAll(".http-assertion")].map((row, index) => {
  const path = row.querySelector(".assertion-path").value.trim();
  const operator = row.querySelector(".assertion-operator").value;
  const type = row.querySelector(".assertion-type").value;
  const raw = row.querySelector(".assertion-expected").value;
  if (!path) throw new Error(`Assertion ${index + 1} requires a JSON path`);
  const assertion = {path, operator};
  if (operator === "exists" || operator === "not_exists") return assertion;
  if (type === "string") assertion.expected = raw;
  else if (type === "null") assertion.expected = null;
  else if (type === "number") {
    assertion.expected = Number(raw);
    if (!raw.trim() || !Number.isFinite(assertion.expected)) throw new Error(`Assertion ${index + 1} requires a number`);
  } else if (type === "boolean") {
    if (!["true", "false"].includes(raw.trim().toLowerCase())) throw new Error(`Assertion ${index + 1} requires true or false`);
    assertion.expected = raw.trim().toLowerCase() === "true";
  } else assertion.expected = JSON.parse(raw);
  return assertion;
});
$("#addHttpAssertion").onclick = addHttpAssertion;
$("#sendHttpRequest").onclick = async () => {
  if (!state.running) {
    setStatus("Start a recording session before sending an API request", true);
    $("#sessionDialog").showModal();
    return;
  }
  const method = $("#httpMethod").value;
  const url = $("#httpUrl").value.trim();
  if (!url) {
    setStatus("API URL is required", true);
    return;
  }
  try {
    const headersText = $("#httpHeaders").value.trim();
    const bodyText = $("#httpBody").value;
    const headers = headersText ? JSON.parse(headersText) : {};
    const expectedStatus = Number($("#httpExpectedStatus").value);
    const timeout = Number($("#httpTimeout").value);
    if (!headers || Array.isArray(headers) || typeof headers !== "object") {
      throw new Error("Headers must be a JSON object");
    }
    if (!Number.isInteger(expectedStatus) || expectedStatus < 100 || expectedStatus > 599) {
      throw new Error("Expected status must be an integer from 100 to 599");
    }
    if (!Number.isFinite(timeout) || timeout <= 0) {
      throw new Error("Timeout must be greater than zero");
    }
    const request = {
      method,
      url,
      headers,
      expected_status: expectedStatus,
      timeout,
      response_assertions: collectHttpAssertions()
    };
    if (bodyText) {
      if ($("#httpBodyType").value === "json") request.json = JSON.parse(bodyText);
      else request.body = bodyText;
    }
    setStatus(`Sending ${method} request`);
    const result = await api().run_http_request(JSON.stringify(request));
    if (!result?.accepted) throw new Error("The HTTP request was not accepted");
    setStatus("HTTP request queued");
  } catch (error) {
    const message = error?.message || String(error);
    setStatus(`API request failed: ${message}`, true);
    addAgentEvent({
      kind: "error",
      message: "API request could not be submitted",
      payload: {reason: message}
    });
  }
};
$("#launchTool").onclick = async () => {
  const path = $("#launchPath").value.trim();
  if (!path) return;
  const args = $("#launchArgs").value.match(/(?:[^\s"]+|"[^"]*")+/g)?.map(value =>
    value.startsWith('"') && value.endsWith('"') ? value.slice(1, -1) : value
  ) || [];
  setStatus("Submitting application launch");
  try {
    const payload = JSON.stringify({path, arguments: args});
    const result = await api().import_and_launch_tool(payload);
    if (!result?.accepted) throw new Error("The launch command was not accepted");
    setStatus("Application launch queued");
  } catch (error) {
    const message = error?.message || String(error);
    setStatus(`Launch failed: ${message}`, true);
    addAgentEvent({
      kind: "error",
      message: "Application launch failed",
      payload: {reason: message}
    });
  }
};
$("#chooseLaunchPath").onclick = async () => {
  const selected = await api().choose_application();
  if (selected.path) $("#launchPath").value = selected.path;
};
document.querySelectorAll(".tabs button").forEach(button => button.onclick = () => {
  document.querySelectorAll(".tabs button,.tab-panel").forEach(x => x.classList.remove("active"));
  button.classList.add("active");
  $(`#${button.dataset.tab}Panel`).classList.add("active");
});
document.querySelectorAll("[data-open-tab]").forEach(button => button.onclick = () => {
  document.querySelector(`.tabs button[data-tab="${button.dataset.openTab}"]`).click();
});

function setStatus(text, error=false) {
  $("#status").innerHTML = `<i style="${error ? "background:var(--danger)" : ""}"></i> ${escapeHtml(text)}`;
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
