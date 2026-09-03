"""The registry is the knowledge layer, so these tests guard citation integrity."""

import pytest
from pypdf import PdfReader

from app.config import PROJECT_ROOT
from app.registry import (
    CriteriaRegistry,
    ExternalRefPredicate,
    NoRouteReason,
    RouteResult,
    normalize_clause,
)


@pytest.fixture(scope="module")
def registry() -> CriteriaRegistry:
    return CriteriaRegistry(PROJECT_ROOT / "knowledge" / "policies")


@pytest.fixture(scope="module")
def pdf_pages() -> dict[int, str]:
    reader = PdfReader(PROJECT_ROOT / "knowledge" / "source" / "BariatricSurgery.pdf")
    return {
        number: normalize_clause(page.extract_text() or "")
        for number, page in enumerate(reader.pages, start=1)
    }


def test_every_clause_appears_verbatim_on_its_cited_page(
    registry: CriteriaRegistry, pdf_pages: dict[int, str]
) -> None:
    """A citation that does not match the source is worse than no citation.

    This is the check that keeps the authored YAML honest: if someone edits a
    clause, paraphrases it, or renumbers a page, the build fails.
    """

    drifted = []
    for criterion in registry.all_criteria:
        page_text = pdf_pages.get(criterion.page, "")
        if normalize_clause(criterion.text) not in page_text:
            drifted.append(f"{criterion.id} (page {criterion.page})")
    assert not drifted, f"Clause text not found on cited page: {drifted}"


def test_exclusion_text_appears_on_its_cited_page(
    registry: CriteriaRegistry, pdf_pages: dict[int, str]
) -> None:
    policy = registry.policies["MGB-008"]
    for exclusion in policy.exclusions:
        assert normalize_clause(exclusion.text) in pdf_pages[exclusion.page]


def test_criterion_ids_are_unique_and_pages_are_operative(
    registry: CriteriaRegistry,
) -> None:
    ids = [criterion.id for criterion in registry.all_criteria]
    assert len(ids) == len(set(ids))
    assert all(1 <= criterion.page <= 7 for criterion in registry.all_criteria)


def test_commercial_tore_routes_to_sixteen_criteria(registry: CriteriaRegistry) -> None:
    route = registry.route("commercial", "TORe")
    assert isinstance(route, RouteResult)
    assert route.criteria_ref == "TORe"
    assert route.pathway_id == "commercial"
    criteria = registry.criteria_for(route)
    assert len(criteria) == 16
    assert criteria[4].id == "MGB-008.TORe.5"


def test_routing_accepts_a_cpt_code(registry: CriteriaRegistry) -> None:
    route = registry.route("commercial", "C9785")
    assert isinstance(route, RouteResult)
    assert route.procedure_id == "TORe"


def test_primary_rygb_is_delegated_not_adjudicated(registry: CriteriaRegistry) -> None:
    """The policy sends adult primary surgery to InterQual, which we do not hold."""

    route = registry.route("commercial", "RYGB")
    assert isinstance(route, RouteResult)
    criteria = registry.criteria_for(route)
    assert len(criteria) == 1
    assert isinstance(criteria[0].predicate, ExternalRefPredicate)
    assert "InterQual" in criteria[0].predicate.ref


def test_medicare_advantage_is_delegated_to_cms(registry: CriteriaRegistry) -> None:
    route = registry.route("Medicare Advantage", "TORe")
    assert isinstance(route, RouteResult)
    criteria = registry.criteria_for(route)
    assert isinstance(criteria[0].predicate, ExternalRefPredicate)
    assert "NCD" in criteria[0].predicate.ref


def test_excluded_procedure_is_flagged_with_its_clause(
    registry: CriteriaRegistry,
) -> None:
    route = registry.route("commercial", "gastric balloon")
    assert isinstance(route, RouteResult)
    assert route.excluded_by is not None
    assert route.excluded_by.id == "MGB-008.EXCL.2"
    assert registry.criteria_for(route) == []


def test_unrelated_service_has_no_route(registry: CriteriaRegistry) -> None:
    result = registry.route("commercial", "screening colonoscopy")
    assert isinstance(result, NoRouteReason)
    assert "colonoscopy" in result.reason


def test_missing_line_of_business_has_no_route(registry: CriteriaRegistry) -> None:
    result = registry.route(None, "TORe")
    assert isinstance(result, NoRouteReason)
    assert "line of business" in result.reason


def test_aco_marks_tore_code_not_covered(registry: CriteriaRegistry) -> None:
    route = registry.route("Mass General Brigham ACO", "C9785")
    assert isinstance(route, RouteResult)
    assert route.not_covered is True


def test_every_yes_no_clause_carries_a_retrieval_question(
    registry: CriteriaRegistry,
) -> None:
    """A negatively-phrased clause read literally inverts its own answer.

    The evaluation caught this: "does not have a substance use disorder" was
    answered "no" (no disorder documented) and scored NOT_MET. The question is
    what makes the answer's polarity well-defined, so every boolean and
    attestation clause must have one and it must ask, not judge.
    """

    for criterion in registry.all_criteria:
        if criterion.predicate.type not in {"boolean", "attestation"}:
            continue
        assert criterion.question, f"{criterion.id} has no retrieval question"
        assert criterion.prompt == criterion.question
        assert "met" not in criterion.question.casefold().split()


def test_clause_text_is_never_replaced_by_its_question(
    registry: CriteriaRegistry,
) -> None:
    """The citation shown to a reviewer stays the policy's own words."""

    criterion = registry.clause("MGB-008.TORe.7")
    assert criterion.text.startswith("The member does not have a substance use")
    assert criterion.question != criterion.text


def test_bm25_search_finds_the_governing_clause(registry: CriteriaRegistry) -> None:
    hits = registry.search("tobacco use before surgery", k=3)
    assert hits
    assert any("tobacco" in criterion.text.casefold() for criterion in hits)


def test_status_reports_policy_provenance(registry: CriteriaRegistry) -> None:
    status = registry.status()
    policy = status["policies"][0]
    assert policy["policy_id"] == "MGB-008"
    assert policy["effective_date"] == "2026-07-01"
    assert policy["source_present"] is True
    assert policy["criteria_count"] == 44


def test_specific_rule_wins_over_a_catch_all(registry: CriteriaRegistry) -> None:
    """Commercial TORe has its own criteria; MA delegates everything."""

    commercial = registry.route("commercial", "Transoral outlet reduction (TORe)")
    assert isinstance(commercial, RouteResult)
    assert commercial.criteria_ref == "TORe"

    medicare = registry.route("Medicare Advantage", "Transoral outlet reduction (TORe)")
    assert isinstance(medicare, RouteResult)
    assert medicare.criteria_ref.startswith("external:CMS NCD")


def test_catch_all_does_not_leak_into_unrelated_services(
    registry: CriteriaRegistry,
) -> None:
    """A delegating pathway still governs bariatric surgery, not everything."""

    result = registry.route("Medicare Advantage", "screening colonoscopy")
    assert isinstance(result, RouteResult)
    assert result.criteria_ref.startswith("external:")


# -- the PA catalog: breadth without criteria ---------------------------------


def test_catalog_loads_from_the_pa_guide(registry: CriteriaRegistry) -> None:
    assert registry.catalog is not None
    assert registry.catalog.catalog_id == "MGBHP-PA-GUIDE"
    assert len(registry.catalog.services) >= 30


def test_pa_required_service_without_a_policy_gets_an_informed_referral(
    registry: CriteriaRegistry,
) -> None:
    result = registry.route("commercial", "Spinal Surgery")
    assert isinstance(result, NoRouteReason)
    assert result.catalog_service == "Spinal Surgery"
    assert "requires prior authorization" in result.reason
    assert "not yet authored" in result.reason


def test_no_pa_service_says_so_instead_of_nothing_found(
    registry: CriteriaRegistry,
) -> None:
    result = registry.route("commercial", "nuclear stress test")
    assert isinstance(result, NoRouteReason)
    assert result.catalog_service == "Nuclear Stress Tests"
    assert "not requiring prior authorization" in result.reason


def test_catalog_alias_matches_inside_a_longer_request(
    registry: CriteriaRegistry,
) -> None:
    result = registry.route("commercial", "MRI brain with contrast")
    assert isinstance(result, NoRouteReason)
    assert result.catalog_service == "High Tech Radiology (CT, MRI, MRA, PET)"
    assert "some plans" in result.reason


def test_catalog_never_shadows_a_held_policy(registry: CriteriaRegistry) -> None:
    """Bariatric surgery is in the catalog AND held; routing must win."""

    result = registry.route("commercial", "Transoral outlet reduction (TORe)")
    assert isinstance(result, RouteResult)
    assert result.policy_id == "MGB-008"


def test_status_reports_the_catalog(registry: CriteriaRegistry) -> None:
    status = registry.status()
    assert status["pa_catalog"]["catalog_id"] == "MGBHP-PA-GUIDE"
    assert status["pa_catalog"]["services"] >= 30
