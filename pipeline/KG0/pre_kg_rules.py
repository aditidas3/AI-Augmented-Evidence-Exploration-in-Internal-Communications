"""
pre_kg_rules.py
===============
Contains node definitions, node attributes, edge definitions, edge properties.
Rule logic lives in rule_engine.py
"""

import json
from pathlib import Path
from datetime import datetime
import argparse
import rule_engine


# ================================================================
# BLOCK 1 — NODE DEFINITIONS
#
# applies_to  : document type tags (not filenames)
# uid_field   : field used as stable node key
#   _doc_uid  = sha256 of record (set by rule C6)
#   _uid      = sha256 of entity fields (set by rules C1-C5/F8-F9)
#   _computed = sha256 computed at load time by kg_loader.py
#   "name"    = the name field itself is the uid (globally unique strings)
# sources     : list of paths into the JSONL
#   None          = the record itself is the node
#   "path[]"      = iterate as array
#   "path"        = single dict
#   filter_key/val = only items where obj[filter_key]==filter_val
# ================================================================

NODE_SOURCES = {

    # ── Core document ─────────────────────────────────────────────
    "Document": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_doc_uid",
        "sources":    [None],
    },

    # ── People ────────────────────────────────────────────────────
    # PPT: hasContent.slides[].entities.people[]
    "Person": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_uid",
        "sources": [
            ("output.author[]",                                        {}),
            ("output.contacts[]",                                      {"filter_key": "contact_type", "filter_val": "individual"}),
            ("output.hasPart[].sender",                                {}),
            ("output.hasPart[].recipient[]",                           {}),
            ("output.hasPart[].entities.people[]",                     {}),
            ("output.hasContent[].entities.people[]",                  {}),
            ("output.hasContent.slides[].entities.people[]",           {}),
        ],
    },

    # ── Organizations ─────────────────────────────────────────────
    "Organization": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_uid",
        "sources": [
            ("output.contacts[]",                                      {"filter_key": "contact_type", "filter_val": "organization"}),
            ("output.hasContent[].entities.organizations[]",           {}),
            ("output.hasPart[].entities.organizations[]",              {}),
            ("output.hasContent.slides[].entities.organizations[]",    {}),
        ],
    },

    # ── GPE (Geo-Political Entities) ──────────────────────────────
    # Separate from Location (physical places) per schema decision.
    "GPE": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "name",
        "sources": [
            ("output.hasContent[].entities.gpe[]",                    {}),
            ("output.hasPart[].entities.gpe[]",                       {}),
            ("output.hasContent.slides[].entities.gpe[]",             {}),
        ],
    },

    # ── Locations ─────────────────────────────────────────────────
    # Physical non-administrative places.
    "Location": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "name",
        "sources": [
            ("output.hasContent[].entities.locations[]",              {}),
            ("output.hasPart[].entities.locations[]",                 {}),
            ("output.hasContent.slides[].entities.locations[]",       {}),
            ("output.sections.sectionDetails[].items[].locations[]",  {}),
        ],
    },

    # ── Drugs ─────────────────────────────────────────────────────
    "Drug": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_uid",
        "sources": [
            ("output.hasContent[].entities.drugs[]",                  {}),
            ("output.hasPart[].entities.drugs[]",                     {}),
            ("output.hasContent.slides[].entities.drugs[]",           {}),
        ],
    },

    # ── Claims ────────────────────────────────────────────────────
    "Claim": {
        "applies_to": ["DOC"],
        "uid_field":  "_computed",
        "sources":    [("output.claims[]", {})],
    },

    # ── Topics ────────────────────────────────────────────────────
    "Topic": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "topic_string",
        "sources": [
            ("output.hasContent[].entities.topics[]",                 {}),
            ("output.hasPart[].entities.topics[]",                    {}),
            ("output.hasContent.slides[].entities.topics[]",          {}),
        ],
    },

    # ── Abbreviations ─────────────────────────────────────────────
    "Abbreviation": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources":    [("output.abbreviations[]", {})],
    },

    # ── Citations / Bibliography ───────────────────────────────────
    "Citation": {
        "applies_to": ["DOC"],
        "uid_field":  "_computed",
        "sources":    [("output.bibliography[]", {})],
    },

    # ── Legal Framework ───────────────────────────────────────────
    "LegalFramework": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources":    [("output.legalFramework", {})],
    },

    # ── Email Messages ────────────────────────────────────────────
    # identifier field removed from EmailMessage attributes (not in new schema).
    "EmailMessage": {
        "applies_to": ["EMAIL"],
        "uid_field":  "_uid",
        "sources":    [("output.hasPart[]", {})],
    },

    # ── PPT Slides ────────────────────────────────────────────────
    "Slide": {
        "applies_to": ["PPT"],
        "uid_field":  "_computed",
        "sources":    [("output.hasContent.slides[]", {})],
    },

    # ── XLS Sheets ────────────────────────────────────────────────
    "Sheet": {
        "applies_to": ["XLS"],
        "uid_field":  "_computed",
        "sources":    [("output.hasContent[]", {})],
    },

    # ── XLS Table Regions ─────────────────────────────────────────
    "TableRegion": {
        "applies_to": ["XLS"],
        "uid_field":  "_computed",
        "sources":    [("output.hasContent[].tableRegions[]", {})],
    },

    # ── XLS Pivot Tables ──────────────────────────────────────────
    "PivotTable": {
        "applies_to": ["XLS"],
        "uid_field":  "_computed",
        "sources":    [("output.hasContent[].sheetObjects.pivotTables[]", {})],
    },

    # ── XLS Formulas ──────────────────────────────────────────────
    "Formula": {
        "applies_to": ["XLS"],
        "uid_field":  "_computed",
        "sources":    [("output.hasContent[].tableRegions[].formulas[]", {})],
    },

    # ── XLS Assessments ───────────────────────────────────────────
    "Assessment": {
        "applies_to": ["XLS"],
        "uid_field":  "_computed",
        "sources":    [("output.hasContent[].assessments", {})],
    },

    # ── TXT Text Content ──────────────────────────────────────────
    "TextContent": {
        "applies_to": ["TXT"],
        "uid_field":  "_uid",
        "sources":    [("output.hasContent[]", {})],
    },

    # ── TXT Tabular Columns ───────────────────────────────────────
    "TabularColumn": {
        "applies_to": ["TXT"],
        "uid_field":  "_computed",
        "sources":    [("output.hasContent[].tabular.columns[]", {})],
    },

    # ── TXT Cell Index entries ────────────────────────────────────
    "CellIndex": {
        "applies_to": ["TXT"],
        "uid_field":  "_computed",
        "sources":    [("output.hasContent[].tabular.cellIndex[]", {})],
    },

    # ── Products ──────────────────────────────────────────────────
    "Product": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.products[]",               {}),
            ("output.hasPart[].entities.products[]",                  {}),
            ("output.hasContent.slides[].entities.products[]",        {}),
        ],
    },

    # ── Events ────────────────────────────────────────────────────
    "Event": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.events[]",                 {}),
            ("output.hasPart[].entities.events[]",                    {}),
            ("output.hasContent.slides[].entities.events[]",          {}),
        ],
    },

    # ── Finance entries ───────────────────────────────────────────
    "Finance": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.finances[]",               {}),
            ("output.hasPart[].entities.finances[]",                  {}),
            ("output.hasContent.slides[].entities.finances[]",        {}),
        ],
    },

    # ── Metrics ───────────────────────────────────────────────────
    "Metric": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.metrics[]",                {}),
            ("output.hasPart[].entities.metrics[]",                   {}),
            ("output.hasContent.slides[].entities.metrics[]",         {}),
        ],
    },

    # ── Risks ─────────────────────────────────────────────────────
    "Risk": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.risks[]",                  {}),
            ("output.hasPart[].entities.risks[]",                     {}),
            ("output.hasContent.slides[].entities.risks[]",           {}),
        ],
    },

    # ── Requirements ──────────────────────────────────────────────
    "Requirement": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.requirements[]",           {}),
            ("output.hasPart[].entities.requirements[]",              {}),
            ("output.hasContent.slides[].entities.requirements[]",    {}),
        ],
    },

    # ── Decisions ─────────────────────────────────────────────────
    "Decision": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.decisionsMade[]",          {}),
            ("output.hasPart[].entities.decisionsMade[]",             {}),
            ("output.hasContent.slides[].entities.decisionsMade[]",   {}),
        ],
    },

    # ── Date Mentions ─────────────────────────────────────────────
    "DateMention": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.datesMentioned[]",         {}),
            ("output.hasPart[].entities.datesMentioned[]",            {}),
            ("output.hasContent.slides[].entities.datesMentioned[]",  {}),
        ],
    },

    # ── Health Mentions ───────────────────────────────────────────
    "HealthMention": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.health[]",                 {}),
            ("output.hasPart[].entities.health[]",                    {}),
            ("output.hasContent.slides[].entities.health[]",          {}),
        ],
    },

    # ── Signature Blocks ──────────────────────────────────────────
    "SignatureBlock": {
        "applies_to": ["DOC", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.signatureBlocks[]",                              {}),
        ],
    },

    # ── Figures ───────────────────────────────────────────────────
    "Figure": {
        "applies_to": ["DOC", "PPT", "XLS"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].visuals.figures[]",                      {}),
            ("output.hasContent.slides[].visuals.figures[]",               {}),
            ("output.hasContent[].sheetObjects.visuals.figures[]",         {}),
        ],
    },

    # ── Links ─────────────────────────────────────────────────────
    "Link": {
        "applies_to": ["DOC", "PPT", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.links[]",                                        {}),
            ("output.hasContent.slides[].links[]",                    {}),
        ],
    },

    # ── Case Context ──────────────────────────────────────────────
    "CaseContext": {
        "applies_to": ["DOC"],
        "uid_field":  "_computed",
        "sources":    [("output.sections.caseContext", {})],
    },

    # ── Section Details ───────────────────────────────────────────
    "SectionDetail": {
        "applies_to": ["DOC", "TXT"],
        "uid_field":  "_computed",
        "sources":    [("output.sections.sectionDetails[]", {})],
    },

    # ── Identifiers ───────────────────────────────────────────────
    "Identifier": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.identifiers[]",            {}),
            ("output.hasPart[].entities.identifiers[]",               {}),
            ("output.hasContent.slides[].entities.identifiers[]",     {}),
        ],
    },

    # ── Embedded Objects ──────────────────────────────────────────
    "EmbeddedObject": {
        "applies_to": ["DOC", "PPT", "XLS"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].visuals.embeddedObjects[]",               {}),
            ("output.hasContent.slides[].visuals.embeddedObjects[]",        {}),
            ("output.hasContent[].sheetObjects.visuals.embeddedObjects[]",  {}),
        ],
    },

    # ── Procedures (TXT only) ─────────────────────────────────────
    "Procedure": {
        "applies_to": ["TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].structure.procedures[]",            {}),
        ],
    },

    # ── Vocab ─────────────────────────────────────────────────────
    "Vocab": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources":    [("output.vocab", {})],
    },
}


# ================================================================
# BLOCK 2 — NODE ATTRIBUTE MAPPING
# "source_field" : "neo4j_property"
# __self__  = the value itself is the property (for string nodes)
# ================================================================

NODE_ATTRIBUTES = {

    "Document": {
        # shared
        "output.url":                        "url",
        "output.documentDate":               "documentDate",
        "output.type":                       "type",
        "output.industry":                   "industry",
        "output.language":                   "language",
        "output.summary":                    "summary",
        "output.bates_number":               "batesNumber",
        "output.collection":                 "collection",
        "output.source":                     "source",
        "output.tid":                        "tid",
        "output.case":                       "case",
        "output.dateAdded":                  "dateAdded",
        # DOC / TXT
        "output.confidentialityNotice":      "confidentialityNotice",
        "output.copyrightNotice":            "copyrightNotice",
        "output.copyright_notice":           "copyrightNotice",  # DOC snake_case alias
        # EMAIL
        "output.semantic_type":              "semanticType",
        "output.legalStatus":                "legalStatus",
        # PPT
        "output.hasContent.audience":        "audience",
        "output.hasContent.purpose":         "purpose",
        "output.hasContent.keywords":        "keywords",
        "output.hasContent.summary":         "summary",
        "output.hasContent.title":           "contentTitle",
        "output.hasContent.mainEntity":      "mainEntity",
        "output.hasContent.sharedVia.sentBy":"sharedViaSentBy",
        # sourceFile — all types
        "output.sourceFile.fileName":        "sourceFileName",
        "output.sourceFile.fileType":        "sourceFileType",
        "output.sourceFile.hash":            "sourceFileHash",
        "output.sourceFile.pageCount":       "sourceFilePageCount",
        "_doc_uid":                          "uid",
    },

    "Person": {
        "name":            "name",
        "email":           "email",
        "phone":           "phone",
        "role":            "role",
        "address":         "address",
        "organization":    "organization",
        "witness_context": "witnessContext",
        "_uid":            "uid",
    },

    "Organization": {
        "name":            "name",
        "witness_context": "witnessContext",
        "_uid":            "uid",
    },

    "GPE": {
        "name":            "name",
        "witness_context": "witnessContext",
    },

    "Location": {
        "name":            "name",
        "address":         "address",
        "witness_context": "witnessContext",
    },

    "Drug": {
        "name":            "name",
        "description":     "description",
        "witness_context": "witnessContext",
        "_uid":            "uid",
    },

    "Claim": {
        "claim_text":       "text",
        "subject":          "subject",
        "qualifier":        "qualifier",
        "metric":           "metric",
        "value":            "value",
        "unit":             "unit",
        "comparison":       "comparison",
        "context":          "context",
        "source_reference": "sourceReference",
        "range_min":        "rangeMin",
        "range_max":        "rangeMax",
    },

    # Topic: uid is topic_string, mapped to neo4j "name" property
    "Topic": {
        "topic_string":    "name",
        "category":        "category",
        "witness_context": "witnessContext",
    },

    "Abbreviation": {
        "abbv_name":   "abbvName",
        "full_form":   "fullForm",
        "description": "description",
        "context":     "context",
    },

    "Citation": {
        "title":            "title",
        "publisher":        "publisher",
        "publication_date": "publicationDate",
        "accessed_date":    "accessedDate",
        "doi":              "doi",
        "url":              "url",
        "authors":          "authors",
        "volume":           "volume",
        "issue":            "issue",
        "pages":            "pages",
        "isbn":             "isbn",
        "issn":             "issn",
        "container_title":  "containerTitle",
        "citation_text":    "citationText",
        "notes":            "notes",
    },

    "LegalFramework": {
        "type":        "type",
        "description": "description",
    },

    # EmailMessage: removed identifier (not in new schema)
    "EmailMessage": {
        "subject":       "subject",
        "dateSent":      "dateSent",
        "body":          "body",
        "semantic_type": "semanticType",
        "_uid":          "uid",
    },

    # Slide: removed visualEvidence (not in simplified schema)
    "Slide": {
        "pageNumber":     "pageNumber",
        "title":          "title",
        "keyClaim":       "keyClaim",
        "speakerNotes":   "speakerNotes",
        "disclaimerText": "disclaimerText",
    },

    "Sheet": {
        "pageNumber": "pageNumber",
        "title":      "title",
        "mainEntity": "mainEntity",
        "summary":    "summary",
        "notes":      "notes",
    },

    "TableRegion": {
        "regionId":             "regionId",
        "rangeA1":              "rangeA1",
        "tableType":            "tableType",
        "layout.hasMergedCell": "hasMergedCell",
        "layout.headerRows":    "headerRows",
        "layout.indexColumn":   "indexColumn",
        "units.currency":       "currency",
        "units.scale":          "scale",
        "units.basis":          "basis",
        "units.unitText":       "unitText",
        "rows.rowNotes":        "rowNotes",
    },

    "PivotTable": {
        "name":          "name",
        "rangeA1":       "rangeA1",
        "sourceRangeA1": "sourceRangeA1",
        "notes":         "notes",
    },

    "Formula": {
        "cell":            "cell",
        "formula":         "formula",
        "calculatedValue": "calculatedValue",
        "isExternal":      "isExternal",
        "externalTarget":  "externalTarget",
    },

    "Assessment": {
        "assessmentType":  "assessmentType",
        "riskType":        "riskType",
        "riskDescription": "riskDescription",
        "riskDataSource":  "riskDataSource",
    },

    "TextContent": {
        "title":                          "title",
        "keyClaim":                       "keyClaim",
        "configuration":                  "configuration",
        "regulatorySubmission":           "regulatorySubmission",
        "report":                         "report",
        "referencedBy":                   "referencedBy",
        "tabular.tableType":              "tableType",
        "tabular.dimensions.rowCount":    "rowCount",
        "tabular.dimensions.columnCount": "columnCount",
        "tabular.dialect.delimiter":      "csvDelimiter",
        "tabular.dialect.encoding":       "csvEncoding",
        "tabular.dialect.hasHeaderRow":   "hasHeaderRow",
        "structure.sourceRefFormat":      "sourceRefFormat",
        "_has_redactions":                "hasRedactions",
        "_uid":                           "uid",
    },

    "TabularColumn": {
        "name":         "name",
        "index":        "index",
        "inferredType": "inferredType",
        "nullable":     "nullable",
        "description":  "description",
        "units":        "units",
    },

    "CellIndex": {
        "columnName":      "columnName",
        "rowNumber":       "rowNumber",
        "value":           "value",
        "normalizedValue": "normalizedValue",
        "valueType":       "valueType",
        "isRedacted":      "isRedacted",
        "redactionText":   "redactionText",
    },

    "Product": {
        "name":            "name",
        "model":           "model",
        "identifier":      "identifier",
        "witness_context": "witnessContext",
    },

    # Semantic entity nodes: *_string -> "text", category where applicable
    "Event": {
        "event_string":    "text",
        "witness_context": "witnessContext",
    },

    "Finance": {
        "finance_string":  "text",
        "witness_context": "witnessContext",
    },

    "Metric": {
        "metric_string":   "text",
        "witness_context": "witnessContext",
    },

    "Risk": {
        "risk_string":     "text",
        "witness_context": "witnessContext",
    },

    "Requirement": {
        "requirement_string": "text",
        "witness_context":    "witnessContext",
    },

    "Decision": {
        "decision_string": "text",
        "category":        "category",
        "witness_context": "witnessContext",
    },

    "DateMention": {
        "date":          "date",
        "contextOfDate": "contextOfDate",
    },

    "HealthMention": {
        "health_string":   "text",
        "witness_context": "witnessContext",
    },

    "SignatureBlock": {
        "signerName":    "signerName",
        "signerTitle":   "signerTitle",
        "organization":  "organization",
        "date":          "date",
        "location":      "location",
        "signatureText": "signatureText",
    },

    # Figure: simplified — removed label, source
    "Figure": {
        "id":      "figureId",
        "title":   "title",
        "caption": "caption",
        "context": "context",
        "notes":   "notes",
    },

    "Link": {
        "url":         "url",
        "displayText": "displayText",
        "type":        "linkType",
    },

    "CaseContext": {
        "case_number":                  "caseNumber",
        "filingDate":                   "filingDate",
        "jurisdiction":                 "jurisdiction",
        "presentedBy":                  "presentedBy",
        "declarationSignedByAuthority": "declarationSignedByAuthority",
        "declarationSignedDate":        "declarationSignedDate",
        "declarationSignedLoction":     "declarationSignedLocation",
    },

    "SectionDetail": {
        "title":        "title",
        "section_type": "sectionType",
    },

    "Identifier": {
        "type":  "identifierType",
        "value": "value",
    },

    # EmbeddedObject: simplified fields only
    "EmbeddedObject": {
        "objectType": "objectType",
        "fileName":   "fileName",
        "notes":      "notes",
    },

    "Procedure": {
        "title":      "title",
        "pageNumber": "pageNumber",
    },

    "Vocab": {
        "name":              "name",
        "type":              "type",
        "rxcui":             "rxcui",
        "canonicalName":     "canonicalName",
        "drugClass":         "drugClass",
        "synonyms":          "synonyms",
        "applicationNumber": "applicationNumber",
        "approvalDate":      "approvalDate",
        "manufacturer":      "manufacturer",
        "raw_json":          "raw_json",
        "sourceUrl":         "sourceUrl",
        "fetchedAt":         "fetchedAt",
    },
}


# ================================================================
# BLOCK 3 — EDGE DEFINITIONS
# ================================================================

EDGE_DEFINITIONS = {

    # ── Document → Author ─────────────────────────────────────────
    "AUTHORED_BY": {
        "from":        "Document",
        "to":          "Person",
        "source_path": "output.author[]",
        "via_uid":     "_uid",
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Contact ────────────────────────────────────────
    "HAS_CONTACT": {
        "from":        "Document",
        "to":          ["Person", "Organization"],
        "source_path": "output.contacts[]",
        "via_uid":     "_uid",
        "applies_to":  ["DOC", "TXT"],
    },
    "WORKS_FOR": {
        "from":        "Person",
        "to":          "Organization",
        "source_path": "output.contacts[]",
        "condition":   "has_organization_field",
        "applies_to":  ["DOC", "TXT"],
    },

    # ── Document → Drug ───────────────────────────────────────────
    "MENTIONS_DRUG": {
        "from":        "Document",
        "to":          "Drug",
        "source_path": [
            "output.hasContent[].entities.drugs[]",
            "output.hasPart[].entities.drugs[]",
            "output.hasContent.slides[].entities.drugs[]",
        ],
        "via_uid":    "_uid",
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Claim ──────────────────────────────────────────
    "HAS_CLAIM": {
        "from":        "Document",
        "to":          "Claim",
        "source_path": "output.claims[]",
        "applies_to":  ["DOC"],
    },

    # ── Document → Topic ──────────────────────────────────────────
    "COVERS_TOPIC": {
        "from":        "Document",
        "to":          "Topic",
        "source_path": [
            "output.hasContent[].entities.topics[]",
            "output.hasPart[].entities.topics[]",
            "output.hasContent.slides[].entities.topics[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → GPE ────────────────────────────────────────────
    "MENTIONS_GPE": {
        "from":        "Document",
        "to":          "GPE",
        "source_path": [
            "output.hasContent[].entities.gpe[]",
            "output.hasPart[].entities.gpe[]",
            "output.hasContent.slides[].entities.gpe[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Location ───────────────────────────────────────
    "LOCATED_IN": {
        "from":        "Document",
        "to":          "Location",
        "source_path": [
            "output.hasContent[].entities.locations[]",
            "output.hasPart[].entities.locations[]",
            "output.hasContent.slides[].entities.locations[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Abbreviation ───────────────────────────────────
    "HAS_ABBREVIATION": {
        "from":        "Document",
        "to":          "Abbreviation",
        "source_path": "output.abbreviations[]",
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Citation ───────────────────────────────────────
    "CITES": {
        "from":        "Document",
        "to":          "Citation",
        "source_path": "output.bibliography[]",
        "applies_to":  ["DOC"],
    },

    # ── Document → LegalFramework ─────────────────────────────────
    "HAS_LEGAL_FRAMEWORK": {
        "from":        "Document",
        "to":          "LegalFramework",
        "source_path": "output.legalFramework",
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → EmailMessage ───────────────────────────────────
    "HAS_MESSAGE": {
        "from":        "Document",
        "to":          "EmailMessage",
        "source_path": "output.hasPart[]",
        "applies_to":  ["EMAIL"],
    },

    # ── EmailMessage → Person ─────────────────────────────────────
    "SENT_BY": {
        "from":        "EmailMessage",
        "to":          "Person",
        "source_path": "output.hasPart[].sender",
        "via_uid":     "_uid",
        "applies_to":  ["EMAIL"],
    },
    "SENT_TO": {
        "from":        "EmailMessage",
        "to":          "Person",
        "source_path": "output.hasPart[].recipient[]",
        "via_uid":     "_uid",
        "applies_to":  ["EMAIL"],
    },
    "MENTIONS_PERSON_IN_MSG": {
        "from":        "EmailMessage",
        "to":          "Person",
        "source_path": "output.hasPart[].entities.people[]",
        "via_uid":     "_uid",
        "applies_to":  ["EMAIL"],
    },

    # ── Document → Slide ──────────────────────────────────────────
    "HAS_SLIDE": {
        "from":        "Document",
        "to":          "Slide",
        "source_path": "output.hasContent.slides[]",
        "applies_to":  ["PPT"],
    },

    # ── Slide → Person ────────────────────────────────────────────
    "MENTIONS_PERSON_ON_SLIDE": {
        "from":        "Slide",
        "to":          "Person",
        "source_path": "output.hasContent.slides[].entities.people[]",
        "via_uid":     "_uid",
        "applies_to":  ["PPT"],
    },

    # ── Slide → Drug ──────────────────────────────────────────────
    "MENTIONS_DRUG_ON_SLIDE": {
        "from":        "Slide",
        "to":          "Drug",
        "source_path": "output.hasContent.slides[].entities.drugs[]",
        "via_uid":     "_uid",
        "applies_to":  ["PPT"],
    },

    # ── Document → Sheet ──────────────────────────────────────────
    "HAS_SHEET": {
        "from":        "Document",
        "to":          "Sheet",
        "source_path": "output.hasContent[]",
        "applies_to":  ["XLS"],
    },

    # ── Sheet → TableRegion ───────────────────────────────────────
    "HAS_TABLE_REGION": {
        "from":        "Sheet",
        "to":          "TableRegion",
        "source_path": "output.hasContent[].tableRegions[]",
        "applies_to":  ["XLS"],
    },

    # ── Sheet → PivotTable ────────────────────────────────────────
    "HAS_PIVOT_TABLE": {
        "from":        "Sheet",
        "to":          "PivotTable",
        "source_path": "output.hasContent[].sheetObjects.pivotTables[]",
        "applies_to":  ["XLS"],
    },

    # ── TableRegion → Formula ─────────────────────────────────────
    "HAS_FORMULA": {
        "from":        "TableRegion",
        "to":          "Formula",
        "source_path": "output.hasContent[].tableRegions[].formulas[]",
        "applies_to":  ["XLS"],
    },

    # ── Sheet → Assessment ────────────────────────────────────────
    "HAS_ASSESSMENT": {
        "from":        "Sheet",
        "to":          "Assessment",
        "source_path": "output.hasContent[].assessments",
        "applies_to":  ["XLS"],
    },

    # ── Document → Person (content mentions) ─────────────────────
    "MENTIONS_PERSON": {
        "from":        "Document",
        "to":          "Person",
        "source_path": [
            "output.hasContent[].entities.people[]",
        ],
        "via_uid":    "_uid",
        "applies_to": ["DOC", "PPT", "XLS", "TXT"],
    },

    # ── Document → Organization ───────────────────────────────────
    "MENTIONS_ORG": {
        "from":        "Document",
        "to":          "Organization",
        "source_path": [
            "output.hasContent[].entities.organizations[]",
            "output.hasPart[].entities.organizations[]",
            "output.hasContent.slides[].entities.organizations[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Product ────────────────────────────────────────
    "MENTIONS_PRODUCT": {
        "from":        "Document",
        "to":          "Product",
        "source_path": [
            "output.hasContent[].entities.products[]",
            "output.hasPart[].entities.products[]",
            "output.hasContent.slides[].entities.products[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Event ──────────────────────────────────────────
    "HAS_EVENT": {
        "from":        "Document",
        "to":          "Event",
        "source_path": [
            "output.hasContent[].entities.events[]",
            "output.hasPart[].entities.events[]",
            "output.hasContent.slides[].entities.events[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Finance ────────────────────────────────────────
    "HAS_FINANCE": {
        "from":        "Document",
        "to":          "Finance",
        "source_path": [
            "output.hasContent[].entities.finances[]",
            "output.hasPart[].entities.finances[]",
            "output.hasContent.slides[].entities.finances[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Metric ─────────────────────────────────────────
    "HAS_METRIC": {
        "from":        "Document",
        "to":          "Metric",
        "source_path": [
            "output.hasContent[].entities.metrics[]",
            "output.hasPart[].entities.metrics[]",
            "output.hasContent.slides[].entities.metrics[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Risk ───────────────────────────────────────────
    "HAS_RISK": {
        "from":        "Document",
        "to":          "Risk",
        "source_path": [
            "output.hasContent[].entities.risks[]",
            "output.hasPart[].entities.risks[]",
            "output.hasContent.slides[].entities.risks[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Requirement ────────────────────────────────────
    "HAS_REQUIREMENT": {
        "from":        "Document",
        "to":          "Requirement",
        "source_path": [
            "output.hasContent[].entities.requirements[]",
            "output.hasPart[].entities.requirements[]",
            "output.hasContent.slides[].entities.requirements[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Decision ───────────────────────────────────────
    "HAS_DECISION": {
        "from":        "Document",
        "to":          "Decision",
        "source_path": [
            "output.hasContent[].entities.decisionsMade[]",
            "output.hasPart[].entities.decisionsMade[]",
            "output.hasContent.slides[].entities.decisionsMade[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → DateMention ────────────────────────────────────
    "MENTIONS_DATE": {
        "from":        "Document",
        "to":          "DateMention",
        "source_path": [
            "output.hasContent[].entities.datesMentioned[]",
            "output.hasPart[].entities.datesMentioned[]",
            "output.hasContent.slides[].entities.datesMentioned[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → HealthMention ──────────────────────────────────
    "MENTIONS_HEALTH": {
        "from":        "Document",
        "to":          "HealthMention",
        "source_path": [
            "output.hasContent[].entities.health[]",
            "output.hasPart[].entities.health[]",
            "output.hasContent.slides[].entities.health[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → SignatureBlock ─────────────────────────────────
    "HAS_SIGNATURE": {
        "from":        "Document",
        "to":          "SignatureBlock",
        "source_path": "output.signatureBlocks[]",
        "applies_to":  ["DOC", "PPT", "XLS", "TXT"],
    },

    # ── Document → Figure ─────────────────────────────────────────
    "HAS_FIGURE": {
        "from":        "Document",
        "to":          "Figure",
        "source_path": [
            "output.hasContent[].visuals.figures[]",
            "output.hasContent.slides[].visuals.figures[]",
            "output.hasContent[].sheetObjects.visuals.figures[]",
        ],
        "applies_to":  ["DOC", "PPT", "XLS"],
    },

    # ── Document → Link ───────────────────────────────────────────
    "HAS_LINK": {
        "from":        "Document",
        "to":          "Link",
        "source_path": [
            "output.links[]",
            "output.hasContent.slides[].links[]",
        ],
        "applies_to":  ["DOC", "PPT", "TXT"],
    },

    # ── Document → CaseContext ────────────────────────────────────
    "HAS_CASE_CONTEXT": {
        "from":        "Document",
        "to":          "CaseContext",
        "source_path": "output.sections.caseContext",
        "applies_to":  ["DOC"],
    },

    # ── Document → SectionDetail ──────────────────────────────────
    "HAS_SECTION": {
        "from":        "Document",
        "to":          "SectionDetail",
        "source_path": "output.sections.sectionDetails[]",
        "applies_to":  ["DOC", "TXT"],
    },

    # ── Document → TextContent ────────────────────────────────────
    "HAS_TEXT_CONTENT": {
        "from":        "Document",
        "to":          "TextContent",
        "source_path": "output.hasContent[]",
        "via_uid":     "_uid",
        "applies_to":  ["TXT"],
    },

    # ── TextContent → TabularColumn ───────────────────────────────
    "HAS_COLUMN": {
        "from":        "TextContent",
        "to":          "TabularColumn",
        "source_path": "output.hasContent[].tabular.columns[]",
        "applies_to":  ["TXT"],
    },

    # ── TextContent → CellIndex ───────────────────────────────────
    "HAS_CELL": {
        "from":        "TextContent",
        "to":          "CellIndex",
        "source_path": "output.hasContent[].tabular.cellIndex[]",
        "applies_to":  ["TXT"],
    },

    # ── TextContent → Location / Organization / Product ───────────
    "MENTIONS_LOCATION_IN_TEXT": {
        "from":        "TextContent",
        "to":          "Location",
        "source_path": "output.hasContent[].entities.locations[]",
        "applies_to":  ["TXT"],
    },
    "MENTIONS_ORG_IN_TEXT": {
        "from":        "TextContent",
        "to":          "Organization",
        "source_path": "output.hasContent[].entities.organizations[]",
        "applies_to":  ["TXT"],
    },
    "MENTIONS_PRODUCT_IN_TEXT": {
        "from":        "TextContent",
        "to":          "Product",
        "source_path": "output.hasContent[].entities.products[]",
        "applies_to":  ["TXT"],
    },

    # ── Document → Identifier ─────────────────────────────────────
    "HAS_IDENTIFIER": {
        "from":        "Document",
        "to":          "Identifier",
        "source_path": [
            "output.hasContent[].entities.identifiers[]",
            "output.hasPart[].entities.identifiers[]",
            "output.hasContent.slides[].entities.identifiers[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → EmbeddedObject ─────────────────────────────────
    "HAS_EMBEDDED_OBJECT": {
        "from":        "Document",
        "to":          "EmbeddedObject",
        "source_path": [
            "output.hasContent[].visuals.embeddedObjects[]",
            "output.hasContent.slides[].visuals.embeddedObjects[]",
            "output.hasContent[].sheetObjects.visuals.embeddedObjects[]",
        ],
        "applies_to":  ["DOC", "PPT", "XLS"],
    },

    # ── Document → Procedure ─────────────────────────────────────
    "HAS_PROCEDURE": {
        "from":        "Document",
        "to":          "Procedure",
        "source_path": "output.hasContent[].structure.procedures[]",
        "applies_to":  ["TXT"],
    },

    # ── Document → Vocab ──────────────────────────────────────────
    "HAS_VOCAB": {
        "from":        "Document",
        "to":          "Vocab",
        "source_path": "output.vocab",
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },
}


# ================================================================
# BLOCK 4 — EDGE ATTRIBUTE MAPPING
# ================================================================

EDGE_ATTRIBUTES = {
    "AUTHORED_BY":               {"pageNumber": "pageNumber"},
    "HAS_CONTACT":               {"pageNumber": "pageNumber"},
    "WORKS_FOR":                 {},
    "MENTIONS_DRUG":             {"pageNumber": "pageNumber"},
    "MENTIONS_DRUG_ON_SLIDE":    {"pageNumber": "pageNumber"},
    "HAS_CLAIM":                 {"pageNumber": "pageNumber"},
    "COVERS_TOPIC":              {"witness_context": "witnessContext"},
    "MENTIONS_GPE":              {"pageNumber": "pageNumber", "witness_context": "witnessContext"},
    "LOCATED_IN":                {"pageNumber": "pageNumber", "witness_context": "witnessContext"},
    "HAS_ABBREVIATION":          {"pageNumber": "pageNumber"},
    "CITES":                     {"pageNumber": "pageNumber"},
    "HAS_LEGAL_FRAMEWORK":       {},
    "HAS_MESSAGE":               {},
    "SENT_BY":                   {},
    "SENT_TO":                   {},
    "MENTIONS_PERSON_IN_MSG":    {"witness_context": "witnessContext"},
    "MENTIONS_PERSON_ON_SLIDE":  {"pageNumber": "pageNumber"},
    "HAS_SLIDE":                 {"pageNumber": "order"},
    "HAS_SHEET":                 {"pageNumber": "order"},
    "HAS_TABLE_REGION":          {"regionId": "regionId"},
    "HAS_PIVOT_TABLE":           {},
    "HAS_FORMULA":               {"cell": "cell"},
    "HAS_ASSESSMENT":            {},
    "MENTIONS_PERSON":           {"pageNumber": "pageNumber"},
    "MENTIONS_ORG":              {"witness_context": "witnessContext"},
    "MENTIONS_PRODUCT":          {"pageNumber": "pageNumber"},
    "HAS_EVENT":                 {"witness_context": "witnessContext"},
    "HAS_FINANCE":               {"witness_context": "witnessContext"},
    "HAS_METRIC":                {"witness_context": "witnessContext"},
    "HAS_RISK":                  {"witness_context": "witnessContext"},
    "HAS_REQUIREMENT":           {"witness_context": "witnessContext"},
    "HAS_DECISION":              {"witness_context": "witnessContext"},
    "MENTIONS_DATE":             {"date": "date", "contextOfDate": "contextOfDate"},
    "MENTIONS_HEALTH":           {"witness_context": "witnessContext"},
    "HAS_SIGNATURE":             {"pageNumber": "pageNumber"},
    "HAS_FIGURE":                {"pageNumber": "pageNumber"},
    "HAS_LINK":                  {"pageNumber": "pageNumber"},
    "HAS_CASE_CONTEXT":          {},
    "HAS_SECTION":               {"section_type": "sectionType"},
    "HAS_TEXT_CONTENT":          {},
    "HAS_COLUMN":                {"index": "colIndex"},
    "HAS_CELL":                  {"rowNumber": "rowNumber", "columnName": "columnName"},
    "MENTIONS_LOCATION_IN_TEXT": {},
    "MENTIONS_ORG_IN_TEXT":      {},
    "MENTIONS_PRODUCT_IN_TEXT":  {"pageNumber": "pageNumber"},
    "HAS_IDENTIFIER":            {"pageNumber": "pageNumber"},
    "HAS_EMBEDDED_OBJECT":       {"objectType": "objectType"},
    "HAS_PROCEDURE":             {"pageNumber": "pageNumber"},
    "HAS_VOCAB":                 {},
}


# ================================================================
# Pipeline — delegates rule logic to rule_engine.py
# ================================================================

def load_input(path_str):
    """
    Accept three input formats:
      - .jsonl file  (one record per line)
      - single .json file
      - folder path  (all *.json files loaded as individual records)
    """
    p = Path(path_str)

    if p.is_dir():
        json_files = sorted(p.glob("*.json"))
        if not json_files:
            print(f"  [WARN] No .json files found in folder: {p}")
            return []
        records = []
        for jf in json_files:
            with open(jf, encoding="utf-8") as f:
                records.append(json.load(f))
        print(f"  [INFO] Loaded {len(records)} .json files from folder: {p}")
        return records

    if p.suffix.lower() == ".json":
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            print(f"  [INFO] Loaded {len(data)} records from JSON array: {p}")
            return data
        print(f"  [INFO] Loaded 1 record from single .json file: {p}")
        return [data]

    with open(p, encoding="utf-8") as f:
        content = f.read().strip()
    if content.startswith("["):
        data = json.loads(content)
        return data if isinstance(data, list) else [data]
    return [json.loads(line) for line in content.splitlines() if line.strip()]


def write_jsonl(records, path):
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Pre-KG rules pipeline. Each --doc/--email/etc. argument accepts:\n"
            "  • a .jsonl file  (one record per line)\n"
            "  • a single .json file\n"
            "  • a folder path  (all *.json files inside)\n"
            "Output is always a clean .jsonl file per document type."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--doc",     default=None)
    parser.add_argument("--email",   default=None)
    parser.add_argument("--ppt",     default=None)
    parser.add_argument("--xls",     default=None)
    parser.add_argument("--txt",     default=None)
    parser.add_argument("--out-dir", default="./clean")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pipeline = [
        (args.doc,   "DOC",   out_dir / "DOC_clean.jsonl"),
        (args.email, "EMAIL", out_dir / "EMAIL_clean.jsonl"),
        (args.ppt,   "PPT",   out_dir / "PPT_clean.jsonl"),
        (args.xls,   "XLS",   out_dir / "XLS_clean.jsonl"),
        (args.txt,   "TXT",   out_dir / "TXT_clean.jsonl"),
    ]

    for src, label, dst in pipeline:
        if src is None:
            print(f"\n[{label}] Skipped — no input provided")
            continue
        if not Path(src).exists():
            print(f"\n[{label}] Skipped — path not found: {src}")
            continue
        print(f"\n[{label}] Reading {src} ...")
        records = load_input(src)
        cleaned = [rule_engine.apply_all_rules(rec, i, label) for i, rec in enumerate(records)]
        write_jsonl(cleaned, dst)
        print(f"[{label}] Wrote {len(cleaned)} records -> {dst}")

    report = {
        "run_at":          datetime.utcnow().isoformat() + "Z",
        "rule_engine":     "rule_engine.py (generated from pre_kg_config.yaml)",
        "stats":           dict(rule_engine.STATS),
        "violation_count": len(rule_engine.VIOLATIONS),
        "warning_count":   len(rule_engine.WARNINGS),
        "violations":      rule_engine.VIOLATIONS,
        "warnings":        rule_engine.WARNINGS,
    }
    report_path = out_dir / "pre_kg_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n{'='*52}")
    print(f"  Violations : {len(rule_engine.VIOLATIONS)}")
    print(f"  Warnings   : {len(rule_engine.WARNINGS)}")
    print(f"  Report     : {report_path}")
    print(f"{'='*52}")
    if rule_engine.VIOLATIONS:
        print("\n  VIOLATIONS (fix before loading):")
        for v in rule_engine.VIOLATIONS:
            print(f"    [{v['rule']}] {v['record']}: {v['message']}")


if __name__ == "__main__":
    main()
