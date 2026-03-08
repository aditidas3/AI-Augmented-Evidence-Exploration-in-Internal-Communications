"""
rule_engine.py
==============
AUTO-GENERATED from pre_kg_config.yaml by an LLM.
DO NOT HAND-EDIT this file. Edit pre_kg_config.yaml and regenerate.

Exposes:
    apply_all_rules(rec, idx, file_type) -> rec
    VIOLATIONS   list of {record, rule, message}
    WARNINGS     list of {record, rule, message}
    STATS        dict of counts

Run order:
    apply_structural_integrity
    -> apply_format_normalization
    -> apply_uid_assignment
    -> apply_graph_semantics
    -> apply_dedup_normalization
    -> apply_txt_rules  (only when file_type matches TXT document type)
"""

import re
import hashlib
from datetime import datetime
from collections import defaultdict

# ---------------------------------------------------------------------------
# State — collected across all records, read by pre_kg_rules.py for report
# ---------------------------------------------------------------------------
VIOLATIONS = []
WARNINGS   = []
STATS      = defaultdict(int)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _str(v):
    return str(v).strip() if v is not None else ""

def _ok(v):
    return _str(v) != ""

def _list(v):
    return v if isinstance(v, list) else []

def _stable_id(*parts):
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]

def _clean_email(addr):
    addr = _str(addr)
    if "@" in addr and re.match(r'[^@]+@[^@]+\.[^@]+', addr):
        return addr.lower()
    return ""

def _clean_url(url):
    url = _str(url)
    return url if url.startswith("http://") or url.startswith("https://") else ""

def _norm_date(raw, formats=None):
    if not raw:
        return ""
    formats = formats or [
        "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%m/%d/%Y", "%d/%m/%Y"
    ]
    for fmt in formats:
        try:
            return datetime.strptime(str(raw)[:len(fmt)], fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return _str(raw)

def _get_nested(obj, dotpath):
    for key in dotpath.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj

def _v(rid, rule, msg):
    VIOLATIONS.append({"record": rid, "rule": rule, "message": msg})

def _w(rid, rule, msg):
    WARNINGS.append({"record": rid, "rule": rule, "message": msg})


# ===========================================================================
# apply_structural_integrity
# Generated from rule_set_structural_integrity in pre_kg_config.yaml
# ===========================================================================

def apply_structural_integrity(rec, rid, file_type):

    # A1: every record must have a non-empty 'id' — auto-generate if missing
    if not _ok(rec.get("id")):
        _v(rid, "A1", f"[{file_type}] Record missing 'id' — auto-generated")
        rec["id"] = _stable_id(file_type, rid)

    # A2: every record must have an 'output' dict — replace with {} if missing
    if not isinstance(rec.get("output"), dict):
        _v(rid, "A2", f"[{file_type}] Record missing 'output' object — defaulting to {{}}")
        rec["output"] = {}

    out = rec["output"]

    # A3: EMAIL must have identifier and non-empty hasPart list
    if file_type == "EMAIL":
        if not _ok(out.get("identifier")):
            _v(rid, "A3", "[EMAIL] Missing output.identifier")
        if not isinstance(out.get("hasPart"), list) or len(out.get("hasPart", [])) == 0:
            _v(rid, "A3", "[EMAIL] output.hasPart is missing or empty")

    # A4: DOC, PPT, XLS, TXT must have url, industry, collection
    if file_type in ("DOC", "PPT", "XLS", "TXT"):
        for f in ["url", "industry", "collection"]:
            if not _ok(out.get(f)):
                _w(rid, "A4", f"[{file_type}] Missing or empty field: output.{f}")

    # A5: PPT, XLS, TXT must have hasContent
    if file_type in ("PPT", "XLS", "TXT"):
        if not out.get("hasContent"):
            _w(rid, "A5", f"[{file_type}] Missing or empty field: output.hasContent")

    return rec


# ===========================================================================
# apply_format_normalization
# Generated from rule_set_format_normalization in pre_kg_config.yaml
# ===========================================================================

_B1_FORMATS = ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%m/%d/%Y", "%d/%m/%Y"]

def apply_format_normalization(rec, rid, file_type):
    out = rec["output"]

    # B1: normalize top-level date fields + nested hasPart[].dateSent
    for f in ["documentDate", "dateAddedUCSF", "dateFiled"]:
        if f in out:
            normed = _norm_date(out[f], _B1_FORMATS)
            if normed != out[f]:
                STATS[f"B1_date_normalized_{file_type}"] += 1
            out[f] = normed
    for msg in _list(out.get("hasPart", [])):
        if "dateSent" in msg:
            msg["dateSent"] = _norm_date(msg["dateSent"], _B1_FORMATS)

    # B2: validate and clean email addresses (DOC, EMAIL)
    if file_type in ("DOC", "EMAIL"):
        for contact in _list(out.get("contacts", [])):
            raw     = contact.get("email", "")
            cleaned = _clean_email(raw)
            if raw and not cleaned:
                _w(rid, "B2", f"[{file_type}] Contact '{contact.get('name','')}' has invalid email: {raw!r}")
            contact["email"] = cleaned
        for msg in _list(out.get("hasPart", [])):
            sender = msg.get("sender", {})
            if isinstance(sender, dict):
                sender["email"] = _clean_email(sender.get("email", ""))
            for r_obj in _list(msg.get("recipient", [])):
                if isinstance(r_obj, dict):
                    r_obj["email"] = _clean_email(r_obj.get("email", ""))

    # B3: validate URLs — now includes XLS as well
    if file_type in ("DOC", "PPT", "TXT", "XLS"):
        if "url" in out:
            out["url"] = _clean_url(out["url"])
        for link in _list(out.get("links", [])):
            if isinstance(link, dict):
                link["url"] = _clean_url(link.get("url", ""))
        for contact in _list(out.get("contacts", [])):
            if isinstance(contact, dict):
                contact["url"] = _clean_url(contact.get("url", ""))

    # B4: normalize language to list of lowercase strings
    lang = out.get("language", "")
    if isinstance(lang, str):
        out["language"] = [lang.lower()] if lang else []
    elif isinstance(lang, list):
        out["language"] = [str(l).lower() for l in lang if l]

    # B5: sourceFile.pageCount to int
    sf = out.get("sourceFile", {})
    if isinstance(sf, dict) and "pageCount" in sf:
        try:
            sf["pageCount"] = int(sf["pageCount"])
        except (ValueError, TypeError):
            _w(rid, "B5", f"[{file_type}] sourceFile.pageCount not coercible to int: {sf['pageCount']!r}")

    return rec


# ===========================================================================
# apply_uid_assignment
# Generated from rule_set_uid_assignment in pre_kg_config.yaml
# ===========================================================================

def apply_uid_assignment(rec, rid, file_type):
    out = rec["output"]

    # C1: contact uids (DOC)
    if file_type == "DOC":
        for contact in _list(out.get("contacts", [])):
            email = _str(contact.get("email")).lower()
            name  = _str(contact.get("name")).lower()
            org   = _str(contact.get("organization")).lower()
            key   = email if email else f"{name}|{org}"
            contact["_uid"] = _stable_id("contact", key)

    # C2: email message uids
    if file_type == "EMAIL":
        for msg in _list(out.get("hasPart", [])):
            msg["_uid"] = _stable_id(
                "email_msg",
                _str(msg.get("identifier")),
                _str(msg.get("subject")),
                _str(msg.get("dateSent")),
            )

    # C3: person uids for sender/recipient
    if file_type == "EMAIL":
        for msg in _list(out.get("hasPart", [])):
            sender = msg.get("sender", {})
            if isinstance(sender, dict):
                e = _clean_email(sender.get("email", ""))
                sender["_uid"] = _stable_id("person", e or _str(sender.get("name")).lower())
            for r_obj in _list(msg.get("recipient", [])):
                if isinstance(r_obj, dict):
                    e = _clean_email(r_obj.get("email", ""))
                    r_obj["_uid"] = _stable_id("person", e or _str(r_obj.get("name")).lower())

    # C4: drug uids (DOC, EMAIL)
    if file_type in ("DOC", "EMAIL"):
        for drug in _list(out.get("drugs", [])):
            generic = _str(drug.get("genericName")).lower()
            name    = _str(drug.get("name")).lower()
            drug["_uid"] = _stable_id("drug", generic or name)
        for hc in _list(out.get("hasContent", [])):
            if not isinstance(hc, dict):
                continue
            for drug in _list(hc.get("entities", {}).get("drugs", [])):
                if isinstance(drug, dict):
                    generic = _str(drug.get("genericName")).lower()
                    name    = _str(drug.get("name")).lower()
                    drug["_uid"] = _stable_id("drug", generic or name)

    # C5: XLS org uid — convert string list to list of {name, _uid}
    if file_type == "XLS":
        se = out.get("sharedEntities", {})
        if isinstance(se, dict):
            normed = []
            for org in _list(se.get("organization", [])):
                normed.append({
                    "name": _str(org),
                    "_uid": _stable_id("org", _str(org).lower()),
                })
            se["organization"] = normed

    # C6: document uid — stored at rec['_doc_uid']
    bates = _str(out.get("bates_number"))
    url   = _str(out.get("url"))
    key   = bates or url or rid
    rec["_doc_uid"] = _stable_id("doc", file_type, key)

    return rec


# ===========================================================================
# apply_graph_semantics
# Generated from rule_set_graph_semantics in pre_kg_config.yaml
# ===========================================================================

_D2_VALID_LEGAL = {"legislation_document", "contract", "regulatory", "other", ""}

def apply_graph_semantics(rec, rid, file_type):
    out = rec["output"]

    # D1: contact_type enum (DOC)
    if file_type == "DOC":
        for contact in _list(out.get("contacts", [])):
            ct = _str(contact.get("contact_type")).lower()
            if ct not in {"individual", "organization"}:
                _w(rid, "D1", f"[DOC] Unknown contact_type '{ct}' — defaulting to 'individual'")
                contact["contact_type"] = "individual"
            else:
                contact["contact_type"] = ct

    # D2: legalFramework.type enum (DOC, PPT, XLS — NOT TXT, handled by apply_txt_rules)
    if file_type in ("DOC", "PPT", "XLS"):
        lf = out.get("legalFramework", {})
        if isinstance(lf, dict):
            lft = _str(lf.get("type")).lower()
            if lft not in _D2_VALID_LEGAL:
                _w(rid, "D2", f"[{file_type}] Unknown legalFramework.type '{lft}'")
            lf["type"] = lft

    # D3: no self-loop sender == recipient (EMAIL) — apply_uid_assignment runs first
    if file_type == "EMAIL":
        for msg in _list(out.get("hasPart", [])):
            s_uid = msg.get("sender", {}).get("_uid", "")
            for r_obj in _list(msg.get("recipient", [])):
                if r_obj.get("_uid") == s_uid and s_uid:
                    _w(rid, "D3", f"[EMAIL] Message '{msg.get('identifier','')}': sender == recipient (uid={s_uid})")

    # D4: claims must have subject — auto-fill from claim_text if missing (DOC)
    if file_type == "DOC":
        for i, claim in enumerate(_list(out.get("claims", []))):
            if not _ok(claim.get("subject")):
                _w(rid, "D4", f"[DOC] claim[{i}] missing 'subject' — auto-filling from claim_text")
                text = _str(claim.get("claim_text"))
                claim["subject"] = text[:60] if text else "unknown_subject"

    # D5: PPT slide pageNumbers must be unique
    if file_type == "PPT":
        slides = out.get("hasContent", {})
        if isinstance(slides, dict):
            slides = slides.get("slides", [])
        seen = set()
        for slide in _list(slides):
            pn = slide.get("pageNumber")
            if pn in seen:
                _w(rid, "D5", f"[PPT] Duplicate slide pageNumber: {pn}")
            seen.add(pn)

    # D6: XLS finance amounts to float
    if file_type == "XLS":
        for hc in _list(out.get("hasContent", [])):
            if not isinstance(hc, dict):
                continue
            for fin in _list(hc.get("semanticMentions", {}).get("finances", [])):
                if isinstance(fin, dict) and "amount" in fin:
                    try:
                        fin["amount"] = float(str(fin["amount"]).replace(",", ""))
                    except (ValueError, TypeError):
                        pass

    return rec


# ===========================================================================
# apply_dedup_normalization
# Generated from rule_set_dedup_normalization in pre_kg_config.yaml
# ===========================================================================

def apply_dedup_normalization(rec, rid, file_type):
    out = rec["output"]

    # E1: deduplicate abbreviations by abbv_name (case-insensitive)
    seen, deduped = {}, []
    for a in _list(out.get("abbreviations", [])):
        key = _str(a.get("abbv_name")).upper()
        if key not in seen:
            seen[key] = True
            deduped.append(a)
        else:
            STATS[f"E1_abbr_dedup_{file_type}"] += 1
    out["abbreviations"] = deduped

    # E2: title-case topic strings (DOC, PPT)
    if file_type in ("DOC", "PPT"):
        for hc in _list(out.get("hasContent", [])):
            if not isinstance(hc, dict):
                continue
            entities = hc.get("entities", {})
            if isinstance(entities, dict):
                entities["topics"] = [
                    t.strip().title()
                    for t in _list(entities.get("topics", []))
                    if _ok(t)
                ]

    # E3: collapse whitespace in org names (DOC, EMAIL, PPT)
    if file_type in ("DOC", "EMAIL", "PPT"):
        for hc in _list(out.get("hasContent", [])):
            if not isinstance(hc, dict):
                continue
            entities = hc.get("entities", {})
            if isinstance(entities, dict):
                entities["organizations"] = [
                    re.sub(r"\s+", " ", o).strip()
                    for o in _list(entities.get("organizations", []))
                    if _ok(o)
                ]

    return rec


# ===========================================================================
# apply_txt_rules
# Generated from rule_set_txt_rules in pre_kg_config.yaml
# Only called when file_type matches the TXT document type
# ===========================================================================

_F3_FORMATS = ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"]

_F6_VALID_TYPES = {
    "string", "integer", "number", "float", "boolean",
    "date", "datetime", "email", "url", "null", "unknown", ""
}

def apply_txt_rules(rec, rid):
    out = rec["output"]

    # F1: sourceFile.fileName required (warning); hasContent required (violation)
    sf = out.get("sourceFile", {})
    if not _ok((sf or {}).get("fileName")):
        _w(rid, "F1", "[TXT] Missing output.sourceFile.fileName")
    hc_top = out.get("hasContent", [])
    if not isinstance(hc_top, list) or len(hc_top) == 0:
        _v(rid, "F1", "[TXT] output.hasContent is missing or empty")

    # F2: each hasContent item must have a non-empty title
    for i, hc in enumerate(_list(out.get("hasContent", []))):
        if not _ok(hc.get("title")):
            _w(rid, "F2", f"[TXT] hasContent[{i}] missing 'title'")

    # F3: normalize creationDate and submittedDate inside hasContent[]
    for hc in _list(out.get("hasContent", [])):
        for df in ("creationDate", "submittedDate"):
            if df in hc:
                hc[df] = _norm_date(hc[df], _F3_FORMATS)

    # F4: tabular dimensions rowCount/columnCount to int
    for hc in _list(out.get("hasContent", [])):
        dims = hc.get("structure", {}).get("tabular", {}).get("dimensions", {})
        if isinstance(dims, dict):
            for dk in ("rowCount", "columnCount"):
                if dk in dims:
                    try:
                        dims[dk] = int(dims[dk])
                    except (ValueError, TypeError):
                        _w(rid, "F4", f"[TXT] tabular.dimensions.{dk} not coercible to int: {dims[dk]!r}")

    # F5: detect UCSF-redacted cells; flag each hasContent item
    total_redacted = 0
    for hc in _list(out.get("hasContent", [])):
        count = 0
        for row in _list(hc.get("structure", {}).get("tabular", {}).get("rows", [])):
            for val in (row.get("values") or {}).values():
                if "UCSF Redaction" in _str(val):
                    count += 1
        hc["_has_redactions"] = count > 0
        total_redacted += count
    if total_redacted > 0:
        _w(rid, "F5", f"[TXT] {total_redacted} tabular cell(s) contain UCSF-redacted values")
        STATS["F5_redacted_cells_TXT"] += total_redacted

    # F6: validate and lowercase tabular column inferredType
    for hc in _list(out.get("hasContent", [])):
        for col in _list(hc.get("structure", {}).get("tabular", {}).get("columns", [])):
            ct = _str(col.get("inferredType", "")).lower()
            if ct not in _F6_VALID_TYPES:
                _w(rid, "F6", f"[TXT] Unknown inferredType '{ct}' for column '{col.get('name','?')}'")
            col["inferredType"] = ct

    # F7: legalFramework.type is free-text for TXT — lowercase + tag, skip D2
    lf = out.get("legalFramework", {})
    if isinstance(lf, dict) and lf.get("type"):
        lf["type"] = _str(lf["type"]).lower()
        lf["_type_is_freetext"] = True

    # F8: stable _uid for each hasContent item
    for i, hc in enumerate(_list(out.get("hasContent", []))):
        doc_id = _str(hc.get("textDocumentId") or rec.get("id", f"TXT_{i}"))
        hc["_uid"] = _stable_id("txt_content", doc_id, str(i))

    # F9: stable _uid for org entities inside hasContent[]
    for hc in _list(out.get("hasContent", [])):
        for org in _list(hc.get("entities", {}).get("organizations", [])):
            if isinstance(org, dict):
                org["_uid"] = _stable_id("org", _str(org.get("name", "")).lower())

    # F10: strip trailing commas and collapse whitespace in TXT org names
    for hc in _list(out.get("hasContent", [])):
        for org in _list(hc.get("entities", {}).get("organizations", [])):
            if isinstance(org, dict) and "name" in org:
                org["name"] = re.sub(r"\s+", " ", org["name"]).strip().rstrip(",")

    return rec


# ===========================================================================
# Public entry point — called by pre_kg_rules.py
# ===========================================================================

def apply_all_rules(rec, idx, file_type):
    """
    Apply all enabled rules in order:
        apply_structural_integrity
        -> apply_format_normalization
        -> apply_uid_assignment
        -> apply_graph_semantics
        -> apply_dedup_normalization
        -> apply_txt_rules  (only when file_type matches TXT document type)
    """
    rid = rec.get("id", f"{file_type}_{idx}")

    rec = apply_structural_integrity(rec, rid, file_type)
    rec = apply_format_normalization(rec, rid, file_type)
    rec = apply_uid_assignment(rec, rid, file_type)
    rec = apply_graph_semantics(rec, rid, file_type)
    rec = apply_dedup_normalization(rec, rid, file_type)

    if file_type == "TXT":
        rec = apply_txt_rules(rec, rid)

    STATS[f"{file_type}_records"] += 1
    return rec
