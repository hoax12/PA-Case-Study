/* The adjudication half of the review screen: the outcome banner, the criteria
   matrix, and the two ways a reviewer disagrees with it - per clause, and on the
   final outcome. Shares globals with app.js, which loads first. */

const STATUS_LABELS = { met: "Met", not_met: "Not met", unknown: "Unknown" };

function renderAdjudication(adjudication) {
  const criteria = adjudication.criteria || [];
  const affirmed = adjudication.outcome === "provisional_affirmation";
  $("#adjudicationPanel").classList.remove("hidden");

  const banner = $("#outcomeBanner");
  banner.className = `outcome-banner ${affirmed ? "affirmed" : "referred"}`;
  banner.querySelector(".outcome-mark").textContent = affirmed ? "✓" : "→";
  $("#outcomeLabel").textContent = affirmed ? "Provisional affirmation" : "Refer to human";
  $("#outcomeRationale").textContent = adjudication.rationale || "";
  $("#outcomeConfidence").textContent = `${Math.round((adjudication.confidence || 0) * 100)}%`;
  $("#outcomeThreshold").textContent = `auto-affirm at ${Math.round((adjudication.threshold || 0) * 100)}%`;

  const met = criteria.filter((item) => item.status === "met").length;
  $("#metricCriteria").textContent = criteria.length ? `${met}/${criteria.length}` : "—";
  $("#policyNumber").textContent = `Policy ${adjudication.policy_id || "—"}`;
  $("#policyEffective").textContent = `Effective ${adjudication.policy_effective_date || "—"}`;
  $("#policyPathway").textContent = `Pathway ${adjudication.pathway_id || "—"}`;

  // A referral is only useful if it says what is blocking it, and what to
  // request, from whom, to unblock it.
  const reasons = adjudication.refer_reasons || [];
  const missing = criteria
    .filter((item) => item.missing)
    .map((item) => `<li><strong>${escapeHtml(item.criterion_id)}</strong>: ${escapeHtml(item.missing)}</li>`);
  const reasonBox = $("#referReasons");
  reasonBox.classList.toggle("hidden", reasons.length === 0 && missing.length === 0);
  reasonBox.innerHTML =
    (reasons.length
      ? `<strong>${reasons.length} reason${reasons.length === 1 ? "" : "s"} this needs a reviewer</strong><ul>${reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>`
      : "") +
    (missing.length
      ? `<strong>Missing information: what to request</strong><ul>${missing.join("")}</ul>`
      : "");

  const overrides = new Map(
    (state.job?.reviewer_decisions || [])
      .filter((decision) => decision.criterion_id)
      .map((decision) => [decision.criterion_id, decision]),
  );
  $("#criteriaRows").innerHTML = criteria.length
    ? criteria.map((criterion) => renderCriterionRow(criterion, overrides.get(criterion.criterion_id))).join("")
    : '<tr><td colspan="6" class="empty-table">No guideline governs this request, so no clause was scored. A reviewer must inspect the packet directly.</td></tr>';
  $$("[data-agree]").forEach((button) =>
    button.addEventListener("click", () => recordCriterion(button.dataset.agree, button.dataset.status, button.dataset.status)),
  );
  $$("[data-override]").forEach((select) =>
    select.addEventListener("change", () => {
      if (select.value) recordCriterion(select.dataset.override, select.dataset.status, select.value);
    }),
  );
}

function renderCriterionRow(criterion, override) {
  const status = STATUS_LABELS[criterion.status] ? criterion.status : "unknown";
  const evidence = criterion.evidence;
  // An unquotable span never reaches the decision, so a row with no evidence is
  // saying the packet is silent - not that the model failed to answer.
  const evidenceCell = evidence?.found
    ? `<span>“${escapeHtml(evidence.evidence_text || evidence.value || "")}”</span><small>${escapeHtml(evidence.document_id || "")}${evidence.page ? ` · p.${evidence.page}` : ""}</small>`
    : "<em>Nothing in the packet addresses this clause</em>";
  const reviewed = override
    ? `<span class="reviewer-mark ${override.reviewer_status === criterion.status ? "agreed" : "overridden"}">${override.reviewer_status === criterion.status ? "Agreed" : `Overridden → ${escapeHtml(STATUS_LABELS[override.reviewer_status] || override.reviewer_status)}`}</span>`
    : `<button type="button" class="agree-button" data-agree="${escapeAttribute(criterion.criterion_id)}" data-status="${status}">Agree</button>
       <select class="override-select" data-override="${escapeAttribute(criterion.criterion_id)}" data-status="${status}" aria-label="Override ${escapeAttribute(criterion.criterion_id)}">
         <option value="">Override…</option>
         ${["met", "not_met", "unknown"].filter((value) => value !== status).map((value) => `<option value="${value}">${STATUS_LABELS[value]}</option>`).join("")}
       </select>`;
  return `<tr class="criterion-row ${status}">
    <td><strong>${escapeHtml(criterion.criterion_id)}</strong><small>${escapeHtml(criterion.predicate_type)} · ${Math.round((criterion.confidence || 0) * 100)}% confidence</small></td>
    <td><span class="criterion-status ${status}"><i></i>${STATUS_LABELS[status]}</span></td>
    <td class="stacked-cell">${evidenceCell}</td>
    <td class="stacked-cell citation-cell"><span>“${escapeHtml(criterion.clause_text)}”</span><small>page ${criterion.page}</small></td>
    <td class="rationale-cell">${escapeHtml(criterion.reason || "")}</td>
    <td class="reviewer-cell">${reviewed}</td>
  </tr>`;
}

async function recordCriterion(criterionId, systemStatus, reviewerStatus) {
  await postDecision(
    {
      criterion_id: criterionId,
      system_status: systemStatus,
      reviewer_status: reviewerStatus,
      reviewer: $("#reviewerName")?.value || "Clinical reviewer",
    },
    systemStatus === reviewerStatus
      ? `Agreement recorded for ${criterionId}.`
      : `Override recorded for ${criterionId}.`,
  );
}

async function submitFinalDecision(event) {
  event.preventDefault();
  await postDecision(
    {
      final_outcome: $("#finalOutcome").value,
      reason_code: $("#reasonCode").value || null,
      note: $("#decisionNote").value || null,
      reviewer: $("#reviewerName").value,
    },
    "Final reviewer decision recorded.",
  );
}

async function postDecision(body, successMessage) {
  if (!state.jobId) return;
  const response = await fetch(`/api/jobs/${state.jobId}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    showToast(result.detail || "Could not record the decision", true);
    return;
  }
  state.job = result;
  if (result.adjudication) renderAdjudication(result.adjudication);
  renderDecisionLog();
  showToast(successMessage);
}

function renderDecisionLog() {
  const decisions = (state.job?.reviewer_decisions || []).filter((item) => item.final_outcome);
  const log = $("#decisionLog");
  if (!decisions.length) {
    log.textContent = "";
    return;
  }
  const latest = decisions[decisions.length - 1];
  log.textContent = `Recorded: ${latest.final_outcome} by ${latest.reviewer}`;
}
