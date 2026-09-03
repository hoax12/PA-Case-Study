"""Ground-truth checks for the synthetic packets.

These run without a network or a model: they assert that the hand-written
`expected.json` files are internally consistent with routing and the decision
function. If a packet's expected outcome disagrees with what `decide` would do
given that criteria matrix, the ground truth is wrong and every metric computed
against it would be wrong too.

The live end-to-end evaluation (does the pipeline actually reach these statuses
from the images?) is `scripts/evaluate.py`, which is nondeterministic and costs
money, so it stays out of the test suite.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from app.adjudication import CriterionResult, Outcome, decide
from app.config import PROJECT_ROOT
from app.models import CriterionStatus
from app.registry import CriteriaRegistry, NoRouteReason, RouteResult


SYNTHETIC_DIR = PROJECT_ROOT / "data" / "synthetic"
DECISION_DATE = date(2026, 9, 1)


def case_dirs() -> list[Path]:
    if not SYNTHETIC_DIR.is_dir():
        return []
    return sorted(path for path in SYNTHETIC_DIR.iterdir() if path.is_dir())


def load(case_dir: Path) -> dict:
    return json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))


pytestmark = pytest.mark.skipif(
    not case_dirs(),
    reason="Run scripts/make_synthetic_packets.py to generate the golden cases.",
)


@pytest.fixture(scope="module")
def registry() -> CriteriaRegistry:
    return CriteriaRegistry(PROJECT_ROOT / "knowledge" / "policies")


@pytest.mark.parametrize("case_dir", case_dirs(), ids=lambda path: path.name)
def test_expected_matrix_produces_the_expected_outcome(
    case_dir: Path, registry: CriteriaRegistry
) -> None:
    """The stated outcome must be what `decide` returns for the stated matrix."""

    case = load(case_dir)
    expected = case["expected"]
    routed = registry.route(
        case["input"]["line_of_business"] or None, case["input"]["procedure"] or None
    )
    route = routed if isinstance(routed, RouteResult) else None
    no_route_reason = None if route else routed.reason  # type: ignore[union-attr]

    results = [
        CriterionResult(
            criterion_id=criterion_id,
            status=CriterionStatus(status),
            reason="ground truth",
            clause_text=_clause_text(registry, criterion_id),
            page=_clause_page(registry, criterion_id),
            predicate_type="ground_truth",
            confidence=0.9 if status == "met" else 0.0,
        )
        for criterion_id, status in expected["criteria"].items()
    ]

    adjudication = decide(
        results, route, DECISION_DATE, threshold=0.7, no_route_reason=no_route_reason
    )
    assert adjudication.outcome.value == expected["outcome"], (
        f"{case_dir.name}: ground truth says {expected['outcome']} but the "
        f"decision function returns {adjudication.outcome.value}"
    )


@pytest.mark.parametrize("case_dir", case_dirs(), ids=lambda path: path.name)
def test_case_routes_where_the_ground_truth_says(
    case_dir: Path, registry: CriteriaRegistry
) -> None:
    case = load(case_dir)
    expected = case["expected"]
    routed = registry.route(
        case["input"]["line_of_business"] or None, case["input"]["procedure"] or None
    )
    if expected["policy_id"] is None:
        assert isinstance(routed, NoRouteReason)
        return
    assert isinstance(routed, RouteResult)
    assert routed.policy_id == expected["policy_id"]
    assert routed.criteria_ref == expected["criteria_ref"]


@pytest.mark.parametrize("case_dir", case_dirs(), ids=lambda path: path.name)
def test_every_named_criterion_exists_in_the_registry(
    case_dir: Path, registry: CriteriaRegistry
) -> None:
    """Ground truth cannot reference a clause the policy does not contain."""

    case = load(case_dir)
    known = {criterion.id for criterion in registry.all_criteria}
    for criterion_id in case["expected"]["criteria"]:
        if criterion_id.endswith(".external"):
            continue  # synthesised for a delegated criteria set
        assert criterion_id in known, f"{case_dir.name} names unknown {criterion_id}"


@pytest.mark.parametrize("case_dir", case_dirs(), ids=lambda path: path.name)
def test_documents_listed_in_ground_truth_are_present(case_dir: Path) -> None:
    case = load(case_dir)
    assert case["documents"], f"{case_dir.name} has no documents"
    for name in case["documents"]:
        assert (case_dir / name).is_file(), f"{case_dir.name}/{name} is missing"


def test_the_suite_covers_both_outcomes() -> None:
    """An evaluation set with no affirmations cannot measure false affirmations."""

    outcomes = {load(path)["expected"]["outcome"] for path in case_dirs()}
    assert "provisional_affirmation" in outcomes
    assert "refer_to_human" in outcomes


def test_full_tore_matrix_is_complete() -> None:
    """The affirmation case must exercise every TORe clause, not a subset."""

    case = load(SYNTHETIC_DIR / "tore_affirm")
    assert len(case["expected"]["criteria"]) == 16
    assert set(case["expected"]["criteria"].values()) == {"met"}


def test_out_of_domain_packet_refers_end_to_end(tmp_path: Path) -> None:
    """The whole pipeline, offline: an unrelated packet must not be scored.

    This is the one end-to-end assertion that runs in CI. It uses the fixture
    provider, so it costs nothing and cannot flake on a model call, but it still
    exercises intake, OCR, extraction, routing, and the decision.
    """

    import asyncio
    import shutil

    from app.config import get_settings
    from app.pipeline import PriorAuthPipeline
    from app.providers import FixtureClinicalProvider
    from app.repository import ReviewRepository

    case_dir = SYNTHETIC_DIR / "out_of_domain"
    settings = get_settings()
    job_id = "test-out-of-domain"
    upload_dir = settings.uploads_dir / job_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in load(case_dir)["documents"]:
        shutil.copyfile(case_dir / name, upload_dir / name)
        paths.append(upload_dir / name)

    repository = ReviewRepository(tmp_path / "test.db")
    repository.initialize()
    repository.create_job(job_id, "fixture", [{"file_name": p.name} for p in paths])
    pipeline = PriorAuthPipeline(
        repository, settings, provider=FixtureClinicalProvider()
    )
    asyncio.run(pipeline.run_job(job_id, paths))

    job = repository.get_job(job_id)
    assert job["status"] == "ready", job.get("error")
    adjudication = job["adjudication"]
    assert adjudication["outcome"] == Outcome.REFER_TO_HUMAN.value
    assert adjudication["policy_id"] is None
    assert adjudication["criteria"] == []
    assert adjudication["refer_reasons"], "a referral must say why"


def _clause_text(registry: CriteriaRegistry, criterion_id: str) -> str:
    try:
        return registry.clause(criterion_id).text
    except KeyError:
        return "delegated criteria"


def _clause_page(registry: CriteriaRegistry, criterion_id: str) -> int:
    try:
        return registry.clause(criterion_id).page
    except KeyError:
        return 1
