---

## 0. Shared Infrastructure

Shared node labels and relationships referenced by both the Evidence Graph and the Reasoning Graph.

---

### 0.1 Node Labels

#### 0.1.1 `:GraphRoot`

The top-level container. The `graphType` discriminator determines whether this root owns an Evidence Graph or a Reasoning Graph.

| Property | Type | Required | Key | Description |
|---|---|---|---|---|
| `uid` | `UUID` | ✓ | PK | Globally unique graph identifier |
| `graphType` | `String` | ✓ | | `EvidenceGraph` or `ReasoningGraph` |
| `version` | `String` | ✓ | | Semantic version of this graph instance |
| `schemaVersion` | `String` | ✓ | | Schema version this graph conforms to |
| `created` | `DateTime` | ✓ | | |
| `modified` | `DateTime` | | | |
| `title` | `String` | | | |
| `description` | `String` | | | |
| `purpose` | `String` | | | EG: why the evidence was assembled |
| `question` | `String` | | | RG: the central question addressed |
| `scope` | `String` | | | |
| `tags` | `String[]` | | | |

#### 0.1.2 `:Agent`

Any actor — human, organizational, software, or model — that creates, modifies, assesses, or asserts elements within either graph.

| Property | Type | Required | Key | Description |
|---|---|---|---|---|
| `uid` | `String` | ✓ | PK | |
| `type` | `String` | ✓ | | `person`, `organization`, `system`, `model` |
| `name` | `String` | | | |
| `role` | `String` | | | |

#### 0.1.3 `:DomainProfile`

An optional overlay declaring domain-specific extensions. The core schema operates fully without one.

| Property | Type | Required | Key | Description |
|---|---|---|---|---|
| `profileId` | `String` | ✓ | PK | |
| `profileVersion` | `String` | | | |
| `description` | `String` | | | |
| `extendedNodeTypes` | `String[]` | | | Additional `domainType` values for EvidenceNodes |
| `extendedEdgeTypes` | `String[]` | | | Additional evidence-layer edge types |
| `extendedClaimTypes` | `String[]` | | | Additional `domainType` values for Claims |
| `extendedInferenceTypes` | `String[]` | | | Additional Inference type values |
| `extendedEvidenceLinkRoles` | `String[]` | | | Additional GROUNDED_BY role values |

#### 0.1.4 `:ValidationRule`

A domain-specific validation constraint declared within a DomainProfile.

| Property | Type | Required | Key | Description |
|---|---|---|---|---|
| `ruleId` | `String` | ✓ | PK (scoped to parent DomainProfile) | |
| `description` | `String` | | | Human-readable description |
| `expression` | `String` | | | Machine-evaluable constraint expression |

---

### 0.2 Shared Relationships

**`(:GraphRoot)-[:HAS_PROFILE]->(:DomainProfile)`** — No properties.

**`(:GraphRoot)-[:AUTHORED_BY]->(:Agent)`** — No properties.

**`(:DomainProfile)-[:HAS_VALIDATION_RULE]->(:ValidationRule)`** — No properties.

> **Implementation note.**  Any relationship — including those declared above
> and elsewhere as "No properties" — may carry an optional `uid: UUID`
> property to support idempotent MERGE operations.  This property is not
> repeated in individual relationship definitions but is universally
> permitted across the schema.
---

### 0.3 Shared Keys

```
KEY graph_root_pk
  FOR (n:GraphRoot) ON (n.uid)

KEY agent_pk
  FOR (n:Agent) ON (n.uid)

KEY domain_profile_pk
  FOR (n:DomainProfile) ON (n.profileId)

KEY validation_rule_pk
  FOR (n:ValidationRule) ON (n.ruleId)
  SCOPED TO (:DomainProfile)-[:HAS_VALIDATION_RULE]->(n)
```

---

### 0.4 Shared Constraints

```
CONSTRAINT graph_type_enum
  FOR (n:GraphRoot)
  REQUIRE n.graphType IN ['EvidenceGraph', 'ReasoningGraph']

CONSTRAINT graph_root_has_type
  FOR (n:GraphRoot) REQUIRE n.graphType IS NOT NULL

CONSTRAINT graph_root_has_version
  FOR (n:GraphRoot) REQUIRE n.version IS NOT NULL

CONSTRAINT graph_root_has_schema_version
  FOR (n:GraphRoot) REQUIRE n.schemaVersion IS NOT NULL

CONSTRAINT graph_root_has_created
  FOR (n:GraphRoot) REQUIRE n.created IS NOT NULL

CONSTRAINT graph_version_semver
  FOR (n:GraphRoot)
  REQUIRE n.version MATCHES '^\d+\.\d+\.\d+$'

CONSTRAINT graph_schema_version_const
  FOR (n:GraphRoot)
  REQUIRE n.schemaVersion = '1.0.0'

CONSTRAINT agent_has_type
  FOR (n:Agent) REQUIRE n.type IS NOT NULL

CONSTRAINT agent_type_enum
  FOR (n:Agent)
  REQUIRE n.type IN ['person', 'organization', 'system', 'model']

CONSTRAINT domain_profile_has_id
  FOR (n:DomainProfile) REQUIRE n.profileId IS NOT NULL

CONSTRAINT validation_rule_has_id
  FOR (n:ValidationRule) REQUIRE n.ruleId IS NOT NULL
```

---
---

## 1. Evidence Graph (EG)

The Evidence Graph captures individual pieces of evidence, their provenance chains, reliability assessments, integrity metadata, lifecycle states, and inter-evidence relationships. An EG is rooted at a `:GraphRoot` node with `graphType = 'EvidenceGraph'`.

---

### 1.1 EG Node Labels

#### 1.1.1 `:EvidenceNode`

A single piece of evidence with full provenance, integrity, reliability, content, lifecycle, and access metadata flattened into scalar properties.

| Property | Type | Required | Key | Description |
|---|---|---|---|---|
| `uid` | `UUID` | ✓ | PK | |
| `type` | `String` | ✓ | | `Document`, `Record`, `Observation`, `Artifact`, `Testimony`, `DataSet`, `Media`, `Correspondence`, `Derived`, `Other` |
| `domainType` | `String` | | | Domain-profile-specific subtype |
| `label` | `String` | ✓ | | Human-readable short name |
| `description` | `String` | | | |
| `createdAt` | `DateTime` | ✓ | | When the evidence was produced |
| `method` | `String` | | | `direct-capture`, `transcription`, `extraction`, `generation`, `transformation`, `aggregation`, `unknown` |
| `methodDetail` | `String` | | | |
| `sourceOrigin` | `String` | | | Originating system, institution, or individual |
| `sourceReference` | `String` | | | External identifier: URL, DOI, catalog number, file path |
| `sourceCategory` | `String` | | | `primary`, `secondary`, `tertiary` |
| `hashAlgorithm` | `String` | | | `SHA-256`, `SHA-384`, `SHA-512`, `MD5`, `other` |
| `hashValue` | `String` | | | |
| `integrityStatus` | `String` | | | `verified`, `unverified`, `tampered`, `degraded`, `unknown` |
| `integrityVerifiedAt` | `DateTime` | | | |
| `reliabilityScore` | `Float` | | | Normalized `[0.0, 1.0]` |
| `reliabilityMethod` | `String` | | | How reliability was assessed |
| `reliabilityAssessedAt` | `DateTime` | | | |
| `accessLevel` | `String` | | | `public`, `internal`, `restricted`, `confidential` |
| `handlingInstructions` | `String` | | | |
| `lifecycleStatus` | `String` | | | `draft`, `active`, `superseded`, `deprecated`, `archived`, `retracted` |
| `supersededBy` | `UUID` | | FK → `:EvidenceNode.uid` | UID of the replacement node |
| `retractedAt` | `DateTime` | | | |
| `retractedReason` | `String` | | | |
| `contentMediaType` | `String` | | | MIME type |
| `contentUri` | `String` | | | Resolvable pointer |
| `contentExcerpt` | `String` | | | Relevant text excerpt or summary |
| `contentSize` | `Integer` | | | Bytes |
| `contentLanguage` | `String` | | | ISO 639-1 |
| `temporalStart` | `DateTime` | | | Coverage period start |
| `temporalEnd` | `DateTime` | | | Coverage period end |
| `temporalPrecision` | `String` | | | `exact`, `day`, `month`, `year`, `approximate`, `unknown` |
| `tags` | `String[]` | | | |
| `domainMetadata` | `Map` | | | Open key-value bag |

#### 1.1.2 `:ProvenanceEvent`

A single event in the custody or handling chain of an evidence item.

| Property | Type | Required | Key | Description |
|---|---|---|---|---|
| `uid` | `UUID` | ✓ | PK | |
| `action` | `String` | ✓ | | `created`, `collected`, `transferred`, `verified`, `modified`, `reviewed`, `redacted`, `archived`, `restored` |
| `timestamp` | `DateTime` | ✓ | | |
| `notes` | `String` | | | |
| `domainMetadata` | `Map` | | | Open key-value bag for implementation-specific metadata |


#### 1.1.3 `:ReliabilityFactor`

An individual factor contributing to the reliability assessment of an evidence item.

| Property | Type | Required | Key | Description |
|---|---|---|---|---|
| `uid` | `UUID` | ✓ | PK | |
| `factor` | `String` | ✓ | | Name of the factor |
| `impact` | `String` | ✓ | | `positive`, `negative`, `neutral` |
| `notes` | `String` | | | |

---

### 1.2 EG Relationships

#### 1.2.1 Graph Membership

**`(:GraphRoot)-[:CONTAINS_NODE]->(:EvidenceNode)`** — No properties. Associates an evidence node with its containing EG.

#### 1.2.2 Evidence-to-Evidence Relationships

All of the following connect `(:EvidenceNode)-[r]->(:EvidenceNode)` and share this common property set:

| Property | Type | Required | Description |
|---|---|---|---|
| `uid` | `UUID` | ✓ | Unique relationship identifier |
| `confidence` | `Float` | | `[0.0, 1.0]` — confidence that this relationship holds |
| `justification` | `String` | | Why this relationship is asserted |
| `assertedByUid` | `String` | | FK → `:Agent.uid` |
| `assertedAt` | `DateTime` | | |
| `domainMetadata` | `Map` | | Open key-value bag |

Relationship types:

**`-[:CORROBORATES]->`** — Target evidence independently supports the same proposition as source.

**`-[:CONTRADICTS]->`** — Target evidence conflicts with source.

**`-[:SUPERSEDES]->`** — Source replaces target as the current authoritative version.

**`-[:DERIVES_FROM]->`** — Source was produced from or computed out of target.

**`-[:REFERENCES]->`** — Source cites, mentions, or links to target.

**`-[:AMENDS]->`** — Source modifies or corrects target without fully replacing it.

**`-[:SUPPLEMENTS]->`** — Source adds complementary information to target.

**`-[:RESPONDS_TO]->`** — Source is a reply, reaction, or follow-up to target.

**`-[:PART_OF]->`** — Source is a constituent component of target.

**`-[:VERSION_OF]->`** — Source is a version variant of target.

**`-[:OTHER_EVIDENCE_REL]->`** — Catch-all. An optional `subtype` property (String) carries the specific label defined by a domain profile.

#### 1.2.3 Provenance & Assessment Relationships

**`(:EvidenceNode)-[:CREATED_BY]->(:Agent)`** — No properties. Who produced the evidence.

**`(:EvidenceNode)-[:HAS_PROVENANCE_EVENT]->(:ProvenanceEvent)`**

| Property | Type | Required | Description |
|---|---|---|---|
| `sequenceIndex` | `Integer` | | Ordering position in the custody chain |

**`(:ProvenanceEvent)-[:PERFORMED_BY]->(:Agent)`** — No properties. Required — every ProvenanceEvent must have exactly one.

**`(:EvidenceNode)-[:INTEGRITY_VERIFIED_BY]->(:Agent)`** — No properties.

**`(:EvidenceNode)-[:RELIABILITY_ASSESSED_BY]->(:Agent)`** — No properties.

**`(:EvidenceNode)-[:HAS_RELIABILITY_FACTOR]->(:ReliabilityFactor)`** — No properties.

---

### 1.3 EG Keys

```
KEY evidence_node_pk
  FOR (n:EvidenceNode) ON (n.uid)

KEY provenance_event_pk
  FOR (n:ProvenanceEvent) ON (n.uid)

KEY reliability_factor_pk
  FOR (n:ReliabilityFactor) ON (n.uid)
```

---

### 1.4 EG Constraints

#### 1.4.1 Existence Constraints

```
CONSTRAINT evidence_has_type
  FOR (n:EvidenceNode) REQUIRE n.type IS NOT NULL

CONSTRAINT evidence_has_label
  FOR (n:EvidenceNode) REQUIRE n.label IS NOT NULL

CONSTRAINT evidence_has_created
  FOR (n:EvidenceNode) REQUIRE n.createdAt IS NOT NULL

CONSTRAINT provenance_event_has_action
  FOR (n:ProvenanceEvent) REQUIRE n.action IS NOT NULL

CONSTRAINT provenance_event_has_timestamp
  FOR (n:ProvenanceEvent) REQUIRE n.timestamp IS NOT NULL

CONSTRAINT reliability_factor_has_factor
  FOR (n:ReliabilityFactor) REQUIRE n.factor IS NOT NULL

CONSTRAINT reliability_factor_has_impact
  FOR (n:ReliabilityFactor) REQUIRE n.impact IS NOT NULL
```

#### 1.4.2 Enumeration Constraints

```
CONSTRAINT evidence_type_enum
  FOR (n:EvidenceNode)
  REQUIRE n.type IN [
    'Document', 'Record', 'Observation', 'Artifact', 'Testimony',
    'DataSet', 'Media', 'Correspondence', 'Derived', 'Other'
  ]

CONSTRAINT evidence_method_enum
  FOR (n:EvidenceNode) WHERE n.method IS NOT NULL
  REQUIRE n.method IN [
    'direct-capture', 'transcription', 'extraction', 'generation',
    'transformation', 'aggregation', 'unknown'
  ]

CONSTRAINT evidence_source_category_enum
  FOR (n:EvidenceNode) WHERE n.sourceCategory IS NOT NULL
  REQUIRE n.sourceCategory IN ['primary', 'secondary', 'tertiary']

CONSTRAINT evidence_hash_algorithm_enum
  FOR (n:EvidenceNode) WHERE n.hashAlgorithm IS NOT NULL
  REQUIRE n.hashAlgorithm IN ['SHA-256', 'SHA-384', 'SHA-512', 'MD5', 'other']

CONSTRAINT evidence_integrity_status_enum
  FOR (n:EvidenceNode) WHERE n.integrityStatus IS NOT NULL
  REQUIRE n.integrityStatus IN [
    'verified', 'unverified', 'tampered', 'degraded', 'unknown'
  ]

CONSTRAINT evidence_access_level_enum
  FOR (n:EvidenceNode) WHERE n.accessLevel IS NOT NULL
  REQUIRE n.accessLevel IN ['public', 'internal', 'restricted', 'confidential']

CONSTRAINT evidence_lifecycle_status_enum
  FOR (n:EvidenceNode) WHERE n.lifecycleStatus IS NOT NULL
  REQUIRE n.lifecycleStatus IN [
    'draft', 'active', 'superseded', 'deprecated', 'archived', 'retracted'
  ]

CONSTRAINT evidence_temporal_precision_enum
  FOR (n:EvidenceNode) WHERE n.temporalPrecision IS NOT NULL
  REQUIRE n.temporalPrecision IN [
    'exact', 'day', 'month', 'year', 'approximate', 'unknown'
  ]

CONSTRAINT provenance_action_enum
  FOR (n:ProvenanceEvent)
  REQUIRE n.action IN [
    'created', 'collected', 'transferred', 'verified',
    'modified', 'reviewed', 'redacted', 'archived', 'restored'
  ]

CONSTRAINT reliability_factor_impact_enum
  FOR (n:ReliabilityFactor)
  REQUIRE n.impact IN ['positive', 'negative', 'neutral']
```

#### 1.4.3 Range Constraints

```
CONSTRAINT evidence_reliability_range
  FOR (n:EvidenceNode) WHERE n.reliabilityScore IS NOT NULL
  REQUIRE 0.0 <= n.reliabilityScore <= 1.0

CONSTRAINT evidence_edge_confidence_range
  FOR (:EvidenceNode)-[r]->(:EvidenceNode) WHERE r.confidence IS NOT NULL
  REQUIRE 0.0 <= r.confidence <= 1.0
```

#### 1.4.4 Format Constraints

```
CONSTRAINT evidence_content_language_iso639
  FOR (n:EvidenceNode) WHERE n.contentLanguage IS NOT NULL
  REQUIRE n.contentLanguage MATCHES '^[a-z]{2}$'
```

#### 1.4.5 Structural & Cardinality Constraints

```
CONSTRAINT provenance_event_has_exactly_one_agent
  FOR (e:ProvenanceEvent)
  REQUIRE COUNT{ (e)-[:PERFORMED_BY]->(:Agent) } = 1

CONSTRAINT evidence_node_belongs_to_graph
  FOR (n:EvidenceNode)
  REQUIRE COUNT{ (:GraphRoot)-[:CONTAINS_NODE]->(n) } >= 1

CONSTRAINT contains_node_source_is_evidence_graph
  FOR (g:GraphRoot)-[:CONTAINS_NODE]->()
  REQUIRE g.graphType = 'EvidenceGraph'

CONSTRAINT evidence_edge_endpoint_types
  FOR (s)-[r]->(t)
  WHERE type(r) IN [
    'CORROBORATES', 'CONTRADICTS', 'SUPERSEDES', 'DERIVES_FROM',
    'REFERENCES', 'AMENDS', 'SUPPLEMENTS', 'RESPONDS_TO',
    'PART_OF', 'VERSION_OF', 'OTHER_EVIDENCE_REL'
  ]
  REQUIRE s:EvidenceNode AND t:EvidenceNode
```

#### 1.4.6 Referential Integrity Constraints

```
CONSTRAINT superseded_by_references_evidence_node
  FOR (n:EvidenceNode) WHERE n.supersededBy IS NOT NULL
  REQUIRE EXISTS { (other:EvidenceNode) WHERE other.uid = n.supersededBy }

CONSTRAINT superseded_by_consistent_with_edge
  FOR (n:EvidenceNode) WHERE n.supersededBy IS NOT NULL
  REQUIRE EXISTS {
    (replacement:EvidenceNode)-[:SUPERSEDES]->(n)
    WHERE replacement.uid = n.supersededBy
  }

CONSTRAINT eg_edge_asserted_by_references_agent
  FOR (:EvidenceNode)-[r]->(:EvidenceNode)
  WHERE r.assertedByUid IS NOT NULL
  REQUIRE EXISTS { (a:Agent) WHERE a.uid = r.assertedByUid }
```

#### 1.4.7 Lifecycle & Temporal Constraints

```
CONSTRAINT supersedes_target_lifecycle
  FOR (:EvidenceNode)-[:SUPERSEDES]->(target:EvidenceNode)
  REQUIRE target.lifecycleStatus IN ['superseded', 'deprecated', 'archived']

CONSTRAINT retracted_requires_timestamp
  FOR (n:EvidenceNode) WHERE n.lifecycleStatus = 'retracted'
  REQUIRE n.retractedAt IS NOT NULL

CONSTRAINT temporal_range_ordering_evidence
  FOR (n:EvidenceNode)
  WHERE n.temporalStart IS NOT NULL AND n.temporalEnd IS NOT NULL
  REQUIRE n.temporalStart <= n.temporalEnd

CONSTRAINT provenance_chain_ordering
  FOR (n:EvidenceNode)-[r1:HAS_PROVENANCE_EVENT]->(e1:ProvenanceEvent),
      (n)-[r2:HAS_PROVENANCE_EVENT]->(e2:ProvenanceEvent)
  WHERE e1 <> e2
    AND r1.sequenceIndex IS NOT NULL
    AND r2.sequenceIndex IS NOT NULL
    AND r1.sequenceIndex < r2.sequenceIndex
  REQUIRE e1.timestamp <= e2.timestamp

CONSTRAINT created_event_is_first
  FOR (n:EvidenceNode)-[r:HAS_PROVENANCE_EVENT]->(e:ProvenanceEvent)
  WHERE e.action = 'created'
    AND r.sequenceIndex IS NOT NULL
  REQUIRE r.sequenceIndex = 0
```

#### 1.4.8 EG Acyclicity Constraints

```
CONSTRAINT supersedes_acyclic
  NO CYCLE ON (:EvidenceNode)-[:SUPERSEDES]->(:EvidenceNode)

CONSTRAINT derives_from_acyclic
  NO CYCLE ON (:EvidenceNode)-[:DERIVES_FROM]->(:EvidenceNode)

CONSTRAINT part_of_acyclic
  NO CYCLE ON (:EvidenceNode)-[:PART_OF]->(:EvidenceNode)
```

---

### 1.5 EG Entailment Rules

All EG entailment rules produce relationships or property mutations marked with `inferred: true` and a `rule` identifier.

#### 1.5.1 Transitivity Rules (EG — Tier 1)

```
RULE transitive_derives_from
  WHEN  (a:EvidenceNode)-[:DERIVES_FROM]->(b:EvidenceNode)
    AND (b)-[:DERIVES_FROM]->(c:EvidenceNode)
    AND a <> c
    AND NOT EXISTS { (a)-[:DERIVES_FROM {inferred: true, rule: 'transitive_derives_from'}]->(c) }
  THEN  CREATE (a)-[:DERIVES_FROM {
          inferred: true,
          rule: 'transitive_derives_from',
          uid: randomUUID()
        }]->(c)

RULE transitive_supersedes
  WHEN  (a:EvidenceNode)-[:SUPERSEDES]->(b:EvidenceNode)
    AND (b)-[:SUPERSEDES]->(c:EvidenceNode)
    AND a <> c
    AND NOT EXISTS { (a)-[:SUPERSEDES {inferred: true, rule: 'transitive_supersedes'}]->(c) }
  THEN  CREATE (a)-[:SUPERSEDES {
          inferred: true,
          rule: 'transitive_supersedes',
          uid: randomUUID()
        }]->(c)

RULE transitive_part_of
  WHEN  (a:EvidenceNode)-[:PART_OF]->(b:EvidenceNode)
    AND (b)-[:PART_OF]->(c:EvidenceNode)
    AND a <> c
    AND NOT EXISTS { (a)-[:PART_OF {inferred: true, rule: 'transitive_part_of'}]->(c) }
  THEN  CREATE (a)-[:PART_OF {
          inferred: true,
          rule: 'transitive_part_of',
          uid: randomUUID()
        }]->(c)
```

#### 1.5.2 Symmetry Rules (EG — Tier 1)

```
RULE symmetric_contradicts
  WHEN  (a:EvidenceNode)-[:CONTRADICTS]->(b:EvidenceNode)
    AND NOT EXISTS { (b)-[:CONTRADICTS]->(a) }
  THEN  CREATE (b)-[:CONTRADICTS {
          inferred: true,
          rule: 'symmetric_contradicts',
          uid: randomUUID()
        }]->(a)

RULE symmetric_corroborates
  WHEN  (a:EvidenceNode)-[:CORROBORATES]->(b:EvidenceNode)
    AND NOT EXISTS { (b)-[:CORROBORATES]->(a) }
  THEN  CREATE (b)-[:CORROBORATES {
          inferred: true,
          rule: 'symmetric_corroborates',
          uid: randomUUID()
        }]->(a)
```

#### 1.5.3 Provenance Inheritance (EG — Tier 1)

```
RULE version_inherits_provenance
  WHEN  (a:EvidenceNode)-[:VERSION_OF]->(b:EvidenceNode)
    AND a.sourceOrigin IS NULL
    AND b.sourceOrigin IS NOT NULL
  THEN  SET a.sourceOrigin = b.sourceOrigin
        SET a.sourceCategory = b.sourceCategory
        SET a.domainMetadata.provenanceInherited = true
        SET a.domainMetadata.provenanceInheritedFrom = b.uid
```

#### 1.5.4 Corroboration Boost (EG — Tier 2)

```
RULE corroborated_evidence_boosts_reliability
  WHEN  (e:EvidenceNode)
    AND COUNT{ (e)<-[:CORROBORATES]-() } >= 2
    AND e.reliabilityScore IS NOT NULL
    AND e.reliabilityScore < 1.0
    AND (e.domainMetadata.reliabilityBoosted IS NULL
         OR e.domainMetadata.reliabilityBoosted = false)
  THEN  SET e.reliabilityScore = CASE
          WHEN e.reliabilityScore + 0.1 > 1.0 THEN 1.0
          ELSE e.reliabilityScore + 0.1
        END
        SET e.domainMetadata.reliabilityBoosted = true
        SET e.domainMetadata.reliabilityBoostRule = 'corroborated_evidence'
```

---
---

## 2. Reasoning Graph (RG)

The Reasoning Graph captures claims, inference steps, defeaters, and their argumentative relationships. An RG is rooted at a `:GraphRoot` node with `graphType = 'ReasoningGraph'`.

---

### 2.1 RG Node Labels

#### 2.1.1 `:Claim`

A propositional assertion carrying its own epistemic status, confidence assessment, scope, and domain extension point.

| Property | Type | Required | Key | Description |
|---|---|---|---|---|
| `uid` | `UUID` | ✓ | PK | |
| `type` | `String` | ✓ | | `hypothesis`, `finding`, `conclusion`, `assumption`, `observation`, `definition`, `constraint`, `other` |
| `domainType` | `String` | | | Domain-profile-specific subtype |
| `statement` | `String` | ✓ | | Propositional content in natural language |
| `formalExpression` | `String` | | | Logical encoding |
| `status` | `String` | | | `proposed`, `supported`, `weakly-supported`, `contested`, `refuted`, `withdrawn`, `undetermined` |
| `confidenceScore` | `Float` | | | `[0.0, 1.0]` |
| `confidenceMethod` | `String` | | | `expert-judgment`, `algorithmic`, `bayesian`, `voting`, `heuristic`, `unspecified` |
| `confidenceRationale` | `String` | | | |
| `confidenceAssessedAt` | `DateTime` | | | |
| `assertedAt` | `DateTime` | | | |
| `scopeTempStart` | `DateTime` | | | |
| `scopeTempEnd` | `DateTime` | | | |
| `scopeTempPrecision` | `String` | | | `exact`, `day`, `month`, `year`, `approximate`, `unknown` |
| `scopeSpatial` | `String` | | | Geographic or spatial scope |
| `scopePopulation` | `String` | | | Set of entities the claim applies to |
| `scopeConditions` | `String` | | | Qualifying conditions |
| `tags` | `String[]` | | | |
| `domainMetadata` | `Map` | | | |

#### 2.1.2 `:Inference`

A reasoning step connecting premises to a conclusion via an identified rule or method.

| Property | Type | Required | Key | Description |
|---|---|---|---|---|
| `uid` | `UUID` | ✓ | PK | |
| `type` | `String` | ✓ | | `deductive`, `inductive`, `abductive`, `analogical`, `statistical`, `causal`, `eliminative`, `other` |
| `label` | `String` | | | Human-readable name for this step |
| `ruleName` | `String` | | | Name of the inference rule applied |
| `ruleFormalExpr` | `String` | | | Formal representation of the rule |
| `ruleSource` | `String` | | | Provenance of the rule |
| `confidenceScore` | `Float` | | | `[0.0, 1.0]` |
| `confidenceMethod` | `String` | | | `expert-judgment`, `algorithmic`, `bayesian`, `voting`, `heuristic`, `unspecified` |
| `confidenceRationale` | `String` | | | |
| `confidenceAssessedAt` | `DateTime` | | | |
| `justification` | `String` | | | Free-text explanation of validity |
| `performedAt` | `DateTime` | | | |
| `domainMetadata` | `Map` | | | |

#### 2.1.3 `:Defeater`

A known or potential challenge to an inference step.

| Property | Type | Required | Key | Description |
|---|---|---|---|---|
| `uid` | `UUID` | ✓ | PK | |
| `type` | `String` | ✓ | | `rebutting`, `undercutting`, `undermining` |
| `description` | `String` | | | |

---

### 2.2 RG Relationships

#### 2.2.1 Graph Membership

**`(:GraphRoot)-[:CONTAINS_CLAIM]->(:Claim)`** — No properties.

**`(:GraphRoot)-[:CONTAINS_INFERENCE]->(:Inference)`** — No properties.

#### 2.2.2 Claim-to-Claim and Claim/Inference Argumentative Relationships

All of the following connect reasoning-layer nodes and share this common property set:

| Property | Type | Required | Description |
|---|---|---|---|
| `uid` | `UUID` | ✓ | |
| `strength` | `Float` | | `[0.0, 1.0]` — how strongly this relationship holds |
| `justification` | `String` | | |
| `assertedByUid` | `String` | | FK → `:Agent.uid` |
| `assertedAt` | `DateTime` | | |
| `domainMetadata` | `Map` | | |

Valid source → target label combinations: `:Claim` → `:Claim`, `:Claim` → `:Inference`, `:Inference` → `:Claim`, `:Inference` → `:Inference`.

**`-[:SUPPORTS]->`**

**`-[:UNDERMINES]->`**

**`-[:REBUTS]->`**

**`-[:QUALIFIES]->`**

**`-[:ASSUMES]->`**

**`-[:DEPENDS_ON]->`**

**`-[:GENERALIZES]->`**

**`-[:SPECIALIZES]->`**

**`-[:ALTERNATIVE_TO]->`**

**`-[:ENTAILS]->`**

**`-[:OTHER_REASONING_REL]->`** — Catch-all with optional `subtype` property (String).

#### 2.2.3 Inference Structural Relationships

**`(:Inference)-[:HAS_PREMISE]->(:Claim)`**

| Property | Type | Required | Description |
|---|---|---|---|
| `role` | `String` | | e.g. `major-premise`, `minor-premise`, `base-case`, `analogue` |

**`(:Inference)-[:HAS_CONCLUSION]->(:Claim)`** — No properties. Exactly one per Inference.

**`(:Inference)-[:PERFORMED_BY]->(:Agent)`** — No properties.

**`(:Inference)-[:HAS_DEFEATER]->(:Defeater)`** — No properties.

**`(:Defeater)-[:REFERENCES_CLAIM]->(:Claim)`** — No properties. Optional — links a defeater to the specific claim that acts as the defeating argument.

#### 2.2.4 Agent Assertion & Assessment Relationships

**`(:Claim)-[:ASSERTED_BY]->(:Agent)`** — No properties.

**`(:Claim)-[:CONFIDENCE_ASSESSED_BY]->(:Agent)`** — No properties.

**`(:Inference)-[:CONFIDENCE_ASSESSED_BY]->(:Agent)`** — No properties.

---

### 2.3 RG Keys

```
KEY claim_pk
  FOR (n:Claim) ON (n.uid)

KEY inference_pk
  FOR (n:Inference) ON (n.uid)

KEY defeater_pk
  FOR (n:Defeater) ON (n.uid)
```

---

### 2.4 RG Constraints

#### 2.4.1 Existence Constraints

```
CONSTRAINT claim_has_type
  FOR (n:Claim) REQUIRE n.type IS NOT NULL

CONSTRAINT claim_has_statement
  FOR (n:Claim) REQUIRE n.statement IS NOT NULL

CONSTRAINT inference_has_type
  FOR (n:Inference) REQUIRE n.type IS NOT NULL

CONSTRAINT defeater_has_type
  FOR (n:Defeater) REQUIRE n.type IS NOT NULL
```

#### 2.4.2 Enumeration Constraints

```
CONSTRAINT claim_type_enum
  FOR (n:Claim)
  REQUIRE n.type IN [
    'hypothesis', 'finding', 'conclusion', 'assumption',
    'observation', 'definition', 'constraint', 'other'
  ]

CONSTRAINT claim_status_enum
  FOR (n:Claim) WHERE n.status IS NOT NULL
  REQUIRE n.status IN [
    'proposed', 'supported', 'weakly-supported', 'contested',
    'refuted', 'withdrawn', 'undetermined'
  ]

CONSTRAINT claim_confidence_method_enum
  FOR (n:Claim) WHERE n.confidenceMethod IS NOT NULL
  REQUIRE n.confidenceMethod IN [
    'expert-judgment', 'algorithmic', 'bayesian',
    'voting', 'heuristic', 'unspecified'
  ]

CONSTRAINT claim_scope_precision_enum
  FOR (n:Claim) WHERE n.scopeTempPrecision IS NOT NULL
  REQUIRE n.scopeTempPrecision IN [
    'exact', 'day', 'month', 'year', 'approximate', 'unknown'
  ]

CONSTRAINT inference_type_enum
  FOR (n:Inference)
  REQUIRE n.type IN [
    'deductive', 'inductive', 'abductive', 'analogical',
    'statistical', 'causal', 'eliminative', 'other'
  ]

CONSTRAINT inference_confidence_method_enum
  FOR (n:Inference) WHERE n.confidenceMethod IS NOT NULL
  REQUIRE n.confidenceMethod IN [
    'expert-judgment', 'algorithmic', 'bayesian',
    'voting', 'heuristic', 'unspecified'
  ]

CONSTRAINT defeater_type_enum
  FOR (n:Defeater)
  REQUIRE n.type IN ['rebutting', 'undercutting', 'undermining']
```

#### 2.4.3 Range Constraints

```
CONSTRAINT claim_confidence_range
  FOR (n:Claim) WHERE n.confidenceScore IS NOT NULL
  REQUIRE 0.0 <= n.confidenceScore <= 1.0

CONSTRAINT inference_confidence_range
  FOR (n:Inference) WHERE n.confidenceScore IS NOT NULL
  REQUIRE 0.0 <= n.confidenceScore <= 1.0

CONSTRAINT reasoning_edge_strength_range
  FOR ()-[r]->() WHERE r.strength IS NOT NULL
  REQUIRE 0.0 <= r.strength <= 1.0
```

#### 2.4.4 Structural & Cardinality Constraints

```
CONSTRAINT inference_has_at_least_one_premise
  FOR (i:Inference)
  REQUIRE COUNT{ (i)-[:HAS_PREMISE]->() } >= 1

CONSTRAINT inference_has_exactly_one_conclusion
  FOR (i:Inference)
  REQUIRE COUNT{ (i)-[:HAS_CONCLUSION]->(:Claim) } = 1

CONSTRAINT claim_belongs_to_graph
  FOR (n:Claim)
  REQUIRE COUNT{ (:GraphRoot)-[:CONTAINS_CLAIM]->(n) } >= 1

CONSTRAINT inference_belongs_to_graph
  FOR (n:Inference)
  REQUIRE COUNT{ (:GraphRoot)-[:CONTAINS_INFERENCE]->(n) } >= 1

CONSTRAINT contains_claim_source_is_reasoning_graph
  FOR (g:GraphRoot)-[:CONTAINS_CLAIM]->()
  REQUIRE g.graphType = 'ReasoningGraph'

CONSTRAINT contains_inference_source_is_reasoning_graph
  FOR (g:GraphRoot)-[:CONTAINS_INFERENCE]->()
  REQUIRE g.graphType = 'ReasoningGraph'

CONSTRAINT has_conclusion_target_is_claim
  FOR (s)-[:HAS_CONCLUSION]->(t)
  REQUIRE s:Inference AND t:Claim

CONSTRAINT has_premise_source_is_inference_rg
  FOR (s)-[:HAS_PREMISE]->(t)
  WHERE t:Claim
  REQUIRE s:Inference

CONSTRAINT has_defeater_source_is_inference
  FOR (s)-[:HAS_DEFEATER]->(t)
  REQUIRE s:Inference AND t:Defeater

CONSTRAINT defeater_claim_ref_valid
  FOR (d:Defeater)-[:REFERENCES_CLAIM]->(c)
  REQUIRE c:Claim

CONSTRAINT reasoning_edge_endpoint_types
  FOR (s)-[r]->(t)
  WHERE type(r) IN [
    'SUPPORTS', 'UNDERMINES', 'REBUTS', 'QUALIFIES', 'ASSUMES',
    'DEPENDS_ON', 'GENERALIZES', 'SPECIALIZES', 'ALTERNATIVE_TO',
    'ENTAILS', 'OTHER_REASONING_REL'
  ]
  REQUIRE (s:Claim OR s:Inference) AND (t:Claim OR t:Inference)
```

#### 2.4.5 Referential Integrity Constraints

```
CONSTRAINT rg_edge_asserted_by_references_agent
  FOR (s)-[r]->(t)
  WHERE r.assertedByUid IS NOT NULL
    AND (s:Claim OR s:Inference)
    AND (t:Claim OR t:Inference)
  REQUIRE EXISTS { (a:Agent) WHERE a.uid = r.assertedByUid }
```

#### 2.4.6 Temporal Constraints

```
CONSTRAINT temporal_range_ordering_claim_scope
  FOR (n:Claim)
  WHERE n.scopeTempStart IS NOT NULL AND n.scopeTempEnd IS NOT NULL
  REQUIRE n.scopeTempStart <= n.scopeTempEnd
```

#### 2.4.7 RG Acyclicity Constraints

```
CONSTRAINT entails_acyclic
  NO CYCLE ON (:Claim)-[:ENTAILS]->(:Claim)

CONSTRAINT depends_on_acyclic
  NO CYCLE ON (:Claim)-[:DEPENDS_ON]->(:Claim)

CONSTRAINT inference_chain_acyclic
  NO CYCLE ON path WHERE ALL relationships(path) HAVE type(r) IN
    ['HAS_PREMISE', 'HAS_CONCLUSION']
```

---

### 2.5 RG Entailment Rules

All RG entailment rules produce relationships or property mutations marked with `inferred: true` and a `rule` identifier.

#### 2.5.1 Transitivity Rules (RG — Tier 1)

```
RULE transitive_entails
  WHEN  (a:Claim)-[:ENTAILS]->(b:Claim)
    AND (b)-[:ENTAILS]->(c:Claim)
    AND a <> c
    AND NOT EXISTS { (a)-[:ENTAILS {inferred: true, rule: 'transitive_entails'}]->(c) }
  THEN  CREATE (a)-[:ENTAILS {
          inferred: true,
          rule: 'transitive_entails',
          uid: randomUUID()
        }]->(c)

RULE transitive_depends_on
  WHEN  (a:Claim)-[:DEPENDS_ON]->(b:Claim)
    AND (b)-[:DEPENDS_ON]->(c:Claim)
    AND a <> c
    AND NOT EXISTS { (a)-[:DEPENDS_ON {inferred: true, rule: 'transitive_depends_on'}]->(c) }
  THEN  CREATE (a)-[:DEPENDS_ON {
          inferred: true,
          rule: 'transitive_depends_on',
          uid: randomUUID()
        }]->(c)
```

#### 2.5.2 Symmetry Rules (RG — Tier 1)

```
RULE symmetric_alternative_to
  WHEN  (a:Claim)-[:ALTERNATIVE_TO]->(b:Claim)
    AND NOT EXISTS { (b)-[:ALTERNATIVE_TO]->(a) }
  THEN  CREATE (b)-[:ALTERNATIVE_TO {
          inferred: true,
          rule: 'symmetric_alternative_to',
          uid: randomUUID()
        }]->(a)
```

#### 2.5.3 Inverse Rules (RG — Tier 1)

```
RULE generalizes_inverse_specializes
  WHEN  (a:Claim)-[:GENERALIZES]->(b:Claim)
    AND NOT EXISTS { (b)-[:SPECIALIZES {inferred: true, rule: 'generalizes_inverse'}]->(a) }
  THEN  CREATE (b)-[:SPECIALIZES {
          inferred: true,
          rule: 'generalizes_inverse',
          uid: randomUUID()
        }]->(a)

RULE specializes_inverse_generalizes
  WHEN  (a:Claim)-[:SPECIALIZES]->(b:Claim)
    AND NOT EXISTS { (b)-[:GENERALIZES {inferred: true, rule: 'specializes_inverse'}]->(a) }
  THEN  CREATE (b)-[:GENERALIZES {
          inferred: true,
          rule: 'specializes_inverse',
          uid: randomUUID()
        }]->(a)
```

#### 2.5.4 Scope Inheritance (RG — Tier 1)

```
RULE specialization_inherits_temporal_scope
  WHEN  (general:Claim)-[:GENERALIZES]->(specific:Claim)
    AND general.scopeTempStart IS NOT NULL
    AND specific.scopeTempStart IS NULL
  THEN  SET specific.scopeTempStart = general.scopeTempStart
        SET specific.scopeTempEnd = general.scopeTempEnd
        SET specific.domainMetadata.scopeInherited = true
        SET specific.domainMetadata.scopeInheritedFrom = general.uid
```

#### 2.5.5 Support Propagation (RG — Tier 2)

```
RULE fully_supported_inference_supports_conclusion
  WHEN  (i:Inference)-[:HAS_CONCLUSION]->(c:Claim)
    AND i.confidenceScore IS NOT NULL
    AND i.confidenceScore >= 0.7
    AND ALL premises IN [(i)-[:HAS_PREMISE]->(p:Claim) | p] SATISFY (
          p.status IN ['supported', 'proposed']
        )
    AND COUNT{ (i)-[:HAS_DEFEATER]->() } = 0
    AND (c.status IS NULL OR c.status IN ['proposed', 'undetermined'])
  THEN  SET c.status = 'supported'
        SET c.domainMetadata.autoSupportedBy = i.uid
        SET c.domainMetadata.autoSupportRule = 'fully_supported_inference'

RULE entailed_claim_inherits_support
  WHEN  (a:Claim)-[:ENTAILS]->(b:Claim)
    AND a.status = 'supported'
    AND (b.status IS NULL OR b.status IN ['proposed', 'undetermined'])
  THEN  SET b.status = 'supported'
        SET b.domainMetadata.supportedViaEntailment = a.uid

RULE deductive_inference_creates_entailment
  WHEN  (i:Inference {type: 'deductive'})-[:HAS_CONCLUSION]->(c:Claim)
    AND i.confidenceScore IS NOT NULL
    AND i.confidenceScore >= 0.9
    AND ALL premises IN [(i)-[:HAS_PREMISE]->(p:Claim) | p] SATISFY (
          p.status = 'supported'
        )
    AND NOT EXISTS {
          (i)-[:HAS_PREMISE]->(e:EvidenceNode)
        }
  THEN  FOR EACH premise IN [(i)-[:HAS_PREMISE]->(p:Claim) | p]:
          WHERE NOT EXISTS { (premise)-[:ENTAILS]->(c) }
          CREATE (premise)-[:ENTAILS {
            inferred: true,
            rule: 'deductive_entailment',
            uid: randomUUID()
          }]->(c)
```

#### 2.5.6 Defeater Propagation (RG — Tier 3)

```
RULE rebutting_defeater_contests_conclusion
  WHEN  (i:Inference)-[:HAS_CONCLUSION]->(c:Claim)
    AND (i)-[:HAS_DEFEATER]->(d:Defeater {type: 'rebutting'})
  THEN  SET c.status = 'contested'
        SET c.domainMetadata.defeaterFlag = d.uid
        SET c.domainMetadata.defeaterRule = 'rebutting_defeater_contests'

RULE undercutting_defeater_weakens_inference
  WHEN  (i:Inference)-[:HAS_DEFEATER]->(d:Defeater {type: 'undercutting'})
    AND (i)-[:HAS_CONCLUSION]->(c:Claim)
    AND i.confidenceScore IS NOT NULL
    AND i.confidenceScore > 0.0
    AND (i.domainMetadata.undercut IS NULL OR i.domainMetadata.undercut = false)
  THEN  SET i.confidenceScore = i.confidenceScore * 0.5
        SET i.domainMetadata.undercut = true
        SET i.domainMetadata.undercutBy = d.uid
        SET c.status = 'weakly-supported'
        SET c.domainMetadata.weakenedByUndercut = d.uid

RULE undermining_defeater_contests_premise
  WHEN  (i:Inference)-[:HAS_DEFEATER]->(d:Defeater {type: 'undermining'})
    AND (d)-[:REFERENCES_CLAIM]->(p:Claim)
    AND (i)-[:HAS_PREMISE]->(p)
  THEN  SET p.status = 'contested'
        SET p.domainMetadata.underminedBy = d.uid
        SET p.domainMetadata.underminingRule = 'undermining_defeater_contests'
```

#### 2.5.7 Contradiction & Rebuttal Propagation (RG — Tier 3)

```
RULE rebuts_relationship_contests_target
  WHEN  (a:Claim)-[:REBUTS]->(b:Claim)
    AND a.status = 'supported'
    AND b.status = 'supported'
  THEN  SET b.status = 'contested'
        SET b.domainMetadata.contestedByRebuttal = a.uid
```

#### 2.5.8 Weakened Premise Propagation (RG — Tier 3)

```
RULE weakened_premise_weakens_conclusion
  WHEN  (i:Inference)-[:HAS_PREMISE]->(p:Claim)
    AND p.status IN ['contested', 'refuted', 'withdrawn']
    AND (i)-[:HAS_CONCLUSION]->(c:Claim)
    AND c.status = 'supported'
  THEN  SET c.status = 'weakly-supported'
        SET c.domainMetadata.weakenedByPremise = p.uid
        SET c.domainMetadata.weakenedRule = 'weakened_premise'
```

---
---

## 3. Bridge Layer

The Bridge Layer connects elements of the Reasoning Graph to elements of the Evidence Graph. Bridges are the only relationships that span both graph types.

---

### 3.1 Bridge Relationships

#### 3.1.1 Evidentiary Grounding

**`(:Claim)-[:GROUNDED_BY]->(:EvidenceNode)`**

| Property | Type | Required | Description |
|---|---|---|---|
| `uid` | `UUID` | ✓ | |
| `role` | `String` | ✓ | `grounds`, `warrant`, `backing`, `rebuttal`, `qualifier`, `illustration`, `counter-example`, `other` |
| `relevance` | `Float` | | `[0.0, 1.0]` |
| `excerpt` | `String` | | Specific portion of evidence cited |
| `justification` | `String` | | |
| `evidenceGraphId` | `UUID` | | FK → `:GraphRoot.uid` — identifies which EG contains the target node |

**`(:Inference)-[:GROUNDED_BY]->(:EvidenceNode)`** — Same property set as above.

#### 3.1.2 Inference Premise from Evidence

**`(:Inference)-[:HAS_PREMISE]->(:EvidenceNode)`**

| Property | Type | Required | Description |
|---|---|---|---|
| `role` | `String` | | e.g. `major-premise`, `minor-premise`, `base-case`, `analogue` |

This is the HAS_PREMISE variant whose target is an EvidenceNode rather than a Claim.

#### 3.1.3 Defeater Reference to Evidence

**`(:Defeater)-[:REFERENCES_EVIDENCE]->(:EvidenceNode)`** — No properties. Optional — links a defeater to the specific evidence that acts as the defeating argument.

---

### 3.2 Bridge Constraints

#### 3.2.1 Existence Constraints

```
CONSTRAINT grounded_by_has_role
  FOR ()-[r:GROUNDED_BY]->()
  REQUIRE r.role IS NOT NULL
```

#### 3.2.2 Enumeration Constraints

```
CONSTRAINT grounded_by_role_enum
  FOR ()-[r:GROUNDED_BY]->()
  REQUIRE r.role IN [
    'grounds', 'warrant', 'backing', 'rebuttal',
    'qualifier', 'illustration', 'counter-example', 'other'
  ]
```

#### 3.2.3 Range Constraints

```
CONSTRAINT grounded_by_relevance_range
  FOR ()-[r:GROUNDED_BY]->() WHERE r.relevance IS NOT NULL
  REQUIRE 0.0 <= r.relevance <= 1.0
```

#### 3.2.4 Structural Constraints

```
CONSTRAINT grounded_by_endpoint_types
  FOR (source)-[:GROUNDED_BY]->(target)
  REQUIRE (source:Claim OR source:Inference) AND target:EvidenceNode

CONSTRAINT has_premise_to_evidence_source_is_inference
  FOR (s)-[:HAS_PREMISE]->(t)
  WHERE t:EvidenceNode
  REQUIRE s:Inference

CONSTRAINT defeater_evidence_ref_valid
  FOR (d:Defeater)-[:REFERENCES_EVIDENCE]->(e)
  REQUIRE e:EvidenceNode
```

#### 3.2.5 Referential Integrity Constraints

```
CONSTRAINT grounded_by_graph_ref_consistent
  FOR (s)-[r:GROUNDED_BY]->(t:EvidenceNode)
  WHERE r.evidenceGraphId IS NOT NULL
  REQUIRE EXISTS {
    (g:GraphRoot {uid: r.evidenceGraphId})-[:CONTAINS_NODE]->(t)
  }
```

---

### 3.3 Bridge Entailment Rules

All Bridge entailment rules span the EG↔RG boundary. They produce inferred relationships or property mutations marked with `inferred: true` and a `rule` identifier.

#### 3.3.1 Independent Corroboration via Grounding (Bridge — Tier 2)

```
RULE independent_support_creates_corroboration
  WHEN  (c:Claim)-[:GROUNDED_BY {role: 'grounds'}]->(e1:EvidenceNode)
    AND (c)-[:GROUNDED_BY {role: 'grounds'}]->(e2:EvidenceNode)
    AND e1 <> e2
    AND NOT EXISTS { (e1)-[:DERIVES_FROM*]->(e2) }
    AND NOT EXISTS { (e2)-[:DERIVES_FROM*]->(e1) }
    AND NOT EXISTS { (e1)-[:CORROBORATES]->(e2) }
  THEN  CREATE (e1)-[:CORROBORATES {
          inferred: true,
          rule: 'independent_support',
          uid: randomUUID()
        }]->(e2)
```

Note: This rule reads from Bridge edges (`:GROUNDED_BY`) and writes into the EG (`:CORROBORATES` between evidence nodes).

#### 3.3.2 Fully Supported Inference with Evidence Premises (Bridge — Tier 2)

```
RULE fully_supported_inference_with_evidence_supports_conclusion
  WHEN  (i:Inference)-[:HAS_CONCLUSION]->(c:Claim)
    AND i.confidenceScore IS NOT NULL
    AND i.confidenceScore >= 0.7
    AND ALL claim_premises IN [(i)-[:HAS_PREMISE]->(p:Claim) | p] SATISFY (
          p.status IN ['supported', 'proposed']
        )
    AND ALL evidence_premises IN [(i)-[:HAS_PREMISE]->(e:EvidenceNode) | e] SATISFY (
          e.lifecycleStatus IN ['active', 'draft']
        )
    AND COUNT{ (i)-[:HAS_DEFEATER]->() } = 0
    AND (c.status IS NULL OR c.status IN ['proposed', 'undetermined'])
  THEN  SET c.status = 'supported'
        SET c.domainMetadata.autoSupportedBy = i.uid
        SET c.domainMetadata.autoSupportRule = 'fully_supported_inference_with_evidence'
```

#### 3.3.3 Retracted Evidence Contests Grounded Claims (Bridge — Tier 3)

```
RULE retracted_evidence_contests_grounded_claims
  WHEN  (c:Claim)-[:GROUNDED_BY {role: 'grounds'}]->(e:EvidenceNode)
    AND e.lifecycleStatus = 'retracted'
    AND COUNT{
          (c)-[:GROUNDED_BY {role: 'grounds'}]->(other:EvidenceNode)
          WHERE other.lifecycleStatus IN ['active', 'draft']
            AND other <> e
        } = 0
  THEN  SET c.status = 'contested'
        SET c.domainMetadata.autoContestReason = 'all grounding evidence retracted'
        SET c.domainMetadata.autoContestRule = 'retracted_evidence_contests'
```

#### 3.3.4 Superseded Evidence Weakens Grounded Claims (Bridge — Tier 3)

```
RULE superseded_evidence_weakens_grounded_claims
  WHEN  (c:Claim)-[:GROUNDED_BY {role: 'grounds'}]->(e:EvidenceNode)
    AND e.lifecycleStatus = 'superseded'
    AND (replacement:EvidenceNode)-[:SUPERSEDES]->(e)
    AND NOT EXISTS { (c)-[:GROUNDED_BY]->(replacement) }
  THEN  SET c.status = 'weakly-supported'
        SET c.domainMetadata.autoWeakenReason = 'grounding evidence superseded; replacement not linked'
        SET c.domainMetadata.autoWeakenRule = 'superseded_evidence_weakens'
```

#### 3.3.5 Deprecated Evidence Weakens Grounded Claims (Bridge — Tier 3)

```
RULE deprecated_evidence_weakens_grounded_claims
  WHEN  (c:Claim)-[:GROUNDED_BY {role: 'grounds'}]->(e:EvidenceNode)
    AND e.lifecycleStatus IN ['deprecated', 'archived']
    AND c.status = 'supported'
  THEN  SET c.status = 'weakly-supported'
        SET c.domainMetadata.autoWeakenReason = 'grounding evidence deprecated/archived'
        SET c.domainMetadata.autoWeakenRule = 'deprecated_evidence_weakens'
```

#### 3.3.6 Contradicting Evidence Contests Grounded Claims (Bridge — Tier 3)

```
RULE contradicting_evidence_contests_supported_claims
  WHEN  (c1:Claim)-[:GROUNDED_BY {role: 'grounds'}]->(e1:EvidenceNode)
    AND (e1)-[:CONTRADICTS]->(e2:EvidenceNode)
    AND (c2:Claim)-[:GROUNDED_BY {role: 'grounds'}]->(e2)
    AND c1.status = 'supported'
    AND c2.status = 'supported'
  THEN  SET c1.status = 'contested'
        SET c2.status = 'contested'
        SET c1.domainMetadata.contestedByContradiction = e2.uid
        SET c2.domainMetadata.contestedByContradiction = e1.uid
```

---
---

## 4. Rule Execution Semantics

### 4.1 Tier Ordering

Rules execute in three priority tiers. Within each tier, rules fire in any order until no new facts or mutations are produced within that tier. Each tier draws from all three layers (EG, RG, Bridge) as applicable.

**Tier 1 — Structural Entailments:** EG §1.5.1–1.5.3, RG §2.5.1–2.5.4. These produce new relationships and inherit missing properties. They never mutate `status` or `confidenceScore`. They are idempotent — each rule's guard clause checks for the prior existence of the inferred fact before creating it.

**Tier 2 — Support Elevation:** EG §1.5.4, RG §2.5.5, Bridge §3.3.1–3.3.2. These elevate claim status to `supported` and boost reliability scores. They run after Tier 1 so that all transitive, symmetric, and inverse edges are available for premise and corroboration queries.

**Tier 3 — Degradation:** RG §2.5.6–2.5.8, Bridge §3.3.3–3.3.6. These lower claim status or inference confidence. They run after Tier 2 so that the support baseline is established before defeaters, contradictions, and lifecycle changes take effect.

### 4.2 Conflict Resolution

When Tier 2 and Tier 3 rules both fire on the same node within the same fixpoint cycle, **Tier 3 wins**. The more conservative (degrading) assessment takes precedence. Both rule firings are logged in the node's `domainMetadata` for audit.

### 4.3 Termination Guarantee

Termination is guaranteed by four properties. The acyclicity constraints in EG §1.4.8 and RG §2.4.7 bound the depth of transitive closure, preventing unbounded Tier 1 expansion. The existence guards (`NOT EXISTS`) on all Tier 1 rules prevent duplicate creation. The bounded range `[0.0, 1.0]` on all confidence and reliability scores prevents unbounded numeric drift. The degradation-wins conflict policy and one-directional status transitions (supported → weakly-supported → contested; never the reverse within a single cycle) prevent oscillation.

### 4.4 Inferred Fact Provenance

Every relationship created by a rule carries `inferred: true` and `rule: '<rule_name>'`. Every property mutation performed by a rule writes the rule name and triggering entity UID into `domainMetadata`. This allows downstream consumers to distinguish human-asserted facts from machine-derived facts, trace the justification chain for any derived conclusion, and selectively retract inferred facts if a rule is revised.

---
---

## 5. Layer Summary

| Aspect | Evidence Graph (§1) | Reasoning Graph (§2) | Bridge (§3) |
|---|---|---|---|
| Root discriminator | `graphType = 'EvidenceGraph'` | `graphType = 'ReasoningGraph'` | N/A — connects both |
| Node labels | `EvidenceNode`, `ProvenanceEvent`, `ReliabilityFactor` | `Claim`, `Inference`, `Defeater` | None (uses nodes from EG & RG) |
| Intra-layer edge families | 11 evidence-to-evidence types + provenance/assessment edges | 11 argumentative types + inference structural edges | — |
| Bridge edges | — | — | `GROUNDED_BY`, `HAS_PREMISE→EvidenceNode`, `REFERENCES_EVIDENCE` |
| Tier 1 rules | 5 (transitivity, symmetry, inheritance) | 5 (transitivity, symmetry, inverse, scope) | 0 |
| Tier 2 rules | 1 (reliability boost) | 3 (support propagation, deductive entailment) | 2 (corroboration, evidence-premise support) |
| Tier 3 rules | 0 | 4 (defeaters, rebuttal, weakened premise) | 4 (retracted, superseded, deprecated, contradiction) |
| Acyclicity constraints | 3 (`SUPERSEDES`, `DERIVES_FROM`, `PART_OF`) | 3 (`ENTAILS`, `DEPENDS_ON`, inference chain) | Inherited from EG + RG |



## 1.1 Schema Amendments

### S1 — ProvenanceEvent action enum extension [Audit-03]

The three new action values represent genuinely distinct lifecycle transitions that cannot be mapped to existing enum members without semantic loss. `'linked'` is not `'modified'` — it denotes relationship creation, not property mutation. `'retracted'` is not `'archived'` — retraction implies evidential withdrawal, while archival implies preservation. `'superseded'` implies temporal displacement by a newer datum.

```
CONSTRAINT provenance_action_enum
  FOR (n:ProvenanceEvent)
  REQUIRE n.action IN [
    'created', 'collected', 'transferred', 'verified',
    'modified', 'reviewed', 'redacted', 'archived', 'restored',
    'linked',        -- entity resolution pairing (TRACE Phase 3)
    'retracted',     -- withdrawal from active status (MAP-TRANSFORM Phase 4)
    'superseded'     -- temporal replacement (CONFLICT Phase 4)
  ]
```

### S2 — `:CONTAINS_DEFEATER` relationship type [Audit-04]

CONFLICT creates Defeater nodes in the RG before CONSTRUCT creates Inferences. Without a graph-membership edge, these Defeaters are structurally orphaned — reachable only through BRIDGE cross-references. Adding `:CONTAINS_DEFEATER` mirrors the existing `:CONTAINS_CLAIM` and `:CONTAINS_INFERENCE` patterns.

```
Relationship type:
  (:GraphRoot {graphType: 'ReasoningGraph'})-[:CONTAINS_DEFEATER]->(:Defeater)
  Properties: { uid: String! }

CONSTRAINT contains_defeater_has_uid
  FOR ()-[r:CONTAINS_DEFEATER]->()
  REQUIRE r.uid IS NOT NULL

CONSTRAINT contains_defeater_source_is_reasoning_graph
  FOR (g:GraphRoot)-[:CONTAINS_DEFEATER]->(d:Defeater)
  REQUIRE g.graphType = 'ReasoningGraph'
```

### S3 — `:SUPERSEDES` relationship verification [Audit-06]

The schema already references `:SUPERSEDES` in the constraint `superseded_by_consistent_with_edge`, which requires a `:SUPERSEDES` edge to exist for any node whose `supersededBy` property is non-null. No new relationship type is needed. The algorithms must create this edge; the schema already mandates it.

Verify the following constraints exist (they were referenced but should be confirmed as present):

```
// Target lifecycle constraint
CONSTRAINT supersedes_target_lifecycle
  FOR (:EvidenceNode)-[:SUPERSEDES]->(target:EvidenceNode)
  REQUIRE target.lifecycleStatus IN ['superseded', 'deprecated', 'archived']

// Referential integrity
CONSTRAINT superseded_by_consistent_with_edge
  FOR (n:EvidenceNode) WHERE n.supersededBy IS NOT NULL
  REQUIRE EXISTS {
    (replacement:EvidenceNode)-[:SUPERSEDES]->(n)
    WHERE replacement.uid = n.supersededBy
  }

// Acyclicity
CONSTRAINT supersedes_acyclicity
  FOR path = (:EvidenceNode)-[:SUPERSEDES*]->(:EvidenceNode)
  REQUIRE isAcyclic(path)
```
