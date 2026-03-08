"""
pre_kg_rules.py
===============
Contains node definitions, node attributes, edge definitions, edge properties.
Rule logic lives in rule_engine.py

Usage:
    python pre_kg_rules.py \
        --doc DOC.jsonl --email EMAIL.jsonl --ppt PPT.jsonl \
        --xls XLS.jsonl --txt TXT.jsonl \
        --out-dir ./clean
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
#   _uid      = sha256 of entity fields (set by rule C1-C5/F8-F9)
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
    # Sources: DOC contacts (individual), EMAIL sender/recipient/semanticMentions.people,
    #          DOC/PPT hasContent entities.people, XLS sharedEntities.people
    "Person": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS"],
        "uid_field":  "_uid",
        "sources": [
            ("output.contacts[]",                                      {"filter_key": "contact_type", "filter_val": "individual"}),
            ("output.hasPart[].sender",                                {}),
            ("output.hasPart[].recipient[]",                           {}),
            ("output.hasPart[].semanticMentions.people[]",             {}),
            ("output.hasPart[].mentions[]",                            {"filter_key": "semantic_type", "filter_val": "Person"}),
            ("output.hasContent[].entities.people[]",                  {}),
            ("output.hasContent.slides[].semanticMentions[].people[]", {}),
            ("output.sharedEntities.people[]",                         {}),
        ],
    },

    # ── Organizations ─────────────────────────────────────────────
    # Sources: DOC contacts (org), all hasContent entities.organizations,
    #          EMAIL/PPT/XLS semanticMentions.organizations, XLS sharedEntities
    "Organization": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_uid",
        "sources": [
            ("output.contacts[]",                                         {"filter_key": "contact_type", "filter_val": "organization"}),
            ("output.hasContent[].entities.organizations[]",              {}),
            ("output.hasPart[].semanticMentions.organizations[]",         {}),
            ("output.hasContent.slides[].semanticMentions[].organization[]", {}),
            ("output.sharedEntities.organization[]",                      {}),
        ],
    },

    # ── Drugs ─────────────────────────────────────────────────────
    # Sources: EMAIL top-level drugs[], DOC hasContent entities.drugs,
    #          PPT/XLS hasContent semanticMentions.drugs,
    #          PPT slides semanticMentions drugs
    "Drug": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS"],
        "uid_field":  "_uid",
        "sources": [
            ("output.drugs[]",                                            {}),
            ("output.hasContent[].entities.drugs[]",                      {}),
            ("output.hasContent[].semanticMentions.drugs[]",              {}),
            ("output.hasContent.slides[].semanticMentions[].drugs[]",     {}),
        ],
    },

    # ── Claims ────────────────────────────────────────────────────
    "Claim": {
        "applies_to": ["DOC"],
        "uid_field":  "_computed",
        "sources":    [("output.claims[]", {})],
    },

    # ── Topics ────────────────────────────────────────────────────
    # PPT hasContent.keywords is also topic-like -> treated as Topics
    "Topic": {
        "applies_to": ["DOC", "PPT"],
        "uid_field":  "name",
        "sources":    [("output.hasContent[].entities.topics[]", {})],
    },

    # ── Locations ─────────────────────────────────────────────────
    # Sources: top-level country, hasContent entities.locations,
    #          EMAIL semanticMentions.locations, PPT slide semanticMentions.locations,
    #          XLS sharedEntities.locations, DOC sectionDetails items locations
    "Location": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "name",
        "sources": [
            ("output.country",                                             {}),
            ("output.hasContent[].entities.locations[]",                   {}),
            ("output.hasPart[].semanticMentions.locations[]",              {}),
            ("output.hasContent.slides[].semanticMentions[].locations[]",  {}),
            ("output.sharedEntities.locations[]",                          {}),
            ("output.sections.sectionDetails[].items[].locations[]",       {}),
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
        "sources":    [("output.hasContent[].structure.tabular.columns[]", {})],
    },

    # ── TXT Pages ─────────────────────────────────────────────────
    "Page": {
        "applies_to": ["TXT"],
        "uid_field":  "_computed",
        "sources":    [("output.hasContent[].structure.pages[]", {})],
    },

    # ── TXT Cell Index entries ────────────────────────────────────
    "CellIndex": {
        "applies_to": ["TXT"],
        "uid_field":  "_computed",
        "sources":    [("output.hasContent[].structure.tabular.cellIndex[]", {})],
    },

    # ── Products ──────────────────────────────────────────────────
    # Sources: DOC/TXT hasContent entities.products,
    #          EMAIL hasPart semanticMentions.products,
    #          XLS hasContent semanticMentions.products,
    #          PPT slides semanticMentions.products
    "Product": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.products[]",                    {}),
            ("output.hasPart[].semanticMentions.products[]",               {}),
            ("output.hasContent[].semanticMentions.products[]",            {}),
            ("output.hasContent.slides[].semanticMentions[].products[]",   {}),
        ],
    },

    # ── Events ────────────────────────────────────────────────────
    "Event": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].semanticMentions.events[]",              {}),
            ("output.hasPart[].semanticMentions.events[]",                 {}),
            ("output.hasContent.slides[].semanticMentions[].events[]",     {}),
        ],
    },

    # ── Finance entries ───────────────────────────────────────────
    "Finance": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].semanticMentions.finances[]",            {}),
            ("output.hasPart[].semanticMentions.finances[]",               {}),
            ("output.hasContent.slides[].semanticMentions[].finances[]",   {}),
        ],
    },

    # ── Metrics ───────────────────────────────────────────────────
    "Metric": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].semanticMentions.metrics[]",             {}),
            ("output.hasPart[].semanticMentions.metrics[]",                {}),
            ("output.hasContent.slides[].semanticMentions[].metrics[]",    {}),
        ],
    },

    # ── Risks ─────────────────────────────────────────────────────
    "Risk": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].semanticMentions.risks[]",               {}),
            ("output.hasPart[].semanticMentions.risks[]",                  {}),
            ("output.hasContent.slides[].semanticMentions[].risks[]",      {}),
        ],
    },

    # ── Requirements ──────────────────────────────────────────────
    "Requirement": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].semanticMentions.requirements[]",        {}),
            ("output.hasPart[].semanticMentions.requirements[]",           {}),
            ("output.hasContent.slides[].semanticMentions[].requirements[]", {}),
        ],
    },

    # ── Decisions ─────────────────────────────────────────────────
    "Decision": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].semanticMentions.decisionsMade[]",       {}),
            ("output.hasPart[].semanticMentions.decisionsMade[]",          {}),
            ("output.hasContent.slides[].semanticMentions[].decisionsMade[]", {}),
        ],
    },

    # ── Date Mentions ─────────────────────────────────────────────
    "DateMention": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.datesMentioned[]",              {}),
            ("output.hasContent[].semanticMentions.datesMentioned[]",      {}),
            ("output.hasPart[].semanticMentions.datesMentioned[]",         {}),
            ("output.hasContent.slides[].semanticMentions[].datesMentioned[]", {}),
        ],
    },

    # ── Health Mentions ───────────────────────────────────────────
    "HealthMention": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].semanticMentions.health[]",              {}),
            ("output.hasPart[].semanticMentions.health[]",                 {}),
            ("output.hasContent.slides[].semanticMentions[].health[]",     {}),
        ],
    },

    # ── Signature Blocks ──────────────────────────────────────────
    # DOC: top-level signatureBlocks[]; TXT: hasContent[].signatureBlock
    "SignatureBlock": {
        "applies_to": ["DOC", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.signatureBlocks[]",                 {}),
            ("output.hasContent[].signatureBlock",       {}),
        ],
    },

    # ── DOC: Figures (visuals) ────────────────────────────────────
    "Figure": {
        "applies_to": ["DOC"],
        "uid_field":  "_computed",
        "sources":    [("output.hasContent[].visuals.figures[]", {})],
    },

    # ── DOC/PPT/XLS: Links ────────────────────────────────────────
    "Link": {
        "applies_to": ["DOC", "PPT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.links[]",                              {}),
            ("output.hasContent.slides[].links[]",          {}),
        ],
    },

    # ── DOC: Case / Section Context ───────────────────────────────
    "CaseContext": {
        "applies_to": ["DOC"],
        "uid_field":  "_computed",
        "sources":    [("output.sections.caseContext", {})],
    },

    # ── DOC: Section Details ──────────────────────────────────────
    "SectionDetail": {
        "applies_to": ["DOC"],
        "uid_field":  "_computed",
        "sources":    [("output.sections.sectionDetails[]", {})],
    },

    # ── PPT: Visual Content on slides ─────────────────────────────
    "VisualContent": {
        "applies_to": ["PPT"],
        "uid_field":  "_computed",
        "sources":    [("output.hasContent.slides[].visualContent[]", {})],
    },

    # ── Identifiers ───────────────────────────────────────────────
    # TXT hasContent entities.identifiers, XLS sharedEntities.identifiers
    "Identifier": {
        "applies_to": ["TXT", "XLS"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].entities.identifiers[]",  {}),
            ("output.sharedEntities.identifiers[]",          {}),
        ],
    },

    # ── Embedded Objects ──────────────────────────────────────────
    # PPT: hasContent.slides[].embeddedObjects[]
    # XLS: hasContent[].sheetObjects.embeddedImages,
    #      hasContent[].sheetObjects.cellComments,
    #      hasContent[].sheetObjects.dataValidations,
    #      hasContent[].sheetObjects.charts
    # DOC: hasContent[].visuals.charts, hasContent[].visuals.tables
    "EmbeddedObject": {
        "applies_to": ["DOC", "PPT", "XLS"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent.slides[].embeddedObjects[]",         {}),
            ("output.hasContent[].sheetObjects.embeddedImages[]",    {}),
            ("output.hasContent[].sheetObjects.cellComments[]",      {}),
            ("output.hasContent[].sheetObjects.dataValidations[]",   {}),
            ("output.hasContent[].sheetObjects.charts[]",            {}),
            ("output.hasContent[].visuals.charts[]",                 {}),
            ("output.hasContent[].visuals.tables[]",                 {}),
        ],
    },

    # ── Procedures ────────────────────────────────────────────────
    # TXT: hasContent[].structure.procedures[]
    # PPT: hasContent.slides[].procedures[] (if populated)
    "Procedure": {
        "applies_to": ["TXT", "PPT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.hasContent[].structure.procedures[]",           {}),
            ("output.hasContent.slides[].procedures[]",              {}),
        ],
    },

    # ── Vocab ─────────────────────────────────────────────────────
    # All 5 file types carry output.vocab (a @context URL string today).
    # Modelled as a dedicated node so external vocabulary enrichment
    # (RxNorm, FDA Orange Book, and any future sources) can be attached
    # as properties on this node via external_libs.py after load.
    # At load time only core fields (name, type, contextUrl) are filled;
    # all API-sourced fields (rxcui, applicationNumber, etc.) start null.
    "Vocab": {
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
        "uid_field":  "_computed",
        "sources": [
            ("output.vocab", {}),
        ],
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
        "output.url":                   "url",
        "output.author":                "author",
        "output.documentDate":          "documentDate",
        "output.type":                  "type",
        "output.industry":              "industry",
        "output.country":               "country",
        "output.language":              "language",
        "output.summary":               "summary",
        "output.bates_number":          "batesNumber",
        "output.collection":            "collection",
        "output.source":                "source",
        "output.tid":                   "tid",
        "output.case":                  "case",
        # DOC-specific
        "output.confidentiality_notice":"confidentialityNotice",
        "output.copyright_notice":      "copyrightNotice",
        # EMAIL-specific
        "output.identifier":            "identifier",
        "output.legalStatus":           "legalStatus",
        "output.semantic_type":         "semanticType",
        "output.confidentialityNotice": "confidentialityNotice",
        "output.@type":                 "schemaType",
        "output.dateFiled":             "dateFiled",
        # PPT-specific (hasContent top-level metadata)
        "output.hasContent.audience":       "audience",
        "output.hasContent.purpose":        "purpose",
        "output.hasContent.keywords":       "keywords",
        "output.hasContent.accessMode":     "accessMode",
        "output.hasContent.copyrightHolder":"copyrightHolder",
        "output.hasContent.citation":       "citation",
        "output.hasContent.mainEntity":     "mainEntity",
        "output.hasContent.summary":        "summary",
        "output.hasContent.title":          "contentTitle",
        "output.hasContent.sharedVia.sentBy":   "sharedViaSentBy",
        "output.hasContent.sharedVia.via":      "sharedViaChannel",
        # sourceFile — present in all file types
        "output.sourceFile.fileName":   "sourceFileName",
        "output.sourceFile.fileType":   "sourceFileType",
        "output.sourceFile.hash":       "sourceFileHash",
        "output.sourceFile.pageCount":  "sourceFilePageCount",
        "_doc_uid":                     "uid",
    },

    "Person": {
        "name":         "name",
        "email":        "email",
        "phone":        "phone",
        "role":         "role",
        "address":      "address",
        "organization": "organization",
        "_uid":         "uid",
    },

    "Organization": {
        "name":  "name",
        "_uid":  "uid",
    },

    "Drug": {
        "name":        "name",
        "genericName": "genericName",
        "dosageForm":  "dosageForm",
        "strength":    "strength",
        "route":       "route",
        "rxnorm":      "rxnorm",
        "ndc":         "ndc",
        "_uid":        "uid",
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

    "Topic":    {"__self__": "name"},
    "Location": {"__self__": "name"},

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

    "EmailMessage": {
        "identifier":    "identifier",
        "subject":       "subject",
        "dateSent":      "dateSent",
        "body":          "body",
        "semantic_type": "semanticType",
        "_uid":          "uid",
    },

    "Slide": {
        "pageNumber":    "pageNumber",
        "title":         "title",
        "keyClaim":      "keyClaim",
        "speakerNotes":  "speakerNotes",
        "disclaimerText":"disclaimerText",
        "visualEvidence":"visualEvidence",
    },

    "Sheet": {
        "pageNumber":  "pageNumber",
        "title":       "title",
        "mainEntity":  "mainEntity",
        "summary":     "summary",
        "notes":       "notes",
    },

    "TableRegion": {
        "regionId":              "regionId",
        "rangeA1":               "rangeA1",
        "tableType":             "tableType",
        "layout.hasMergedCell":  "hasMergedCell",
        "layout.headerRows":     "headerRows",
        "layout.indexColumn":    "indexColumn",
        "units.currency":        "currency",
        "units.scale":           "scale",
        "units.basis":           "basis",
        "units.unitText":        "unitText",
        "rows.rowNotes":         "rowNotes",
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
        "textDocumentId":                           "textDocumentId",
        "title":                                    "title",
        "summary":                                  "summary",
        "creationDate":                             "creationDate",
        "submittedDate":                            "submittedDate",
        "submittedBy":                              "submittedBy",
        "submittedTo":                              "submittedTo",
        "configuration":                            "configuration",
        "evidence":                                 "evidence",
        "referencedBy":                             "referencedBy",
        "regulatorySubmission":                     "regulatorySubmission",
        "report":                                   "report",
        "structure.tabular.tableType":              "tableType",
        "structure.tabular.dimensions.rowCount":    "rowCount",
        "structure.tabular.dimensions.columnCount": "columnCount",
        "structure.tabular.dialect.delimiter":      "csvDelimiter",
        "structure.tabular.dialect.encoding":       "csvEncoding",
        "structure.tabular.dialect.hasHeaderRow":   "hasHeaderRow",
        "structure.sourceRefFormat":                "sourceRefFormat",
        "_has_redactions":                          "hasRedactions",
        "_uid":                                     "uid",
    },

    "TabularColumn": {
        "name":         "name",
        "index":        "index",
        "inferredType": "inferredType",
        "nullable":     "nullable",
        "description":  "description",
        "units":        "units",
    },

    "Page": {
        "pageNumber":    "pageNumber",
        "bodyText":      "bodyText",
        # header fields — from structure.pages[].header and hasContent[].header
        "header.left":   "headerLeft",
        "header.center": "headerCenter",
        "header.right":  "headerRight",
        # footer fields — from structure.pages[].footer and hasContent[].footer
        "footer.left":   "footerLeft",
        "footer.center": "footerCenter",
        "footer.right":  "footerRight",
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
        "name":       "name",
        "model":      "model",
        "identifier": "identifier",
    },

    "Event": {
        "name":      "name",
        "date":      "date",
        "startDate": "startDate",
        "location":  "location",
        "context":   "context",
        "platform":  "platform",
        "time":      "time",
        "attendees": "attendees",
    },

    "Finance": {
        "amount":   "amount",
        "currency": "currency",
        "item":     "item",
        "context":  "context",
    },

    "Metric": {
        "name":  "name",
        "value": "value",
    },

    "Risk": {
        "__self__": "description",
    },

    "Requirement": {
        "__self__": "description",
    },

    "Decision": {
        "__self__": "description",
    },

    "DateMention": {
        "date":          "date",
        "contextOfDate": "contextOfDate",
    },

    "HealthMention": {
        "__self__": "description",
    },

    "SignatureBlock": {
        "signerName":    "signerName",
        "signerTitle":   "signerTitle",
        "organization":  "organization",
        "date":          "date",
        "location":      "location",
        "signatureText": "signatureText",
    },

    "Figure": {
        "id":      "figureId",
        "title":   "title",
        "label":   "label",
        "caption": "caption",
        "context": "context",
        "source":  "source",
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

    "VisualContent": {
        "type":         "visualType",
        "description":  "description",
        "altText":      "altText",
        "embeddedText": "embeddedText",
        "source":       "source",
    },

    "Identifier": {
        "type":  "identifierType",
        "value": "value",
    },

    # ── EmbeddedObject ────────────────────────────────────────────
    # objectType distinguishes chart/table/image/comment/validation
    # collection and source trace back to the originating document
    "EmbeddedObject": {
        "objectType":  "objectType",    # e.g. chart, table, image, comment, validation
        "title":       "title",
        "notes":       "notes",
        "rangeA1":     "rangeA1",       # XLS: cell range the object occupies
        "source":      "source",        # data source for charts
        "collection":  "collection",    # inherited from parent Document.collection
        "chartType":   "chartType",     # for chart objects
        "dataSource":  "dataSource",    # for chart objects
    },

    # ── Procedure ─────────────────────────────────────────────────
    "Procedure": {
        "title":          "title",
        "preconditions":  "preconditions",
        "postconditions": "postconditions",
        "pageNumber":     "pageNumber",
        "synopsis":       "synopsis",     # TXT structure.cli.synopsis
        "steps":          "steps",        # serialized step list if present
    },

    # ── Vocab ─────────────────────────────────────────────────────
    # Core fields populated at load time from JSONL output.vocab.
    # External-library fields (rxcui, applicationNumber, etc.) are
    # null at load time and filled by post_kg_rules + external_libs.py.
    #
    # Design rule:
    #   Known sources  → named flat properties  (fast Cypher queries)
    #   Unknown future → raw_json string        (no schema migration needed)
    "Vocab": {
        # --- Core (from JSONL, written by kg_loader.py) ---
        "name":         "name",        # source id: "rxnorm" | "fda_orange_book" | "context"
        "type":         "type",        # "standardized" | "regulatory" | "contextual"
        "contextUrl":   "contextUrl",  # original @context URL from JSONL

        # --- RxNorm (filled by external_libs.py) ---
        "rxcui":          "rxcui",
        "canonicalName":  "canonicalName",
        "drugClass":      "drugClass",
        "synonyms":       "synonyms",       # JSON array string

        # --- FDA Orange Book (filled by external_libs.py) ---
        "applicationNumber": "applicationNumber",
        "approvalDate":      "approvalDate",
        "manufacturer":      "manufacturer",

        # --- Any source, always filled by external_libs.py ---
        "raw_json":     "raw_json",    # leftover API fields not covered above
        "sourceUrl":    "sourceUrl",   # exact API endpoint called
        "fetchedAt":    "fetchedAt",   # ISO timestamp of the API call
    },
}


# ================================================================
# BLOCK 3 — EDGE DEFINITIONS
# from        : source node label
# to          : target node label (list = polymorphic)
# source_path : path(s) in JSONL to find the edge targets
# via_uid     : uid field on target object to resolve node
# direction   : "reverse" = edge is target->source
# applies_to  : document type tags
# ================================================================

EDGE_DEFINITIONS = {

    # ── Document → Contact (Person / Organization) ────────────────
    "HAS_CONTACT": {
        "from":        "Document",
        "to":          ["Person", "Organization"],
        "source_path": "output.contacts[]",
        "via_uid":     "_uid",
        "applies_to":  ["DOC"],
    },
    "WORKS_FOR": {
        "from":        "Person",
        "to":          "Organization",
        "source_path": "output.contacts[]",
        "condition":   "has_organization_field",
        "applies_to":  ["DOC"],
    },

    # ── Document → Drug ───────────────────────────────────────────
    "MENTIONS_DRUG": {
        "from":        "Document",
        "to":          "Drug",
        "source_path": [
            "output.drugs[]",
            "output.hasContent[].entities.drugs[]",
            "output.hasContent[].semanticMentions.drugs[]",
            "output.hasContent.slides[].semanticMentions[].drugs[]",
        ],
        "via_uid":    "_uid",
        "applies_to": ["DOC", "EMAIL", "PPT", "XLS"],
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
        "source_path": "output.hasContent[].entities.topics[]",
        "applies_to":  ["DOC", "PPT"],
    },

    # ── Document → Location ───────────────────────────────────────
    "LOCATED_IN": {
        "from":        "Document",
        "to":          "Location",
        "source_path": [
            "output.country",
            "output.hasContent[].entities.locations[]",
            "output.hasPart[].semanticMentions.locations[]",
            "output.hasContent.slides[].semanticMentions[].locations[]",
            "output.sharedEntities.locations[]",
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
        "source_path": "output.hasPart[].semanticMentions.people[]",
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

    # ── Slide → VisualContent ─────────────────────────────────────
    "HAS_VISUAL": {
        "from":        "Slide",
        "to":          "VisualContent",
        "source_path": "output.hasContent.slides[].visualContent[]",
        "applies_to":  ["PPT"],
    },

    # ── Slide → Person (people mentioned on slide) ────────────────
    "MENTIONS_PERSON_ON_SLIDE": {
        "from":        "Slide",
        "to":          "Person",
        "source_path": "output.hasContent.slides[].semanticMentions[].people[]",
        "via_uid":     "_uid",
        "applies_to":  ["PPT"],
    },

    # ── Slide → Drug ──────────────────────────────────────────────
    "MENTIONS_DRUG_ON_SLIDE": {
        "from":        "Slide",
        "to":          "Drug",
        "source_path": "output.hasContent.slides[].semanticMentions[].drugs[]",
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

    # ── Organization → Document (XLS listed entities) ─────────────
    "LISTED_IN": {
        "from":        "Organization",
        "to":          "Document",
        "source_path": "output.sharedEntities.organization[]",
        "direction":   "reverse",
        "applies_to":  ["XLS"],
    },

    # ── Document → Person (shared / content mentions) ─────────────
    "MENTIONS_PERSON": {
        "from":        "Document",
        "to":          "Person",
        "source_path": [
            "output.hasContent[].entities.people[]",
            "output.sharedEntities.people[]",
        ],
        "via_uid":    "_uid",
        "applies_to": ["DOC", "XLS"],
    },

    # ── Document → Organization ───────────────────────────────────
    "MENTIONS_ORG": {
        "from":        "Document",
        "to":          "Organization",
        "source_path": [
            "output.hasContent[].entities.organizations[]",
            "output.hasPart[].semanticMentions.organizations[]",
            "output.sharedEntities.organization[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "XLS", "TXT"],
    },

    # ── Document → Product ────────────────────────────────────────
    "MENTIONS_PRODUCT": {
        "from":        "Document",
        "to":          "Product",
        "source_path": [
            "output.hasContent[].entities.products[]",
            "output.hasPart[].semanticMentions.products[]",
            "output.hasContent[].semanticMentions.products[]",
            "output.hasContent.slides[].semanticMentions[].products[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Event ──────────────────────────────────────────
    "HAS_EVENT": {
        "from":        "Document",
        "to":          "Event",
        "source_path": [
            "output.hasContent[].semanticMentions.events[]",
            "output.hasPart[].semanticMentions.events[]",
            "output.hasContent.slides[].semanticMentions[].events[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Finance ────────────────────────────────────────
    "HAS_FINANCE": {
        "from":        "Document",
        "to":          "Finance",
        "source_path": [
            "output.hasContent[].semanticMentions.finances[]",
            "output.hasPart[].semanticMentions.finances[]",
            "output.hasContent.slides[].semanticMentions[].finances[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Metric ─────────────────────────────────────────
    "HAS_METRIC": {
        "from":        "Document",
        "to":          "Metric",
        "source_path": [
            "output.hasContent[].semanticMentions.metrics[]",
            "output.hasPart[].semanticMentions.metrics[]",
            "output.hasContent.slides[].semanticMentions[].metrics[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Risk ───────────────────────────────────────────
    "HAS_RISK": {
        "from":        "Document",
        "to":          "Risk",
        "source_path": [
            "output.hasContent[].semanticMentions.risks[]",
            "output.hasPart[].semanticMentions.risks[]",
            "output.hasContent.slides[].semanticMentions[].risks[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Requirement ────────────────────────────────────
    "HAS_REQUIREMENT": {
        "from":        "Document",
        "to":          "Requirement",
        "source_path": [
            "output.hasContent[].semanticMentions.requirements[]",
            "output.hasPart[].semanticMentions.requirements[]",
            "output.hasContent.slides[].semanticMentions[].requirements[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → Decision ───────────────────────────────────────
    "HAS_DECISION": {
        "from":        "Document",
        "to":          "Decision",
        "source_path": [
            "output.hasContent[].semanticMentions.decisionsMade[]",
            "output.hasPart[].semanticMentions.decisionsMade[]",
            "output.hasContent.slides[].semanticMentions[].decisionsMade[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → DateMention ────────────────────────────────────
    "MENTIONS_DATE": {
        "from":        "Document",
        "to":          "DateMention",
        "source_path": [
            "output.hasContent[].entities.datesMentioned[]",
            "output.hasContent[].semanticMentions.datesMentioned[]",
            "output.hasPart[].semanticMentions.datesMentioned[]",
            "output.hasContent.slides[].semanticMentions[].datesMentioned[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → HealthMention ──────────────────────────────────
    "MENTIONS_HEALTH": {
        "from":        "Document",
        "to":          "HealthMention",
        "source_path": [
            "output.hasContent[].semanticMentions.health[]",
            "output.hasPart[].semanticMentions.health[]",
            "output.hasContent.slides[].semanticMentions[].health[]",
        ],
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },

    # ── Document → SignatureBlock ─────────────────────────────────
    "HAS_SIGNATURE": {
        "from":        "Document",
        "to":          "SignatureBlock",
        "source_path": [
            "output.signatureBlocks[]",
            "output.hasContent[].signatureBlock",
        ],
        "applies_to":  ["DOC", "TXT"],
    },

    # ── Document → Figure ─────────────────────────────────────────
    "HAS_FIGURE": {
        "from":        "Document",
        "to":          "Figure",
        "source_path": "output.hasContent[].visuals.figures[]",
        "applies_to":  ["DOC"],
    },

    # ── Document → Link ───────────────────────────────────────────
    "HAS_LINK": {
        "from":        "Document",
        "to":          "Link",
        "source_path": [
            "output.links[]",
            "output.hasContent.slides[].links[]",
        ],
        "applies_to":  ["DOC", "PPT"],
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
        "applies_to":  ["DOC"],
    },

    # ── Document → TextContent (TXT) ──────────────────────────────
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
        "source_path": "output.hasContent[].structure.tabular.columns[]",
        "applies_to":  ["TXT"],
    },

    # ── TextContent → Page ────────────────────────────────────────
    "HAS_PAGE": {
        "from":        "TextContent",
        "to":          "Page",
        "source_path": "output.hasContent[].structure.pages[]",
        "applies_to":  ["TXT"],
    },

    # ── TextContent → CellIndex ───────────────────────────────────
    "HAS_CELL": {
        "from":        "TextContent",
        "to":          "CellIndex",
        "source_path": "output.hasContent[].structure.tabular.cellIndex[]",
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

    # ── TextContent / Document → Identifier ───────────────────────
    "HAS_IDENTIFIER": {
        "from":        "Document",
        "to":          "Identifier",
        "source_path": [
            "output.hasContent[].entities.identifiers[]",
            "output.sharedEntities.identifiers[]",
        ],
        "applies_to":  ["TXT", "XLS"],
    },

    # ── Document → EmbeddedObject ─────────────────────────────────
    # Covers charts/tables in DOC visuals, PPT slide embeddedObjects,
    # XLS sheetObjects (images, comments, validations, charts)
    "HAS_EMBEDDED_OBJECT": {
        "from":        "Document",
        "to":          "EmbeddedObject",
        "source_path": [
            "output.hasContent.slides[].embeddedObjects[]",
            "output.hasContent[].sheetObjects.embeddedImages[]",
            "output.hasContent[].sheetObjects.cellComments[]",
            "output.hasContent[].sheetObjects.dataValidations[]",
            "output.hasContent[].sheetObjects.charts[]",
            "output.hasContent[].visuals.charts[]",
            "output.hasContent[].visuals.tables[]",
        ],
        "applies_to":  ["DOC", "PPT", "XLS"],
    },

    # ── Document / TextContent → Procedure ───────────────────────
    "HAS_PROCEDURE": {
        "from":        "Document",
        "to":          "Procedure",
        "source_path": [
            "output.hasContent[].structure.procedures[]",
            "output.hasContent.slides[].procedures[]",
        ],
        "applies_to":  ["TXT", "PPT"],
    },

    # ── Document → Vocab ──────────────────────────────────────────
    # One Vocab node per (Document, source) pair.
    # Core fields from JSONL at load time; API fields added post-load.
    "HAS_VOCAB": {
        "from":        "Document",
        "to":          "Vocab",
        "source_path": "output.vocab",
        "applies_to":  ["DOC", "EMAIL", "PPT", "XLS", "TXT"],
    },
}


# ================================================================
# BLOCK 4 — EDGE ATTRIBUTE MAPPING
# Properties to carry on each relationship.
# "source_field_on_target" : "neo4j_rel_property"
# Empty dict = no properties on the relationship.
# ================================================================

EDGE_ATTRIBUTES = {
    "HAS_CONTACT":               {"pageNumber": "pageNumber"},
    "WORKS_FOR":                 {},
    "MENTIONS_DRUG":             {"pageNumber": "pageNumber"},
    "MENTIONS_DRUG_ON_SLIDE":    {"pageNumber": "pageNumber"},
    "HAS_CLAIM":                 {"pageNumber": "pageNumber"},
    "COVERS_TOPIC":              {},
    "LOCATED_IN":                {},
    "HAS_ABBREVIATION":          {"pageNumber": "pageNumber"},
    "CITES":                     {"pageNumber": "pageNumber"},
    "HAS_LEGAL_FRAMEWORK":       {},
    "HAS_MESSAGE":               {},
    "SENT_BY":                   {},
    "SENT_TO":                   {},
    "MENTIONS_PERSON_IN_MSG":    {},
    "MENTIONS_PERSON_ON_SLIDE":  {"pageNumber": "pageNumber"},
    "HAS_SLIDE":                 {"pageNumber": "order"},
    "HAS_VISUAL":                {},
    "HAS_SHEET":                 {"pageNumber": "order"},
    "HAS_TABLE_REGION":          {"regionId": "regionId"},
    "HAS_PIVOT_TABLE":           {},
    "HAS_FORMULA":               {"cell": "cell"},
    "HAS_ASSESSMENT":            {},
    "LISTED_IN":                 {},
    "MENTIONS_PERSON":           {"pageNumber": "pageNumber"},
    "MENTIONS_ORG":              {},
    "MENTIONS_PRODUCT":          {"pageNumber": "pageNumber"},
    "HAS_EVENT":                 {"date": "date"},
    "HAS_FINANCE":               {"amount": "amount", "currency": "currency"},
    "HAS_METRIC":                {"value": "value"},
    "HAS_RISK":                  {},
    "HAS_REQUIREMENT":           {},
    "HAS_DECISION":              {},
    "MENTIONS_DATE":             {"date": "date", "contextOfDate": "contextOfDate"},
    "MENTIONS_HEALTH":           {},
    "HAS_SIGNATURE":             {"pageNumber": "pageNumber"},
    "HAS_FIGURE":                {"pageNumber": "pageNumber"},
    "HAS_LINK":                  {"pageNumber": "pageNumber"},
    "HAS_CASE_CONTEXT":          {},
    "HAS_SECTION":               {"section_type": "sectionType"},
    "HAS_TEXT_CONTENT":          {},
    "HAS_COLUMN":                {"index": "colIndex"},
    "HAS_PAGE":                  {"pageNumber": "pageNumber"},
    "HAS_CELL":                  {"rowNumber": "rowNumber", "columnName": "columnName"},
    "MENTIONS_LOCATION_IN_TEXT": {},
    "MENTIONS_ORG_IN_TEXT":      {},
    "MENTIONS_PRODUCT_IN_TEXT":  {"pageNumber": "pageNumber"},
    "HAS_IDENTIFIER":            {"pageNumber": "pageNumber"},
    "HAS_EMBEDDED_OBJECT":       {"pageNumber": "pageNumber", "objectType": "objectType"},
    "HAS_PROCEDURE":             {"pageNumber": "pageNumber"},
    "HAS_VOCAB":                  {},
}


# ================================================================
# Pipeline — delegates rule logic to rule_engine.py
# ================================================================

def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc",     default="DOC.jsonl")
    parser.add_argument("--email",   default="EMAIL.jsonl")
    parser.add_argument("--ppt",     default="PPT.jsonl")
    parser.add_argument("--xls",     default="XLS.jsonl")
    parser.add_argument("--txt",     default="TXT.jsonl")
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
        print(f"\n[{label}] Reading {src} ...")
        records = load_jsonl(src)
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
