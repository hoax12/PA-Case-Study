"""Clause-level criteria registry.

A payer guideline is not prose to be retrieved; it is a set of numbered criteria,
each of which is a predicate over patient facts. This module holds that
representation: one node per numbered clause, carrying its verbatim text, its page,
and the machine-evaluable predicate behind it.

Retrieval by embedding returns a passage. A reviewer needs the clause. Keeping the
policy as structured criteria is what lets a decision cite `MGB-008.TORe.5` rather
than "page 4", and what lets a new payer be added by authoring data instead of code.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Field

from app.config import PROJECT_ROOT, Settings, get_settings


class ThresholdPredicate(BaseModel):
    type: Literal["threshold"]
    field: str
    op: Literal[">=", ">", "<=", "<", "=="]
    value: float
    # A band the policy resolves only through a fact we refuse to infer (e.g. the
    # 32.5 BMI branch turns on the member being of Asian descent). Values inside it
    # are UNKNOWN, never NOT_MET.
    unknown_band: tuple[float, float] | None = None


class TemporalPredicate(BaseModel):
    type: Literal["temporal"]
    anchor: str
    op: Literal[">=", "<=", ">", "<"]
    duration: str
    relative_to: Literal["order_date", "planned_surgery_date", "decision_date"]
    # "never used tobacco" satisfies a lookback with no anchor date at all.
    none_ok: bool = False


class BooleanPredicate(BaseModel):
    type: Literal["boolean"]
    field: str


class EnumPredicate(BaseModel):
    type: Literal["enum"]
    field: str
    allowed: list[str]


class AttestationPredicate(BaseModel):
    type: Literal["attestation"]
    field: str


class ExternalRefPredicate(BaseModel):
    """Criteria the policy delegates elsewhere (InterQual, an NCD, MassHealth).

    The text is not in this document, so the system cannot evaluate it and must not
    pretend to. It always resolves UNKNOWN, which routes the request to a human.
    """

    type: Literal["external_ref"]
    ref: str


Predicate = Annotated[
    ThresholdPredicate
    | TemporalPredicate
    | BooleanPredicate
    | EnumPredicate
    | AttestationPredicate
    | ExternalRefPredicate,
    Field(discriminator="type"),
]


class Criterion(BaseModel):
    id: str
    page: int = Field(ge=1)
    text: str
    predicate: Predicate
    note: str | None = None
    # `text` is the citation and must stay verbatim; it is often a poor retrieval
    # instruction. A clause phrased as an absence ("does not have a substance use
    # disorder") makes "no substance use documented" read as a failure when the
    # model answers about the fact rather than the clause. `question` fixes the
    # answer's polarity and shape without judging: it is what the model is asked,
    # `text` is what the reviewer is shown.
    question: str | None = None

    @property
    def prompt(self) -> str:
        return self.question or self.text


class CriteriaSet(BaseModel):
    logic: Literal["all"] = "all"
    items: list[Criterion]


class ProcedureRule(BaseModel):
    id: str
    cpt: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    criteria_ref: str
    # A pathway that delegates every bariatric procedure to an outside source
    # (an NCD, MassHealth) should not have to enumerate them. Such a rule matches
    # anything the pathway's more specific rules did not.
    catch_all: bool = False


class Pathway(BaseModel):
    id: str
    lines_of_business: list[str]
    pa_required: bool = True
    page: int = Field(default=1, ge=1)
    not_covered_cpt: list[str] = Field(default_factory=list)
    procedures: list[ProcedureRule] = Field(default_factory=list)


class Exclusion(BaseModel):
    id: str
    page: int = Field(ge=1)
    text: str
    procedures: list[str] = Field(default_factory=list)


class CrossReference(BaseModel):
    from_pathway: str = Field(alias="from")
    text: str
    to: list[str]

    model_config = {"populate_by_name": True}


class Policy(BaseModel):
    policy_id: str
    title: str
    payer: str
    effective_date: str
    source_file: str
    source_sha256: str | None = None
    pathways: list[Pathway]
    criteria_sets: dict[str, CriteriaSet]
    exclusions: list[Exclusion] = Field(default_factory=list)
    cross_references: list[CrossReference] = Field(default_factory=list)
    # Retrieval questions keyed by predicate field. The same concept (
    # "substance_use_cleared") is cited by several clauses across criteria sets,
    # so the question is authored once against the concept rather than repeated
    # under every clause that happens to cite it.
    evidence_questions: dict[str, str] = Field(default_factory=dict)


class RouteResult(BaseModel):
    policy_id: str
    policy_title: str
    effective_date: str
    pathway_id: str
    procedure_id: str
    criteria_ref: str
    pa_required: bool
    excluded_by: Exclusion | None = None
    not_covered: bool = False


class NoRouteReason(BaseModel):
    """Why nothing was adjudicated. Refer-to-human always has a stated cause."""

    reason: str
    line_of_business: str | None = None
    procedure: str | None = None
    nearest_clauses: list[str] = Field(default_factory=list)
    # Set when the payer's PA catalog knows the service even though no criteria
    # policy is held. This is the authoring-backlog telemetry signal.
    catalog_service: str | None = None


class PaCatalogEntry(BaseModel):
    """One row of the payer's PA/notification/referral index.

    An entry says whether a service needs PA at all; it never carries criteria.
    Only an authored policy in knowledge/policies/ can evaluate a request.
    """

    service: str
    page: int
    pa_required: Literal["yes", "no", "varies"]
    aliases: list[str] = Field(default_factory=list)
    note: str | None = None
    policy_id: str | None = None


class PaCatalog(BaseModel):
    catalog_id: str
    title: str
    payer: str
    source_file: str
    services: list[PaCatalogEntry]


def normalize_clause(value: str) -> str:
    """Fold PDF line wrapping and typography so clause text can be verified.

    Used only to compare a stored clause against the source PDF; the text a
    reviewer sees is always the verbatim string from the policy file.
    """

    folded = unicodedata.normalize("NFKC", value)
    folded = folded.replace("’", "'").replace("‘", "'")
    folded = folded.replace("“", '"').replace("”", '"')
    folded = folded.replace("≥", ">=").replace("≤", "<=")
    folded = folded.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", folded).strip().casefold()


def normalization_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


class CriteriaRegistry:
    """Loads every policy file and answers routing and clause lookups."""

    def __init__(self, policies_dir: Path | None = None):
        settings: Settings = get_settings()
        self.policies_dir = policies_dir or getattr(
            settings, "policies_dir_path", PROJECT_ROOT / "knowledge" / "policies"
        )
        self.policies: dict[str, Policy] = {}
        self._criteria_by_id: dict[str, Criterion] = {}
        self.catalog: PaCatalog | None = None
        self._load()
        self._load_catalog()

    def _load(self) -> None:
        if not self.policies_dir.is_dir():
            raise FileNotFoundError(f"Policy directory not found: {self.policies_dir}")
        for path in sorted(self.policies_dir.glob("*.yaml")):
            if path.name.startswith("_"):
                # `_draft_*.yaml` is machine-generated and awaiting human review.
                continue
            policy = Policy.model_validate(
                yaml.safe_load(path.read_text(encoding="utf-8"))
            )
            self.policies[policy.policy_id] = policy
            for criteria_set in policy.criteria_sets.values():
                for criterion in criteria_set.items:
                    if criterion.id in self._criteria_by_id:
                        raise ValueError(f"Duplicate criterion id: {criterion.id}")
                    field = getattr(criterion.predicate, "field", None)
                    if criterion.question is None and field:
                        criterion.question = policy.evidence_questions.get(field)
                    self._criteria_by_id[criterion.id] = criterion
        if not self.policies:
            raise ValueError(f"No policy files found in {self.policies_dir}")

    def _load_catalog(self) -> None:
        """The payer's PA index is optional; without it routing works as before."""

        path = self.policies_dir.parent / "catalog" / "pa_catalog.yaml"
        if path.is_file():
            self.catalog = PaCatalog.model_validate(
                yaml.safe_load(path.read_text(encoding="utf-8"))
            )

    def catalog_lookup(self, procedure: str) -> PaCatalogEntry | None:
        """Match a requested service against the PA catalog by name or alias."""

        if self.catalog is None or not procedure:
            return None
        procedure_key = normalization_key(procedure)
        for entry in self.catalog.services:
            names = {normalization_key(entry.service)} | {
                normalization_key(alias) for alias in entry.aliases
            }
            # Token-boundary containment: alias "mri" matches "mri brain",
            # never the inside of another word.
            if procedure_key in names or any(
                name and f"-{name}-" in f"-{procedure_key}-" for name in names
            ):
                return entry
        return None

    # -- routing ---------------------------------------------------------------

    def route(
        self, line_of_business: str | None, procedure: str | None
    ) -> RouteResult | NoRouteReason:
        """Map (plan, requested service) to the criteria that actually govern it."""

        if not line_of_business:
            return NoRouteReason(
                reason="The line of business was not found in the packet.",
                procedure=procedure,
            )
        if not procedure:
            return NoRouteReason(
                reason="The requested procedure or service was not found in the packet.",
                line_of_business=line_of_business,
            )
        lob_key = normalization_key(line_of_business)
        procedure_key = normalization_key(procedure)
        miss: NoRouteReason | None = None
        for policy in self.policies.values():
            for pathway in policy.pathways:
                if lob_key not in {
                    normalization_key(item) for item in pathway.lines_of_business
                }:
                    continue
                exclusion = self._matching_exclusion(policy, procedure_key)
                if exclusion is not None:
                    return RouteResult(
                        policy_id=policy.policy_id,
                        policy_title=policy.title,
                        effective_date=policy.effective_date,
                        pathway_id=pathway.id,
                        procedure_id=procedure,
                        criteria_ref="",
                        pa_required=pathway.pa_required,
                        excluded_by=exclusion,
                    )
                # Specific rules win; a catch-all only applies if nothing named it.
                ordered = sorted(pathway.procedures, key=lambda rule: rule.catch_all)
                for rule in ordered:
                    names = {normalization_key(rule.id)} | {
                        normalization_key(alias) for alias in rule.aliases
                    }
                    codes = {code.upper() for code in rule.cpt}
                    matched = (
                        rule.catch_all
                        or procedure_key in names
                        or procedure.strip().upper() in codes
                        or any(name and name in procedure_key for name in names)
                    )
                    if matched:
                        return RouteResult(
                            policy_id=policy.policy_id,
                            policy_title=policy.title,
                            effective_date=policy.effective_date,
                            pathway_id=pathway.id,
                            procedure_id=rule.id,
                            criteria_ref=rule.criteria_ref,
                            pa_required=pathway.pa_required,
                            not_covered=any(
                                code in {c.upper() for c in pathway.not_covered_cpt}
                                for code in codes
                            ),
                        )
                # Remember the miss but keep looking: another policy, or the PA
                # catalog, may still know this service.
                miss = miss or NoRouteReason(
                    reason=(
                        f"No criteria in {policy.policy_id} cover "
                        f"'{procedure}' under the {pathway.id} pathway."
                    ),
                    line_of_business=line_of_business,
                    procedure=procedure,
                    nearest_clauses=[item.id for item in self.search(procedure, 3)],
                )
        entry = self.catalog_lookup(procedure)
        if entry is not None and entry.policy_id in self.policies:
            # The catalog points at a policy we hold, so "not yet authored" would
            # be false — the routing miss above is the truthful reason.
            entry = None
        if entry is not None:
            guide = f"the {self.catalog.title} (p. {entry.page})"  # type: ignore[union-attr]
            if entry.pa_required == "no":
                reason = (
                    f"{guide} lists '{entry.service}' as not requiring prior "
                    "authorization. A reviewer should confirm plan-specific rules "
                    "and close the request without a criteria adjudication."
                )
            else:
                qualifier = (
                    "requires prior authorization"
                    if entry.pa_required == "yes"
                    else "requires prior authorization for some plans"
                )
                reason = (
                    f"{guide} states '{entry.service}' {qualifier}, but the "
                    "governing medical policy is not yet authored in this system, "
                    "so a reviewer must adjudicate it."
                )
            if entry.note:
                reason += f" ({entry.note})"
            return NoRouteReason(
                reason=reason,
                line_of_business=line_of_business,
                procedure=procedure,
                catalog_service=entry.service,
            )
        return miss or NoRouteReason(
            reason=(
                "No indexed policy covers this request. A guideline for this plan "
                "and service must be loaded before it can be adjudicated."
            ),
            line_of_business=line_of_business,
            procedure=procedure,
            nearest_clauses=[item.id for item in self.search(procedure or "", 3)],
        )

    @staticmethod
    def _matching_exclusion(policy: Policy, procedure_key: str) -> Exclusion | None:
        for exclusion in policy.exclusions:
            for name in exclusion.procedures:
                if normalization_key(name) == procedure_key:
                    return exclusion
        return None

    def criteria_for(self, route: RouteResult) -> list[Criterion]:
        """Resolve a route to its criteria, including delegated external sets."""

        if route.excluded_by is not None:
            return []
        reference = route.criteria_ref
        if reference.startswith("external:"):
            target = reference.split(":", 1)[1]
            return [
                Criterion(
                    id=f"{route.policy_id}.{route.pathway_id}.external",
                    page=self._pathway_page(route),
                    text=(
                        f"Medical necessity for this request is determined through "
                        f"{target}, which is not part of this policy document."
                    ),
                    predicate=ExternalRefPredicate(type="external_ref", ref=target),
                    note="Delegated criteria are never evaluated automatically.",
                )
            ]
        policy = self.policies[route.policy_id]
        criteria_set = policy.criteria_sets.get(reference)
        if criteria_set is None:
            raise KeyError(f"Unknown criteria_ref: {reference}")
        return criteria_set.items

    def _pathway_page(self, route: RouteResult) -> int:
        policy = self.policies[route.policy_id]
        for pathway in policy.pathways:
            if pathway.id == route.pathway_id:
                return pathway.page
        return 1

    # -- lookup ----------------------------------------------------------------

    def clause(self, criterion_id: str) -> Criterion:
        return self._criteria_by_id[criterion_id]

    @property
    def all_criteria(self) -> list[Criterion]:
        return list(self._criteria_by_id.values())

    def search(self, text: str, k: int = 5) -> list[Criterion]:
        """Lexical search over clause text.

        BM25 over a few dozen clauses, not embeddings over chunks: the corpus is
        small, the vocabulary is the policy's own, and precision on the exact
        clause matters more than semantic recall. It also has no model to version.
        """

        criteria = self.all_criteria
        if not text.strip() or not criteria:
            return []
        from rank_bm25 import BM25Okapi

        corpus = [re.findall(r"[a-z0-9]+", item.text.casefold()) for item in criteria]
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(re.findall(r"[a-z0-9]+", text.casefold()))
        ranked = sorted(zip(scores, criteria), key=lambda pair: pair[0], reverse=True)
        return [criterion for score, criterion in ranked[:k] if score > 0]

    def status(self) -> dict[str, Any]:
        policies = []
        for policy in self.policies.values():
            source = PROJECT_ROOT / "knowledge" / "source" / policy.source_file
            current = (
                hashlib.sha256(source.read_bytes()).hexdigest()
                if source.is_file()
                else None
            )
            policies.append(
                {
                    "policy_id": policy.policy_id,
                    "title": policy.title,
                    "payer": policy.payer,
                    "effective_date": policy.effective_date,
                    "source_file": policy.source_file,
                    "source_present": source.is_file(),
                    "source_matches_recorded_hash": (
                        current == policy.source_sha256
                        if policy.source_sha256
                        else None
                    ),
                    "pathways": [pathway.id for pathway in policy.pathways],
                    "criteria_count": sum(
                        len(item.items) for item in policy.criteria_sets.values()
                    ),
                }
            )
        return {
            "ready": True,
            "policies": policies,
            "pa_catalog": (
                {
                    "catalog_id": self.catalog.catalog_id,
                    "payer": self.catalog.payer,
                    "source_file": self.catalog.source_file,
                    "services": len(self.catalog.services),
                }
                if self.catalog
                else None
            ),
        }


@lru_cache(maxsize=1)
def get_registry() -> CriteriaRegistry:
    return CriteriaRegistry()
