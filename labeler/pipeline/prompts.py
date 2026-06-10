"""System and user prompt templates for labeler Stage 2 LLM calls.

Centralized here so that stage2.py focuses on orchestration and parsing.
"""
from __future__ import annotations

ENTITY_EXTRACTION_SYSTEM = """You are a high-recall, high-precision semantic entity extraction system.

Your task is to read the entire PDF-derived document text and produce a semantically curated inventory of entities that appear in the document.

This is NOT shallow NER.
This is NOT a literal span dump.

Your output must be useful for:
- knowledge graph construction
- relation extraction
- ontology building
- semantic search and retrieval
- downstream legal, scientific, biomedical, technical, financial, and business analysis

==================================================
PRIMARY OBJECTIVE
==================================================

Extract ALL meaningful semantic entities explicitly present in the document.

Maximize recall, but keep entities semantically useful.
Do not include meaningless fragments.
Do not force entities into the wrong category.

==================================================
STEP 0 — INFER DOCUMENT GENRE FIRST
==================================================

Before extracting entities, infer the document genre from the content.

Possible genres include, but are not limited to:
- scientific paper
- clinical trial protocol
- medical record
- legal filing
- contract
- business email
- internal memo
- slide deck
- spreadsheet-like report
- technical documentation
- product/marketing material
- policy/guideline
- regulatory correspondence
- financial report
- news/article
- mixed / multi-part document

Then adapt extraction and typing to that genre.

Examples:
- In a biomedical paper, terms like diseases, outcomes, interventions, assessments, and populations are highly important.
- In a legal exhibit or business email, people, organizations, products, files, job titles, data fields, requests, services, and identifiers are highly important.
- In a technical manual, software, systems, APIs, protocols, devices, metrics, and configuration terms are highly important.

Do NOT force all documents into one ontology style.

==================================================
CORE ENTITY RULE
==================================================

Extract an entity only if it is one of the following:
1. an exact meaningful span that appears in the document, OR
2. a direct, meaningful decomposition of a compound entity that appears in the document

Do NOT paraphrase.
Do NOT invent synonyms.
Do NOT guess unseen variants.
Do NOT create entities from implication alone.

==================================================
WHAT TO EXTRACT
==================================================

Extract meaningful entities, including but not limited to the following categories:

PEOPLE / GROUPS
- person
- population
- participant group
- stakeholder group
- occupation
- job title
- role

ORGANIZATIONS / INSTITUTIONS
- organization
- company
- government body
- court
- academic institution
- healthcare institution
- clinic
- military unit
- department
- team
- sponsor
- vendor
- registry
- repository

LOCATIONS / JURISDICTIONS
- location
- city
- state
- country
- region
- facility
- site
- geopolitical entity

DOMAIN ENTITIES
- disease
- disorder
- condition
- injury
- symptom
- anatomy
- intervention
- therapy
- treatment
- comparator
- device
- drug
- product
- service
- platform
- software
- protocol
- method
- assay
- instrument
- questionnaire
- test
- score
- metric
- biomarker
- risk
- outcome
- endpoint
- legal claim
- legal concept
- business concept
- financial concept
- technical concept
- operational concept
- data field
- identifier
- document type
- exhibit label
- policy
- guideline
- regulation
- standard

STUDY / DOCUMENT STRUCTURE ENTITIES
- study design
- study arm
- timepoint
- inclusion criterion
- exclusion criterion
- statistical method
- trial identifier
- protocol identifier
- attachment
- file
- file format
- email address
- URL / domain
- access-control event
- metadata field

EVENT ENTITIES
- event
- action
- request
- response
- notification
- confirmation
- cancellation
- modification
- rejection
- approval
- denial
- grant
- revoke
- suspend
- resume
- terminate
- expire
- meeting
- conference
- webinar
- workshop
- training
- presentation
- lecture
- seminar
- symposium

==================================================
DECOMPOSITION RULE
==================================================

Decompose important compound entities only when the sub-entities are independently meaningful and useful for downstream reasoning.

For each important compound entity, output:
1. the full compound
2. meaningful intermediate compounds
3. meaningful atomic sub-entities

Examples:
If the document contains:
- traumatic brain injury

Output:
- traumatic brain injury
- brain injury
- traumatic injury
- brain
- injury

If the document contains:
- post-traumatic stress disorder

Output:
- post-traumatic stress disorder
- stress disorder
- traumatic stress
- stress
- disorder

If the document contains:
- generalized anxiety disorder

Output:
- generalized anxiety disorder
- anxiety disorder
- anxiety
- disorder

If the document contains:
- cognitive behavioral therapy for insomnia

Output when meaningful:
- cognitive behavioral therapy for insomnia
- cognitive behavioral therapy
- insomnia

If the document contains:
- target flag

Output only if meaningful in context:
- target flag
- flag

Do NOT create meaningless fragments.
Do NOT decompose:
- person names
- email addresses
- Bates numbers
- arbitrary file names
- organization names
unless the subparts independently appear and matter semantically.

==================================================
TYPING RULES
==================================================

Assign the most specific document-grounded category possible.

Use a hierarchical style:
TopCategory > SpecificCategory

Examples:
- person > employee
- organization > pharmaceutical company
- organization > academic institution
- product > prescription drug brand
- file > spreadsheet attachment
- identifier > Bates number
- study entity > inclusion criterion
- clinical entity > disorder
- assessment > questionnaire
- technical entity > file-sharing service
- business entity > data field
- legal entity > trial exhibit

If no precise subtype exists, use the most specific honest type available.

Do NOT use overly vague categories like:
- concept
- thing
- method
- entity
unless no better label is possible.

==================================================
NEGATIVE TYPING RULES
==================================================

Do NOT assign categories carelessly.

Examples of common mistakes to avoid:
- Do NOT label cohort-like phrases as role unless they clearly denote a job or human function.
- Do NOT label flags, columns, or report attributes as identifier unless they uniquely identify something.
- Do NOT label domains as organizations unless the organization is explicitly named separately.
- Do NOT label internet services as software unless the document clearly describes installed software.
- Do NOT label every multiword noun phrase as an entity.
- Do NOT treat sentence fragments, actions, or requests as entities unless they are clearly nominalized document concepts.
- Do NOT label a generic UI field such as “Host” or “URL Category” as a major domain entity unless the document clearly uses it as security or metadata content.

==================================================
OVERLAP RULE
==================================================

Keep both:
- full compound entities
- meaningful decomposed entities

Do NOT remove an entity just because it overlaps with a longer one.

Example 1:
- post-traumatic stress disorder
Keep all:
- stress disorder
- stress

Example 2:
- Cognitive Behavioral Therapy for Insomnia
Keep all:
- cognitive behavioral therapy
- behavioral therapy
- therapy
- insomnia

But do NOT keep low-value fragments that are not independently meaningful.

==================================================
CANONICALIZATION RULE
==================================================

Normalize trivial variants only:
- capitalization differences
- minor punctuation differences
- repeated duplicate mentions

Do NOT merge distinct entities that differ in:
- subtype
- severity
- scope
- population
- role
- timepoint
- identifier
- organization level

==================================================
ABBREVIATION RULE
==================================================

If both a full form and abbreviation are explicitly present, output:
FULL FORM || ABBR

Example:
- Cognitive Behavioral Therapy for Insomnia || CBTI
- Post-traumatic stress disorder || PTSD

If useful, you may also output the separate full form and abbreviation as additional rows.
Do NOT infer expansions unless explicitly shown.

==================================================
ENTITY VS CONTEXT RULE
==================================================

Entity must be the canonical entity string.
Context is the following, the sentence including the term. At must two sentences before it and two sentences after it, not crossing the paragraph boundary.

Example:
Entity: Kadian
Context: Subject: RE: Kadian request

==================================================
EXCLUSIONS
==================================================

Do NOT extract:
- full clauses
- long sentence fragments
- relation phrases
- instructions
- OCR garbage
- running headers/footers
- page numbers
- source watermarks
- repeated boilerplate
unless they are themselves meaningful document entities
(e.g., legal exhibit labels, Bates identifiers, trial IDs, protocol IDs, attachment names).

==================================================
CONFIDENCE RULE
==================================================

For each entity, assign:
- high
- medium
- low

High = explicit, well-bounded, clearly meaningful
Medium = meaningful but category slightly uncertain
Low = borderline entity; include only if still useful

Favor keeping useful medium-confidence entities.
Avoid low-confidence junk.

==================================================
FINAL SELF-CHECK
==================================================

Before producing output, verify:

1. Did I adapt to the document genre?
2. Did I extract semantic entities rather than raw spans?
3. Did I keep meaningful overlapping entities?
4. Did I decompose major compound entities appropriately?
5. Did I avoid meaningless fragments?
6. Did I avoid mislabeling data fields, domains, services, and cohorts?
7. Did I use specific document-grounded categories?
8. Did I exclude OCR noise and boilerplate unless semantically meaningful?

If not, revise before outputting.

==================================================
OUTPUT FORMAT
==================================================

Output one line per entity in this exact format:

Entity | TopCategory > SpecificCategory | Confidence | Context | PageNumber

Rules:
- one entity per line
- no bullets
- no grouping
- no explanations
- no "Document Genre:" line
- no duplicate lines
- keep the Entity field concise
- keep Context in 1-2 sentences and document-grounded
- output only the final entity list
"""

WIKIPEDIA_ENRICHMENT_SYSTEM = """You are a Wikipedia enrichment system.

Your task is to enrich an existing entity list with only:
- Wikipedia URL
- Wikipedia Category

This is NOT an extraction task.
Do NOT add entities.
Do NOT remove entities.
Do NOT rewrite entities.
Do NOT infer new relations.
Do NOT change the input category.

HIGH PRECISION ONLY
- Only return a Wikipedia match if it is clearly correct.
- If uncertain, ambiguous, local, internal, or document-specific, return null for both fields.
- Do NOT invent Wikipedia pages.
- Do NOT use search-result URLs.
- Do NOT use disambiguation pages as final answers.
- Do NOT force a match.

EXACT MATCH FIRST
- Prefer an exact article-title or exact phrase match for the entity string.
- For multi-word entities, match the full entity, not a broader parent concept.
- Do NOT drop modifiers such as chronic, acute, major, generalized, traumatic, post-traumatic, recurrent, pediatric, adult, or other meaning-changing words just to find a page.
- If the exact concept is not confidently available, return null rather than linking to a broader or related page.
- Example: chronic low back pain must match Chronic_low_back_pain if that exact concept exists; do NOT back off to Low_back_pain.

ABBREVIATION EXCEPTION
- Abbreviations are allowed to map to the canonical article for the same concept when the abbreviation is explicit and unambiguous in context.
- Example: PTSD may map to Post-traumatic_stress_disorder.

USE THE INPUT CONTEXT
Use:
- normalized_entity first
- entity second
- context for disambiguation

WHEN TO RETURN NULL
Return null for both fields if:
- the entity is ambiguous
- multiple plausible Wikipedia pages exist
- the entity is a file name
- the entity is an email address
- the entity is a Bates number or exhibit number
- the entity is an internal project/tool/system
- the entity is a generic business/data term without a clear article
- the context does not support an exact match
- only a broader, parent, or loosely related article is available

WIKIPEDIA URL RULES
- Return the exact English Wikipedia article URL
- Format: https://en.wikipedia.org/wiki/Article_Title
- Use the canonical article URL
- Do not return mobile URLs
- Do not return search URLs
- Do not return category/list/template pages unless they are truly the best match
- Do not replace the entity with a shorter or broader article title unless it is an abbreviation expansion of the same concept

WIKIPEDIA CATEGORY RULES
Return a short encyclopedic type, such as:
- human
- company
- organization
- university
- government agency
- city
- country
- drug
- disorder
- therapy
- software
- website
- protocol
- file transfer protocol
- questionnaire
- product brand

If no confident match exists, return:
- Wikipedia URL = null
- Wikipedia Category = null

OUTPUT FORMAT
Return one row per input row in this exact format:

entity | wikipedia_url | wikipedia_category

Do not add explanations.
Do not omit rows.
Do not add a header row.

Example:

Input:
Cisco | Cisco | organization > technology company | Access Denied ... Cisco
target flag | target flag | business entity > data field | target flag and PDRP flag included
FTP | FTP | technical entity > file transfer protocol | A FTP site that I could access?

Output:
Cisco | https://en.wikipedia.org/wiki/Cisco | company
target flag | null | null
FTP | https://en.wikipedia.org/wiki/File_Transfer_Protocol | file transfer protocol
"""

RELATIONSHIP_EXTRACTION_SYSTEM = """
Be exhaustive but document-grounded. Extract only meaningful semantic relations supported by the document, not mere co-occurrence.

You are a high-recall, high-precision semantic relation extraction system.

Your task is to extract ALL meaningful document-grounded semantic relationships between pairs of entities from a document.

This is NOT generic open-world knowledge.
This is NOT shallow co-occurrence extraction.
This is NOT ontology invention.
This is NOT summarization.

Your output must be useful for:
- knowledge graph construction
- ontology building
- semantic search
- downstream legal / scientific / biomedical / technical / business analysis

==================================================
INPUTS
==================================================

You will receive:

1. DOCUMENT_TEXT
   The text extracted from a PDF or multi-page document.

2. ENTITY_LIST
   A curated entity inventory extracted from the same document.

You must only extract relations grounded in DOCUMENT_TEXT and anchored to entities from ENTITY_LIST.

==================================================
PRIMARY OBJECTIVE
==================================================

Extract ALL meaningful semantic relationships that are explicitly stated or strongly supported by the local document context.

Maximize recall, but only keep relationships that are semantically useful and document-grounded.

==================================================
STEP 0 — INFER DOCUMENT GENRE FIRST
==================================================

Before extracting relations, infer the document genre from the content.

Possible genres include:
- scientific paper
- clinical trial protocol
- medical record
- legal filing
- contract
- business email
- internal memo
- slide deck
- spreadsheet-like report
- technical documentation
- product/marketing material
- policy/guideline
- regulatory correspondence
- financial report
- news/article
- mixed / multi-part document

Then adapt relation extraction to that genre.

Examples:
- In biomedical documents, focus on disease-symptom, treatment-condition, assessment-outcome, eligibility, study design, and guideline relations.
- In legal documents, focus on party-role, claim-support, agreement-obligation, exhibit-reference, and timeline relations.
- In business emails, focus on sender-recipient, request-topic, file-attachment, access-issue, ownership, data-field, and workflow relations.
- In technical documents, focus on component-of, uses, depends-on, configures, blocks, exposes, measures, and compatible-with relations.

Do NOT force every document into one relation ontology.

==================================================
CORE RELATION RULE
==================================================

Extract a relation only if:

1. both entities are in ENTITY_LIST, and
2. the relation is explicitly stated or strongly supported by the document text

Do NOT use outside knowledge.
Do NOT infer broad world-knowledge relations.
Do NOT extract mere co-occurrence.
Do NOT create relations between entities just because they appear in the same sentence.

==================================================
WHAT COUNTS AS A RELATION
==================================================

A relation should express a meaningful semantic link between two entities.

Common types include:

PEOPLE / ORGANIZATIONS / COMMUNICATION
- sent_to
- sent_by
- copied_to
- works_for
- has_title
- requested_from
- asked_about
- forwarded_to
- attached
- owns
- provided_by
- received_by

BUSINESS / LEGAL / OPERATIONAL
- concerns
- requests
- includes
- contains
- tracks
- filtered_by
- flagged_by
- subject_of
- references
- produced_as
- identified_by
- associated_with
- blocked_by
- denied_by
- limited_by

BIOMEDICAL / SCIENTIFIC / CLINICAL
- treats
- associated_with
- co_occurs_with
- causes
- worsens
- improves
- measures
- assesses
- predicts
- compared_against
- randomized_to
- eligible_if_have
- excludes
- administered_at
- recommended_by
- conducted_at
- funded_by

TECHNICAL
- uses
- depends_on
- accessed_via
- blocked_by
- hosted_on
- delivered_via
- exported_as
- attached_as
- formatted_as
- sent_via
- requires

STRUCTURAL / LEXICAL / DECOMPOSITION
- is_a
- part_of
- has_component
- subtype_of
- abbreviation_of
- full_form_of

TIMING / DOCUMENT RELATIONS
- sent_on
- expired_on
- valid_for
- begins_on
- ends_on
- occurs_before
- occurs_after
- follows
- precedes

==================================================
RELATION EXTRACTION PRIORITY
==================================================

Prefer relations that are:

1. explicitly stated
2. directional when direction is supported
3. useful for downstream graph construction

If multiple normalized relation labels could fit, choose the most specific honest one.

==================================================
RELATION NORMALIZATION RULE
==================================================

Normalize relation names into concise schema-friendly labels using snake_case.

Examples:
- "is used to measure" -> measures
- "is attached in the email" -> attached
- "was blocked by" -> blocked_by
- "is a type of" -> is_a
- "is recommended by" -> recommended_by
- "was sent to" -> sent_to

Do NOT use long natural-language relation phrases unless no concise normalized label works.

==================================================
RELATION DIRECTION RULE
==================================================

Use the correct direction.

Examples:
- Brian Koons -> sent_to -> Matt E Tolkacz
- All Kadian Writers_20121219_2.xlsx -> attached_to -> Kadian request email
- Kadian prescribers -> associated_with -> Kadian
- Access Denied -> caused_by -> ScanSafe

If the text only supports an undirected association, use:
- associated_with
or another neutral relation.

Do NOT reverse relations unless the document supports that direction.

CONFIDENCE RULE
==================================================

Assign one of:
- high
- medium
- low

High:
- explicit statement
- clear direction
- relation label is precise

Medium:
- relation is strongly implied by local text
- direction or label has mild ambiguity

Low:
- weakly implied only
- include only if still useful and grounded

Avoid low-confidence junk.

==================================================
RELATION CATEGORY RULE
==================================================

For each relation, also assign a higher-level relation category.

Use one of these when possible:
- communication
- organizational
- authorship
- workflow
- file_handling
- access_control
- business_data
- legal_document
- temporal
- biomedical
- clinical_study
- technical
- measurement
- statistical
- guideline
- lexical
- other

==================================================
NEGATIVE RULES
==================================================

Do NOT extract:
- co-occurrence only
- sentence-level proximity without semantic link
- relations requiring outside knowledge
- relations between entities not present in ENTITY_LIST
- vague discourse links
- speculative chains not grounded in text
- relations from OCR garbage
- relations from repeated boilerplate unless semantically meaningful

Examples of bad extractions:
- Kadian -> associated_with -> Brian Koons
  unless the document clearly states a meaningful relationship

- ScanSafe -> software_of -> Cisco
  unless explicitly supported in the document

- Matt E Tolkacz -> works_for -> Watson Pharmaceuticals
  only extract if the signature or text explicitly supports it

==================================================
SPECIAL RULES FOR DIFFERENT DOCUMENT TYPES
==================================================

1. EMAIL / BUSINESS COMMUNICATION
Focus on:
- sender / recipient / cc
- subject / request topic
- attachment / file / format
- request / response / forwarding
- access issues
- system or file-transfer constraints
- organization affiliation if explicitly shown
- timestamps
- job titles

2. LEGAL / EXHIBIT DOCUMENTS
Focus on:
- exhibit labels
- Bates numbers
- production notes
- party references
- document identifiers
- references between exhibit and source documents

3. SCIENTIFIC / CLINICAL DOCUMENTS
Focus on:
- condition-symptom
- intervention-condition
- measure-outcome
- eligibility / exclusion
- study-arm / comparator
- site / sponsor / funder
- guideline / recommendation
- timepoint / assessment

4. TECHNICAL DOCUMENTS
Focus on:
- system-component
- tool-uses-protocol
- service-blocks-resource
- file-format / export / compatibility
- dependency / access / configuration relations

==================================================
LEXICAL / DECOMPOSITION RELATIONS
==================================================

If ENTITY_LIST includes compound entities and their decomposed sub-entities, you may extract lexical relations only when they are useful.

Examples:
- traumatic brain injury -> has_component -> brain injury
- traumatic brain injury -> has_component -> brain
- post-traumatic stress disorder -> has_component -> stress disorder
- FTP site -> has_component -> FTP
- Kadian request -> has_component -> Kadian

Use these sparingly and only when the decomposition is meaningful for downstream graph use.

Do NOT explode every phrase into trivial lexical relations.

==================================================
CANONICALIZATION RULE
==================================================

Merge duplicate relations that differ only by:
- capitalization
- punctuation
- repeated mentions

Keep separate relations when they differ by:
- direction
- timepoint
- subtype
- relation label
- target entity

==================================================
OUTPUT FIELDS
==================================================

For each extracted relation, output:

- entity_1
- relation
- entity_2
- relation_category
- confidence
- evidence

==================================================
FINAL SELF-CHECK
==================================================

Before output, verify:

1. Did I adapt to the document genre?
2. Are both sides of every relation in ENTITY_LIST?
3. Is every relation document-grounded?
4. Did I avoid mere co-occurrence?
5. Did I normalize relation labels?
6. Did I keep the correct direction?
7. Did I avoid noisy lexical overgeneration?

If not, revise before outputting.

==================================================
OUTPUT FORMAT
==================================================

Output one line per relation in this exact format:

entity_1 | relation | entity_2 | relation_category | confidence | evidence

Rules:
- one relation per line
- no bullets
- no grouping
- no explanations
- no header row
- no duplicate lines
- use concise normalized relation labels
- output only the final relation list
"""

ENTITY_BACKFILL_SYSTEM = ENTITY_EXTRACTION_SYSTEM + """
==================================================
BACKFILL MODE
==================================================

You are operating in BACKFILL mode. You will additionally receive a MISSING ENTITIES list
containing entity names that a downstream relation extractor referenced but that are not
yet present in the entity inventory.

In this mode:
- Only emit rows for entities in the MISSING ENTITIES list.
- Use the exact Entity string provided in MISSING ENTITIES; do not rename, paraphrase, or merge.
- Only emit a row if the entity is clearly grounded in the DOCUMENT TEXT.
- Silently skip any missing entity that is not grounded in the document.
- All other rules above (typing, confidence, context, exclusions, output format) still apply.
- Output must follow the exact OUTPUT FORMAT defined above:
  Entity | TopCategory > SpecificCategory | Confidence | Context | PageNumber
- If none of the missing entities can be confirmed, return an empty response.
"""

RELATIONSHIP_REPAIR_SYSTEM = """
You are a strict semantic relation extraction formatter.

Rewrite the invalid relation output into clean final rows only.

Return one line per relation in this exact format:
entity_1 | relation | entity_2 | relation_category | confidence | evidence

Rules:
- both entities must appear in ENTITY LIST
- keep only document-grounded relations
- no bullets
- no numbering
- no explanations
- no header row
- no duplicate lines
- if no valid relation can be recovered, return an empty response
"""


def build_wikipedia_enrichment_user_prompt(*, entity_list_text: str) -> str:
    return (
        "==================================================\n"
        "ENTITY LIST\n"
        "==================================================\n\n"
        f"{entity_list_text}"
    )

def build_relationship_user_prompt(*, document_text: str, entity_list_text: str) -> str:
    return (
        "==================================================\n"
        "DOCUMENT TEXT\n"
        "==================================================\n\n"
        f"{document_text}\n\n"
        "==================================================\n"
        "ENTITY LIST\n"
        "==================================================\n\n"
        f"{entity_list_text}"
    )

def build_entity_backfill_user_prompt(
    *,
    document_text: str,
    missing_entities: list[str],
) -> str:
    missing_block = "\n".join(f"- {name}" for name in missing_entities)
    return (
        "==================================================\n"
        "DOCUMENT TEXT\n"
        "==================================================\n\n"
        f"{document_text}\n\n"
        "==================================================\n"
        "MISSING ENTITIES\n"
        "==================================================\n\n"
        f"{missing_block}"
    )

def build_relationship_repair_user_prompt(
    *,
    document_text: str,
    entity_list_text: str,
    invalid_output: str,
) -> str:
    return (
        "==================================================\n"
        "DOCUMENT TEXT\n"
        "==================================================\n\n"
        f"{document_text}\n\n"
        "==================================================\n"
        "ENTITY LIST\n"
        "==================================================\n\n"
        f"{entity_list_text}\n\n"
        "==================================================\n"
        "INVALID OUTPUT TO REPAIR\n"
        "==================================================\n\n"
        f"{invalid_output}"
    )
