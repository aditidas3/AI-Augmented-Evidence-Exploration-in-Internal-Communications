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
from ..search.subgraph_reranking import (
    collect_subgraph_kg0_nodes as _collect_subgraph_kg0_nodes,
    diversity_rerank_subgraphs as _diversity_rerank_subgraphs,
)
from .phase3 import Phase3_AnchorMentionExtraction

class Phase5_SubgraphDiscovery:
    """
    Discover subgraphs matching the graph spec using beam search.

    After beam search completes, this phase:
        1. Computes a normalized coherence score per subgraph
        2. Performs MMR-style diversity reranking
        3. Assembles a frame witness for each retained subgraph
        4. Leaves KG structure materialization to the post-Phase-5 step
        5. Returns (subgraphs, stats) for the run manifest

    FIX #1:  Uses link hypotheses for cross-artifact edge satisfaction.
    FIX #3:  Evaluates temporal constraints during scoring.
    FIX #7:  Returns (subgraphs, stats) tuple instead of bare list.
    FIX #8:  Assembles frame witness and snapshot per subgraph.
    FIX #9:  Diversity reranking with MMR-style scoring.
    FIX #10: _find_var_candidates corrected into class body.
    """

    def __init__(self, config: AlignConfig, index: IndexFacade):
        self.config = config
        self.index = index
        # Transient state set during execute(), cleared after
        self._link_index: Dict[Tuple[str, str], List[LinkHypothesis]] = {}
        self._anchor_lookup: Dict[str, Anchor] = {}
        self._mention_lookup: Dict[str, Mention] = {}
        self._entity_lookup: Dict[str, EntityHypothesis] = {}

    # ----------------------------------------------------------------
    # Main entry point
    # ----------------------------------------------------------------

    def execute(
        self,
        all_anchors: Dict[str, List[Anchor]],
        all_mentions: Dict[str, List[Mention]],
        entity_hyps: List[EntityHypothesis],
        link_hyps: List[LinkHypothesis],
        skeleton: Optional[CompiledGraphSkeleton],
        intent: IntentObject,
    ) -> Tuple[List[Subgraph], Dict[str, Any]]:
        """
        Execute Phase 5: beam search, coherence scoring, diversity
        reranking, witness assembly, and snapshot generation.

        Returns
        -------
        subgraphs : list[Subgraph]
            Ranked subgraphs, each carrying .frame_witness and .snapshot
        stats : dict
            Counters for the run manifest
        """
        gs = intent.graph_spec
        stats: Dict[str, Any] = {
            "beam_states_explored": 0,
            "complete_subgraphs_found": 0,
            "subgraphs_after_diversity_rerank": 0,
            "witnesses_assembled": 0,
        }

        if gs is None or skeleton is None:
            logger.info(
                "Phase 5: No graph spec, skipping subgraph discovery"
            )
            trivial = self._create_trivial_subgraph(
                all_anchors, all_mentions
            )
            stats["complete_subgraphs_found"] = 1
            stats["subgraphs_after_diversity_rerank"] = 1
            return [trivial], stats

        logger.info(
            f"Phase 5: Discovering subgraphs for '{gs.query_name}'"
        )

        # --- Build transient indexes ---

        self._link_index = defaultdict(list)
        for lh in link_hyps:
            self._link_index[
                (lh.source_entity_id, lh.target_entity_id)
            ].append(lh)
            self._link_index[
                (lh.target_entity_id, lh.source_entity_id)
            ].append(lh)

        self._anchor_lookup = {}
        for aid, anchors in all_anchors.items():
            for anchor in anchors:
                self._anchor_lookup[anchor.anchor_id] = anchor

        self._mention_lookup = {}
        for anchor_id, mentions in all_mentions.items():
            for mention in mentions:
                self._mention_lookup[mention.mention_id] = mention

        self._entity_lookup = {
            eh.hypothesis_id: eh for eh in entity_hyps
        }

        mention_by_category: Dict[
            str, List[Tuple[Mention, Anchor]]
        ] = defaultdict(list)
        for anchor_id, mentions in all_mentions.items():
            anchor = self._anchor_lookup.get(anchor_id)
            if anchor is None:
                continue
            for mention in mentions:
                mention_by_category[mention.category].append(
                    (mention, anchor)
                )

        entity_by_category: Dict[
            str, List[EntityHypothesis]
        ] = defaultdict(list)
        for eh in entity_hyps:
            entity_by_category[eh.category].append(eh)

        # --- Seed bindings from entity hints ---
        seed_subgraphs = self._seed_from_entity_hints(
            gs, intent, entity_by_category, mention_by_category,
        )
        if seed_subgraphs:
            beam = seed_subgraphs
        else:
            beam = [
                Subgraph(
                    subgraph_id=Subgraph.generate_id(),
                    bindings={
                        v.var: SubgraphBinding(var_name=v.var)
                        for v in gs.vars
                    },
                )
            ]

        seeded_vars: Set[str] = set()
        if seed_subgraphs:
            for binding in seed_subgraphs[0].bindings.values():
                if binding.bound:
                    seeded_vars.add(binding.var_name)

        var_order = [
            v
            for v in (gs.hard_vars + gs.soft_vars)
            if v.var not in seeded_vars
        ]
        var_order.sort(key=self._var_binding_priority)

        # --- Beam search ---

        for var in var_order:
            next_beam: List[Subgraph] = []

            candidates = self._find_var_candidates(
                var,
                gs,
                mention_by_category,
                entity_by_category,
                skeleton,
                all_mentions,
            )

            if not candidates:
                if var.hard:
                    beam = []
                    break
                for sg in beam:
                    sg.bindings[var.var].bound = False
                    sg.bindings[var.var].quality = (
                        EvidenceQuality.AMBIGUOUS
                    )
                next_beam = beam
            else:
                for sg in beam:
                    for candidate_binding in candidates:
                        new_sg = self._extend_subgraph(
                            sg, var, candidate_binding, gs
                        )
                        next_beam.append(new_sg)
                        stats["beam_states_explored"] += 1

            for sg in next_beam:
                sg.score = self._score_subgraph(sg, gs)
            next_beam.sort(key=lambda s: s.score, reverse=True)
            beam = next_beam[: self.config.beam_width]

        # --- Final scoring and validation ---

        results: List[Subgraph] = []
        for sg in beam:
            sg.hard_coverage = self._compute_hard_coverage(sg, gs)
            sg.soft_coverage = self._compute_soft_coverage(sg, gs)
            sg.score = self._score_subgraph(sg, gs)
            results.append(sg)

        results.sort(key=lambda s: s.score, reverse=True)
        results = self._dedupe_subgraphs(results)
        (
            results,
            unbound_hard_var_rejections,
            unbound_hard_var_rejections_by_var,
        ) = (
            self._filter_unbound_hard_var_subgraphs(results, gs)
        )
        stats["subgraphs_rejected_unbound_hard_vars"] = (
            unbound_hard_var_rejections
        )
        stats["subgraphs_rejected_unbound_hard_vars_by_var"] = (
            unbound_hard_var_rejections_by_var
        )
        results = results[: self.config.max_subgraphs]

        valid_results = [s for s in results if s.is_valid]
        if valid_results:
            results = valid_results

        stats["complete_subgraphs_found"] = len(results)
        logger.info(
            f"  Discovered {len(results)} subgraphs "
            f"({sum(1 for s in results if s.is_valid)} valid)"
        )

        # --- Post-search assembly ---

        # A. Coherence scores
        for sg in results:
            sg.coherence_score = self._compute_coherence_score(sg, gs)

        # B. Diversity reranking
        results = self._diversity_rerank(results)
        stats["subgraphs_after_diversity_rerank"] = len(results)

        # C. Frame witnesses — also materialize proper Witness objects
        #    on each subgraph so the bundle carries auditable provenance
        #    per the paper's Proposition 4 (witness completeness of ALIGN).
        for sg in results:
            sg.frame_witness = self._assemble_frame_witness(sg, gs, intent)
            sg.witnesses = self._frame_witness_to_witness_objects(
                sg, gs, intent,
            )
            stats["witnesses_assembled"] += 1

        logger.info(
            f"  Assembled {stats['witnesses_assembled']} witnesses, "
            f"explored {stats['beam_states_explored']} beam states"
        )

        # Clean up transient state
        self._link_index = {}
        self._anchor_lookup = {}
        self._mention_lookup = {}
        self._entity_lookup = {}

        return results, stats

    def materialize_kg_structure(
        self,
        subgraphs: List[Subgraph],
        entity_hyps: List[EntityHypothesis],
        link_hyps: List[LinkHypothesis],
        all_anchors: Dict[str, List[Anchor]],
        all_mentions: Dict[str, List[Mention]],
        intent: IntentObject,
    ) -> None:
        """
        Populate subgraph snapshots after Phase 5 has completed.

        This keeps live KG structure fetches out of the earlier collection and
        search phases while preserving the downstream snapshot contract.

        When ``defer_kg_structure_until_post_phase5`` is True (the default),
        Phase 5's ``_check_edge_satisfaction`` skipped KG0 path lookups because
        bindings lacked ``kg0_node_id``.  After recovering those IDs below we
        **re-evaluate every edge satisfaction** so the bundle accurately reflects
        actual KG0 connectivity.  Subgraph scores are then recomputed to reflect
        the updated edge state.
        """
        gs = intent.graph_spec
        if gs is None:
            for sg in subgraphs:
                if sg.snapshot is None:
                    sg.snapshot = {"nodes": [], "edges": []}
            return

        self._anchor_lookup = {}
        for anchors in all_anchors.values():
            for anchor in anchors:
                self._anchor_lookup[anchor.anchor_id] = anchor

        self._entity_lookup = {
            eh.hypothesis_id: eh for eh in entity_hyps
        }
        self._mention_lookup = {}
        for mentions in all_mentions.values():
            for mention in mentions:
                self._mention_lookup[mention.mention_id] = mention

        # Rebuild link index for edge satisfaction re-evaluation
        self._link_index = defaultdict(list)
        for lh in link_hyps:
            self._link_index[
                (lh.source_entity_id, lh.target_entity_id)
            ].append(lh)
            self._link_index[
                (lh.target_entity_id, lh.source_entity_id)
            ].append(lh)

        mention_to_kg0_id: Dict[str, str] = {}
        for eh in entity_hyps:
            fallback_kg0_id = (
                str(eh.kg0_entity_ids[0]).strip()
                if eh.kg0_entity_ids
                else ""
            )
            for mention in eh.mentions:
                mention_kg0_id = str(
                    mention.kg0_entity_id or fallback_kg0_id
                ).strip()
                if mention_kg0_id:
                    mention_to_kg0_id[mention.mention_id] = mention_kg0_id
        try:
            edges_flipped = 0
            for sg in subgraphs:
                # --- Recover kg0_node_id on bindings ---
                for binding in sg.bindings.values():
                    if binding.kg0_node_id:
                        continue
                    if binding.entity_hypothesis_id:
                        entity = self._entity_lookup.get(
                            binding.entity_hypothesis_id
                        )
                        if entity and entity.kg0_entity_ids:
                            binding.kg0_node_id = entity.kg0_entity_ids[0]
                            continue
                    if binding.mention_id:
                        recovered_kg0_id = mention_to_kg0_id.get(
                            binding.mention_id
                        )
                        if recovered_kg0_id:
                            binding.kg0_node_id = recovered_kg0_id
                            continue
                    var = gs.var_by_name(binding.var_name)
                    artifact_kg0_id = self._recover_artifact_node_id_for_binding(
                        binding,
                        var,
                    )
                    if artifact_kg0_id:
                        binding.kg0_node_id = artifact_kg0_id

                # --- Re-evaluate edge satisfactions now that kg0_node_ids ---
                # --- are populated (deferred from Phase 5 beam search).  ---
                for edge in gs.edges:
                    edge_key = f"{edge.src}-[{edge.rel}]->{edge.dst}"
                    src_b = sg.bindings.get(edge.src)
                    dst_b = sg.bindings.get(edge.dst)
                    if src_b and src_b.bound and dst_b and dst_b.bound:
                        new_sat = self._check_edge_satisfaction(
                            edge, src_b, dst_b
                        )
                        old_sat = sg.edge_satisfactions.get(edge_key, False)
                        if new_sat and not old_sat:
                            edges_flipped += 1
                        sg.edge_satisfactions[edge_key] = new_sat

                # --- Re-score with updated edge state ---
                sg.score = self._score_subgraph(sg, gs)

                sg.snapshot = self._build_snapshot(sg, gs)

            if edges_flipped:
                logger.info(
                    "  Post-materialization: %d edge satisfactions "
                    "flipped True (deferred KG0 path checks)",
                    edges_flipped,
                )
            # Re-sort subgraphs by updated scores
            subgraphs.sort(key=lambda s: s.score, reverse=True)
        finally:
            self._anchor_lookup = {}
            self._mention_lookup = {}
            self._entity_lookup = {}
            self._link_index = {}

    def _dedupe_subgraphs(
        self,
        subgraphs: List[Subgraph],
    ) -> List[Subgraph]:
        deduped: List[Subgraph] = []
        seen: Set[Tuple[Tuple[str, str, str, str], ...]] = set()
        for sg in subgraphs:
            signature = self._subgraph_signature(sg)
            if signature in seen:
                continue
            seen.add(signature)
            deduped.append(sg)
        return self._dedupe_semantic_subgraphs(deduped)

    def _dedupe_semantic_subgraphs(
        self,
        subgraphs: List[Subgraph],
    ) -> List[Subgraph]:
        deduped: List[Subgraph] = []
        seen: Set[Tuple[Tuple[str, str, str, str], ...]] = set()
        for sg in subgraphs:
            signature = self._subgraph_semantic_signature(sg)
            if signature in seen:
                continue
            seen.add(signature)
            deduped.append(sg)
        return self._dedupe_secondary_variants(deduped)

    def _subgraph_signature(
        self,
        sg: Subgraph,
    ) -> Tuple[Tuple[str, str, str, str], ...]:
        signature: List[Tuple[str, str, str, str]] = []
        for var_name in sorted(sg.bindings.keys()):
            binding = sg.bindings[var_name]
            if not binding.bound:
                signature.append((var_name, "UNBOUND", "", ""))
                continue

            identity_type = ""
            identity_value = ""
            aux = ""
            if binding.entity_hypothesis_id:
                identity_type = "ENTITY"
                identity_value = binding.entity_hypothesis_id
                aux = binding.anchor_id or ""
            elif binding.kg0_node_id:
                identity_type = "KG0"
                identity_value = binding.kg0_node_id
                aux = binding.anchor_id or ""
            elif binding.anchor_id:
                identity_type = "ANCHOR"
                identity_value = binding.anchor_id
            elif binding.mention_id:
                identity_type = "MENTION"
                identity_value = binding.mention_id
            signature.append((var_name, identity_type, identity_value, aux))
        return tuple(signature)

    def _subgraph_semantic_signature(
        self,
        sg: Subgraph,
    ) -> Tuple[Tuple[str, str, str, str], ...]:
        signature: List[Tuple[str, str, str, str]] = []
        for var_name in sorted(sg.bindings.keys()):
            binding = sg.bindings[var_name]
            if not binding.bound:
                signature.append((var_name, "UNBOUND", "", ""))
                continue

            if (
                var_name == "P"
                and self._binding_is_header_only_for_discovery(binding)
            ):
                artifact_ids = sorted(self._binding_artifact_ids(binding))
                signature.append(
                    (
                        var_name,
                        "HEADER_ONLY",
                        artifact_ids[0] if artifact_ids else "",
                        "",
                    )
                )
                continue

            identity_type = ""
            identity_value = ""
            aux = ""
            if binding.entity_hypothesis_id:
                identity_type = "ENTITY"
                identity_value = binding.entity_hypothesis_id
                aux = binding.anchor_id or ""
            elif binding.kg0_node_id:
                identity_type = "KG0"
                identity_value = binding.kg0_node_id
                aux = binding.anchor_id or ""
            elif binding.anchor_id:
                identity_type = "ANCHOR"
                identity_value = binding.anchor_id
            elif binding.mention_id:
                identity_type = "MENTION"
                identity_value = binding.mention_id
            signature.append((var_name, identity_type, identity_value, aux))
        return tuple(signature)

    def _dedupe_secondary_variants(
        self,
        subgraphs: List[Subgraph],
    ) -> List[Subgraph]:
        deduped: List[Subgraph] = []
        by_core_signature: Dict[
            Tuple[Tuple[str, str, str, str], ...],
            Tuple[int, Subgraph],
        ] = {}

        for sg in subgraphs:
            core_signature = self._subgraph_core_signature(sg)
            existing = by_core_signature.get(core_signature)
            if existing is None:
                by_core_signature[core_signature] = (len(deduped), sg)
                deduped.append(sg)
                continue

            existing_index, existing_sg = existing
            existing_is_low = self._subgraph_is_low_value_variant(existing_sg)
            current_is_low = self._subgraph_is_low_value_variant(sg)

            if current_is_low and not existing_is_low:
                continue
            if existing_is_low and not current_is_low:
                deduped[existing_index] = sg
                by_core_signature[core_signature] = (existing_index, sg)
                continue

        return deduped

    def _subgraph_core_signature(
        self,
        sg: Subgraph,
    ) -> Tuple[Tuple[str, str, str, str], ...]:
        signature: List[Tuple[str, str, str, str]] = []
        for var_name in sorted(sg.bindings.keys()):
            if var_name in {"P", "RESP"}:
                continue
            binding = sg.bindings[var_name]
            if not binding.bound:
                signature.append((var_name, "UNBOUND", "", ""))
                continue

            identity_type = ""
            identity_value = ""
            aux = ""
            if binding.entity_hypothesis_id:
                identity_type = "ENTITY"
                identity_value = binding.entity_hypothesis_id
                aux = binding.anchor_id or ""
            elif binding.kg0_node_id:
                identity_type = "KG0"
                identity_value = binding.kg0_node_id
                aux = binding.anchor_id or ""
            elif binding.anchor_id:
                identity_type = "ANCHOR"
                identity_value = binding.anchor_id
            elif binding.mention_id:
                identity_type = "MENTION"
                identity_value = binding.mention_id
            signature.append((var_name, identity_type, identity_value, aux))
        return tuple(signature)

    def _subgraph_is_low_value_variant(
        self,
        sg: Subgraph,
    ) -> bool:
        person_binding = sg.bindings.get("P")
        response_binding = sg.bindings.get("RESP")
        return bool(
            self._binding_is_header_only_for_discovery(person_binding)
            or self._binding_is_secondary_action_for_discovery(response_binding)
        )

    # ----------------------------------------------------------------
    # Seeding
    # ----------------------------------------------------------------

    def _seed_from_entity_hints(
        self,
        gs: GraphSpec,
        intent: IntentObject,
        entity_by_category: Dict[str, List[EntityHypothesis]],
        mention_by_category: Dict[str, List[Tuple[Mention, Anchor]]],
    ) -> Optional[List[Subgraph]]:
        """Create seed subgraphs from entity hints.

        When several hints target the same variable (e.g. a
        ``"how is X connected to Y and Z"`` question with a single
        ``org`` slot and three hints), do not lock that variable to one
        hint. Instead, skip seeding for the variable so Phase 5's beam
        search can explore every hint-grounded entity hypothesis.
        Seeding remains active for variables that have exactly one
        matching hint.
        """
        # Pre-compute which hints match which vars so we can distinguish
        # the single-match case (safe to seed) from the multi-match case
        # (must defer to beam search).
        hints_by_var: Dict[str, List[Tuple[EntityHint, Set[str]]]] = {}
        for hint in intent.entity_hints:
            if str(hint.category or "").upper() == "ENTITY_COLLECTION":
                continue
            # Expand the hint's category to the alias set shared by
            # mentions and entity hypotheses (e.g. ``ORGANIZATION`` and
            # ``ENTITY_ORGANIZATION``), so a raw user-facing category in
            # the intent still matches the adapter-normalized ``ENTITY_*``
            # form used inside ALIGN.
            hint_aliases = self._category_aliases(hint.category)
            for var in gs.vars:
                matches = False
                if var.hint and hint.surface:
                    if (
                        hint.surface.lower() in var.hint.lower()
                        or var.hint.lower() in hint.surface.lower()
                    ):
                        matches = True
                if (
                    not matches
                    and self._category_aliases(var.type) & hint_aliases
                ):
                    matches = True
                if matches:
                    hints_by_var.setdefault(var.var, []).append(
                        (hint, hint_aliases)
                    )

        hint_bindings: Dict[str, SubgraphBinding] = {}

        for hint in intent.entity_hints:
            if str(hint.category or "").upper() == "ENTITY_COLLECTION":
                continue
            hint_aliases = self._category_aliases(hint.category)
            for var in gs.vars:
                # Skip variables that have multiple competing hints: beam
                # search will enumerate all candidates and produce one
                # subgraph per hint-grounded entity hypothesis.
                if len(hints_by_var.get(var.var, [])) > 1:
                    continue
                if var.var in hint_bindings:
                    continue

                hint_matches_var = False
                if var.hint and hint.surface:
                    if (
                        hint.surface.lower() in var.hint.lower()
                        or var.hint.lower() in hint.surface.lower()
                    ):
                        hint_matches_var = True
                if (
                    not hint_matches_var
                    and self._category_aliases(var.type) & hint_aliases
                ):
                    hint_matches_var = True

                if not hint_matches_var:
                    continue

                seen_entity_ids: Set[str] = set()
                matching_entities: List[EntityHypothesis] = []
                for alias in hint_aliases:
                    for eh in entity_by_category.get(alias, []):
                        if eh.hypothesis_id in seen_entity_ids:
                            continue
                        if (
                            hint.surface.lower()
                            in eh.canonical_name.lower()
                            or eh.canonical_name.lower()
                            in hint.surface.lower()
                        ):
                            seen_entity_ids.add(eh.hypothesis_id)
                            matching_entities.append(eh)

                if matching_entities:
                    eh = max(
                        matching_entities,
                        key=lambda e: e.confidence,
                    )
                    hint_bindings[var.var] = SubgraphBinding(
                        var_name=var.var,
                        entity_hypothesis_id=eh.hypothesis_id,
                        kg0_node_id=(
                            eh.kg0_entity_ids[0]
                            if eh.kg0_entity_ids
                            else None
                        ),
                        anchor_id=(
                            eh.mentions[0].anchor_id
                            if eh.mentions
                            else None
                        ),
                        mention_id=(
                            eh.mentions[0].mention_id
                            if eh.mentions
                            else None
                        ),
                        bound=True,
                        quality=EvidenceQuality.GROUNDED,
                    )
                else:
                    matched = False
                    for alias in hint_aliases:
                        if matched:
                            break
                        for mention, anchor in mention_by_category.get(
                            alias, []
                        ):
                            if (
                                hint.surface.lower()
                                in mention.surface.lower()
                            ):
                                hint_bindings[var.var] = SubgraphBinding(
                                    var_name=var.var,
                                    anchor_id=anchor.anchor_id,
                                    mention_id=mention.mention_id,
                                    bound=True,
                                    quality=EvidenceQuality.GROUNDED,
                                )
                                matched = True
                                break

        if not hint_bindings:
            return None

        seed = Subgraph(
            subgraph_id=Subgraph.generate_id(),
            bindings={
                v.var: SubgraphBinding(var_name=v.var)
                for v in gs.vars
            },
        )
        for var_name, binding in hint_bindings.items():
            seed.bindings[var_name] = binding

        logger.info(
            f"  Seeded {len(hint_bindings)} variables from entity "
            f"hints: {list(hint_bindings.keys())}"
        )
        return [seed]

    # ----------------------------------------------------------------
    # Candidate finding
    # ----------------------------------------------------------------

    def _var_compatible_categories(self, var_type: str) -> Set[str]:
        """Expanded category aliases that can satisfy a graph variable."""
        if not var_type:
            return set()

        ontology = self.config.ontology
        raw_categories = ontology.expand_category(var_type)
        if not raw_categories:
            raw_categories = {var_type.upper()}

        expanded: Set[str] = set()
        for category in raw_categories:
            expanded |= self._category_aliases(category)
        if not expanded:
            expanded = self._category_aliases(var_type)
        return expanded

    def _category_aliases(self, category: str) -> Set[str]:
        normalized = str(category or "").strip().upper()
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

    @staticmethod
    def _is_artifact_document_var(var: GraphVar) -> bool:
        return str(var.type or "").strip().upper() in {
            "ARTIFACT_DOCUMENT",
            "ARTIFACT_PDF",
        }

    def _text_tokens(self, text: str) -> Set[str]:
        tokens: Set[str] = set()
        for token in re.findall(r"[A-Za-z0-9]+", (text or "").lower()):
            if len(token) <= 1:
                continue
            tokens.add(token)
            if token.endswith("ies") and len(token) > 4:
                tokens.add(token[:-3] + "y")
            elif token.endswith("s") and len(token) > 4:
                tokens.add(token[:-1])
        return tokens

    def _is_disclaimer_context(self, text: str) -> bool:
        lowered = str(text or "").lower()
        if not lowered:
            return False
        return any(cue in lowered for cue in Phase3_AnchorMentionExtraction._DISCLAIMER_CUES)

    def _mention_in_disclaimer_context(
        self,
        mention: Mention,
        anchor: Optional[Anchor],
    ) -> bool:
        if anchor is None:
            return False
        raw_text = str(anchor.raw_text or "")
        start = max(0, int(mention.span_start) - 120)
        end = min(len(raw_text), int(mention.span_end) + 120)
        return self._is_disclaimer_context(raw_text[start:end])

    def _score_var_candidate(
        self,
        var: GraphVar,
        binding: SubgraphBinding,
        entity_hyp: Optional[EntityHypothesis] = None,
        mention: Optional[Mention] = None,
        anchor: Optional[Anchor] = None,
    ) -> float:
        """Intrinsic candidate score used before beam expansion."""
        score = 0.0
        compatible_categories = self._var_compatible_categories(var.type)
        concept_like_categories = {
            "ENTITY_CONCEPT",
            "CONCEPT",
            "ENTITY_TOPIC",
            "TOPIC",
            "ENTITY_RISK_FINDING",
            "RISK_FINDING",
        }

        if entity_hyp is not None:
            score += 1.15 * float(entity_hyp.confidence)
            if entity_hyp.kg0_entity_ids:
                score += 0.30
            if entity_hyp.kg0_link_candidates:
                score += min(
                    0.15,
                    0.04 * len(entity_hyp.kg0_link_candidates),
                )

        if mention is not None:
            score += float(mention.confidence)
            surface_tokens = len((mention.surface or "").split())
            if surface_tokens >= 5:
                score += 0.08
            if mention.kg0_entity_id:
                score += 0.18
            kg_link = mention.qualifiers.get("kg_link", {}) if mention.qualifiers else {}
            best_link_score = float(kg_link.get("best_score", 0.0) or 0.0)
            if best_link_score:
                score += min(0.18, 0.22 * best_link_score)
            if var.hint:
                hint_tokens = self._text_tokens(var.hint)
                mention_tokens = self._text_tokens(
                    f"{mention.surface} {mention.normalized}"
                )
                score += 0.08 * len(hint_tokens & mention_tokens)

        primary_category = ""
        if entity_hyp is not None:
            primary_category = str(entity_hyp.category or "").upper()
        elif mention is not None:
            primary_category = str(mention.category or "").upper()

        if binding.quality == EvidenceQuality.GROUNDED:
            score += 0.18
        elif binding.quality == EvidenceQuality.INFERRED:
            score += 0.08

        if binding.kg0_node_id:
            score += 0.16

        if primary_category == "ENTITY_ROLE":
            if mention is not None:
                score += self._role_surface_specificity_bonus(
                    mention.surface
                )
                if (
                    anchor is not None
                    and self._mention_in_disclaimer_context(mention, anchor)
                ):
                    score -= 0.40
                role_tokens = self._text_tokens(mention.surface)
                if (
                    len(role_tokens) == 1
                    and str(mention.surface or "").strip().lower()
                    == str(mention.surface or "").strip()
                ):
                    score -= 0.22
            if anchor is not None and not self._anchor_is_header_like_for_discovery(anchor):
                score += 0.16
        elif primary_category == "ENTITY_PERSON":
            if mention is not None:
                score += self._person_surface_specificity_bonus(
                    mention.surface
                )
                score += self._person_role_context_bonus(
                    entity_hyp,
                    mention,
                    anchor,
                )
            if entity_hyp is not None and any(
                (
                    self._anchor_lookup.get(m.anchor_id) is not None
                    and not self._anchor_is_header_like_for_discovery(
                        self._anchor_lookup[m.anchor_id]
                    )
                )
                for m in entity_hyp.mentions
            ):
                score += 0.30
            elif anchor is not None and self._anchor_is_header_like_for_discovery(anchor):
                score -= 0.22
        elif primary_category == "ENTITY_ORGANIZATION":
            if mention is not None:
                score += self._organization_surface_specificity_bonus(
                    mention.surface
                )
            if anchor is not None and not self._anchor_is_header_like_for_discovery(anchor):
                score += 0.18

        if compatible_categories & concept_like_categories:
            if entity_hyp and entity_hyp.category in concept_like_categories:
                score += 0.24
            if entity_hyp and entity_hyp.kg0_entity_ids:
                score += 0.16
            if mention and mention.kg0_entity_id:
                score += 0.14
            surface_text = ""
            if mention is not None:
                surface_text = str(mention.surface or "")
            elif entity_hyp is not None:
                surface_text = str(entity_hyp.canonical_name or "")
            concept_tokens = self._text_tokens(surface_text)
            generic_concept_tokens = {
                "condition",
                "conditions",
                "disease",
                "diseases",
                "symptom",
                "symptoms",
                "complication",
                "complications",
                "disorder",
                "disorders",
                "factors",
                "factor",
            }
            if concept_tokens and concept_tokens <= generic_concept_tokens:
                score -= 0.30
                if anchor is not None and self._anchor_is_header_like_for_discovery(anchor):
                    score -= 0.16
            elif concept_tokens:
                score += 0.10
            if mention and re.search(
                r"\b(?:depression|anxiety|obesity|ptsd|cptsd|insomnia|brain\s+injury|"
                r"traumatic\s+brain\s+injury|post-?concussive\s+symptoms?)\b",
                mention.surface or "",
                re.IGNORECASE,
            ):
                score += 0.16

        if {
            "ARTIFACT_DOCUMENT",
            "ENTITY_DOCUMENT",
            "DOCUMENT",
        } & compatible_categories:
            if entity_hyp and entity_hyp.category in {
                "ENTITY_DOCUMENT",
                "DOCUMENT",
                "ARTIFACT_DOCUMENT",
            }:
                score += 0.45
            if mention and mention.category in {
                "ENTITY_DOCUMENT",
                "DOCUMENT",
            }:
                score += 0.30
            anchor_family = (
                str(anchor.metadata.get("family", "")).upper()
                if anchor is not None
                else ""
            )
            if anchor_family in {
                "DOCUMENT",
                "EMAIL",
                "TEXT",
                "SPREADSHEET",
                "PRESENTATION",
            }:
                score += 0.12
            if entity_hyp and entity_hyp.kg0_link_candidates:
                score += 0.10
            if anchor is not None and self._anchor_is_header_like_for_discovery(anchor):
                score += 0.16
            if mention and self._is_document_like_form_surface(mention.surface):
                if anchor is not None and not self._anchor_is_header_like_for_discovery(anchor):
                    score -= 0.22
                else:
                    score += 0.06
            if (
                entity_hyp
                and self._is_document_like_form_surface(entity_hyp.canonical_name)
                and not any(
                    self._anchor_is_header_like_for_discovery(
                        self._anchor_lookup[m.anchor_id]
                    )
                    for m in entity_hyp.mentions
                    if self._anchor_lookup.get(m.anchor_id) is not None
                )
            ):
                score -= 0.18

        if {"ENTITY_CLAIM", "CLAIM"} & compatible_categories:
            if entity_hyp and entity_hyp.category in {
                "ENTITY_CLAIM",
                "CLAIM",
            }:
                score += 0.35
            if mention and len((mention.surface or "").split()) >= 6:
                score += 0.15
            if anchor is not None and not self._anchor_is_header_like_for_discovery(anchor):
                score += 0.10

        if {"ENTITY_POLICY", "POLICY"} & compatible_categories:
            if entity_hyp and entity_hyp.category in {
                "ENTITY_POLICY",
                "POLICY",
            }:
                score += 0.28
            if entity_hyp and entity_hyp.kg0_entity_ids:
                score += 0.12
            if mention and re.search(
                r"\b(policy|dispensing|fulfillment|compliance|requirement)\b",
                mention.surface or "",
                re.IGNORECASE,
            ):
                score += 0.10
            if mention and mention.kg0_entity_id:
                score += 0.18

        if {"ENTITY_TEXT_SPAN", "TEXT_SPAN"} & compatible_categories:
            if mention and len((mention.surface or "").split()) >= 6:
                score += 0.36
            if mention and mention.category in {"ENTITY_CLAIM", "CLAIM"}:
                score += 0.24
            if anchor is not None and not self._anchor_is_header_like_for_discovery(anchor):
                score += 0.18

        if {"ENTITY_RISK_FINDING", "RISK_FINDING"} & compatible_categories:
            if entity_hyp and entity_hyp.category in {
                "ENTITY_RISK_FINDING",
                "RISK_FINDING",
            }:
                score += 0.28
            if mention and re.search(
                r"\b(red-?flag|shopping|diversion|abuse|misuse|inappropriate)\b",
                mention.surface or "",
                re.IGNORECASE,
            ):
                score += 0.14

        if {"ENTITY_ACTION_ITEM", "ACTION_ITEM"} & compatible_categories:
            if mention and re.search(
                r"\b(move\s+forward|next\s+steps?|review|implementation|respond)\b",
                mention.surface or "",
                re.IGNORECASE,
            ):
                score += 0.14
            if mention is not None:
                score += self._action_item_specificity_bonus(
                    mention.surface
                )
                score += self._action_item_context_bonus(
                    mention,
                    anchor,
                )

        return score

    def _best_kg_candidate_score(self, candidates: List[Dict[str, Any]]) -> float:
        best = 0.0
        for candidate in candidates or []:
            score = float(
                candidate.get("normalized_score")
                or candidate.get("aggregate_score")
                or candidate.get("score")
                or 0.0
            )
            if score > best:
                best = score
        return best

    def _binding_quality_for_entity_hypothesis(
        self,
        entity_hyp: EntityHypothesis,
    ) -> EvidenceQuality:
        category = str(entity_hyp.category or "").upper()
        best_candidate_score = self._best_kg_candidate_score(
            entity_hyp.kg0_link_candidates
        )
        if entity_hyp.kg0_entity_ids:
            return EvidenceQuality.GROUNDED
        if category in {"ENTITY_ROLE", "ROLE"}:
            for mention in entity_hyp.mentions:
                anchor = self._anchor_lookup.get(mention.anchor_id)
                tokens = self._text_tokens(mention.surface)
                if (
                    anchor is not None
                    and not self._anchor_is_header_like_for_discovery(anchor)
                    and len(tokens) >= 2
                    and not self._mention_in_disclaimer_context(mention, anchor)
                ):
                    return EvidenceQuality.GROUNDED
            return EvidenceQuality.INFERRED
        if category in {"ENTITY_ACTION_ITEM", "ACTION_ITEM"}:
            for mention in entity_hyp.mentions:
                anchor = self._anchor_lookup.get(mention.anchor_id)
                if (
                    anchor is not None
                    and not self._anchor_is_header_like_for_discovery(anchor)
                    and (
                        self._action_item_specificity_bonus(mention.surface)
                        + self._action_item_context_bonus(mention, anchor)
                    )
                    >= 0.14
                ):
                    return EvidenceQuality.GROUNDED
            return EvidenceQuality.INFERRED
        if category in {
            "ENTITY_DOCUMENT",
            "DOCUMENT",
            "ARTIFACT_DOCUMENT",
            "ENTITY_POLICY",
            "POLICY",
        } and best_candidate_score >= 0.82:
            return EvidenceQuality.GROUNDED
        if category in {"ENTITY_RISK_FINDING", "RISK_FINDING"}:
            if best_candidate_score >= 0.28:
                return EvidenceQuality.GROUNDED
            if entity_hyp.confidence >= 0.70 and any(
                len((mention.surface or "").split()) >= 2
                and (
                    self._anchor_lookup.get(mention.anchor_id) is not None
                    and not self._anchor_is_header_like_for_discovery(
                        self._anchor_lookup[mention.anchor_id]
                    )
                )
                for mention in entity_hyp.mentions
            ):
                return EvidenceQuality.GROUNDED
        if entity_hyp.confidence >= 0.76:
            return EvidenceQuality.GROUNDED
        return EvidenceQuality.INFERRED

    def _binding_quality_for_mention(
        self,
        var: GraphVar,
        mention: Mention,
        anchor: Optional[Anchor],
    ) -> EvidenceQuality:
        compatible_categories = self._var_compatible_categories(var.type)
        kg_link = mention.qualifiers.get("kg_link", {}) if mention.qualifiers else {}
        best_link_score = float(kg_link.get("best_score", 0.0) or 0.0)
        token_count = len((mention.surface or "").split())

        if mention.kg0_entity_id:
            return EvidenceQuality.GROUNDED
        if {"ENTITY_TEXT_SPAN", "TEXT_SPAN"} & compatible_categories:
            if (
                anchor is not None
                and not self._anchor_is_header_like_for_discovery(anchor)
                and token_count >= 6
            ):
                return EvidenceQuality.GROUNDED
        if {"ENTITY_DOCUMENT", "DOCUMENT", "ARTIFACT_DOCUMENT"} & compatible_categories:
            if best_link_score >= 0.80:
                return EvidenceQuality.GROUNDED
        if {"ENTITY_POLICY", "POLICY"} & compatible_categories:
            if best_link_score >= 0.80 or mention.confidence >= 0.76:
                return EvidenceQuality.GROUNDED
        if {"ENTITY_RISK_FINDING", "RISK_FINDING"} & compatible_categories:
            if (
                mention.confidence >= 0.70
                and token_count >= 2
                and anchor is not None
                and not self._anchor_is_header_like_for_discovery(anchor)
            ):
                return EvidenceQuality.GROUNDED
        if {"ENTITY_CLAIM", "CLAIM"} & compatible_categories:
            if (
                anchor is not None
                and not self._anchor_is_header_like_for_discovery(anchor)
                and token_count >= 8
            ):
                return EvidenceQuality.GROUNDED
        return (
            EvidenceQuality.GROUNDED
            if mention.confidence > 0.74
            else EvidenceQuality.INFERRED
        )

    def _append_scored_candidate(
        self,
        scored_candidates: List[Tuple[float, SubgraphBinding]],
        seen_keys: Set[Tuple[str, str, str, str]],
        score: float,
        binding: SubgraphBinding,
    ) -> None:
        key = (
            binding.entity_hypothesis_id or "",
            binding.mention_id or "",
            binding.kg0_node_id or "",
            binding.anchor_id or "",
        )
        if key in seen_keys:
            return
        seen_keys.add(key)
        scored_candidates.append((score, binding))

    def _role_surface_specificity_bonus(self, surface: str) -> float:
        tokens = self._text_tokens(surface)
        bonus = 0.0
        if len(tokens) >= 2:
            bonus += 0.18
        if len(tokens) >= 3:
            bonus += 0.10
        if any(
            token in {
                "senior",
                "assistant",
                "associate",
                "chief",
                "confidential",
                "litigation",
                "regulatory",
                "r",
                "ph",
            }
            for token in tokens
        ):
            bonus += 0.12
        if any(
            token
            in {
                "attorney",
                "counsel",
                "reviewer",
                "director",
                "manager",
                "supervisor",
                "officer",
            }
            for token in tokens
        ):
            bonus += 0.12
        if "confidential" in tokens:
            bonus -= 0.18
        if tokens <= {"pharmacist"}:
            bonus -= 0.16
        return bonus

    def _action_item_specificity_bonus(self, surface: str) -> float:
        tokens = self._text_tokens(surface)
        bonus = 0.0
        if len(tokens) >= 2:
            bonus += 0.08
        if tokens & Phase3_AnchorMentionExtraction._ACTION_ITEM_DECISIVE_TOKENS:
            bonus += 0.18
        if tokens <= {"next", "steps"} or tokens <= {"next", "step"}:
            bonus -= 0.12
        if tokens <= {"final", "review"} or tokens <= {"review"}:
            bonus -= 0.10
        if tokens <= {"follow", "up"}:
            bonus -= 0.18
        if tokens <= {"move", "forward"}:
            bonus += 0.08
        if (
            tokens & Phase3_AnchorMentionExtraction._ACTION_ITEM_HEDGE_TOKENS
            and not tokens
            & Phase3_AnchorMentionExtraction._ACTION_ITEM_DECISIVE_TOKENS
        ):
            bonus -= 0.10
        return bonus

    def _person_role_context_bonus(
        self,
        entity_hyp: Optional[EntityHypothesis],
        mention: Optional[Mention],
        anchor: Optional[Anchor],
    ) -> float:
        anchor_candidates: List[Anchor] = []
        if anchor is not None:
            anchor_candidates.append(anchor)
        if entity_hyp is not None:
            for hyp_mention in entity_hyp.mentions:
                hyp_anchor = self._anchor_lookup.get(hyp_mention.anchor_id)
                if hyp_anchor is not None and hyp_anchor not in anchor_candidates:
                    anchor_candidates.append(hyp_anchor)

        bonus = 0.0
        for candidate_anchor in anchor_candidates:
            if self._anchor_is_header_like_for_discovery(candidate_anchor):
                continue
            candidate_mentions = self._mention_lookup.get(
                candidate_anchor.anchor_id,
                []
            )
            categories = {
                str(m.category or "").upper()
                for m in candidate_mentions
            }
            if "ENTITY_ROLE" in categories:
                bonus = max(bonus, 0.30)
            if "ENTITY_ORGANIZATION" in categories:
                bonus = max(bonus, 0.22)
            raw_text = str(candidate_anchor.raw_text or "")
            if re.search(
                r"\b(?:attorney|counsel|manager|director|officer|supervisor)\b",
                raw_text,
                re.IGNORECASE,
            ):
                bonus = max(bonus, 0.28)
            if re.search(
                r"\b\d{3}[-)\s]\d{3}[-\s]\d{4}\b|@[A-Za-z0-9._-]+",
                raw_text,
            ):
                bonus = max(bonus, 0.24)
            if self._anchor_has_signature_context_for_discovery(
                candidate_anchor
            ):
                bonus = max(bonus, 0.42)
        return bonus

    def _anchor_has_signature_context_for_discovery(
        self,
        anchor: Optional[Anchor],
    ) -> bool:
        if anchor is None:
            return False
        if self._anchor_is_header_like_for_discovery(anchor):
            return False
        raw_text = str(anchor.raw_text or "")
        if not raw_text:
            return False
        if re.search(
            r"\b\d{3}[-)\s]\d{3}[-\s]\d{4}\b|@[A-Za-z0-9._-]+",
            raw_text,
        ) is None:
            return False
        return bool(
            re.search(
                r"\b(?:attorney|counsel|manager|director|officer|supervisor|road|ms\s*#)\b",
                raw_text,
                re.IGNORECASE,
            )
        )

    def _action_item_context_bonus(
        self,
        mention: Optional[Mention],
        anchor: Optional[Anchor],
    ) -> float:
        if mention is None or anchor is None:
            return 0.0
        raw_text = str(anchor.raw_text or "")
        if not raw_text:
            return 0.0
        start = max(0, int(mention.span_start) - 140)
        end = min(len(raw_text), int(mention.span_end) + 140)
        context_window = raw_text[start:end].lower()
        tokens = self._text_tokens(mention.surface)
        bonus = 0.0
        if tokens & Phase3_AnchorMentionExtraction._ACTION_ITEM_DECISIVE_TOKENS:
            bonus += 0.12
        if any(
            cue in context_window
            for cue in Phase3_AnchorMentionExtraction._ACTION_ITEM_PLANNED_CUES
        ):
            bonus += 0.10
        if tokens <= {"move", "forward"}:
            bonus += 0.10
        if any(
            cue in context_window
            for cue in Phase3_AnchorMentionExtraction._ACTION_ITEM_HEDGE_CUES
        ):
            bonus -= 0.16
        if tokens <= {"follow", "up"}:
            bonus -= 0.16
            if re.search(
                r"\bfollow(?:-?\s*up)\s+process(?:es)?\b",
                context_window,
            ):
                bonus -= 0.10
        if (
            tokens & Phase3_AnchorMentionExtraction._ACTION_ITEM_HEDGE_TOKENS
            and not tokens
            & Phase3_AnchorMentionExtraction._ACTION_ITEM_DECISIVE_TOKENS
        ):
            bonus -= 0.10
        if self._anchor_is_header_like_for_discovery(anchor):
            bonus -= 0.08
        return bonus

    def _person_surface_specificity_bonus(self, surface: str) -> float:
        normalized = str(surface or "").strip()
        tokens = normalized.split()
        bonus = 0.0
        if len(tokens) >= 2:
            bonus += 0.08
        if len(tokens) >= 3:
            bonus += 0.08
        if re.search(r"\b[A-Z]\.", normalized):
            bonus += 0.12
        if "," in normalized:
            bonus -= 0.03
        return bonus

    def _organization_surface_specificity_bonus(self, surface: str) -> float:
        tokens = self._text_tokens(surface)
        bonus = 0.0
        if len(tokens) >= 2:
            bonus += 0.08
        if len(tokens) >= 3:
            bonus += 0.10
        if any(
            token
            in {
                "regulatory",
                "litigation",
                "law",
                "legal",
                "compliance",
                "group",
                "office",
            }
            for token in tokens
        ):
            bonus += 0.14
        if tokens <= {"combined", "company"} or "company" in tokens and len(tokens) <= 2:
            bonus -= 0.18
        return bonus

    def _is_document_like_form_surface(self, surface: str) -> bool:
        tokens = self._text_tokens(surface)
        if not tokens:
            return False
        return bool(
            tokens
            & Phase3_AnchorMentionExtraction._DOCUMENT_LIKE_FORM_TOKENS
        )

    def _var_binding_priority(self, var: GraphVar) -> Tuple[int, int, str]:
        aliases = self._var_compatible_categories(var.type)
        if aliases & {"ARTIFACT_DOCUMENT", "ENTITY_DOCUMENT", "DOCUMENT"}:
            return (0, 0 if var.hard else 1, var.var)
        if aliases & {"ENTITY_STRATEGY", "STRATEGY"}:
            return (1, 0 if var.hard else 1, var.var)
        if aliases & {"ENTITY_CLAIM", "CLAIM"}:
            return (2, 0 if var.hard else 1, var.var)
        if aliases & {"ENTITY_POLICY", "POLICY"}:
            return (3, 0 if var.hard else 1, var.var)
        if aliases & {"ENTITY_TEXT_SPAN", "TEXT_SPAN"}:
            return (4, 0 if var.hard else 1, var.var)
        if aliases & {"ENTITY_RISK_FINDING", "RISK_FINDING"}:
            return (5, 0 if var.hard else 1, var.var)
        if aliases & {"ENTITY_EVENT", "EVENT"}:
            return (6, 0 if var.hard else 1, var.var)
        if aliases & {"ENTITY_COLLECTION", "COLLECTION"}:
            return (8, 0 if var.hard else 1, var.var)
        return (7, 0 if var.hard else 1, var.var)

    def _find_var_candidates(
        self,
        var: GraphVar,
        gs: GraphSpec,
        mention_by_category: Dict[str, List[Tuple[Mention, Anchor]]],
        entity_by_category: Dict[str, List[EntityHypothesis]],
        skeleton: CompiledGraphSkeleton,
        all_mentions: Dict[str, List[Mention]],
    ) -> List[SubgraphBinding]:
        """Find candidate bindings for a graph spec variable."""
        scored_candidates: List[Tuple[float, SubgraphBinding]] = []
        seen_keys: Set[Tuple[str, str, str, str]] = set()
        compatible_categories = self._var_compatible_categories(var.type)
        artifact_document_var = self._is_artifact_document_var(var)

        if not artifact_document_var:
            for cat in compatible_categories:
                for eh in entity_by_category.get(cat, []):
                    binding = SubgraphBinding(
                        var_name=var.var,
                        entity_hypothesis_id=eh.hypothesis_id,
                        kg0_node_id=(
                            eh.kg0_entity_ids[0]
                            if eh.kg0_entity_ids
                            else None
                        ),
                        anchor_id=(
                            eh.mentions[0].anchor_id
                            if eh.mentions
                            else None
                        ),
                        mention_id=(
                            eh.mentions[0].mention_id
                            if eh.mentions
                            else None
                        ),
                        bound=True,
                        quality=self._binding_quality_for_entity_hypothesis(eh),
                    )
                    anchor = (
                        self._anchor_lookup.get(binding.anchor_id)
                        if binding.anchor_id
                        else None
                    )
                    mention = eh.mentions[0] if eh.mentions else None
                    score = self._score_var_candidate(
                        var,
                        binding,
                        entity_hyp=eh,
                        mention=mention,
                        anchor=anchor,
                    )
                    self._append_scored_candidate(
                        scored_candidates,
                        seen_keys,
                        score,
                        binding,
                    )

            for cat in compatible_categories:
                for mention, anchor in mention_by_category.get(cat, [])[:50]:
                    if var.hint:
                        hint_lower = var.hint.lower()
                        if (
                            hint_lower not in mention.surface.lower()
                            and hint_lower not in mention.normalized.lower()
                        ):
                            continue
                    binding = SubgraphBinding(
                        var_name=var.var,
                        anchor_id=anchor.anchor_id,
                        mention_id=mention.mention_id,
                        bound=True,
                        quality=self._binding_quality_for_mention(
                            var,
                            mention,
                            anchor,
                        ),
                    )
                    score = self._score_var_candidate(
                        var,
                        binding,
                        mention=mention,
                        anchor=anchor,
                    )
                    self._append_scored_candidate(
                        scored_candidates,
                        seen_keys,
                        score,
                        binding,
                    )

        if {"ARTIFACT_DOCUMENT", "ENTITY_DOCUMENT", "DOCUMENT"} & compatible_categories:
            for score, binding in self._artifact_document_anchor_candidates(var):
                self._append_scored_candidate(
                    scored_candidates,
                    seen_keys,
                    score,
                    binding,
                )

        if {"ENTITY_TEXT_SPAN", "TEXT_SPAN"} & compatible_categories:
            for claim_cat in ("ENTITY_CLAIM", "CLAIM"):
                for eh in entity_by_category.get(claim_cat, [])[:20]:
                    for mention in eh.mentions[:2]:
                        anchor = self._anchor_lookup.get(mention.anchor_id)
                        if anchor is None:
                            continue
                        binding = SubgraphBinding(
                            var_name=var.var,
                            anchor_id=anchor.anchor_id,
                            mention_id=mention.mention_id,
                            bound=True,
                            quality=self._binding_quality_for_mention(
                                var,
                                mention,
                                anchor,
                            ),
                        )
                        score = self._score_var_candidate(
                            var,
                            binding,
                            entity_hyp=eh,
                            mention=mention,
                            anchor=anchor,
                        )
                        self._append_scored_candidate(
                            scored_candidates,
                            seen_keys,
                            score,
                            binding,
                        )

        kg0_labels = skeleton.var_labels.get(var.var, [])
        if (
            kg0_labels
            and not scored_candidates
            and not self.config.defer_kg_structure_until_post_phase5
        ):
            for label in kg0_labels[:2]:
                if not self.index.neo4j.label_exists(label):
                    logger.debug("Skipping nonexistent KG0 label %s for var %s", label, var.var)
                    continue
                cypher = (
                    f"MATCH (n:{label}) "
                    f"WHERE {_node_text_expression('n')} CONTAINS toLower($hint) "
                    f"RETURN {_node_id_expression(self.index.neo4j, 'n', as_name='id')}, "
                    f"coalesce(n.name, n.title, n.subject, "
                    f"n.summary, n.text, n.description, n.rationale, "
                    f"n.citationText, n.context, n.caption, n.notes, "
                    f"n.abbvName, n.fullForm, n.contextOfDate, n.identifier, "
                    f"n.recordId, n.sourceFileName, n.witness, "
                    f"n.witnessContext, '') AS name "
                    f"LIMIT 10"
                )
                try:
                    results = self.index.neo4j.execute_cypher(
                        cypher, {"hint": var.hint or ""}
                    )
                    for rec in results:
                        binding = SubgraphBinding(
                            var_name=var.var,
                            kg0_node_id=rec.get("id", ""),
                            bound=True,
                            quality=EvidenceQuality.INFERRED,
                        )
                        score = self._score_var_candidate(var, binding)
                        self._append_scored_candidate(
                            scored_candidates,
                            seen_keys,
                            score,
                            binding,
                        )
                except _NEO4J_QUERY_ERRORS as exc:
                    logger.debug(
                        "KG0 fallback query for %s failed (%s): %s",
                        var.var,
                        type(exc).__name__,
                        exc,
                    )

        scored_candidates.sort(
            key=lambda item: (
                item[0],
                item[1].quality == EvidenceQuality.GROUNDED,
                bool(item[1].kg0_node_id),
                bool(item[1].entity_hypothesis_id),
                item[1].mention_id or "",
                item[1].kg0_node_id or "",
            ),
            reverse=True,
        )
        return [binding for _, binding in scored_candidates[:20]]

    def _artifact_document_anchor_candidates(
        self,
        var: GraphVar,
    ) -> List[Tuple[float, SubgraphBinding]]:
        """Use retrieved anchors as candidates for artifact document vars."""
        by_artifact: Dict[str, Tuple[float, SubgraphBinding]] = {}
        for anchor in self._anchor_lookup.values():
            score = self._score_artifact_document_anchor(var, anchor)
            if score <= 0.0:
                continue
            binding = SubgraphBinding(
                var_name=var.var,
                anchor_id=anchor.anchor_id,
                bound=True,
                quality=(
                    EvidenceQuality.GROUNDED
                    if score >= 0.45
                    else EvidenceQuality.INFERRED
                ),
            )
            artifact_id = str(anchor.artifact_id or "")
            current = by_artifact.get(artifact_id)
            if current is None or score > current[0]:
                by_artifact[artifact_id] = (score, binding)

        return sorted(
            by_artifact.values(),
            key=lambda item: (
                item[0],
                item[1].anchor_id or "",
            ),
            reverse=True,
        )

    def _score_artifact_document_anchor(
        self,
        var: GraphVar,
        anchor: Anchor,
    ) -> float:
        metadata = anchor.metadata or {}
        family = str(metadata.get("family", "") or "").upper()
        family_norm = (
            family.replace("ARTIFACT_", "", 1)
            if family.startswith("ARTIFACT_")
            else family
        )
        if family_norm and family_norm not in {
            "DOCUMENT",
            "PDF",
            "TEXT",
            "EMAIL",
            "PRESENTATION",
            "SPREADSHEET",
        }:
            return 0.0

        haystack = " ".join(
            str(value or "")
            for value in (
                anchor.raw_text,
                metadata.get("artifact_name"),
                metadata.get("title"),
                metadata.get("subject"),
                metadata.get("document_id"),
                metadata.get("filename"),
                metadata.get("sourceFileName"),
            )
        )
        haystack_tokens = self._text_tokens(haystack)
        hint_tokens = self._text_tokens(var.hint)
        role_tokens = self._text_tokens(var.role)
        query_tokens = hint_tokens or role_tokens
        if query_tokens and not (query_tokens & haystack_tokens):
            return 0.0

        score = 0.10
        if family_norm in {"DOCUMENT", "PDF"}:
            score += 0.18
        elif family_norm:
            score += 0.08
        if anchor.relevance_score:
            score += min(0.30, float(anchor.relevance_score or 0.0) * 0.30)
        if hint_tokens:
            score += min(0.36, 0.09 * len(hint_tokens & haystack_tokens))
        if role_tokens:
            score += min(0.16, 0.04 * len(role_tokens & haystack_tokens))
        if self._anchor_is_header_like_for_discovery(anchor):
            score += 0.08
        return score

    # ----------------------------------------------------------------
    # Beam search helpers
    # ----------------------------------------------------------------

    def _extend_subgraph(
        self,
        sg: Subgraph,
        var: GraphVar,
        binding: SubgraphBinding,
        gs: GraphSpec,
    ) -> Subgraph:
        """Create a new subgraph by binding one variable."""
        new_bindings = {
            k: SubgraphBinding(
                var_name=v.var_name,
                entity_hypothesis_id=v.entity_hypothesis_id,
                kg0_node_id=v.kg0_node_id,
                anchor_id=v.anchor_id,
                mention_id=v.mention_id,
                bound=v.bound,
                quality=v.quality,
            )
            for k, v in sg.bindings.items()
        }
        new_bindings[var.var] = binding

        new_edge_sats = dict(sg.edge_satisfactions)
        for edge in gs.edges:
            edge_key = f"{edge.src}-[{edge.rel}]->{edge.dst}"
            if edge_key in new_edge_sats:
                continue
            src_b = new_bindings.get(edge.src)
            dst_b = new_bindings.get(edge.dst)
            if src_b and src_b.bound and dst_b and dst_b.bound:
                new_edge_sats[edge_key] = (
                    self._check_edge_satisfaction(edge, src_b, dst_b)
                )

        return Subgraph(
            subgraph_id=Subgraph.generate_id(),
            bindings=new_bindings,
            edge_satisfactions=new_edge_sats,
            witnesses=list(sg.witnesses),
        )

    def _check_edge_satisfaction(
        self,
        edge: GraphEdge,
        src_binding: SubgraphBinding,
        dst_binding: SubgraphBinding,
    ) -> bool:
        """
        Check if an edge constraint is satisfied.

        Evidence tiers, strongest first:
            1. KG0 graph connectivity
            2. Link hypothesis (cross-artifact)
            3. Structural co-occurrence (same artifact)
        Hard edges require tier 1 or 2, or strong co-occurrence.
        """
        src_categories = self._binding_category_aliases(src_binding)
        dst_categories = self._binding_category_aliases(dst_binding)
        src_anchors = self._resolve_binding_anchors(src_binding)
        dst_anchors = self._resolve_binding_anchors(dst_binding)

        if edge.rel == "EVIDENCED_BY":
            if {"ENTITY_COLLECTION", "COLLECTION"} & src_categories and dst_anchors:
                return True

            if (
                {"ENTITY_TEXT_SPAN", "TEXT_SPAN", "ENTITY_CLAIM", "CLAIM"} & src_categories
                and {"ENTITY_DOCUMENT", "ARTIFACT_DOCUMENT", "DOCUMENT"} & dst_categories
            ):
                src_artifacts = {anchor.artifact_id for anchor in src_anchors}
                dst_artifacts = {anchor.artifact_id for anchor in dst_anchors}
                if src_artifacts & dst_artifacts:
                    return True

        # 1. KG0 path
        if (
            not self.config.defer_kg_structure_until_post_phase5
            and src_binding.kg0_node_id
            and dst_binding.kg0_node_id
        ):
            relationship_types = _resolved_edge_relationship_types(
                self.config, edge.rel
            )
            paths = []
            if relationship_types != []:
                paths = self.index.neo4j.find_paths(
                    src_binding.kg0_node_id,
                    dst_binding.kg0_node_id,
                    max_hops=self.config.max_hops,
                    relationship_types=relationship_types,
                )
            if paths:
                return True

        # 2. Link hypothesis
        if (
            src_binding.entity_hypothesis_id
            and dst_binding.entity_hypothesis_id
        ):
            key = (
                src_binding.entity_hypothesis_id,
                dst_binding.entity_hypothesis_id,
            )
            if key in self._link_index:
                return True

        # 3. Structural co-occurrence
        if src_binding.anchor_id and dst_binding.anchor_id:
            if src_binding.anchor_id == dst_binding.anchor_id:
                return True
            src_a = self._anchor_lookup.get(src_binding.anchor_id)
            dst_a = self._anchor_lookup.get(dst_binding.anchor_id)
            if src_a and dst_a:
                if src_a.artifact_id == dst_a.artifact_id:
                    if (
                        src_a.same_page(dst_a)
                        or src_a.contains(dst_a)
                        or dst_a.contains(src_a)
                    ):
                        return True
                    # Weak co-occurrence satisfies only soft edges
                    return not edge.hard

        return False

    def _binding_category_aliases(
        self,
        binding: SubgraphBinding,
    ) -> Set[str]:
        categories: Set[str] = set()
        if binding.mention_id:
            mention = self._mention_lookup.get(binding.mention_id)
            if mention:
                categories |= self._category_aliases(mention.category)
        if binding.entity_hypothesis_id:
            entity = self._entity_lookup.get(binding.entity_hypothesis_id)
            if entity:
                categories |= self._category_aliases(entity.category)
        return categories

    # ----------------------------------------------------------------
    # Scoring
    # ----------------------------------------------------------------

    def _score_subgraph(self, sg: Subgraph, gs: GraphSpec) -> float:
        """Score a subgraph by constraint satisfaction."""
        score = 0.0

        for edge in gs.hard_edges:
            ek = f"{edge.src}-[{edge.rel}]->{edge.dst}"
            if sg.edge_satisfactions.get(ek, False):
                score += self.config.hard_constraint_weight

        for edge in gs.soft_edges:
            ek = f"{edge.src}-[{edge.rel}]->{edge.dst}"
            if sg.edge_satisfactions.get(ek, False):
                score += self.config.soft_constraint_weight

        for var in gs.vars:
            b = sg.bindings.get(var.var)
            if b and b.bound:
                score += 1.0
                if b.quality == EvidenceQuality.GROUNDED:
                    score += 0.5

        artifact_ids = self._collect_subgraph_artifact_ids(sg)
        if len(artifact_ids) > 1:
            score += self.config.cross_artifact_bridge_weight * (
                len(artifact_ids) - 1
            )

        score += self._actor_chain_coherence_bonus(sg)

        temporal = self._evaluate_temporal_constraints(sg, gs)
        for satisfied in temporal.values():
            if satisfied is True:
                score += self.config.temporal_coherence_weight
            elif satisfied is False:
                score -= self.config.temporal_coherence_weight * 2.0

        score += self._grounded_binding_tiebreak(sg)
        score += self._artifact_locality_tiebreak(sg)
        score += self._preferred_chain_grounding_bonus(sg)
        score += self._actor_anchor_specificity_bonus(sg)
        score += self._binding_surface_specificity_tiebreak(sg)

        return score

    def _grounded_binding_tiebreak(self, sg: Subgraph) -> float:
        bound = [binding for binding in sg.bindings.values() if binding.bound]
        if not bound:
            return 0.0
        grounded = sum(
            1
            for binding in bound
            if binding.quality == EvidenceQuality.GROUNDED
        )
        return 0.18 * (grounded / len(bound))

    def _artifact_locality_tiebreak(self, sg: Subgraph) -> float:
        artifact_ids = list(self._collect_subgraph_artifact_ids(sg))
        if not artifact_ids:
            return 0.0
        counts: Dict[str, int] = defaultdict(int)
        for binding in sg.bindings.values():
            if not binding.bound:
                continue
            for artifact_id in self._binding_artifact_ids(binding):
                if artifact_id:
                    counts[artifact_id] += 1
        if not counts:
            return 0.0
        dominant_ratio = max(counts.values()) / max(1, sum(counts.values()))
        return 0.12 * dominant_ratio

    def _preferred_chain_grounding_bonus(self, sg: Subgraph) -> float:
        bonus = 0.0
        for var_name, weight in (
            ("A", 0.12),
            ("CL", 0.10),
            ("POL", 0.10),
            ("TS", 0.10),
            ("RF", 0.08),
            ("RESP", 0.08),
            ("P", 0.06),
            ("R", 0.06),
        ):
            binding = sg.bindings.get(var_name)
            if not binding or not binding.bound:
                continue
            if binding.quality == EvidenceQuality.GROUNDED:
                bonus += weight
            elif binding.quality == EvidenceQuality.INFERRED:
                bonus -= 0.55 * weight
            elif binding.quality == EvidenceQuality.AMBIGUOUS:
                bonus -= 0.75 * weight
        return bonus

    def _actor_anchor_specificity_bonus(self, sg: Subgraph) -> float:
        evidence_body_anchor_ids: Set[str] = set()
        for var_name in ("A", "CL", "TS", "POL", "S", "RF", "EV"):
            binding = sg.bindings.get(var_name)
            for anchor in self._resolve_binding_anchors(binding):
                if not self._anchor_is_header_like_for_discovery(anchor):
                    evidence_body_anchor_ids.add(anchor.anchor_id)

        if not evidence_body_anchor_ids:
            return 0.0

        actor_anchor_ids: Dict[str, Set[str]] = {}
        actor_body_anchor_ids: Dict[str, Set[str]] = {}
        for var_name in ("P", "R", "O", "RESP"):
            anchors = self._resolve_binding_anchors(sg.bindings.get(var_name))
            actor_anchor_ids[var_name] = {anchor.anchor_id for anchor in anchors}
            actor_body_anchor_ids[var_name] = {
                anchor.anchor_id
                for anchor in anchors
                if not self._anchor_is_header_like_for_discovery(anchor)
            }

        bonus = 0.0
        for var_name, weight, penalty in (
            ("P", 0.42, -0.28),
            ("R", 0.36, -0.12),
            ("O", 0.28, -0.08),
        ):
            body_anchor_ids = actor_body_anchor_ids[var_name]
            all_anchor_ids = actor_anchor_ids[var_name]
            if body_anchor_ids & evidence_body_anchor_ids:
                bonus += weight
            elif all_anchor_ids and not body_anchor_ids:
                bonus += penalty

        person_anchors = self._resolve_binding_anchors(sg.bindings.get("P"))
        if any(
            self._anchor_has_signature_context_for_discovery(anchor)
            for anchor in person_anchors
        ):
            bonus += 0.24

        if actor_body_anchor_ids["P"] & actor_body_anchor_ids["R"] & evidence_body_anchor_ids:
            bonus += 0.55
        if actor_body_anchor_ids["R"] & actor_body_anchor_ids["O"] & evidence_body_anchor_ids:
            bonus += 0.30
        if actor_body_anchor_ids["P"] & actor_body_anchor_ids["O"] & evidence_body_anchor_ids:
            bonus += 0.20

        return bonus

    def _binding_surface_specificity_tiebreak(self, sg: Subgraph) -> float:
        bonus = 0.0
        for mention in self._resolve_binding_mentions(sg.bindings.get("R")):
            bonus = max(
                bonus,
                0.10 * self._role_surface_specificity_bonus(
                    mention.surface
                ),
            )

        org_bonus = 0.0
        for mention in self._resolve_binding_mentions(sg.bindings.get("O")):
            org_bonus = max(
                org_bonus,
                0.06 * self._organization_surface_specificity_bonus(
                    mention.surface
                ),
            )
        bonus += org_bonus

        person_bonus = 0.0
        for mention in self._resolve_binding_mentions(sg.bindings.get("P")):
            person_bonus = max(
                person_bonus,
                0.04 * self._person_surface_specificity_bonus(
                    mention.surface
                ),
            )
        bonus += person_bonus

        return bonus

    @staticmethod
    def _anchor_is_header_like_for_discovery(anchor: Anchor) -> bool:
        component_type = str(anchor.metadata.get("component_type", "") or "").lower()
        if component_type in {"header", "subject", "title"}:
            return True
        address = str(anchor.address or "").lower()
        return ".header" in address or ".subject" in address or ".title" in address

    def _evaluate_temporal_constraints(
        self, sg: Subgraph, gs: GraphSpec
    ) -> Dict[str, Optional[bool]]:
        """Evaluate temporal constraints.  None = not evaluable."""
        results: Dict[str, Optional[bool]] = {}

        for tc in gs.temporal_constraints:
            tc_kind = (tc.kind or "ORDER").upper()
            if tc_kind == "CONCURRENT":
                tc_key = f"temporal:{tc_kind}({','.join(tc.vars)})"
                results[tc_key] = None
                continue

            tc_key = f"temporal:{tc_kind}({tc.before},{tc.after})"
            before_b = sg.bindings.get(tc.before)
            after_b = sg.bindings.get(tc.after)
            if not (
                before_b
                and before_b.bound
                and after_b
                and after_b.bound
            ):
                results[tc_key] = None
                continue

            d_before = self._get_binding_date(before_b)
            d_after = self._get_binding_date(after_b)
            if not d_before or not d_after:
                results[tc_key] = None
                continue

            if tc_kind == "ORDER":
                results[tc_key] = d_before <= d_after
            elif tc_kind == "WITHIN" and tc.window_days is not None:
                try:
                    dt1 = datetime.fromisoformat(
                        d_before.replace("Z", "+00:00")
                    )
                    dt2 = datetime.fromisoformat(
                        d_after.replace("Z", "+00:00")
                    )
                    results[tc_key] = (
                        abs((dt2 - dt1).days) <= tc.window_days
                    )
                except (ValueError, TypeError):
                    results[tc_key] = None
            else:
                results[tc_key] = None

        return results

    def _get_binding_date(self, binding: SubgraphBinding) -> str:
        """Date string from anchor metadata, or empty."""
        if binding.anchor_id:
            a = self._anchor_lookup.get(binding.anchor_id)
            if a:
                return a.metadata.get("artifact_date", "")
        return ""

    # ----------------------------------------------------------------
    # Coverage
    # ----------------------------------------------------------------

    def _filter_unbound_hard_var_subgraphs(
        self,
        subgraphs: List[Subgraph],
        gs: GraphSpec,
    ) -> Tuple[List[Subgraph], int, Dict[str, int]]:
        """Drop subgraphs that would violate hard-variable soundness."""
        hard_vars = gs.hard_vars
        if not hard_vars:
            return subgraphs, 0, {}

        filtered: List[Subgraph] = []
        rejected_by_var: Dict[str, int] = {}
        for sg in subgraphs:
            missing_vars: List[str] = []
            for var in hard_vars:
                binding = sg.bindings.get(var.var)
                if not (binding and binding.bound):
                    missing_vars.append(var.var)
            if not missing_vars:
                filtered.append(sg)
                continue
            for var_name in missing_vars:
                rejected_by_var[var_name] = rejected_by_var.get(var_name, 0) + 1

        return filtered, len(subgraphs) - len(filtered), rejected_by_var

    def _compute_hard_coverage(
        self, sg: Subgraph, gs: GraphSpec
    ) -> float:
        hard_edges = gs.hard_edges
        hard_vars = gs.hard_vars

        if not hard_edges and not hard_vars:
            return 1.0

        var_cov = 1.0
        if hard_vars:
            bound = sum(
                1
                for v in hard_vars
                if sg.bindings.get(v.var)
                and sg.bindings[v.var].bound
            )
            var_cov = bound / len(hard_vars)

        if not hard_edges:
            return var_cov

        sat = sum(
            1
            for e in hard_edges
            if sg.edge_satisfactions.get(
                f"{e.src}-[{e.rel}]->{e.dst}", False
            )
        )
        edge_cov = sat / len(hard_edges)
        return min(edge_cov, var_cov)

    def _compute_soft_coverage(
        self, sg: Subgraph, gs: GraphSpec
    ) -> float:
        soft_edges = gs.soft_edges
        if not soft_edges:
            return 1.0
        sat = sum(
            1
            for e in soft_edges
            if sg.edge_satisfactions.get(
                f"{e.src}-[{e.rel}]->{e.dst}", False
            )
        )
        return sat / len(soft_edges)

    # ----------------------------------------------------------------
    # Coherence score  (normalized 0–1)
    # ----------------------------------------------------------------

    def _compute_coherence_score(
        self, sg: Subgraph, gs: GraphSpec
    ) -> float:
        """
        Weighted combination:
            hard coverage            40 %
            soft coverage            15 %
            average binding quality  20 %
            temporal consistency     10 %
            cross-artifact bridging  15 %
        """
        hard_cov = sg.hard_coverage or 0.0
        soft_cov = sg.soft_coverage or 0.0

        quality_map = {
            EvidenceQuality.GROUNDED: 1.0,
            EvidenceQuality.INFERRED: 0.6,
            EvidenceQuality.AMBIGUOUS: 0.2,
        }
        binding_scores = []
        for var in gs.vars:
            b = sg.bindings.get(var.var)
            if b and b.bound:
                binding_scores.append(quality_map.get(b.quality, 0.2))
            else:
                binding_scores.append(0.0)
        avg_q = (
            sum(binding_scores) / len(binding_scores)
            if binding_scores
            else 0.0
        )

        temporal = self._evaluate_temporal_constraints(sg, gs)
        evaluable = [v for v in temporal.values() if v is not None]
        temp_score = (
            (sum(1.0 for v in evaluable if v) / len(evaluable))
            if evaluable
            else 1.0
        )

        n_art = len(self._collect_subgraph_artifact_ids(sg))
        cross_score = min(1.0, (n_art - 1) / 2.0) if n_art > 0 else 0.0

        coherence = (
            0.40 * hard_cov
            + 0.15 * soft_cov
            + 0.20 * avg_q
            + 0.10 * temp_score
            + 0.15 * cross_score
        )
        return round(min(1.0, max(0.0, coherence)), 3)

    # ----------------------------------------------------------------
    # Diversity reranking  (MMR-style)
    # ----------------------------------------------------------------

    def _diversity_rerank(
        self, subgraphs: List[Subgraph]
    ) -> List[Subgraph]:
        """
        At each step select the subgraph maximising
        (1 − λ) · norm_score  +  λ · novelty
        where novelty is the fraction of KG0 node IDs not yet
        covered, and λ = config.diversity_bonus.
        """
        return _diversity_rerank_subgraphs(
            subgraphs,
            diversity_bonus=self.config.diversity_bonus,
        )

    # ----------------------------------------------------------------
    # Frame witness assembly
    # ----------------------------------------------------------------

    def _assemble_frame_witness(
        self,
        sg: Subgraph,
        gs: GraphSpec,
        intent: IntentObject,
    ) -> Dict[str, Any]:
        """
        Build the ``witness`` dict for one subgraph, matching the
        frame output schema.
        """
        anchor_ids: List[str] = []
        mention_ids: List[str] = []
        artifact_ids_set: Set[str] = set()
        skeleton_bindings: Dict[str, Optional[str]] = {}

        for var in gs.vars:
            b = sg.bindings.get(var.var)
            if not b or not b.bound:
                skeleton_bindings[var.var] = None
                continue

            skeleton_bindings[var.var] = b.kg0_node_id

            for a in self._resolve_binding_anchors(b):
                if a.anchor_id not in anchor_ids:
                    anchor_ids.append(a.anchor_id)
                artifact_ids_set.add(a.artifact_id)

            for m in self._resolve_binding_mentions(b):
                if m.mention_id not in mention_ids:
                    mention_ids.append(m.mention_id)

        temporal = self._evaluate_temporal_constraints(sg, gs)
        evaluable = [v for v in temporal.values() if v is not None]
        temporal_consistent = all(evaluable) if evaluable else True

        temporal_span = self._compute_temporal_span(sg)
        artifact_coverage = self._compute_artifact_coverage(
            sg, artifact_ids_set
        )
        chain = self._build_evidence_chain_summary(sg, gs)
        description = self._generate_witness_description(sg, gs)

        return {
            "witness_id": f"witness-{sg.subgraph_id}",
            "subgraph_id": sg.subgraph_id,
            "description": description,
            "anchor_ids": anchor_ids,
            "mention_ids": mention_ids,
            "artifact_ids": sorted(artifact_ids_set),
            "skeleton_bindings": skeleton_bindings,
            "coherence_score": getattr(sg, "coherence_score", 0.0),
            "temporal_consistency": temporal_consistent,
            "temporal_span": temporal_span,
            "artifact_coverage": artifact_coverage,
            "evidence_chain_summary": chain,
        }

    def _frame_witness_to_witness_objects(
        self,
        sg: Subgraph,
        gs: GraphSpec,
        intent: IntentObject,
    ) -> List[Witness]:
        """Convert subgraph bindings into proper Witness objects.

        Each bound variable that has both an anchor and a mention gets
        a Witness object linking the intent graph variable to the
        primary-source span.  This satisfies Proposition 4 (witness
        completeness of ALIGN decisions).
        """
        witnesses: List[Witness] = []
        for var in gs.vars:
            b = sg.bindings.get(var.var)
            if not b or not b.bound:
                continue
            anchors = self._resolve_binding_anchors(b)
            mentions = self._resolve_binding_mentions(b)
            if not anchors or not mentions:
                continue
            anchor = anchors[0]
            mention = mentions[0]
            w = Witness(
                witness_id=Witness.generate_id(),
                phase=Phase.PHASE_5_SUBGRAPH,
                intent_element=IntentElementRef(
                    element_type="graph_var",
                    element_id=var.var,
                    element_detail={
                        "type": var.type,
                        "role": var.role or "",
                        "hint": var.hint or "",
                    },
                ),
                anchor=anchor,
                mention=mention,
                score=sg.score,
                quality=b.quality,
                justification=(
                    f"Subgraph {sg.subgraph_id} binds var {var.var} "
                    f"({var.type}) to mention '{mention.surface}' "
                    f"at anchor {anchor.address}"
                ),
            )
            w.compute_content_hash()
            witnesses.append(w)
        return witnesses

    def _resolve_binding_anchors(
        self, binding: Optional[SubgraphBinding]
    ) -> List[Anchor]:
        """All anchors backing a binding, via direct ref and via
        entity hypothesis mentions."""
        anchors: List[Anchor] = []
        if binding is None:
            return anchors
        if binding.anchor_id:
            a = self._anchor_lookup.get(binding.anchor_id)
            if a:
                anchors.append(a)
        if binding.entity_hypothesis_id:
            eh = self._entity_lookup.get(binding.entity_hypothesis_id)
            if eh:
                for m in eh.mentions:
                    a = self._anchor_lookup.get(m.anchor_id)
                    if a and a not in anchors:
                        anchors.append(a)
        return anchors

    def _resolve_binding_mentions(
        self, binding: Optional[SubgraphBinding]
    ) -> List[Mention]:
        """All mentions backing a binding."""
        mentions: List[Mention] = []
        if binding is None:
            return mentions
        if binding.mention_id:
            m = self._mention_lookup.get(binding.mention_id)
            if m:
                mentions.append(m)
        if binding.entity_hypothesis_id:
            eh = self._entity_lookup.get(binding.entity_hypothesis_id)
            if eh:
                for m in eh.mentions:
                    if m not in mentions:
                        mentions.append(m)
        return mentions

    def _binding_is_header_only_for_discovery(
        self,
        binding: Optional[SubgraphBinding],
    ) -> bool:
        if binding is None or not binding.bound:
            return False
        anchors = self._resolve_binding_anchors(binding)
        if not anchors:
            return False
        return all(self._anchor_is_header_like_for_discovery(anchor) for anchor in anchors)

    def _binding_is_secondary_action_for_discovery(
        self,
        binding: Optional[SubgraphBinding],
    ) -> bool:
        if binding is None or not binding.bound:
            return False
        mentions = self._resolve_binding_mentions(binding)
        if not mentions:
            return binding.quality != EvidenceQuality.GROUNDED

        best_signal = float("-inf")
        for mention in mentions:
            anchor = self._anchor_lookup.get(mention.anchor_id)
            signal = self._action_item_context_bonus(mention, anchor)
            signal += 0.5 * self._action_item_specificity_bonus(mention.surface)
            if binding.quality == EvidenceQuality.GROUNDED:
                signal += 0.08
            best_signal = max(best_signal, signal)

        return best_signal < 0.12

    def _compute_temporal_span(self, sg: Subgraph) -> Dict[str, str]:
        """Earliest/latest dates across all bound anchors."""
        dates: List[str] = []
        for b in sg.bindings.values():
            if b.bound:
                for a in self._resolve_binding_anchors(b):
                    d = a.metadata.get("artifact_date", "")
                    if d:
                        dates.append(d)
        if not dates:
            return {"earliest": "", "latest": ""}
        dates.sort()
        return {"earliest": dates[0], "latest": dates[-1]}

    def _compute_artifact_coverage(
        self, sg: Subgraph, artifact_ids: Set[str]
    ) -> List[Dict[str, Any]]:
        """Per-artifact evidence chain count and weight fraction."""
        counts: Dict[str, int] = defaultdict(int)
        for b in sg.bindings.values():
            if b.bound:
                for a in self._resolve_binding_anchors(b):
                    counts[a.artifact_id] += 1
        total = sum(counts.values()) or 1
        return [
            {
                "artifact_id": aid,
                "evidence_chain_count": counts.get(aid, 0),
                "evidence_weight_fraction": round(
                    counts.get(aid, 0) / total, 2
                ),
            }
            for aid in sorted(artifact_ids)
        ]

    def _build_evidence_chain_summary(
        self, sg: Subgraph, gs: GraphSpec
    ) -> List[Dict[str, Any]]:
        """Chronologically ordered evidence chain steps."""
        steps: List[Dict[str, Any]] = []

        for var in gs.vars:
            b = sg.bindings.get(var.var)
            if not b or not b.bound:
                continue
            anchors = self._resolve_binding_anchors(b)
            mentions = self._resolve_binding_mentions(b)
            if not anchors:
                continue

            primary_anchor = anchors[0]
            primary_mention = mentions[0] if mentions else None
            date = primary_anchor.metadata.get("artifact_date", "")
            entity_name = self._binding_display_name(b)

            raw = primary_anchor.raw_text
            if len(raw) > 120:
                raw = raw[:117] + "..."

            role_desc = var.role or var.type or var.var

            steps.append(
                {
                    "step": 0,
                    "label": self._step_label(var, b),
                    "date": date,
                    "anchor_id": primary_anchor.anchor_id,
                    "mention_id": (
                        primary_mention.mention_id
                        if primary_mention
                        else None
                    ),
                    "artifact_id": primary_anchor.artifact_id,
                    "summary": (
                        f"'{entity_name}' identified as "
                        f"{role_desc} in "
                        f"{primary_anchor.metadata.get('artifact_family', 'document')} "
                        f"{primary_anchor.artifact_id}"
                        f"{' (' + date + ')' if date else ''}: "
                        f'"{raw}"'
                    ),
                }
            )

        steps.sort(key=lambda s: s.get("date") or "9999-99-99")
        for i, step in enumerate(steps):
            step["step"] = i + 1
        return steps

    def _step_label(
        self, var: GraphVar, binding: SubgraphBinding
    ) -> str:
        """Short human-readable label for an evidence chain step."""
        t = (var.type or "").lower()
        r = (var.role or "").lower()

        for key, label in [
            ("person", "Actor Identified"),
            ("organization", "Organization Identified"),
            ("decision", "Decision Documented"),
            ("document", "Source Document Established"),
            ("email", "Communication Identified"),
            ("event", "Event Identified"),
            ("topic", "Topic Identified"),
            ("strategy", "Strategy Documented"),
            ("risk", "Risk Finding Surfaced"),
            ("claim", "Claim Documented"),
            ("policy", "Policy Identified"),
            ("location", "Location Identified"),
        ]:
            if key in t:
                return label

        for key, label in [
            ("author", "Author Identified"),
            ("recipient", "Recipient Identified"),
            ("recommendation", "Recommendation Documented"),
            ("rationale", "Rationale Surfaced"),
            ("decision", "Decision Documented"),
            ("source", "Source Established"),
        ]:
            if key in r:
                return label

        return f"Variable '{var.var}' Bound"

    def _binding_display_name(self, binding: SubgraphBinding) -> str:
        """Best human-readable name for a binding."""
        if binding.entity_hypothesis_id:
            eh = self._entity_lookup.get(binding.entity_hypothesis_id)
            if eh:
                return eh.canonical_name
        if binding.mention_id:
            m = self._mention_lookup.get(binding.mention_id)
            if m:
                return m.surface
        return binding.kg0_node_id or "Unknown"

    def _generate_witness_description(
        self, sg: Subgraph, gs: GraphSpec
    ) -> str:
        """One-sentence description of the subgraph witness."""
        parts: List[str] = []
        for var in gs.vars:
            b = sg.bindings.get(var.var)
            if not b or not b.bound:
                continue
            name = self._binding_display_name(b)
            role = var.role or var.type or var.var
            parts.append(f"{name} ({role})")

        n_art = len(self._collect_subgraph_artifact_ids(sg))
        if parts:
            return (
                f"Subgraph connecting {', '.join(parts)}, "
                f"drawn from {n_art} artifact(s)."
            )
        return "Subgraph with no named bindings."

    # ----------------------------------------------------------------
    # Subgraph snapshot
    # ----------------------------------------------------------------

    def _build_snapshot(
        self, sg: Subgraph, gs: GraphSpec
    ) -> Dict[str, Any]:
        """
        KG0 node/edge details for the subgraph.  Queries Neo4j for
        each bound node's labels, display name, and properties, then
        discovers the actual edges between them.
        """
        nodes: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()

        for var in gs.vars:
            b = sg.bindings.get(var.var)
            if not b or not b.bound:
                continue
            if b.kg0_node_id:
                nid = b.kg0_node_id
                if nid in seen_ids:
                    continue
                seen_ids.add(nid)

                details = self._fetch_node_details(nid)
                nodes.append(
                    {
                        "node_id": nid,
                        "labels": details.get("labels", []),
                        "display_name": details.get("display_name", nid),
                        "properties": details.get("properties", {}),
                        "assigned_to": var.var,
                        "inferred": (
                            b.quality == EvidenceQuality.INFERRED
                            and b.anchor_id is None
                        ),
                    }
                )
                continue

            inferred_node = self._build_inferred_snapshot_node(
                sg.subgraph_id,
                var,
                b,
            )
            inferred_node_id = inferred_node["node_id"]
            if inferred_node_id in seen_ids:
                continue
            seen_ids.add(inferred_node_id)
            nodes.append(inferred_node)

        edges = self._discover_snapshot_edges(sg, gs, seen_ids)

        # Contextual nodes from discovered edges
        for edge in edges:
            for nid in (edge["source"], edge["target"]):
                if nid not in seen_ids:
                    seen_ids.add(nid)
                    details = self._fetch_node_details(nid)
                    nodes.append(
                        {
                            "node_id": nid,
                            "labels": details.get("labels", []),
                            "display_name": details.get(
                                "display_name", nid
                            ),
                            "properties": details.get("properties", {}),
                            "assigned_to": None,
                            "inferred": False,
                        }
                    )

        return {"nodes": nodes, "edges": edges}

    def _fetch_node_details(self, node_id: str) -> Dict[str, Any]:
        """Fetch KG0 node details; falls back to entity hypothesis
        data or a stub."""
        try:
            result = self.index.neo4j.get_node(node_id)
            if result:
                labels = result.pop("_labels", [])
                result.pop("_id", None)
                name = (
                    result.get("display_name")
                    or result.get("name")
                    or result.get("title")
                    or result.get("subject")
                    or result.get("summary")
                    or result.get("text")
                    or result.get("description")
                    or result.get("citationText")
                    or result.get("context")
                    or result.get("caption")
                    or result.get("notes")
                    or result.get("abbvName")
                    or result.get("fullForm")
                    or result.get("contextOfDate")
                    or result.get("identifier")
                    or result.get("recordId")
                    or result.get("sourceFileName")
                    or result.get("witness")
                    or result.get("witnessContext")
                    or result.get("rationale")
                    or node_id
                )
                props = {
                    k: v
                    for k, v in result.items()
                    if not k.startswith("_")
                }
                return {
                    "labels": labels,
                    "display_name": name,
                    "properties": props,
                }
        except _NEO4J_QUERY_ERRORS as exc:
            logger.debug(
                "Failed to fetch KG0 node %s (%s): %s",
                node_id,
                type(exc).__name__,
                exc,
            )

        for eh in self._entity_lookup.values():
            if node_id in eh.kg0_entity_ids:
                return {
                    "labels": [eh.category],
                    "display_name": eh.canonical_name,
                    "properties": {"category": eh.category},
                }

        return {
            "labels": [],
            "display_name": node_id,
            "properties": {},
        }

    def _recover_artifact_node_id_for_binding(
        self,
        binding: SubgraphBinding,
        var: Optional[GraphVar],
    ) -> str:
        if var is None:
            return ""
        compatible_categories = self._var_compatible_categories(var.type)
        if not (
            {"ENTITY_DOCUMENT", "DOCUMENT", "ARTIFACT_DOCUMENT"}
            & compatible_categories
        ):
            return ""

        anchors = self._resolve_binding_anchors(binding)
        if not anchors and binding.anchor_id:
            anchor = self._anchor_lookup.get(binding.anchor_id)
            if anchor is not None:
                anchors = [anchor]
        if not anchors:
            return ""

        artifact_id = str(anchors[0].artifact_id or "").strip()
        if not artifact_id:
            return ""
        try:
            artifact_node = self.index.neo4j.get_node(artifact_id)
        except _NEO4J_QUERY_ERRORS as exc:
            logger.debug(
                "get_node failed for artifact %s (%s): %s",
                artifact_id,
                type(exc).__name__,
                exc,
            )
            return ""
        if not artifact_node:
            return ""
        return str(artifact_node.get("_id", "") or "").strip()

    def _build_inferred_snapshot_node(
        self,
        subgraph_id: str,
        var: GraphVar,
        binding: SubgraphBinding,
    ) -> Dict[str, Any]:
        node_id = f"inferred::{subgraph_id}::{var.var}"
        labels: List[str] = []
        display_name = ""
        properties: Dict[str, Any] = {
            "var_name": var.var,
            "var_type": var.type,
        }

        entity = (
            self._entity_lookup.get(binding.entity_hypothesis_id)
            if binding.entity_hypothesis_id
            else None
        )
        mention = (
            self._mention_lookup.get(binding.mention_id)
            if binding.mention_id
            else None
        )
        anchors = self._resolve_binding_anchors(binding)
        anchor = anchors[0] if anchors else None

        if entity is not None:
            labels = [str(entity.category or "").strip() or var.type]
            display_name = str(entity.canonical_name or "").strip()
            properties.update(
                {
                    "entity_hypothesis_id": entity.hypothesis_id,
                    "confidence": entity.confidence,
                    "category": entity.category,
                }
            )
        elif mention is not None:
            labels = [str(mention.category or "").strip() or var.type]
            display_name = str(
                mention.surface or mention.normalized or ""
            ).strip()
            properties.update(
                {
                    "mention_id": mention.mention_id,
                    "category": mention.category,
                    "surface": mention.surface,
                    "normalized": mention.normalized,
                    "confidence": mention.confidence,
                }
            )
        else:
            labels = [var.type]

        if anchor is not None:
            properties.update(
                {
                    "anchor_id": anchor.anchor_id,
                    "artifact_id": anchor.artifact_id,
                }
            )
            if not display_name:
                display_name = str(anchor.raw_text or "").strip()

        if not display_name:
            display_name = (
                str(var.role or "").strip()
                or str(var.hint or "").strip()
                or var.var
            )

        return {
            "node_id": node_id,
            "labels": labels,
            "display_name": display_name,
            "properties": properties,
            "assigned_to": var.var,
            "inferred": True,
        }

    def _snapshot_node_id_for_binding(
        self,
        subgraph_id: str,
        var: GraphVar,
        binding: SubgraphBinding,
    ) -> str:
        if binding.kg0_node_id:
            return binding.kg0_node_id
        return f"inferred::{subgraph_id}::{var.var}"

    def _discover_snapshot_edges(
        self,
        sg: Subgraph,
        gs: GraphSpec,
        node_ids: Set[str],
    ) -> List[Dict[str, Any]]:
        """Query KG0 for actual edges between bound nodes."""
        edges: List[Dict[str, Any]] = []
        seen_edge_keys: Set[Tuple[str, str, str]] = set()
        counter = 0

        for edge_spec in gs.edges:
            ek = f"{edge_spec.src}-[{edge_spec.rel}]->{edge_spec.dst}"
            if not sg.edge_satisfactions.get(ek, False):
                continue

            src_b = sg.bindings.get(edge_spec.src)
            dst_b = sg.bindings.get(edge_spec.dst)
            src_var = gs.var_by_name(edge_spec.src)
            dst_var = gs.var_by_name(edge_spec.dst)
            if not (src_b and dst_b and src_var and dst_var):
                continue

            src_snapshot_id = self._snapshot_node_id_for_binding(
                sg.subgraph_id,
                src_var,
                src_b,
            )
            dst_snapshot_id = self._snapshot_node_id_for_binding(
                sg.subgraph_id,
                dst_var,
                dst_b,
            )

            if not (src_b.kg0_node_id and dst_b.kg0_node_id):
                edge_key = (
                    src_snapshot_id,
                    edge_spec.rel,
                    dst_snapshot_id,
                )
                if edge_key in seen_edge_keys:
                    continue
                seen_edge_keys.add(edge_key)
                counter += 1
                edges.append(
                    {
                        "edge_id": f"inferred-edge-{sg.subgraph_id}-{counter}",
                        "type": edge_spec.rel,
                        "source": src_snapshot_id,
                        "target": dst_snapshot_id,
                        "inferred": True,
                    }
                )
                continue

            resolved_rel_types = _resolved_edge_relationship_types(
                self.config, edge_spec.rel
            )
            if resolved_rel_types == []:
                continue
            if resolved_rel_types and hasattr(
                self.index.neo4j, "relationship_type_exists"
            ):
                resolved_rel_types = [
                    rel_type
                    for rel_type in resolved_rel_types
                    if self.index.neo4j.relationship_type_exists(rel_type)
                ]
                if not resolved_rel_types:
                    continue
            rel_filter = (
                f":{'|'.join(resolved_rel_types)}"
                if resolved_rel_types
                else ""
            )
            cypher = (
                f"MATCH (a)-[r{rel_filter}]-(b) "
                f"WHERE {_node_match_condition(self.index.neo4j, 'a', 'src')} "
                f"AND {_node_match_condition(self.index.neo4j, 'b', 'dst')} "
                f"RETURN type(r) AS type, "
                f"elementId(r) AS eid, "
                f"{_node_id_expression(self.index.neo4j, 'a', as_name='source')}, "
                f"{_node_id_expression(self.index.neo4j, 'b', as_name='target')} "
                f"LIMIT 5"
            )
            try:
                rows = self.index.neo4j.execute_cypher(
                    cypher,
                    {
                        "src": src_b.kg0_node_id,
                        "dst": dst_b.kg0_node_id,
                    },
                )
                if not rows:
                    edge_key = (
                        src_snapshot_id,
                        edge_spec.rel,
                        dst_snapshot_id,
                    )
                    if edge_key in seen_edge_keys:
                        continue
                    seen_edge_keys.add(edge_key)
                    counter += 1
                    edges.append(
                        {
                            "edge_id": f"inferred-edge-{sg.subgraph_id}-{counter}",
                            "type": edge_spec.rel,
                            "source": src_snapshot_id,
                            "target": dst_snapshot_id,
                            "inferred": True,
                        }
                    )
                    continue
                for row in rows:
                    edge_key = (
                        str(row.get("source", src_b.kg0_node_id)),
                        str(row.get("type", edge_spec.rel)),
                        str(row.get("target", dst_b.kg0_node_id)),
                    )
                    if edge_key in seen_edge_keys:
                        continue
                    seen_edge_keys.add(edge_key)
                    counter += 1
                    edges.append(
                        {
                            "edge_id": row.get(
                                "eid",
                                f"edge-{sg.subgraph_id}-{counter}",
                            ),
                            "type": row.get("type", edge_spec.rel),
                            "source": row.get(
                                "source", src_b.kg0_node_id
                            ),
                            "target": row.get(
                                "target", dst_b.kg0_node_id
                            ),
                            "inferred": False,
                        }
                    )
            except _NEO4J_QUERY_ERRORS as exc:
                logger.debug(
                    "Snapshot edge Cypher failed for %s -[%s]-> %s (%s): %s",
                    src_snapshot_id,
                    edge_spec.rel,
                    dst_snapshot_id,
                    type(exc).__name__,
                    exc,
                )
                edge_key = (
                    src_snapshot_id,
                    edge_spec.rel,
                    dst_snapshot_id,
                )
                if edge_key in seen_edge_keys:
                    continue
                seen_edge_keys.add(edge_key)
                counter += 1
                edges.append(
                    {
                        "edge_id": f"edge-{sg.subgraph_id}-{counter}",
                        "type": edge_spec.rel,
                        "source": src_snapshot_id,
                        "target": dst_snapshot_id,
                        "inferred": True,
                    }
                )

        return edges

    # ----------------------------------------------------------------
    # Shared helpers
    # ----------------------------------------------------------------

    def _collect_subgraph_artifact_ids(
        self, sg: Subgraph
    ) -> Set[str]:
        """All artifact IDs referenced by a subgraph's bindings,
        including those reachable through entity hypotheses."""
        aids: Set[str] = set()
        for b in sg.bindings.values():
            if not b.bound:
                continue
            for a in self._resolve_binding_anchors(b):
                aids.add(a.artifact_id)
        return aids

    def _collect_subgraph_kg0_nodes(
        self, sg: Subgraph
    ) -> Set[str]:
        """All KG0 node IDs from a subgraph's bindings."""
        return _collect_subgraph_kg0_nodes(sg)

    def _binding_artifact_ids(
        self,
        binding: Optional[SubgraphBinding],
    ) -> Set[str]:
        if binding is None or not binding.bound:
            return set()
        return {
            anchor.artifact_id
            for anchor in self._resolve_binding_anchors(binding)
        }

    def _actor_chain_coherence_bonus(
        self,
        sg: Subgraph,
    ) -> float:
        """
        Prefer actor/role/org bindings that stay close to the main
        document/claim evidence chain instead of drifting to unrelated
        high-confidence entities from other artifacts.
        """
        evidence_artifacts: Set[str] = set()
        for var_name in ("A", "CL", "TS", "POL", "S", "RF", "EV"):
            evidence_artifacts |= self._binding_artifact_ids(
                sg.bindings.get(var_name)
            )

        if not evidence_artifacts:
            return 0.0

        bonus = 0.0
        actor_artifacts: Dict[str, Set[str]] = {}
        for var_name in ("P", "R", "O", "RESP"):
            actor_artifacts[var_name] = self._binding_artifact_ids(
                sg.bindings.get(var_name)
            )

        if actor_artifacts["P"]:
            if actor_artifacts["P"] & evidence_artifacts:
                bonus += 1.40
            else:
                bonus -= 0.60

        if actor_artifacts["R"]:
            if actor_artifacts["R"] & evidence_artifacts:
                bonus += 0.95
            else:
                bonus -= 0.35

        if actor_artifacts["O"]:
            if actor_artifacts["O"] & evidence_artifacts:
                bonus += 0.70
            else:
                bonus -= 0.20

        if actor_artifacts["RESP"]:
            if actor_artifacts["RESP"] & evidence_artifacts:
                bonus += 0.45

        if actor_artifacts["P"] & actor_artifacts["R"] & evidence_artifacts:
            bonus += 0.85

        if actor_artifacts["R"] & actor_artifacts["O"] & evidence_artifacts:
            bonus += 0.60

        if actor_artifacts["P"] & actor_artifacts["O"] & evidence_artifacts:
            bonus += 0.35

        document_artifacts = self._binding_artifact_ids(sg.bindings.get("A"))
        claim_artifacts = self._binding_artifact_ids(sg.bindings.get("CL"))
        if actor_artifacts["P"] and document_artifacts:
            if actor_artifacts["P"] & document_artifacts:
                bonus += 0.80
        if actor_artifacts["P"] and claim_artifacts:
            if actor_artifacts["P"] & claim_artifacts:
                bonus += 0.55

        return bonus

    def _create_trivial_subgraph(
        self,
        all_anchors: Dict[str, List[Anchor]],
        all_mentions: Dict[str, List[Mention]],
    ) -> Subgraph:
        """Trivial subgraph when no graph spec is available."""
        sg = Subgraph(
            subgraph_id=Subgraph.generate_id(),
            bindings={},
            score=0.0,
            hard_coverage=1.0,
        )
        sg.coherence_score = 0.5
        sg.diversity_score = 1.0
        sg.frame_witness = {
            "witness_id": f"witness-{sg.subgraph_id}",
            "subgraph_id": sg.subgraph_id,
            "description": "Trivial subgraph (no graph spec).",
            "anchor_ids": [],
            "mention_ids": [],
            "artifact_ids": [],
            "skeleton_bindings": {},
            "coherence_score": 0.5,
            "temporal_consistency": True,
            "temporal_span": {"earliest": "", "latest": ""},
            "artifact_coverage": [],
            "evidence_chain_summary": [],
        }
        sg.snapshot = {"nodes": [], "edges": []}
        return sg


# ============================================================
# Phase 6: Slot Binding and Witness Construction
# ============================================================
