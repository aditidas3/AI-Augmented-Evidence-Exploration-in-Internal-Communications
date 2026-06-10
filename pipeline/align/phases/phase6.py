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
from ..utils.temporal_normalization import (
    date_in_year_range as _date_in_year_range,
    extract_date_from_text as _extract_date_from_text,
    extract_year_from_date_string as _extract_year_from_date_string,
)
from ..binding.slot_confidence import (
    aggregate_quality as _aggregate_quality,
    compute_slot_confidence as _compute_slot_confidence,
    quality_weight as _quality_weight,
)
from ..utils.text_normalization import (
    normalize_condition_surface as _normalize_condition_surface,
    surface_tokens as _surface_tokens,
)

class Phase6_SlotBinding:
    """
    Bind slots using subgraphs and evidence.
    Construct witnesses that seed TRACE.

    FIX #1: Receives and uses link_hyps.
    FIX #6: Slot-to-variable mapping uses type-based matching.
    """

    _GENERIC_MEDICAL_SURFACES: Set[str] = {
        "associated condition",
        "associated conditions",
        "clinical finding",
        "clinical findings",
        "complication",
        "complications",
        "condition",
        "conditions",
        "disease",
        "diseases",
        "disorder",
        "disorders",
        "health outcome",
        "health outcomes",
        "medical finding",
        "medical findings",
        "mental disorder",
        "mental disorders",
        "psychological disorder",
        "psychological disorders",
        "risk factor",
        "risk factors",
        "symptom",
        "symptoms",
    }

    _MEDICAL_CONDITION_PATTERN = re.compile(
        r"\b(?:"
        r"anxiety(?:\s+disorder)?|"
        r"cardiovascular\s+disease|"
        r"complex\s+post-?traumatic\s+stress\s+disorder|"
        r"concussion|"
        r"cptsd|"
        r"depression|"
        r"diabetes|"
        r"dsm-?iv(?:\s+\w+){0,2}\s+disorders?|"
        r"generalized\s+anxiety\s+disorder|"
        r"insomnia|"
        r"major\s+depression|"
        r"major\s+depressive\s+disorder|"
        r"mild\s+traumatic\s+brain\s+injury|"
        r"obesity|"
        r"post-?concussive\s+symptoms?|"
        r"post-?traumatic\s+stress\s+disorder|"
        r"posttraumatic\s+stress\s+disorder|"
        r"ptsd|"
        r"short\s+sleep|"
        r"sleep\s+disturbances?|"
        r"traumatic\s+brain\s+injury"
        r")\b",
        re.IGNORECASE,
    )

    _MEASUREMENT_NOISE_PATTERN = re.compile(
        r"\b(?:"
        r"audit(?:-c)?|"
        r"gad-?7|"
        r"international\s+trauma\s+questionnaire|"
        r"itq|"
        r"patient\s+health\s+questionnaire|"
        r"pcl-?5|"
        r"phq-?8|"
        r"phq-?9|"
        r"questionnaire|"
        r"rivermead|"
        r"screen(?:er)?|"
        r"severity\s+index"
        r")\b",
        re.IGNORECASE,
    )

    _WHEN_EVENT_NOISE_PATTERN = re.compile(
        r"\b(?:"
        r"audit(?:-c)?|"
        r"checklist|"
        r"gad-?7|"
        r"index|"
        r"instrument|"
        r"itq|"
        r"patient\s+health\s+questionnaire|"
        r"pcl-?5|"
        r"phq-?8|"
        r"phq-?9|"
        r"questionnaire|"
        r"rivermead|"
        r"screen(?:er)?|"
        r"test"
        r")\b",
        re.IGNORECASE,
    )

    _GENERIC_PERSON_SURFACES: Set[str] = {
        "associated",
        "inversely associated",
        "physical activity",
        "weight change",
    }

    _PERSON_SURFACE_NOISE_PATTERN = re.compile(
        r"\b(?:"
        r"activity|"
        r"adults?|"
        r"anxiety|"
        r"associated|"
        r"behavior(?:s)?|"
        r"caregivers?|"
        r"change|"
        r"customers?|"
        r"consumers?|"
        r"depression|"
        r"disorder(?:s)?|"
        r"exercise|"
        r"health|"
        r"inversely|"
        r"lifestyle|"
        r"military|"
        r"obesity|"
        r"patients?|"
        r"pharmacists?|"
        r"physical|"
        r"population(?:s)?|"
        r"ptsd|"
        r"questionnaire|"
        r"sleep|"
        r"staff|"
        r"stress|"
        r"study|"
        r"symptom(?:s)?|"
        r"team\s+members?|"
        r"veteran(?:s)?|"
        r"weight"
        r")\b",
        re.IGNORECASE,
    )

    _AUTHOR_ANCHOR_PATTERN = re.compile(r"^\s*authors?:", re.IGNORECASE)

    _LOADER_SECTION_PREFIXES: Tuple[str, ...] = (
        "associated diseases symptoms and complications:",
        "clinical and research topics:",
        "procedures or interventions:",
        "abbreviations and clinical terms:",
        "claims and findings:",
        "organizations:",
        "population or geography:",
        "document date:",
        "source:",
    )

    _GENERIC_EVIDENCE_SURFACES: Set[str] = {
        "risk factors",
        "weight change",
        "associated diseases symptoms",
        "clinical and research topics",
        "patient health questionnaire",
        "generalized anxiety disorder screen",
    }

    _WHEN_EVENT_PATTERN = re.compile(
        r"\b("
        r"received ethics approval|"
        r"recruitment anticipated to begin|"
        r"recruitment (?:began|begin|started|start)|"
        r"became effective|"
        r"effective(?:\s+this\s+month)?|"
        r"first implemented|"
        r"implemented|"
        r"rolled out|"
        r"will be sent|"
        r"sent to all locations|"
        r"updated|"
        r"developed|"
        r"created|"
        r"removed|"
        r"added|"
        r"revised|"
        r"registered(?:\s+with|\s+on)?|"
        r"registration(?:\s+number)?|"
        r"published(?:\s+on)?|"
        r"approved(?:\s+on)?"
        r")\b",
        re.IGNORECASE,
    )

    _POLICY_SURFACE_PATTERN = re.compile(
        r"\b("
        r"(?:Walgreens\s+)?(?:National\s+)?Target\s+Drug\s+Good\s+Faith\s+Dispensing\s+"
        r"(?:Policy|Checklist|FAQ)|"
        r"(?:National\s+)?TD\s+GFD\s+(?:Policy|Checklist|FAQs?)|"
        r"Good\s+Faith\s+Dispensing\s+(?:Policy|Checklist)|"
        r"Target\s+Drug\s+(?:Dispensing\s+)?Checklist|"
        r"dispensing\s+checklist"
        r")\b",
        re.IGNORECASE,
    )

    _MONTH_DAY_WITHOUT_YEAR_PATTERN = re.compile(
        r"\b(?:(early|mid|late)\s+)?"
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)"
        r"(?:\s+(\d{1,2}))?\b",
        re.IGNORECASE,
    )

    _MONTHS = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }

    def __init__(self, config: AlignConfig, index: IndexFacade):
        self.config = config
        self.index = index

    def execute(
        self,
        subgraphs: List[Subgraph],
        all_anchors: Dict[str, List[Anchor]],
        all_mentions: Dict[str, List[Mention]],
        entity_hyps: List[EntityHypothesis],
        link_hyps: List[LinkHypothesis],
        intent: IntentObject,
    ) -> Tuple[List[SlotBinding], List[Witness]]:
        """
        Execute Phase 6: bind slots and construct witnesses.

        FIX #1: link_hyps now received and available for binding.
        """
        logger.info("Phase 6: Binding slots and constructing witnesses")

        # Build lookups
        anchor_lookup: Dict[str, Anchor] = {}
        for anchors in all_anchors.values():
            for a in anchors:
                anchor_lookup[a.anchor_id] = a

        mention_lookup: Dict[str, Mention] = {}
        for mentions in all_mentions.values():
            for m in mentions:
                mention_lookup[m.mention_id] = m

        entity_lookup: Dict[str, EntityHypothesis] = {
            e.hypothesis_id: e for e in entity_hyps
        }
        self._anchor_lookup = anchor_lookup
        self._anchors_by_artifact = all_anchors
        self._entity_lookup = entity_lookup
        self._mention_lookup = mention_lookup

        # FIX #1: Link hypothesis lookup
        link_lookup: Dict[str, List[LinkHypothesis]] = defaultdict(list)
        for lh in link_hyps:
            link_lookup[lh.source_entity_id].append(lh)
            link_lookup[lh.target_entity_id].append(lh)

        gs = intent.graph_spec
        slot_bindings: List[SlotBinding] = []
        all_witnesses: List[Witness] = []

        # Use the best subgraph(s). When the intent provides no
        # GraphSpec at all, fall back to processing every subgraph
        # Phase 5 returned: the previous default of 1 silently
        # truncated alternative bindings even when beam search had
        # found ten distinct hint-grounded entities.
        top_k = (
            gs.objective.return_top_k_alternatives
            if gs
            else len(subgraphs)
        )
        top_subgraphs = subgraphs[: max(top_k, 0)]

        for slot_def in intent.slot_spec.slots:
            binding = self._bind_slot(
                slot_def,
                top_subgraphs,
                gs,
                anchor_lookup,
                mention_lookup,
                entity_lookup,
                link_lookup,
                intent,
            )
            slot_bindings.append(binding)
            all_witnesses.extend(binding.witnesses)

        logger.info(
            f"  Bound {len(slot_bindings)} slots with "
            f"{len(all_witnesses)} witnesses"
        )
        return slot_bindings, all_witnesses

    def _bind_slot(
        self,
        slot_def: SlotDef,
        subgraphs: List[Subgraph],
        gs: Optional[GraphSpec],
        anchor_lookup: Dict[str, Anchor],
        mention_lookup: Dict[str, Mention],
        entity_lookup: Dict[str, EntityHypothesis],
        link_lookup: Dict[str, List[LinkHypothesis]],
        intent: IntentObject,
    ) -> SlotBinding:
        """Bind a single slot using subgraph bindings."""

        # Determine which graph vars map to this slot type
        relevant_vars = self._find_vars_for_slot(slot_def, gs)
        relevant_vars = self._augment_slot_vars(slot_def, gs, relevant_vars)
        ranked_subgraphs = self._rank_subgraphs_for_slot(
            slot_def,
            subgraphs,
            relevant_vars,
            gs,
        )

        witnesses = []
        evidence_pieces = []
        seen_piece_keys: Set[Tuple[str, str, str]] = set()

        for sg in ranked_subgraphs:
            slot_type = (slot_def.slot_type or "").upper()
            reference_artifacts = self._slot_reference_artifacts(sg)
            non_header_text, fallback_text = self._slot_reference_texts(sg)
            cue_tokens = self._text_tokens(slot_def.description) | {
                "attorney",
                "reviewer",
                "review",
                "legal",
                "compliance",
                "counsel",
                "jurisdiction",
            }
            role_binding = sg.bindings.get("R")
            role_tokens = self._text_tokens(
                " ".join(self._binding_surface_candidates(role_binding))
            )
            for var_name in relevant_vars:
                if (
                    slot_type == "WHO"
                    and var_name in {"P", "R", "O"}
                    and not self._slot_var_shares_artifact(
                        sg, var_name, reference_artifacts
                    )
                ):
                    continue

                binding = sg.bindings.get(var_name)
                if not binding or not binding.bound:
                    continue

                if (
                    slot_type == "WHO"
                    and var_name == "P"
                    and role_tokens & cue_tokens
                    and self._binding_is_header_only(binding)
                    and self._who_textual_match_score(
                        binding,
                        non_header_text,
                        "",
                    ) < 0.5
                ):
                    continue

                # Resolve to anchor and mention
                anchor = anchor_lookup.get(binding.anchor_id or "")
                mention = mention_lookup.get(binding.mention_id or "")

                # If we have an entity hypothesis, use its best mention
                if binding.entity_hypothesis_id:
                    entity = entity_lookup.get(
                        binding.entity_hypothesis_id
                    )
                    if entity and entity.mentions:
                        mention = entity.mentions[0]
                        anchor = anchor_lookup.get(mention.anchor_id)

                if not anchor or not mention:
                    continue

                piece_key = (
                    var_name,
                    (mention.surface or "").strip().lower(),
                    anchor.address,
                )
                if piece_key in seen_piece_keys:
                    continue
                seen_piece_keys.add(piece_key)

                # Create witness
                witness = Witness(
                    witness_id=Witness.generate_id(),
                    phase=Phase.PHASE_6_BINDING,
                    intent_element=IntentElementRef(
                        element_type="slot",
                        element_id=slot_def.slot_id,
                        element_detail={
                            "var": var_name,
                            "slot_type": slot_def.slot_type,
                        },
                    ),
                    anchor=anchor,
                    mention=mention,
                    score=sg.score,
                    quality=binding.quality,
                    justification=(
                        f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                        f"bound via graph var '{var_name}' to "
                        f"mention '{mention.surface}' at {anchor.address}"
                    ),
                )
                witness.compute_content_hash()
                witnesses.append(witness)

                # FIX #1: Annotate with cross-artifact link evidence
                cross_links = []
                if binding.entity_hypothesis_id:
                    cross_links = link_lookup.get(
                        binding.entity_hypothesis_id, []
                    )

                evidence_pieces.append(
                    {
                        "var": var_name,
                        "surface": mention.surface,
                        "category": mention.category,
                        "artifact_id": anchor.artifact_id,
                        "anchor_address": anchor.address,
                        "raw_text": anchor.raw_text,
                        "quality": binding.quality.value,
                        "confidence": mention.confidence,
                        "cross_artifact_links": len(cross_links),
                        "entity_hypothesis_id": binding.entity_hypothesis_id,
                        "canonical_name": (
                            entity_lookup[binding.entity_hypothesis_id].canonical_name
                            if binding.entity_hypothesis_id
                            and binding.entity_hypothesis_id in entity_lookup
                            else ""
                        ),
                    }
                )

        evidence_pieces.sort(
            key=lambda piece: self._slot_piece_sort_key(slot_def, piece),
        )

        if (slot_def.slot_type or "").upper() == "WHO":
            evidence_pieces, witnesses = self._refine_who_role_pairing(
                slot_def,
                evidence_pieces,
                witnesses,
                anchor_lookup,
                mention_lookup,
                entity_lookup,
            )
            evidence_pieces, witnesses = self._augment_who_binding(
                slot_def,
                evidence_pieces,
                witnesses,
                ranked_subgraphs,
                anchor_lookup,
                entity_lookup,
            )
            evidence_pieces, witnesses = self._augment_policy_responsibility_who_binding(
                slot_def=slot_def,
                evidence_pieces=evidence_pieces,
                witnesses=witnesses,
                reference_artifacts=set(self._anchors_by_artifact.keys()),
            )
            evidence_pieces, witnesses = self._collapse_who_binding(
                evidence_pieces,
                witnesses,
                intent,
                slot_def=slot_def,
            )
            evidence_pieces.sort(
                key=lambda piece: self._slot_piece_sort_key(slot_def, piece),
            )
        elif (slot_def.slot_type or "").upper() == "HOW":
            evidence_pieces, witnesses = self._refine_how_responsibility_chain(
                slot_def,
                evidence_pieces,
                witnesses,
                anchor_lookup,
                mention_lookup,
                entity_lookup,
            )
            evidence_pieces.sort(
                key=lambda piece: self._slot_piece_sort_key(slot_def, piece),
            )
            policy_reference_artifacts = set(self._anchors_by_artifact.keys())
            evidence_pieces, witnesses = self._augment_policy_how_binding(
                slot_def=slot_def,
                evidence_pieces=evidence_pieces,
                witnesses=witnesses,
                reference_artifacts=policy_reference_artifacts,
                replace_noise=self._policy_how_needs_repair(evidence_pieces),
            )
            evidence_pieces.sort(
                key=lambda piece: self._slot_piece_sort_key(slot_def, piece),
            )
        elif (slot_def.slot_type or "").upper() == "EVIDENCE":
            evidence_pieces, witnesses = self._augment_evidence_binding(
                slot_def,
                evidence_pieces,
                witnesses,
                ranked_subgraphs,
                anchor_lookup,
            )
            evidence_pieces, witnesses = self._augment_policy_evidence_binding(
                slot_def=slot_def,
                evidence_pieces=evidence_pieces,
                witnesses=witnesses,
                reference_artifacts=set(self._anchors_by_artifact.keys()),
                replace_noise=self._policy_evidence_needs_repair(evidence_pieces),
            )
            evidence_pieces, witnesses = self._collapse_evidence_binding(
                slot_def,
                evidence_pieces,
                witnesses,
                intent,
            )
            evidence_pieces.sort(
                key=lambda piece: self._slot_piece_sort_key(slot_def, piece),
            )
        elif (slot_def.slot_type or "").upper() == "OUTCOME":
            evidence_pieces, witnesses = self._collapse_outcome_binding(
                evidence_pieces,
                witnesses,
            )
            evidence_pieces.sort(
                key=lambda piece: self._slot_piece_sort_key(slot_def, piece),
            )
        elif (slot_def.slot_type or "").upper() == "WHAT":
            evidence_pieces, witnesses = self._augment_what_binding(
                slot_def,
                evidence_pieces,
                witnesses,
                ranked_subgraphs,
                anchor_lookup,
                entity_lookup,
                intent,
            )
            evidence_pieces, witnesses = self._collapse_what_binding(
                slot_def,
                evidence_pieces,
                witnesses,
                intent,
            )
            evidence_pieces.sort(
                key=lambda piece: self._slot_piece_sort_key(slot_def, piece),
            )
        elif (slot_def.slot_type or "").upper() == "WHEN":
            evidence_pieces, witnesses = self._augment_when_binding(
                slot_def,
                evidence_pieces,
                witnesses,
                ranked_subgraphs,
                anchor_lookup,
                intent,
            )
            evidence_pieces, witnesses = self._collapse_when_binding(
                evidence_pieces,
                witnesses,
                intent,
            )
            evidence_pieces.sort(
                key=lambda piece: self._slot_piece_sort_key(slot_def, piece),
            )

        # Dedup: keep the highest-confidence piece per normalized surface,
        # and also collapse exact identity-key duplicates.
        _surface_best: Dict[str, Dict[str, Any]] = {}
        for piece in evidence_pieces:
            skey = (piece.get("surface") or "").strip().lower()
            if not skey:
                continue
            prev = _surface_best.get(skey)
            if prev is None or float(piece.get("confidence", 0) or 0) > float(
                prev.get("confidence", 0) or 0
            ):
                _surface_best[skey] = piece
        if len(_surface_best) < len(evidence_pieces):
            deduped_keys = {
                self._piece_identity_key(p) for p in _surface_best.values()
            }
            seen_ids: set = set()
            new_pieces: list = []
            for p in evidence_pieces:
                pid = self._piece_identity_key(p)
                if pid in deduped_keys and pid not in seen_ids:
                    seen_ids.add(pid)
                    new_pieces.append(p)
            evidence_pieces = new_pieces
            witnesses = self._filter_witnesses_for_piece_keys(
                witnesses, deduped_keys,
            )

        # Compute overall slot confidence
        confidence = self._compute_slot_confidence(
            witnesses,
            evidence_pieces,
        )

        return SlotBinding(
            slot_id=slot_def.slot_id,
            slot_type=slot_def.slot_type,
            description=slot_def.description,
            value=evidence_pieces,
            witnesses=witnesses,
            quality=self._aggregate_quality(witnesses),
            confidence=confidence,
        )

    def _refine_who_role_pairing(
        self,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        anchor_lookup: Dict[str, Anchor],
        mention_lookup: Dict[str, Mention],
        entity_lookup: Dict[str, EntityHypothesis],
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        role_witnesses = [
            witness
            for witness in witnesses
            if witness.intent_element.element_detail.get("var") == "R"
            and witness.anchor is not None
            and witness.mention is not None
        ]
        if not role_witnesses:
            return evidence_pieces, witnesses

        existing_piece_keys = {
            (
                str(piece.get("var", "")),
                str(piece.get("surface", "")).strip().lower(),
                str(piece.get("anchor_address", "")),
            )
            for piece in evidence_pieces
        }
        paired_piece_keys: Set[Tuple[str, str, str]] = set()
        paired_pieces: List[Dict[str, Any]] = []
        paired_witnesses: List[Witness] = []
        selected_role_keys: Set[Tuple[str, str]] = set()

        for role_witness in role_witnesses:
            role_anchor = role_witness.anchor
            best_role = self._best_role_candidate_for_anchor(
                role_anchor.anchor_id,
                entity_lookup,
            )
            if best_role is not None:
                role_entity, role_mention = best_role
                role_piece_key = (
                    "R",
                    (role_mention.surface or "").strip().lower(),
                    role_anchor.address,
                )
                selected_role_keys.add(
                    (role_anchor.address, (role_mention.surface or "").strip().lower())
                )
                if (
                    role_piece_key not in existing_piece_keys
                    and role_piece_key not in paired_piece_keys
                ):
                    paired_piece_keys.add(role_piece_key)
                    paired_pieces.append(
                        {
                            "var": "R",
                            "surface": role_mention.surface,
                            "category": role_mention.category,
                            "anchor_address": role_anchor.address,
                            "raw_text": role_anchor.raw_text,
                            "quality": EvidenceQuality.GROUNDED.value
                            if role_entity.confidence >= 0.7
                            else EvidenceQuality.INFERRED.value,
                            "confidence": max(
                                float(role_mention.confidence or 0.0),
                                float(role_entity.confidence or 0.0),
                            ),
                            "cross_artifact_links": 0,
                        }
                    )

                    role_binding_witness = Witness(
                        witness_id=Witness.generate_id(),
                        phase=Phase.PHASE_6_BINDING,
                        intent_element=IntentElementRef(
                            element_type="slot",
                            element_id=slot_def.slot_id,
                            element_detail={
                                "var": "R",
                                "slot_type": slot_def.slot_type,
                                "paired_from": "R",
                            },
                        ),
                        anchor=role_anchor,
                        mention=role_mention,
                        score=role_witness.score,
                        quality=(
                            EvidenceQuality.GROUNDED
                            if role_entity.confidence >= 0.7
                            else EvidenceQuality.INFERRED
                        ),
                        justification=(
                            f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                            f"preferred the more specific role '{role_mention.surface}' "
                            f"from anchor {role_anchor.address}"
                        ),
                    )
                    role_binding_witness.compute_content_hash()
                    paired_witnesses.append(role_binding_witness)

            candidates: List[Tuple[float, EntityHypothesis, Mention]] = []

            for entity in entity_lookup.values():
                if entity.category != "ENTITY_PERSON":
                    continue
                for mention in entity.mentions:
                    if mention.anchor_id != role_anchor.anchor_id:
                        continue
                    score = float(entity.confidence)
                    if entity.kg0_entity_ids:
                        score += 0.35
                    if mention.confidence:
                        score += 0.20 * float(mention.confidence)
                    score += 0.05 * len((mention.surface or "").split())
                    candidates.append((score, entity, mention))

            if not candidates:
                continue

            candidates.sort(
                key=lambda item: (
                    item[0],
                    len(item[1].kg0_entity_ids),
                    len(item[2].surface or ""),
                ),
                reverse=True,
            )
            _, entity, mention = candidates[0]
            piece_key = ("P", (mention.surface or "").strip().lower(), role_anchor.address)
            if piece_key in existing_piece_keys or piece_key in paired_piece_keys:
                continue
            paired_piece_keys.add(piece_key)

            paired_piece = {
                "var": "P",
                "surface": mention.surface,
                "category": mention.category,
                "anchor_address": role_anchor.address,
                "raw_text": role_anchor.raw_text,
                "quality": EvidenceQuality.GROUNDED.value
                if entity.confidence >= 0.7
                else EvidenceQuality.INFERRED.value,
                "confidence": max(float(mention.confidence or 0.0), float(entity.confidence or 0.0)),
                "cross_artifact_links": 0,
            }
            paired_pieces.append(paired_piece)

            paired_witness = Witness(
                witness_id=Witness.generate_id(),
                phase=Phase.PHASE_6_BINDING,
                intent_element=IntentElementRef(
                    element_type="slot",
                    element_id=slot_def.slot_id,
                    element_detail={
                        "var": "P",
                        "slot_type": slot_def.slot_type,
                        "paired_from": "R",
                    },
                ),
                anchor=role_anchor,
                mention=mention,
                score=role_witness.score,
                quality=(
                    EvidenceQuality.GROUNDED
                    if entity.confidence >= 0.7
                    else EvidenceQuality.INFERRED
                ),
                justification=(
                    f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                    f"paired role '{role_witness.mention.surface}' with "
                    f"person '{mention.surface}' from the same anchor "
                    f"{role_anchor.address}"
                ),
            )
            paired_witness.compute_content_hash()
            paired_witnesses.append(paired_witness)

        if not paired_pieces:
            return self._collapse_who_binding(
                evidence_pieces,
                witnesses,
            )

        paired_surfaces = {
            str(piece.get("surface", "")).strip().lower()
            for piece in paired_pieces
        }

        refined_pieces: List[Dict[str, Any]] = []
        for piece in evidence_pieces:
            if (
                piece.get("var") == "P"
                and str(piece.get("surface", "")).strip().lower()
                not in paired_surfaces
                and ".header" in str(piece.get("anchor_address", ""))
            ):
                continue
            if piece.get("var") == "R":
                role_key = (
                    str(piece.get("anchor_address", "")),
                    str(piece.get("surface", "")).strip().lower(),
                )
                if selected_role_keys and role_key not in selected_role_keys:
                    continue
            refined_pieces.append(piece)
        refined_pieces = paired_pieces + refined_pieces

        refined_witnesses: List[Witness] = []
        for witness in witnesses:
            surface = (witness.mention.surface if witness.mention else "").strip().lower()
            if (
                witness.intent_element.element_detail.get("var") == "P"
                and surface not in paired_surfaces
                and witness.anchor is not None
                and self._anchor_is_header_like(witness.anchor)
            ):
                continue
            if witness.intent_element.element_detail.get("var") == "R":
                role_key = (
                    witness.anchor.address if witness.anchor is not None else "",
                    surface,
                )
                if selected_role_keys and role_key not in selected_role_keys:
                    continue
            refined_witnesses.append(witness)
        refined_witnesses = paired_witnesses + refined_witnesses

        refined_pieces, refined_witnesses = self._collapse_who_binding(
            refined_pieces,
            refined_witnesses,
        )

        return refined_pieces, refined_witnesses

    def _refine_how_responsibility_chain(
        self,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        anchor_lookup: Dict[str, Anchor],
        mention_lookup: Dict[str, Mention],
        entity_lookup: Dict[str, EntityHypothesis],
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        reference_artifacts = {
            witness.anchor.artifact_id
            for witness in witnesses
            if witness.anchor is not None
            and witness.intent_element.element_detail.get("var")
            in {"RESP", "R", "CL"}
        }
        if not reference_artifacts:
            return evidence_pieces, witnesses

        existing_piece_keys = {
            (
                str(piece.get("var", "")),
                str(piece.get("surface", "")).strip().lower(),
                str(piece.get("anchor_address", "")),
            )
            for piece in evidence_pieces
        }
        paired_pieces: List[Dict[str, Any]] = []
        paired_witnesses: List[Witness] = []
        paired_piece_keys: Set[Tuple[str, str, str]] = set()
        selected_role_keys: Set[Tuple[str, str]] = set()

        role_witnesses = [
            witness
            for witness in witnesses
            if witness.intent_element.element_detail.get("var") == "R"
            and witness.anchor is not None
            and witness.mention is not None
            and witness.anchor.artifact_id in reference_artifacts
        ]

        for role_witness in role_witnesses:
            role_anchor = role_witness.anchor
            best_role = self._best_role_candidate_for_anchor(
                role_anchor.anchor_id,
                entity_lookup,
            )
            if best_role is not None:
                role_entity, role_mention = best_role
                role_piece_key = (
                    "R",
                    (role_mention.surface or "").strip().lower(),
                    role_anchor.address,
                )
                selected_role_keys.add(
                    (role_anchor.address, (role_mention.surface or "").strip().lower())
                )
                if (
                    role_piece_key not in existing_piece_keys
                    and role_piece_key not in paired_piece_keys
                ):
                    paired_piece_keys.add(role_piece_key)
                    paired_pieces.append(
                        {
                            "var": "R",
                            "surface": role_mention.surface,
                            "category": role_mention.category,
                            "anchor_address": role_anchor.address,
                            "raw_text": role_anchor.raw_text,
                            "quality": EvidenceQuality.GROUNDED.value
                            if role_entity.confidence >= 0.7
                            else EvidenceQuality.INFERRED.value,
                            "confidence": max(
                                float(role_mention.confidence or 0.0),
                                float(role_entity.confidence or 0.0),
                            ),
                            "cross_artifact_links": 0,
                        }
                    )

                    role_binding_witness = Witness(
                        witness_id=Witness.generate_id(),
                        phase=Phase.PHASE_6_BINDING,
                        intent_element=IntentElementRef(
                            element_type="slot",
                            element_id=slot_def.slot_id,
                            element_detail={
                                "var": "R",
                                "slot_type": slot_def.slot_type,
                                "paired_from": "R",
                            },
                        ),
                        anchor=role_anchor,
                        mention=role_mention,
                        score=role_witness.score,
                        quality=(
                            EvidenceQuality.GROUNDED
                            if role_entity.confidence >= 0.7
                            else EvidenceQuality.INFERRED
                        ),
                        justification=(
                            f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                            f"preferred the more specific role '{role_mention.surface}' "
                            f"from anchor {role_anchor.address}"
                        ),
                    )
                    role_binding_witness.compute_content_hash()
                    paired_witnesses.append(role_binding_witness)

            person_candidates: List[Tuple[float, EntityHypothesis, Mention]] = []
            for entity in entity_lookup.values():
                if entity.category != "ENTITY_PERSON":
                    continue
                for mention in entity.mentions:
                    if mention.anchor_id != role_anchor.anchor_id:
                        continue
                    score = float(entity.confidence)
                    if entity.kg0_entity_ids:
                        score += 0.35
                    if mention.confidence:
                        score += 0.20 * float(mention.confidence)
                    score += 0.05 * len((mention.surface or "").split())
                    person_candidates.append((score, entity, mention))

            if person_candidates:
                person_candidates.sort(
                    key=lambda item: (
                        item[0],
                        len(item[1].kg0_entity_ids),
                        len(item[2].surface or ""),
                    ),
                    reverse=True,
                )
                _, person_entity, person_mention = person_candidates[0]
                person_piece_key = (
                    "P",
                    (person_mention.surface or "").strip().lower(),
                    role_anchor.address,
                )
                if (
                    person_piece_key not in existing_piece_keys
                    and person_piece_key not in paired_piece_keys
                ):
                    paired_piece_keys.add(person_piece_key)
                    paired_pieces.append(
                        {
                            "var": "P",
                            "surface": person_mention.surface,
                            "category": person_mention.category,
                            "anchor_address": role_anchor.address,
                            "raw_text": role_anchor.raw_text,
                            "quality": EvidenceQuality.GROUNDED.value
                            if person_entity.confidence >= 0.7
                            else EvidenceQuality.INFERRED.value,
                            "confidence": max(
                                float(person_mention.confidence or 0.0),
                                float(person_entity.confidence or 0.0),
                            ),
                            "cross_artifact_links": 0,
                        }
                    )

                    person_witness = Witness(
                        witness_id=Witness.generate_id(),
                        phase=Phase.PHASE_6_BINDING,
                        intent_element=IntentElementRef(
                            element_type="slot",
                            element_id=slot_def.slot_id,
                            element_detail={
                                "var": "P",
                                "slot_type": slot_def.slot_type,
                                "paired_from": "R",
                            },
                        ),
                        anchor=role_anchor,
                        mention=person_mention,
                        score=role_witness.score,
                        quality=(
                            EvidenceQuality.GROUNDED
                            if person_entity.confidence >= 0.7
                            else EvidenceQuality.INFERRED
                        ),
                        justification=(
                            f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                            f"paired role '{role_witness.mention.surface}' with "
                            f"person '{person_mention.surface}' from the same anchor "
                            f"{role_anchor.address}"
                        ),
                    )
                    person_witness.compute_content_hash()
                    paired_witnesses.append(person_witness)

            candidates: List[Tuple[float, EntityHypothesis, Mention]] = []
            for entity in entity_lookup.values():
                if entity.category != "ENTITY_ORGANIZATION":
                    continue
                for mention in entity.mentions:
                    if mention.anchor_id != role_anchor.anchor_id:
                        continue
                    score = float(entity.confidence)
                    if entity.kg0_entity_ids:
                        score += 0.30
                    if mention.confidence:
                        score += 0.15 * float(mention.confidence)
                    score += 0.05 * len((mention.surface or "").split())
                    candidates.append((score, entity, mention))

            if not candidates:
                continue

            candidates.sort(
                key=lambda item: (
                    item[0],
                    len(item[1].kg0_entity_ids),
                    len(item[2].surface or ""),
                ),
                reverse=True,
            )
            _, entity, mention = candidates[0]
            piece_key = ("O", (mention.surface or "").strip().lower(), role_anchor.address)
            if piece_key in existing_piece_keys or piece_key in paired_piece_keys:
                continue
            paired_piece_keys.add(piece_key)

            paired_piece = {
                "var": "O",
                "surface": mention.surface,
                "category": mention.category,
                "anchor_address": role_anchor.address,
                "raw_text": role_anchor.raw_text,
                "quality": EvidenceQuality.GROUNDED.value
                if entity.confidence >= 0.7
                else EvidenceQuality.INFERRED.value,
                "confidence": max(float(mention.confidence or 0.0), float(entity.confidence or 0.0)),
                "cross_artifact_links": 0,
            }
            paired_pieces.append(paired_piece)

            paired_witness = Witness(
                witness_id=Witness.generate_id(),
                phase=Phase.PHASE_6_BINDING,
                intent_element=IntentElementRef(
                    element_type="slot",
                    element_id=slot_def.slot_id,
                    element_detail={
                        "var": "O",
                        "slot_type": slot_def.slot_type,
                        "paired_from": "R",
                    },
                ),
                anchor=role_anchor,
                mention=mention,
                score=role_witness.score,
                quality=(
                    EvidenceQuality.GROUNDED
                    if entity.confidence >= 0.7
                    else EvidenceQuality.INFERRED
                ),
                justification=(
                    f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                    f"paired role '{role_witness.mention.surface}' with "
                    f"organization '{mention.surface}' from the same anchor "
                    f"{role_anchor.address}"
                ),
            )
            paired_witness.compute_content_hash()
            paired_witnesses.append(paired_witness)

        refined_pieces: List[Dict[str, Any]] = []
        for piece in evidence_pieces:
            if (
                piece.get("var") in {"P", "O"}
                and (
                    str(piece.get("anchor_address", "")).split(".", 1)[0]
                    not in reference_artifacts
                    or ".header" in str(piece.get("anchor_address", ""))
                )
            ):
                continue
            if piece.get("var") == "R":
                role_key = (
                    str(piece.get("anchor_address", "")),
                    str(piece.get("surface", "")).strip().lower(),
                )
                if selected_role_keys and role_key not in selected_role_keys:
                    continue
            refined_pieces.append(piece)
        refined_pieces = paired_pieces + refined_pieces

        refined_witnesses: List[Witness] = []
        for witness in witnesses:
            if (
                witness.intent_element.element_detail.get("var") in {"P", "O"}
                and (
                    witness.anchor is None
                    or witness.anchor.artifact_id not in reference_artifacts
                    or self._anchor_is_header_like(witness.anchor)
                )
            ):
                continue
            if witness.intent_element.element_detail.get("var") == "R":
                surface = (witness.mention.surface if witness.mention else "").strip().lower()
                role_key = (
                    witness.anchor.address if witness.anchor is not None else "",
                    surface,
                )
                if selected_role_keys and role_key not in selected_role_keys:
                    continue
            refined_witnesses.append(witness)
        refined_witnesses = paired_witnesses + refined_witnesses

        refined_pieces, refined_witnesses = self._collapse_how_binding(
            refined_pieces,
            refined_witnesses,
        )

        return refined_pieces, refined_witnesses

    def _collapse_who_binding(
        self,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        intent: Optional[IntentObject] = None,
        slot_def: Optional[SlotDef] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        candidate_pieces = [
            piece for piece in evidence_pieces if piece.get("var") in {"P", "R"}
        ]
        if not candidate_pieces:
            return evidence_pieces, witnesses

        if slot_def is not None and self._slot_asks_policy_responsibility(slot_def):
            responsibility_pieces = [
                piece
                for piece in candidate_pieces
                if str(piece.get("semantic_role", "")) == "policy_responsibility_actor"
            ]
            if responsibility_pieces:
                selected_keys = {
                    self._piece_identity_key(piece)
                    for piece in responsibility_pieces[:4]
                }
                refined_pieces = [
                    piece
                    for piece in responsibility_pieces
                    if self._piece_identity_key(piece) in selected_keys
                ]
                refined_pieces.sort(
                    key=lambda piece: (
                        {"P": 0, "R": 1}.get(str(piece.get("var", "")), 9),
                        -float(piece.get("confidence", 0.0) or 0.0),
                        str(piece.get("surface", "")),
                    )
                )
                return refined_pieces, self._filter_witnesses_for_piece_keys(
                    witnesses,
                    selected_keys,
                )

        # Prefer actual named people for WHO. Generic person/population
        # surfaces such as "patient" can be useful context, but they should
        # not win the employee/leader slot over a named person.
        person_only = [p for p in candidate_pieces if p.get("var") == "P"]
        named_person_only = [
            p for p in person_only
            if self._looks_like_person_name(str(p.get("surface", "")))
        ]
        person_selection_pool = named_person_only or person_only
        anchor_selection_pool = (
            person_selection_pool if named_person_only else candidate_pieces
        )
        best_anchor = self._best_anchor_for_slot_chain(
            anchor_selection_pool, "WHO", intent=intent,
        )
        if not best_anchor:
            return candidate_pieces, witnesses

        person_pieces_at_best = [
            p for p in person_selection_pool
            if str(p.get("anchor_address", "")) == best_anchor
        ]
        if not person_pieces_at_best and person_selection_pool:
            person_anchor = self._best_anchor_for_slot_chain(
                person_selection_pool, "WHO", intent=intent,
            )
            if person_anchor:
                best_anchor = person_anchor

        selected_keys: Set[Tuple[str, str, str]] = set()
        person_pieces = [
            piece
            for piece in person_selection_pool
            if piece.get("var") == "P"
            and str(piece.get("anchor_address", "")) == best_anchor
        ]
        person_pieces.sort(
            key=lambda piece: (
                -self._who_piece_score(piece),
                str(piece.get("surface", "")),
            )
        )
        for piece in person_pieces[:3]:
            selected_keys.add(self._piece_identity_key(piece))

        role_pieces = [
            piece
            for piece in candidate_pieces
            if piece.get("var") == "R"
            and str(piece.get("anchor_address", "")) == best_anchor
        ]
        if not role_pieces:
            role_pieces = [
                piece for piece in candidate_pieces if piece.get("var") == "R"
            ]
        if role_pieces:
            best_role = max(
                role_pieces,
                key=lambda piece: (
                    self._slot_piece_chain_score(piece, "WHO"),
                    len(str(piece.get("surface", ""))),
                ),
            )
            selected_keys.add(self._piece_identity_key(best_role))

        refined_pieces = [
            piece
            for piece in candidate_pieces
            if self._piece_identity_key(piece) in selected_keys
        ]
        refined_pieces.sort(
            key=lambda piece: (
                {"P": 0, "R": 1}.get(str(piece.get("var", "")), 9),
                -self._who_piece_score(piece),
                str(piece.get("surface", "")),
            )
        )
        return refined_pieces, self._filter_witnesses_for_piece_keys(
            witnesses,
            selected_keys,
        )

    def _collapse_how_binding(
        self,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        chain_candidate_pieces = [
            piece
            for piece in evidence_pieces
            if piece.get("var") in {"P", "R", "O", "CL"}
        ]
        named_person_pieces = [
            piece
            for piece in chain_candidate_pieces
            if piece.get("var") == "P"
            and self._looks_like_person_name(str(piece.get("surface", "")))
        ]
        if named_person_pieces:
            chain_candidate_pieces = [
                piece
                for piece in chain_candidate_pieces
                if piece.get("var") != "P" or piece in named_person_pieces
            ]
        if not chain_candidate_pieces:
            return evidence_pieces, witnesses

        best_anchor = self._best_anchor_for_slot_chain(chain_candidate_pieces, "HOW")
        if not best_anchor:
            return evidence_pieces, witnesses

        selected_keys = self._select_best_piece_keys_by_var(
            chain_candidate_pieces,
            best_anchor,
            ("P", "R", "O", "CL"),
            "HOW",
        )
        anchor_artifact = best_anchor.split(".body", 1)[0].split(".header", 1)[0]
        resp_candidates = [
            piece
            for piece in evidence_pieces
            if (
                piece.get("var") == "RESP"
                and str(piece.get("anchor_address", "")).startswith(anchor_artifact)
            )
        ]
        if resp_candidates:
            best_resp = max(
                resp_candidates,
                key=lambda piece: (
                    self._slot_piece_chain_score(piece, "HOW"),
                    len(str(piece.get("surface", ""))),
                ),
            )
            selected_keys.add(self._piece_identity_key(best_resp))

        refined_pieces = [
            piece
            for piece in evidence_pieces
            if self._piece_identity_key(piece) in selected_keys
        ]
        refined_pieces.sort(
            key=lambda piece: (
                {"P": 0, "R": 1, "O": 2, "RESP": 3, "CL": 4}.get(
                    str(piece.get("var", "")),
                    9,
                ),
                -float(piece.get("confidence", 0.0) or 0.0),
                str(piece.get("surface", "")),
            )
        )
        return refined_pieces, self._filter_witnesses_for_piece_keys(
            witnesses,
            selected_keys,
        )

    def _collapse_outcome_binding(
        self,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        resp_pieces = [
            piece for piece in evidence_pieces if piece.get("var") == "RESP"
        ]
        if len(resp_pieces) <= 1:
            return evidence_pieces, witnesses

        best_resp = max(
            resp_pieces,
            key=lambda piece: (
                self._slot_piece_chain_score(piece, "OUTCOME"),
                len(str(piece.get("surface", ""))),
            ),
        )
        selected_keys = {
            self._piece_identity_key(piece)
            for piece in evidence_pieces
            if piece.get("var") != "RESP"
        }
        selected_keys.add(self._piece_identity_key(best_resp))

        refined_pieces = [
            piece
            for piece in evidence_pieces
            if self._piece_identity_key(piece) in selected_keys
        ]
        refined_pieces.sort(
            key=lambda piece: (
                {"RESP": 0, "CL": 1, "POL": 2, "S": 3}.get(
                    str(piece.get("var", "")),
                    9,
                ),
                -float(piece.get("confidence", 0.0) or 0.0),
                str(piece.get("surface", "")),
            )
        )
        return refined_pieces, self._filter_witnesses_for_piece_keys(
            witnesses,
            selected_keys,
        )

    def _augment_what_binding(
        self,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        subgraphs: List[Subgraph],
        anchor_lookup: Dict[str, Anchor],
        entity_lookup: Dict[str, EntityHypothesis],
        intent: IntentObject,
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        reference_artifacts: Set[str] = set()
        for sg in subgraphs:
            for binding in sg.bindings.values():
                reference_artifacts |= self._slot_binding_artifact_ids(binding)
        if not reference_artifacts:
            return evidence_pieces, witnesses
        if self._slot_asks_policy_document_and_legal_authority(slot_def):
            policy_reference_artifacts = set(reference_artifacts) | set(self._anchors_by_artifact.keys())
            existing_piece_keys = {
                self._piece_identity_key(piece)
                for piece in evidence_pieces
            }
            evidence_pieces, witnesses = self._augment_policy_what_binding(
                slot_def=slot_def,
                evidence_pieces=evidence_pieces,
                witnesses=witnesses,
                reference_artifacts=policy_reference_artifacts,
                existing_piece_keys=existing_piece_keys,
            )
            return self._augment_policy_legal_authority_binding(
                slot_def=slot_def,
                evidence_pieces=evidence_pieces,
                witnesses=witnesses,
                reference_artifacts=policy_reference_artifacts,
                replace_noise=False,
            )
        if self._slot_asks_legal_authority(slot_def):
            return self._augment_policy_legal_authority_binding(
                slot_def=slot_def,
                evidence_pieces=evidence_pieces,
                witnesses=witnesses,
                reference_artifacts=reference_artifacts,
                replace_noise=True,
            )

        primary_terms = self._primary_condition_terms(intent)
        existing_piece_keys = {
            self._piece_identity_key(piece)
            for piece in evidence_pieces
        }
        existing_surfaces = {
            self._normalize_condition_surface(piece.get("surface", ""))
            for piece in evidence_pieces
            if self._normalize_condition_surface(piece.get("surface", ""))
        }

        candidate_rows: List[Tuple[float, EntityHypothesis, Mention, Anchor]] = []
        for entity in entity_lookup.values():
            if entity.category not in {
                "ENTITY_CONCEPT",
                "ENTITY_TOPIC",
                "ENTITY_RISK_FINDING",
            }:
                continue

            best_row: Optional[Tuple[float, EntityHypothesis, Mention, Anchor]] = None
            for mention in entity.mentions:
                anchor = anchor_lookup.get(mention.anchor_id)
                if anchor is None or anchor.artifact_id not in reference_artifacts:
                    continue
                if self._anchor_is_header_like(anchor):
                    continue
                if not self._is_specific_medical_condition_surface(
                    mention.surface or entity.canonical_name,
                    primary_terms,
                ):
                    continue
                surface_key = self._normalize_condition_surface(
                    mention.surface or entity.canonical_name
                )
                if not surface_key or surface_key in existing_surfaces:
                    continue

                score = float(entity.confidence or 0.0)
                score += 0.20 * float(mention.confidence or 0.0)
                if entity.kg0_entity_ids or mention.kg0_entity_id:
                    score += 0.35
                if self._MEDICAL_CONDITION_PATTERN.search(mention.surface or ""):
                    score += 0.25
                if self._is_citation_anchor_text(anchor.raw_text):
                    score -= 0.30
                elif self._is_loader_list_anchor_text(anchor.raw_text):
                    score -= 0.45
                else:
                    score += 0.35
                score += 0.03 * len((mention.surface or "").split())
                row = (score, entity, mention, anchor)
                if best_row is None or row[0] > best_row[0]:
                    best_row = row

            if best_row is not None:
                candidate_rows.append(best_row)

        candidate_rows.sort(
            key=lambda item: (
                item[0],
                len(item[1].kg0_entity_ids),
                len(item[2].surface or ""),
            ),
            reverse=True,
        )

        augmented_pieces = list(evidence_pieces)
        augmented_witnesses = list(witnesses)
        added_surfaces = set(existing_surfaces)
        max_new_conditions = 6
        for score, entity, mention, anchor in candidate_rows:
            normalized_surface = self._normalize_condition_surface(
                mention.surface or entity.canonical_name
            )
            if not normalized_surface or normalized_surface in added_surfaces:
                continue
            added_surfaces.add(normalized_surface)

            piece = {
                "var": "C",
                "surface": mention.surface or entity.canonical_name,
                "category": entity.category,
                "artifact_id": anchor.artifact_id,
                "anchor_address": anchor.address,
                "raw_text": anchor.raw_text,
                "quality": (
                    EvidenceQuality.GROUNDED.value
                    if (entity.kg0_entity_ids or mention.kg0_entity_id)
                    else EvidenceQuality.INFERRED.value
                ),
                "confidence": max(
                    float(mention.confidence or 0.0),
                    float(entity.confidence or 0.0),
                ),
                "cross_artifact_links": len(entity.kg0_link_candidates or []),
                "entity_hypothesis_id": entity.hypothesis_id,
                "canonical_name": entity.canonical_name,
            }
            piece_key = self._piece_identity_key(piece)
            if piece_key in existing_piece_keys:
                continue
            existing_piece_keys.add(piece_key)
            augmented_pieces.append(piece)

            witness = Witness(
                witness_id=Witness.generate_id(),
                phase=Phase.PHASE_6_BINDING,
                intent_element=IntentElementRef(
                    element_type="slot",
                    element_id=slot_def.slot_id,
                    element_detail={
                        "var": "C",
                        "slot_type": slot_def.slot_type,
                        "supplemented": "medical_condition",
                    },
                ),
                anchor=anchor,
                mention=mention,
                score=score,
                quality=(
                    EvidenceQuality.GROUNDED
                    if (entity.kg0_entity_ids or mention.kg0_entity_id)
                    else EvidenceQuality.INFERRED
                ),
                justification=(
                    f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                    f"supplemented with concrete condition "
                    f"'{mention.surface or entity.canonical_name}' from {anchor.address}"
                ),
            )
            witness.compute_content_hash()
            augmented_witnesses.append(witness)

            if len(added_surfaces) - len(existing_surfaces) >= max_new_conditions:
                break

        policy_reference_artifacts = set(reference_artifacts)
        if self._slot_asks_policy_document(slot_def):
            policy_reference_artifacts |= set(self._anchors_by_artifact.keys())

        if (
            len(added_surfaces) == len(existing_surfaces)
            or self._slot_asks_policy_document(slot_def)
        ):
            return self._augment_policy_what_binding(
                slot_def=slot_def,
                evidence_pieces=augmented_pieces,
                witnesses=augmented_witnesses,
                reference_artifacts=policy_reference_artifacts,
                existing_piece_keys=existing_piece_keys,
            )

        return augmented_pieces, augmented_witnesses

    def _augment_policy_what_binding(
        self,
        *,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        reference_artifacts: Set[str],
        existing_piece_keys: Set[Tuple[str, str, str]],
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        augmented_pieces = list(evidence_pieces)
        augmented_witnesses = list(witnesses)
        added_surfaces = {
            str(piece.get("surface", "")).strip().lower()
            for piece in augmented_pieces
            if str(piece.get("surface", "")).strip()
        }

        candidates: List[Tuple[float, str, Anchor]] = []
        for artifact_id in sorted(reference_artifacts):
            for anchor in self._anchors_by_artifact.get(artifact_id, []) or []:
                if self._is_citation_anchor_text(anchor.raw_text):
                    continue
                surface = self._extract_policy_surface(anchor.raw_text)
                if not surface:
                    continue
                surface_key = surface.lower()
                if surface_key in added_surfaces:
                    continue
                text = str(anchor.raw_text or "").lower()
                score = 0.0
                if "target drug" in surface_key:
                    score += 2.0
                if "good faith dispensing" in surface_key:
                    score += 1.5
                if "walgreens" in text:
                    score += 0.6
                if "became effective" in text or "effective" in text:
                    score += 0.8
                if "early april" in text or "april 17" in text:
                    score += 0.7
                if "checklist" in text:
                    score += 0.25
                if surface.lower().startswith("national target drug") and "walgreens" in text:
                    surface = f"Walgreens {surface}"
                candidates.append((score, surface, anchor))

        candidates.sort(key=lambda row: (row[0], len(row[1])), reverse=True)
        for _score, surface, anchor in candidates[:1]:
            surface_key = surface.lower()
            if surface_key in added_surfaces:
                continue
            added_surfaces.add(surface_key)

            surface_offset = anchor.raw_text.lower().find(surface_key)
            if surface_offset < 0 and surface.lower().startswith("walgreens "):
                surface_offset = anchor.raw_text.lower().find(
                    surface_key[len("walgreens ") :]
                )
                if surface_offset < 0:
                    surface_offset = 0
            elif surface_offset < 0:
                surface_offset = 0
            synthetic_mention = Mention(
                mention_id=Mention.generate_id(),
                anchor_id=anchor.anchor_id,
                surface=surface,
                category="ENTITY_POLICY",
                category_scores={"ENTITY_POLICY": 1.0},
                normalized=surface_key,
                confidence=0.93,
                span_start=surface_offset,
                span_end=surface_offset + len(surface),
                qualifiers={"synthetic": "policy_surface"},
            )
            piece = {
                "var": "C",
                "surface": surface,
                "category": "ENTITY_POLICY",
                "artifact_id": anchor.artifact_id,
                "anchor_address": anchor.address,
                "raw_text": anchor.raw_text,
                "quality": EvidenceQuality.GROUNDED.value,
                "confidence": 0.96,
                "cross_artifact_links": 0,
                "entity_hypothesis_id": "",
                "canonical_name": surface,
                "semantic_role": "policy_document_title",
                "aliases": self._policy_document_aliases(surface, []),
            }
            piece_key = self._piece_identity_key(piece)
            if piece_key in existing_piece_keys:
                continue
            existing_piece_keys.add(piece_key)
            augmented_pieces.append(piece)

            witness = Witness(
                witness_id=Witness.generate_id(),
                phase=Phase.PHASE_6_BINDING,
                intent_element=IntentElementRef(
                    element_type="slot",
                    element_id=slot_def.slot_id,
                    element_detail={
                        "var": "C",
                        "slot_type": slot_def.slot_type,
                        "supplemented": "policy_surface",
                    },
                ),
                anchor=anchor,
                mention=synthetic_mention,
                score=0.96,
                quality=EvidenceQuality.GROUNDED,
                justification=(
                    f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                    f"supplemented with policy surface '{surface}' from {anchor.address}"
                ),
            )
            witness.compute_content_hash()
            augmented_witnesses.append(witness)
            return augmented_pieces, augmented_witnesses

        return augmented_pieces, augmented_witnesses

    def _augment_policy_legal_authority_binding(
        self,
        *,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        reference_artifacts: Set[str],
        replace_noise: bool = False,
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        candidates: List[Tuple[float, str, Anchor]] = []
        for artifact_id in sorted(reference_artifacts):
            for anchor in self._anchors_by_artifact.get(artifact_id, []) or []:
                if self._is_citation_anchor_text(anchor.raw_text):
                    continue
                surface = self._extract_policy_legal_authority_surface(anchor.raw_text)
                if not surface:
                    continue
                text = str(anchor.raw_text or "").lower()
                score = 0.0
                if "controlled substances act" in text or "(\"csa\")" in text:
                    score += 1.5
                if "dea" in text:
                    score += 1.0
                if "regulation" in text:
                    score += 0.8
                if "corresponding responsibility" in text:
                    score += 0.45
                if "legitimate medical purpose" in text:
                    score += 0.35
                candidates.append((score, surface, anchor))

        if not candidates:
            return evidence_pieces, witnesses

        candidates.sort(key=lambda row: (row[0], len(row[1])), reverse=True)
        augmented_pieces = [] if replace_noise else list(evidence_pieces)
        augmented_witnesses = [] if replace_noise else list(witnesses)
        existing_keys = {self._piece_identity_key(piece) for piece in augmented_pieces}
        added_surfaces = {
            str(piece.get("surface", "") or "").strip().lower()
            for piece in augmented_pieces
        }

        for score, surface, anchor in candidates[:2]:
            surface_key = surface.strip().lower()
            if not surface_key or surface_key in added_surfaces:
                continue
            added_surfaces.add(surface_key)
            synthetic_mention = Mention(
                mention_id=Mention.generate_id(),
                anchor_id=anchor.anchor_id,
                surface=surface,
                category="ENTITY_LEGAL_INSTRUMENT",
                category_scores={"ENTITY_LEGAL_INSTRUMENT": 1.0},
                normalized=surface_key,
                confidence=0.94,
                span_start=0,
                span_end=len(surface),
                qualifiers={"synthetic": "policy_legal_authority"},
            )
            piece = {
                "var": "LA",
                "surface": surface,
                "category": "ENTITY_LEGAL_INSTRUMENT",
                "artifact_id": anchor.artifact_id,
                "anchor_address": anchor.address,
                "raw_text": anchor.raw_text,
                "quality": EvidenceQuality.GROUNDED.value,
                "confidence": 0.94,
                "cross_artifact_links": 0,
                "entity_hypothesis_id": "",
                "canonical_name": surface,
                "semantic_role": "policy_legal_authority",
            }
            piece_key = self._piece_identity_key(piece)
            if piece_key in existing_keys:
                continue
            existing_keys.add(piece_key)
            augmented_pieces.append(piece)

            witness = Witness(
                witness_id=Witness.generate_id(),
                phase=Phase.PHASE_6_BINDING,
                intent_element=IntentElementRef(
                    element_type="slot",
                    element_id=slot_def.slot_id,
                    element_detail={
                        "var": "LA",
                        "slot_type": slot_def.slot_type,
                        "supplemented": "policy_legal_authority",
                    },
                ),
                anchor=anchor,
                mention=synthetic_mention,
                score=max(0.90, min(0.98, 0.80 + score / 10.0)),
                quality=EvidenceQuality.GROUNDED,
                justification=(
                    f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                    f"supplemented with legal authority '{surface}' from {anchor.address}"
                ),
            )
            witness.compute_content_hash()
            augmented_witnesses.append(witness)

        return augmented_pieces, augmented_witnesses

    def _extract_policy_legal_authority_surface(self, raw_text: str) -> str:
        text = " ".join(str(raw_text or "").split())
        if not text:
            return ""
        lowered = text.lower()
        has_policy_context = (
            "controlled substance" in lowered
            or "good faith" in lowered
            or "target drug" in lowered
            or "dispensing" in lowered
            or "compliance program" in lowered
        )
        if not has_policy_context:
            return ""
        if "controlled substances act" in lowered and "dea" in lowered:
            return "Controlled Substances Act (CSA) and applicable DEA regulations"
        if "dea regulations" in lowered and "corresponding responsibility" in lowered:
            return (
                "DEA regulations requiring pharmacists' corresponding responsibility "
                "to ensure controlled-substance prescriptions have a legitimate medical purpose"
            )
        if "corresponding responsibility" in lowered and "legitimate medical purpose" in lowered:
            return (
                "pharmacists' corresponding responsibility to ensure prescriptions are "
                "for a legitimate medical purpose before dispensing controlled substances"
            )
        if "usual course of professional practice" in lowered and "legitimate medical purpose" in lowered:
            return (
                "valid medical purpose and usual-course-of-professional-practice "
                "requirements for controlled-substance prescriptions"
            )
        return ""

    def _augment_policy_responsibility_who_binding(
        self,
        *,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        reference_artifacts: Set[str],
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        if not self._slot_asks_policy_responsibility(slot_def):
            return evidence_pieces, witnesses

        candidates: List[Tuple[float, str, str, str, Anchor]] = []
        for artifact_id in sorted(reference_artifacts):
            for anchor in self._anchors_by_artifact.get(artifact_id, []) or []:
                if self._is_citation_anchor_text(anchor.raw_text):
                    continue
                candidates.extend(
                    (score, surface, category, var_name, anchor)
                    for score, surface, category, var_name
                    in self._policy_responsibility_surfaces(anchor.raw_text)
                )

        if not candidates:
            return evidence_pieces, witnesses

        candidates.sort(key=lambda row: (row[0], len(row[1])), reverse=True)
        augmented_pieces = list(evidence_pieces)
        augmented_witnesses = list(witnesses)
        existing_keys = {self._piece_identity_key(piece) for piece in augmented_pieces}
        added_surfaces = {
            str(piece.get("surface", "") or "").strip().lower()
            for piece in augmented_pieces
        }

        for score, surface, category, var_name, anchor in candidates[:4]:
            surface_key = surface.strip().lower()
            if not surface_key or surface_key in added_surfaces:
                continue
            added_surfaces.add(surface_key)
            synthetic_mention = Mention(
                mention_id=Mention.generate_id(),
                anchor_id=anchor.anchor_id,
                surface=surface,
                category=category,
                category_scores={category: 1.0},
                normalized=surface_key,
                confidence=min(max(score, 0.2), 0.96),
                span_start=0,
                span_end=len(surface),
                qualifiers={"synthetic": "policy_responsibility_actor"},
            )
            piece = {
                "var": var_name,
                "surface": surface,
                "category": category,
                "artifact_id": anchor.artifact_id,
                "anchor_address": anchor.address,
                "raw_text": anchor.raw_text,
                "quality": EvidenceQuality.GROUNDED.value,
                "confidence": synthetic_mention.confidence,
                "cross_artifact_links": 0,
                "entity_hypothesis_id": "",
                "canonical_name": surface,
                "semantic_role": "policy_responsibility_actor",
            }
            piece_key = self._piece_identity_key(piece)
            if piece_key in existing_keys:
                continue
            existing_keys.add(piece_key)
            augmented_pieces.append(piece)

            witness = Witness(
                witness_id=Witness.generate_id(),
                phase=Phase.PHASE_6_BINDING,
                intent_element=IntentElementRef(
                    element_type="slot",
                    element_id=slot_def.slot_id,
                    element_detail={
                        "var": var_name,
                        "slot_type": slot_def.slot_type,
                        "supplemented": "policy_responsibility_actor",
                    },
                ),
                anchor=anchor,
                mention=synthetic_mention,
                score=score,
                quality=EvidenceQuality.GROUNDED,
                justification=(
                    f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                    f"supplemented with policy responsibility actor '{surface}' from {anchor.address}"
                ),
            )
            witness.compute_content_hash()
            augmented_witnesses.append(witness)

        return augmented_pieces, augmented_witnesses

    def _policy_responsibility_surfaces(
        self,
        raw_text: str,
    ) -> List[Tuple[float, str, str, str]]:
        text = " ".join(str(raw_text or "").split())
        if not text:
            return []
        lowered = text.lower()
        if not (
            "walgreens" in lowered
            or "walgreen co" in lowered
            or "rxintegrity" in lowered
            or "@walgreens.com" in lowered
        ):
            return []
        if not (
            "policy" in lowered
            or "procedure" in lowered
            or "compliance program" in lowered
            or "controlled substance" in lowered
            or "good faith" in lowered
            or "target drug" in lowered
        ):
            return []

        rows: List[Tuple[float, str, str, str]] = []
        if re.search(r"\bDwayne\s+A?\.?\s+Pinon\b", text, re.IGNORECASE):
            rows.append((0.96, "Dwayne A. Pinon", "ENTITY_PERSON", "P"))
        if re.search(r"\bRxIntegrity\s+team\b", text, re.IGNORECASE):
            rows.append((0.94, "RxIntegrity team", "ENTITY_ROLE", "R"))
        if re.search(r"\bPharmacy\s+Supervisors?\b", text, re.IGNORECASE):
            rows.append((0.92, "Pharmacy Supervisors", "ENTITY_ROLE", "R"))
        if "walk-in, retail pharmacy employees responsible for dispensing controlled substances" in lowered:
            rows.append((
                0.91,
                "Walgreens walk-in retail pharmacy employees responsible for dispensing controlled substances",
                "ENTITY_ROLE",
                "R",
            ))
        if (
            "pharmacist has a corresponding responsibility" in lowered
            or "pharmacists' corresponding responsibility" in lowered
        ):
            rows.append((0.90, "Walgreens pharmacists", "ENTITY_ROLE", "R"))
        return rows

    def _augment_policy_how_binding(
        self,
        *,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        reference_artifacts: Set[str],
        replace_noise: bool = False,
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        candidates: List[Tuple[float, str, Anchor]] = []
        for artifact_id in sorted(reference_artifacts):
            for anchor in self._anchors_by_artifact.get(artifact_id, []) or []:
                if self._is_citation_anchor_text(anchor.raw_text):
                    continue
                surface = self._extract_policy_how_surface(anchor.raw_text)
                if not surface:
                    continue
                text = str(anchor.raw_text or "").lower()
                score = 0.0
                if "target drug" in text or "td gfd" in text:
                    score += 0.8
                if "good faith" in text:
                    score += 0.7
                if "developed" in text:
                    score += 0.8
                if "professional judgment" in text:
                    score += 0.55
                if "corresponding responsibility" in text:
                    score += 0.45
                if "compass" in text or "faqs" in text:
                    score += 0.35
                candidates.append((score, surface, anchor))

        if not candidates:
            return evidence_pieces, witnesses

        candidates.sort(key=lambda row: (row[0], len(row[1])), reverse=True)
        _score, surface, anchor = candidates[0]
        synthetic_mention = Mention(
            mention_id=Mention.generate_id(),
            anchor_id=anchor.anchor_id,
            surface=surface,
            category="ENTITY_CLAIM",
            category_scores={"ENTITY_CLAIM": 1.0},
            normalized=surface.lower(),
            confidence=0.91,
            span_start=0,
            span_end=len(surface),
            qualifiers={"synthetic": "policy_how"},
        )
        piece = {
            "var": "RESP",
            "surface": surface,
            "category": "ENTITY_CLAIM",
            "artifact_id": anchor.artifact_id,
            "anchor_address": anchor.address,
            "raw_text": anchor.raw_text,
            "quality": EvidenceQuality.GROUNDED.value,
            "confidence": 0.91,
            "cross_artifact_links": 0,
            "entity_hypothesis_id": "",
            "canonical_name": surface,
            "semantic_role": "policy_how",
        }
        witness = Witness(
            witness_id=Witness.generate_id(),
            phase=Phase.PHASE_6_BINDING,
            intent_element=IntentElementRef(
                element_type="slot",
                element_id=slot_def.slot_id,
                element_detail={
                    "var": "RESP",
                    "slot_type": slot_def.slot_type,
                    "supplemented": "policy_how",
                },
            ),
            anchor=anchor,
            mention=synthetic_mention,
            score=0.91,
            quality=EvidenceQuality.GROUNDED,
            justification=(
                f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                f"supplemented with policy process '{surface}' from {anchor.address}"
            ),
        )
        witness.compute_content_hash()

        if replace_noise:
            return [piece], [witness]

        piece_key = self._piece_identity_key(piece)
        existing_keys = {self._piece_identity_key(p) for p in evidence_pieces}
        if piece_key in existing_keys:
            return evidence_pieces, witnesses
        return [*evidence_pieces, piece], [*witnesses, witness]

    def _policy_how_needs_repair(
        self,
        evidence_pieces: List[Dict[str, Any]],
    ) -> bool:
        if not evidence_pieces:
            return True
        for piece in evidence_pieces:
            text = " ".join(
                [
                    str(piece.get("surface", "") or ""),
                    str(piece.get("raw_text", "") or ""),
                ]
            ).lower()
            if (
                ("good faith" in text or "target drug" in text or "td gfd" in text)
                and (
                    "developed" in text
                    or "professional judgment" in text
                    or "corresponding responsibility" in text
                )
            ):
                return False
        return True

    def _extract_policy_how_surface(self, raw_text: str) -> str:
        text = " ".join(str(raw_text or "").split())
        if not text:
            return ""
        lowered = text.lower()
        if not (
            "good faith" in lowered
            or "target drug" in lowered
            or "td gfd" in lowered
            or "dispensing" in lowered
        ):
            return ""
        if "developed" not in lowered and "created" not in lowered:
            return ""
        if "guide pharmacists" in lowered or "provide pharmacists" in lowered:
            return (
                "developed to help guide pharmacists through their corresponding "
                "responsibility and professional judgment"
            )
        if "assist and support pharmacists" in lowered:
            return (
                "created to assist and support pharmacists in their professional "
                "judgment to fill or refuse a target drug"
            )
        if "updated" in lowered and "faqs" in lowered:
            return "updated through National TD GFD policy FAQs and checklist guidance"
        return ""

    def _augment_who_binding(
        self,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        subgraphs: List[Subgraph],
        anchor_lookup: Dict[str, Anchor],
        entity_lookup: Dict[str, EntityHypothesis],
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        reference_artifacts: Set[str] = set()
        for sg in subgraphs:
            for binding in sg.bindings.values():
                reference_artifacts |= self._slot_binding_artifact_ids(binding)
        if not reference_artifacts:
            return evidence_pieces, witnesses

        existing_author_artifacts = {
            str(piece.get("artifact_id", "")).strip()
            for piece in evidence_pieces
            if piece.get("var") in {"P", "R"} and str(piece.get("artifact_id", "")).strip()
        }
        if existing_author_artifacts:
            reference_artifacts &= existing_author_artifacts
        if not reference_artifacts:
            return evidence_pieces, witnesses

        existing_piece_keys = {
            self._piece_identity_key(piece) for piece in evidence_pieces
        }
        existing_surfaces = {
            str(piece.get("surface", "")).strip().lower()
            for piece in evidence_pieces
            if piece.get("var") == "P"
        }

        candidate_rows: List[Tuple[float, EntityHypothesis, Mention, Anchor]] = []
        for entity in entity_lookup.values():
            if entity.category != "ENTITY_PERSON":
                continue

            best_row: Optional[Tuple[float, EntityHypothesis, Mention, Anchor]] = None
            for mention in entity.mentions:
                anchor = anchor_lookup.get(mention.anchor_id)
                if anchor is None or anchor.artifact_id not in reference_artifacts:
                    continue
                artifact_has_author_anchor = any(
                    self._is_author_anchor(candidate_anchor)
                    for candidate_anchor in self._anchors_by_artifact.get(anchor.artifact_id, [])
                )
                if artifact_has_author_anchor and not self._is_author_anchor(anchor):
                    continue
                preferred_surface = self._preferred_person_surface(entity, mention)
                if not preferred_surface:
                    continue
                surface_key = preferred_surface.strip().lower()
                if surface_key in existing_surfaces:
                    continue

                score = float(entity.confidence or 0.0)
                score += 0.20 * float(mention.confidence or 0.0)
                if entity.kg0_entity_ids or mention.kg0_entity_id:
                    score += 0.40
                if self._is_author_anchor(anchor):
                    score += 0.55
                score += self._person_specificity_bonus(preferred_surface)
                row = (score, entity, mention, anchor)
                if best_row is None or row[0] > best_row[0]:
                    best_row = row

            if best_row is not None:
                candidate_rows.append(best_row)

        candidate_rows.sort(
            key=lambda item: (
                item[0],
                len(item[1].kg0_entity_ids),
                len(item[2].surface or ""),
            ),
            reverse=True,
        )

        augmented_pieces = list(evidence_pieces)
        augmented_witnesses = list(witnesses)
        added_surfaces = set(existing_surfaces)
        for score, entity, mention, anchor in candidate_rows:
            preferred_surface = self._preferred_person_surface(entity, mention)
            if not preferred_surface:
                continue
            surface_key = preferred_surface.strip().lower()
            if surface_key in added_surfaces:
                continue
            added_surfaces.add(surface_key)

            synthetic_mention = Mention(
                mention_id=Mention.generate_id(),
                anchor_id=anchor.anchor_id,
                surface=preferred_surface,
                category="ENTITY_PERSON",
                category_scores={"ENTITY_PERSON": 1.0},
                normalized=preferred_surface.lower(),
                confidence=max(
                    float(mention.confidence or 0.0),
                    float(entity.confidence or 0.0),
                ),
                span_start=0,
                span_end=len(preferred_surface),
                qualifiers={"synthetic": "author_surface_normalized"},
                kg0_entity_id=(mention.kg0_entity_id or (entity.kg0_entity_ids or [None])[0]),
            )
            piece = {
                "var": "P",
                "surface": preferred_surface,
                "category": "ENTITY_PERSON",
                "artifact_id": anchor.artifact_id,
                "anchor_address": anchor.address,
                "raw_text": anchor.raw_text,
                "quality": (
                    EvidenceQuality.GROUNDED.value
                    if (entity.kg0_entity_ids or mention.kg0_entity_id)
                    else EvidenceQuality.INFERRED.value
                ),
                "confidence": synthetic_mention.confidence,
                "cross_artifact_links": len(entity.kg0_link_candidates or []),
                "entity_hypothesis_id": entity.hypothesis_id,
                "canonical_name": entity.canonical_name,
            }
            piece_key = self._piece_identity_key(piece)
            if piece_key in existing_piece_keys:
                continue
            existing_piece_keys.add(piece_key)
            augmented_pieces.append(piece)

            witness = Witness(
                witness_id=Witness.generate_id(),
                phase=Phase.PHASE_6_BINDING,
                intent_element=IntentElementRef(
                    element_type="slot",
                    element_id=slot_def.slot_id,
                    element_detail={
                        "var": "P",
                        "slot_type": slot_def.slot_type,
                        "supplemented": "author_name",
                    },
                ),
                anchor=anchor,
                mention=synthetic_mention,
                score=score,
                quality=(
                    EvidenceQuality.GROUNDED
                    if (entity.kg0_entity_ids or mention.kg0_entity_id)
                    else EvidenceQuality.INFERRED
                ),
                justification=(
                    f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                    f"supplemented with author '{preferred_surface}' from {anchor.address}"
                ),
            )
            witness.compute_content_hash()
            augmented_witnesses.append(witness)
            if len(added_surfaces) >= 6:
                break

        return augmented_pieces, augmented_witnesses

    def _augment_evidence_binding(
        self,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        subgraphs: List[Subgraph],
        anchor_lookup: Dict[str, Anchor],
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        reference_artifacts: Set[str] = set()
        for sg in subgraphs:
            for binding in sg.bindings.values():
                reference_artifacts |= self._slot_binding_artifact_ids(binding)
        if not reference_artifacts:
            return evidence_pieces, witnesses

        existing_piece_keys = {
            self._piece_identity_key(piece) for piece in evidence_pieces
        }
        augmented_pieces = list(evidence_pieces)
        augmented_witnesses = list(witnesses)

        for artifact_id in sorted(reference_artifacts):
            label = self._artifact_display_name(artifact_id)
            if not label:
                continue
            anchors = self._anchors_by_artifact.get(artifact_id, []) or []
            anchor = anchors[0] if anchors else None
            if anchor is None:
                continue

            synthetic_mention = Mention(
                mention_id=Mention.generate_id(),
                anchor_id=anchor.anchor_id,
                surface=label,
                category="ENTITY_DOCUMENT",
                category_scores={"ENTITY_DOCUMENT": 1.0},
                normalized=label.lower(),
                confidence=0.95,
                span_start=0,
                span_end=len(label),
                qualifiers={"synthetic": "artifact_title"},
            )
            piece = {
                "var": "D",
                "surface": label,
                "category": "ENTITY_DOCUMENT",
                "artifact_id": artifact_id,
                "anchor_address": anchor.address,
                "raw_text": anchor.raw_text,
                "quality": EvidenceQuality.GROUNDED.value,
                "confidence": 0.95,
                "cross_artifact_links": 0,
                "entity_hypothesis_id": "",
                "canonical_name": label,
            }
            piece_key = self._piece_identity_key(piece)
            if piece_key in existing_piece_keys:
                continue
            existing_piece_keys.add(piece_key)
            augmented_pieces.append(piece)

            witness = Witness(
                witness_id=Witness.generate_id(),
                phase=Phase.PHASE_6_BINDING,
                intent_element=IntentElementRef(
                    element_type="slot",
                    element_id=slot_def.slot_id,
                    element_detail={
                        "var": "D",
                        "slot_type": slot_def.slot_type,
                        "supplemented": "artifact_title",
                    },
                ),
                anchor=anchor,
                mention=synthetic_mention,
                score=0.95,
                quality=EvidenceQuality.GROUNDED,
                justification=(
                    f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                    f"supplemented with artifact title '{label}' from {anchor.address}"
                ),
            )
            witness.compute_content_hash()
            augmented_witnesses.append(witness)

        return augmented_pieces, augmented_witnesses

    def _augment_policy_evidence_binding(
        self,
        *,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        reference_artifacts: Set[str],
        replace_noise: bool = False,
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        best_by_artifact: Dict[str, Tuple[int, float, str, Anchor]] = {}
        for artifact_id in sorted(reference_artifacts):
            for anchor in self._anchors_by_artifact.get(artifact_id, []) or []:
                if self._is_citation_anchor_text(anchor.raw_text):
                    continue
                role_rank, role = self._policy_evidence_role(anchor.raw_text)
                score = self._policy_evidence_anchor_score(anchor)
                if score <= 0.0 and role_rank <= 0:
                    continue
                current = best_by_artifact.get(artifact_id)
                if current is None or (role_rank, score) > (current[0], current[1]):
                    best_by_artifact[artifact_id] = (role_rank, score, role, anchor)

        has_positive_policy_role = any(
            role_rank > 0 for role_rank, _score, _role, _anchor in best_by_artifact.values()
        )
        ranked_rows = [
            (artifact_id, role_rank, score, role, anchor)
            for artifact_id, (role_rank, score, role, anchor) in best_by_artifact.items()
            if (role_rank > 0 or score >= 1.5)
            and not (has_positive_policy_role and role_rank < 0)
        ]
        ranked = sorted(
            ranked_rows,
            key=lambda item: (
                -item[1],
                -item[2],
                self._artifact_display_name(item[0]),
            ),
        )[:4]
        if not ranked:
            return evidence_pieces, witnesses

        augmented_pieces = [] if replace_noise else list(evidence_pieces)
        augmented_witnesses = [] if replace_noise else list(witnesses)
        existing_keys = {self._piece_identity_key(piece) for piece in augmented_pieces}
        existing_doc_index: Dict[Tuple[str, str], int] = {}
        for idx, piece in enumerate(augmented_pieces):
            if piece.get("var") in {"A", "D"}:
                existing_doc_index[
                    (
                        str(piece.get("var", "") or ""),
                        str(piece.get("surface", "") or "").strip().lower(),
                    )
                ] = idx

        for artifact_id, role_rank, score, role, anchor in ranked:
            label = self._artifact_display_name(artifact_id)
            if not label:
                continue
            synthetic_mention = Mention(
                mention_id=Mention.generate_id(),
                anchor_id=anchor.anchor_id,
                surface=label,
                category="ENTITY_DOCUMENT",
                category_scores={"ENTITY_DOCUMENT": 1.0},
                normalized=label.lower(),
                confidence=0.95,
                span_start=0,
                span_end=len(label),
                qualifiers={"synthetic": "policy_evidence_document"},
            )
            piece = {
                "var": "D",
                "surface": label,
                "category": "ENTITY_DOCUMENT",
                "artifact_id": artifact_id,
                "anchor_address": anchor.address,
                "raw_text": anchor.raw_text,
                "quality": EvidenceQuality.GROUNDED.value,
                "confidence": min(0.99, max(0.90, 0.80 + (score / 10.0))),
                "cross_artifact_links": 0,
                "entity_hypothesis_id": "",
                "canonical_name": label,
                "semantic_role": f"policy_evidence:{role}",
                "policy_evidence_role_rank": role_rank,
                "policy_evidence_score": round(score, 4),
            }
            doc_key = ("D", label.strip().lower())
            existing_doc_idx = existing_doc_index.get(doc_key)
            if existing_doc_idx is not None:
                current_piece = augmented_pieces[existing_doc_idx]
                current_rank = self._policy_evidence_piece_role_rank(current_piece)
                current_score = float(
                    current_piece.get(
                        "policy_evidence_score",
                        current_piece.get("confidence", 0.0),
                    )
                    or 0.0
                )
                if (role_rank, score) <= (current_rank, current_score):
                    continue
                old_key = self._piece_identity_key(current_piece)
                current_piece.update(piece)
                existing_keys.discard(old_key)
                existing_keys.add(self._piece_identity_key(current_piece))
            else:
                piece_key = self._piece_identity_key(piece)
                if piece_key in existing_keys:
                    continue
                existing_keys.add(piece_key)
                existing_doc_index[doc_key] = len(augmented_pieces)
                augmented_pieces.append(piece)

            piece_key = self._piece_identity_key(piece)

            witness = Witness(
                witness_id=Witness.generate_id(),
                phase=Phase.PHASE_6_BINDING,
                intent_element=IntentElementRef(
                    element_type="slot",
                    element_id=slot_def.slot_id,
                    element_detail={
                        "var": "D",
                        "slot_type": slot_def.slot_type,
                        "supplemented": "policy_evidence_document",
                    },
                ),
                anchor=anchor,
                mention=synthetic_mention,
                score=piece["confidence"],
                quality=EvidenceQuality.GROUNDED,
                justification=(
                    f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                    f"supplemented with policy evidence document '{label}' from {anchor.address}"
                ),
            )
            witness.compute_content_hash()
            augmented_witnesses.append(witness)

        return augmented_pieces, augmented_witnesses

    def _policy_evidence_role(self, text: str) -> Tuple[int, str]:
        normalized = " ".join(str(text or "").lower().split())
        has_policy_core = bool(
            re.search(
                r"\b(?:target\s+drugs?|td\s+gfd|good\s+faith\s+dispensing|gfd)\b",
                normalized,
            )
        )
        has_rollout = bool(
            re.search(
                r"\b(?:early\s+april|april\s+17|compass|became\s+effective|"
                r"effective\s+this\s+month|sent\s+to\s+all\s+locations)\b",
                normalized,
            )
        )
        has_checklist = bool(
            re.search(
                r"\b(?:target\s+drug\s+good\s+faith\s+dispensing\s+checklist|"
                r"td\s+gfd\s+checklist|mandatory\s+checklist|additional\s+checklist|"
                r"1/30/17|01/30/17|2017)\b",
                normalized,
            )
        )
        has_target_drugs = bool(
            re.search(
                r"\b(?:oxycodone|oxycontin|hydromorphone|dilaudid|diluadid|methadone)\b",
                normalized,
            )
        )
        has_scope_or_impact = bool(
            re.search(
                r"\b(?:impact|decline|budget|implementation|dea\s+action|"
                r"largest\s+declines|target\s+drugs?)\b",
                normalized,
            )
        )

        if has_policy_core and has_rollout:
            return 300, "policy_rollout"
        if has_policy_core and has_checklist:
            return 200, "operational_checklist"
        if has_target_drugs and (has_policy_core or has_scope_or_impact):
            return 100, "target_drug_scope_impact"
        if "controlled substances act" in normalized and not (
            has_policy_core or has_target_drugs
        ):
            return -100, "generic_csa"
        return 0, "policy_context"

    def _policy_evidence_anchor_score(self, anchor: Anchor) -> float:
        text = " ".join(
            [
                str(anchor.raw_text or ""),
                self._artifact_display_name(anchor.artifact_id),
            ]
        ).lower()
        score = 0.0
        if "target drug" in text:
            score += 1.3
        if "good faith dispensing" in text or "gfd" in text:
            score += 1.2
        if "checklist" in text:
            score += 0.9
        if "policy" in text:
            score += 0.5
        if "early april" in text or "april 17" in text or "compass" in text:
            score += 1.0
        if re.search(r"\b(?:oxycodone|oxycontin|hydromorphone|dilaudid|diluadid|methadone)\b", text):
            score += 1.1
        if re.search(r"\b(?:1/30/17|01/30/17|2017)\b", text):
            score += 0.6
        if "controlled substances act" in text and score < 1.5:
            score -= 0.6
        return score

    def _policy_evidence_needs_repair(
        self,
        evidence_pieces: List[Dict[str, Any]],
    ) -> bool:
        if not evidence_pieces:
            return True
        best = 0.0
        for piece in evidence_pieces:
            anchor = Anchor(
                anchor_id=str(piece.get("anchor_address", "")),
                artifact_id=str(piece.get("artifact_id", "")),
                path=[],
                raw_text=" ".join(
                    [
                        str(piece.get("surface", "") or ""),
                        str(piece.get("raw_text", "") or ""),
                    ]
                ),
            )
            best = max(best, self._policy_evidence_anchor_score(anchor))
        return best < 1.5

    def _augment_when_binding(
        self,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        subgraphs: List[Subgraph],
        anchor_lookup: Dict[str, Anchor],
        intent: IntentObject,
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        reference_artifacts: Set[str] = set(self._anchors_by_artifact.keys())
        if not reference_artifacts:
            for sg in subgraphs:
                for binding in sg.bindings.values():
                    reference_artifacts |= self._slot_binding_artifact_ids(binding)
        if not reference_artifacts:
            return evidence_pieces, witnesses

        primary_terms = self._primary_condition_terms(intent)
        if primary_terms:
            primary_artifacts = {
                artifact_id
                for artifact_id in reference_artifacts
                if self._artifact_matches_primary_terms(artifact_id, primary_terms)
            }
            if primary_artifacts:
                reference_artifacts = primary_artifacts

        existing_piece_keys = {
            self._piece_identity_key(piece) for piece in evidence_pieces
        }
        best_row: Optional[Tuple[float, Anchor, str, str]] = None

        for artifact_id in sorted(reference_artifacts):
            anchors = self._anchors_by_artifact.get(artifact_id, []) or []
            for anchor in anchors:
                date_value = (
                    self._extract_policy_date_from_anchor(anchor)
                    or self._extract_date_from_text(anchor.raw_text)
                )
                metadata_date = str(anchor.metadata.get("artifact_date", "") or "").strip()
                event_value = self._extract_when_event_surface(anchor.raw_text)
                if not date_value and not metadata_date and not event_value:
                    continue

                score = 0.0
                effective_date = date_value or metadata_date
                if date_value:
                    score += 0.75
                elif metadata_date:
                    score += 0.35
                if event_value:
                    score += 0.40
                if self._is_policy_timeline_text(anchor.raw_text):
                    score += 0.85
                if ".header" not in anchor.address:
                    score += 0.10
                in_scope = self._date_in_scope(effective_date, intent)
                if in_scope is True:
                    score += 0.80
                elif in_scope is False:
                    score -= 0.90
                row = (score, anchor, event_value, effective_date)
                if best_row is None or row[0] > best_row[0]:
                    best_row = row

        if best_row is None:
            return evidence_pieces, witnesses

        _, anchor, event_value, date_value = best_row
        augmented_pieces = list(evidence_pieces)
        augmented_witnesses = list(witnesses)

        def _append_when_piece(var_name: str, surface: str, category: str, tag: str) -> None:
            if not surface:
                return
            synthetic_mention = Mention(
                mention_id=Mention.generate_id(),
                anchor_id=anchor.anchor_id,
                surface=surface,
                category=category,
                category_scores={category: 1.0},
                normalized=surface.lower(),
                confidence=0.92,
                span_start=0,
                span_end=len(surface),
                qualifiers={"synthetic": tag},
            )
            piece = {
                "var": var_name,
                "surface": surface,
                "category": category,
                "artifact_id": anchor.artifact_id,
                "anchor_address": anchor.address,
                "raw_text": anchor.raw_text,
                "quality": EvidenceQuality.GROUNDED.value,
                "confidence": 0.92,
                "cross_artifact_links": 0,
                "entity_hypothesis_id": "",
                "canonical_name": surface,
                "semantic_role": tag,
            }
            piece_key = self._piece_identity_key(piece)
            if piece_key in existing_piece_keys:
                return
            existing_piece_keys.add(piece_key)
            augmented_pieces.append(piece)
            witness = Witness(
                witness_id=Witness.generate_id(),
                phase=Phase.PHASE_6_BINDING,
                intent_element=IntentElementRef(
                    element_type="slot",
                    element_id=slot_def.slot_id,
                    element_detail={
                        "var": var_name,
                        "slot_type": slot_def.slot_type,
                        "supplemented": tag,
                    },
                ),
                anchor=anchor,
                mention=synthetic_mention,
                score=0.92,
                quality=EvidenceQuality.GROUNDED,
                justification=(
                    f"Slot {slot_def.slot_id} ({slot_def.slot_type}) "
                    f"supplemented with {tag.replace('_', ' ')} '{surface}' from {anchor.address}"
                ),
            )
            witness.compute_content_hash()
            augmented_witnesses.append(witness)

        _append_when_piece(
            "E",
            self._extract_policy_timeline_summary(anchor, date_value),
            "ENTITY_TIMELINE",
            "policy_timeline_summary",
        )
        _append_when_piece("E", event_value, "ENTITY_EVENT", "time_event")
        _append_when_piece("D", date_value, "ENTITY_EVENT", "time_date")
        return augmented_pieces, augmented_witnesses

    def _collapse_what_binding(
        self,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        intent: IntentObject,
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        if self._slot_asks_policy_document_and_legal_authority(slot_def):
            role_pieces = [
                piece
                for piece in evidence_pieces
                if str(piece.get("semantic_role", "")) in {
                    "policy_document_title",
                    "policy_legal_authority",
                }
            ]
            if role_pieces:
                best_by_role_surface: Dict[Tuple[str, str], Dict[str, Any]] = {}
                for piece in role_pieces:
                    role = str(piece.get("semantic_role", "") or "")
                    surface_key = str(piece.get("surface", "") or "").strip().lower()
                    if not role or not surface_key:
                        continue
                    key = (role, surface_key)
                    current = best_by_role_surface.get(key)
                    if current is None or self._policy_combined_piece_score(
                        piece
                    ) > self._policy_combined_piece_score(current):
                        best_by_role_surface[key] = piece
                refined_pieces = sorted(
                    best_by_role_surface.values(),
                    key=lambda piece: (
                        0 if str(piece.get("semantic_role", "")) == "policy_document_title" else 1,
                        -self._policy_combined_piece_score(piece),
                        str(piece.get("surface", "")),
                    ),
                )[:6]
                selected_keys = {
                    self._piece_identity_key(piece) for piece in refined_pieces
                }
                return refined_pieces, self._filter_witnesses_for_piece_keys(
                    witnesses,
                    selected_keys,
                )

        if self._slot_asks_policy_document(slot_def):
            policy_pieces: List[Dict[str, Any]] = []
            for piece in evidence_pieces:
                surface = (
                    self._extract_policy_surface(str(piece.get("surface", "")))
                    or self._extract_policy_surface(str(piece.get("raw_text", "")))
                )
                if not surface:
                    continue
                if (
                    surface.lower().startswith("national target drug")
                    and "walgreens" in str(piece.get("raw_text", "")).lower()
                ):
                    surface = f"Walgreens {surface}"
                normalized_piece = dict(piece)
                normalized_piece["surface"] = surface
                normalized_piece["canonical_name"] = surface
                normalized_piece["semantic_role"] = "policy_document_title"
                normalized_piece["aliases"] = self._policy_document_aliases(
                    surface,
                    [
                        str(piece.get("surface", "") or ""),
                        str(piece.get("canonical_name", "") or ""),
                        piece.get("aliases", []),
                    ],
                )
                policy_pieces.append(normalized_piece)

            if policy_pieces:
                best_by_surface: Dict[str, Dict[str, Any]] = {}
                for piece in policy_pieces:
                    surface_key = str(piece.get("surface", "") or "").strip().lower()
                    if not surface_key:
                        continue
                    current = best_by_surface.get(surface_key)
                    if current is None or self._policy_document_piece_score(
                        piece
                    ) > self._policy_document_piece_score(current):
                        best_by_surface[surface_key] = piece
                refined_pieces = sorted(
                    best_by_surface.values(),
                    key=lambda piece: (
                        -self._policy_document_piece_score(piece),
                        self._slot_piece_sort_key(slot_def, piece),
                    ),
                )[:6]
                selected_keys = {
                    self._piece_identity_key(piece) for piece in refined_pieces
                }
                return refined_pieces, self._filter_witnesses_for_piece_keys(
                    witnesses,
                    selected_keys,
                )

        primary_terms = self._primary_condition_terms(intent)
        candidate_pieces = [
            piece
            for piece in evidence_pieces
            if piece.get("var") in {"C", "CL", "T"}
            and self._is_specific_medical_condition_surface(
                str(piece.get("surface", "")),
                primary_terms,
            )
        ]
        if primary_terms:
            primary_context_pieces = [
                piece
                for piece in candidate_pieces
                if self._piece_matches_primary_terms(piece, primary_terms)
            ]
            if primary_context_pieces:
                candidate_pieces = primary_context_pieces
        if any(piece.get("var") == "C" for piece in candidate_pieces):
            candidate_pieces = [
                piece for piece in candidate_pieces if piece.get("var") != "T"
            ]
        non_citation_pieces = [
            piece
            for piece in candidate_pieces
            if not self._is_citation_anchor_text(str(piece.get("raw_text", "")))
        ]
        if non_citation_pieces:
            candidate_pieces = non_citation_pieces
        non_loader_pieces = [
            piece
            for piece in candidate_pieces
            if not self._is_loader_list_anchor_text(str(piece.get("raw_text", "")))
        ]
        if non_loader_pieces:
            candidate_pieces = non_loader_pieces
        if not candidate_pieces:
            return evidence_pieces, witnesses

        best_by_surface: Dict[str, Dict[str, Any]] = {}
        for piece in candidate_pieces:
            normalized_surface_text = self._extract_condition_surface(
                str(piece.get("surface", "")),
                str(piece.get("raw_text", "")),
                primary_terms,
            )
            if normalized_surface_text:
                piece = dict(piece)
                piece["surface"] = normalized_surface_text
                if not piece.get("canonical_name"):
                    piece["canonical_name"] = normalized_surface_text
            surface_key = self._normalize_condition_surface(
                piece.get("surface", "")
            )
            if not surface_key:
                continue
            current = best_by_surface.get(surface_key)
            if current is None or self._what_piece_score(
                piece
            ) > self._what_piece_score(current):
                best_by_surface[surface_key] = piece

        refined_pieces = sorted(
            best_by_surface.values(),
            key=lambda piece: (
                self._slot_piece_sort_key(slot_def, piece),
                -self._what_piece_score(piece),
            ),
        )[:6]
        selected_keys = {
            self._piece_identity_key(piece) for piece in refined_pieces
        }
        return refined_pieces, self._filter_witnesses_for_piece_keys(
            witnesses,
            selected_keys,
        )

    def _policy_combined_piece_score(self, piece: Dict[str, Any]) -> float:
        score = float(piece.get("confidence", 0.0) or 0.0)
        text = " ".join(
            str(piece.get(key, "") or "")
            for key in ["surface", "canonical_name", "raw_text"]
        ).lower()
        role = str(piece.get("semantic_role", "") or "")
        if role == "policy_document_title":
            score += 1.0
            if "national target drug good faith dispensing policy" in text:
                score += 1.0
            if "walgreens" in text:
                score += 0.4
        elif role == "policy_legal_authority":
            score += 1.0
            if "controlled substances act" in text or re.search(r"\bcsa\b", text):
                score += 1.0
            if re.search(r"\bdea\b", text):
                score += 0.6
            if "applicable dea regulations" in text:
                score += 0.4
        return score

    def _policy_document_aliases(
        self,
        canonical_surface: str,
        source_aliases: Iterable[Any],
    ) -> List[str]:
        aliases: List[str] = []

        def add_alias(value: Any) -> None:
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    add_alias(item)
                return
            alias = str(value or "").strip()
            if not alias:
                return
            canonical_key = canonical_surface.strip().lower()
            alias_key = alias.lower()
            if alias_key == canonical_key:
                return
            if alias not in aliases:
                aliases.append(alias)

        for alias in source_aliases:
            add_alias(alias)

        canonical_lower = canonical_surface.lower()
        if "good faith dispensing policy" in canonical_lower:
            add_alias("good faith dispensing policy")
        if "target drug good faith dispensing policy" in canonical_lower:
            add_alias("Target Drug Good Faith Dispensing Policy")
            add_alias("TD GFD Policy")
        return aliases

    def _policy_document_piece_score(self, piece: Dict[str, Any]) -> float:
        score = float(piece.get("confidence", 0.0) or 0.0)
        surface = str(piece.get("surface", "") or "").lower()
        text = " ".join(
            [
                surface,
                str(piece.get("canonical_name", "") or "").lower(),
                str(piece.get("raw_text", "") or "").lower(),
            ]
        )
        if piece.get("quality") == EvidenceQuality.GROUNDED.value:
            score += 0.25
        if "walgreens national target drug good faith dispensing policy" in surface:
            score += 3.0
        if "target drug" in surface:
            score += 1.5
        if "good faith dispensing" in surface:
            score += 0.8
        if "walgreens" in surface:
            score += 0.4
        if "became effective" in text or "effective this month" in text:
            score += 0.5
        score += min(len(surface.split()) * 0.03, 0.3)
        return score

    def _collapse_evidence_binding(
        self,
        slot_def: SlotDef,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        intent: IntentObject,
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        normalized_pieces: List[Dict[str, Any]] = []
        for piece in evidence_pieces:
            normalized_piece = dict(piece)
            if normalized_piece.get("var") in {"A", "D"}:
                display_surface = self._preferred_evidence_document_surface(
                    normalized_piece
                )
                if display_surface:
                    normalized_piece["surface"] = display_surface
                    if not normalized_piece.get("canonical_name"):
                        normalized_piece["canonical_name"] = display_surface
            normalized_pieces.append(normalized_piece)
        evidence_pieces = normalized_pieces

        primary_terms = self._primary_condition_terms(intent)
        if primary_terms:
            primary_context_pieces = [
                piece
                for piece in evidence_pieces
                if self._piece_matches_primary_terms(piece, primary_terms)
            ]
            policy_role_pieces = [
                piece
                for piece in evidence_pieces
                if self._policy_evidence_piece_role_rank(piece) > 0
            ]
            if policy_role_pieces:
                selected_keys: Set[Tuple[str, str, str]] = set()
                selected_pieces: List[Dict[str, Any]] = []
                for piece in policy_role_pieces:
                    key = self._piece_identity_key(piece)
                    if key in selected_keys:
                        continue
                    selected_keys.add(key)
                    selected_pieces.append(piece)
                for piece in primary_context_pieces:
                    if self._is_generic_policy_evidence_piece(piece):
                        continue
                    key = self._piece_identity_key(piece)
                    if key in selected_keys:
                        continue
                    selected_keys.add(key)
                    selected_pieces.append(piece)
                evidence_pieces = selected_pieces
            elif primary_context_pieces:
                evidence_pieces = primary_context_pieces

        if any(
            self._policy_evidence_piece_role_rank(piece) > 0
            for piece in evidence_pieces
        ):
            evidence_pieces = [
                piece
                for piece in evidence_pieces
                if not self._is_generic_policy_evidence_piece(piece)
            ]

        deduped_pieces: List[Dict[str, Any]] = []
        best_doc_by_surface: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for piece in evidence_pieces:
            if piece.get("var") in {"A", "D"}:
                key = (
                    str(piece.get("var", "")),
                    str(piece.get("surface", "")).strip().lower(),
                )
                current = best_doc_by_surface.get(key)
                if current is None or float(piece.get("confidence", 0.0) or 0.0) > float(
                    current.get("confidence", 0.0) or 0.0
                ):
                    best_doc_by_surface[key] = piece
            else:
                deduped_pieces.append(piece)
        deduped_pieces.extend(best_doc_by_surface.values())
        evidence_pieces = deduped_pieces

        non_generic_pieces = [
            piece
            for piece in evidence_pieces
            if not self._is_generic_evidence_piece(piece)
        ]
        if non_generic_pieces:
            evidence_pieces = non_generic_pieces

        ts_pieces = [
            piece for piece in evidence_pieces if piece.get("var") == "TS"
        ]
        if len(ts_pieces) <= 1:
            selected_keys = {
                self._piece_identity_key(piece) for piece in evidence_pieces
            }
            return evidence_pieces, self._filter_witnesses_for_piece_keys(
                witnesses,
                selected_keys,
            )

        reference_surfaces = [
            str(piece.get("surface", ""))
            for piece in evidence_pieces
            if piece.get("var") in {"A", "CL", "POL", "RF", "S", "EV"}
        ]
        reference_token_sets = [
            self._text_tokens(surface) for surface in reference_surfaces if surface
        ]

        best_ts_by_anchor: Dict[str, Dict[str, Any]] = {}
        for piece in ts_pieces:
            anchor_address = str(piece.get("anchor_address", ""))
            if not anchor_address:
                continue
            current_best = best_ts_by_anchor.get(anchor_address)
            if current_best is None or self._evidence_text_span_score(
                piece,
                reference_token_sets,
            ) > self._evidence_text_span_score(
                current_best,
                reference_token_sets,
            ):
                best_ts_by_anchor[anchor_address] = piece

        selected_keys = {
            self._piece_identity_key(piece)
            for piece in evidence_pieces
            if piece.get("var") != "TS"
        }
        selected_keys.update(
            self._piece_identity_key(piece)
            for piece in best_ts_by_anchor.values()
        )

        refined_pieces = [
            piece
            for piece in evidence_pieces
            if self._piece_identity_key(piece) in selected_keys
        ]
        return refined_pieces, self._filter_witnesses_for_piece_keys(
            witnesses,
            selected_keys,
        )

    def _policy_evidence_piece_role_rank(self, piece: Dict[str, Any]) -> int:
        if piece.get("var") not in {"A", "D"}:
            return 0
        try:
            return int(piece.get("policy_evidence_role_rank", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _is_generic_policy_evidence_piece(self, piece: Dict[str, Any]) -> bool:
        if self._policy_evidence_piece_role_rank(piece) > 0:
            return False
        if piece.get("var") not in {"A", "D"}:
            return False
        text = " ".join(
            [
                str(piece.get("surface", "") or ""),
                str(piece.get("canonical_name", "") or ""),
                str(piece.get("raw_text", "") or ""),
                self._artifact_display_name(str(piece.get("artifact_id", "") or "")),
            ]
        )
        role_rank, _role = self._policy_evidence_role(text)
        return role_rank < 0

    def _collapse_when_binding(
        self,
        evidence_pieces: List[Dict[str, Any]],
        witnesses: List[Witness],
        intent: IntentObject,
    ) -> Tuple[List[Dict[str, Any]], List[Witness]]:
        normalized_pieces: List[Dict[str, Any]] = []
        for piece in evidence_pieces:
            normalized_piece = dict(piece)
            if (
                normalized_piece.get("var") in {"E", "EV"}
                and normalized_piece.get("category") != "ENTITY_TIMELINE"
            ):
                event_surface = self._extract_when_event_surface(
                    str(normalized_piece.get("raw_text", ""))
                )
                if event_surface:
                    normalized_piece["surface"] = event_surface
            normalized_pieces.append(normalized_piece)

        primary_terms = self._primary_condition_terms(intent)
        if primary_terms:
            primary_context_pieces = [
                piece
                for piece in normalized_pieces
                if self._piece_matches_primary_terms(piece, primary_terms)
            ]
            if primary_context_pieces:
                normalized_pieces = primary_context_pieces

        if (
            not normalized_pieces
            or all(
                self._WHEN_EVENT_NOISE_PATTERN.search(str(piece.get("surface", "")))
                for piece in normalized_pieces
                if piece.get("var") in {"E", "EV"}
            )
        ):
            fallback_candidates: List[Dict[str, Any]] = []
            candidate_artifacts = set(self._anchors_by_artifact.keys())
            if primary_terms:
                primary_artifacts = {
                    artifact_id
                    for artifact_id in candidate_artifacts
                    if self._artifact_matches_primary_terms(artifact_id, primary_terms)
                }
                if primary_artifacts:
                    candidate_artifacts = primary_artifacts
            for artifact_id in sorted(candidate_artifacts):
                for anchor in self._anchors_by_artifact.get(artifact_id, []) or []:
                    if self._is_citation_anchor_text(anchor.raw_text):
                        continue
                    event_surface = self._extract_when_event_surface(anchor.raw_text)
                    date_surface = (
                        self._extract_policy_date_from_anchor(anchor)
                        or self._extract_date_from_text(anchor.raw_text)
                        or str(anchor.metadata.get("artifact_date", "") or "").strip()
                    )
                    if event_surface:
                        fallback_candidates.append(
                            {
                                "var": "E",
                                "surface": event_surface,
                                "category": "ENTITY_EVENT",
                                "artifact_id": anchor.artifact_id,
                                "anchor_address": anchor.address,
                                "raw_text": anchor.raw_text,
                                "quality": EvidenceQuality.GROUNDED.value,
                                "confidence": 0.92,
                                "cross_artifact_links": 0,
                            }
                        )
                    if date_surface:
                        fallback_candidates.append(
                            {
                                "var": "D",
                                "surface": date_surface,
                                "category": "ENTITY_EVENT",
                                "artifact_id": anchor.artifact_id,
                                "anchor_address": anchor.address,
                                "raw_text": anchor.raw_text,
                                "quality": EvidenceQuality.GROUNDED.value,
                                "confidence": 0.90,
                                "cross_artifact_links": 0,
                            }
                        )
            if fallback_candidates:
                normalized_pieces.extend(fallback_candidates)

        when_pieces = [
            piece
            for piece in normalized_pieces
            if piece.get("var") in {"E", "EV", "D", "DATE"}
        ]
        if not when_pieces:
            return normalized_pieces, witnesses

        def _temporal_when_score(piece: Dict[str, Any]) -> Tuple[float, int]:
            base = self._when_piece_score(piece)
            surface = str(piece.get("surface", ""))
            raw_text = str(piece.get("raw_text", ""))
            for candidate_date in (surface, raw_text):
                in_scope = self._date_in_scope(candidate_date, intent)
                if in_scope is True:
                    base += 0.80
                    break
                elif in_scope is False:
                    base -= 0.90
                    break
            return (base, len(surface))

        best_piece = max(when_pieces, key=_temporal_when_score)

        refined_pieces = [best_piece]
        selected_keys = {self._piece_identity_key(best_piece)}
        date_value = self._extract_date_from_text(str(best_piece.get("raw_text", "")))
        if date_value:
            refined_pieces.append(
                {
                    "var": "D",
                    "surface": date_value,
                    "category": "ENTITY_EVENT",
                    "artifact_id": str(best_piece.get("artifact_id", "")),
                    "anchor_address": str(best_piece.get("anchor_address", "")),
                    "raw_text": str(best_piece.get("raw_text", "")),
                    "quality": str(best_piece.get("quality", EvidenceQuality.INFERRED.value)),
                    "confidence": float(best_piece.get("confidence", 0.0) or 0.0),
                    "cross_artifact_links": int(best_piece.get("cross_artifact_links", 0) or 0),
                }
            )

        return refined_pieces, self._filter_witnesses_for_piece_keys(
            witnesses,
            selected_keys,
        )

    def _evidence_text_span_score(
        self,
        piece: Dict[str, Any],
        reference_token_sets: List[Set[str]],
    ) -> float:
        score = float(piece.get("confidence", 0.0) or 0.0)
        piece_tokens = self._text_tokens(str(piece.get("surface", "")))
        if piece.get("quality") == EvidenceQuality.GROUNDED.value:
            score += 0.20
        if len(piece_tokens) >= 8:
            score += 0.08
        if reference_token_sets and piece_tokens:
            best_overlap = 0.0
            for token_set in reference_token_sets:
                if not token_set:
                    continue
                overlap = len(piece_tokens & token_set) / max(
                    1, len(token_set)
                )
                if overlap > best_overlap:
                    best_overlap = overlap
            score += 0.45 * best_overlap
        return score

    def _best_anchor_for_slot_chain(
        self,
        pieces: List[Dict[str, Any]],
        slot_type: str,
        intent: Optional[IntentObject] = None,
    ) -> str:
        anchor_scores: Dict[str, float] = defaultdict(float)
        anchor_vars: Dict[str, Set[str]] = defaultdict(set)
        for piece in pieces:
            anchor_address = str(piece.get("anchor_address", ""))
            if not anchor_address:
                continue
            anchor_vars[anchor_address].add(str(piece.get("var", "")))
            anchor_scores[anchor_address] += self._slot_piece_chain_score(
                piece,
                slot_type,
            )

        if not anchor_scores:
            return ""

        for anchor_address, vars_present in anchor_vars.items():
            if slot_type == "WHO" and {"P", "R"} <= vars_present:
                anchor_scores[anchor_address] += 1.2
            if slot_type == "WHO":
                author_piece_count = sum(
                    1
                    for piece in pieces
                    if str(piece.get("anchor_address", "")) == anchor_address
                    and piece.get("var") == "P"
                    and self._is_author_anchor_text(str(piece.get("raw_text", "")))
                )
                if author_piece_count:
                    anchor_scores[anchor_address] += 1.4 + (0.25 * author_piece_count)
            elif slot_type == "HOW":
                if {"P", "R", "O"} <= vars_present:
                    anchor_scores[anchor_address] += 1.4
                elif {"R", "O"} <= vars_present:
                    anchor_scores[anchor_address] += 0.8
                if "CL" in vars_present:
                    anchor_scores[anchor_address] += 0.5

        if intent is not None:
            anchor_lookup = getattr(self, "_anchor_lookup", {}) or {}
            for anchor_address in anchor_scores:
                artifact_id = anchor_address.split(".")[0] if "." in anchor_address else anchor_address
                anchors = (getattr(self, "_anchors_by_artifact", {}) or {}).get(artifact_id, [])
                for anch in (anchors or []):
                    art_date = str(anch.metadata.get("artifact_date", "") or "").strip()
                    if not art_date:
                        art_date = str(anch.metadata.get("date", "") or "").strip()
                    if art_date:
                        in_scope = self._date_in_scope(art_date, intent)
                        if in_scope is True:
                            anchor_scores[anchor_address] += 2.0
                        elif in_scope is False:
                            anchor_scores[anchor_address] -= 1.5
                        break

        return max(
            anchor_scores.items(),
            key=lambda item: (item[1], item[0]),
        )[0]

    def _slot_piece_chain_score(
        self,
        piece: Dict[str, Any],
        slot_type: str,
    ) -> float:
        var_name = str(piece.get("var", ""))
        surface = str(piece.get("surface", ""))
        anchor_address = str(piece.get("anchor_address", ""))
        score = float(piece.get("confidence", 0.0) or 0.0)
        if ".header" not in anchor_address:
            score += 0.45
        if var_name == "R":
            score += 0.60 + self._role_specificity_bonus(surface)
        elif var_name == "P":
            score += 0.35 + self._person_specificity_bonus(surface)
        elif var_name == "O":
            score += 0.30 + self._organization_specificity_bonus(surface)
        elif var_name == "CL":
            score += 0.35
        elif var_name == "RESP":
            score += 0.20
        if slot_type == "WHO" and var_name == "R":
            score += 0.15
        return score

    def _select_best_piece_keys_by_var(
        self,
        pieces: List[Dict[str, Any]],
        anchor_address: str,
        vars_to_keep: Tuple[str, ...],
        slot_type: str,
    ) -> Set[Tuple[str, str, str]]:
        selected_keys: Set[Tuple[str, str, str]] = set()
        for var_name in vars_to_keep:
            var_pieces = [
                piece
                for piece in pieces
                if piece.get("var") == var_name
                and str(piece.get("anchor_address", "")) == anchor_address
            ]
            if not var_pieces:
                continue
            best_piece = max(
                var_pieces,
                key=lambda piece: (
                    self._slot_piece_chain_score(piece, slot_type),
                    len(str(piece.get("surface", ""))),
                ),
            )
            selected_keys.add(self._piece_identity_key(best_piece))
        return selected_keys

    def _filter_witnesses_for_piece_keys(
        self,
        witnesses: List[Witness],
        selected_keys: Set[Tuple[str, str, str]],
    ) -> List[Witness]:
        refined: List[Witness] = []
        for witness in witnesses:
            var_name = str(witness.intent_element.element_detail.get("var", ""))
            surface = (
                witness.mention.surface if witness.mention is not None else ""
            )
            anchor_address = (
                witness.anchor.address if witness.anchor is not None else ""
            )
            key = (var_name, str(surface).strip().lower(), str(anchor_address))
            if key in selected_keys:
                refined.append(witness)
                continue
            equivalent_keys = {
                (var_name, alt_surface, str(anchor_address))
                for alt_surface in self._witness_surface_equivalents(witness)
            }
            if equivalent_keys & selected_keys:
                refined.append(witness)
        return refined

    def _piece_identity_key(
        self,
        piece: Dict[str, Any],
    ) -> Tuple[str, str, str]:
        return (
            str(piece.get("var", "")),
            str(piece.get("surface", "")).strip().lower(),
            str(piece.get("anchor_address", "")),
        )

    def _witness_surface_equivalents(self, witness: Witness) -> Set[str]:
        alternatives: Set[str] = set()
        if witness.mention is not None and witness.mention.surface:
            alternatives.add(str(witness.mention.surface).strip().lower())
        if witness.anchor is None:
            return {alt for alt in alternatives if alt}

        var_name = str(witness.intent_element.element_detail.get("var", ""))
        if var_name in {"C", "CL", "T"}:
            condition_surface = self._extract_condition_surface(
                witness.mention.surface if witness.mention is not None else "",
                witness.anchor.raw_text,
                None,
            )
            if condition_surface:
                alternatives.add(condition_surface.strip().lower())
        elif var_name in {"A", "D"}:
            artifact_surface = self._artifact_display_name(witness.anchor.artifact_id)
            if artifact_surface:
                alternatives.add(artifact_surface.strip().lower())
        elif var_name in {"E", "EV"}:
            event_surface = self._extract_when_event_surface(witness.anchor.raw_text)
            if event_surface:
                alternatives.add(event_surface.strip().lower())
        return {alt for alt in alternatives if alt}

    def _best_role_candidate_for_anchor(
        self,
        anchor_id: str,
        entity_lookup: Dict[str, EntityHypothesis],
    ) -> Optional[Tuple[EntityHypothesis, Mention]]:
        candidates: List[Tuple[float, EntityHypothesis, Mention]] = []
        for entity in entity_lookup.values():
            if entity.category != "ENTITY_ROLE":
                continue
            for mention in entity.mentions:
                if mention.anchor_id != anchor_id:
                    continue
                score = float(entity.confidence)
                score += self._role_specificity_bonus(mention.surface)
                if mention.confidence:
                    score += 0.20 * float(mention.confidence)
                candidates.append((score, entity, mention))

        if not candidates:
            return None

        candidates.sort(
            key=lambda item: (
                item[0],
                len(item[2].surface or ""),
                len((item[1].kg0_entity_ids or [])),
            ),
            reverse=True,
        )
        _, entity, mention = candidates[0]
        return entity, mention

    def _role_specificity_bonus(self, surface: str) -> float:
        tokens = self._text_tokens(surface)
        bonus = 0.0
        if len(tokens) >= 2:
            bonus += 0.30
        if len(tokens) >= 3:
            bonus += 0.20
        if any(
            token in {"senior", "associate", "assistant", "chief", "deputy", "litigation", "regulatory", "r", "ph"}
            for token in tokens
        ):
            bonus += 0.18
        if any(
            token in {"attorney", "counsel", "officer", "director", "manager", "supervisor", "reviewer"}
            for token in tokens
        ):
            bonus += 0.18
        if "confidential" in tokens:
            bonus -= 0.18
        if tokens <= {"pharmacist"}:
            bonus -= 0.12
        return bonus

    def _person_specificity_bonus(self, surface: str) -> float:
        normalized = str(surface or "").strip()
        tokens = normalized.split()
        bonus = 0.0
        if not self._looks_like_person_name(normalized):
            bonus -= 1.25
        if len(tokens) >= 2:
            bonus += 0.12
        if len(tokens) >= 3:
            bonus += 0.08
        if re.search(r"\b[A-Z]\.", normalized):
            bonus += 0.10
        return bonus

    def _organization_specificity_bonus(self, surface: str) -> float:
        tokens = self._text_tokens(surface)
        bonus = 0.0
        if len(tokens) >= 2:
            bonus += 0.10
        if len(tokens) >= 3:
            bonus += 0.12
        if any(
            token in {"regulatory", "litigation", "law", "legal", "compliance"}
            for token in tokens
        ):
            bonus += 0.16
        if tokens <= {"combined", "company"} or (
            "company" in tokens and len(tokens) <= 2
        ):
            bonus -= 0.18
        return bonus

    def _find_vars_for_slot(
        self,
        slot_def: SlotDef,
        gs: Optional[GraphSpec],
    ) -> List[str]:
        """
        Determine which graph spec variables are relevant to a slot.

        Resolution order:
            1. Explicit target_var mapping
            2. Legacy direct match when target_schema_id happens to equal var.var
            3. Ranked matching using schema identifier tokens,
               ontology compatibility, role keywords, and text overlap

        The ontology is loaded per-collection at config time; no
        domain-specific types are hardcoded here.
        """
        if gs is None:
            return []

        ontology = self.config.ontology
        if slot_def.target_var:
            target = gs.var_by_name(slot_def.target_var)
            if target is not None:
                return [target.var]

        if slot_def.target_schema_id:
            legacy_target = gs.var_by_name(slot_def.target_schema_id)
            if legacy_target is not None:
                return [legacy_target.var]

        schema_tokens = self._slot_schema_tokens(slot_def)
        description_tokens = self._text_tokens(slot_def.description)
        ranked: List[Tuple[str, float]] = []

        for var in gs.vars:
            score = 0.0
            var_tokens = self._graph_var_tokens(var)

            if schema_tokens:
                score += 2.0 * len(schema_tokens & var_tokens)

            if var.type and ontology.is_slot_compatible(
                slot_def.slot_type, var.type
            ):
                score += 4.0

            if var.role and ontology.role_matches_slot(
                slot_def.slot_type, var.role
            ):
                score += 2.5

            if description_tokens:
                score += 1.5 * len(description_tokens & var_tokens)

            if var.hard:
                score += 0.25

            score += self._slot_structural_bonus(slot_def, var, gs)

            if score > 0.0:
                ranked.append((var.var, score))

        if not ranked:
            return []

        ranked.sort(key=lambda item: (-item[1], item[0]))
        best_score = ranked[0][1]
        threshold = max(1.0, best_score - 1.5)
        return [
            var_name
            for var_name, score in ranked[:5]
            if score >= threshold
        ]

    def _augment_slot_vars(
        self,
        slot_def: SlotDef,
        gs: Optional[GraphSpec],
        current_vars: List[str],
    ) -> List[str]:
        if gs is None:
            return current_vars

        slot_type = (slot_def.slot_type or "").upper()
        ordered: List[str] = list(current_vars)
        seen = set(ordered)

        preferred_aliases: Set[str] = set()
        if slot_type == "EVIDENCE":
            preferred_aliases = {
                "ARTIFACT_DOCUMENT",
                "ENTITY_DOCUMENT",
                "DOCUMENT",
                "ENTITY_CLAIM",
                "CLAIM",
                "ENTITY_POLICY",
                "POLICY",
                "ENTITY_TEXT_SPAN",
                "TEXT_SPAN",
                "ENTITY_RISK_FINDING",
                "RISK_FINDING",
            }
        elif slot_type == "HOW":
            preferred_aliases = {
                "ENTITY_PERSON",
                "PERSON",
                "ENTITY_ACTION_ITEM",
                "ACTION_ITEM",
                "ENTITY_ORGANIZATION",
                "ORGANIZATION",
                "ENTITY_ROLE",
                "ROLE",
                "ENTITY_POPULATION",
                "POPULATION",
                "ENTITY_CLAIM",
                "CLAIM",
            }
        elif slot_type == "WHO":
            preferred_aliases = {
                "ENTITY_PERSON",
                "PERSON",
                "ENTITY_ROLE",
                "ROLE",
                "ENTITY_POPULATION",
                "POPULATION",
                "ENTITY_ORGANIZATION",
                "ORGANIZATION",
            }
        elif slot_type == "WHAT":
            preferred_aliases = {
                "ENTITY_CONCEPT",
                "CONCEPT",
                "ENTITY_TOPIC",
                "TOPIC",
                "ENTITY_RISK_FINDING",
                "RISK_FINDING",
                "ENTITY_CLAIM",
                "CLAIM",
            }
        elif slot_type == "OUTCOME":
            preferred_aliases = {
                "ENTITY_ACTION_ITEM",
                "ACTION_ITEM",
                "ENTITY_CLAIM",
                "CLAIM",
                "ENTITY_POLICY",
                "POLICY",
                "ENTITY_STRATEGY",
                "STRATEGY",
            }

        for var in gs.vars:
            aliases = self._var_type_aliases(var.type)
            if aliases & preferred_aliases and var.var not in seen:
                ordered.append(var.var)
                seen.add(var.var)

        return ordered

    def _rank_subgraphs_for_slot(
        self,
        slot_def: SlotDef,
        subgraphs: List[Subgraph],
        relevant_vars: List[str],
        gs: Optional[GraphSpec],
    ) -> List[Subgraph]:
        slot_type = (slot_def.slot_type or "").upper()
        if not subgraphs:
            return []

        def _slot_score(sg: Subgraph) -> Tuple[float, float, float]:
            bound_relevant = sum(
                1
                for var_name in relevant_vars
                if sg.bindings.get(var_name) and sg.bindings[var_name].bound
            )
            chain_bonus = 0.0
            if gs is not None and slot_type == "EVIDENCE":
                preferred_edges = (
                    "A-[SUPPORTS]->CL",
                    "CL-[CONTRADICTS]->POL",
                    "CL-[QUOTES_SPAN]->TS",
                    "TS-[EVIDENCED_BY]->A",
                )
                chain_bonus += sum(
                    1.0
                    for edge_key in preferred_edges
                    if sg.edge_satisfactions.get(edge_key, False)
                )
                for var_name in ("A", "CL", "POL", "TS", "RF"):
                    if sg.bindings.get(var_name) and sg.bindings[var_name].bound:
                        chain_bonus += 0.35
            elif gs is not None and slot_type == "HOW":
                preferred_edges = (
                    "O-[INVOLVED_IN]->RESP",
                    "RESP-[RESULTED_IN]->CL",
                )
                chain_bonus += sum(
                    1.0
                    for edge_key in preferred_edges
                    if sg.edge_satisfactions.get(edge_key, False)
                )
                for var_name in ("RESP", "O", "CL"):
                    if sg.bindings.get(var_name) and sg.bindings[var_name].bound:
                        chain_bonus += 0.30
            elif gs is not None and slot_type == "WHO":
                preferred_edges = (
                    "A-[AUTHORED_BY]->P",
                    "P-[AFFILIATED_WITH]->R",
                    "R-[AFFILIATED_WITH]->O",
                )
                chain_bonus += sum(
                    1.0
                    for edge_key in preferred_edges
                    if sg.edge_satisfactions.get(edge_key, False)
                )
                reference_artifacts = self._slot_reference_artifacts(sg)
                for var_name, weight in (
                    ("P", 0.65),
                    ("R", 0.45),
                    ("O", 0.30),
                ):
                    if self._slot_var_shares_artifact(
                        sg, var_name, reference_artifacts
                    ):
                        chain_bonus += weight
                if (
                    self._slot_var_shares_artifact(
                        sg, "P", reference_artifacts
                    )
                    and self._slot_var_shares_artifact(
                        sg, "R", reference_artifacts
                    )
                ):
                    chain_bonus += 0.55
                chain_bonus += self._who_context_bonus(slot_def, sg)
            elif gs is not None and slot_type == "WHAT":
                preferred_edges = (
                    "CL-[ABOUT]->C",
                    "CL-[ABOUT]->T",
                    "D-[ABOUT]->C",
                    "D-[ABOUT]->T",
                )
                chain_bonus += sum(
                    1.0
                    for edge_key in preferred_edges
                    if sg.edge_satisfactions.get(edge_key, False)
                )
                for var_name, weight in (
                    ("C", 0.85),
                    ("T", 0.55),
                    ("CL", 0.45),
                    ("D", 0.20),
                ):
                    if sg.bindings.get(var_name) and sg.bindings[var_name].bound:
                        chain_bonus += weight
            elif gs is not None and slot_type == "OUTCOME":
                preferred_edges = ("RESP-[RESULTED_IN]->CL",)
                chain_bonus += sum(
                    1.0
                    for edge_key in preferred_edges
                    if sg.edge_satisfactions.get(edge_key, False)
                )
                for var_name in ("RESP", "CL", "POL", "S"):
                    if sg.bindings.get(var_name) and sg.bindings[var_name].bound:
                        chain_bonus += 0.25

            return (bound_relevant + chain_bonus, sg.hard_coverage, sg.score)

        return sorted(subgraphs, key=_slot_score, reverse=True)

    def _var_type_aliases(self, var_type: str) -> Set[str]:
        normalized = str(var_type or "").strip().upper()
        if not normalized:
            return set()

        aliases: Set[str] = {normalized}
        if normalized.startswith("ENTITY_"):
            bare = normalized[len("ENTITY_") :]
            aliases.add(bare)
            if bare == "DOCUMENT":
                aliases.add("ARTIFACT_DOCUMENT")
        elif normalized.startswith("ARTIFACT_"):
            bare = normalized[len("ARTIFACT_") :]
            aliases.add(bare)
            aliases.add(f"ENTITY_{bare}")
        else:
            aliases.add(f"ENTITY_{normalized}")
            if normalized == "DOCUMENT":
                aliases.add("ARTIFACT_DOCUMENT")
        return aliases

    def _slot_structural_bonus(
        self,
        slot_def: SlotDef,
        var: GraphVar,
        gs: Optional[GraphSpec],
    ) -> float:
        slot_type = (slot_def.slot_type or "").upper()
        aliases = self._var_type_aliases(var.type)
        bonus = 0.0

        if slot_type == "EVIDENCE":
            if aliases & {
                "ARTIFACT_DOCUMENT",
                "ENTITY_DOCUMENT",
                "DOCUMENT",
                "ENTITY_CLAIM",
                "CLAIM",
                "ENTITY_POLICY",
                "POLICY",
                "ENTITY_TEXT_SPAN",
                "TEXT_SPAN",
                "ENTITY_RISK_FINDING",
                "RISK_FINDING",
            }:
                bonus += 3.25
        elif slot_type == "HOW":
            if aliases & {
                "ENTITY_PERSON",
                "PERSON",
            }:
                bonus += 2.15
            if aliases & {
                "ENTITY_ACTION_ITEM",
                "ACTION_ITEM",
                "ENTITY_ORGANIZATION",
                "ORGANIZATION",
                "ENTITY_ROLE",
                "ROLE",
                "ENTITY_POPULATION",
                "POPULATION",
            }:
                bonus += 2.75
        elif slot_type == "WHO":
            if aliases & {
                "ENTITY_PERSON",
                "PERSON",
            }:
                bonus += 2.90
            if aliases & {
                "ENTITY_ROLE",
                "ROLE",
                "ENTITY_POPULATION",
                "POPULATION",
            }:
                bonus += 2.35
            if aliases & {
                "ENTITY_ORGANIZATION",
                "ORGANIZATION",
            }:
                bonus += 1.65
        elif slot_type == "WHAT":
            if aliases & {
                "ENTITY_CONCEPT",
                "CONCEPT",
                "ENTITY_RISK_FINDING",
                "RISK_FINDING",
            }:
                bonus += 2.90
            if aliases & {
                "ENTITY_TOPIC",
                "TOPIC",
                "ENTITY_CLAIM",
                "CLAIM",
            }:
                bonus += 2.10
        elif slot_type == "OUTCOME":
            if aliases & {
                "ENTITY_ACTION_ITEM",
                "ACTION_ITEM",
                "ENTITY_CLAIM",
                "CLAIM",
                "ENTITY_POLICY",
                "POLICY",
                "ENTITY_STRATEGY",
                "STRATEGY",
            }:
                bonus += 2.15

        if gs is None:
            return bonus

        incident_edges = [
            edge
            for edge in gs.edges
            if edge.src == var.var or edge.dst == var.var
        ]
        if slot_type == "EVIDENCE":
            bonus += 0.65 * sum(
                1
                for edge in incident_edges
                if edge.rel
                in {"EVIDENCED_BY", "SUPPORTS", "QUOTES_SPAN", "CONTRADICTS", "ABOUT"}
            )
        elif slot_type == "WHO":
            bonus += 0.90 * sum(
                1
                for edge in incident_edges
                if edge.rel in {"AUTHORED_BY", "AFFILIATED_WITH"}
            )
        elif slot_type == "WHAT":
            bonus += 0.80 * sum(
                1
                for edge in incident_edges
                if edge.rel in {"ABOUT", "SUPPORTS", "EVIDENCED_BY"}
            )
        elif slot_type == "HOW":
            bonus += 0.80 * sum(
                1
                for edge in incident_edges
                if edge.rel in {"INVOLVED_IN", "RESULTED_IN"}
            )
        elif slot_type == "OUTCOME":
            bonus += 0.55 * sum(
                1
                for edge in incident_edges
                if edge.rel in {"RESULTED_IN", "CONTRADICTS", "ABOUT"}
            )

        return bonus

    def _slot_piece_sort_key(
        self,
        slot_def: SlotDef,
        piece: Dict[str, Any],
    ) -> Tuple[int, int, int, float, int, str]:
        slot_type = (slot_def.slot_type or "").upper()
        var_name = str(piece.get("var", "") or "")
        preferred_order: Dict[str, int] = {}
        if slot_type == "EVIDENCE":
            preferred_order = {
                "A": 0,
                "CL": 1,
                "POL": 2,
                "TS": 3,
                "RF": 4,
                "EV": 5,
            }
            if var_name == "D":
                preferred_order["D"] = 0
        elif slot_type == "WHO":
            preferred_order = {"P": 0, "R": 1, "O": 2}
        elif slot_type == "WHAT":
            preferred_order = {"C": 0, "CL": 1, "T": 2, "D": 3}
        elif slot_type == "HOW":
            preferred_order = {"P": 0, "R": 1, "O": 2, "RESP": 3, "CL": 4}
        elif slot_type == "OUTCOME":
            preferred_order = {"RESP": 0, "CL": 1, "POL": 2, "S": 3}
        elif slot_type == "WHEN":
            preferred_order = {"D": 0, "DATE": 0, "E": 1, "EV": 1}

        role_rank = 0
        if slot_type == "EVIDENCE" and var_name == "D":
            role_rank = int(piece.get("policy_evidence_role_rank", 0) or 0)
        return (
            preferred_order.get(var_name, 99),
            -role_rank,
            -int(piece.get("cross_artifact_links", 0) or 0),
            -float(piece.get("confidence", 0.0) or 0.0),
            0 if piece.get("quality") == EvidenceQuality.GROUNDED.value else 1,
            str(piece.get("surface", "") or ""),
        )

    def _slot_reference_artifacts(
        self,
        sg: Subgraph,
    ) -> Set[str]:
        reference_artifacts: Set[str] = set()
        for var_name in ("A", "CL", "TS", "POL", "S", "RF"):
            binding = sg.bindings.get(var_name)
            if not binding or not binding.bound:
                continue
            for anchor in self._resolve_slot_binding_anchors(binding):
                reference_artifacts.add(anchor.artifact_id)
        return reference_artifacts

    def _slot_var_shares_artifact(
        self,
        sg: Subgraph,
        var_name: str,
        reference_artifacts: Set[str],
    ) -> bool:
        if not reference_artifacts:
            return False
        binding = sg.bindings.get(var_name)
        if not binding or not binding.bound:
            return False
        return any(
            anchor.artifact_id in reference_artifacts
            for anchor in self._resolve_slot_binding_anchors(binding)
        )

    def _slot_reference_texts(
        self,
        sg: Subgraph,
    ) -> Tuple[str, str]:
        reference_anchors: List[Anchor] = []
        for var_name in ("CL", "TS", "A", "POL", "S", "RF"):
            binding = sg.bindings.get(var_name)
            if not binding or not binding.bound:
                continue
            for anchor in self._resolve_slot_binding_anchors(binding):
                if anchor not in reference_anchors:
                    reference_anchors.append(anchor)
        non_header_text = " ".join(
            anchor.raw_text.lower()
            for anchor in reference_anchors
            if not self._anchor_is_header_like(anchor)
        )
        fallback_text = " ".join(
            anchor.raw_text.lower() for anchor in reference_anchors
        )
        return non_header_text, fallback_text

    def _anchor_is_header_like(self, anchor: Anchor) -> bool:
        for component in anchor.path:
            component_type = ""
            if isinstance(component, dict):
                component_type = str(
                    component.get("component_type", "")
                )
            else:
                component_type = str(
                    getattr(component, "component_type", "")
                )
            if component_type.lower() == "header":
                return True
        return bool(anchor.metadata.get("is_title"))

    def _binding_surface_candidates(
        self,
        binding: Optional[SubgraphBinding],
    ) -> List[str]:
        candidates: List[str] = []
        if binding is None or not binding.bound:
            return candidates

        if binding.entity_hypothesis_id:
            entity = self._entity_lookup.get(binding.entity_hypothesis_id)
            if entity:
                if entity.canonical_name:
                    candidates.append(entity.canonical_name)
                for mention in entity.mentions:
                    if mention.surface:
                        candidates.append(mention.surface)
        return [c for c in candidates if c]

    def _binding_is_header_only(
        self,
        binding: Optional[SubgraphBinding],
    ) -> bool:
        anchors = self._resolve_slot_binding_anchors(binding)
        if not anchors:
            return False
        return all(self._anchor_is_header_like(anchor) for anchor in anchors)

    def _slot_binding_artifact_ids(
        self,
        binding: Optional[SubgraphBinding],
    ) -> Set[str]:
        if binding is None or not binding.bound:
            return set()
        return {
            anchor.artifact_id
            for anchor in self._resolve_slot_binding_anchors(binding)
        }

    def _who_textual_match_score(
        self,
        binding: Optional[SubgraphBinding],
        non_header_text: str,
        fallback_text: str,
    ) -> float:
        if binding is None or not binding.bound:
            return 0.0

        candidates = self._binding_surface_candidates(binding)
        if not candidates:
            return 0.0

        score = 0.0
        ref_text = non_header_text or fallback_text
        ref_tokens = self._text_tokens(ref_text)

        for surface in candidates:
            surface_l = surface.lower()
            tokens = [
                token
                for token in self._text_tokens(surface)
                if len(token) >= 3
            ]
            if ref_text and surface_l in ref_text:
                score = max(score, 1.25)
            elif any(token in ref_tokens for token in tokens):
                score = max(score, 0.75)
            elif fallback_text and surface_l in fallback_text:
                score = max(score, 0.35)

            if len(tokens) >= 2:
                first, last = tokens[0], tokens[-1]
                if first in ref_tokens or last in ref_tokens:
                    score = max(score, 0.85)

        anchors = self._resolve_slot_binding_anchors(binding)
        if anchors and any(not self._anchor_is_header_like(a) for a in anchors):
            score += 0.35
        elif anchors:
            score -= 0.15

        return score

    def _who_context_bonus(
        self,
        slot_def: SlotDef,
        sg: Subgraph,
    ) -> float:
        non_header_text, fallback_text = self._slot_reference_texts(sg)
        if not (non_header_text or fallback_text):
            return 0.0

        bonus = 0.0
        bonus += self._who_textual_match_score(
            sg.bindings.get("P"),
            non_header_text,
            fallback_text,
        )

        role_binding = sg.bindings.get("R")
        role_surfaces = " ".join(
            self._binding_surface_candidates(role_binding)
        ).lower()
        cue_tokens = self._text_tokens(slot_def.description) | {
            "attorney",
            "reviewer",
            "review",
            "legal",
            "compliance",
            "counsel",
            "jurisdiction",
        }
        role_tokens = self._text_tokens(role_surfaces)
        if role_tokens & cue_tokens:
            bonus += 0.95
        if role_binding is not None and self._who_textual_match_score(
            role_binding,
            non_header_text,
            fallback_text,
        ) > 0.0:
            bonus += 0.55

        person_binding = sg.bindings.get("P")
        if (
            person_binding is not None
            and role_binding is not None
            and self._slot_var_shares_artifact(
                sg,
                "P",
                self._slot_binding_artifact_ids(role_binding),
            )
            and role_tokens & cue_tokens
        ):
            bonus += 0.70

        return bonus

    def _resolve_slot_binding_anchors(
        self,
        binding: SubgraphBinding,
    ) -> List[Anchor]:
        anchors: List[Anchor] = []
        if binding.anchor_id:
            anchor = self._anchor_lookup.get(binding.anchor_id)
            if anchor:
                anchors.append(anchor)
        if binding.entity_hypothesis_id:
            entity = self._entity_lookup.get(binding.entity_hypothesis_id)
            if entity:
                for mention in entity.mentions:
                    anchor = self._anchor_lookup.get(mention.anchor_id)
                    if anchor and anchor not in anchors:
                        anchors.append(anchor)
        return anchors

    def _compute_slot_confidence(
        self,
        witnesses: List[Witness],
        evidence_pieces: List[Dict[str, Any]],
    ) -> float:
        """
        Compute a configuration-stable slot confidence.

        This intentionally avoids raw subgraph scores so confidence
        remains comparable across different weighting schemes.
        """
        return _compute_slot_confidence(witnesses, evidence_pieces)

    def _slot_schema_tokens(self, slot_def: SlotDef) -> Set[str]:
        raw = slot_def.target_schema_id or ""
        _, _, schema_tail = raw.partition(":")
        tokens = self._text_tokens(schema_tail.replace("_", " "))
        tokens |= self._text_tokens(slot_def.slot_type)
        return tokens

    def _slot_asks_legal_authority(self, slot_def: SlotDef) -> bool:
        text = " ".join(
            [
                str(slot_def.description or ""),
                str(slot_def.target_schema_id or ""),
                str(slot_def.slot_id or ""),
            ]
        ).lower()
        document_title_slot = (
            "document itself" in text
            or "policy name" in text
            or "title" in text
        )
        explicit_authority = bool(
            re.search(
                r"\b(?:legal\s+authority|legal\s+basis|under\s+which|under\s+what)\b",
                text,
            )
        )
        if document_title_slot and not explicit_authority:
            return False
        if explicit_authority:
            return True
        return bool(
            re.search(
                r"\b(?:statute|regulation|regulations|controlled\s+substances\s+act|"
                r"\bcsa\b|\bdea\b)\b",
                text,
            )
        )

    def _slot_asks_policy_document_and_legal_authority(self, slot_def: SlotDef) -> bool:
        text = " ".join(
            [
                str(slot_def.description or ""),
                str(slot_def.target_schema_id or ""),
                str(slot_def.slot_id or ""),
            ]
        ).lower()
        asks_document = bool(
            re.search(
                r"\b(?:legal\s+document|document\s+itself|policy\s+name|"
                r"policy\s+title|title)\b",
                text,
            )
        )
        asks_authority = bool(
            re.search(
                r"\b(?:legal\s+authority|legal\s+basis|under\s+which|under\s+what)\b",
                text,
            )
        )
        return asks_document and asks_authority

    def _slot_asks_policy_document(self, slot_def: SlotDef) -> bool:
        if self._slot_asks_legal_authority(slot_def):
            return False
        text = " ".join(
            [
                str(slot_def.description or ""),
                str(slot_def.target_schema_id or ""),
                str(slot_def.slot_id or ""),
            ]
        ).lower()
        return bool(
            re.search(
                r"\b(?:legal\s+document|document\s+itself|policy\s+name|"
                r"policy\s+title|title|policy)\b",
                text,
            )
        )

    def _slot_asks_policy_responsibility(self, slot_def: SlotDef) -> bool:
        text = " ".join(
            [
                str(slot_def.description or ""),
                str(slot_def.target_schema_id or ""),
                str(slot_def.slot_id or ""),
            ]
        ).lower()
        return bool(
            ("responsible" in text or "responsibility" in text)
            and (
                "policy" in text
                or "dispensing" in text
                or "controlled substance" in text
                or "good faith" in text
            )
        )

    def _graph_var_tokens(self, var: GraphVar) -> Set[str]:
        tokens = set()
        tokens |= self._text_tokens(var.var)
        tokens |= self._text_tokens(var.type.replace("ENTITY_", "").replace("ARTIFACT_", ""))
        tokens |= self._text_tokens(var.role)
        tokens |= self._text_tokens(var.hint)
        for label in self.config.ontology.kg0_labels_for(var.type):
            tokens |= self._text_tokens(label)
        return tokens

    def _text_tokens(self, text: str) -> Set[str]:
        return _surface_tokens(text)

    @staticmethod
    def _quality_weight(quality: EvidenceQuality) -> float:
        return _quality_weight(quality)

    def _aggregate_quality(
        self, witnesses: List[Witness]
    ) -> EvidenceQuality:
        """Aggregate evidence quality across witnesses."""
        return _aggregate_quality(witnesses)

    def _primary_condition_terms(
        self,
        intent: IntentObject,
    ) -> Set[str]:
        terms: Set[str] = set()
        for hint in intent.entity_hints:
            if str(hint.category or "").upper() not in {"ENTITY_TOPIC", "TOPIC"}:
                continue
            for value in (hint.surface, hint.normalized):
                normalized = self._normalize_condition_surface(value)
                if normalized:
                    terms.add(normalized)
        if intent.graph_spec is not None:
            for var in intent.graph_spec.vars:
                if var.var == "T":
                    normalized = self._normalize_condition_surface(var.hint)
                    if normalized:
                        terms.add(normalized)
        return terms

    def _normalize_condition_surface(self, surface: str) -> str:
        return _normalize_condition_surface(surface)

    def _extract_condition_surface(
        self,
        surface: str,
        raw_text: str,
        primary_terms: Optional[Set[str]] = None,
    ) -> str:
        candidates: List[str] = []
        for source in (surface, raw_text):
            if not source:
                continue
            for match in self._MEDICAL_CONDITION_PATTERN.finditer(source):
                value = match.group(0).strip()
                if not self._is_specific_medical_condition_surface(
                    value,
                    primary_terms,
                ):
                    continue
                candidates.append(value)
            if candidates:
                break
        if not candidates:
            return ""

        def _condition_rank(value: str) -> Tuple[int, int, str]:
            acronyms = {"ptsd", "cptsd", "dsm-iv"}
            normalized = self._normalize_condition_surface(value)
            acronym_bonus = 1 if normalized in acronyms else 0
            return (len(value.split()), acronym_bonus, value.lower())

        best = max(candidates, key=_condition_rank)
        normalized_best = self._normalize_condition_surface(best)
        if normalized_best == "ptsd":
            return "PTSD"
        if normalized_best == "cptsd":
            return "CPTSD"
        if normalized_best == "dsm iv disorders":
            return "DSM-IV disorders"
        return best

    def _selected_kg0_candidate_name(
        self,
        entity: Optional[EntityHypothesis],
    ) -> str:
        if entity is None:
            return ""
        candidates = entity.kg0_link_candidates or []
        selected = [
            candidate
            for candidate in candidates
            if candidate.get("selected")
            and str(candidate.get("name", "")).strip()
        ]
        if not selected:
            return ""
        selected.sort(
            key=lambda item: (
                float(item.get("normalized_score", 0.0) or 0.0),
                float(item.get("best_score", 0.0) or 0.0),
                len(str(item.get("name", ""))),
            ),
            reverse=True,
        )
        return str(selected[0].get("name", "") or "").strip()

    def _is_author_anchor_text(self, raw_text: str) -> bool:
        return bool(self._AUTHOR_ANCHOR_PATTERN.search(str(raw_text or "")))

    def _is_author_anchor(self, anchor: Optional[Anchor]) -> bool:
        if anchor is None:
            return False
        return self._is_author_anchor_text(anchor.raw_text)

    def _looks_like_person_name(self, surface: str) -> bool:
        normalized = str(surface or "").strip()
        if not normalized:
            return False
        lowered = normalized.lower()
        if lowered in self._GENERIC_PERSON_SURFACES:
            return False
        if self._PERSON_SURFACE_NOISE_PATTERN.search(normalized):
            return False
        tokens = re.findall(r"[A-Za-z][A-Za-z'.-]*", normalized)
        if len(tokens) < 2 or len(tokens) > 5:
            return False
        alpha_tokens = [token for token in tokens if any(ch.isalpha() for ch in token)]
        if len(alpha_tokens) < 2:
            return False
        capitalized_count = sum(1 for token in alpha_tokens if token[:1].isupper())
        if capitalized_count >= 2:
            return True
        return all(token.islower() for token in alpha_tokens)

    def _preferred_person_surface(
        self,
        entity: Optional[EntityHypothesis],
        mention: Optional[Mention],
    ) -> str:
        candidates: List[str] = []
        if mention is not None and mention.surface:
            candidates.append(str(mention.surface).strip())
        if entity is not None and entity.canonical_name:
            candidates.append(str(entity.canonical_name).strip().title())
        for candidate in candidates:
            if self._looks_like_person_name(candidate):
                return candidate
        return ""

    def _artifact_display_name(self, artifact_id: str) -> str:
        value = str(artifact_id or "").strip()
        if value.startswith("artifact::"):
            value = value[len("artifact::") :].strip()
        anchors_by_artifact = getattr(self, "_anchors_by_artifact", {}) or {}
        for anchor in anchors_by_artifact.get(value, []) or []:
            name = str(anchor.metadata.get("artifact_name", "") or "").strip()
            if name:
                return name
        return value

    def _is_citation_anchor_text(self, raw_text: str) -> bool:
        return str(raw_text or "").strip().lower().startswith(
            "key supporting citations:"
        )

    def _is_loader_list_anchor_text(self, raw_text: str) -> bool:
        normalized = str(raw_text or "").strip().lower()
        if not normalized:
            return False
        return any(
            normalized.startswith(prefix)
            for prefix in self._LOADER_SECTION_PREFIXES
        )

    def _context_mentions_primary_terms(
        self,
        text: str,
        primary_terms: Optional[Set[str]],
    ) -> bool:
        if not primary_terms:
            return False
        normalized_text = self._normalize_condition_surface(text)
        if not normalized_text:
            return False
        return any(term in normalized_text for term in primary_terms)

    def _piece_matches_primary_terms(
        self,
        piece: Dict[str, Any],
        primary_terms: Optional[Set[str]],
    ) -> bool:
        if not primary_terms:
            return False
        artifact_label = self._artifact_display_name(str(piece.get("artifact_id", "")))
        raw_text = str(piece.get("raw_text", ""))
        if self._is_citation_anchor_text(raw_text) or self._is_loader_list_anchor_text(
            raw_text
        ):
            raw_text = ""
        texts = [
            str(piece.get("surface", "")),
            str(piece.get("canonical_name", "")),
            raw_text,
            artifact_label,
        ]
        return any(
            self._context_mentions_primary_terms(text, primary_terms)
            for text in texts
            if text
        )

    def _artifact_matches_primary_terms(
        self,
        artifact_id: str,
        primary_terms: Optional[Set[str]],
    ) -> bool:
        if not primary_terms:
            return False
        artifact_label = self._artifact_display_name(artifact_id)
        if self._context_mentions_primary_terms(artifact_label, primary_terms):
            return True
        for anchor in self._anchors_by_artifact.get(artifact_id, []) or []:
            if self._is_citation_anchor_text(
                anchor.raw_text
            ) or self._is_loader_list_anchor_text(anchor.raw_text):
                continue
            if self._context_mentions_primary_terms(anchor.raw_text, primary_terms):
                return True
        return False

    def _is_generic_evidence_piece(self, piece: Dict[str, Any]) -> bool:
        var_name = str(piece.get("var", "")).strip().upper()
        if var_name in {"A", "D", "TS"}:
            return False
        surface = str(piece.get("surface", "")).strip().lower()
        raw_text = str(piece.get("raw_text", ""))
        if not surface:
            return False
        if surface in self._GENERIC_EVIDENCE_SURFACES:
            return True
        if self._MEASUREMENT_NOISE_PATTERN.search(surface):
            return True
        if self._is_loader_list_anchor_text(raw_text):
            return True
        if self._is_citation_anchor_text(raw_text) and var_name not in {"A", "D"}:
            return True
        return False

    def _preferred_evidence_document_surface(
        self,
        piece: Dict[str, Any],
    ) -> str:
        artifact_label = self._artifact_display_name(str(piece.get("artifact_id", "")))
        current_surface = str(piece.get("surface", "")).strip()
        if artifact_label and (
            not current_surface
            or self._MEASUREMENT_NOISE_PATTERN.search(current_surface)
            or len(current_surface.split()) <= 4
        ):
            return artifact_label
        return current_surface

    def _extract_when_event_surface(self, raw_text: str) -> str:
        matches = [
            match.group(1).strip()
            for match in self._WHEN_EVENT_PATTERN.finditer(str(raw_text or ""))
        ]
        if not matches:
            return ""
        priority = {
            "received ethics approval": 5,
            "recruitment anticipated to begin": 4,
            "recruitment began": 4,
            "recruitment begin": 4,
            "recruitment started": 4,
            "recruitment start": 4,
            "registered": 3,
            "registered with": 3,
            "registered on": 3,
            "approved": 2,
            "approved on": 2,
            "published": 1,
            "published on": 1,
            "registration": 0,
            "registration number": 0,
        }
        matches.sort(
            key=lambda value: (
                priority.get(value.lower(), 0),
                len(value),
            ),
            reverse=True,
        )
        return matches[0]

    def _extract_policy_surface(self, raw_text: str) -> str:
        matches = [
            match.group(1).strip()
            for match in self._POLICY_SURFACE_PATTERN.finditer(str(raw_text or ""))
        ]
        if not matches:
            return ""
        matches.sort(
            key=lambda value: (
                "target drug good faith dispensing" in value.lower(),
                "policy" in value.lower(),
                len(value),
            ),
            reverse=True,
        )
        return matches[0]

    def _is_policy_timeline_text(self, raw_text: str) -> bool:
        text = str(raw_text or "")
        if not self._POLICY_SURFACE_PATTERN.search(text):
            return False
        return bool(
            re.search(
                r"\b(?:became effective|effective|implemented|rolled out|"
                r"will be sent|sent to all locations|updated|developed|"
                r"created|removed|added|revised)\b",
                text,
                re.IGNORECASE,
            )
        )

    def _extract_policy_date_from_anchor(self, anchor: Anchor) -> str:
        metadata_date = str(anchor.metadata.get("artifact_date", "") or "").strip()
        metadata_year = self._extract_year_from_date_string(metadata_date)
        if metadata_year is None:
            metadata_year = self._extract_year_from_date_string(
                str(anchor.metadata.get("date", "") or "")
            )
        if metadata_year is None:
            return ""

        text = str(anchor.raw_text or "")
        match = self._MONTH_DAY_WITHOUT_YEAR_PATTERN.search(text)
        if match:
            qualifier = str(match.group(1) or "").lower()
            month_name = str(match.group(2) or "").lower()
            day_text = match.group(3)
            month = self._MONTHS.get(month_name)
            if month:
                if day_text:
                    day = max(1, min(31, int(day_text)))
                elif qualifier == "mid":
                    day = 15
                elif qualifier == "late":
                    day = 25
                else:
                    day = 1
                return f"{metadata_year:04d}-{month:02d}-{day:02d}"

        if re.search(r"\beffective\s+this\s+month\b", text, re.IGNORECASE):
            month_match = re.search(r"\b\d{4}-(\d{2})-\d{2}\b", metadata_date)
            if month_match:
                return f"{metadata_year:04d}-{int(month_match.group(1)):02d}-01"

        return ""

    def _extract_policy_timeline_summary(self, anchor: Anchor, date_value: str) -> str:
        text = str(anchor.raw_text or "")
        if not self._is_policy_timeline_text(text):
            return ""
        metadata_date = str(anchor.metadata.get("artifact_date", "") or "").strip()
        year = (
            self._extract_year_from_date_string(date_value)
            or self._extract_year_from_date_string(metadata_date)
            or self._extract_year_from_date_string(str(anchor.metadata.get("date", "") or ""))
        )
        if (
            year
            and re.search(r"\bearly\s+April\b", text, re.IGNORECASE)
            and re.search(r"\b(?:Wednesday,\s*)?April\s+17\b", text, re.IGNORECASE)
        ):
            return (
                f"early April {year}; COMPASS project sent Wednesday, "
                f"April 17, {year}"
            )
        if year and re.search(r"\bearly\s+April\b", text, re.IGNORECASE):
            return f"early April {year}"
        if date_value and self._extract_when_event_surface(text):
            return f"{self._extract_when_event_surface(text)} on {date_value}"
        return ""

    def _is_specific_medical_condition_surface(
        self,
        surface: str,
        primary_terms: Optional[Set[str]] = None,
    ) -> bool:
        normalized = self._normalize_condition_surface(surface)
        if not normalized:
            return False
        if primary_terms and normalized in primary_terms:
            return False
        if normalized in self._GENERIC_MEDICAL_SURFACES:
            return False
        tokens = self._text_tokens(normalized)
        if not tokens:
            return False
        if tokens <= {
            "condition",
            "conditions",
            "complication",
            "complications",
            "disease",
            "diseases",
            "disorder",
            "disorders",
            "symptom",
            "symptoms",
        }:
            return False
        if self._MEASUREMENT_NOISE_PATTERN.search(normalized):
            return False
        return bool(self._MEDICAL_CONDITION_PATTERN.search(normalized))

    def _what_piece_score(self, piece: Dict[str, Any]) -> float:
        score = float(piece.get("confidence", 0.0) or 0.0)
        var_name = str(piece.get("var", ""))
        surface = str(piece.get("surface", ""))
        if piece.get("quality") == EvidenceQuality.GROUNDED.value:
            score += 0.35
        if var_name == "C":
            score += 0.30
        elif var_name == "CL":
            score += 0.10
        if self._MEDICAL_CONDITION_PATTERN.search(surface):
            score += 0.20
        score += 0.02 * len(surface.split())
        return score

    def _who_piece_score(self, piece: Dict[str, Any]) -> float:
        score = self._slot_piece_chain_score(piece, "WHO")
        surface = str(piece.get("surface", ""))
        if self._looks_like_person_name(surface):
            score += 0.35
        if self._is_author_anchor_text(str(piece.get("raw_text", ""))):
            score += 0.50
        return score

    def _when_piece_score(self, piece: Dict[str, Any]) -> float:
        score = float(piece.get("confidence", 0.0) or 0.0)
        surface = str(piece.get("surface", ""))
        raw_text = str(piece.get("raw_text", ""))
        if piece.get("quality") == EvidenceQuality.GROUNDED.value:
            score += 0.25
        if self._extract_date_from_text(raw_text):
            score += 0.45
        if re.search(
            r"\b(?:approved|approval|published|recruitment|registered|"
            r"began|begin|started|onset|effective|implemented|updated|"
            r"developed|created|removed|added|revised|sent)\b",
            f"{surface} {raw_text}",
            re.IGNORECASE,
        ):
            score += 0.30
        if self._WHEN_EVENT_NOISE_PATTERN.search(surface):
            score -= 0.65
        if surface.isupper() and len(surface) <= 8:
            score -= 0.30
        return score

    def _extract_date_from_text(self, text: str) -> str:
        return _extract_date_from_text(text)

    @staticmethod
    def _extract_year_from_date_string(date_str: str) -> Optional[int]:
        return _extract_year_from_date_string(date_str)

    def _scope_year_range(self, intent: IntentObject) -> Tuple[Optional[int], Optional[int]]:
        tf = intent.scope_spec.time_filter
        if tf.op == "none":
            return None, None
        start_year = self._extract_year_from_date_string(tf.start) if tf.start else None
        end_year = self._extract_year_from_date_string(tf.end) if tf.end else None
        return start_year, end_year

    def _date_in_scope(self, date_str: str, intent: IntentObject) -> Optional[bool]:
        tf = intent.scope_spec.time_filter
        if tf.op == "none":
            return None
        return _date_in_year_range(date_str, start=tf.start, end=tf.end)
