"""Criterion evaluation and the decision.

This module is pure: no I/O, no model calls, no clock reads except the one passed
in. That is deliberate. The decision a payer acts on is the part that must be
inspectable, testable, and identical every time it runs on the same inputs.

Two invariants hold here and nowhere else in the system:

1. `Outcome` has two members. There is no denial value to return, so no code path,
   prompt injection, or model error can produce one.
2. Absent evidence is UNKNOWN, never NOT_MET. A gap in the chart routes the request
   to a reviewer; it never counts against the member.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field

from app.models import CriterionEvidence, CriterionStatus, OcrBatchResult
from app.providers import best_evidence_match
from app.registry import (
    AttestationPredicate,
    BooleanPredicate,
    Criterion,
    EnumPredicate,
    ExternalRefPredicate,
    Exclusion,
    RouteResult,
    TemporalPredicate,
    ThresholdPredicate,
)
from app.temporal import parse_date, shift


class Outcome(StrEnum):
    """The only outcomes this system can produce.

    A non-affirmation requires a licensed clinical reviewer, so the type has no
    member for it. `tests/test_adjudication.py` asserts len(Outcome) == 2.
    """

    PROVISIONAL_AFFIRMATION = "provisional_affirmation"
    REFER_TO_HUMAN = "refer_to_human"


TRUE_WORDS = {"yes", "true", "y", "documented", "completed", "cleared", "present"}
FALSE_WORDS = {"no", "false", "n", "not documented", "absent", "none documented"}
NEVER_WORDS = {"never", "never used", "no history", "lifetime non-user", "non-smoker"}


class DecisionContext(BaseModel):
    """Dates a temporal window can be anchored to."""

    order_date: date | None = None
    planned_surgery_date: date | None = None
    decision_date: date


class CriterionResult(BaseModel):
    criterion_id: str
    status: CriterionStatus
    reason: str
    clause_text: str
    page: int
    predicate_type: str
    evidence: CriterionEvidence | None = None
    confidence: float = Field(ge=0, le=1)
    missing: str | None = None
    """When UNKNOWN because information is obtainable, what to request and from whom."""


class Adjudication(BaseModel):
    outcome: Outcome
    confidence: float = Field(ge=0, le=1)
    rationale: str
    criteria: list[CriterionResult] = Field(default_factory=list)
    refer_reasons: list[str] = Field(default_factory=list)
    policy_id: str | None = None
    policy_title: str | None = None
    policy_effective_date: str | None = None
    pathway_id: str | None = None
    procedure_id: str | None = None
    decision_date: date
    threshold: float


# -- predicate evaluation -----------------------------------------------------


def evaluate_criterion(
    criterion: Criterion,
    evidence: CriterionEvidence | None,
    context: DecisionContext,
    ocr: OcrBatchResult | None = None,
) -> CriterionResult:
    """Decide one criterion from its predicate and the located evidence."""

    predicate = criterion.predicate
    base = {
        "criterion_id": criterion.id,
        "clause_text": criterion.text,
        "page": criterion.page,
        "predicate_type": predicate.type,
        "evidence": evidence,
    }

    if isinstance(predicate, ExternalRefPredicate):
        return CriterionResult(
            **base,
            status=CriterionStatus.UNKNOWN,
            reason=(
                f"This policy delegates the criteria to {predicate.ref}, which is "
                "not held by this system. A reviewer applies them."
            ),
            confidence=0.0,
        )

    if evidence is None or not evidence.found:
        return CriterionResult(
            **base,
            status=CriterionStatus.UNKNOWN,
            reason="No evidence in the submitted packet addresses this criterion.",
            confidence=0.0,
            missing=f"Ask the ordering provider for {_requestable(predicate, criterion.text)}.",
        )

    if ocr is not None and not evidence_is_traceable(evidence, ocr):
        return CriterionResult(
            **base,
            status=CriterionStatus.UNKNOWN,
            reason=(
                "The cited evidence span was not found in the document transcript, "
                "so it cannot be relied on."
            ),
            confidence=0.0,
            missing=(
                f"Ask the ordering provider to re-submit a legible copy of the "
                f"document containing {_requestable(predicate, criterion.text)}."
            ),
        )

    if isinstance(predicate, ThresholdPredicate):
        return _evaluate_threshold(predicate, evidence, base)
    if isinstance(predicate, TemporalPredicate):
        return _evaluate_temporal(predicate, evidence, context, base)
    if isinstance(predicate, EnumPredicate):
        return _evaluate_enum(predicate, evidence, base)
    return _evaluate_boolean(predicate, evidence, base)


def _requestable(predicate, clause_text: str) -> str:
    """Phrase what a submitter would have to provide to resolve an UNKNOWN."""

    if isinstance(predicate, ThresholdPredicate):
        return f"a documented {predicate.field.replace('_', ' ')} value"
    if isinstance(predicate, TemporalPredicate):
        return f"the {predicate.anchor.replace('_', ' ')}"
    if isinstance(predicate, EnumPredicate):
        return f"documentation of the {predicate.field.replace('_', ' ')}"
    clipped = clause_text if len(clause_text) <= 100 else clause_text[:97] + "..."
    if isinstance(predicate, AttestationPredicate):
        return f'a signed attestation addressing: "{clipped}"'
    return f'documentation addressing: "{clipped}"'


def evidence_is_traceable(evidence: CriterionEvidence, ocr: OcrBatchResult) -> bool:
    """An evidence span must be quotable from the transcript it names."""

    if not evidence.evidence_text.strip():
        return False
    for document in ocr.documents:
        if evidence.document_id and document.document_id != evidence.document_id:
            continue
        if best_evidence_match(evidence.evidence_text, document.blocks) is not None:
            return True
    return False


def _evaluate_threshold(
    predicate: ThresholdPredicate, evidence: CriterionEvidence, base: dict
) -> CriterionResult:
    number = _first_number(evidence.value)
    if number is None:
        return CriterionResult(
            **base,
            status=CriterionStatus.UNKNOWN,
            reason=f"No numeric value for {predicate.field} was found in the packet.",
            confidence=0.0,
            missing=(
                f"Ask the ordering provider for a documented "
                f"{predicate.field.replace('_', ' ')} value."
            ),
        )
    band = predicate.unknown_band
    if band and band[0] <= number < band[1]:
        return CriterionResult(
            **base,
            status=CriterionStatus.UNKNOWN,
            reason=(
                f"{predicate.field} is {number:g}, inside the {band[0]:g}-{band[1]:g} "
                "band where the clause depends on a fact this system will not infer. "
                "A reviewer applies the alternative branch."
            ),
            confidence=0.0,
        )
    satisfied = _compare(number, predicate.op, predicate.value)
    return CriterionResult(
        **base,
        status=CriterionStatus.MET if satisfied else CriterionStatus.NOT_MET,
        reason=(
            f"{predicate.field} is {number:g}; the clause requires "
            f"{predicate.op} {predicate.value:g}."
        ),
        confidence=0.9,
    )


def _evaluate_temporal(
    predicate: TemporalPredicate,
    evidence: CriterionEvidence,
    context: DecisionContext,
    base: dict,
) -> CriterionResult:
    """Evaluate a window against every reading of an ambiguous date.

    A status is returned only when all readings agree. If Sep 3rd meets the window
    and Mar 9th does not, the honest answer is that we do not know.
    """

    raw = evidence.value_date or evidence.value
    if predicate.none_ok and raw and raw.strip().casefold() in NEVER_WORDS:
        return CriterionResult(
            **base,
            status=CriterionStatus.MET,
            reason="The packet states the event never occurred, which satisfies the lookback.",
            confidence=0.85,
        )

    reference = getattr(context, predicate.relative_to, None)
    if reference is None:
        return CriterionResult(
            **base,
            status=CriterionStatus.UNKNOWN,
            reason=(
                f"The window is anchored to the {predicate.relative_to.replace('_', ' ')}, "
                "which is not present in the packet."
            ),
            confidence=0.0,
            missing=(
                f"Ask the submitter for the {predicate.relative_to.replace('_', ' ')} "
                "of this request."
            ),
        )

    resolved = parse_date(raw)
    if resolved is None:
        return CriterionResult(
            **base,
            status=CriterionStatus.UNKNOWN,
            reason=(
                f"No usable date for {predicate.anchor.replace('_', ' ')} was found "
                "in the packet."
            ),
            confidence=0.0,
            missing=(
                f"Ask the ordering provider for the "
                f"{predicate.anchor.replace('_', ' ')}."
            ),
        )

    verdicts = {
        candidate: _window_holds(candidate, predicate, reference)
        for candidate in resolved.candidates
    }
    elapsed = ", ".join(
        f"{candidate.isoformat()} ({_describe_gap(candidate, reference)})"
        for candidate in resolved.candidates
    )
    if len(set(verdicts.values())) > 1:
        return CriterionResult(
            **base,
            status=CriterionStatus.UNKNOWN,
            reason=(
                f"'{resolved.raw}' can be read as {elapsed}. The readings disagree "
                "about this window, so the date must be confirmed."
            ),
            confidence=0.0,
            missing=(
                f"Ask the ordering provider to confirm the "
                f"{predicate.anchor.replace('_', ' ')} written '{resolved.raw}'; "
                "it can be read more than one way."
            ),
        )

    satisfied = next(iter(verdicts.values()))
    window = predicate.duration.removeprefix("P").lower()
    return CriterionResult(
        **base,
        status=CriterionStatus.MET if satisfied else CriterionStatus.NOT_MET,
        reason=(
            f"{predicate.anchor.replace('_', ' ')} {elapsed} against "
            f"{predicate.relative_to.replace('_', ' ')} {reference.isoformat()}; "
            f"the clause requires {window}."
        ),
        confidence=0.85 if not resolved.ambiguous else 0.7,
    )


def _window_holds(
    anchor: date, predicate: TemporalPredicate, reference: date
) -> bool:
    """True when `anchor` sits far enough from `reference` to satisfy the clause."""

    boundary = shift(anchor, predicate.duration)
    if predicate.op == ">=":
        return boundary <= reference
    if predicate.op == ">":
        return boundary < reference
    if predicate.op == "<=":
        return boundary >= reference
    return boundary > reference


def _describe_gap(anchor: date, reference: date) -> str:
    months = (reference.year - anchor.year) * 12 + reference.month - anchor.month
    if reference.day < anchor.day:
        months -= 1
    if months < 0:
        return "in the future"
    if months < 2:
        return f"{(reference - anchor).days} days earlier"
    if months < 12:
        return f"{months} months earlier"
    years, remainder = divmod(months, 12)
    if remainder:
        return f"{years}y {remainder}m earlier"
    return f"{years} years earlier"


def _evaluate_enum(
    predicate: EnumPredicate, evidence: CriterionEvidence, base: dict
) -> CriterionResult:
    value = (evidence.value or "").strip()
    if not value:
        return CriterionResult(
            **base,
            status=CriterionStatus.UNKNOWN,
            reason=f"No value for {predicate.field} was found in the packet.",
            confidence=0.0,
            missing=(
                f"Ask the ordering provider for documentation of the "
                f"{predicate.field.replace('_', ' ')}."
            ),
        )
    allowed = {item.casefold() for item in predicate.allowed}
    satisfied = value.casefold() in allowed
    return CriterionResult(
        **base,
        status=CriterionStatus.MET if satisfied else CriterionStatus.NOT_MET,
        reason=(
            f"{predicate.field} is '{value}'; the clause allows "
            f"{', '.join(predicate.allowed)}."
        ),
        confidence=0.85,
    )


def _evaluate_boolean(
    predicate: BooleanPredicate | AttestationPredicate,
    evidence: CriterionEvidence,
    base: dict,
) -> CriterionResult:
    value = (evidence.value or "").strip().casefold()
    kind = "attestation" if predicate.type == "attestation" else "documentation"
    if value in TRUE_WORDS:
        return CriterionResult(
            **base,
            status=CriterionStatus.MET,
            reason=f"The packet documents this requirement ({kind} found).",
            confidence=0.8,
        )
    if value in FALSE_WORDS:
        return CriterionResult(
            **base,
            status=CriterionStatus.NOT_MET,
            reason=f"The packet states this requirement is not satisfied ({kind}).",
            confidence=0.8,
        )
    return CriterionResult(
        **base,
        status=CriterionStatus.UNKNOWN,
        reason=(
            f"The packet does not clearly state whether this requirement is met; "
            f"an explicit {kind} is required."
        ),
        confidence=0.0,
        missing=f"Ask the ordering provider for {_requestable(predicate, base['clause_text'])}.",
    )


def _first_number(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(match.group()) if match else None


def _compare(left: float, op: str, right: float) -> bool:
    return {
        ">=": left >= right,
        ">": left > right,
        "<=": left <= right,
        "<": left < right,
        "==": left == right,
    }[op]


# -- the decision -------------------------------------------------------------


def decide(
    results: list[CriterionResult],
    route: RouteResult | None,
    decision_date: date,
    threshold: float = 0.7,
    no_route_reason: str | None = None,
) -> Adjudication:
    """Compute the outcome from evaluated criteria.

    Affirmation requires every criterion met, with evidence, above the confidence
    threshold. Everything else - a gap, a failure, an exclusion, a delegated
    criteria set, an unrouted request - is a referral to a human. There is no
    branch that returns a denial because `Outcome` cannot express one.
    """

    common = {
        "criteria": results,
        "decision_date": decision_date,
        "threshold": threshold,
        "policy_id": route.policy_id if route else None,
        "policy_title": route.policy_title if route else None,
        "policy_effective_date": route.effective_date if route else None,
        "pathway_id": route.pathway_id if route else None,
        "procedure_id": route.procedure_id if route else None,
    }

    if route is None:
        reason = no_route_reason or "No applicable guideline was found for this request."
        return Adjudication(
            **common,
            outcome=Outcome.REFER_TO_HUMAN,
            confidence=0.0,
            refer_reasons=[reason],
            rationale=(
                f"{reason} The request is referred to a clinical reviewer; no "
                "coverage determination was made."
            ),
        )

    if route.excluded_by is not None:
        return _excluded(route.excluded_by, common)

    if route.not_covered:
        reason = (
            f"The requested code is listed as not covered under the "
            f"{route.pathway_id} pathway of {route.policy_id}."
        )
        return Adjudication(
            **common,
            outcome=Outcome.REFER_TO_HUMAN,
            confidence=0.0,
            refer_reasons=[reason],
            rationale=(
                f"{reason} A benefit exclusion is a coverage determination that "
                "requires a licensed reviewer; this system does not issue one."
            ),
        )

    blocking = [
        result for result in results if result.status is not CriterionStatus.MET
    ]
    met = [result for result in results if result.status is CriterionStatus.MET]
    lowest = min((result.confidence for result in met), default=0.0)

    if not results:
        reason = "No criteria were evaluated for this request."
        return Adjudication(
            **common,
            outcome=Outcome.REFER_TO_HUMAN,
            confidence=0.0,
            refer_reasons=[reason],
            rationale=f"{reason} The request is referred to a clinical reviewer.",
        )

    if not blocking and lowest >= threshold:
        return Adjudication(
            **common,
            outcome=Outcome.PROVISIONAL_AFFIRMATION,
            confidence=lowest,
            refer_reasons=[],
            rationale=_rationale(route, results, [], Outcome.PROVISIONAL_AFFIRMATION),
        )

    refer_reasons = [
        f"{result.criterion_id} ({result.status.value}): {result.reason}"
        for result in blocking
    ]
    if not blocking and lowest < threshold:
        refer_reasons.append(
            f"Every criterion is met, but the weakest supporting evidence scores "
            f"{lowest:.2f}, below the {threshold:.2f} auto-affirmation threshold."
        )
    return Adjudication(
        **common,
        outcome=Outcome.REFER_TO_HUMAN,
        confidence=lowest if not blocking else 0.0,
        refer_reasons=refer_reasons,
        rationale=_rationale(route, results, blocking, Outcome.REFER_TO_HUMAN),
    )


def _excluded(exclusion: Exclusion, common: dict) -> Adjudication:
    reason = (
        f"The requested procedure appears in exclusion {exclusion.id} "
        f"(page {exclusion.page}): \"{exclusion.text[:120]}\""
    )
    return Adjudication(
        **common,
        outcome=Outcome.REFER_TO_HUMAN,
        confidence=0.0,
        refer_reasons=[reason],
        rationale=(
            f"{reason} An exclusion points toward non-coverage, which only a "
            "licensed clinical reviewer may determine. The request is referred "
            "with the clause attached."
        ),
    )


def _rationale(
    route: RouteResult,
    results: list[CriterionResult],
    blocking: list[CriterionResult],
    outcome: Outcome,
) -> str:
    """Compose the reviewer-facing explanation from the evaluated criteria.

    Written by template rather than by a model: the rationale must say exactly what
    the decision function did, and a generated summary can drift from it.
    """

    met = sum(1 for result in results if result.status is CriterionStatus.MET)
    header = (
        f"Request: {route.procedure_id} under the {route.pathway_id} pathway of "
        f"{route.policy_id} ({route.policy_title}, effective "
        f"{route.effective_date}). {met} of {len(results)} criteria met."
    )
    if outcome is Outcome.PROVISIONAL_AFFIRMATION:
        return (
            f"{header} Every criterion is supported by cited patient evidence and a "
            "cited policy clause, so the request is provisionally affirmed. A "
            "reviewer may still open it; nothing here is a denial."
        )
    lines = [
        f"- {result.criterion_id} ({result.status.value}, p. {result.page}): "
        f"{result.reason}"
        for result in blocking[:8]
    ]
    text = (
        f"{header} The request is referred to a licensed clinical reviewer because "
        f"the following criteria are not established:\n" + "\n".join(lines)
    )
    requests = [result.missing for result in blocking if result.missing]
    if requests:
        text += "\n\nTo resolve before re-submission:\n" + "\n".join(
            f"- {request}" for request in requests[:8]
        )
    return text
