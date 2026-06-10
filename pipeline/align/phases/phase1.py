from __future__ import annotations

from .shared import *  # noqa: F401,F403


class Phase1_ScopedRetrieval:
    """
    Execute Solr lexical and Qdrant semantic retrieval with scope filters.
    Fuse results via RRF or configured method.
    """

    def __init__(self, config: AlignConfig, index: IndexFacade):
        self.config = config
        self.index = index

    def execute(
        self,
        retrieval_query: CompiledRetrievalQuery,
        scope: CompiledScopePredicate,
    ) -> List[CandidateArtifact]:
        """Execute Phase 1: Solr lexical retrieval plus Qdrant semantic retrieval.

        Solr returns document-level lexical candidates with family,
        name/title, date, and text metadata where available.
        Qdrant page-level hits are converted to document candidates by
        ``payload.artifact_id`` before fusion.
        """
        logger.info("Phase 1: Executing scoped retrieval (Solr + Qdrant)")

        # --- Solr lexical retrieval ---
        graph = self.index.neo4j
        candidates = self.index.lexical_retrieve(retrieval_query, scope)
        logger.info(
            "  Solr lexical retrieval: %d raw candidates",
            len(candidates),
        )

        sem_candidates: List[CandidateArtifact] = []
        if (
            retrieval_query.qdrant_vector is not None
            and getattr(self.index, "qdrant", None) is not None
        ):
            try:
                sem_candidates = self.index.semantic_retrieve(
                    retrieval_query, scope
                )
            except Exception as exc:
                logger.warning(
                    "Semantic retrieval unavailable (%s): %s; "
                    "falling back to Solr lexical candidates.",
                    type(exc).__name__,
                    exc,
                )
                sem_candidates = []

            if sem_candidates:
                in_scope_sem = [
                    sc for sc in sem_candidates
                    if scope.evaluate(sc.metadata or {})
                ]
                dropped = len(sem_candidates) - len(in_scope_sem)
                if dropped:
                    logger.info(
                        "  Semantic retrieval: %d candidates (%d dropped "
                        "by in-memory scope filter)",
                        len(in_scope_sem), dropped,
                    )
                else:
                    logger.info(
                        f"  Semantic retrieval: {len(in_scope_sem)} candidates"
                    )
                sem_candidates = in_scope_sem

        if sem_candidates:
            candidates = self.index.union_and_score(
                candidates,
                sem_candidates,
                retrieval_query.fusion_method,
            )

        # Filter by minimum score
        min_score = float(getattr(self.config, "min_retrieval_score", 0.0) or 0.0)
        candidates = [c for c in candidates if c.fused_score >= min_score]

        # Backfill artifact_name from KG0 for candidates that lack it so
        # Phase 2+ and the final bundle carry human-readable artifact
        # identifiers.
        missing_name_ids = [
            c.artifact_id for c in candidates
            if not c.artifact_name
        ]
        if missing_name_ids and graph:
            try:
                name_cypher = (
                    "MATCH (d:Document) "
                    "WHERE d.kg_id IN $ids "
                    "RETURN d.kg_id AS id, coalesce(d.name, '') AS name"
                )
                name_rows = graph.execute_cypher(
                    name_cypher, {"ids": missing_name_ids}
                )
                name_map = {
                    str(row["id"]): str(row["name"])
                    for row in name_rows
                    if row.get("name")
                }
                backfilled = 0
                for c in candidates:
                    if not c.artifact_name and c.artifact_id in name_map:
                        c.artifact_name = name_map[c.artifact_id]
                        backfilled += 1
                if backfilled:
                    logger.info(
                        "  Backfilled artifact_name from KG0 for %d candidates",
                        backfilled,
                    )
            except Exception as exc:
                logger.debug(
                    "  artifact_name backfill failed (%s): %s",
                    type(exc).__name__, exc,
                )

        logger.info(f"  Phase 1 candidates after min-score filter: {len(candidates)}")
        return candidates


# ============================================================
# Phase 2: Artifact Set Selection
# ============================================================
