from __future__ import annotations

from .shared import *  # noqa: F401,F403
from .shared import (
    _NEO4J_QUERY_ERRORS,
    _normalize_time_filter_bound,
    _node_id_expression,
    _node_identity_value,
    _node_match_condition,
    _node_text_expression,
    _resolved_edge_relationship_types,
)
from ..linking.kg_link import (
    organization_subset_link_bonus as _organization_subset_link_bonus,
    score_kg_link_candidate as _score_kg_link_candidate,
)
from ..utils.text_normalization import (
    context_window as _context_window,
    document_name_variants as _document_name_variants,
    link_hint_tokens as _link_hint_tokens,
    looks_like_filename as _looks_like_filename,
    matches_metadata_value as _matches_metadata_value,
    normalize_phrase as _normalize_phrase,
    person_name_variants as _person_name_variants,
    tokenize_phrase as _tokenize_phrase,
)

class Phase3_AnchorMentionExtraction:
    """
    For each selected artifact:
        1. Determine family
        2. Get adapter
        3. Extract anchors (structural locations)
        4. Extract mentions (all semantic elements at each anchor)
    """

    _RESOLUTION_CATEGORIES: Tuple[str, ...] = (
        "ENTITY_COLLECTION",
        "ENTITY_PERSON",
        "ENTITY_ORGANIZATION",
        "ENTITY_ROLE",
        "ENTITY_POPULATION",
        "ENTITY_ACTION_ITEM",
        "ENTITY_DOCUMENT",
        "ENTITY_CLAIM",
        "ENTITY_RISK_FINDING",
        "ENTITY_CONCEPT",
        "ENTITY_POLICY",
        "ENTITY_EVENT",
        "ENTITY_LOCATION",
    )

    _ROLE_TOKENS: Set[str] = {
        "attorney",
        "attorneys",
        "counsel",
        "officer",
        "officers",
        "director",
        "directors",
        "manager",
        "managers",
        "supervisor",
        "supervisors",
        "pharmacist",
        "pharmacists",
        "reviewer",
        "reviewers",
        "employee",
        "employees",
        "prescriber",
        "prescribers",
        "leadership",
        "executive",
        "executives",
        "analyst",
        "analysts",
    }

    _ORGANIZATION_TOKENS: Set[str] = {
        "company",
        "companies",
        "corporation",
        "corp",
        "inc",
        "llc",
        "ltd",
        "co",
        "group",
        "committee",
        "board",
        "association",
        "partners",
        "operations",
        "department",
        "division",
        "office",
        "unit",
        "team",
        "services",
        "management",
        "pharma",
        "pharmaceuticals",
        "laboratories",
        "labs",
        "prevention",
        "compliance",
        "regulatory",
        "legal",
        "finance",
        "marketing",
        "operations",
    }

    _ORGANIZATION_LEGAL_ENTITY_TOKENS: Set[str] = {
        "company",
        "companies",
        "co",
        "corp",
        "corporation",
        "inc",
        "llc",
        "ltd",
        "lp",
        "llp",
        "plc",
        "holdings",
        "group",
    }

    _ORGANIZATION_LINK_MODIFIER_TOKENS: Set[str] = {
        "business",
        "commercial",
        "compliance",
        "corporate",
        "department",
        "division",
        "enterprise",
        "external",
        "finance",
        "functional",
        "group",
        "hr",
        "human",
        "information",
        "internal",
        "legal",
        "litigation",
        "law",
        "management",
        "office",
        "operations",
        "operational",
        "pharmacy",
        "policy",
        "prevention",
        "regulatory",
        "risk",
        "services",
        "support",
        "team",
        "technology",
        "unit",
    }

    _CONCEPT_TOKENS: Set[str] = {
        "policy",
        "policies",
        "procedure",
        "procedures",
        "protocol",
        "protocols",
        "guideline",
        "guidelines",
        "presentation",
        "presentations",
        "slide",
        "slides",
        "fax",
        "email",
        "emails",
        "letter",
        "letters",
        "plan",
        "plans",
    }

    _COLLECTION_TOKENS: Set[str] = {
        "records",
        "record",
        "archive",
        "archives",
        "corpus",
        "collection",
        "collections",
        "dataset",
        "datasets",
        "materials",
        "files",
    }

    _DOCUMENT_TOKENS: Set[str] = {
        "attachment",
        "attachments",
        "document",
        "documents",
        "memo",
        "memos",
        "report",
        "reports",
        "briefing",
        "draft",
        "drafts",
        "presentation",
        "presentations",
        "deck",
        "decks",
        "slide",
        "slides",
        "form",
        "forms",
        "web",
        "questionnaire",
        "questionnaires",
        "template",
        "templates",
        "summary",
        "agenda",
        "pdf",
        "doc",
        "docx",
        "ppt",
        "pptx",
        "xls",
        "xlsx",
        "txt",
        "subject",
    }

    _DOCUMENT_LIKE_FORM_TOKENS: Set[str] = {
        "form",
        "forms",
        "web",
        "questionnaire",
        "questionnaires",
        "template",
        "templates",
    }

    _POLICY_TOKENS: Set[str] = {
        "policy",
        "policies",
        "compliance",
        "requirement",
        "requirements",
        "guidance",
        "regulation",
        "regulations",
        "retention",
        "standard",
        "standards",
        "protocol",
        "protocols",
        "regulatory",
        "legal",
    }

    _CLAIM_TOKENS: Set[str] = {
        "claim",
        "claims",
        "increase",
        "decrease",
        "reduction",
        "change",
        "concern",
        "concerns",
        "indicator",
        "indicators",
        "question",
        "questions",
        "noncompliance",
        "risk",
    }

    _ACTION_ITEM_TOKENS: Set[str] = {
        "response",
        "respond",
        "review",
        "implementation",
        "implement",
        "forward",
        "collect",
        "analyze",
        "analysis",
        "escalate",
        "unresolved",
        "silence",
        "documenting",
        "steps",
    }

    _RISK_TOKENS: Set[str] = {
        "risk",
        "red",
        "flag",
        "redflag",
        "shopping",
        "pharmacy",
        "doctor",
        "diversion",
        "abuse",
        "misuse",
        "inappropriate",
        "prescriptions",
        "prescription",
        "indicator",
        "indicators",
    }

    _FUNCTION_TOKENS: Set[str] = {
        "compliance",
        "legal",
        "regulatory",
        "finance",
        "marketing",
        "operations",
        "prevention",
    }

    _GENERIC_CONCEPT_SINGLETONS: Set[str] = {
        "attachment",
        "attachments",
        "document",
        "documents",
        "form",
        "forms",
        "materials",
        "report",
        "reports",
        "summary",
    }

    _ROLE_NOISE_TOKENS: Set[str] = {
        "announcing",
        "announcement",
        "confidential",
        "final",
        "next",
        "proposed",
    }

    _DISCLAIMER_CUES: Tuple[str, ...] = (
        "attorney work product",
        "attorney-client privilege",
        "intended recipient",
        "not the intended recipient",
        "received this message",
        "delete the information",
        "contact the sender",
    )

    _ACTION_ITEM_DECISIVE_TOKENS: Set[str] = {
        "move",
        "forward",
        "implement",
        "implementation",
        "respond",
        "response",
        "address",
        "resolved",
        "resolve",
        "share",
        "collect",
        "analyze",
        "analysis",
        "recirculate",
        "follow",
        "escalate",
    }

    _ACTION_ITEM_GENERIC_TOKENS: Set[str] = {
        "next",
        "step",
        "steps",
        "final",
        "review",
        "proposed",
    }

    _ACTION_ITEM_HEDGE_TOKENS: Set[str] = {
        "arguably",
        "consider",
        "concerned",
        "could",
        "if",
        "may",
        "might",
        "perhaps",
        "potential",
        "questioning",
        "questions",
        "should",
    }

    _ACTION_ITEM_HEDGE_CUES: Tuple[str, ...] = (
        "we should",
        "perhaps we should",
        "i am somewhat concerned",
        "i am concerned",
        "if these are legitimate",
        "arguably imply",
        "potential noncompliance",
    )

    _ACTION_ITEM_PLANNED_CUES: Tuple[str, ...] = (
        "we will",
        "next steps",
        "proposed next steps",
        "move forward",
        "follow up",
        "share results",
        "recirculate",
        "implementation",
        "review and we will",
    )

    _EVENT_TOKENS: Set[str] = {
        "meeting",
        "meetings",
        "conference",
        "conferences",
        "hearing",
        "hearings",
        "call",
        "calls",
        "audit",
        "audits",
        "review",
        "reviews",
        "workshop",
        "launch",
        "training",
        "trainings",
    }

    _LOCATION_TOKENS: Set[str] = {
        "road",
        "street",
        "drive",
        "avenue",
        "boulevard",
        "blvd",
        "lane",
        "court",
        "way",
        "county",
        "state",
    }

    _PERSON_SECTION_TOKENS: Set[str] = {
        "abstract",
        "background",
        "conclusion",
        "conclusions",
        "discussion",
        "introduction",
        "method",
        "methods",
        "outcome",
        "outcomes",
        "result",
        "results",
        "summary",
    }

    _GENERIC_POLICY_SINGLETONS: Set[str] = {
        "retention",
    }

    _GENERIC_RISK_SINGLETONS: Set[str] = {
        "risk",
        "risks",
    }

    _PROGRAM_ENTITY_TOKENS: Set[str] = {
        "award",
        "awards",
        "fund",
        "funded",
        "funding",
        "grant",
        "grants",
        "program",
        "programs",
        "research",
        "sponsored",
    }

    _CLAUSAL_FINDING_TOKENS: Set[str] = {
        "demonstrated",
        "found",
        "identified",
        "improved",
        "included",
        "indicated",
        "prevented",
        "reduced",
        "showed",
        "suggested",
    }

    _DOCUMENTISH_EVENT_TOKENS: Set[str] = {
        "clinical",
        "index",
        "indices",
        "program",
        "programs",
        "protocol",
        "protocols",
        "questionnaire",
        "questionnaires",
        "trial",
        "trials",
    }

    _PERSON_INSTITUTION_TOKENS: Set[str] = {
        "academy",
        "center",
        "centers",
        "clinic",
        "clinics",
        "college",
        "hospital",
        "hospitals",
        "institute",
        "institutes",
        "laboratory",
        "laboratories",
        "medical",
        "medicine",
        "naval",
        "research",
        "school",
        "university",
    }

    _PERSON_INSTRUMENT_TOKENS: Set[str] = {
        "index",
        "indices",
        "inventory",
        "inventories",
        "measure",
        "measures",
        "questionnaire",
        "questionnaires",
        "scale",
        "scales",
    }

    _PERSON_CONDITION_TOKENS: Set[str] = {
        "behavioral",
        "clinical",
        "concussion",
        "condition",
        "conditions",
        "cognitive",
        "disease",
        "disorder",
        "disorders",
        "injury",
        "injuries",
        "insomnia",
        "military",
        "population",
        "populations",
        "protocol",
        "severity",
        "stress",
        "study",
        "studies",
        "syndrome",
        "syndromes",
        "therapy",
        "treatment",
        "trial",
        "veteran",
        "veterans",
    }

    _PERSON_TITLES: Set[str] = {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
    }

    _LINK_STOPWORDS: Set[str] = {
        "a",
        "an",
        "and",
        "for",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
    # Every label below must actually exist in the live KG0 — the old
    # Topic/HealthMention/Claim/Decision/Risk/LegalFramework labels are
    # gone, replaced by the dynamic PascalCase labels the LLM extractor
    # produces (see kg0_from_db.resolve_labels).
    _KG_LINK_LABEL_OVERRIDES: Dict[str, List[str]] = {
        "ENTITY_ROLE": ["Role", "JobTitle", "Person", "Executive", "Manager"],
        "ROLE": ["Role", "JobTitle", "Person", "Executive", "Manager"],
        "ENTITY_POPULATION": ["Population"],
        "POPULATION": ["Population"],
        "ENTITY_POLICY": [
            "Policy", "Regulation", "CodeOfFederalRegulations",
            "RegulatoryEntity", "LegalConcept",
        ],
        "POLICY": [
            "Policy", "Regulation", "CodeOfFederalRegulations",
            "RegulatoryEntity", "LegalConcept",
        ],
        "ENTITY_CLAIM": [
            "BusinessConcept", "DomainEntity", "Concept",
            "ClinicalConcept", "LegalConcept",
        ],
        "CLAIM": [
            "BusinessConcept", "DomainEntity", "Concept",
            "ClinicalConcept", "LegalConcept",
        ],
        "ENTITY_STRATEGY": [
            "Action", "Program", "Method", "Intervention",
            "BusinessConcept", "FinancialPlan",
        ],
        "STRATEGY": [
            "Action", "Program", "Method", "Intervention",
            "BusinessConcept", "FinancialPlan",
        ],
        "ENTITY_CONCEPT": [
            "Concept", "BusinessConcept", "ClinicalConcept", "LegalConcept",
            "DomainEntity", "ClinicalEntity",
        ],
        "CONCEPT": [
            "Concept", "BusinessConcept", "ClinicalConcept", "LegalConcept",
            "DomainEntity", "ClinicalEntity",
        ],
        "ENTITY_TOPIC": [
            "Concept", "BusinessConcept", "ClinicalConcept", "LegalConcept",
            "DomainEntity",
        ],
        "TOPIC": [
            "Concept", "BusinessConcept", "ClinicalConcept", "LegalConcept",
            "DomainEntity",
        ],
        "ENTITY_DOCUMENT": ["Document"],
        "DOCUMENT": ["Document"],
        "ARTIFACT_DOCUMENT": ["Document"],
        # Page is a first-class node in the new KG0, produced by load_pages.py.
        "PAGE": ["Page"],
        "ARTIFACT_PAGE": ["Page"],
    }
    _KG_LINK_FIELD_PRIORITIES: Dict[str, List[str]] = {
        "DEFAULT": [
            "name",
            "title",
            "subject",
            "summary",
            "text",
            "description",
            "citationText",
            "context",
            "caption",
            "notes",
            "abbvName",
            "fullForm",
            "contextOfDate",
            "identifier",
            "recordId",
            "sourceFileName",
            "witness",
            "witnessContext",
        ],
        "ENTITY_ROLE": [
            "role",
            "title",
            "description",
            "organization",
            "name",
            "summary",
            "text",
            "notes",
            "witness",
            "witnessContext",
        ],
        "ROLE": [
            "role",
            "title",
            "description",
            "organization",
            "name",
            "summary",
            "text",
            "notes",
            "witness",
            "witnessContext",
        ],
        "ENTITY_POPULATION": [
            "name",
            "description",
            "summary",
            "text",
            "notes",
            "witness",
            "witnessContext",
        ],
        "POPULATION": [
            "name",
            "description",
            "summary",
            "text",
            "notes",
            "witness",
            "witnessContext",
        ],
        "ENTITY_POLICY": [
            "title",
            "name",
            "summary",
            "description",
            "text",
            "notes",
            "identifier",
            "recordId",
            "sourceFileName",
            "witness",
            "witnessContext",
        ],
        "POLICY": [
            "title",
            "name",
            "summary",
            "description",
            "text",
            "notes",
            "identifier",
            "recordId",
            "sourceFileName",
            "witness",
            "witnessContext",
        ],
        "ENTITY_CLAIM": [
            "text",
            "summary",
            "description",
            "title",
            "name",
            "context",
            "citationText",
            "witness",
            "witnessContext",
        ],
        "CLAIM": [
            "text",
            "summary",
            "description",
            "title",
            "name",
            "context",
            "citationText",
            "witness",
            "witnessContext",
        ],
        "ENTITY_DOCUMENT": [
            "title",
            "subject",
            "summary",
            "recordId",
            "sourceFileName",
            "name",
            "description",
            "text",
            "witness",
            "witnessContext",
        ],
        "DOCUMENT": [
            "title",
            "subject",
            "summary",
            "recordId",
            "sourceFileName",
            "name",
            "description",
            "text",
            "witness",
            "witnessContext",
        ],
        "ARTIFACT_DOCUMENT": [
            "title",
            "subject",
            "summary",
            "recordId",
            "sourceFileName",
            "name",
            "description",
            "text",
            "witness",
            "witnessContext",
        ],
    }

    def __init__(
        self,
        config: AlignConfig,
        index: IndexFacade,
        adapters: AdapterRegistry,
    ):
        self.config = config
        self.index = index
        self.adapters = adapters
        self._mention_link_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def execute(
        self,
        selected_artifacts: List[CandidateArtifact],
        intent: IntentObject,
    ) -> Tuple[
        Dict[str, List[Anchor]],
        Dict[str, List[Mention]],
        Dict[str, List[Mention]],
    ]:
        """
        Execute Phase 3.
        Returns:
            all_anchors: artifact_id -> List[Anchor]
            all_mentions: anchor_id -> List[Mention]
            suppressed_mentions: anchor_id -> List[Mention] filtered as ambiguous/low-signal
        """
        logger.info(
            f"Phase 3: Extracting anchors/mentions for "
            f"{len(selected_artifacts)} artifacts"
        )

        all_anchors: Dict[str, List[Anchor]] = {}
        all_mentions: Dict[str, List[Mention]] = {}
        suppressed_mentions_map: Dict[str, List[Mention]] = {}
        retyped_mentions = 0
        suppressed_mentions = 0

        # Batch fetch artifact texts from Solr
        artifact_ids = [a.artifact_id for a in selected_artifacts]
        artifact_texts = self.index.get_artifact_texts_batch(artifact_ids)

        # Batch-prefetch KG0 page records for every selected artifact in
        # one Cypher round-trip. The KG0-native adapter will then read
        # each artifact's records from its in-memory cache instead of
        # issuing N separate queries; this collapses Phase 3's wall-clock
        # cost from O(artifacts) round-trips to O(1).
        if selected_artifacts:
            sample_family = self.config.family_for_artifact_type(
                selected_artifacts[0].family
            )
            sample_adapter = self.adapters.get_adapter(sample_family)
            prefetch_fn = getattr(sample_adapter, "prefetch_documents", None)
            if callable(prefetch_fn):
                prefetch_fn(artifact_ids)

        for candidate in selected_artifacts:
            aid = candidate.artifact_id
            family = self.config.family_for_artifact_type(candidate.family)
            adapter = self.adapters.get_adapter(family)

            # Get artifact data from Solr (text) + Neo4j (structure)
            artifact_data = artifact_texts.get(aid, {})

            # When KG structure is deferred, Phase 3 stays text-first and does
            # not pull artifact graph structure early.
            if not self.config.defer_kg_structure_until_post_phase5:
                node_data = self.index.neo4j.get_node(aid)
                if node_data:
                    artifact_data.update(
                        {
                            k: v
                            for k, v in node_data.items()
                            if k not in artifact_data and not k.startswith("_")
                        }
                    )

            # Extract anchors
            anchors = adapter.extract_anchors(aid, artifact_data, intent)

            # FIX #3 (support): Enrich anchors with artifact-level temporal
            # metadata so Phase 5 can evaluate temporal constraints.
            artifact_date = (
                artifact_data.get("date", "")
                or str(candidate.metadata.get("date", "") or "")
            )
            artifact_title = str(
                candidate.metadata.get("title", "")
                or artifact_data.get("title", "")
                or ""
            ).strip()
            artifact_name = (
                candidate.artifact_name
                or str(artifact_data.get("name", "") or "")
                or str(artifact_data.get("title", "") or "")
                or str(artifact_data.get("source_file_name", "") or "")
            )
            for anchor in anchors:
                anchor.metadata["artifact_date"] = artifact_date
                anchor.metadata["artifact_family"] = family
                if artifact_title:
                    anchor.metadata["title"] = artifact_title
                if artifact_name:
                    anchor.metadata["artifact_name"] = artifact_name

            all_anchors[aid] = anchors

            # Extract mentions at each anchor (query-independent)
            artifact_context = self._build_artifact_context(artifact_data)
            anchor_lookup = {anchor.anchor_id: anchor for anchor in anchors}
            mention_map = adapter.extract_mentions(aid, anchors)
            mention_map = self._inject_metadata_mentions(
                artifact_data,
                anchors,
                mention_map,
                intent,
            )
            for anchor_id, mentions in mention_map.items():
                anchor = anchor_lookup.get(anchor_id)
                if anchor is not None:
                    # KG0-native mentions already carry authoritative
                    # categories (from the graph's top_category); skip
                    # the text-heuristic resolver in that case. We also
                    # skip KG0 re-linking because kg0_entity_id is
                    # already set by the adapter.
                    kg0_native = any(
                        getattr(m, "kg0_entity_id", None) for m in mentions
                    )
                    if kg0_native:
                        # Soldier through: mentions keep their adapter
                        # category and kg0 id as-is.
                        pass
                    else:
                        mentions, changes = self._resolve_mentions_for_anchor(
                            mentions,
                            anchor,
                            artifact_context,
                        )
                        retyped_mentions += changes
                        mentions = self._link_mentions_to_kg0(mentions)
                suppressed_for_anchor = [
                    mention
                    for mention in mentions
                    if mention.qualifiers.get("type_resolution", {}).get("ambiguous")
                    and mention.confidence < self.config.mention_confidence_threshold
                ]
                if suppressed_for_anchor:
                    suppressed_mentions_map[anchor_id] = suppressed_for_anchor
                    suppressed_mentions += len(suppressed_for_anchor)
                # Filter by confidence threshold
                filtered = [
                    m
                    for m in mentions
                    if m.confidence >= self.config.mention_confidence_threshold
                ]
                all_mentions[anchor_id] = filtered

        total_anchors = sum(len(a) for a in all_anchors.values())
        total_mentions = sum(len(m) for m in all_mentions.values())
        logger.info(
            f"  Extracted {total_anchors} anchors, {total_mentions} mentions "
            f"({retyped_mentions} retyped, {suppressed_mentions} suppressed)"
        )

        return all_anchors, all_mentions, suppressed_mentions_map

    def _make_metadata_mention(
        self,
        anchor: Anchor,
        surface: str,
        category: str,
        confidence: float,
        source: str,
    ) -> Mention:
        normalized = self._normalize_phrase(surface)
        return Mention(
            mention_id=Mention.generate_id(),
            anchor_id=anchor.anchor_id,
            surface=surface,
            category=category,
            category_scores={category: confidence},
            normalized=normalized,
            confidence=confidence,
            span_start=0,
            span_end=min(len(anchor.raw_text or ""), len(surface)),
            qualifiers={
                "metadata_source": source,
                "type_resolution": {
                    "original_category": category,
                    "resolved_category": category,
                    "resolved_score": confidence,
                    "resolution_margin": confidence,
                    "ambiguous": False,
                },
            },
        )

    def _inject_metadata_mentions(
        self,
        artifact_data: Dict[str, Any],
        anchors: List[Anchor],
        mention_map: Dict[str, List[Mention]],
        intent: IntentObject,
    ) -> Dict[str, List[Mention]]:
        if not anchors:
            return mention_map

        primary_anchor = anchors[0]
        bucket = list(mention_map.get(primary_anchor.anchor_id, []))
        existing = {
            (m.category, self._normalize_phrase(m.surface))
            for m in bucket
        }

        collection_surface = ""
        for hint in intent.entity_hints:
            if str(hint.category or "").upper() == "ENTITY_COLLECTION" and str(hint.surface or "").strip():
                collection_surface = str(hint.surface).strip()
                break
        if not collection_surface:
            collection_surface = str(
                artifact_data.get("collection")
                or artifact_data.get("scope_notes")
                or ""
            ).strip()

        if collection_surface:
            key = ("ENTITY_COLLECTION", self._normalize_phrase(collection_surface))
            if key not in existing:
                bucket.append(
                    self._make_metadata_mention(
                        primary_anchor,
                        collection_surface,
                        "ENTITY_COLLECTION",
                        0.78,
                        "artifact_metadata",
                    )
                )

        mention_map[primary_anchor.anchor_id] = bucket
        return mention_map

    def _normalize_phrase(self, value: Any) -> str:
        return _normalize_phrase(value)

    def _tokenize_phrase(self, value: Any) -> List[str]:
        return _tokenize_phrase(value)

    def _person_name_variants(self, value: Any) -> Set[str]:
        return _person_name_variants(value)

    def _document_name_variants(self, value: Any) -> Set[str]:
        return _document_name_variants(value)

    def _anchor_component_types(self, anchor: Anchor) -> Set[str]:
        component_types: Set[str] = set()
        for component in anchor.path or []:
            component_type = getattr(component, "component_type", None)
            if component_type is None and isinstance(component, dict):
                component_type = component.get("component_type")
            if component_type:
                component_types.add(str(component_type).strip().lower())
        return component_types

    def _looks_like_filename(self, surface: str) -> bool:
        return _looks_like_filename(surface)

    def _normalize_score_map(self, scores: Dict[str, float]) -> Dict[str, float]:
        clipped = {category: max(0.0, value) for category, value in scores.items()}
        total = sum(clipped.values())
        if total <= 0:
            return {}
        normalized = {
            category: round(value / total, 4)
            for category, value in clipped.items()
            if value > 0
        }
        return normalized

    def _resolution_margin(self, normalized_scores: Dict[str, float]) -> float:
        ranked = sorted(normalized_scores.values(), reverse=True)
        if len(ranked) < 2:
            return ranked[0] if ranked else 0.0
        return round(ranked[0] - ranked[1], 4)

    def _should_suppress_ambiguous_mention(
        self,
        tokens: List[str],
        best_category: str,
        best_score: float,
        margin: float,
    ) -> bool:
        if not tokens:
            return False

        head = tokens[-1]
        if len(tokens) == 1 and head in self._FUNCTION_TOKENS:
            return best_score < 0.55 or margin < 0.12

        if len(tokens) == 1 and head in self._GENERIC_CONCEPT_SINGLETONS:
            return best_score < 0.72 or margin < 0.22

        if len(tokens) == 1 and head in self._GENERIC_POLICY_SINGLETONS:
            return best_score < 0.78 or margin < 0.24

        if len(tokens) == 1 and head in self._GENERIC_RISK_SINGLETONS:
            return best_score < 0.8 or margin < 0.24

        if len(tokens) == 1 and best_category in {
            "ENTITY_ORGANIZATION",
            "ENTITY_ROLE",
            "ENTITY_POLICY",
            "ENTITY_DOCUMENT",
            "ENTITY_CLAIM",
        }:
            return best_score < 0.5 and margin < 0.1

        if (
            len(tokens) == 1
            and head.endswith("ing")
            and best_category in {"ENTITY_ACTION_ITEM", "ENTITY_DOCUMENT"}
        ):
            return best_score < 0.72 or margin < 0.18

        if (
            best_category == "ENTITY_ROLE"
            and (len(tokens) >= 3 or head not in self._ROLE_TOKENS)
        ):
            return best_score < 0.74 or margin < 0.18

        if (
            best_category == "ENTITY_COLLECTION"
            and any(token in self._COLLECTION_TOKENS for token in tokens)
        ):
            return best_score < 0.78 or margin < 0.18

        return False

    def _build_artifact_context(self, artifact_data: Dict[str, Any]) -> Dict[str, Set[str]]:
        context: Dict[str, Set[str]] = {
            "collections": set(),
            "people": set(),
            "participants": set(),
            "organizations": set(),
            "roles": set(),
            "topics": set(),
            "documents": set(),
            "claims": set(),
        }

        for field_name in ("collection", "scope_notes"):
            normalized = self._normalize_phrase(artifact_data.get(field_name, ""))
            if normalized:
                context["collections"].add(normalized)

        for person in artifact_data.get("people", []) or []:
            if isinstance(person, dict):
                context["people"].update(self._person_name_variants(person.get("name", "")))
                role = self._normalize_phrase(person.get("role", ""))
                if role:
                    context["roles"].add(role)
                organization = self._normalize_phrase(person.get("organization", ""))
                if organization:
                    context["organizations"].add(organization)
            else:
                context["people"].update(self._person_name_variants(person))

        for participant in artifact_data.get("participants", []) or []:
            context["participants"].update(self._person_name_variants(participant))

        for message in artifact_data.get("messages", []) or []:
            if not isinstance(message, dict):
                continue
            context["participants"].update(
                self._person_name_variants(message.get("sender", ""))
            )
            for recipient in message.get("recipients", []) or []:
                context["participants"].update(self._person_name_variants(recipient))

        for organization in artifact_data.get("organizations", []) or []:
            normalized = self._normalize_phrase(organization)
            if normalized:
                context["organizations"].add(normalized)

        for role in artifact_data.get("roles", []) or []:
            normalized = self._normalize_phrase(role)
            if normalized:
                context["roles"].add(normalized)

        for topic in artifact_data.get("topics", []) or []:
            normalized = self._normalize_phrase(topic)
            if normalized:
                context["topics"].add(normalized)

        for field_name in (
            "title",
            "subject",
            "summary",
            "source_file_name",
            "record_id",
            "source_id",
        ):
            context["documents"].update(
                self._document_name_variants(artifact_data.get(field_name, ""))
            )

        for message in artifact_data.get("messages", []) or []:
            if not isinstance(message, dict):
                continue
            context["documents"].update(
                self._document_name_variants(message.get("subject", ""))
            )

        for claim in artifact_data.get("claims", []) or []:
            if isinstance(claim, dict):
                candidate = (
                    claim.get("text")
                    or claim.get("claim")
                    or claim.get("summary")
                    or claim.get("description")
                )
                normalized = self._normalize_phrase(candidate)
                if normalized:
                    context["claims"].add(normalized)
            else:
                normalized = self._normalize_phrase(claim)
                if normalized:
                    context["claims"].add(normalized)

        return context

    def _matches_metadata_value(self, surface_norm: str, values: Set[str]) -> bool:
        return _matches_metadata_value(surface_norm, values)

    def _looks_like_person_name(self, surface: str) -> bool:
        tokens = self._tokenize_phrase(surface)
        if len(tokens) not in {2, 3}:
            return False
        if any(
            token in (
                self._ROLE_TOKENS
                | self._ORGANIZATION_TOKENS
                | self._CONCEPT_TOKENS
                | self._COLLECTION_TOKENS
                | self._DOCUMENT_TOKENS
                | self._POLICY_TOKENS
                | self._ACTION_ITEM_TOKENS
                | self._CLAIM_TOKENS
                | self._RISK_TOKENS
                | self._EVENT_TOKENS
                | self._LOCATION_TOKENS
                | self._FUNCTION_TOKENS
                | self._PERSON_SECTION_TOKENS
                | self._PERSON_INSTITUTION_TOKENS
                | self._PERSON_INSTRUMENT_TOKENS
                | self._PERSON_CONDITION_TOKENS
            )
            for token in tokens
        ):
            return False
        raw_tokens = [part for part in re.split(r"\s+", str(surface or "").strip()) if part]
        if len(raw_tokens) < 2:
            return False
        return all(token[:1].isupper() for token in raw_tokens[: len(tokens)])

    def _context_window(self, text: str, start: int, end: int, width: int = 48) -> str:
        return _context_window(text, start, end, width)

    def _is_disclaimer_context(self, text: str) -> bool:
        lowered = str(text or "").lower()
        if not lowered:
            return False
        return any(cue in lowered for cue in self._DISCLAIMER_CUES)

    def _mention_in_disclaimer_context(
        self,
        mention: Mention,
        anchor: Optional[Anchor],
    ) -> bool:
        if anchor is None:
            return False
        context_window = self._context_window(
            anchor.raw_text,
            mention.span_start,
            mention.span_end,
            width=120,
        )
        return self._is_disclaimer_context(context_window)

    def _kg_link_candidate_labels(self, category: str) -> List[str]:
        category_key = str(category or "").strip().upper()
        labels: List[str] = []
        seen: Set[str] = set()
        for label in self.config.kg0_labels_for_category(category):
            if label and label not in seen:
                labels.append(label)
                seen.add(label)
        for label in self._KG_LINK_LABEL_OVERRIDES.get(category_key, []):
            if label and label not in seen:
                labels.append(label)
                seen.add(label)
        return labels

    def _kg_link_field_names(self, category: str) -> List[str]:
        category_key = str(category or "").strip().upper()
        fields: List[str] = []
        seen: Set[str] = set()
        for field_name in self._KG_LINK_FIELD_PRIORITIES.get(category_key, []):
            if field_name not in seen:
                fields.append(field_name)
                seen.add(field_name)
        for field_name in self._KG_LINK_FIELD_PRIORITIES["DEFAULT"]:
            if field_name not in seen:
                fields.append(field_name)
                seen.add(field_name)
        return fields

    def _kg_link_text_expression(self, alias: str, field_name: str) -> str:
        field_expr = f"{alias}.{field_name}"
        return (
            f"CASE "
            f"WHEN {field_expr} IS NULL THEN '' "
            f"WHEN valueType({field_expr}) STARTS WITH 'LIST' "
            f"THEN toLower(trim(reduce(acc = '', item IN {field_expr} | acc + ' ' + toString(item)))) "
            f"ELSE toLower(toString({field_expr})) "
            f"END"
        )

    def _kg_link_display_expression(self, alias: str, field_names: List[str]) -> str:
        if not field_names:
            return "''"
        values = ", ".join(
            (
                f"CASE "
                f"WHEN {alias}.{field_name} IS NULL THEN NULL "
                f"WHEN valueType({alias}.{field_name}) STARTS WITH 'LIST' "
                f"THEN trim(reduce(acc = '', item IN {alias}.{field_name} | acc + ' ' + toString(item))) "
                f"ELSE toString({alias}.{field_name}) "
                f"END"
            )
            for field_name in field_names
        )
        return f"coalesce({values}, '')"

    def _link_hint_tokens(self, surface_norm: str, category: str = "") -> List[str]:
        return _link_hint_tokens(
            surface_norm,
            category,
            stopwords=self._LINK_STOPWORDS,
        )

    def _score_kg_link_candidate(
        self,
        surface_norm: str,
        candidate_text: str,
        category: str = "",
    ) -> float:
        return _score_kg_link_candidate(
            surface_norm,
            candidate_text,
            category,
            legal_entity_tokens=self._ORGANIZATION_LEGAL_ENTITY_TOKENS,
            modifier_tokens=self._ORGANIZATION_LINK_MODIFIER_TOKENS,
        )

    def _organization_subset_link_bonus(
        self,
        surface_tokens: Set[str],
        candidate_tokens: Set[str],
    ) -> float:
        return _organization_subset_link_bonus(
            surface_tokens,
            candidate_tokens,
            legal_entity_tokens=self._ORGANIZATION_LEGAL_ENTITY_TOKENS,
            modifier_tokens=self._ORGANIZATION_LINK_MODIFIER_TOKENS,
        )

    def _lookup_kg0_entity_link(
        self,
        category: str,
        surface_norm: str,
    ) -> Dict[str, Any]:
        if not category or not surface_norm:
            return {"resolved_id": None, "candidates": []}

        cache_key = (category, surface_norm)
        if cache_key in self._mention_link_cache:
            return self._mention_link_cache[cache_key]

        candidate_labels = [
            label
            for label in self._kg_link_candidate_labels(category)
            if self.index.neo4j.label_exists(label)
        ]
        if not candidate_labels:
            result = {"resolved_id": None, "candidates": []}
            self._mention_link_cache[cache_key] = result
            return result

        hint_tokens = self._link_hint_tokens(surface_norm, category)
        cypher_candidates: Dict[str, Dict[str, Any]] = {}
        field_names = self._kg_link_field_names(category)
        text_expressions = [
            self._kg_link_text_expression("n", field_name)
            for field_name in field_names
        ]
        text_list_expr = ", ".join(text_expressions)
        display_expr = self._kg_link_display_expression("n", field_names)

        for label in candidate_labels[:3]:
            cypher = (
                f"MATCH (n:{label}) "
                f"WITH n, [{text_list_expr}] AS candidate_texts "
                f"WITH n, [text IN candidate_texts WHERE text <> '' AND text <> '[]'] AS candidate_texts "
                f"WITH n, candidate_texts, "
                f"[text IN candidate_texts WHERE text = $surface "
                f"OR ANY(hint IN $hints WHERE text CONTAINS hint)] AS matched_texts "
                f"WHERE size(matched_texts) > 0 "
                f"RETURN {_node_id_expression(self.index.neo4j, 'n', as_name='id')}, "
                f"labels(n) AS labels, "
                f"{display_expr} AS name, "
                f"head(matched_texts) AS matched_text "
                f"LIMIT 8"
            )
            try:
                rows = self.index.neo4j.execute_cypher(
                    cypher,
                    {"surface": surface_norm, "hints": hint_tokens or [surface_norm]},
                )
            except _NEO4J_QUERY_ERRORS as exc:
                # Missing label, timeout, or transport drop; skip this
                # surface and try the next. Programmer errors still
                # propagate so the caller sees them.
                logger.debug(
                    "KG0 surface-expansion Cypher failed for %r (%s): %s",
                    surface_norm,
                    type(exc).__name__,
                    exc,
                )
                continue
            for row in rows:
                candidate_id = str(row.get("id", "")).strip()
                if not candidate_id:
                    continue
                candidate_text = str(
                    row.get("matched_text")
                    or row.get("name")
                    or ""
                ).strip()
                score = self._score_kg_link_candidate(
                    surface_norm,
                    candidate_text,
                    category=category,
                )
                existing = cypher_candidates.get(candidate_id)
                payload = {
                    "id": candidate_id,
                    "label": label,
                    "name": str(row.get("name", "")).strip() or candidate_text,
                    "matched_text": str(row.get("matched_text", "")).strip(),
                    "score": score,
                }
                if existing is None or payload["score"] > existing["score"]:
                    cypher_candidates[candidate_id] = payload

        ranked = sorted(
            cypher_candidates.values(),
            key=lambda item: (item["score"], item["name"], item["id"]),
            reverse=True,
        )
        top_candidates = ranked[:3]
        if not top_candidates:
            result = {"resolved_id": None, "candidates": []}
            self._mention_link_cache[cache_key] = result
            return result

        best = top_candidates[0]
        second_score = top_candidates[1]["score"] if len(top_candidates) > 1 else 0.0
        margin = round(best["score"] - second_score, 4)
        resolved_id: Optional[str] = None
        category_key = str(category or "").strip().upper()
        best_match_norm = self._normalize_phrase(
            best.get("matched_text") or best.get("name") or ""
        )
        if category_key in {"ENTITY_ROLE", "ROLE"}:
            if best["score"] >= 0.88 and best_match_norm == surface_norm:
                resolved_id = best["id"]
        else:
            if best["score"] >= 0.95:
                resolved_id = best["id"]
            elif best["score"] >= 0.78 and margin >= 0.10:
                resolved_id = best["id"]

        result = {
            "resolved_id": resolved_id,
            "candidates": top_candidates,
            "best_score": best["score"],
            "margin": margin,
        }
        self._mention_link_cache[cache_key] = result
        return result

    def _link_mentions_to_kg0(self, mentions: List[Mention]) -> List[Mention]:
        for mention in mentions:
            if mention.kg0_entity_id:
                continue
            surface_norm = self._normalize_phrase(
                mention.normalized or mention.surface
            )
            link_result = self._lookup_kg0_entity_link(
                mention.category,
                surface_norm,
            )
            mention.qualifiers.setdefault("kg_link", {})
            mention.qualifiers["kg_link"].update(
                {
                    "matched_surface": surface_norm,
                    "candidates": link_result.get("candidates", []),
                    "best_score": link_result.get("best_score", 0.0),
                    "margin": link_result.get("margin", 0.0),
                }
            )
            resolved_id = link_result.get("resolved_id")
            if not resolved_id:
                mention.qualifiers["kg_link"]["match_type"] = "candidate_only"
                continue
            mention.kg0_entity_id = resolved_id
            mention.qualifiers["kg_link"].update(
                {
                    "kg0_entity_id": resolved_id,
                    "match_type": (
                        "exact_label_text"
                        if link_result.get("best_score", 0.0) >= 0.95
                        else "fuzzy_label_text"
                    ),
                }
            )
        return mentions

    def _resolve_mentions_for_anchor(
        self,
        mentions: List[Mention],
        anchor: Anchor,
        artifact_context: Dict[str, Set[str]],
    ) -> Tuple[List[Mention], int]:
        changes = 0
        for mention in mentions:
            original_category = mention.category
            surface_norm = self._normalize_phrase(mention.surface)
            tokens = self._tokenize_phrase(mention.surface)
            if not surface_norm or not tokens:
                continue

            scores = {category: 0.0 for category in self._RESOLUTION_CATEGORIES}
            scores[original_category] = max(0.45, mention.confidence)

            head = tokens[-1]
            context_window = self._context_window(
                anchor.raw_text,
                mention.span_start,
                mention.span_end,
            )
            component_types = self._anchor_component_types(anchor)
            header_like_anchor = bool(component_types & {"header", "subject", "title"})

            if self._matches_metadata_value(surface_norm, artifact_context["collections"]):
                scores["ENTITY_COLLECTION"] += 0.80
            if self._matches_metadata_value(surface_norm, artifact_context["people"]):
                scores["ENTITY_PERSON"] += 0.65
            if self._matches_metadata_value(surface_norm, artifact_context["participants"]):
                scores["ENTITY_PERSON"] += 0.45
            if self._matches_metadata_value(surface_norm, artifact_context["organizations"]):
                scores["ENTITY_ORGANIZATION"] += 0.65
            if self._matches_metadata_value(surface_norm, artifact_context["roles"]):
                scores["ENTITY_ROLE"] += 0.55
            if self._matches_metadata_value(surface_norm, artifact_context["documents"]):
                scores["ENTITY_DOCUMENT"] += 0.75
            if self._matches_metadata_value(surface_norm, artifact_context["claims"]):
                scores["ENTITY_CLAIM"] += 0.55
            if self._matches_metadata_value(surface_norm, artifact_context["topics"]):
                scores["ENTITY_CONCEPT"] += 0.30

            if head in self._ROLE_TOKENS or any(token in self._ROLE_TOKENS for token in tokens):
                scores["ENTITY_ROLE"] += 0.55
            if head in self._COLLECTION_TOKENS or any(token in self._COLLECTION_TOKENS for token in tokens):
                scores["ENTITY_COLLECTION"] += 0.45
            if head in self._ACTION_ITEM_TOKENS or any(token in self._ACTION_ITEM_TOKENS for token in tokens):
                scores["ENTITY_ACTION_ITEM"] += 0.45
            if head in self._DOCUMENT_TOKENS or any(token in self._DOCUMENT_TOKENS for token in tokens):
                scores["ENTITY_DOCUMENT"] += 0.55
            if head in self._CLAIM_TOKENS or any(token in self._CLAIM_TOKENS for token in tokens):
                scores["ENTITY_CLAIM"] += 0.35
            if head in self._RISK_TOKENS or any(token in self._RISK_TOKENS for token in tokens):
                scores["ENTITY_RISK_FINDING"] += 0.45
            if head in self._ORGANIZATION_TOKENS or any(token in self._ORGANIZATION_TOKENS for token in tokens):
                scores["ENTITY_ORGANIZATION"] += 0.55
            if head in self._CONCEPT_TOKENS or any(token in self._CONCEPT_TOKENS for token in tokens):
                scores["ENTITY_CONCEPT"] += 0.55
            if any(token in self._DOCUMENT_LIKE_FORM_TOKENS for token in tokens):
                scores["ENTITY_DOCUMENT"] += 0.30
                scores["ENTITY_CONCEPT"] -= 0.12
            if head in self._POLICY_TOKENS or any(token in self._POLICY_TOKENS for token in tokens):
                scores["ENTITY_POLICY"] += 0.40
            if head in self._EVENT_TOKENS or any(token in self._EVENT_TOKENS for token in tokens):
                scores["ENTITY_EVENT"] += 0.45
            if head in self._LOCATION_TOKENS or any(token in self._LOCATION_TOKENS for token in tokens):
                scores["ENTITY_LOCATION"] += 0.55

            if self._looks_like_person_name(mention.surface):
                scores["ENTITY_PERSON"] += 0.25
            if self._looks_like_filename(mention.surface):
                scores["ENTITY_DOCUMENT"] += 0.85
            if (
                header_like_anchor
                and mention.span_start <= 120
                and len(tokens) >= 2
                and original_category in {
                    "ENTITY_STRATEGY",
                    "ENTITY_POLICY",
                    "ENTITY_CONCEPT",
                    "ENTITY_EVENT",
                    "ENTITY_DOCUMENT",
                }
            ):
                scores["ENTITY_DOCUMENT"] += 0.45

            if any(cue in context_window for cue in ("department", "team", "office", "group", "committee", "board", "unit")):
                scores["ENTITY_ORGANIZATION"] += 0.20
            if any(cue in context_window for cue in ("attorney", "counsel", "officer", "supervisor", "manager", "employee", "pharmacist")):
                scores["ENTITY_ROLE"] += 0.20
            if any(cue in context_window for cue in ("attachment", "attached", "document", "memo", "report", "draft", "presentation", "deck", "subject", "web form", "web-form")):
                scores["ENTITY_DOCUMENT"] += 0.30
            if any(
                cue in context_window
                for cue in (
                    "web form",
                    "web-form",
                    "questionnaire",
                    "template",
                    "templates",
                    "fill out",
                    "enter the information",
                    "enter and gather information",
                    "listed on the document",
                )
            ):
                scores["ENTITY_DOCUMENT"] += 0.24
                scores["ENTITY_CONCEPT"] -= 0.08
            if any(cue in context_window for cue in ("policy", "procedure", "protocol", "form", "attachment", "document", "report", "training")):
                scores["ENTITY_CONCEPT"] += 0.20
            if any(cue in context_window for cue in ("caused", "concerned", "concern", "imply", "indicate", "indicator", "increase", "decrease", "risk", "noncompliance")):
                scores["ENTITY_CLAIM"] += 0.18
            if any(cue in context_window for cue in ("red-flag", "red flag", "pharmacy-shopping", "doctor-shopping", "inappropriate prescription", "diversion", "abuse")):
                scores["ENTITY_RISK_FINDING"] += 0.25
            if any(cue in context_window for cue in ("review", "implementation", "response", "respond", "forward comments", "next steps", "unresolved", "silence")):
                scores["ENTITY_ACTION_ITEM"] += 0.22
            if any(cue in context_window for cue in ("meeting", "conference", "hearing", "call", "review", "audit")):
                scores["ENTITY_EVENT"] += 0.20
            if any(cue in context_window for cue in ("road", "street", "drive", "avenue", "boulevard", "county", "state")):
                scores["ENTITY_LOCATION"] += 0.20
            if self._is_disclaimer_context(context_window):
                scores["ENTITY_ROLE"] -= 0.65
                scores["ENTITY_DOCUMENT"] += 0.12
                scores["ENTITY_CONCEPT"] += 0.06

            if tokens and tokens[0] in self._PERSON_SECTION_TOKENS:
                scores["ENTITY_DOCUMENT"] += 0.42
                scores["ENTITY_CONCEPT"] += 0.12
                scores["ENTITY_PERSON"] -= 0.75
            if any(token in self._PERSON_INSTITUTION_TOKENS for token in tokens):
                scores["ENTITY_ORGANIZATION"] += 0.48
                scores["ENTITY_DOCUMENT"] += 0.12
                scores["ENTITY_PERSON"] -= 0.75
            if any(token in self._PERSON_INSTRUMENT_TOKENS for token in tokens):
                scores["ENTITY_DOCUMENT"] += 0.52
                scores["ENTITY_CONCEPT"] += 0.10
                scores["ENTITY_PERSON"] -= 0.78
            if any(token in self._PERSON_CONDITION_TOKENS for token in tokens):
                scores["ENTITY_CONCEPT"] += 0.52
                scores["ENTITY_DOCUMENT"] += 0.10
                scores["ENTITY_PERSON"] -= 0.78

            if len(tokens) == 1 and head in self._FUNCTION_TOKENS:
                scores["ENTITY_ORGANIZATION"] += 0.25
                scores["ENTITY_POLICY"] += 0.20
                scores["ENTITY_ROLE"] -= 0.20
            if len(tokens) == 1 and head in self._GENERIC_CONCEPT_SINGLETONS:
                scores["ENTITY_CONCEPT"] -= 0.35
                scores["ENTITY_DOCUMENT"] -= 0.20
            if len(tokens) == 1 and head in self._GENERIC_POLICY_SINGLETONS:
                scores["ENTITY_POLICY"] -= 0.45
                scores["ENTITY_CONCEPT"] += 0.10
                if not any(
                    cue in context_window
                    for cue in (
                        "policy",
                        "procedure",
                        "protocol",
                        "requirement",
                        "guidance",
                        "regulation",
                        "compliance",
                    )
                ):
                    scores["ENTITY_POLICY"] -= 0.25
                    scores["ENTITY_CONCEPT"] += 0.10
            if len(tokens) == 1 and head in self._GENERIC_RISK_SINGLETONS:
                scores["ENTITY_RISK_FINDING"] -= 0.45
                scores["ENTITY_CONCEPT"] += 0.08
            if (
                len(tokens) == 1
                and head.endswith("ing")
                and original_category == "ENTITY_ACTION_ITEM"
            ):
                scores["ENTITY_ACTION_ITEM"] -= 0.40
                scores["ENTITY_DOCUMENT"] -= 0.10
                scores["ENTITY_CLAIM"] += 0.06
            if "/" in mention.surface and any(
                token in self._FUNCTION_TOKENS for token in tokens
            ):
                scores["ENTITY_ORGANIZATION"] += 0.35
                scores["ENTITY_ROLE"] -= 0.30
            if header_like_anchor and any(
                token in self._ROLE_NOISE_TOKENS for token in tokens
            ):
                scores["ENTITY_ROLE"] -= 0.40
                scores["ENTITY_EVENT"] += 0.12
                scores["ENTITY_DOCUMENT"] += 0.18
            if (
                len(tokens) >= 3
                and any(token in self._ROLE_NOISE_TOKENS for token in tokens)
                and head not in self._ROLE_TOKENS
            ):
                scores["ENTITY_ROLE"] -= 0.35
            if (
                len(tokens) == 1
                and head in self._ROLE_TOKENS
                and str(mention.surface or "").strip().lower()
                == str(mention.surface or "").strip()
            ):
                scores["ENTITY_ROLE"] -= 0.28
                scores["ENTITY_CLAIM"] += 0.10
                scores["ENTITY_RISK_FINDING"] += 0.06
            if len(tokens) >= 2 and (
                "operational" in tokens or "response" in tokens or "silence" in tokens
            ):
                scores["ENTITY_ACTION_ITEM"] += 0.22
            if (
                original_category == "ENTITY_ACTION_ITEM"
                and len(tokens) >= 3
                and any(token in self._CLAUSAL_FINDING_TOKENS for token in tokens)
            ):
                scores["ENTITY_ACTION_ITEM"] -= 0.52
                scores["ENTITY_CLAIM"] += 0.50
                scores["ENTITY_DOCUMENT"] += 0.06
            if {"red", "flag"} <= set(tokens) or "shopping" in tokens:
                scores["ENTITY_RISK_FINDING"] += 0.22
            if (
                "program" in tokens
                and any(token in self._PROGRAM_ENTITY_TOKENS for token in tokens)
            ) or (
                "program" in tokens
                and any(
                    cue in context_window
                    for cue in (
                        "funded by",
                        "funding",
                        "grant",
                        "award",
                        "sponsored by",
                        "registration number",
                    )
                )
            ):
                scores["ENTITY_ORGANIZATION"] += 0.26
                scores["ENTITY_DOCUMENT"] += 0.22
                scores["ENTITY_STRATEGY"] -= 0.32
            if any(token in self._DOCUMENTISH_EVENT_TOKENS for token in tokens):
                scores["ENTITY_DOCUMENT"] += 0.12
                if original_category == "ENTITY_EVENT":
                    scores["ENTITY_EVENT"] -= 0.10

            if (
                original_category == "ENTITY_PERSON"
                and any(
                    token in (
                        self._ROLE_TOKENS
                        | self._ACTION_ITEM_TOKENS
                        | self._ORGANIZATION_TOKENS
                        | self._COLLECTION_TOKENS
                        | self._DOCUMENT_TOKENS
                        | self._POLICY_TOKENS
                        | self._ACTION_ITEM_TOKENS
                        | self._CLAIM_TOKENS
                        | self._CONCEPT_TOKENS
                        | self._EVENT_TOKENS
                        | self._LOCATION_TOKENS
                        | self._RISK_TOKENS
                        | self._FUNCTION_TOKENS
                        | self._PERSON_SECTION_TOKENS
                        | self._PERSON_INSTITUTION_TOKENS
                        | self._PERSON_INSTRUMENT_TOKENS
                        | self._PERSON_CONDITION_TOKENS
                    )
                    for token in tokens
                )
            ):
                scores["ENTITY_PERSON"] -= 0.55

            normalized_scores = self._normalize_score_map(scores)
            if not normalized_scores:
                continue

            best_category = max(normalized_scores, key=normalized_scores.get)
            best_score = normalized_scores[best_category]
            original_score = normalized_scores.get(original_category, 0.0)
            margin = self._resolution_margin(normalized_scores)
            suppress_ambiguous = self._should_suppress_ambiguous_mention(
                tokens,
                best_category,
                best_score,
                margin,
            )

            force_strategy_program_document = (
                original_category == "ENTITY_STRATEGY"
                and "program" in tokens
                and (
                    any(token in self._PROGRAM_ENTITY_TOKENS for token in tokens)
                    or any(
                        cue in context_window
                        for cue in (
                            "funded by",
                            "funding",
                            "grant",
                            "award",
                            "sponsored by",
                            "registration number",
                        )
                    )
                )
            )
            force_action_clause_claim = (
                original_category == "ENTITY_ACTION_ITEM"
                and len(tokens) >= 3
                and any(token in self._CLAUSAL_FINDING_TOKENS for token in tokens)
            )
            force_generic_policy_concept = (
                original_category == "ENTITY_POLICY"
                and len(tokens) == 1
                and head in self._GENERIC_POLICY_SINGLETONS
                and not any(
                    cue in context_window
                    for cue in (
                        "policy",
                        "procedure",
                        "protocol",
                        "requirement",
                        "guidance",
                        "regulation",
                        "compliance",
                    )
                )
            )
            force_weak_gerund_suppress = (
                original_category == "ENTITY_ACTION_ITEM"
                and len(tokens) == 1
                and head.endswith("ing")
            )

            if force_strategy_program_document:
                candidate_category = (
                    "ENTITY_DOCUMENT"
                    if normalized_scores.get("ENTITY_DOCUMENT", 0.0)
                    >= normalized_scores.get("ENTITY_ORGANIZATION", 0.0)
                    else "ENTITY_ORGANIZATION"
                )
                best_category = candidate_category
                best_score = max(
                    best_score,
                    normalized_scores.get(candidate_category, 0.0),
                )
                suppress_ambiguous = False

            if force_action_clause_claim:
                best_category = "ENTITY_CLAIM"
                best_score = max(
                    best_score,
                    normalized_scores.get("ENTITY_CLAIM", 0.0),
                    0.55,
                )
                suppress_ambiguous = False

            if force_generic_policy_concept:
                best_category = "ENTITY_CONCEPT"
                best_score = max(
                    normalized_scores.get("ENTITY_CONCEPT", 0.0),
                    0.32,
                )
                suppress_ambiguous = True

            if force_weak_gerund_suppress:
                suppress_ambiguous = True

            mention.category_scores = normalized_scores
            mention.qualifiers.setdefault("type_resolution", {})
            mention.qualifiers["type_resolution"].update(
                {
                    "original_category": original_category,
                    "resolved_category": best_category,
                    "resolved_score": best_score,
                    "resolution_margin": margin,
                    "ambiguous": suppress_ambiguous,
                }
            )

            if suppress_ambiguous:
                suppression_penalty = 0.2
                if (
                    len(tokens) == 1
                    and (
                        head.endswith("ing")
                        or head in self._GENERIC_POLICY_SINGLETONS
                        or head in self._GENERIC_RISK_SINGLETONS
                    )
                ):
                    suppression_penalty = 0.45
                mention.confidence = min(
                    mention.confidence,
                    max(0.05, round(best_score - suppression_penalty, 4)),
                )
                if force_weak_gerund_suppress:
                    mention.confidence = min(mention.confidence, 0.05)
            else:
                mention.confidence = max(
                    mention.confidence,
                    round(best_score, 4),
                )

            rewrite_margin = 0.12
            if (
                original_category == "ENTITY_CONCEPT"
                and best_category == "ENTITY_DOCUMENT"
                and any(token in self._DOCUMENT_LIKE_FORM_TOKENS for token in tokens)
            ):
                rewrite_margin = 0.02
            elif (
                original_category == "ENTITY_PERSON"
                and best_category != "ENTITY_PERSON"
            ):
                rewrite_margin = 0.02
            elif (
                original_category in {"ENTITY_EVENT", "ENTITY_STRATEGY"}
                and best_category == "ENTITY_DOCUMENT"
                and (
                    any(token in self._DOCUMENTISH_EVENT_TOKENS for token in tokens)
                    or "program" in tokens
                )
            ):
                rewrite_margin = 0.02
            elif (
                original_category == "ENTITY_ACTION_ITEM"
                and best_category != "ENTITY_ACTION_ITEM"
                and (
                    (len(tokens) == 1 and head.endswith("ing"))
                    or any(token in self._CLAUSAL_FINDING_TOKENS for token in tokens)
                )
            ):
                rewrite_margin = 0.02

            if (
                best_category != original_category
                and (
                    force_strategy_program_document
                    or force_action_clause_claim
                    or force_generic_policy_concept
                    or best_score >= original_score + rewrite_margin
                )
            ):
                mention.category = best_category
                changes += 1

        return mentions, changes


# ============================================================
# Phase 4: Entity and Link Hypothesis Generation
# ============================================================
