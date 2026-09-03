"""Tests for the part of the system a payer would audit first."""

import random
from datetime import date

import pytest

from app.adjudication import (
    Adjudication,
    CriterionResult,
    DecisionContext,
    Outcome,
    decide,
    evaluate_criterion,
    parse_date,
    shift,
)
from app.models import CriterionEvidence, CriterionStatus, OcrBatchResult, OcrBlock, OcrDocument
from app.registry import CriteriaRegistry, RouteResult
from app.config import PROJECT_ROOT


ORDER_DATE = date(2026, 9, 1)
SURGERY_DATE = date(2026, 10, 6)
DECISION_DATE = date(2026, 9, 1)


@pytest.fixture(scope="module")
def registry() -> CriteriaRegistry:
    return CriteriaRegistry(PROJECT_ROOT / "knowledge" / "policies")


@pytest.fixture
def context() -> DecisionContext:
    return DecisionContext(
        order_date=ORDER_DATE,
        planned_surgery_date=SURGERY_DATE,
        decision_date=DECISION_DATE,
    )


def route() -> RouteResult:
    return RouteResult(
        policy_id="MGB-008",
        policy_title="MGB Bariatric Surgery",
        effective_date="2026-07-01",
        pathway_id="commercial",
        procedure_id="TORe",
        criteria_ref="TORe",
        pa_required=True,
    )


def result(status: CriterionStatus, confidence: float = 0.9) -> CriterionResult:
    return CriterionResult(
        criterion_id="MGB-008.TORe.1",
        status=status,
        reason="test",
        clause_text="text",
        page=4,
        predicate_type="boolean",
        confidence=confidence,
    )


def evidence(criterion_id: str, **fields) -> CriterionEvidence:
    return CriterionEvidence(
        criterion_id=criterion_id,
        found=fields.pop("found", True),
        evidence_text=fields.pop("evidence_text", "documented in the packet"),
        document_id=fields.pop("document_id", "document-1"),
        **fields,
    )


# -- the no-denial invariant --------------------------------------------------


def test_outcome_type_cannot_express_a_denial() -> None:
    """The mechanical guarantee, asserted rather than promised."""

    assert len(Outcome) == 2
    assert {member.value for member in Outcome} == {
        "provisional_affirmation",
        "refer_to_human",
    }
    assert not any(
        word in member.value
        for member in Outcome
        for word in ("deny", "denial", "non_affirm", "reject")
    )


def test_no_matrix_can_produce_anything_but_the_two_outcomes() -> None:
    """Fuzz the decision function; the outcome set must stay closed."""

    random.seed(20260903)
    statuses = list(CriterionStatus)
    for _ in range(500):
        results = [
            result(random.choice(statuses), random.random())
            for _ in range(random.randint(0, 16))
        ]
        adjudication = decide(results, route(), DECISION_DATE, threshold=0.7)
        assert adjudication.outcome in set(Outcome)
        assert isinstance(adjudication, Adjudication)
        # An affirmation is only ever reachable with a fully met matrix.
        if adjudication.outcome is Outcome.PROVISIONAL_AFFIRMATION:
            assert all(item.status is CriterionStatus.MET for item in results)
            assert results


def test_any_unknown_or_not_met_refers() -> None:
    met = [result(CriterionStatus.MET) for _ in range(5)]
    assert (
        decide(met, route(), DECISION_DATE).outcome
        is Outcome.PROVISIONAL_AFFIRMATION
    )
    for blocking in (CriterionStatus.UNKNOWN, CriterionStatus.NOT_MET):
        mixed = [*met, result(blocking)]
        assert decide(mixed, route(), DECISION_DATE).outcome is Outcome.REFER_TO_HUMAN


def test_low_confidence_refers_even_when_every_criterion_is_met() -> None:
    weak = [result(CriterionStatus.MET, confidence=0.55) for _ in range(3)]
    adjudication = decide(weak, route(), DECISION_DATE, threshold=0.7)
    assert adjudication.outcome is Outcome.REFER_TO_HUMAN
    assert "below the 0.70" in adjudication.refer_reasons[0]


def test_no_route_refers_with_a_stated_reason() -> None:
    adjudication = decide([], None, DECISION_DATE, no_route_reason="No policy covers X.")
    assert adjudication.outcome is Outcome.REFER_TO_HUMAN
    assert adjudication.refer_reasons == ["No policy covers X."]


def test_empty_matrix_refers() -> None:
    assert decide([], route(), DECISION_DATE).outcome is Outcome.REFER_TO_HUMAN


def test_exclusion_refers_rather_than_denying(registry: CriteriaRegistry) -> None:
    excluded = registry.route("commercial", "gastric balloon")
    adjudication = decide([], excluded, DECISION_DATE)
    assert adjudication.outcome is Outcome.REFER_TO_HUMAN
    assert "MGB-008.EXCL.2" in adjudication.refer_reasons[0]
    assert "licensed clinical reviewer" in adjudication.rationale


# -- date resolution ----------------------------------------------------------


def test_iso_date_has_one_reading() -> None:
    resolved = parse_date("2025-03-10")
    assert resolved.candidates == [date(2025, 3, 10)]
    assert resolved.ambiguous is False


def test_slash_date_keeps_both_readings() -> None:
    resolved = parse_date("02/04/14")
    assert resolved.ambiguous is True
    assert set(resolved.candidates) == {date(2014, 2, 4), date(2014, 4, 2)}


def test_impossible_reading_is_dropped() -> None:
    resolved = parse_date("10/25/2025")
    assert resolved.candidates == [date(2025, 10, 25)]
    assert resolved.ambiguous is False


def test_named_month_is_unambiguous() -> None:
    assert parse_date("March 10, 2025").candidates == [date(2025, 3, 10)]


def test_duration_shift_handles_month_ends() -> None:
    assert shift(date(2025, 1, 31), "P1M") == date(2025, 2, 28)
    assert shift(date(2025, 3, 10), "P1Y") == date(2026, 3, 10)
    assert shift(date(2026, 8, 25), "P6W") == date(2026, 10, 6)


# -- TORe.5, the temporal criterion end to end --------------------------------


def temporal_case(registry: CriteriaRegistry, raw_date: str, context: DecisionContext):
    criterion = registry.clause("MGB-008.TORe.5")
    return evaluate_criterion(
        criterion, evidence("MGB-008.TORe.5", value_date=raw_date), context
    )


def test_primary_surgery_more_than_a_year_before_the_order_is_met(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    outcome = temporal_case(registry, "2025-03-10", context)
    assert outcome.status is CriterionStatus.MET
    assert "1y" in outcome.reason or "years" in outcome.reason


def test_primary_surgery_eight_months_before_the_order_is_not_met(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    outcome = temporal_case(registry, "2026-01-02", context)
    assert outcome.status is CriterionStatus.NOT_MET
    assert "7 months earlier" in outcome.reason or "8 months earlier" in outcome.reason


def test_ambiguous_date_whose_readings_disagree_is_unknown(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    """09/03/25 is Sep 3rd (two days short of a year) or Mar 9th (well past it)."""

    outcome = temporal_case(registry, "09/03/25", context)
    assert outcome.status is CriterionStatus.UNKNOWN
    assert "readings disagree" in outcome.reason
    assert "2025-09-03" in outcome.reason and "2025-03-09" in outcome.reason


def test_ambiguous_date_whose_readings_agree_still_decides(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    outcome = temporal_case(registry, "03/07/24", context)
    assert outcome.status is CriterionStatus.MET


def test_missing_date_is_unknown_never_not_met(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    criterion = registry.clause("MGB-008.TORe.5")
    outcome = evaluate_criterion(
        criterion, evidence("MGB-008.TORe.5", found=False), context
    )
    assert outcome.status is CriterionStatus.UNKNOWN
    assert "No evidence" in outcome.reason


def test_missing_anchor_date_is_unknown(registry: CriteriaRegistry) -> None:
    """A window with no reference date cannot be evaluated, so it is not."""

    criterion = registry.clause("MGB-008.TORe.5")
    outcome = evaluate_criterion(
        criterion,
        evidence("MGB-008.TORe.5", value_date="2025-03-10"),
        DecisionContext(decision_date=DECISION_DATE),
    )
    assert outcome.status is CriterionStatus.UNKNOWN
    assert "order date" in outcome.reason


# -- the tobacco lookback, anchored to the surgery date -----------------------


def test_recent_tobacco_use_misses_the_six_week_window(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    criterion = registry.clause("MGB-008.TORe.10")
    outcome = evaluate_criterion(
        criterion, evidence("MGB-008.TORe.10", value_date="2026-08-26"), context
    )
    assert outcome.status is CriterionStatus.NOT_MET


def test_never_used_tobacco_satisfies_the_lookback(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    criterion = registry.clause("MGB-008.TORe.10")
    outcome = evaluate_criterion(
        criterion, evidence("MGB-008.TORe.10", value="never"), context
    )
    assert outcome.status is CriterionStatus.MET


# -- thresholds, enums, and the protected-characteristic band -----------------


def test_bmi_above_the_threshold_is_met(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    criterion = registry.clause("MGB-008.TORe.4")
    outcome = evaluate_criterion(
        criterion, evidence("MGB-008.TORe.4", value="38.2"), context
    )
    assert outcome.status is CriterionStatus.MET


def test_bmi_inside_the_asian_descent_band_is_unknown_not_failed(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    """The alternative branch turns on ancestry, which we refuse to infer."""

    criterion = registry.clause("MGB-008.TORe.4")
    outcome = evaluate_criterion(
        criterion, evidence("MGB-008.TORe.4", value="33.1"), context
    )
    assert outcome.status is CriterionStatus.UNKNOWN
    assert "will not infer" in outcome.reason


def test_bmi_below_every_branch_is_not_met(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    criterion = registry.clause("MGB-008.TORe.4")
    outcome = evaluate_criterion(
        criterion, evidence("MGB-008.TORe.4", value="29"), context
    )
    assert outcome.status is CriterionStatus.NOT_MET


def test_wrong_primary_procedure_is_not_met(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    criterion = registry.clause("MGB-008.TORe.16")
    outcome = evaluate_criterion(
        criterion, evidence("MGB-008.TORe.16", value="Sleeve Gastrectomy"), context
    )
    assert outcome.status is CriterionStatus.NOT_MET
    assert "RYGB" in outcome.reason


def test_silence_on_an_attestation_is_unknown(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    criterion = registry.clause("MGB-008.TORe.14")
    outcome = evaluate_criterion(
        criterion, evidence("MGB-008.TORe.14", value="not addressed"), context
    )
    assert outcome.status is CriterionStatus.UNKNOWN


def test_delegated_criteria_are_never_evaluated(registry: CriteriaRegistry) -> None:
    routed = registry.route("commercial", "RYGB")
    criteria = registry.criteria_for(routed)
    outcome = evaluate_criterion(
        criteria[0], None, DecisionContext(decision_date=DECISION_DATE)
    )
    assert outcome.status is CriterionStatus.UNKNOWN
    assert "InterQual" in outcome.reason
    assert decide([outcome], routed, DECISION_DATE).outcome is Outcome.REFER_TO_HUMAN


# -- evidence traceability ----------------------------------------------------


def test_untraceable_evidence_cannot_satisfy_a_criterion(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    """A span that is not in the transcript is treated as absent, not as proof."""

    ocr = OcrBatchResult(
        documents=[
            OcrDocument(
                document_id="document-1",
                file_name="note.png",
                document_type="clinic note",
                raw_text="Height 68 in, weight 214 lb",
                overall_confidence=0.95,
                blocks=[
                    OcrBlock(
                        block_id="line-1",
                        document_id="document-1",
                        text="Height 68 in, weight 214 lb",
                        confidence=0.95,
                    )
                ],
            )
        ],
        average_confidence=0.95,
        provider="fixture",
    )
    criterion = registry.clause("MGB-008.TORe.4")
    invented = evidence(
        "MGB-008.TORe.4",
        value="38.2",
        evidence_text="BMI 38.2 documented at the pre-op visit",
    )
    outcome = evaluate_criterion(criterion, invented, context, ocr)
    assert outcome.status is CriterionStatus.UNKNOWN
    assert "not found in the document transcript" in outcome.reason


def test_quoted_evidence_is_accepted(
    registry: CriteriaRegistry, context: DecisionContext
) -> None:
    ocr = OcrBatchResult(
        documents=[
            OcrDocument(
                document_id="document-1",
                file_name="note.png",
                document_type="clinic note",
                raw_text="BMI 38.2 kg/m2 recorded 2026-08-20",
                overall_confidence=0.95,
                blocks=[
                    OcrBlock(
                        block_id="line-1",
                        document_id="document-1",
                        text="BMI 38.2 kg/m2 recorded 2026-08-20",
                        confidence=0.95,
                    )
                ],
            )
        ],
        average_confidence=0.95,
        provider="fixture",
    )
    criterion = registry.clause("MGB-008.TORe.4")
    quoted = evidence(
        "MGB-008.TORe.4", value="38.2", evidence_text="BMI 38.2 kg/m2"
    )
    outcome = evaluate_criterion(criterion, quoted, context, ocr)
    assert outcome.status is CriterionStatus.MET


# -- the rationale reflects the computation -----------------------------------


def test_rationale_names_every_blocking_clause() -> None:
    results = [
        result(CriterionStatus.MET),
        CriterionResult(
            criterion_id="MGB-008.TORe.5",
            status=CriterionStatus.NOT_MET,
            reason="primary surgery 7 months earlier",
            clause_text="The primary surgery was performed at least one year ago",
            page=4,
            predicate_type="temporal",
            confidence=0.85,
        ),
    ]
    adjudication = decide(results, route(), DECISION_DATE)
    assert adjudication.outcome is Outcome.REFER_TO_HUMAN
    assert "MGB-008.TORe.5" in adjudication.rationale
    assert "7 months earlier" in adjudication.rationale
    assert "1 of 2 criteria met" in adjudication.rationale
