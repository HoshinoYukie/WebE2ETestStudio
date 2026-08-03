const $ = (s) => document.querySelector(s);
const api = () => window.pywebview.api;
let state = {
  running: false,
  workspace: "",
  snapshot: null,
  modelAvailable: false,
  agentBusy: false,
  validating: false,
  polling: false
};

window.addEventListener("pywebviewready", async () => {
  const boot = await api().bootstrap();
  state.workspace = boot.workspace;
  $("#workspace").textContent = boot.workspace;
  $("#projectName").textContent = boot.workspace.split(/[\\/]/).pop() || "Workspace";
  $("#outputDir").value = `${boot.workspace}\\recordings`;
  applyModelSettings(boot.model);
  renderCapabilities(boot.capabilities);
  renderRecoverableSessions(boot.recoverable_sessions || []);
  setInterval(pollEvents, 250);
  await checkModel();
});

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
      setStatus("Resuming session");
      await api().resume_session(button.dataset.checkpoint);
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

function renderCapabilities(capabilities) {
  $("#capabilityList").innerHTML = capabilities.map(c => `
    <div class="capability">
      <div class="capability-head"><strong>${escapeHtml(c.label)}</strong><small>${escapeHtml(c.channel)}</small></div>
      <div class="chips">${c.actions.map(a => `<span class="chip">${escapeHtml(a.name)}</span>`).join("")}</div>
    </div>`).join("");
}

async function pollEvents() {
  if (state.polling) return;
  state.polling = true;
  try {
  const events = await api().poll_events();
  for (const event of events) {
    if (event.kind !== "browser_preview") addAgentEvent(event);
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
  $("#pageMeta").textContent = `${obs.title || "Untitled"} · ${obs.url || "No URL"}`;
  const frame = $("#browserFrame");
  frame.innerHTML = obs.screenshot_data_url
    ? `<img src="${obs.screenshot_data_url}" alt="Latest browser observation">`
    : `<div class="browser-empty">当前观察没有截图</div>`;
  $("#timeline").innerHTML = snapshot.steps.length ? snapshot.steps.map((s, i) => `
    <div class="step"><span class="step-index">${i + 1}</span><div>
      <strong>${escapeHtml(s.action)} <small>· ${escapeHtml(s.channel)}</small></strong>
      <small>${escapeHtml(s.intent)}</small>
    </div></div>`).join("") : `<div class="empty-row">尚无执行步骤</div>`;
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

function addAgentEvent(event) {
  $(".welcome")?.remove();
  const feed = $("#runPanel");
  const payload = event.payload || {};
  const rows = Object.entries(payload)
    .filter(([, value]) => value !== "" && value !== null && value !== undefined)
    .map(([key, value]) => `<div class="event-row"><span>${escapeHtml(key.replaceAll("_", " "))}</span><strong>${escapeHtml(String(value))}</strong></div>`)
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
$("#runTool").onclick = async () => {
  const ref = $("#toolRef").value.trim();
  if (!ref) return;
  const args = $("#toolArgs").value.match(/(?:[^\s"]+|"[^"]*")+/g)?.map(value =>
    value.startsWith('"') && value.endsWith('"') ? value.slice(1, -1) : value
  ) || [];
  setStatus("Running tool");
  await api().run_tool(ref, args);
};
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
      timeout
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
