const state = {
  files: [],
  config: null,
  jobId: null,
  job: null,
  events: [],
  eventSource: null,
  activeDocument: 0,
  completedStages: new Set(),
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

document.addEventListener("DOMContentLoaded", async () => {
  bindUploadControls();
  await loadConfig();
  const persistedJobId = new URLSearchParams(window.location.search).get("job");
  if (persistedJobId) await resumeJob(persistedJobId);
});

function bindUploadControls() {
  const dropzone = $("#dropzone");
  const input = $("#fileInput");
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      input.click();
    }
  });
  ["dragenter", "dragover"].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add("dragover");
    }),
  );
  ["dragleave", "drop"].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragover");
    }),
  );
  dropzone.addEventListener("drop", (event) => addFiles([...event.dataTransfer.files]));
  input.addEventListener("change", () => {
    addFiles([...input.files]);
    input.value = "";
  });
  $("#clearFilesButton").addEventListener("click", clearSelectedFiles);
  $("#analyzeButton").addEventListener("click", startAnalysis);
  $$('[data-reset-intake]').forEach((button) => button.addEventListener("click", resetIntake));
}

async function loadConfig() {
  try {
    const response = await fetch("/api/config");
    state.config = await response.json();
    $("#maxUpload").textContent = state.config.max_upload_mb;
    renderProviderBadge(state.config.provider_mode);
    if (state.config.fixture_notice) {
      $("#fixtureNotice").textContent = state.config.fixture_notice;
      $("#fixtureNotice").classList.remove("hidden");
    }
  } catch (error) {
    showToast("Could not load application configuration.", true);
  }
}

function addFiles(files) {
  const allowed = ["image/png", "image/jpeg", "image/webp"];
  const isSupported = (file) => allowed.includes(file.type) || /\.(png|jpe?g|webp)$/i.test(file.name);
  const candidates = [...state.files, ...files.filter(isSupported)];
  const unique = new Map();
  candidates.forEach((file) => unique.set(`${file.name}:${file.size}:${file.lastModified}`, file));
  const duplicateSkipped = unique.size < candidates.length;
  state.files = [...unique.values()].slice(0, 8);
  renderSelectedFiles();
  if (files.some((file) => !isSupported(file)) || duplicateSkipped || unique.size > 8) {
    showToast("Unsupported, duplicate, or excess files were skipped.", true);
  }
}

function renderSelectedFiles() {
  const fileList = $("#fileList");
  fileList.innerHTML = state.files
    .map(
      (file, index) => `<div class="file-chip"><b>DOC</b><span title="${escapeAttribute(file.name)}">${escapeHtml(file.name)}</span><small>${formatBytes(file.size)}</small><button type="button" data-remove-file="${index}" aria-label="Remove ${escapeAttribute(file.name)}">×</button></div>`,
    )
    .join("");
  fileList.querySelectorAll("[data-remove-file]").forEach((button) =>
    button.addEventListener("click", () => {
      state.files.splice(Number(button.dataset.removeFile), 1);
      renderSelectedFiles();
    }),
  );
  fileList.classList.toggle("has-files", state.files.length > 0);
  $("#clearFilesButton").classList.toggle("hidden", state.files.length === 0);
  $("#analyzeButton").disabled = state.files.length === 0;
}

function clearSelectedFiles() {
  state.files = [];
  $("#fileInput").value = "";
  renderSelectedFiles();
}

async function startAnalysis() {
  if (!state.files.length) return;
  resetWorkspace();
  $("#analyzeButton").disabled = true;
  $("#analyzeButton span").textContent = "Uploading…";
  $("#fileInput").disabled = true;
  $("#uploadPanel").classList.add("locked");
  $$('[data-reset-intake]').forEach((button) => button.classList.remove("hidden"));
  const body = new FormData();
  state.files.forEach((file) => body.append("files", file));
  try {
    const response = await fetch("/api/jobs", { method: "POST", body });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Upload failed");
    state.jobId = result.job_id;
    window.history.replaceState({}, "", `?job=${encodeURIComponent(state.jobId)}`);
    $("#workspace").classList.remove("hidden");
    updateProgress("upload", true);
    renderDocumentTabs(result.files);
    connectEventStream();
    window.scrollTo({ top: $("#workspace").offsetTop - 95, behavior: "smooth" });
  } catch (error) {
    showToast(error.message, true);
    $("#analyzeButton").disabled = false;
    $("#fileInput").disabled = false;
    $("#uploadPanel").classList.remove("locked");
  } finally {
    $("#analyzeButton span").textContent = "Start extraction";
  }
}

async function resumeJob(jobId) {
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!response.ok) throw new Error("Saved intake was not found");
    resetWorkspace();
    state.jobId = jobId;
    state.job = await response.json();
    renderProviderBadge(state.job.provider_mode || state.config?.provider_mode);
    $("#workspace").classList.remove("hidden");
    $("#fileInput").disabled = true;
    $("#uploadPanel").classList.add("locked");
    $$('[data-reset-intake]').forEach((button) => button.classList.remove("hidden"));
    renderDocumentTabs(state.job.files);
    updateProgress("upload", true);
    if (state.job.ocr) {
      updateProgress("ocr", true);
      renderOcr(state.job);
    }
    if (state.job.extraction) {
      updateProgress("ner", true);
      renderExtraction(state.job.extraction);
    }
    if (state.job.grounding) {
      updateProgress("grounding", true);
      renderGrounding(state.job.grounding);
    }
    // Replay persisted SSE events even for completed jobs so a saved demo URL
    // retains the same auditable tool trace as the original live run.
    connectEventStream();
    if (state.job.status === "ready") {
      updateProgress("review", true);
      setJobStatus("ready", "Ready for review");
    } else if (state.job.status === "error") {
      setJobStatus("error", "Needs attention");
    }
  } catch (error) {
    window.history.replaceState({}, "", window.location.pathname);
    showToast(error.message, true);
  }
}

function resetWorkspace() {
  if (state.eventSource) state.eventSource.close();
  state.jobId = null;
  state.job = null;
  state.events = [];
  state.activeDocument = 0;
  state.completedStages = new Set();
  state.eventSource = null;
  $("#activityFeed").innerHTML = "";
  $("#metrics").classList.add("hidden");
  $("#entityGroups").innerHTML = '<div class="skeleton-list"><span></span><span></span><span></span><span></span></div>';
  $("#missingFields").classList.add("hidden");
  $("#reviewReasons").classList.add("hidden");
  $("#groundingPanel").classList.add("hidden");
  $("#groundingSummary").textContent = "";
  $("#groundingWarnings").classList.add("hidden");
  $("#groundingWarnings").innerHTML = "";
  $("#criteriaRows").innerHTML = "";
  $("#retrievedPassages").innerHTML = "";
  $("#passageCount").textContent = "";
  $$(".progress-step").forEach((element, index) => {
    element.classList.remove("active", "complete");
    element.querySelector("i").textContent = String(index + 1);
  });
  $("#progressFill").style.width = "0%";
  $("#documentTabs").innerHTML = "";
  $("#documentImage").removeAttribute("src");
  $("#documentImage").style.display = "none";
  $("#documentEmpty").style.display = "block";
  $("#ocrTranscript").textContent = "Waiting for OCR…";
  $("#ocrConfidence").textContent = "";
  setJobStatus("running", "Running");
}

function resetIntake() {
  resetWorkspace();
  clearSelectedFiles();
  window.history.replaceState({}, "", window.location.pathname);
  $("#workspace").classList.add("hidden");
  $("#fileInput").disabled = false;
  $("#uploadPanel").classList.remove("locked");
  $("#analyzeButton span").textContent = "Start extraction";
  renderProviderBadge(state.config?.provider_mode);
  $$('[data-reset-intake]').forEach((button) => button.classList.add("hidden"));
  window.scrollTo({ top: Math.max(0, $("#uploadPanel").offsetTop - 105), behavior: "smooth" });
  showToast("Intake reset. Choose documents to begin again.");
}

function renderProviderBadge(mode) {
  const normalized = mode === "gemini" ? "gemini" : "fixture";
  const badge = $("#providerBadge");
  badge.textContent = normalized === "gemini" ? "Gemini · ADK live" : "Fixture · rehearsal";
  badge.className = `provider-badge ${normalized}`;
}

function connectEventStream() {
  const stream = new EventSource(`/api/jobs/${state.jobId}/events`);
  state.eventSource = stream;
  stream.addEventListener("workflow", async (message) => {
    const event = JSON.parse(message.data);
    state.events.push(event);
    renderActivityEvent(event);
    if (event.event_type === "tool_call") updateProgress(event.stage, false);
    if (event.event_type === "tool_result") {
      updateProgress(event.stage, true);
      await refreshJob();
    }
    if (event.stage === "review" && event.payload.status === "completed") {
      updateProgress("review", true);
      await refreshJob();
      stream.close();
    }
    if (event.event_type === "error") {
      setJobStatus("error", "Needs attention");
      stream.close();
      await refreshJob();
    }
  });
  stream.onerror = () => {
    if (state.job?.status === "ready" || state.job?.status === "error") stream.close();
  };
}

async function refreshJob() {
  if (!state.jobId) return;
  const response = await fetch(`/api/jobs/${state.jobId}`);
  if (!response.ok) return;
  state.job = await response.json();
  if (state.job.ocr) renderOcr(state.job);
  if (state.job.extraction) renderExtraction(state.job.extraction);
  if (state.job.grounding) renderGrounding(state.job.grounding);
  if (state.job.status === "ready") setJobStatus("ready", "Ready for review");
  if (state.job.status === "error") {
    setJobStatus("error", "Needs attention");
    showToast(state.job.error || "Workflow failed", true);
  }
}

function updateProgress(stage, completed) {
  const order = ["upload", "ocr", "ner", "grounding", "review"];
  const index = order.indexOf(stage);
  if (index < 0) return;
  const element = $(`.progress-step[data-stage="${stage}"]`);
  if (completed) {
    state.completedStages.add(stage);
    element.classList.remove("active");
    element.classList.add("complete");
    element.querySelector("i").textContent = "✓";
  } else {
    element.classList.add("active");
  }
  const completedCount = state.completedStages.size;
  $("#progressFill").style.width = `${Math.max(0, ((completedCount - 1) / 4) * 100)}%`;
}

function setJobStatus(kind, label) {
  const status = $("#jobStatus");
  status.className = `status-pill ${kind}`;
  status.innerHTML = `<span></span>${escapeHtml(label)}`;
}

function renderDocumentTabs(files) {
  const tabs = $("#documentTabs");
  tabs.innerHTML = files
    .map(
      (file, index) => `<button class="document-tab ${index === 0 ? "active" : ""}" data-index="${index}">${escapeHtml(file.original_name)}</button>`,
    )
    .join("");
  tabs.querySelectorAll("button").forEach((button) =>
    button.addEventListener("click", () => selectDocument(Number(button.dataset.index))),
  );
  state.job = { ...(state.job || {}), files };
  selectDocument(0);
}

function selectDocument(index) {
  state.activeDocument = index;
  $$(".document-tab").forEach((tab, tabIndex) => tab.classList.toggle("active", tabIndex === index));
  const file = state.job?.files?.[index];
  if (!file) return;
  const image = $("#documentImage");
  image.src = file.url;
  image.style.display = "block";
  $("#documentEmpty").style.display = "none";
  renderActiveTranscript();
}

function renderOcr(job) {
  if (!state.job?.files?.length) renderDocumentTabs(job.files);
  renderActiveTranscript();
}

function renderActiveTranscript() {
  const document = state.job?.ocr?.documents?.[state.activeDocument];
  if (!document) return;
  $("#ocrTranscript").textContent = document.raw_text || "No text detected.";
  $("#ocrConfidence").textContent = `${Math.round(document.overall_confidence * 100)}% confidence`;
}

function renderExtraction(extraction) {
  const entities = extraction.entities || [];
  const needsReview = entities.filter((entity) => entity.confidence < 0.85 && entity.verification_status !== "corrected").length;
  $("#metrics").classList.remove("hidden");
  $("#metricDocs").textContent = state.job?.ocr?.documents?.length ?? "—";
  $("#metricEntities").textContent = entities.length;
  $("#metricConfidence").textContent = `${Math.round(extraction.overall_confidence * 100)}%`;
  $("#metricReview").textContent = needsReview;
  $("#metricCriteria").textContent = state.job?.grounding?.criteria?.length ?? "—";

  if (extraction.review_reasons?.length) {
    const callout = $("#reviewReasons");
    callout.classList.remove("hidden");
    callout.innerHTML = `<details><summary><strong>${extraction.review_reasons.length} review flag${extraction.review_reasons.length === 1 ? "" : "s"}</strong><span>Open details</span></summary><ul>${extraction.review_reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul></details>`;
  } else {
    $("#reviewReasons").classList.add("hidden");
    $("#reviewReasons").innerHTML = "";
  }

  const sectionNames = {
    patient: "Patient details",
    provider: "Provider details",
    request: "Requested therapy",
    clinical: "Clinical observations",
    history: "Pertinent history",
    assessment: "Assessment & plan",
    other: "Other evidence",
  };
  $("#entityGroups").innerHTML = entities.length
    ? `<div class="entity-table-scroll"><table class="entity-table">
        <thead><tr><th>Field</th><th>Extracted value</th><th>Confidence</th><th class="evidence-column">Evidence</th><th><span class="visually-hidden">Action</span></th></tr></thead>
        <tbody>${entities.map((entity) => renderEntityRow(entity, sectionNames)).join("")}</tbody>
      </table></div>`
    : '<div class="review-callout">No supported entities were extracted. Review the OCR transcript.</div>';

  $$(".verify-button").forEach((button) => button.addEventListener("click", () => saveCorrection(button.dataset.entityId)));

  if (extraction.missing_fields?.length) {
    const missing = $("#missingFields");
    missing.classList.remove("hidden");
    missing.innerHTML = `<strong>Missing intake information</strong>${extraction.missing_fields.map((field) => `<span class="missing-chip">${escapeHtml(field)}</span>`).join("")}`;
  } else {
    $("#missingFields").classList.add("hidden");
    $("#missingFields").innerHTML = "";
  }
}

function renderGrounding(grounding) {
  const criteria = grounding.criteria || [];
  const passages = grounding.passages || [];
  $("#groundingPanel").classList.remove("hidden");
  $("#metricCriteria").textContent = criteria.length;
  $("#policyNumber").textContent = `Policy ${grounding.policy_number || "—"}`;
  $("#policyEffective").textContent = `Effective ${grounding.effective_date || "—"}`;
  $("#groundingSummary").textContent = grounding.summary || "Human review is required.";
  $("#passageCount").textContent = `${passages.length} passage${passages.length === 1 ? "" : "s"}`;

  const warnings = grounding.warnings || [];
  const warningBox = $("#groundingWarnings");
  warningBox.classList.toggle("hidden", warnings.length === 0);
  warningBox.innerHTML = warnings.length
    ? `<strong>Grounding notes</strong><ul>${warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>`
    : "";

  $("#criteriaRows").innerHTML = criteria.length
    ? criteria.map(renderCriterionRow).join("")
    : '<tr><td colspan="5" class="empty-table">No criteria were produced. A reviewer must inspect the packet and policy directly.</td></tr>';
  $("#retrievedPassages").innerHTML = passages.length
    ? passages.map(renderPassage).join("")
    : '<div class="empty-passage">No passage cleared the configured retrieval threshold.</div>';
}

function renderCriterionRow(criterion) {
  const status = ["met", "not_met", "unknown"].includes(criterion.status) ? criterion.status : "unknown";
  const labels = { met: "Met", not_met: "Not met", unknown: "Unknown" };
  const patientEvidence = criterion.patient_evidence?.length
    ? criterion.patient_evidence.map((item) => `<span>${escapeHtml(item)}</span>`).join("")
    : '<em>Not found in packet</em>';
  const citations = criterion.guideline_citations?.length
    ? criterion.guideline_citations.map((item) => `<span>${escapeHtml(item)}</span>`).join("")
    : '<em>No valid citation</em>';
  return `<tr class="criterion-row ${status}">
    <td><strong>${escapeHtml(criterion.criterion)}</strong><small>${Math.round((criterion.confidence || 0) * 100)}% assessment confidence</small></td>
    <td><span class="criterion-status ${status}"><i></i>${labels[status]}</span></td>
    <td class="stacked-cell">${patientEvidence}</td>
    <td class="stacked-cell citation-cell">${citations}</td>
    <td class="rationale-cell">${escapeHtml(criterion.rationale || "Human review required.")}</td>
  </tr>`;
}

function renderPassage(passage) {
  return `<article class="passage-card">
    <div><strong>${escapeHtml(passage.section)}</strong><span>${escapeHtml(passage.citation)}</span><b>${Math.round((passage.score || 0) * 100)}% match</b></div>
    <p>${escapeHtml(passage.text)}</p>
  </article>`;
}

function renderEntityRow(entity, sectionNames) {
  const band = confidenceBand(entity.confidence);
  const corrected = entity.verification_status === "corrected";
  const confidenceLabel = corrected ? "Verified" : `${Math.round(entity.confidence * 100)}%`;
  const evidenceText = entity.evidence_text || "No source span";
  const alternatives = entity.alternatives?.length
    ? ` · Alternatives: ${entity.alternatives.join(" · ")}`
    : "";
  return `<tr class="entity-row ${corrected ? "corrected" : band}">
    <td class="field-cell"><strong>${escapeHtml(entity.display_name)}</strong><small>${escapeHtml(sectionNames[entity.section] || entity.section)}</small></td>
    <td class="value-cell"><input id="entity-${escapeAttribute(entity.entity_id)}" value="${escapeAttribute(entity.value || "")}" aria-label="${escapeAttribute(entity.display_name)}" /></td>
    <td><span class="confidence-pill ${band}"><i></i>${confidenceLabel}</span></td>
    <td class="evidence-cell evidence-column" title="${escapeAttribute(`${evidenceText}${alternatives}`)}"><span>“${escapeHtml(evidenceText)}”</span><small>${escapeHtml(entity.document_id)}${entity.notes ? ` · ${escapeHtml(entity.notes)}` : ""}</small></td>
    <td class="action-cell"><button class="verify-button" data-entity-id="${escapeAttribute(entity.entity_id)}">${corrected ? "Update" : "Verify"}</button></td>
  </tr>`;
}

async function saveCorrection(entityId) {
  const input = document.getElementById(`entity-${entityId}`);
  if (!input) return;
  const response = await fetch(`/api/jobs/${state.jobId}/corrections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      entity_id: entityId,
      corrected_value: input.value,
      reviewer: "Clinical reviewer",
      verified: true,
    }),
  });
  const result = await response.json();
  if (!response.ok) {
    showToast(result.detail || "Could not save correction", true);
    return;
  }
  state.job = result;
  renderExtraction(result.extraction);
  showToast("Reviewer correction saved.");
}

function renderActivityEvent(event) {
  const payload = event.payload || {};
  const kindLabels = {
    stage: "Workflow",
    tool_call: "Tool call",
    tool_result: "Tool result",
    agent_message: "Agent",
    mode: "Runtime",
    error: "Error",
    reviewer_action: "Reviewer",
  };
  let message = payload.message || "";
  if (event.event_type === "tool_call") {
    const fileText = payload.arguments?.files ? ` · ${payload.arguments.files.join(", ")}` : "";
    message = `<code>${escapeHtml(payload.tool)}</code> started${escapeHtml(fileText)}`;
  }
  if (event.event_type === "tool_result") {
    const count = payload.document_count ?? payload.entity_count ?? payload.criteria_count ?? "";
    const noun = payload.document_count !== undefined ? "documents" : payload.entity_count !== undefined ? "entities" : payload.criteria_count !== undefined ? "criteria" : "";
    const confidence = payload.average_confidence ?? payload.overall_confidence;
    message = `<code>${escapeHtml(payload.tool || "tool")}</code> completed${count !== "" ? ` · ${count} ${noun}` : ""}${confidence !== undefined ? ` · ${Math.round(confidence * 100)}% confidence` : ""}`;
  }
  const icon = event.event_type === "tool_result" ? "✓" : event.event_type === "error" ? "!" : event.event_type === "tool_call" ? "↗" : "•";
  const item = document.createElement("div");
  item.className = `activity-item ${event.event_type}`;
  item.innerHTML = `<div class="activity-icon">${icon}</div><div class="activity-kind">${escapeHtml(kindLabels[event.event_type] || event.event_type)}</div><div class="activity-message">${message}</div><div class="activity-time">${formatTime(event.created_at)}</div>`;
  $("#activityFeed").appendChild(item);
  $("#activityFeed").scrollTop = $("#activityFeed").scrollHeight;
}

function confidenceBand(value) {
  if (value >= 0.85) return "high";
  if (value >= 0.65) return "medium";
  return "low";
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatTime(value) {
  try { return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
  catch { return ""; }
}

function showToast(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = `toast visible${isError ? " error" : ""}`;
  window.setTimeout(() => toast.classList.remove("visible"), 2600);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}

function escapeAttribute(value) { return escapeHtml(value); }
