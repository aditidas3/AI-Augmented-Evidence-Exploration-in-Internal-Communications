"""
conflict.py — CONFLICT: Witness Contradiction Detection Operator
================================================================

Consumes the TRACE output already written into the Evidence Graph (EG)
and Reasoning Graph (RG), performs pairwise comparison of witness nodes,
detects contradictions using five typed rules, and writes results back
into the graph as CONTRADICTS edges, Defeater nodes, and Claim status
updates.

Pipeline position:
    ALIGN ──► TRACE ──► CONFLICT ──► Updated EG + RG

Negation detection strategy (Rule 3):
    Layer 1 — Regex patterns   : fast, catches simple cases ("not", "never")
    Layer 2 — spaCy dep parser : catches long-range negation via parse tree
    Layer 3 — NegSpacy         : catches negative quantifiers ("None", "Neither")

    If spaCy / NegSpacy are not installed, the operator falls back
    gracefully to regex-only mode. No crash, just reduced recall.

Five Conflict Detection Rules:
    Rule 1  SURFACE_MISMATCH      Same slot, different incompatible surfaces
    Rule 2  TEMPORAL_CLASH        Same slot, dates more than 6 months apart
    Rule 3  NEGATION_CONFLICT     One witness text negates the other's claim
    Rule 4  CROSS_ARTIFACT_ENTITY Same KG0 entity, different documents, clash
    Rule 5  RELIABILITY_DIVERGE   Same slot, large reliability score gap

Graph writes per conflict:
    EG     : (witness_a)-[:CONTRADICTS]->(witness_b)  + symmetric back-edge
    RG     : Defeater node  {type: rebutting | undercutting}
    RG     : (rg_root)-[:CONTAINS_DEFEATER]->(defeater)
    RG     : (inference)-[:HAS_DEFEATER]->(defeater)
    RG     : (defeater)-[:REFERENCES_CLAIM]->(weaker_claim)
    Bridge : (defeater)-[:REFERENCES_EVIDENCE]->(weaker_witness)
    RG     : Claim.status → 'contested'     (rebutting defeaters only)
    RG     : Claim.status → 'weakly-supported' (undercutting, via schema rule)
"""

from __future__ import annotations

import re
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── Deterministic UID namespace ───────────────────────────────────────────────
CONFLICT_NS = uuid.UUID("c9f3a821-5e47-4b2e-9d6c-1a2b3c4d5e6f")

# ── Optional spaCy import — graceful fallback if not installed ────────────────
try:
    import spacy as _spacy
    _SPACY_AVAILABLE = True
except ImportError:
    _spacy = None          # type: ignore
    _SPACY_AVAILABLE = False

try:
    from negspacy.negation import Negex as _Negex  # noqa: F401
    _NEGSPACY_AVAILABLE = True
except ImportError:
    _Negex = None           # type: ignore
    _NEGSPACY_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — Configuration
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ConflictConfig:
    """
    All tuneable settings for one CONFLICT execution.

    Attributes whose names start with ``tau_`` are numeric threshold
    gates — changing them makes detection stricter or more lenient.
    Attributes whose names start with ``enable_`` are on/off switches
    for individual rules.
    """

    # ── Schema bookkeeping ───────────────────────────────────────────────────
    schema_version: str = "1.0.0"
    graph_version: str  = "1.0.0"

    # ── Agent identity (the node that CONFLICT writes as the asserting agent) ─
    conflict_agent_uid:  str = "agent::conflict::detector"
    conflict_agent_name: str = "CONFLICT Witness Contradiction Detector"

    # ── Rule on/off switches ─────────────────────────────────────────────────
    enable_rule1_surface_mismatch:   bool = True
    enable_rule2_temporal_clash:     bool = True
    enable_rule3_negation:           bool = True
    enable_rule4_cross_artifact:     bool = True
    enable_rule5_reliability_diverge: bool = True

    # ── Negation backend (Rule 3) ─────────────────────────────────────────────
    # "auto"   → use spaCy+NegSpacy if available, else regex only
    # "spacy"  → require spaCy (raise if not installed)
    # "regex"  → regex only, never use spaCy
    negation_backend: str = "auto"

    # spaCy model name to load (must be downloaded separately)
    spacy_model: str = "en_core_web_sm"

    # ── Numeric thresholds ───────────────────────────────────────────────────
    # Minimum reliabilityScore a witness must have to be compared at all.
    # Witnesses below this are skipped entirely (too noisy to be useful).
    tau_min_witness_reliability: float = 0.05

    # Minimum absolute gap between two witnesses' reliabilityScores that
    # triggers a Rule 5 (undercutting) conflict.
    tau_reliability_gap: float = 0.40

    # Minimum number of characters a surface string must have.
    # Surfaces shorter than this are too vague to compare.
    tau_min_surface_length: int = 3

    # Minimum months apart for two dates to count as a temporal clash.
    tau_temporal_months: int = 6

    # Maximum pairs compared per slot group.
    # Prevents O(n²) blow-up when a slot has many witnesses.
    max_pairs_per_slot: int = 200

    # ── Behaviour flags ───────────────────────────────────────────────────────
    # Whether to mutate Claim.status = 'contested' for rebutting conflicts.
    update_claim_status: bool = True

    # Whether to write the symmetric CONTRADICTS back-edge (b → a).
    # The schema infers this automatically, but writing it explicitly
    # guarantees it exists even before entailment rules run.
    write_symmetric_contradicts: bool = True

    # ── Cross-run deduplication ──────────────────────────────────────────────
    # When True, CONFLICT skips a pair if a CONTRADICTS edge already exists
    # between the same two witness UIDs in the graph. This makes CONFLICT
    # idempotent — safe to run multiple times on the same data without
    # creating duplicate Defeater nodes or CONTRADICTS edges.
    enable_cross_run_dedup: bool = True

    # ── Pair sampling strategy ────────────────────────────────────────────────
    # When a slot group has more witnesses than max_pairs_per_slot,
    # use stratified sampling instead of a hard head-truncation cap.
    # Stratified sampling ensures high-reliability witnesses are always
    # compared and low-reliability ones are sampled proportionally.
    # Set to False to use the original head-truncation behaviour.
    use_stratified_sampling: bool = True

    # Rule 1 guard: skip surfaces that look like document reference IDs
    # rather than real slot answer text.
    # A surface is treated as a document ID when it is:
    #   - short (≤ tau_doc_id_max_length characters), AND
    #   - all lowercase alphanumeric with no spaces or punctuation
    # This prevents false SURFACE_MISMATCH conflicts when TRACE stores
    # corpus document identifiers (e.g. "zlcx0257") as witness surfaces
    # instead of the actual document title or evidence text.
    # Set to False to disable this guard if TRACE is fixed upstream.
    skip_document_id_surfaces: bool = True
    tau_doc_id_max_length:     int  = 12

    # ── Configurable thresholds (previously hardcoded) ───────────────────────
    # Token overlap threshold for Rule 1 Surface Mismatch.
    # Two surfaces with overlap >= this are treated as paraphrases — not conflicts.
    # Default 0.60 works for Walgreens. Lower for corpora with more variation.
    tau_surface_overlap:       float = 0.60

    # Token overlap threshold for Rule 4 Cross-Artifact Entity.
    # Lower than Rule 1 because cross-document wording legitimately varies.
    tau_cross_artifact_overlap: float = 0.50

    # Token overlap threshold for negation window check (Rule 3 Layer 1).
    tau_negation_window_overlap: float = 0.30

    # Configurable regex pattern for document ID detection.
    # Default matches Walgreens format: 4-12 char lowercase alphanumeric.
    # Override for other IDL collections:
    #   Tobacco: r'^[a-z0-9]{4,20}$'
    #   With hyphens: r'^[a-z0-9\-]{4,20}$'
    doc_id_pattern: str = r'^[a-z0-9]{4,12}$'

    # Configurable spaCy model name — swap for domain-specific model.
    # Download with: python -m spacy download en_core_web_sm
    # For legal domain: python -m spacy download en_core_web_lg
    # spacy_model is already defined above — doc_id_pattern is the new addition.

    # Corpus profile name — used in diagnostics for traceability.
    # Set this when running on a new collection so outputs are labelled.
    corpus_profile: str = "default"

    # ── EvalSupersession (Algorithm 6 — Line 14) ──────────────────────────────
    # When True, Rule 2 checks whether a temporal clash is actually
    # temporal supersession rather than a genuine contradiction.
    # A pair is classified as SUPERSEDES when the later document contains
    # explicit supersession language — "supersedes", "replaces", "amends",
    # "revokes", "effective from", "cancels".
    # SUPERSEDES produces an undercutting Defeater (lighter penalty)
    # rather than a rebutting Defeater (heavier penalty).
    # Especially important for collections spanning many decades (Tobacco).
    enable_eval_supersession: bool = True

    # Supersession keywords — extend for domain-specific language
    supersession_keywords: tuple = (
        "supersedes", "supersede", "superseding",
        "replaces", "replace", "replacing",
        "amends", "amend", "amended",
        "revokes", "revoke", "revoked",
        "effective from", "effective date",
        "cancels", "cancel", "cancelled",
        "pursuant to", "in lieu of",
    )

    # ── ClusterConflicts (Algorithm 6 — Line 22) ──────────────────────────────
    # When True, groups related raw conflicts into clusters before writing
    # Defeater nodes. Conflicts sharing the same contested Claim UID are
    # grouped into one cluster. CONSTRUCT receives one weighted Defeater
    # per cluster rather than one per raw pair.
    # Prevents over-penalisation when many conflicts point to the same
    # underlying disagreement (e.g. three legal instruments for one question).
    enable_cluster_conflicts: bool = True

    # ── ExpandConflictScope (Algorithm 6 — Lines 1-3) ─────────────────────────
    # When True, expands the witness set beyond direct TRACE chains by
    # querying KG0 for entities related to seed witnesses up to max_scope_hops.
    # Improves recall by finding conflicts the direct witness comparison misses.
    # Requires KG0 to be available at runtime.
    enable_expand_scope:  bool = False  # off by default — requires KG0 access
    max_scope_hops:       int  = 2


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — Data structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class WitnessRecord:
    """
    A flat snapshot of one EvidenceNode[Testimony] loaded from the graph.

    TRACE writes witness nodes with all fields inside domainMetadata.
    CONFLICT reads those fields once and stores them here so the rest
    of the algorithm works with plain Python objects instead of raw
    graph dicts.

    Fields
    ------
    uid              : the graph node UID
    slot_type        : WHO / WHAT / WHEN / WHERE / HOW / EVIDENCE / OUTCOME
    var_name         : the graph variable this witness filled (e.g. "P", "D")
    surface          : the exact text span that provided the answer
    content_excerpt  : the full justification sentence from ALIGN
    reliability_score: normalised [0, 1] binding confidence from ALIGN
    kg0_entity_id    : KG0 graph node ID if the mention was entity-linked
    anchor_id        : the anchor (text location) this witness came from
    artifact_id      : the document, derived from anchor_id prefix
    quality          : GROUNDED / INFERRED / AMBIGUOUS
    claim_uid        : the RG Claim that this witness grounds via GROUNDED_BY
    raw              : the full properties dict for anything not extracted above
    """
    uid:               str
    slot_type:         str
    var_name:          str
    surface:           str
    content_excerpt:   str
    reliability_score: float
    kg0_entity_id:     str
    anchor_id:         str
    artifact_id:       str
    quality:           str
    claim_uid:         str
    raw:               Dict[str, Any]


@dataclass
class ConflictRecord:
    """
    One detected contradiction between two witnesses.

    Produced by a rule method and consumed by _phase3_write to
    create the appropriate graph nodes and edges.

    Fields
    ------
    conflict_id      : deterministic UID for this conflict pair + rule
    rule             : which rule fired (SURFACE_MISMATCH, etc.)
    defeater_type    : 'rebutting' (hard conflict) or 'undercutting' (soft)
    witness_a_uid    : first witness in the pair
    witness_b_uid    : second witness in the pair
    description      : human-readable explanation of why this is a conflict
    confidence       : how certain we are [0, 1]
    weaker_witness_uid: the witness with lower reliability_score
    claim_a_uid      : Claim grounded by witness A
    claim_b_uid      : Claim grounded by witness B
    """
    conflict_id:        str
    rule:               str
    defeater_type:      str
    witness_a_uid:      str
    witness_b_uid:      str
    description:        str
    confidence:         float
    weaker_witness_uid: str
    claim_a_uid:        str
    claim_b_uid:        str
    # ── Negation metadata (Rule 3 only — empty string for other rules) ────────
    # negation_type  : linguistic style of negation detected
    #   "explicit_keyword"   — regex caught it ("never", "did not", "denied")
    #   "long_range_clausal" — spaCy dep=neg caught it (negation governs
    #                          a clause far from the negation word)
    #   "explicit_quantifier"— custom Layer 3 caught it ("None", "Neither",
    #                          "Nobody" as grammatical subject)
    #   ""                   — not a negation conflict
    # negation_cue   : the specific word or phrase that triggered detection
    #   e.g. "never", "not", "None"
    # negation_layer : which detection layer found the negation
    #   "regex", "spacy_dep_neg", "custom_quantifier"
    negation_type:      str = ""
    negation_cue:       str = ""
    negation_layer:     str = ""


@dataclass
class ConflictResult:
    """
    The complete output of one CONFLICT execution.

    Fields
    ------
    conflicts                  : every ConflictRecord that was detected
    defeater_uids              : UIDs of Defeater nodes written to the RG
    contradicts_edges_written  : number of CONTRADICTS edges written to the EG
    claims_contested           : UIDs of Claims whose status was set to 'contested'
    diagnostics                : INFO / WARNING log entries from the run
    stats                      : summary counts keyed by rule name
    """
    conflicts:                 List[ConflictRecord]
    defeater_uids:             List[str]
    contradicts_edges_written: int
    claims_contested:          List[str]
    diagnostics:               List[Dict[str, Any]]
    stats:                     Dict[str, int]


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — Negation engine (spaCy + NegSpacy + regex)
# ═══════════════════════════════════════════════════════════════════════════════

class NegationEngine:
    """
    Three-layer negation detector used by Rule 3.

    Layer 1 — Regex
        Scans for explicit negation keywords ("not", "never", "denied"…).
        Very fast. Works for simple, direct negations.
        Limitation: only looks within a 160-character window around the
        negation word, so it misses long-range negations.

    Layer 2 — spaCy dependency parser
        Parses the sentence grammar and finds tokens with dep='neg'.
        Then walks the full subtree of the negated head verb to get the
        complete negated span, regardless of character distance.
        Handles: long-range negation, clausal negation.

    Layer 3 — NegSpacy
        Extends spaCy with negative quantifier detection.
        Handles: "None of the studies…", "Neither company…",
                 "No evidence was found…"
        Also adds pseudo-negation filtering ("not only… but also") and
        scope boundary detection ("but", "however" end the negation scope).

    Usage
    -----
        engine = NegationEngine(cfg)
        engine.load()                          # call once at startup
        found = engine.has_negation_about(text, target_surface)
    """

    # ── Regex negation patterns ───────────────────────────────────────────────
    # Each pattern is a word-boundary-anchored phrase that signals negation.
    # \b ensures we match whole words only ("not" never matches "notable").
    REGEX_PATTERNS = [
        r"\bdid not\b",          # "the company did not fund"
        r"\bdoes not\b",         # "this does not indicate"
        r"\bnever\b",            # "they never acknowledged"
        r"\bno evidence\b",      # "no evidence of funding was found"
        r"\bdenied\b",           # "executives denied involvement"
        r"\bdenies\b",           # "the memo denies any link"
        r"\bcontradicts\b",      # "this contradicts the earlier claim"
        r"\bnot\s+\w+ed\b",      # "not approved", "not funded", "not verified"
        r"\bfailed to\b",        # "failed to disclose"
        r"\bno\s+\w+\s+was\b",   # "no approval was", "no study was"
    ]

    # ── NegSpacy termset — domain-tuned for tobacco/legal documents ───────────
    # These lists tell NegSpacy what words trigger negation, what words end
    # a negation scope, and what phrases look like negation but are not.
    NEGSPACY_TERMSET = {
        # Words and phrases that start a negation
        "preceding_negations": [
            "never", "no", "none", "not", "neither", "nobody",
            "nothing", "nowhere", "denied", "denies", "failed to",
            "no evidence", "not the case", "contrary to",
        ],
        # Words that end a negation scope (the negation does not extend past these)
        "termination": [
            "but", "however", "although", "except", "yet",
            "nevertheless", "notwithstanding", "while",
        ],
        # Phrases that look like negation but are not
        "pseudo_negations": [
            "not only", "not always", "not necessarily",
            "not just", "not merely", "not limited to",
        ],
        # Words that follow a noun/verb and signal negation
        "following_negations": [
            "unlikely", "absent", "excluded", "ruled out",
        ],
    }

    def __init__(self, cfg: ConflictConfig) -> None:
        self.cfg   = cfg
        self._nlp  = None   # spaCy Language object, loaded lazily
        self._mode = "regex"  # actual mode after load()

    def load(self) -> None:
        """
        Load the spaCy model and add NegSpacy if available.
        Called once at Conflict.__init__ time.

        Sets self._mode to one of:
            "spacy+negspacy"  — full three-layer detection
            "spacy"           — spaCy only (NegSpacy not installed)
            "regex"           — regex only (spaCy not installed)
        """
        backend = self.cfg.negation_backend

        # If the user explicitly chose regex-only, skip spaCy entirely.
        if backend == "regex":
            self._mode = "regex"
            logger.info("[CONFLICT] Negation backend: regex-only (by config)")
            return

        if not _SPACY_AVAILABLE:
            if backend == "spacy":
                raise RuntimeError(
                    "negation_backend='spacy' but spaCy is not installed. "
                    "Run: pip install spacy && python -m spacy download en_core_web_sm"
                )
            self._mode = "regex"
            logger.warning("[CONFLICT] spaCy not installed — using regex-only negation")
            return

        try:
            self._nlp = _spacy.load(self.cfg.spacy_model)
        except OSError:
            logger.warning(
                "[CONFLICT] spaCy model '%s' not found. "
                "Run: python -m spacy download %s  "
                "Falling back to regex-only negation.",
                self.cfg.spacy_model, self.cfg.spacy_model,
            )
            self._mode = "regex"
            return

        # Try to add NegSpacy pipeline component
        if _NEGSPACY_AVAILABLE:
            try:
                self._nlp.add_pipe(
                    "negex",
                    config={"neg_termset": self.NEGSPACY_TERMSET},
                )
                self._mode = "spacy+negspacy"
                logger.info("[CONFLICT] Negation backend: spaCy + NegSpacy")
            except Exception as exc:
                logger.warning(
                    "[CONFLICT] Could not add NegSpacy pipe (%s) — using spaCy only", exc
                )
                self._mode = "spacy"
        else:
            self._mode = "spacy"
            logger.info("[CONFLICT] Negation backend: spaCy only (NegSpacy not installed)")

    # ── Public method ─────────────────────────────────────────────────────────

    # ── Negative quantifier words (custom Layer 3) ───────────────────────────
    # These words carry negation semantics as grammatical subjects.
    # spaCy tags them as nsubj (subject), not dep=neg, so the standard
    # dep=neg scan misses them. Layer 3 detects them directly.
    NEGATIVE_QUANTIFIERS = {
        "none", "neither", "nobody", "nothing", "nowhere", "no-one"
    }

    # ── Pseudo-negation phrases — must be blocked before all layers ───────────
    # These phrases contain negation words but are NOT real negations.
    # "not only...but also" is additive, "not always" is a hedge.
    # The guard checks these first so spaCy never gets a chance to
    # fire incorrectly on them.
    PSEUDO_NEGATIONS = {
        "not only", "not always", "not necessarily",
        "not just", "not merely", "not limited to",
    }

    def has_negation_about(
        self, text: str, target_surface: str
    ) -> Dict[str, Any]:
        """
        Detect whether `text` contains a negation covering `target_surface`.

        Returns a dict with:
            found          : bool   — True if negation detected
            negation_type  : str    — "explicit_keyword" | "long_range_clausal"
                                      | "explicit_quantifier" | ""
            negation_cue   : str    — the word that triggered detection
            negation_layer : str    — "regex" | "spacy_dep_neg"
                                      | "custom_quantifier" | ""

        Layers run in order — returns on first match (fast path):
            Guard  — pseudo-negation check (blocks "not only", "not always")
            Layer 1 — regex patterns (fast, simple cases)
            Layer 2 — spaCy dep=neg (long-range clausal negation)
            Layer 3 — custom quantifier handler (None, Neither, Nobody)

        Parameters
        ----------
        text           : witness text to scan (content_excerpt + surface)
        target_surface : the other witness's surface to check against
        """
        _no = {"found": False, "negation_type": "",
               "negation_cue": "", "negation_layer": ""}

        if not text or not target_surface:
            return _no

        text_lower = text.lower()

        # ── Pseudo-negation guard — runs before everything else ───────────────
        # If the text contains a pseudo-negation phrase, block immediately.
        # This prevents spaCy from firing on "not only" and "not always".
        for pseudo in self.PSEUDO_NEGATIONS:
            if pseudo in text_lower:
                return _no

        # ── Layer 1 — Regex ───────────────────────────────────────────────────
        result = self._regex_negation(text, target_surface)
        if result["found"]:
            return result

        # ── Layer 2 + 3 — spaCy (only if model loaded) ───────────────────────
        if self._nlp is not None:
            result = self._spacy_negation(text, target_surface)
            if result["found"]:
                return result

        return _no

    # ── Layer 1: regex ────────────────────────────────────────────────────────

    def _regex_negation(self, text: str, target_surface: str) -> Dict[str, Any]:
        """
        Layer 1 — Regex negation detection.

        Scans `text` for each negation pattern. When a pattern matches,
        extracts a 160-character window (80 chars before + 80 after the
        match position) and checks Jaccard overlap with target_surface.

        Returns a result dict:
            found          : True if negation detected
            negation_type  : "explicit_keyword"
            negation_cue   : the matched pattern text (e.g. "never")
            negation_layer : "regex"

        Limitation: only catches negation within 160 chars of the keyword.
        Long-range negations are handled by Layer 2 (spaCy dep=neg).
        """
        _no = {"found": False, "negation_type": "",
               "negation_cue": "", "negation_layer": ""}
        text_lower    = text.lower()
        surface_lower = target_surface.lower()

        for pat in self.REGEX_PATTERNS:
            match = re.search(pat, text_lower)
            if not match:
                continue
            neg_pos = match.start()
            nearby  = text_lower[max(0, neg_pos - 80) : neg_pos + 80]
            if _token_overlap(surface_lower, nearby) >= self.cfg.tau_negation_window_overlap:
                return {
                    "found":          True,
                    "negation_type":  "explicit_keyword",
                    "negation_cue":   match.group().strip(),
                    "negation_layer": "regex",
                }

        return _no

    # ── Layer 2 + 3: spaCy + NegSpacy ────────────────────────────────────────

    def _spacy_negation(self, text: str, target_surface: str) -> Dict[str, Any]:
        """
        Layer 2 + 3 — spaCy dependency tree negation detection.

        Layer 2: spaCy dep=neg
            Finds tokens with dep="neg" (grammatical negation modifiers
            like "not", "never"). Walks the full subtree of the negated
            head verb to get the complete negated scope regardless of
            character distance from the negation word.

        Layer 3: Custom negative quantifier handler
            Handles "None of the studies...", "Neither X nor Y...",
            "Nobody authorised..." — words that carry negation as
            grammatical subjects (dep=nsubj), not as dep=neg modifiers.
            spaCy alone misses these. NegSpacy also misses them for
            general document text without named entities.
            The fix: detect quantifier words directly from our list,
            check their grammatical role, and walk the governing verb's
            subtree to get the negated scope.

        Returns a result dict:
            found          : True if negation detected
            negation_type  : "long_range_clausal" | "explicit_quantifier"
            negation_cue   : the negation word (e.g. "not", "None")
            negation_layer : "spacy_dep_neg" | "custom_quantifier"
        """
        _no = {"found": False, "negation_type": "",
               "negation_cue": "", "negation_layer": ""}

        surface_tokens = set(re.findall(r"[a-z0-9]+", target_surface.lower()))
        if not surface_tokens:
            return _no

        doc = self._nlp(text)

        # ── Layer 2: spaCy dep=neg ────────────────────────────────────────────
        # Handles: "never funded", "did not approve",
        #          "It is not the case...that the company acknowledged"
        # The subtree walk gets the full negated clause regardless of
        # how far the negated content is from the negation word.
        for token in doc:
            if token.dep_ != "neg":
                continue
            scope_tokens = {t.text.lower() for t in token.head.subtree}
            overlap      = scope_tokens & surface_tokens
            if len(overlap) / max(len(surface_tokens), 1) >= self.cfg.tau_negation_window_overlap:
                return {
                    "found":          True,
                    "negation_type":  "long_range_clausal",
                    "negation_cue":   token.text.lower(),
                    "negation_layer": "spacy_dep_neg",
                }

        # ── Layer 3: Custom negative quantifier handler ───────────────────────
        # Handles: "None of the studies concluded..."
        #          "Neither the division nor the department disclosed..."
        #          "Nobody at the executive level authorised..."
        #
        # How it works:
        #   1. Check if any token is in our NEGATIVE_QUANTIFIERS set
        #   2. Check its grammatical role — must be a subject (nsubj)
        #      because quantifiers act as subjects of the negated verb
        #   3. Walk the full subtree of the governing verb (token.head)
        #      to get everything the quantifier negates
        #   4. Check overlap with target surface
        for token in doc:
            if token.text.lower() not in self.NEGATIVE_QUANTIFIERS:
                continue
            # Must be a subject — quantifiers govern verbs as subjects
            if token.dep_ not in ("nsubj", "nsubjpass", "expl"):
                continue
            # Walk the subtree of the governing verb
            scope_tokens = {t.text.lower() for t in token.head.subtree}
            overlap      = scope_tokens & surface_tokens
            if len(overlap) / max(len(surface_tokens), 1) >= self.cfg.tau_negation_window_overlap:
                return {
                    "found":          True,
                    "negation_type":  "explicit_quantifier",
                    "negation_cue":   token.text.lower(),
                    "negation_layer": "custom_quantifier",
                }

        return _no


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — Shared helper functions
# ═══════════════════════════════════════════════════════════════════════════════

def _token_overlap(text_a: str, text_b: str) -> float:
    """
    Jaccard similarity between the token sets of two strings.

    Jaccard = |intersection| / |union|

    Returns a float between 0.0 (no shared tokens) and 1.0 (identical
    token sets). Used throughout the rules to measure how similar two
    text spans are without caring about word order.

    Example
    -------
        _token_overlap("funded research", "never funded health research")
        # tokens_a = {funded, research}
        # tokens_b = {never, funded, health, research}
        # intersection = {funded, research}  → 2 tokens
        # union = {never, funded, health, research}  → 4 tokens
        # Jaccard = 2/4 = 0.50
    """
    tokens_a = set(re.findall(r"[a-z0-9]+", text_a.lower()))
    tokens_b = set(re.findall(r"[a-z0-9]+", text_b.lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _extract_date(text: str) -> str:
    """
    Extract the first recognisable date string from `text`.

    Tries three formats in priority order:
        1. ISO date      e.g. "2020-01-15"
        2. Month + year  e.g. "January 1994" or "Jan 1994"
        3. Year only     e.g. "1994"

    Returns the matched string, or "" if no date is found.
    Only the first match is returned — later dates in the same text
    are ignored. This is a known limitation (see documentation).
    """
    if not text:
        return ""
    # Priority 1 — ISO date (most specific)
    m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if m:
        return m.group(1)
    # Priority 2 — Month name + 4-digit year
    m = re.search(
        r"\b(January|February|March|April|May|June|July|August|"
        r"September|October|November|December|"
        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\s+(\d{4})\b",
        text, re.IGNORECASE,
    )
    if m:
        return f"{m.group(1)} {m.group(2)}"
    # Priority 3 — Year only (least specific)
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    if m:
        return m.group(1)
    return ""


def _parse_year_month(date_str: str) -> Tuple[int, int]:
    """
    Convert an extracted date string to a (year, month) integer tuple.

    This lets us subtract dates to get the number of months apart,
    which is what Rule 2 uses to detect temporal clashes.

    For year-only dates, month is set to 6 (mid-year) so that two
    year-only dates produce a reasonable approximate gap.

    Raises ValueError if the string cannot be parsed.
    """
    month_map = {
        "january": 1,  "february": 2, "march": 3,    "april": 4,
        "may": 5,      "june": 6,     "july": 7,     "august": 8,
        "september": 9,"october": 10, "november": 11,"december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9,
        "oct": 10,"nov": 11,"dec": 12,
    }
    # ISO format: "2020-01"  or  "2020-01-15"
    m = re.match(r"(\d{4})-(\d{2})", date_str)
    if m:
        return int(m.group(1)), int(m.group(2))
    # "Month Year" format: "March 1994"
    parts = date_str.lower().split()
    if len(parts) == 2:
        month = month_map.get(parts[0], 0)
        if month and parts[1].isdigit():
            return int(parts[1]), month
    # Year only: "1994"
    if re.match(r"^\d{4}$", date_str.strip()):
        return int(date_str.strip()), 6
    raise ValueError(f"Cannot parse date: {date_str!r}")


def _deterministic_uid(key: str) -> str:
    """UUID v5 from CONFLICT_NS + key. Same input always gives same output."""
    return str(uuid.uuid5(CONFLICT_NS, key))


def _edge_uid(from_uid: str, to_uid: str, rel_type: str) -> str:
    """Deterministic UID for a graph edge based on its endpoints and type."""
    return _deterministic_uid(f"{from_uid}::{rel_type}::{to_uid}")


def _now() -> str:
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 5 — The CONFLICT algorithm
# ═══════════════════════════════════════════════════════════════════════════════

class Conflict:
    """
    Stateful, single-use operator.

    Lifecycle: instantiate → execute() → discard.
    Do not call execute() twice on the same instance.

    Parameters
    ----------
    eg     : GraphWriter — Evidence Graph writer (read + write)
    rg     : GraphWriter — Reasoning Graph writer (read + write)
    bridge : GraphWriter — cross-graph edge writer (defaults to eg)
    cfg    : ConflictConfig — optional settings override
    """

    def __init__(
        self,
        eg:     Any,
        rg:     Any,
        bridge: Any = None,
        cfg:    Optional[ConflictConfig] = None,
    ) -> None:
        self.eg     = eg
        self.rg     = rg
        self.bridge = bridge or eg
        self.cfg    = cfg or ConflictConfig()

        # Load negation engine (spaCy model, if configured)
        self._negation = NegationEngine(self.cfg)
        self._negation.load()

        # ── Internal indexes built by Phase 1 ────────────────────────────────
        self._witnesses:    List[WitnessRecord]                    = []
        self._by_slot:      Dict[str, List[WitnessRecord]]         = {}
        self._by_slot_var:  Dict[Tuple[str,str], List[WitnessRecord]] = {}
        self._by_kg0:       Dict[str, List[WitnessRecord]]         = {}
        # _by_slot_var groups witnesses by (slot_type, var_name) together.
        # This powers the var_name aware Rule 1 — only witnesses filling
        # the same graph variable in the same slot are compared as potential
        # contradictions. Two witnesses filling different variables (P vs RESP)
        # within the same slot are complementary answers, not contradictions.

        # ── Results accumulated across phases ────────────────────────────────
        self._conflicts:        List[ConflictRecord]   = []
        self._defeater_uids:    List[str]              = []
        self._claims_contested: List[str]              = []

        # Cross-run deduplication — tracks witness pairs already processed
        # so re-running CONFLICT on the same data never creates duplicates.
        self._seen_pairs:       Set[Tuple[str, str]]   = set()
        self._diags:            List[Dict[str, Any]]   = []

        # ── Idempotency guards ────────────────────────────────────────────────
        # These sets prevent writing the same edge or node twice if execute()
        # is somehow called on overlapping data.
        self._seen_contradicts: Set[Tuple[str, str]] = set()
        self._seen_defeaters:   Set[str]             = set()

    # ─────────────────────────────────────────────────────────────────────────
    #  Public entry point
    # ─────────────────────────────────────────────────────────────────────────

    def execute(
        self,
        trace_result: Any,
        rg_root_uid:  str,
        eg_root_uid:  str,
    ) -> ConflictResult:
        """
        Run the full CONFLICT pipeline: load → detect → write → return.

        Parameters
        ----------
        trace_result : TraceResult from trace.py
        rg_root_uid  : UID of the RG GraphRoot (for CONTAINS_DEFEATER edges)
        eg_root_uid  : UID of the EG GraphRoot (for evidenceGraphId on edges)

        Returns
        -------
        ConflictResult with all detected conflicts, graph write counts,
        and diagnostic messages.
        """
        self._ensure_agent()

        # ── Phase 1: load witnesses from the graph ────────────────────────
        self._phase1_load_witnesses(trace_result)

        if not self._witnesses:
            self._diag("WARNING", "CONFLICT_NO_WITNESSES",
                       "No witnesses found — CONFLICT is a no-op")
            return self._finalise()

        self._diag("INFO", "CONFLICT_LOADED",
                   f"{len(self._witnesses)} witnesses in "
                   f"{len(self._by_slot)} slot groups, "
                   f"{len(self._by_kg0)} KG0 entity groups")

        # ── Phase 2: pairwise comparison — apply all five rules ───────────
        self._phase2_detect()

        if not self._conflicts:
            self._diag("INFO", "CONFLICT_NONE_FOUND",
                       "No contradictions detected")
            return self._finalise()

        self._diag("INFO", "CONFLICT_FOUND",
                   f"{len(self._conflicts)} conflicts — writing to graph")

        # ── Phase 3: write results back to EG + RG + bridge ───────────────
        self._phase3_write(rg_root_uid, eg_root_uid)

        return self._finalise()

    # ─────────────────────────────────────────────────────────────────────────
    #  Phase 1 — Load and index witnesses
    # ─────────────────────────────────────────────────────────────────────────

    def _phase1_load_witnesses(self, trace_result: Any) -> None:
        """
        Read all EvidenceNode[Testimony] nodes from the EG writer and
        convert them into WitnessRecord objects.

        TRACE stores each witness's metadata inside the node's
        domainMetadata dict. This method extracts those fields.

        Two indexes are built:
            _by_slot  : slot_type  → [WitnessRecord, ...]
            _by_kg0   : kg0_entity_id → [WitnessRecord, ...]

        Rules 1, 2, 3, 5 iterate _by_slot (same question, same slot).
        Rule 4 iterates _by_kg0 (same real-world entity, any slot).
        """
        all_nodes = getattr(self.eg, "nodes", [])

        for node in all_nodes:
            # Skip anything that is not an EvidenceNode[Testimony]
            if "EvidenceNode" not in node.get("labels", []):
                continue
            props = node.get("properties", {})
            if "Witness" not in props.get("domainType", ""):
                continue

            uid = props.get("uid", "")
            if not uid:
                continue

            # Drop witnesses below the minimum reliability threshold
            rel_score = float(props.get("reliabilityScore", 0.0) or 0.0)
            if rel_score < self.cfg.tau_min_witness_reliability:
                continue

            dm         = props.get("domainMetadata", {}) or {}
            slot_type  = str(dm.get("slot_type",  "") or "").upper()
            var_name   = str(dm.get("var_name",   "") or "")
            anchor_id  = str(dm.get("anchor_id",  "") or "")
            surface    = str(dm.get("surface",    "") or props.get("contentExcerpt", "") or "")
            kg0_id     = str(dm.get("kg0_entity_id", "") or "")
            quality    = str(dm.get("quality",    "AMBIGUOUS") or "AMBIGUOUS")

            # Prefer TRACE's explicit artifact id; anchor-derived ids are a
            # compatibility fallback for old bundles.
            artifact_id = str(dm.get("artifact_id", "") or "")
            if not artifact_id:
                artifact_id = anchor_id.split("::")[0] if "::" in anchor_id else anchor_id

            # Find which RG Claim this witness grounds (via GROUNDED_BY edge)
            claim_uid = self._find_claim_for_witness(uid)

            # Drop surfaces that are too short to be meaningful
            if len(surface) < self.cfg.tau_min_surface_length:
                continue

            rec = WitnessRecord(
                uid=uid, slot_type=slot_type, var_name=var_name,
                surface=surface,
                content_excerpt=str(props.get("contentExcerpt", "") or ""),
                reliability_score=rel_score, kg0_entity_id=kg0_id,
                anchor_id=anchor_id, artifact_id=artifact_id,
                quality=quality, claim_uid=claim_uid, raw=props,
            )

            self._witnesses.append(rec)

            if slot_type:
                self._by_slot.setdefault(slot_type, []).append(rec)
                # Also index by (slot_type, var_name) for var_name aware Rule 1.
                # var_name is the graph variable e.g. "P", "R", "RESP", "CL".
                # If var_name is empty, use slot_type alone as the key so the
                # witness still participates in comparison.
                var_key = (slot_type, var_name) if var_name else (slot_type, "__no_var__")
                self._by_slot_var.setdefault(var_key, []).append(rec)
            if kg0_id:
                self._by_kg0.setdefault(kg0_id, []).append(rec)

        self._diag("INFO", "CONFLICT_PHASE1",
                   f"Loaded {len(self._witnesses)} witnesses across "
                   f"{len(self._by_slot)} slot groups, "
                   f"{len(self._by_slot_var)} slot+var groups")

        # ── ExpandConflictScope (Algorithm 6 Lines 1-3) ──────────────────────
        # If KG0 access is enabled, expand scope to include contextually
        # related objects beyond direct TRACE chains.
        # This improves recall by finding conflicts the direct witness
        # comparison would otherwise miss.
        if self.cfg.enable_expand_scope:
            self._expand_conflict_scope()

        # ── Cross-run dedup: load existing CONTRADICTS edges ──────────────────
        # If this is a re-run, skip pairs already written to the graph
        # so no duplicate Defeater nodes or CONTRADICTS edges are created.
        if self.cfg.enable_cross_run_dedup:
            n_existing = 0
            for edge in getattr(self.eg, "edges", []):
                if edge.get("type") == "CONTRADICTS":
                    wa = edge.get("from", "")
                    wb = edge.get("to",   "")
                    if wa and wb:
                        self._seen_pairs.add((wa, wb))
                        self._seen_pairs.add((wb, wa))
                        n_existing += 1
            if n_existing:
                self._diag("INFO", "CROSS_RUN_DEDUP",
                           f"Found {n_existing} existing CONTRADICTS edges — "
                           f"these pairs will be skipped this run")

    def _find_claim_for_witness(self, witness_uid: str) -> str:
        """
        Scan bridge edges to find the Claim grounded by this witness.

        The bridge contains edges of the form:
            (Claim)-[:GROUNDED_BY]->(EvidenceNode[Testimony])

        So we look for a GROUNDED_BY edge whose 'to' end is our witness.
        """
        for edge in getattr(self.bridge, "edges", []):
            if edge.get("type") == "GROUNDED_BY" and edge.get("to") == witness_uid:
                return edge.get("from", "")
        return ""

    # ─────────────────────────────────────────────────────────────────────────
    #  Phase 2 — Pairwise detection
    # ─────────────────────────────────────────────────────────────────────────

    def _expand_conflict_scope(self) -> None:
        """
        ExpandConflictScope — Algorithm 6 Lines 1-3.

        Expands the witness set beyond direct TRACE chains by querying
        KG0 for entities related to seed witnesses up to max_scope_hops.

        Currently a stub — requires KG0 to be passed to CONFLICT at
        runtime. Set enable_expand_scope=True and pass kg0_writer to
        activate. Planned for a future iteration.

        When implemented this would:
            1. Collect KG0 entity IDs from all loaded witnesses
            2. Query KG0 for entities within max_scope_hops hops
            3. Load any additional witnesses found for those entities
            4. Add them to self._witnesses and rebuild slot indexes

        This improves recall — conflicts between related entities that
        are not in the same direct TRACE chain become detectable.
        """
        self._diag("INFO", "EXPAND_SCOPE",
                   f"ExpandConflictScope is enabled but KG0 not yet "
                   f"wired — skipping scope expansion this run. "
                   f"Pass kg0_writer to Conflict() to activate.")

    def _eval_supersession(
        self, a: "WitnessRecord", b: "WitnessRecord"
    ):
        """
        EvalSupersession — Algorithm 6 Line 14.

        Checks whether a REFUTES pair (temporal clash) is actually temporal
        supersession — i.e. the later document explicitly replaces the earlier.

        Returns:
            (True, cue_word)  if supersession language is detected
            (False, "")       if this is a genuine contradiction

        How it works:
            1. Determine which witness is later (higher year)
            2. Search the later witness's content_excerpt for supersession keywords
            3. If found return True with the triggering keyword

        This is called by Rule 2 before returning a TEMPORAL_CLASH record.
        If supersession is detected Rule 2 returns TEMPORAL_SUPERSESSION
        (undercutting) instead of TEMPORAL_CLASH (rebutting).
        """
        try:
            ya, _ = _parse_year_month(
                _extract_date(a.content_excerpt) or _extract_date(a.surface) or ""
            )
            yb, _ = _parse_year_month(
                _extract_date(b.content_excerpt) or _extract_date(b.surface) or ""
            )
        except (ValueError, TypeError):
            return False, ""

        # Later witness is the one with the higher year
        later_witness = b if yb >= ya else a
        search_text   = (
            (later_witness.content_excerpt or "") + " " +
            (later_witness.surface or "")
        ).lower()

        for kw in self.cfg.supersession_keywords:
            if kw.lower() in search_text:
                return True, kw

        return False, ""

    def _cluster_conflicts(self) -> None:
        """
        ClusterConflicts — Algorithm 6 Line 22.

        Groups raw conflict pairs that share the same contested Claim UID
        into clusters. Each cluster produces ONE Defeater node with a
        cluster_size field instead of one Defeater per raw pair.

        This prevents over-penalisation in CONSTRUCT when many conflicts
        all point to the same underlying disagreement.

        For example — three WHAT slot conflicts on the Walgreens sample:
            Before: 3 Defeaters -> CONSTRUCT applies 3x rebutting penalties
            After:  1 Defeater (cluster_size=3) -> 1 weighted penalty

        Updates self._conflicts in place — clusters replace their members.
        If enable_cluster_conflicts=False this is a no-op.
        """
        if not self.cfg.enable_cluster_conflicts:
            return
        if len(self._conflicts) <= 1:
            return

        from collections import defaultdict

        # Group by the contested Claim UID (claim_a_uid — both usually same slot)
        clusters: dict = defaultdict(list)
        unclustered = []
        for c in self._conflicts:
            key = c.claim_a_uid or c.claim_b_uid or c.conflict_id
            clusters[key].append(c)

        new_conflicts = []
        n_clustered   = 0

        for claim_uid, members in clusters.items():
            if len(members) == 1:
                new_conflicts.append(members[0])
                continue

            # Multiple raw conflicts share this Claim — create cluster record
            n_clustered += len(members)
            representative = members[0]  # use first as representative
            cluster_descs  = " | ".join(m.description[:60] for m in members[:3])
            if len(members) > 3:
                cluster_descs += f" ... (+{len(members)-3} more)"

            clustered = ConflictRecord(
                conflict_id     = _deterministic_uid(
                    f"cluster::{claim_uid}::{len(members)}"),
                rule            = representative.rule,
                defeater_type   = representative.defeater_type,
                witness_a_uid   = representative.witness_a_uid,
                witness_b_uid   = representative.witness_b_uid,
                description     = (
                    f"[CLUSTER size={len(members)}] {cluster_descs}"
                ),
                confidence      = max(m.confidence for m in members),
                weaker_witness_uid = representative.weaker_witness_uid,
                claim_a_uid     = representative.claim_a_uid,
                claim_b_uid     = representative.claim_b_uid,
                # Extra metadata for CONSTRUCT
                negation_type   = "",
                negation_cue    = "",
                negation_layer  = "",
            )
            # Store cluster_size as an attribute for CONSTRUCT Rule 3
            object.__setattr__(clustered, "cluster_size", len(members))                 if hasattr(clustered, "__dataclass_fields__") else None
            new_conflicts.append(clustered)

        if n_clustered > 1:
            self._diag("INFO", "CLUSTER_CONFLICTS",
                       f"Clustered {n_clustered} raw conflicts into "
                       f"{len(new_conflicts)} cluster(s)")

        self._conflicts = new_conflicts

    def _is_duplicate_pair(self, uid_a: str, uid_b: str) -> bool:
        """
        Cross-run deduplication check.
        Returns True if this pair was already detected in a previous run
        (a CONTRADICTS edge already exists between these two witnesses).
        Skips the pair so no duplicate Defeater is created.
        """
        if not self.cfg.enable_cross_run_dedup:
            return False
        key_ab = (uid_a, uid_b)
        key_ba = (uid_b, uid_a)
        return key_ab in self._seen_pairs or key_ba in self._seen_pairs

    @staticmethod
    def _pair_key(uid_a: str, uid_b: str) -> Tuple[str, str]:
        return tuple(sorted([uid_a, uid_b]))

    def _stratified_sample(
        self,
        witnesses: list,
        max_pairs: int,
    ) -> list:
        """
        Stratified pair sampling — replacement for hard head-truncation.

        Instead of taking the first max_pairs pairs, this ensures:
          - High-reliability witnesses (top 50%) are always compared
          - Low-reliability witnesses are sampled proportionally

        This gives better conflict coverage than truncation when a slot
        has many witnesses, because truncation silently drops all pairs
        involving later witnesses regardless of their quality.
        """
        from itertools import combinations
        all_pairs = list(combinations(witnesses, 2))
        if len(all_pairs) <= max_pairs:
            return all_pairs

        # Sort witnesses by reliability descending
        sorted_ws = sorted(witnesses, key=lambda w: w.reliability_score, reverse=True)
        mid = max(1, len(sorted_ws) // 2)
        top_tier = sorted_ws[:mid]
        low_tier = sorted_ws[mid:]

        # Always include all top-tier pairs
        import itertools
        top_pairs = list(combinations(top_tier, 2))

        # Cross-tier pairs (top vs low) — include all
        cross_pairs = [(a, b) for a in top_tier for b in low_tier]

        # Low-tier pairs — sample randomly if needed
        low_pairs = list(combinations(low_tier, 2))

        selected = top_pairs + cross_pairs
        remaining = max_pairs - len(selected)

        if remaining > 0 and low_pairs:
            import random
            random.seed(42)   # deterministic sampling
            sampled_low = random.sample(low_pairs, min(remaining, len(low_pairs)))
            selected += sampled_low

        return selected[:max_pairs]

    def _phase2_detect(self) -> None:
        """
        Compare all witness pairs and apply conflict detection rules.

        Rules 1, 2, 3, 5 — group by slot type:
            Only witnesses answering the same question slot are compared.
            e.g. WHO vs WHO, WHEN vs WHEN.

        Rule 4 — group by KG0 entity ID:
            Witnesses from DIFFERENT documents that reference the SAME
            real-world entity are compared, regardless of slot type.

        For Rules 1-3, a 'continue' after a match means we do not
        check further rules for that pair — one conflict per pair is enough.
        Rule 5 can fire on a pair that Rules 1-3 did not catch.
        """
        n_compared = 0
        emitted_pairs: Set[Tuple[str, str]] = set()

        def already_emitted_or_existing(a: WitnessRecord, b: WitnessRecord) -> bool:
            pair_key = self._pair_key(a.uid, b.uid)
            return pair_key in emitted_pairs or self._is_duplicate_pair(a.uid, b.uid)

        def record_conflict(
            conflict: Optional[ConflictRecord],
            a: WitnessRecord,
            b: WitnessRecord,
        ) -> bool:
            if not conflict:
                return False
            self._conflicts.append(conflict)
            emitted_pairs.add(self._pair_key(a.uid, b.uid))
            return True

        # ── Rule 1: var_name aware — within same slot + same variable ───────
        # Groups by (slot_type, var_name) so only witnesses filling the
        # SAME graph variable in the SAME slot are compared.
        # e.g. HOW/P witnesses only compare against other HOW/P witnesses,
        # not against HOW/RESP witnesses (which are complementary, not contradictory).
        # This eliminates false positives from cross-variable comparisons.
        if self.cfg.enable_rule1_surface_mismatch:
            for (slot_type, var_name), witnesses in self._by_slot_var.items():
                if len(witnesses) < 2:
                    continue
                pairs = list(combinations(witnesses, 2))
                if len(pairs) > self.cfg.max_pairs_per_slot:
                    self._diag("WARNING", "CONFLICT_PAIR_CAP",
                               f"Slot {slot_type}/{var_name}: {len(pairs)} pairs, "
                               f"capping at {self.cfg.max_pairs_per_slot} "
                               f"({'stratified' if self.cfg.use_stratified_sampling else 'truncated'})")
                    if self.cfg.use_stratified_sampling:
                        pairs = self._stratified_sample(
                            [w for w in witnesses], self.cfg.max_pairs_per_slot
                        )
                    else:
                        pairs = pairs[:self.cfg.max_pairs_per_slot]
                for a, b in pairs:
                    # Cross-run dedup — skip if already detected
                    if already_emitted_or_existing(a, b):
                        continue
                    n_compared += 1
                    c = self._rule1_surface_mismatch(a, b)
                    record_conflict(c, a, b)

        # ── Rules 2, 3, 5: within same slot group (slot_type only) ───────────
        # These rules still group by slot_type alone because:
        # - Rule 2 (temporal clash): dates can clash across variables
        # - Rule 3 (negation): negation can target any witness in the slot
        # - Rule 5 (reliability): quality gap is meaningful across variables
        for slot_type, witnesses in self._by_slot.items():
            if len(witnesses) < 2:
                continue

            pairs = list(combinations(witnesses, 2))
            if len(pairs) > self.cfg.max_pairs_per_slot:
                self._diag("WARNING", "CONFLICT_PAIR_CAP",
                           f"Slot {slot_type}: {len(pairs)} pairs, "
                           f"capping at {self.cfg.max_pairs_per_slot} "
                           f"({'stratified' if self.cfg.use_stratified_sampling else 'truncated'})")
                if self.cfg.use_stratified_sampling:
                    witnesses = self._by_slot[slot_type]
                    pairs = self._stratified_sample(witnesses, self.cfg.max_pairs_per_slot)
                else:
                    pairs = pairs[:self.cfg.max_pairs_per_slot]

            for a, b in pairs:
                # Cross-run dedup — skip if already detected
                if already_emitted_or_existing(a, b):
                    continue
                n_compared += 1

                if self.cfg.enable_rule2_temporal_clash:
                    c = self._rule2_temporal_clash(a, b)
                    if record_conflict(c, a, b):
                        continue

                if self.cfg.enable_rule3_negation:
                    c = self._rule3_negation(a, b)
                    if record_conflict(c, a, b):
                        continue

                if self.cfg.enable_rule5_reliability_diverge:
                    c = self._rule5_reliability_diverge(a, b)
                    record_conflict(c, a, b)

        # ── Rule 4: cross-artifact, same KG0 entity ───────────────────────
        if self.cfg.enable_rule4_cross_artifact:
            for kg0_id, witnesses in self._by_kg0.items():
                if len(witnesses) < 2:
                    continue
                cross_pairs = [
                    (a, b) for a, b in combinations(witnesses, 2)
                    if a.artifact_id != b.artifact_id
                ]
                for a, b in cross_pairs:
                    if already_emitted_or_existing(a, b):
                        continue
                    n_compared += 1
                    c = self._rule4_cross_artifact(a, b, kg0_id)
                    record_conflict(c, a, b)

        self._diag("INFO", "CONFLICT_PHASE2",
                   f"{n_compared} pairs compared, "
                   f"{len(self._conflicts)} conflicts found")

        # ── ClusterConflicts (Algorithm 6 Line 22) ──────────────────
        self._cluster_conflicts()

    # ─────────────────────────────────────────────────────────────────────────
    #  Rules 1 – 5
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _looks_like_doc_id(surface: str, max_len: int, pattern: str = None) -> bool:
        """
        Returns True when a surface string looks like a corpus document
        reference ID rather than real slot answer text.

        A surface is classified as a document ID when ALL of:
            - length is at or below max_len characters
            - all characters are lowercase alphanumeric (no spaces,
              punctuation, or uppercase letters)

        Examples that match  : "zlcx0257", "jgvx0257", "abc123"
        Examples that do not : "Walgreens", "Focus on Profit",
                               "Title 21 CFR", "October 10, 2016"
        """
        import re as _re
        s = surface.strip()
        if not s or len(s) > max_len:
            return False
        if pattern:
            return bool(_re.match(pattern, s))
        return s.isalnum() and s.islower()

    @staticmethod
    def _different_nonempty_var(a: WitnessRecord, b: WitnessRecord) -> bool:
        return bool(a.var_name and b.var_name and a.var_name != b.var_name)

    @staticmethod
    def _same_nonempty_entity(a: WitnessRecord, b: WitnessRecord) -> bool:
        return bool(a.kg0_entity_id and a.kg0_entity_id == b.kg0_entity_id)

    @staticmethod
    def _short_alias_surface(surface: str) -> bool:
        return len(re.findall(r"[a-z0-9]+", surface.lower())) <= 4

    def _has_negation_relation(self, a: WitnessRecord, b: WitnessRecord) -> bool:
        text_a = (a.content_excerpt + " " + a.surface).strip()
        text_b = (b.content_excerpt + " " + b.surface).strip()
        return (
            bool(self._negation.has_negation_about(text_a, b.surface)["found"])
            or bool(self._negation.has_negation_about(text_b, a.surface)["found"])
        )

    def _rule1_surface_mismatch(
        self, a: WitnessRecord, b: WitnessRecord
    ) -> Optional[ConflictRecord]:
        """
        Rule 1 — SURFACE_MISMATCH

        Fires when two witnesses answer the same slot but their surface
        texts refer to incompatible things.

        Guards (all must pass):
            - Both surfaces are non-empty
            - Neither surface is a document reference ID
              (skip_document_id_surfaces guard — prevents false positives
               when TRACE stores corpus IDs like "zlcx0257" as surfaces)
            - Neither surface is a substring of the other
              (rules out "Reynolds" vs "R.J. Reynolds" — same entity)
            - Token overlap is below 60%
              (rules out minor paraphrasing of the same answer)
            - At least one witness is GROUNDED
              (two AMBIGUOUS witnesses conflicting is not meaningful)

        Confidence: min(reliability_a, reliability_b) + 0.1
            The weaker witness sets the ceiling.
        """
        if not a.surface or not b.surface:
            return None
        if self._different_nonempty_var(a, b):
            return None
        if self._same_nonempty_entity(a, b):
            return None
        if self._has_negation_relation(a, b):
            return None

        # ── Document ID guard ─────────────────────────────────────────────
        # If either surface looks like a corpus document reference ID
        # (short, lowercase, alphanumeric only) skip the comparison.
        # These are not competing slot answers — they are document pointers.
        if self.cfg.skip_document_id_surfaces:
            max_len = self.cfg.tau_doc_id_max_length
            if (self._looks_like_doc_id(a.surface, max_len, self.cfg.doc_id_pattern) or
                    self._looks_like_doc_id(b.surface, max_len, self.cfg.doc_id_pattern)):
                return None

        sa, sb = a.surface.lower().strip(), b.surface.lower().strip()
        if sa in sb or sb in sa:
            return None
        if _token_overlap(sa, sb) >= self.cfg.tau_surface_overlap:
            return None
        if a.quality == "AMBIGUOUS" and b.quality == "AMBIGUOUS":
            return None

        weaker = a if a.reliability_score <= b.reliability_score else b
        conf   = min(a.reliability_score + 0.1, b.reliability_score + 0.1, 1.0)

        return ConflictRecord(
            conflict_id=_deterministic_uid(
                f"conflict::R1::{':'.join(sorted([a.uid, b.uid]))}"),
            rule="SURFACE_MISMATCH", defeater_type="rebutting",
            witness_a_uid=a.uid, witness_b_uid=b.uid,
            description=(
                f"Slot '{a.slot_type}': incompatible answers "
                f"'{a.surface}' (rel={a.reliability_score:.2f}) vs "
                f"'{b.surface}' (rel={b.reliability_score:.2f})."
            ),
            confidence=conf, weaker_witness_uid=weaker.uid,
            claim_a_uid=a.claim_uid, claim_b_uid=b.claim_uid,
        )

    def _rule2_temporal_clash(
        self, a: WitnessRecord, b: WitnessRecord
    ) -> Optional[ConflictRecord]:
        """
        Rule 2 — TEMPORAL_CLASH

        Fires when both witnesses contain a recognisable date and those
        dates are more than tau_temporal_months apart (default 6 months).

        Date extraction tries content_excerpt first (the full justification
        sentence), then surface (the raw entity text). content_excerpt is
        preferred because it contains more context.

        Confidence: 0.7 + min(months_apart / 120, 0.25)
            Scales up with gap size, capped at 0.95.
            A 6-month gap → 0.75. A 30-month gap → 0.95.
        """
        date_a = _extract_date(a.content_excerpt) or _extract_date(a.surface)
        date_b = _extract_date(b.content_excerpt) or _extract_date(b.surface)

        if not date_a or not date_b or date_a == date_b:
            return None

        try:
            ya, ma = _parse_year_month(date_a)
            yb, mb = _parse_year_month(date_b)
        except ValueError:
            return None

        months_apart = abs((ya * 12 + ma) - (yb * 12 + mb))
        if months_apart < self.cfg.tau_temporal_months:
            return None

        weaker = a if a.reliability_score <= b.reliability_score else b
        conf   = min(0.7 + months_apart / 120, 0.95)

        # ── EvalSupersession (Algorithm 6 Line 14) ───────────────────────────
        # Before classifying as REFUTES check whether the later document
        # supersedes the earlier one. If yes use SUPERSEDES (undercutting)
        # instead of TEMPORAL_CLASH (rebutting).
        if self.cfg.enable_eval_supersession:
            is_supersession, supersession_cue = self._eval_supersession(a, b)
            if is_supersession:
                return ConflictRecord(
                    conflict_id=_deterministic_uid(
                        f"conflict::R2S::{':'.join(sorted([a.uid, b.uid]))}"),
                    rule="TEMPORAL_SUPERSESSION",
                    defeater_type="undercutting",   # lighter penalty than rebutting
                    witness_a_uid=a.uid, witness_b_uid=b.uid,
                    description=(
                        f"Slot '{a.slot_type}': '{date_a}' superseded by "
                        f"'{date_b}' ({months_apart} months apart). "
                        f"Supersession cue: '{supersession_cue}'."
                    ),
                    confidence=min(conf, 0.75),   # lower confidence — resolved by time
                    weaker_witness_uid=weaker.uid,
                    claim_a_uid=a.claim_uid, claim_b_uid=b.claim_uid,
                )

        return ConflictRecord(
            conflict_id=_deterministic_uid(
                f"conflict::R2::{':'.join(sorted([a.uid, b.uid]))}"),
            rule="TEMPORAL_CLASH", defeater_type="rebutting",
            witness_a_uid=a.uid, witness_b_uid=b.uid,
            description=(
                f"Slot '{a.slot_type}': date clash "
                f"'{date_a}' vs '{date_b}' ({months_apart} months apart)."
            ),
            confidence=conf, weaker_witness_uid=weaker.uid,
            claim_a_uid=a.claim_uid, claim_b_uid=b.claim_uid,
        )

    def _rule3_negation(
        self, a: WitnessRecord, b: WitnessRecord
    ) -> Optional[ConflictRecord]:
        """
        Rule 3 — NEGATION_CONFLICT

        Fires when one witness's text contains a negation that covers
        the subject matter of the other witness's surface.

        Detection uses the NegationEngine (Section 3) which runs:
            Layer 1 — Regex  (fast, simple cases)
            Layer 2 — spaCy  (long-range, clausal negation)
            Layer 3 — NegSpacy (negative quantifiers: None, Neither…)

        The search text for each witness is:
            content_excerpt + " " + surface
        This gives the fullest possible view of what the witness is saying.

        Confidence: fixed at 0.65 — text proximity is an uncertain signal
        regardless of which layer catches the negation.

        The negating witness is always treated as the weaker one.
        A denial is less direct evidence than an affirmation.
        """
        text_a = (a.content_excerpt + " " + a.surface).strip()
        text_b = (b.content_excerpt + " " + b.surface).strip()

        # has_negation_about now returns a details dict, not a bool
        result_ab = self._negation.has_negation_about(text_a, b.surface)
        result_ba = self._negation.has_negation_about(text_b, a.surface)

        neg_ab = result_ab["found"]
        neg_ba = result_ba["found"]

        if not (neg_ab or neg_ba):
            return None

        # The negating witness is the one whose text contains the negation.
        # The affirming witness is the one whose surface is being negated.
        negating  = a if neg_ab else b
        affirming = b if neg_ab else a
        neg_result = result_ab if neg_ab else result_ba

        return ConflictRecord(
            conflict_id=_deterministic_uid(
                f"conflict::R3::{':'.join(sorted([a.uid, b.uid]))}"),
            rule="NEGATION_CONFLICT", defeater_type="rebutting",
            witness_a_uid=a.uid, witness_b_uid=b.uid,
            description=(
                f"Slot '{a.slot_type}': witness {negating.uid[:12]} "
                f"negates '{affirming.surface}' which "
                f"witness {affirming.uid[:12]} affirms. "
                f"[{neg_result['negation_type']} via {neg_result['negation_layer']}, "
                f"cue: '{neg_result['negation_cue']}']"
            ),
            confidence=0.65,
            weaker_witness_uid=negating.uid,
            claim_a_uid=a.claim_uid, claim_b_uid=b.claim_uid,
            negation_type=neg_result["negation_type"],
            negation_cue=neg_result["negation_cue"],
            negation_layer=neg_result["negation_layer"],
        )

    def _rule4_cross_artifact(
        self, a: WitnessRecord, b: WitnessRecord, kg0_id: str
    ) -> Optional[ConflictRecord]:
        """
        Rule 4 — CROSS_ARTIFACT_ENTITY

        Fires when two witnesses from DIFFERENT source documents share
        the same KG0 entity ID but describe that entity in incompatible
        ways.

        Unlike Rules 1-3, this rule does not require the witnesses to be
        in the same slot group. It groups by entity identity (KG0 ID).

        Extra guard — both must be GROUNDED:
            Cross-document contradiction is a serious claim. We only
            make it when both sides have solid evidence quality.

        Confidence: average of both reliability scores × 0.8
            The 0.8 discount reflects that surface wording can differ
            across documents for legitimate reasons (different roles,
            different time periods).
        """
        if a.artifact_id == b.artifact_id:
            return None  # same document — Rule 1 covers this
        if a.slot_type and b.slot_type and a.slot_type != b.slot_type:
            return None
        if self._different_nonempty_var(a, b):
            return None
        if a.quality != "GROUNDED" or b.quality != "GROUNDED":
            return None

        sa, sb = a.surface.lower().strip(), b.surface.lower().strip()
        if sa in sb or sb in sa:
            return None
        if self._short_alias_surface(a.surface) and self._short_alias_surface(b.surface):
            return None
        if _token_overlap(sa, sb) >= self.cfg.tau_cross_artifact_overlap:
            return None

        weaker = a if a.reliability_score <= b.reliability_score else b
        conf   = min((a.reliability_score + b.reliability_score) / 2.0 * 0.8, 1.0)

        return ConflictRecord(
            conflict_id=_deterministic_uid(
                f"conflict::R4::{':'.join(sorted([a.uid, b.uid]))}"),
            rule="CROSS_ARTIFACT_ENTITY", defeater_type="rebutting",
            witness_a_uid=a.uid, witness_b_uid=b.uid,
            description=(
                f"KG0 entity '{kg0_id}': "
                f"doc {a.artifact_id} says '{a.surface}', "
                f"doc {b.artifact_id} says '{b.surface}'."
            ),
            confidence=conf, weaker_witness_uid=weaker.uid,
            claim_a_uid=a.claim_uid, claim_b_uid=b.claim_uid,
        )

    def _rule5_reliability_diverge(
        self, a: WitnessRecord, b: WitnessRecord
    ) -> Optional[ConflictRecord]:
        """
        Rule 5 — RELIABILITY_DIVERGE

        Fires when two witnesses for the same slot have a large gap in
        reliability score (default threshold: 0.40).

        This is NOT a content contradiction. The two witnesses may say
        the same thing. The conflict is epistemic: the system found
        evidence of wildly different quality for the same slot, which
        means the answer is less solid than it appeared.

        Produces an UNDERCUTTING defeater (not rebutting):
            - Does NOT set Claim.status = 'contested'
            - Schema rule undercutting_defeater_weakens_inference fires:
              → Inference.confidenceScore × 0.5  (halved)
              → Claim.status → 'weakly-supported'

        Guards:
            - Gap must meet tau_reliability_gap (default 0.40)
            - Weaker score must be > 0.01 (not a ghost witness)

        Confidence: min(gap, 0.90)
            Larger gap = more certain this is a real quality problem.
        """
        gap = abs(a.reliability_score - b.reliability_score)
        if gap < self.cfg.tau_reliability_gap:
            return None
        if self._different_nonempty_var(a, b):
            return None

        weaker   = a if a.reliability_score < b.reliability_score else b
        stronger = b if weaker is a else a

        if weaker.reliability_score < 0.01:
            return None

        # Document ID guard — skip when either surface is a corpus doc ID
        # (e.g. zlcx0257) not real slot answer text. The reliability gap in
        # that case reflects a text witness vs a document pointer — not a
        # meaningful quality divergence for the same slot answer.
        if self.cfg.skip_document_id_surfaces:
            max_len = self.cfg.tau_doc_id_max_length
            if (self._looks_like_doc_id(a.surface, max_len, self.cfg.doc_id_pattern) or
                    self._looks_like_doc_id(b.surface, max_len, self.cfg.doc_id_pattern)):
                return None

        surface_similar = _token_overlap(a.surface, b.surface) >= self.cfg.tau_surface_overlap
        if not (self._same_nonempty_entity(a, b) or surface_similar):
            return None

        return ConflictRecord(
            conflict_id=_deterministic_uid(
                f"conflict::R5::{':'.join(sorted([a.uid, b.uid]))}"),
            rule="RELIABILITY_DIVERGE", defeater_type="undercutting",
            witness_a_uid=a.uid, witness_b_uid=b.uid,
            description=(
                f"Slot '{a.slot_type}': reliability gap {gap:.2f} "
                f"(strong={stronger.reliability_score:.2f}, "
                f"weak={weaker.reliability_score:.2f}). "
                f"Weak witness undercuts inference confidence."
            ),
            confidence=min(gap, 0.9),
            weaker_witness_uid=weaker.uid,
            claim_a_uid=a.claim_uid, claim_b_uid=b.claim_uid,
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  Phase 3 — Write results to graph
    # ─────────────────────────────────────────────────────────────────────────

    def _phase3_write(self, rg_root_uid: str, eg_root_uid: str) -> None:
        """
        For each ConflictRecord write all required graph elements.

        Order of writes per conflict:
            1. CONTRADICTS edge in EG  (+ symmetric back-edge)
            2. Defeater node in RG
            3. CONTAINS_DEFEATER from RG root
            4. HAS_DEFEATER from Inference(s) to Defeater
            5. REFERENCES_CLAIM from Defeater to weaker Claim
            6. REFERENCES_EVIDENCE bridge edge to weaker witness
            7. Claim.status mutation  (rebutting conflicts only)
        """
        n_contradicts = 0
        n_defeaters   = 0
        contested_set: Set[str] = set()
        now = _now()

        for conflict in self._conflicts:
            wa, wb = conflict.witness_a_uid, conflict.witness_b_uid

            # 1. CONTRADICTS ──────────────────────────────────────────────────
            pair_key = tuple(sorted([wa, wb]))
            if pair_key not in self._seen_contradicts:
                self._seen_contradicts.add(pair_key)

                # Build domainMetadata for the CONTRADICTS edge.
                # negation_type, negation_cue, negation_layer are populated
                # only for NEGATION_CONFLICT rules — empty string for others.
                # This is the schema addition approved by the professor:
                # adding negation style information directly on the edge
                # so downstream systems (EXPLAIN) can describe the nature
                # of the contradiction without traversing to the Defeater.
                edge_dm = {
                    "conflict_rule":  conflict.rule,
                    "conflict_id":    conflict.conflict_id,
                }
                if conflict.rule == "NEGATION_CONFLICT":
                    edge_dm["negation_type"]  = conflict.negation_type
                    edge_dm["negation_cue"]   = conflict.negation_cue
                    edge_dm["negation_layer"] = conflict.negation_layer

                self.eg.create_edge(wa, wb, "CONTRADICTS", {
                    "uid":           _edge_uid(wa, wb, "CONTRADICTS"),
                    "confidence":    conflict.confidence,
                    "justification": conflict.description,
                    "assertedByUid": self.cfg.conflict_agent_uid,
                    "assertedAt":    now,
                    "domainMetadata": edge_dm,
                })
                n_contradicts += 1

                if self.cfg.write_symmetric_contradicts:
                    sym_dm = {
                        "conflict_rule": conflict.rule,
                        "inferred":      True,
                    }
                    if conflict.rule == "NEGATION_CONFLICT":
                        sym_dm["negation_type"]  = conflict.negation_type
                        sym_dm["negation_cue"]   = conflict.negation_cue
                        sym_dm["negation_layer"] = conflict.negation_layer
                    self.eg.create_edge(wb, wa, "CONTRADICTS", {
                        "uid":           _edge_uid(wb, wa, "CONTRADICTS"),
                        "confidence":    conflict.confidence,
                        "justification": f"Symmetric: {conflict.description}",
                        "assertedByUid": self.cfg.conflict_agent_uid,
                        "assertedAt":    now,
                        "domainMetadata": sym_dm,
                    })

            # 2. Defeater node ─────────────────────────────────────────────
            defeater_uid = _deterministic_uid(
                f"defeater::conflict::{conflict.conflict_id}")

            if defeater_uid not in self._seen_defeaters:
                self._seen_defeaters.add(defeater_uid)

                self.rg.create_node(["Defeater"], {
                    "uid":          defeater_uid,
                    "type":         conflict.defeater_type,
                    "description":  conflict.description,
                    "cluster_size": getattr(conflict, "cluster_size", 1),
                    "domainMetadata": {
                        "conflict_id":   conflict.conflict_id,
                        "conflict_rule": conflict.rule,
                        "confidence":    conflict.confidence,
                    },
                })

                # 3. CONTAINS_DEFEATER ──────────────────────────────────────
                if rg_root_uid:
                    self.rg.create_edge(
                        rg_root_uid, defeater_uid, "CONTAINS_DEFEATER",
                        {"uid": _edge_uid(rg_root_uid, defeater_uid,
                                          "CONTAINS_DEFEATER")},
                    )

                n_defeaters += 1
                self._defeater_uids.append(defeater_uid)

            # 4. HAS_DEFEATER — attach to any Inference involving the Claims
            for inf_uid in self._find_inferences_for_claims(
                conflict.claim_a_uid, conflict.claim_b_uid
            ):
                self.rg.create_edge(
                    inf_uid, defeater_uid, "HAS_DEFEATER",
                    {"uid": _edge_uid(inf_uid, defeater_uid, "HAS_DEFEATER")},
                )

            # 5. REFERENCES_CLAIM ─────────────────────────────────────────
            weaker_claim = (
                conflict.claim_a_uid
                if conflict.weaker_witness_uid == conflict.witness_a_uid
                else conflict.claim_b_uid
            )
            if weaker_claim:
                self.rg.create_edge(
                    defeater_uid, weaker_claim, "REFERENCES_CLAIM",
                    {"uid": _edge_uid(defeater_uid, weaker_claim,
                                      "REFERENCES_CLAIM")},
                )

            # 6. REFERENCES_EVIDENCE bridge ───────────────────────────────
            w_uid = conflict.weaker_witness_uid
            if w_uid and self.eg.node_exists(w_uid):
                self.bridge.create_edge(
                    defeater_uid, w_uid, "REFERENCES_EVIDENCE",
                    {"uid": _edge_uid(defeater_uid, w_uid,
                                      "REFERENCES_EVIDENCE")},
                )

            # 7. Claim status update (rebutting only) ─────────────────────
            if (
                self.cfg.update_claim_status
                and conflict.defeater_type == "rebutting"
            ):
                for claim_uid in [conflict.claim_a_uid, conflict.claim_b_uid]:
                    if claim_uid and claim_uid not in contested_set:
                        contested_set.add(claim_uid)
                        self._set_claim_contested(claim_uid, conflict)

        self._claims_contested = list(contested_set)
        self._diag("INFO", "CONFLICT_PHASE3",
                   f"{n_contradicts} CONTRADICTS edges, "
                   f"{n_defeaters} Defeaters, "
                   f"{len(self._claims_contested)} Claims contested")

    def _set_claim_contested(
        self, claim_uid: str, conflict: ConflictRecord
    ) -> None:
        """
        Set Claim.status = 'contested' and record the conflict reason.

        In-memory path: mutates the node dict directly.
        Memgraph path:  issues a SET Cypher statement.
        """
        # In-memory path
        for node in getattr(self.rg, "nodes", []):
            if (
                "Claim" in node.get("labels", [])
                and node["properties"].get("uid") == claim_uid
            ):
                node["properties"]["status"] = "contested"
                dm = node["properties"].setdefault("domainMetadata", {})
                dm["contestedByConflict"] = conflict.conflict_id
                dm["contestedRule"]       = conflict.rule
                return

        # Memgraph path
        if hasattr(self.rg, "_run"):
            self.rg._run(
                "MATCH (c:Claim {uid: $uid}) "
                "SET c.status = 'contested', "
                "    c.domainMetadata.contestedByConflict = $cid, "
                "    c.domainMetadata.contestedRule = $rule",
                {"uid": claim_uid,
                 "cid": conflict.conflict_id,
                 "rule": conflict.rule},
            )

    def _find_inferences_for_claims(
        self, claim_a_uid: str, claim_b_uid: str
    ) -> List[str]:
        """
        Find Inference UIDs that involve either Claim as premise or conclusion.
        Used to attach the Defeater to the right reasoning step.
        """
        claim_set = {c for c in [claim_a_uid, claim_b_uid] if c}
        inf_uids: List[str] = []
        for edge in getattr(self.rg, "edges", []):
            if edge.get("type") not in ("HAS_PREMISE", "HAS_CONCLUSION"):
                continue
            if edge.get("to") in claim_set or edge.get("from") in claim_set:
                inf_uids.append(edge.get("from", ""))
        return list(set(filter(None, inf_uids)))

    # ─────────────────────────────────────────────────────────────────────────
    #  Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _ensure_agent(self) -> None:
        """Create the CONFLICT Agent node in both EG and RG if not present."""
        agent_props = {
            "uid":  self.cfg.conflict_agent_uid,
            "type": "system",
            "name": self.cfg.conflict_agent_name,
            "role": "conflict_detector",
        }
        for writer in (self.eg, self.rg):
            if not writer.node_exists(self.cfg.conflict_agent_uid):
                writer.create_node(["Agent"], agent_props)

    def _diag(self, level: str, code: str, message: str) -> None:
        self._diags.append({"level": level, "code": code, "message": message})
        (logger.info if level == "INFO" else logger.warning)(
            "[CONFLICT] %s: %s", code, message
        )

    def _finalise(self) -> ConflictResult:
        stats = {
            "witnesses_indexed":      len(self._witnesses),
            "slot_groups":            len(self._by_slot),
            "kg0_entity_groups":      len(self._by_kg0),
            "conflicts_found":        len(self._conflicts),
            "rule1_surface_mismatch": sum(1 for c in self._conflicts if c.rule == "SURFACE_MISMATCH"),
            "rule2_temporal_clash":         sum(1 for c in self._conflicts if c.rule == "TEMPORAL_CLASH"),
            "rule2_supersession":            sum(1 for c in self._conflicts if c.rule == "TEMPORAL_SUPERSESSION"),
            "clusters_written":              sum(1 for c in self._conflicts if getattr(c, "cluster_size", 1) > 1),
            
            "rule3_negation":         sum(1 for c in self._conflicts if c.rule == "NEGATION_CONFLICT"),
            "rule4_cross_artifact":   sum(1 for c in self._conflicts if c.rule == "CROSS_ARTIFACT_ENTITY"),
            "rule5_reliability":      sum(1 for c in self._conflicts if c.rule == "RELIABILITY_DIVERGE"),
            "defeaters_created":      len(self._defeater_uids),
            "claims_contested":       len(self._claims_contested),
            "negation_backend":       self._negation._mode,
        }
        return ConflictResult(
            conflicts=self._conflicts,
            defeater_uids=self._defeater_uids,
            contradicts_edges_written=len(self._seen_contradicts),
            claims_contested=self._claims_contested,
            diagnostics=self._diags,
            stats=stats,
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 6 — Memgraph witness reader (live database deployments)
# ═══════════════════════════════════════════════════════════════════════════════

class MemgraphWitnessReader:
    """
    Reads witness nodes from a live Memgraph database via Cypher.

    Use this instead of the in-memory node list scan when TRACE has
    written to a real Memgraph instance rather than InMemoryGraphWriter.

    Usage
    -----
        reader   = MemgraphWitnessReader(conn)
        witnesses = reader.load_witnesses(eg_root_uid)
        # inject witnesses into the Conflict instance before execute()
    """

    QUERY = """
    MATCH (root:GraphRoot {uid: $eg_root_uid})-[:CONTAINS_NODE]->(e:EvidenceNode)
    WHERE e.domainType CONTAINS 'Witness'
    OPTIONAL MATCH (claim:Claim)-[:GROUNDED_BY]->(e)
    RETURN
        e.uid               AS uid,
        e.reliabilityScore  AS reliability,
        e.contentExcerpt    AS content_excerpt,
        e.domainMetadata    AS dm,
        coalesce(claim.uid, '') AS claim_uid
    ORDER BY e.uid
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def load_witnesses(self, eg_root_uid: str) -> List[Dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute(self.QUERY, {"eg_root_uid": eg_root_uid})
        results = []
        for row in cur.fetchall():
            dm = row.get("dm", {}) or {}
            results.append({
                "uid":               row["uid"],
                "reliability_score": float(row.get("reliability") or 0.0),
                "content_excerpt":   row.get("content_excerpt", ""),
                "claim_uid":         row.get("claim_uid", ""),
                "slot_type":         dm.get("slot_type",     ""),
                "var_name":          dm.get("var_name",      ""),
                "surface":           dm.get("surface",       ""),
                "kg0_entity_id":     dm.get("kg0_entity_id", ""),
                "anchor_id":         dm.get("anchor_id",     ""),
                "quality":           dm.get("quality",       "AMBIGUOUS"),
            })
        return results


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 7 — Convenience entry point
# ═══════════════════════════════════════════════════════════════════════════════

def run_conflict(
    trace_result: Any,
    eg_writer:    Any,
    rg_writer:    Any,
    bridge_writer: Any = None,
    rg_root_uid:  str = "",
    eg_root_uid:  str = "",
    cfg: Optional[ConflictConfig] = None,
) -> ConflictResult:
    """
    Run CONFLICT in one line after TRACE has completed.

    Parameters
    ----------
    trace_result  : TraceResult from trace.py
    eg_writer     : the same EG GraphWriter used by TRACE
    rg_writer     : the same RG GraphWriter used by TRACE
    bridge_writer : bridge GraphWriter (defaults to eg_writer)
    rg_root_uid   : from trace_result.rg_root_uid
    eg_root_uid   : from trace_result.eg_root_uid
    cfg           : optional ConflictConfig to override defaults

    Example
    -------
        from trace2 import Trace, InMemoryGraphWriter
        from conflict import run_conflict

        eg = InMemoryGraphWriter()
        rg = InMemoryGraphWriter()
        trace = Trace(eg=eg, rg=rg, bridge=eg)
        trace_result = trace.execute(bundle)

        result = run_conflict(
            trace_result = trace_result,
            eg_writer    = eg,
            rg_writer    = rg,
            bridge_writer= eg,
            rg_root_uid  = trace_result.rg_root_uid,
            eg_root_uid  = trace_result.eg_root_uid,
        )

        print(result.stats)
        for c in result.conflicts:
            print(c.rule, "→", c.description)
    """
    op = Conflict(
        eg=eg_writer,
        rg=rg_writer,
        bridge=bridge_writer or eg_writer,
        cfg=cfg,
    )
    return op.execute(
        trace_result=trace_result,
        rg_root_uid=rg_root_uid,
        eg_root_uid=eg_root_uid,
    )
