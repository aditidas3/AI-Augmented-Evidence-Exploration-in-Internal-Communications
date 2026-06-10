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
from ..linking.entity_similarity import (
    artifact_context_overlap_score as _artifact_context_overlap_score,
    normalize_entity_phrase as _normalize_entity_phrase,
    surface_similar as _surface_similar,
    surface_similarity_ratio as _surface_similarity_ratio,
)
from ..linking.link_confidence import (
    score_link_path_confidence as _score_link_path_confidence,
    shared_anchor_link_confidence as _shared_anchor_link_confidence,
    shared_artifact_link_confidence as _shared_artifact_link_confidence,
)
from ..infrastructure.adapters import MentionExtractor
from .phase3 import Phase3_AnchorMentionExtraction

class Phase4_EntityLinkHypothesis:
    """
    Propose entity hypotheses (mention clustering) and
    link hypotheses (cross-artifact connections via KG0).
    """

    # Tokens stripped before computing token-set similarity. These are
    # legal-entity suffixes and generic boilerplate that the LLM
    # extractor sometimes includes and sometimes doesn't, so they
    # cause spurious cluster splits when present in only one variant.
    # Stripping them lets ``"Cephalon"`` cluster with ``"Cephalon
    # Inc."`` and with ``"Cephalon Pharmaceuticals"`` while still
    # keeping ``"Cephalon Inc."`` separate from ``"Watson Inc."``
    # (after stripping, the only remaining tokens are the
    # discriminating brand names).
    _SURFACE_SIM_STOPWORDS: frozenset = frozenset({
        "inc", "incorporated",
        "llc", "ltd", "lp", "llp", "plc",
        "corp", "corporation",
        "co", "company", "companies",
        "group", "holdings",
        "pharmaceuticals", "pharmaceutical", "pharma",
        "labs", "laboratories",
        "the", "and",
    })

    def __init__(self, config: AlignConfig, index: IndexFacade):
        self.config = config
        self.index = index

    def execute(
        self,
        all_anchors: Dict[str, List[Anchor]],
        all_mentions: Dict[str, List[Mention]],
        intent: IntentObject,
    ) -> Tuple[List[EntityHypothesis], List[LinkHypothesis]]:
        """Execute Phase 4."""
        logger.info("Phase 4: Generating entity and link hypotheses")

        # Flatten all mentions
        flat_mentions: List[Mention] = []
        for mentions in all_mentions.values():
            flat_mentions.extend(mentions)

        # Build anchor lookup
        anchor_lookup: Dict[str, Anchor] = {}
        for anchors in all_anchors.values():
            for anchor in anchors:
                anchor_lookup[anchor.anchor_id] = anchor

        # Entity hypotheses: cluster mentions by category + surface similarity
        entity_hyps = self._propose_entity_hypotheses(flat_mentions, anchor_lookup)
        entity_hyps = self._prune_redundant_hypotheses(
            entity_hyps,
            anchor_lookup,
        )

        # Link hypotheses: find KG0 paths between entities in different artifacts
        link_hyps = self._propose_link_hypotheses(
            entity_hyps, anchor_lookup, intent
        )

        logger.info(
            f"  Generated {len(entity_hyps)} entity hypotheses, "
            f"{len(link_hyps)} link hypotheses"
        )
        return entity_hyps, link_hyps

    def _propose_entity_hypotheses(
        self,
        mentions: List[Mention],
        anchor_lookup: Dict[str, Anchor],
    ) -> List[EntityHypothesis]:
        """Cluster mentions by category and surface similarity."""
        # Group by category
        by_category: Dict[str, List[Mention]] = defaultdict(list)
        for m in mentions:
            by_category[m.category].append(m)

        hypotheses = []
        ordered_categories = sorted(
            by_category,
            key=lambda category: MentionExtractor.CATEGORY_PRIORITY.get(category, 0),
            reverse=True,
        )
        for category in ordered_categories:
            cat_mentions = by_category[category]
            # Cluster by normalized surface form. The naive O(N^2)
            # all-pairs scan dominated Phase 4 wall-clock once large
            # retrieval runs crossed ~1k mentions. Two cheap tricks
            # bring it down by ~20x:
            #
            #   1. Exact-match fast path via dict lookup: every
            #      duplicate surface (e.g. "Cephalon, Inc." appearing
            #      five times) skips the similarity scan entirely.
            #   2. First-character bucketing: only compare keys
            #      sharing the same leading character, since the
            #      similarity ratio cannot exceed 1 - 1/max_len when
            #      the leading characters differ.
            clusters: Dict[str, List[Mention]] = defaultdict(list)
            buckets: Dict[str, List[str]] = defaultdict(list)
            for m in cat_mentions:
                key = (
                    m.normalized.lower().strip()
                    if m.normalized
                    else m.surface.lower().strip()
                )
                if key in clusters:
                    clusters[key].append(m)
                    continue
                bucket_key = key[:1] if key else ""
                matched = False
                for existing_key in buckets[bucket_key]:
                    if self._surface_similar(key, existing_key):
                        clusters[existing_key].append(m)
                        matched = True
                        break
                if not matched:
                    clusters[key].append(m)
                    buckets[bucket_key].append(key)

            for canonical, cluster_mentions in clusters.items():
                if not cluster_mentions:
                    continue

                canonical_surface = canonical or cluster_mentions[0].surface
                kg_link_candidates = self._aggregate_hypothesis_kg_candidates(
                    cluster_mentions,
                    canonical_surface,
                )
                kg0_ids = [
                    candidate["id"]
                    for candidate in kg_link_candidates
                    if candidate.get("selected")
                ]
                evidence = self._build_entity_hypothesis_evidence(
                    cluster_mentions,
                    kg_link_candidates,
                )

                hyp = EntityHypothesis(
                    hypothesis_id=EntityHypothesis.generate_id(),
                    canonical_name=canonical_surface,
                    category=category,
                    mentions=cluster_mentions,
                    kg0_entity_ids=kg0_ids,
                    kg0_link_candidates=kg_link_candidates,
                    confidence=self._compute_entity_confidence(
                        cluster_mentions,
                        anchor_lookup,
                        kg_link_candidates,
                    ),
                    evidence=evidence,
                )
                hypotheses.append(hyp)

                if len(hypotheses) >= self.config.max_entity_hypotheses:
                    return hypotheses

        return hypotheses

    def _prune_redundant_hypotheses(
        self,
        hypotheses: List[EntityHypothesis],
        anchor_lookup: Dict[str, Anchor],
    ) -> List[EntityHypothesis]:
        keep_ids: Set[str] = {hyp.hypothesis_id for hyp in hypotheses}
        grouped: Dict[Tuple[str, str], List[EntityHypothesis]] = defaultdict(list)

        for hyp in hypotheses:
            if hyp.category not in {"ENTITY_ROLE", "ENTITY_ACTION_ITEM"}:
                continue
            primary_anchor_id = hyp.mentions[0].anchor_id if hyp.mentions else ""
            if primary_anchor_id:
                grouped[(hyp.category, primary_anchor_id)].append(hyp)

        for (category, _anchor_id), group in grouped.items():
            if len(group) <= 1:
                continue

            if category == "ENTITY_ROLE":
                ranked = sorted(
                    group,
                    key=lambda hyp: (
                        self._role_hypothesis_rank(hyp, anchor_lookup),
                        hyp.confidence,
                        len(hyp.canonical_name or ""),
                    ),
                    reverse=True,
                )
                best = ranked[0]
                best_rank = self._role_hypothesis_rank(best, anchor_lookup)
                for hyp in ranked[1:]:
                    hyp_rank = self._role_hypothesis_rank(hyp, anchor_lookup)
                    if (
                        best_rank >= hyp_rank + 0.18
                        and self._role_hypothesis_is_generic(hyp, anchor_lookup)
                    ):
                        keep_ids.discard(hyp.hypothesis_id)
            elif category == "ENTITY_ACTION_ITEM":
                ranked = sorted(
                    group,
                    key=lambda hyp: (
                        self._action_item_hypothesis_rank(hyp, anchor_lookup),
                        hyp.confidence,
                        len(hyp.canonical_name or ""),
                    ),
                    reverse=True,
                )
                best = ranked[0]
                best_rank = self._action_item_hypothesis_rank(
                    best,
                    anchor_lookup,
                )
                for hyp in ranked[1:]:
                    hyp_rank = self._action_item_hypothesis_rank(
                        hyp,
                        anchor_lookup,
                    )
                    if (
                        best_rank >= hyp_rank + 0.18
                        and self._action_item_hypothesis_is_generic(
                            hyp,
                            anchor_lookup,
                        )
                    ):
                        keep_ids.discard(hyp.hypothesis_id)

        return [hyp for hyp in hypotheses if hyp.hypothesis_id in keep_ids]

    def _role_hypothesis_rank(
        self,
        hyp: EntityHypothesis,
        anchor_lookup: Dict[str, Anchor],
    ) -> float:
        score = float(hyp.confidence or 0.0)
        tokens = set(re.findall(r"[A-Za-z0-9]+", str(hyp.canonical_name or "").lower()))
        if len(tokens) >= 2:
            score += 0.18
        if tokens & {"senior", "associate", "assistant", "chief", "deputy", "reviewer", "r", "ph"}:
            score += 0.16
        if self._role_hypothesis_is_generic(hyp, anchor_lookup):
            score -= 0.22
        return score

    def _role_hypothesis_is_generic(
        self,
        hyp: EntityHypothesis,
        anchor_lookup: Dict[str, Anchor],
    ) -> bool:
        for mention in hyp.mentions:
            surface = str(mention.surface or "").strip()
            tokens = [token for token in re.findall(r"[A-Za-z0-9]+", surface.lower()) if token]
            anchor = anchor_lookup.get(mention.anchor_id)
            if (
                len(tokens) == 1
                and surface.lower() == surface
                and tokens[0] in Phase3_AnchorMentionExtraction._ROLE_TOKENS
            ):
                return True
            if "confidential" in tokens:
                return True
            if anchor is not None:
                raw_text = str(anchor.raw_text or "")
                start = max(0, int(mention.span_start) - 120)
                end = min(len(raw_text), int(mention.span_end) + 120)
                context = raw_text[start:end].lower()
                if any(
                    cue in context
                    for cue in Phase3_AnchorMentionExtraction._DISCLAIMER_CUES
                ):
                    return True
        return False

    def _action_item_hypothesis_rank(
        self,
        hyp: EntityHypothesis,
        anchor_lookup: Dict[str, Anchor],
    ) -> float:
        score = float(hyp.confidence or 0.0)
        tokens = set(re.findall(r"[A-Za-z0-9]+", str(hyp.canonical_name or "").lower()))
        if len(tokens) >= 2:
            score += 0.10
        if tokens & Phase3_AnchorMentionExtraction._ACTION_ITEM_DECISIVE_TOKENS:
            score += 0.18
        score += self._action_item_hypothesis_context_bonus(
            hyp,
            anchor_lookup,
        )
        if self._action_item_hypothesis_is_generic(hyp, anchor_lookup):
            score -= 0.18
        return score

    def _action_item_hypothesis_is_generic(
        self,
        hyp: EntityHypothesis,
        anchor_lookup: Dict[str, Anchor],
    ) -> bool:
        tokens = set(re.findall(r"[A-Za-z0-9]+", str(hyp.canonical_name or "").lower()))
        if not tokens:
            return False
        if tokens <= {"next", "step"} or tokens <= {"next", "steps"}:
            return True
        if tokens <= {"final", "review"} or tokens <= {"review"}:
            return True
        if tokens <= {"follow", "up"}:
            return True
        if (
            tokens & Phase3_AnchorMentionExtraction._ACTION_ITEM_HEDGE_TOKENS
            and not tokens
            & Phase3_AnchorMentionExtraction._ACTION_ITEM_DECISIVE_TOKENS
            and self._action_item_hypothesis_context_bonus(hyp, anchor_lookup)
            < 0.04
        ):
            return True
        return False

    def _action_item_hypothesis_context_bonus(
        self,
        hyp: EntityHypothesis,
        anchor_lookup: Dict[str, Anchor],
    ) -> float:
        best = 0.0
        for mention in hyp.mentions:
            anchor = anchor_lookup.get(mention.anchor_id)
            if anchor is None:
                continue
            best = max(
                best,
                self._action_item_mention_context_score(mention, anchor),
            )
        return best

    def _action_item_mention_context_score(
        self,
        mention: Mention,
        anchor: Anchor,
    ) -> float:
        raw_text = str(anchor.raw_text or "")
        start = max(0, int(mention.span_start) - 120)
        end = min(len(raw_text), int(mention.span_end) + 120)
        context = raw_text[start:end].lower()
        tokens = set(re.findall(r"[A-Za-z0-9]+", str(mention.surface or "").lower()))

        score = 0.0
        if tokens & Phase3_AnchorMentionExtraction._ACTION_ITEM_DECISIVE_TOKENS:
            score += 0.10
        if any(
            cue in context
            for cue in Phase3_AnchorMentionExtraction._ACTION_ITEM_PLANNED_CUES
        ):
            score += 0.10
        if tokens <= {"move", "forward"}:
            score += 0.10
        if any(
            cue in context
            for cue in Phase3_AnchorMentionExtraction._ACTION_ITEM_HEDGE_CUES
        ):
            score -= 0.14
        if tokens <= {"follow", "up"}:
            score -= 0.16
            if re.search(r"\bfollow(?:-?\s*up)\s+process(?:es)?\b", context):
                score -= 0.10
        if (
            tokens & Phase3_AnchorMentionExtraction._ACTION_ITEM_HEDGE_TOKENS
            and not tokens
            & Phase3_AnchorMentionExtraction._ACTION_ITEM_DECISIVE_TOKENS
        ):
            score -= 0.10
        if self._anchor_is_header_like_for_pruning(anchor):
            score -= 0.08
        return score

    @staticmethod
    def _anchor_is_header_like_for_pruning(anchor: Anchor) -> bool:
        for component in anchor.path or []:
            component_type = getattr(component, "component_type", None)
            if component_type is None and isinstance(component, dict):
                component_type = component.get("component_type")
            if str(component_type or "").strip().lower() == "header":
                return True
        return False

    def _surface_similar(self, a: str, b: str) -> bool:
        """Check if two normalized surfaces are similar enough to cluster.

        Token-set Jaccard with legal-entity stopword stripping. The
        previous character-ratio implementation could not cluster
        ``"Cephalon Inc."`` with ``"Cephalon Pharmaceuticals"`` --
        once the brand-name characters were exhausted, the suffix
        characters drove the score below threshold and the LLM's
        spelling drift across rebuilds produced spurious extra
        entity hypotheses. Switching to token-set Jaccard lets us
        treat ``Inc.`` / ``Pharmaceuticals`` / ``Corp`` as
        interchangeable noise around the discriminating tokens.
        """
        return _surface_similar(
            a,
            b,
            threshold=self.config.entity_similarity_threshold,
            stopwords=self._SURFACE_SIM_STOPWORDS,
        )

    def _normalize_entity_phrase(self, value: Any) -> str:
        return _normalize_entity_phrase(value)

    def _surface_similarity_ratio(self, a: str, b: str) -> float:
        """Compute a soft similarity ratio for phrase-level clustering/linking."""
        return _surface_similarity_ratio(a, b)

    def _aggregate_hypothesis_kg_candidates(
        self,
        mentions: List[Mention],
        canonical_surface: str,
    ) -> List[Dict[str, Any]]:
        """Aggregate mention-level KG link candidates into hypothesis-level support."""
        if not mentions:
            return []

        canonical_norm = self._normalize_entity_phrase(canonical_surface)
        category_key = str(mentions[0].category or "").strip().upper()
        aggregated: Dict[str, Dict[str, Any]] = {}

        for mention in mentions:
            mention_weight = max(0.35, float(mention.confidence or 0.0))
            kg_link = mention.qualifiers.get("kg_link", {}) or {}
            raw_candidates = kg_link.get("candidates", []) or []

            # If a mention already resolved to a KG node, preserve it as the
            # strongest support signal for hypothesis-level aggregation.
            if mention.kg0_entity_id:
                resolved_name = ""
                for candidate in raw_candidates:
                    if str(candidate.get("id", "")) == str(mention.kg0_entity_id):
                        resolved_name = str(candidate.get("name", "")).strip()
                        break
                raw_candidates = [
                    {
                        "id": mention.kg0_entity_id,
                        "label": "",
                        "name": resolved_name,
                        "score": max(0.98, float(kg_link.get("best_score", 0.0) or 0.0)),
                    }
                ] + raw_candidates

            seen_candidate_ids: Set[str] = set()
            for rank, candidate in enumerate(raw_candidates[:3]):
                candidate_id = str(candidate.get("id", "")).strip()
                if not candidate_id or candidate_id in seen_candidate_ids:
                    continue
                seen_candidate_ids.add(candidate_id)

                candidate_name = str(candidate.get("name", "")).strip()
                candidate_norm = self._normalize_entity_phrase(candidate_name)
                surface_ratio = self._surface_similarity_ratio(
                    canonical_norm,
                    candidate_norm or canonical_norm,
                )
                base_score = float(candidate.get("score", 0.0) or 0.0)
                rank_weight = max(0.55, 1.0 - (0.12 * rank))
                contribution = mention_weight * base_score * rank_weight
                contribution *= 0.85 + (0.15 * surface_ratio)

                bucket = aggregated.setdefault(
                    candidate_id,
                    {
                        "id": candidate_id,
                        "label": str(candidate.get("label", "")).strip(),
                        "name": candidate_name,
                        "aggregate_score": 0.0,
                        "best_score": 0.0,
                        "support_mentions": set(),
                        "resolved_mentions": 0,
                        "exact_surface_mentions": 0,
                    },
                )
                bucket["aggregate_score"] += contribution
                bucket["best_score"] = max(bucket["best_score"], base_score)
                bucket["support_mentions"].add(mention.mention_id)
                if mention.kg0_entity_id and str(mention.kg0_entity_id) == candidate_id:
                    bucket["resolved_mentions"] += 1
                if candidate_norm and candidate_norm == canonical_norm:
                    bucket["exact_surface_mentions"] += 1
                if candidate_name and (
                    not bucket["name"]
                    or surface_ratio
                    > self._surface_similarity_ratio(
                        canonical_norm,
                        self._normalize_entity_phrase(str(bucket["name"])),
                    )
                ):
                    bucket["name"] = candidate_name

        if not aggregated:
            return []

        mention_count = max(1, len(mentions))
        ranked_candidates: List[Dict[str, Any]] = []
        for candidate in aggregated.values():
            support_mentions = len(candidate["support_mentions"])
            normalized_score = candidate["aggregate_score"] / mention_count
            ranked_candidates.append(
                {
                    "id": candidate["id"],
                    "label": candidate["label"],
                    "name": candidate["name"],
                    "aggregate_score": round(candidate["aggregate_score"], 4),
                    "normalized_score": round(normalized_score, 4),
                    "best_score": round(candidate["best_score"], 4),
                    "support_mentions": support_mentions,
                    "resolved_mentions": int(candidate["resolved_mentions"]),
                    "exact_surface_mentions": int(candidate["exact_surface_mentions"]),
                }
            )

        ranked_candidates.sort(
            key=lambda item: (
                item["resolved_mentions"],
                item["support_mentions"],
                item["normalized_score"],
                item["best_score"],
                item["name"],
                item["id"],
            ),
            reverse=True,
        )

        second_score = (
            float(ranked_candidates[1]["normalized_score"])
            if len(ranked_candidates) > 1
            else 0.0
        )
        for index, candidate in enumerate(ranked_candidates[:3]):
            margin = float(candidate["normalized_score"]) - (
                second_score if index == 0 else 0.0
            )
            selected = False
            if candidate["resolved_mentions"] > 0:
                selected = True
            elif (
                candidate["support_mentions"] >= 2
                and candidate["normalized_score"] >= 0.38
            ):
                selected = True
            elif (
                index == 0
                and candidate["normalized_score"] >= 0.62
                and margin >= 0.08
            ):
                selected = True
            elif (
                category_key
                in {
                    "ENTITY_POLICY",
                    "POLICY",
                    "ENTITY_CLAIM",
                    "CLAIM",
                    "ENTITY_DOCUMENT",
                    "DOCUMENT",
                    "ARTIFACT_DOCUMENT",
                }
                and index == 0
                and candidate["normalized_score"] >= 0.50
                and candidate["best_score"] >= 0.70
                and margin >= 0.10
            ):
                selected = True
            if (
                category_key in {"ENTITY_ROLE", "ROLE"}
                and candidate["resolved_mentions"] == 0
                and candidate["exact_surface_mentions"] == 0
            ):
                selected = False
            candidate["selected"] = selected
            candidate["selection_margin"] = round(max(0.0, margin), 4)

        return ranked_candidates[:3]

    def _build_entity_hypothesis_evidence(
        self,
        mentions: List[Mention],
        kg_link_candidates: List[Dict[str, Any]],
    ) -> List[str]:
        evidence: List[str] = []
        if not mentions:
            return evidence

        if len(mentions) > 1:
            evidence.append(f"Clustered from {len(mentions)} aligned mentions")

        if kg_link_candidates:
            top = kg_link_candidates[0]
            evidence.append(
                "Top KG candidate "
                f"{top.get('id', '')} ({top.get('name', '') or 'unnamed'}) "
                f"score={top.get('normalized_score', 0.0):.2f} "
                f"support={top.get('support_mentions', 0)}"
            )
            if top.get("selected"):
                evidence.append("Promoted to hypothesis-level KG grounding")

        return evidence

    def _compute_entity_confidence(
        self,
        mentions: List[Mention],
        anchor_lookup: Dict[str, Anchor],
        kg_link_candidates: Optional[List[Dict[str, Any]]] = None,
    ) -> float:
        """Compute confidence for an entity hypothesis."""
        if not mentions:
            return 0.0
        # Higher confidence with more mentions from different artifacts
        artifact_ids: Set[str] = set()
        for mention in mentions:
            anchor = anchor_lookup.get(mention.anchor_id)
            artifact_id = anchor.artifact_id if anchor else mention.anchor_id
            if artifact_id:
                artifact_ids.add(artifact_id)
        base = max(m.confidence for m in mentions)
        cross_artifact_bonus = min(0.2, 0.05 * max(0, len(artifact_ids) - 1))
        candidate_bonus = 0.0
        if kg_link_candidates:
            top = kg_link_candidates[0]
            normalized_score = float(top.get("normalized_score", 0.0) or 0.0)
            support_mentions = int(top.get("support_mentions", 0) or 0)
            resolved_mentions = int(top.get("resolved_mentions", 0) or 0)
            if resolved_mentions > 0:
                candidate_bonus = min(
                    0.15,
                    0.05 * resolved_mentions + 0.08 * normalized_score,
                )
            elif support_mentions >= 2 and normalized_score >= 0.38:
                candidate_bonus = min(
                    0.12,
                    0.03 * support_mentions + 0.08 * normalized_score,
                )
            elif normalized_score >= 0.62:
                candidate_bonus = min(0.08, 0.05 * normalized_score)
        return min(1.0, base + cross_artifact_bonus + candidate_bonus)

    def _linkable_kg_candidates(
        self,
        entity_hyp: EntityHypothesis,
    ) -> List[Dict[str, Any]]:
        """
        Return the subset of hypothesis-level KG candidates that are stable enough
        to participate in link proposal.
        """
        linkable: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()

        for kg0_id in entity_hyp.kg0_entity_ids:
            candidate = next(
                (
                    item
                    for item in entity_hyp.kg0_link_candidates
                    if str(item.get("id", "")) == str(kg0_id)
                ),
                None,
            )
            linkable.append(
                {
                    "id": str(kg0_id),
                    "name": str((candidate or {}).get("name", "")).strip(),
                    "quality": 1.0,
                    "selected": True,
                    "source": "direct",
                    "normalized_score": float((candidate or {}).get("normalized_score", 1.0) or 1.0),
                    "support_mentions": int((candidate or {}).get("support_mentions", 1) or 1),
                }
            )
            seen_ids.add(str(kg0_id))

        for candidate in entity_hyp.kg0_link_candidates:
            candidate_id = str(candidate.get("id", "")).strip()
            if not candidate_id or candidate_id in seen_ids:
                continue

            normalized_score = float(candidate.get("normalized_score", 0.0) or 0.0)
            best_score = float(candidate.get("best_score", 0.0) or 0.0)
            support_mentions = int(candidate.get("support_mentions", 0) or 0)
            selection_margin = float(candidate.get("selection_margin", 0.0) or 0.0)
            selected = bool(candidate.get("selected"))

            if selected:
                quality = max(0.72, min(0.9, normalized_score + 0.20))
                source = "selected"
            else:
                if entity_hyp.category in {"ENTITY_ROLE", "ROLE"}:
                    continue
                stable_candidate_only = (
                    normalized_score >= 0.40
                    and (
                        support_mentions >= 2
                        or best_score >= 0.60
                        or selection_margin >= 0.15
                    )
                )
                if not stable_candidate_only:
                    continue
                quality = max(0.45, min(0.68, normalized_score + 0.12))
                source = "candidate_only"

            linkable.append(
                {
                    "id": candidate_id,
                    "name": str(candidate.get("name", "")).strip(),
                    "quality": round(quality, 4),
                    "selected": selected,
                    "source": source,
                    "normalized_score": normalized_score,
                    "support_mentions": support_mentions,
                }
            )
            seen_ids.add(candidate_id)

        linkable.sort(
            key=lambda item: (
                item["source"] == "direct",
                item["selected"],
                item["quality"],
                item["normalized_score"],
                item["support_mentions"],
                item["id"],
            ),
            reverse=True,
        )
        return linkable[:2]

    def _score_link_path_confidence(
        self,
        source_candidate: Dict[str, Any],
        target_candidate: Dict[str, Any],
        path_length: int,
    ) -> float:
        """Score a KG path using hypothesis-level candidate confidence."""
        return _score_link_path_confidence(
            source_candidate,
            target_candidate,
            path_length,
        )

    def _shared_anchor_link_confidence(
        self,
        source_entity: EntityHypothesis,
        target_entity: EntityHypothesis,
        shared_anchor_count: int,
    ) -> float:
        return _shared_anchor_link_confidence(
            source_entity.confidence,
            target_entity.confidence,
            shared_anchor_count=shared_anchor_count,
        )

    def _normalize_phrase(self, value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    def _text_tokens(self, text: str) -> Set[str]:
        tokens: Set[str] = set()
        for token in re.findall(r"[A-Za-z0-9]+", str(text or "").lower()):
            if len(token) <= 1:
                continue
            tokens.add(token)
            if token.endswith("ies") and len(token) > 4:
                tokens.add(token[:-3] + "y")
            elif token.endswith("s") and len(token) > 4:
                tokens.add(token[:-1])
        return tokens

    def _shared_artifact_link_confidence(
        self,
        source_entity: EntityHypothesis,
        target_entity: EntityHypothesis,
        shared_artifact_count: int,
        lexical_overlap: float,
    ) -> float:
        return _shared_artifact_link_confidence(
            source_entity.confidence,
            target_entity.confidence,
            shared_artifact_count=shared_artifact_count,
            lexical_overlap=lexical_overlap,
        )

    def _entity_artifact_ids(
        self,
        entity_hyp: EntityHypothesis,
        anchor_lookup: Dict[str, Anchor],
    ) -> Set[str]:
        artifact_ids: Set[str] = set()
        for mention in entity_hyp.mentions:
            anchor = anchor_lookup.get(mention.anchor_id)
            if anchor and anchor.artifact_id:
                artifact_ids.add(anchor.artifact_id)
        return artifact_ids

    def _artifact_context_overlap(
        self,
        source_entity: EntityHypothesis,
        target_entity: EntityHypothesis,
        anchor_lookup: Dict[str, Anchor],
    ) -> float:
        source_texts: List[str] = []
        for mention in source_entity.mentions:
            source_texts.append(self._normalize_phrase(mention.surface))
            anchor = anchor_lookup.get(mention.anchor_id)
            if anchor and anchor.raw_text:
                source_texts.append(self._normalize_phrase(anchor.raw_text))

        target_phrases: List[str] = []
        for mention in target_entity.mentions:
            normalized = self._normalize_phrase(mention.surface)
            if normalized:
                target_phrases.append(normalized)
        if target_entity.canonical_name:
            target_phrases.append(self._normalize_phrase(target_entity.canonical_name))

        return _artifact_context_overlap_score(source_texts, target_phrases)

    def _edge_compatible_category_pairs(
        self,
        intent: IntentObject,
    ) -> Set[Tuple[str, str]]:
        """Return directed category pairs that are relevant to the current graph spec."""
        gs = intent.graph_spec
        if gs is None or not gs.edges:
            return set()

        var_lookup = {var.var: var for var in gs.vars}
        allowed_pairs: Set[Tuple[str, str]] = set()

        for edge in gs.edges:
            src_var = var_lookup.get(edge.src)
            dst_var = var_lookup.get(edge.dst)
            if not src_var or not dst_var:
                continue

            src_categories = self._var_compatible_categories(src_var.type)
            dst_categories = self._var_compatible_categories(dst_var.type)

            for src_category in src_categories:
                for dst_category in dst_categories:
                    for src_alias in self._category_aliases(src_category):
                        for dst_alias in self._category_aliases(dst_category):
                            allowed_pairs.add((src_alias, dst_alias))
                            # Link hypotheses are undirected evidence for Phase 5 lookup.
                            allowed_pairs.add((dst_alias, src_alias))

        return allowed_pairs

    def _var_compatible_categories(self, var_type: str) -> Set[str]:
        """Expanded category aliases that can satisfy a graph variable."""
        if not var_type:
            return set()

        raw_categories = self.config.ontology.expand_category(var_type)
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

    def _propose_link_hypotheses(
        self,
        entity_hyps: List[EntityHypothesis],
        anchor_lookup: Dict[str, Anchor],
        intent: IntentObject,
    ) -> List[LinkHypothesis]:
        """Find connections between entity hypotheses via KG0."""
        link_hyps = []
        seen_paths: Set[Tuple[str, str, Tuple[str, ...]]] = set()
        allowed_pairs = self._edge_compatible_category_pairs(intent)

        # Identify entity hypotheses that are grounded in user-specified hints.
        # These pairs receive a dedicated bridging pass because the question
        # itself names them, so shared anchors/artifacts are first-class
        # evidence of a relationship regardless of category compatibility.
        hint_grounded_ids = self._hint_grounded_entity_ids(entity_hyps, intent)
        if len(hint_grounded_ids) >= 2:
            self._bridge_hint_grounded_pairs(
                entity_hyps,
                hint_grounded_ids,
                anchor_lookup,
                link_hyps,
                seen_paths,
            )
            if len(link_hyps) >= self.config.max_link_hypotheses:
                return sorted(
                    link_hyps,
                    key=lambda link: link.confidence,
                    reverse=True,
                )

        # Get entities that have direct or stable hypothesis-level KG candidates.
        entities_with_kg0: List[Tuple[EntityHypothesis, List[Dict[str, Any]]]] = []
        for entity_hyp in entity_hyps:
            candidates = self._linkable_kg_candidates(entity_hyp)
            if candidates:
                entities_with_kg0.append((entity_hyp, candidates))

        entity_by_id = {entity_hyp.hypothesis_id: entity_hyp for entity_hyp in entity_hyps}

        # Pre-Phase-5 KG structure access is optional. When deferred, Phase 4
        # keeps only lightweight hypothesis aggregation and contextual bridges.
        if not self.config.defer_kg_structure_until_post_phase5:
            for i, (e1, e1_candidates) in enumerate(entities_with_kg0):
                for e2, e2_candidates in entities_with_kg0[i + 1 :]:
                    if e1.hypothesis_id == e2.hypothesis_id:
                        continue
                    if allowed_pairs and not any(
                        (src_alias, dst_alias) in allowed_pairs
                        for src_alias in self._category_aliases(e1.category)
                        for dst_alias in self._category_aliases(e2.category)
                    ):
                        continue

                    e1_artifacts = {
                        anchor_lookup.get(
                            m.anchor_id,
                            Anchor(
                                anchor_id="",
                                artifact_id="",
                                path=[],
                                raw_text="",
                            ),
                        ).artifact_id
                        for m in e1.mentions
                    }
                    e2_artifacts = {
                        anchor_lookup.get(
                            m.anchor_id,
                            Anchor(
                                anchor_id="",
                                artifact_id="",
                                path=[],
                                raw_text="",
                            ),
                        ).artifact_id
                        for m in e2.mentions
                    }
                    same_artifact_overlap = bool(e1_artifacts & e2_artifacts)

                    for source_candidate in e1_candidates:
                        for target_candidate in e2_candidates:
                            kg0_id1 = source_candidate["id"]
                            kg0_id2 = target_candidate["id"]
                            if kg0_id1 == kg0_id2:
                                continue
                            search_hops = self.config.kg0_max_path_hops
                            if (
                                source_candidate.get("source") == "candidate_only"
                                or target_candidate.get("source") == "candidate_only"
                            ):
                                search_hops = max(search_hops, 4)
                            paths = self.index.neo4j.find_paths(
                                kg0_id1,
                                kg0_id2,
                                max_hops=search_hops,
                            )
                            for path in paths:
                                path_ids = tuple(
                                    _node_identity_value(self.index.neo4j, n)
                                    for n in path
                                )
                                path_key = (
                                    e1.hypothesis_id,
                                    e2.hypothesis_id,
                                    path_ids,
                                )
                                if path_key in seen_paths:
                                    continue
                                seen_paths.add(path_key)

                                relationship_type = "KG0_PATH"
                                if (
                                    source_candidate.get("source") == "candidate_only"
                                    or target_candidate.get("source") == "candidate_only"
                                ):
                                    relationship_type = "KG0_PATH_CANDIDATE"

                                link_hyp = LinkHypothesis(
                                    hypothesis_id=LinkHypothesis.generate_id(),
                                    source_entity_id=e1.hypothesis_id,
                                    target_entity_id=e2.hypothesis_id,
                                    relationship_type=relationship_type,
                                    path=list(path_ids),
                                    confidence=round(
                                        max(
                                            0.10,
                                            self._score_link_path_confidence(
                                                source_candidate,
                                                target_candidate,
                                                len(path_ids),
                                            )
                                            - (0.03 if same_artifact_overlap else 0.0),
                                        ),
                                        4,
                                    ),
                                )
                                link_hyps.append(link_hyp)

                                if (
                                    len(link_hyps)
                                    >= self.config.max_link_hypotheses
                                ):
                                    return sorted(
                                        link_hyps,
                                        key=lambda link: link.confidence,
                                        reverse=True,
                                    )

        claim_like_categories = {"ENTITY_CLAIM", "CLAIM"}
        context_bridge_categories = {
            "ENTITY_DOCUMENT",
            "DOCUMENT",
            "ARTIFACT_DOCUMENT",
            "ENTITY_POLICY",
            "POLICY",
            "ENTITY_STRATEGY",
            "STRATEGY",
        }
        for i, source_entity in enumerate(entity_hyps):
            source_aliases = self._category_aliases(source_entity.category)
            for target_entity in entity_hyps[i + 1 :]:
                if source_entity.hypothesis_id == target_entity.hypothesis_id:
                    continue
                target_aliases = self._category_aliases(target_entity.category)
                if allowed_pairs and not any(
                    (src_alias, dst_alias) in allowed_pairs
                    for src_alias in source_aliases
                    for dst_alias in target_aliases
                ):
                    continue

                if not (
                    (source_aliases & claim_like_categories and target_aliases & context_bridge_categories)
                    or (target_aliases & claim_like_categories and source_aliases & context_bridge_categories)
                ):
                    continue

                source_anchor_ids = {mention.anchor_id for mention in source_entity.mentions if mention.anchor_id}
                target_anchor_ids = {mention.anchor_id for mention in target_entity.mentions if mention.anchor_id}
                shared_anchor_ids = sorted(source_anchor_ids & target_anchor_ids)
                if shared_anchor_ids:
                    path_key = (
                        source_entity.hypothesis_id,
                        target_entity.hypothesis_id,
                        tuple(f"anchor:{anchor_id}" for anchor_id in shared_anchor_ids),
                    )
                    if path_key not in seen_paths:
                        seen_paths.add(path_key)
                        link_hyps.append(
                            LinkHypothesis(
                                hypothesis_id=LinkHypothesis.generate_id(),
                                source_entity_id=source_entity.hypothesis_id,
                                target_entity_id=target_entity.hypothesis_id,
                                relationship_type="ANCHOR_CONTEXT",
                                path=[f"anchor:{anchor_id}" for anchor_id in shared_anchor_ids[:2]],
                                confidence=self._shared_anchor_link_confidence(
                                    source_entity,
                                    target_entity,
                                    len(shared_anchor_ids),
                                ),
                            )
                        )
                        if len(link_hyps) >= self.config.max_link_hypotheses:
                            return sorted(
                                link_hyps,
                                key=lambda link: link.confidence,
                                reverse=True,
                            )

                shared_artifact_ids = sorted(
                    self._entity_artifact_ids(source_entity, anchor_lookup)
                    & self._entity_artifact_ids(target_entity, anchor_lookup)
                )
                if not shared_artifact_ids:
                    continue

                source_claim_like = source_aliases & claim_like_categories
                target_claim_like = target_aliases & claim_like_categories
                if source_claim_like:
                    claim_entity = source_entity
                    context_entity = target_entity
                elif target_claim_like:
                    claim_entity = target_entity
                    context_entity = source_entity
                else:
                    continue

                lexical_overlap = self._artifact_context_overlap(
                    claim_entity,
                    context_entity,
                    anchor_lookup,
                )
                if lexical_overlap < 0.35:
                    continue

                artifact_path = tuple(
                    f"artifact:{artifact_id}" for artifact_id in shared_artifact_ids[:2]
                )
                path_key = (
                    source_entity.hypothesis_id,
                    target_entity.hypothesis_id,
                    artifact_path,
                )
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)

                link_hyps.append(
                    LinkHypothesis(
                        hypothesis_id=LinkHypothesis.generate_id(),
                        source_entity_id=source_entity.hypothesis_id,
                        target_entity_id=target_entity.hypothesis_id,
                        relationship_type="ARTIFACT_CONTEXT",
                        path=list(artifact_path),
                        confidence=self._shared_artifact_link_confidence(
                            source_entity,
                            target_entity,
                            len(shared_artifact_ids),
                            lexical_overlap,
                        ),
                    )
                )
                if len(link_hyps) >= self.config.max_link_hypotheses:
                    return sorted(
                        link_hyps,
                        key=lambda link: link.confidence,
                        reverse=True,
                    )

        return sorted(
            link_hyps,
            key=lambda link: link.confidence,
            reverse=True,
        )

    def _hint_grounded_entity_ids(
        self,
        entity_hyps: List[EntityHypothesis],
        intent: IntentObject,
    ) -> Set[str]:
        """Return hypothesis ids whose mentions surface-match an EntityHint.

        A hypothesis is hint-grounded when any of its mention surfaces (or its
        canonical name) contains — or is contained by — a hint surface under
        lowercase comparison. This mirrors the substring matching used by
        Phase 5's :meth:`_seed_from_entity_hints` so the two agree on which
        entities the user explicitly named.
        """
        grounded: Set[str] = set()
        hint_surfaces = [
            (h.surface or "").strip().lower()
            for h in intent.entity_hints
            if (h.surface or "").strip()
        ]
        if not hint_surfaces:
            return grounded
        for eh in entity_hyps:
            candidate_surfaces = {
                (m.surface or "").strip().lower() for m in eh.mentions
            }
            candidate_surfaces.add((eh.canonical_name or "").strip().lower())
            candidate_surfaces.discard("")
            for hs in hint_surfaces:
                if any(
                    hs in cs or cs in hs for cs in candidate_surfaces
                ):
                    grounded.add(eh.hypothesis_id)
                    break
        return grounded

    def _bridge_hint_grounded_pairs(
        self,
        entity_hyps: List[EntityHypothesis],
        hint_grounded_ids: Set[str],
        anchor_lookup: Dict[str, Anchor],
        link_hyps: List[LinkHypothesis],
        seen_paths: Set[Tuple[str, str, Tuple[str, ...]]],
    ) -> None:
        """Emit ANCHOR_CONTEXT / ARTIFACT_CONTEXT links for hint-grounded pairs.

        The regular context-bridge pass below is restricted to claim-like ×
        context-bridge categories to keep combinatorial cost bounded. When
        both endpoints are named in the question itself, that restriction is
        inappropriate — the user is explicitly asking how these entities
        relate — so we propose a link whenever they share any anchor or
        artifact, regardless of category.

        This pass is bounded by ``len(hint_grounded_ids) ** 2``, which is
        typically at most a few dozen pairs.
        """
        grounded_hyps = [
            eh for eh in entity_hyps if eh.hypothesis_id in hint_grounded_ids
        ]
        for i, source_entity in enumerate(grounded_hyps):
            for target_entity in grounded_hyps[i + 1:]:
                if source_entity.hypothesis_id == target_entity.hypothesis_id:
                    continue

                source_anchor_ids = {
                    m.anchor_id for m in source_entity.mentions if m.anchor_id
                }
                target_anchor_ids = {
                    m.anchor_id for m in target_entity.mentions if m.anchor_id
                }
                shared_anchor_ids = sorted(
                    source_anchor_ids & target_anchor_ids
                )
                if shared_anchor_ids:
                    path_key = (
                        source_entity.hypothesis_id,
                        target_entity.hypothesis_id,
                        tuple(
                            f"anchor:{aid}" for aid in shared_anchor_ids
                        ),
                    )
                    if path_key not in seen_paths:
                        seen_paths.add(path_key)
                        link_hyps.append(
                            LinkHypothesis(
                                hypothesis_id=LinkHypothesis.generate_id(),
                                source_entity_id=source_entity.hypothesis_id,
                                target_entity_id=target_entity.hypothesis_id,
                                relationship_type="ANCHOR_CONTEXT",
                                path=[
                                    f"anchor:{aid}"
                                    for aid in shared_anchor_ids[:2]
                                ],
                                confidence=self._shared_anchor_link_confidence(
                                    source_entity,
                                    target_entity,
                                    len(shared_anchor_ids),
                                ),
                            )
                        )
                        if len(link_hyps) >= self.config.max_link_hypotheses:
                            return

                shared_artifact_ids = sorted(
                    self._entity_artifact_ids(source_entity, anchor_lookup)
                    & self._entity_artifact_ids(target_entity, anchor_lookup)
                )
                if not shared_artifact_ids:
                    continue

                artifact_path = tuple(
                    f"artifact:{aid}" for aid in shared_artifact_ids[:2]
                )
                path_key = (
                    source_entity.hypothesis_id,
                    target_entity.hypothesis_id,
                    artifact_path,
                )
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)

                # Lexical overlap is still informative but not gating.
                lexical_overlap = self._artifact_context_overlap(
                    source_entity,
                    target_entity,
                    anchor_lookup,
                )
                link_hyps.append(
                    LinkHypothesis(
                        hypothesis_id=LinkHypothesis.generate_id(),
                        source_entity_id=source_entity.hypothesis_id,
                        target_entity_id=target_entity.hypothesis_id,
                        relationship_type="ARTIFACT_CONTEXT",
                        path=list(artifact_path),
                        confidence=self._shared_artifact_link_confidence(
                            source_entity,
                            target_entity,
                            len(shared_artifact_ids),
                            max(lexical_overlap, 0.5),
                        ),
                    )
                )
                if len(link_hyps) >= self.config.max_link_hypotheses:
                    return


# ============================================================
# Phase 5: Subgraph Discovery
# ============================================================
