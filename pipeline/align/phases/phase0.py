from __future__ import annotations

from .shared import *  # noqa: F401,F403
from .shared import (
    _NEO4J_QUERY_ERRORS,
    _document_date_scope_clause,
    _normalize_time_filter_bound,
    _node_id_expression,
    _node_identity_value,
    _node_match_condition,
    _node_text_expression,
    _resolved_edge_relationship_types,
)
from ..retrieval.helpers import (
    build_solr_or_query as _build_solr_or_query,
    entity_hint_terms as _entity_hint_terms,
    expanded_retrieval_terms as _expanded_retrieval_terms,
    required_hint_terms as _required_hint_terms,
)

class Phase0_IntentValidation:
    """
    Parse, validate, and compile the intent object.

    Produces:
        - CompiledScopePredicate (filter expressions for each store)
        - CompiledGraphSkeleton (bounded query skeleton)
        - CompiledRetrievalQuery (multi-backend query plan)
        - Stable hashes for replay
    """

    def __init__(self, config: AlignConfig, index: IndexFacade):
        self.config = config
        self.index = index
        self._anchor_lookup: Dict[str, Anchor] = {}
        self._entity_lookup: Dict[str, EntityHypothesis] = {}

    def execute(self, intent: IntentObject) -> Dict[str, Any]:
        """Execute Phase 0: validate and compile."""
        logger.info(f"Phase 0: Validating intent {intent.header.intent_id}")

        # Validate structural completeness
        self._validate(intent)

        # Compile scope predicate
        scope = self._compile_scope(intent)

        # Compile graph skeleton
        skeleton = self._compile_graph_skeleton(intent)

        # Build retrieval query (FIX #4: pass skeleton so G expands query)
        retrieval_query = self._build_retrieval_query(intent, skeleton)

        # Compute stable hashes
        h_intent = intent.content_hash()
        h_scope = scope.compute_hash()
        h_graph = skeleton.compute_hash() if skeleton else ""

        logger.info(
            f"Phase 0 complete: h_intent={h_intent}, "
            f"h_scope={h_scope}, h_graph={h_graph}"
        )

        return {
            "intent": intent,
            "scope": scope,
            "skeleton": skeleton,
            "retrieval_query": retrieval_query,
            "hashes": {
                "intent": h_intent,
                "scope": h_scope,
                "graph": h_graph,
            },
        }

    def _validate(self, intent: IntentObject):
        """Validate structural completeness of the intent object."""
        if not intent.header.intent_id:
            raise ValueError("Missing intent_id")
        if not intent.header.question_text:
            raise ValueError("Missing question_text")
        if not intent.retrieval_spec.query_text:
            raise ValueError("Missing retrieval query_text")
        if not intent.slot_spec.slots:
            raise ValueError("No slots defined")

        gs = intent.graph_spec
        if gs:
            # Validate graph spec internal consistency
            var_names = {v.var for v in gs.vars}
            for edge in gs.edges:
                if edge.src not in var_names:
                    raise ValueError(f"Edge src '{edge.src}' not in vars")
                if edge.dst not in var_names:
                    raise ValueError(f"Edge dst '{edge.dst}' not in vars")
            # Validate temporal constraints reference valid vars
            for tc in gs.temporal_constraints:
                tc_kind = (tc.kind or "ORDER").upper()
                if tc_kind in {"ORDER", "WITHIN"}:
                    if tc.before not in var_names:
                        raise ValueError(
                            f"Temporal constraint before '{tc.before}' not in vars"
                        )
                    if tc.after not in var_names:
                        raise ValueError(
                            f"Temporal constraint after '{tc.after}' not in vars"
                        )
                elif tc_kind == "CONCURRENT":
                    # Current ALIGN scoring does not enforce CONCURRENT constraints yet.
                    # We preserve them for replay/audit, but do not reject the intent
                    # when upstream intent analysis emits a concurrency block.
                    continue
                else:
                    logger.warning(
                        "Unsupported temporal constraint kind '%s' in intent %s; "
                        "keeping it for replay but skipping strict validation.",
                        tc.kind,
                        intent.header.intent_id,
                    )

    def _compile_scope(self, intent: IntentObject) -> CompiledScopePredicate:
        """Compile ScopeSpec into store-native filter predicates."""
        spec = intent.scope_spec
        # FIX #2 (partial): store source_spec for in-memory evaluate()
        scope = CompiledScopePredicate(mode=spec.mode, source_spec=spec)
        canonical_types = self.config.canonical_families(spec.artifact_types)

        tf = spec.time_filter

        # Solr lexical retrieval uses the same strict scope as downstream
        # soundness checks. Under PREFER mode, scope remains a ranking hint.
        if spec.mode == ScopeMode.STRICT:
            solr_fqs = []

            # Collection filter
            if spec.collections.include:
                collections_clause = " OR ".join(
                    f'collection:"{c}"' for c in spec.collections.include
                )
                solr_fqs.append(f"({collections_clause})")
            if spec.collections.exclude:
                for c in spec.collections.exclude:
                    solr_fqs.append(f'-collection:"{c}"')

            # Artifact type filter
            if canonical_types:
                types_clause = " OR ".join(
                    f'family:"{t}"' for t in canonical_types
                )
                solr_fqs.append(f"({types_clause})")

            # Time filter
            start_bound = _normalize_time_filter_bound(tf.start, end_of_day=False)
            end_bound = _normalize_time_filter_bound(tf.end, end_of_day=True)
            missing_date_clause = "(*:* -date:[* TO *])"
            if tf.op == "between" and tf.start and tf.end:
                solr_fqs.append(
                    f"(date:[{start_bound} TO {end_bound}] OR {missing_date_clause})"
                )
            elif tf.op == "before" and tf.end:
                solr_fqs.append(f"(date:[* TO {end_bound}] OR {missing_date_clause})")
            elif tf.op == "after" and tf.start:
                solr_fqs.append(f"(date:[{start_bound} TO *] OR {missing_date_clause})")

            # Exclude features
            for feat in spec.exclude_features:
                solr_fqs.append(f'-features:"{feat}"')

            scope.solr_fqs = solr_fqs

        # Qdrant semantic retrieval is page-level; these payload filters keep
        # semantic hits inside the same strict scope as the Solr lexical path.
        if spec.mode == ScopeMode.STRICT:
            qdrant_filters = {}
            if spec.collections.include:
                qdrant_filters["collection"] = {"any": spec.collections.include}
            if canonical_types:
                qdrant_filters["family"] = {"any": sorted(canonical_types)}
            # Current Qdrant page payloads also do not carry document dates.

            scope.qdrant_filters = qdrant_filters

        # --- Cypher WHERE clauses ---
        # Under PREFER mode, every ScopeSpec predicate is a soft preference —
        # scope should rank candidates, not exclude them. Emit no WHERE
        # clauses so Phase 1 retrieval stays in-bounds only on entity hits.
        cypher_where = []
        if spec.mode != ScopeMode.STRICT:
            scope.cypher_where = cypher_where
            return scope
        if spec.collections.include:
            collections_list = ", ".join(
                f'"{c}"' for c in spec.collections.include
            )
            cypher_where.append(f"n.collection IN [{collections_list}]")
        if spec.artifact_types:
            # Document.artifact_type no longer exists in the new KG0. Artifact
            # family is now a property of the per-page (:Page) layer because
            # ~63% of the opioid corpus PDFs are mixed-label. We accept the
            # Document if any of its pages carry one of the requested labels.
            # Page labels are stored lowercase in KG0; canonical_families
            # uppercases for stability, so compare case-insensitively.
            labels_list = ", ".join(f'"{t.lower()}"' for t in canonical_types)
            cypher_where.append(
                "size([(n)-[:HAS_PAGE]->(p:Page) "
                f"WHERE toLower(p.label) IN [{labels_list}] | p]) > 0"
            )
        # KG0 stores the document date as `documentDate` in YYYY-MM-DD form
        # (left empty until the metadata stage runs). Truncate ISO bounds to
        # YYYY-MM-DD so lexical string comparison works correctly — otherwise
        # "2010-01-01" < "2010-01-01T00:00:00Z" and the lower bound silently
        # excludes the boundary day.
        # Docs with missing/empty documentDate must not be silently dropped:
        # null/""  ordering against an ISO date in Cypher returns null → WHERE
        # treats as false. Treat "date unknown" as "don't exclude".
        date_scope_clause = _document_date_scope_clause(
            "n.documentDate",
            op=tf.op,
            start=tf.start,
            end=tf.end,
        )
        if date_scope_clause:
            cypher_where.append(date_scope_clause)

        scope.cypher_where = cypher_where

        return scope

    def _compile_graph_skeleton(
        self, intent: IntentObject
    ) -> Optional[CompiledGraphSkeleton]:
        """Compile GraphSpec into a bounded query skeleton."""
        gs = intent.graph_spec
        if gs is None:
            return None

        skeleton = CompiledGraphSkeleton(
            graph_spec=gs,
            max_hops=self.config.max_hops,
        )

        # Map each variable to allowed KG0 labels via ontology
        for var in gs.vars:
            labels = self.config.ontology.kg0_labels_for(var.type)
            skeleton.var_labels[var.var] = labels

        # Map each edge to a pattern with hop bound
        for edge in gs.edges:
            edge_key = f"{edge.src}-[{edge.rel}]->{edge.dst}"
            skeleton.edge_patterns[edge_key] = {
                "src_var": edge.src,
                "dst_var": edge.dst,
                "rel_type": edge.rel,
                "resolved_rel_types": _resolved_edge_relationship_types(
                    self.config, edge.rel
                )
                or [],
                "hard": edge.hard,
                "max_hops": self.config.max_hops,
            }

        skeleton.compute_hash()
        return skeleton

    def _build_retrieval_query(
        self,
        intent: IntentObject,
        skeleton: Optional[CompiledGraphSkeleton] = None,
    ) -> CompiledRetrievalQuery:
        """
        Build a multi-backend query plan from RetrievalSpec.

        FIX #4: Incorporates GraphSpec variable hints and KG0 labels
        into query expansion, matching pseudocode line 5:
            q' <- BuildRetrievalQuery(q, I.EntityHints, I.RetrievalSpec, G)
        """
        spec = intent.retrieval_spec

        # Main query: query_text + expansions
        all_terms = [spec.query_text] + list(spec.query_expansions)

        # FIX #4: Expand query using GraphSpec variable hints + KG0 labels
        gs = intent.graph_spec
        if gs and skeleton:
            all_terms = _expanded_retrieval_terms(
                all_terms,
                gs.vars,
                skeleton.var_labels,
            )

        # Build an OR-joined query string from the expanded terms for
        # Solr lexical retrieval in Phase 1.
        solr_query = _build_solr_or_query(all_terms)
        solr_boost = ""
        field_boosts: Dict[str, float] = {}
        qdrant_vector = None
        if self.index.qdrant is not None and self.index.embedder is not None:
            embed_text = spec.query_text
            if spec.query_expansions:
                embed_text = (
                    f"{spec.query_text} {' '.join(spec.query_expansions[:5])}"
                )

            try:
                qdrant_vector = self.index.embedder.embed(embed_text)
            except Exception as e:
                logger.warning(
                    f"Embedding failed: {e}; semantic retrieval disabled"
                )
                qdrant_vector = None

        # Collect lowercased entity-name hints for diagnostics and any
        # compatibility consumers of the retrieval query plan.
        # 1. Expanded query terms (entity labels, variable hints, user expansions)
        # 2. Explicit entity-hint surface AND normalized forms from the intent.
        # Surface is the literal span; normalized is the canonical alias the
        # intent analyzer assigned. Both should feed retrieval so KG0 entities
        # stored under the canonical form (e.g., "DRUG ENFORCEMENT
        # ADMINISTRATION") match hints whose surface is the acronym ("DEA").
        hint_terms = _entity_hint_terms(all_terms, intent.entity_hints)

        # Required-ANY presence floor: doc must mention at least one surface/
        # normalized form of any high-confidence ORG or PERSON hint. Keeps the
        # pure-noise docs out when the intent has unambiguous subjects, and
        # degenerates to a no-op when it doesn't.
        required_hint_terms = _required_hint_terms(intent.entity_hints)

        return CompiledRetrievalQuery(
            solr_query=solr_query,
            solr_boost_query=solr_boost,
            solr_fields=list(field_boosts.keys()),
            qdrant_vector=qdrant_vector,
            entity_hint_terms=hint_terms,
            required_hint_terms=required_hint_terms,
            top_k_lex=spec.top_k_lex,
            top_k_sem=spec.top_k_sem,
            fusion_method=spec.fusion_method,
        )


# ============================================================
# Phase 1: Scope-Filtered Retrieval
# ============================================================
