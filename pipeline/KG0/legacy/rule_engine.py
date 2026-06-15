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
# ===========================================================================

def apply_structural_integrity(rec, rid, file_type):

    # A1: every record must have a non-empty 'id'
    if not _ok(rec.get("id")):
        _v(rid, "A1", f"[{file_type}] Record missing 'id' — auto-generated")
        rec["id"] = _stable_id(file_type, rid)

    # A2: every record must have an 'output' dict
    if not isinstance(rec.get("output"), dict):
        _v(rid, "A2", f"[{file_type}] Record missing 'output' object — defaulting to {{}}")
        rec["output"] = {}

    out = rec["output"]

    # A3: EMAIL must have non-empty hasPart list
    if file_type == "EMAIL":
        if not isinstance(out.get("hasPart"), list) or len(out.get("hasPart", [])) == 0:
            _v(rid, "A3", "[EMAIL] output.hasPart is missing or empty")

    # A4: disabled — see pre_kg_config.yaml
    # if file_type in ("DOC", "PPT", "XLS", "TXT"):
    #     for f in ["url", "industry", "collection"]:
    #         if not _ok(out.get(f)):
    #             _w(rid, "A4", f"[{file_type}] Missing or empty field: output.{f}")

    # A5: PPT, XLS, TXT must have hasContent
    if file_type in ("PPT", "XLS", "TXT"):
        if not out.get("hasContent"):
            _w(rid, "A5", f"[{file_type}] Missing or empty field: output.hasContent")

    return rec


# ===========================================================================
# apply_format_normalization
# ===========================================================================

_B1_FORMATS = ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%m/%d/%Y", "%d/%m/%Y"]

def apply_format_normalization(rec, rid, file_type):
    out = rec["output"]

    # B1: normalize date fields
    for f in ["documentDate", "dateAdded", "dateFiled"]:
        if f in out:
            normed = _norm_date(out[f], _B1_FORMATS)
            if normed != out[f]:
                STATS[f"B1_date_normalized_{file_type}"] += 1
            out[f] = normed
    
    for msg in _list(out.get("hasPart", [])):
        if "dateSent" in msg:
            msg["dateSent"] = _norm_date(msg["dateSent"], _B1_FORMATS)

    # B2: validate and clean email addresses
    if file_type in ("DOC", "TXT"):
        for contact in _list(out.get("contacts", [])):
            raw     = contact.get("email", "")
            cleaned = _clean_email(raw)
            if raw and not cleaned:
                _w(rid, "B2", f"[{file_type}] Contact '{contact.get('name','')}' has invalid email: {raw!r}")
            contact["email"] = cleaned
    if file_type == "EMAIL":
        for msg in _list(out.get("hasPart", [])):
            sender = msg.get("sender", {})
            if isinstance(sender, dict):
                sender["email"] = _clean_email(sender.get("email", ""))
            for r_obj in _list(msg.get("recipient", [])):
                if isinstance(r_obj, dict):
                    r_obj["email"] = _clean_email(r_obj.get("email", ""))

    # B3: validate URLs
    if file_type in ("DOC", "PPT", "TXT", "XLS"):
        if "url" in out:
            out["url"] = _clean_url(out["url"])
        for link in _list(out.get("links", [])):
            if isinstance(link, dict):
                link["url"] = _clean_url(link.get("url", ""))
        for contact in _list(out.get("contacts", [])):
            if isinstance(contact, dict):
                contact["url"] = _clean_url(contact.get("url", ""))

    # B4: normalize language to lowercase plain string (not list)
    lang = out.get("language", "")
    if isinstance(lang, list):
        out["language"] = str(lang[0]).lower() if lang else ""
    elif isinstance(lang, str):
        out["language"] = lang.lower()

    # B5: sourceFile.pageCount to int.
    # Skip silently if empty — pageCount may not be available at extraction time.
    # EMAIL records do not carry pageCount at all, also skipped.
    if file_type != "EMAIL":
        sf = out.get("sourceFile", {})
        if isinstance(sf, dict) and sf.get("pageCount", "") != "":
            try:
                sf["pageCount"] = int(sf["pageCount"])
            except (ValueError, TypeError):
                _w(rid, "B5", f"[{file_type}] sourceFile.pageCount not coercible to int: {sf['pageCount']!r}")

    return rec


# ===========================================================================
# apply_uid_assignment
# ===========================================================================

def apply_uid_assignment(rec, rid, file_type):
    out = rec["output"]

    # C1: contact uids — DOC and TXT only
    if file_type in ("DOC", "TXT"):
        for contact in _list(out.get("contacts", [])):
            email = _str(contact.get("email")).lower()
            name  = _str(contact.get("name")).lower()
            org   = _str(contact.get("organization")).lower()
            key   = email if email else f"{name}|{org}"
            contact["_uid"] = _stable_id("contact", key)

    # C2: email message uids — key: subject + dateSent
    if file_type == "EMAIL":
        for msg in _list(out.get("hasPart", [])):
            msg["_uid"] = _stable_id(
                "email_msg",
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

    # C4: drug uids — all types, key: lowercased name
    drug_paths = []
    for hc in _list(out.get("hasContent", [])):
        if isinstance(hc, dict):
            drug_paths.extend(_list(hc.get("entities", {}).get("drugs", [])))
    # PPT slides path
    hc_ppt = out.get("hasContent", {})
    if isinstance(hc_ppt, dict):
        for slide in _list(hc_ppt.get("slides", [])):
            drug_paths.extend(_list(slide.get("entities", {}).get("drugs", [])))
    # EMAIL hasPart path
    for msg in _list(out.get("hasPart", [])):
        drug_paths.extend(_list(msg.get("entities", {}).get("drugs", [])))
    for drug in drug_paths:
        if isinstance(drug, dict):
            name = _str(drug.get("name")).lower()
            drug["_uid"] = _stable_id("drug", name)

    # C5: organization uids — all types, key: lowercased name
    org_paths = []
    for hc in _list(out.get("hasContent", [])):
        if isinstance(hc, dict):
            org_paths.extend(_list(hc.get("entities", {}).get("organizations", [])))
    hc_ppt = out.get("hasContent", {})
    if isinstance(hc_ppt, dict):
        for slide in _list(hc_ppt.get("slides", [])):
            org_paths.extend(_list(slide.get("entities", {}).get("organizations", [])))
    for msg in _list(out.get("hasPart", [])):
        org_paths.extend(_list(msg.get("entities", {}).get("organizations", [])))
    for org in org_paths:
        if isinstance(org, dict):
            org["_uid"] = _stable_id("org", _str(org.get("name", "")).lower())

    # C6: document uid — keyed on record id
    rec["_doc_uid"] = _stable_id("doc", file_type, _str(rec.get("id", rid)))

    # C7: author uids — all types
    for author in _list(out.get("author", [])):
        if isinstance(author, dict):
            email = _clean_email(author.get("email", ""))
            name  = _str(author.get("name")).lower()
            org   = _str(author.get("organization")).lower()
            key   = email if email else f"{name}|{org}"
            author["_uid"] = _stable_id("person", key)

    return rec


# ===========================================================================
# apply_graph_semantics
# ===========================================================================

def apply_graph_semantics(rec, rid, file_type):
    out = rec["output"]

    # D1: normalise contact_type to lowercase — no enum restriction.
    # The LLM may assign any descriptive role (individual, organization, author,
    # sender, signatory, etc.) and all are valid. Default empty to "individual".
    if file_type in ("DOC", "TXT"):
        for contact in _list(out.get("contacts", [])):
            ct = _str(contact.get("contact_type")).lower()
            contact["contact_type"] = ct if ct else "individual"

    # D2: normalise legalFramework.type to lowercase across all types.
    lf = out.get("legalFramework", {})
    if isinstance(lf, dict) and lf.get("type"):
        lf["type"] = _str(lf["type"]).lower()



    # D4: claims subject auto-fill
    if file_type == "DOC":
        for claim in _list(out.get("claims", [])):
            if not _ok(claim.get("subject")):
                fallback = _str(claim.get("claim_text", ""))[:60] or "unknown_subject"
                _w(rid, "D4", f"[DOC] Claim missing 'subject' — auto-filled: {fallback!r}")
                claim["subject"] = fallback

    # D5: PPT slide pageNumber uniqueness
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

    # D6: Finance amounts to float — all types, entities path
    for hc in _list(out.get("hasContent", [])):
        if not isinstance(hc, dict):
            continue
        for fin in _list(hc.get("entities", {}).get("finances", [])):
            if isinstance(fin, dict) and "finance_string" in fin:
                try:
                    fin["finance_string"] = float(str(fin["finance_string"]).replace(",", ""))
                except (ValueError, TypeError):
                    pass
    # EMAIL hasPart path
    for msg in _list(out.get("hasPart", [])):
        for fin in _list(msg.get("entities", {}).get("finances", [])):
            if isinstance(fin, dict) and "finance_string" in fin:
                try:
                    fin["finance_string"] = float(str(fin["finance_string"]).replace(",", ""))
                except (ValueError, TypeError):
                    pass

    return rec


# ===========================================================================
# apply_dedup_normalization
# ===========================================================================

def apply_dedup_normalization(rec, rid, file_type):
    out = rec["output"]

    # E1: deduplicate abbreviations by abbv_name
    seen, deduped = {}, []
    for a in _list(out.get("abbreviations", [])):
        key = _str(a.get("abbv_name")).upper()
        if key not in seen:
            seen[key] = True
            deduped.append(a)
        else:
            STATS[f"E1_abbr_dedup_{file_type}"] += 1
    out["abbreviations"] = deduped

    # E2: title-case topic_string — all 5 types
    def _norm_topics(topics):
        result = []
        for t in _list(topics):
            if isinstance(t, dict):
                ts = _str(t.get("topic_string", ""))
                t["topic_string"] = ts.strip().title()
                result.append(t)
        return result

    for hc in _list(out.get("hasContent", [])):
        if isinstance(hc, dict):
            entities = hc.get("entities", {})
            if isinstance(entities, dict):
                entities["topics"] = _norm_topics(entities.get("topics", []))
    hc_ppt = out.get("hasContent", {})
    if isinstance(hc_ppt, dict):
        for slide in _list(hc_ppt.get("slides", [])):
            entities = slide.get("entities", {})
            if isinstance(entities, dict):
                entities["topics"] = _norm_topics(entities.get("topics", []))
    for msg in _list(out.get("hasPart", [])):
        entities = msg.get("entities", {})
        if isinstance(entities, dict):
            entities["topics"] = _norm_topics(entities.get("topics", []))

    # E3: collapse whitespace in org name — all types, object arrays
    def _norm_org_names(orgs):
        for o in _list(orgs):
            if isinstance(o, dict) and "name" in o:
                o["name"] = re.sub(r"\s+", " ", o["name"]).strip()
        return orgs

    for hc in _list(out.get("hasContent", [])):
        if isinstance(hc, dict):
            entities = hc.get("entities", {})
            if isinstance(entities, dict):
                _norm_org_names(entities.get("organizations", []))
    hc_ppt = out.get("hasContent", {})
    if isinstance(hc_ppt, dict):
        for slide in _list(hc_ppt.get("slides", [])):
            entities = slide.get("entities", {})
            if isinstance(entities, dict):
                _norm_org_names(entities.get("organizations", []))
    for msg in _list(out.get("hasPart", [])):
        entities = msg.get("entities", {})
        if isinstance(entities, dict):
            _norm_org_names(entities.get("organizations", []))

    return rec


# ===========================================================================
# apply_txt_rules
# Only called when file_type == 'TXT'
# ===========================================================================

_F3_FORMATS = ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"]

_F6_VALID_TYPES = {
    "string", "integer", "number", "float", "boolean",
    "date", "datetime", "email", "url", "null", "unknown", ""
}

def apply_txt_rules(rec, rid):
    out = rec["output"]

    # F1: sourceFile.fileName required; hasContent required
    sf = out.get("sourceFile", {})
    if not _ok((sf or {}).get("fileName")):
        _w(rid, "F1", "[TXT] Missing output.sourceFile.fileName")
    hc_top = out.get("hasContent", [])
    if not isinstance(hc_top, list) or len(hc_top) == 0:
        _v(rid, "F1", "[TXT] output.hasContent is missing or empty")

    # F3: normalize creationDate and submittedDate inside hasContent[]
    for hc in _list(out.get("hasContent", [])):
        for df in ("creationDate", "submittedDate"):
            if df in hc:
                hc[df] = _norm_date(hc[df], _F3_FORMATS)

    # F4: tabular dimensions to int
    for hc in _list(out.get("hasContent", [])):
        dims = hc.get("tabular", {}).get("dimensions", {})
        if isinstance(dims, dict):
            for dk in ("rowCount", "columnCount"):
                if dk in dims:
                    try:
                        dims[dk] = int(dims[dk])
                    except (ValueError, TypeError):
                        _w(rid, "F4", f"[TXT] tabular.dimensions.{dk} not coercible to int: {dims[dk]!r}")

    # F5: detect redacted cells; flag each hasContent item
    total_redacted = 0
    for hc in _list(out.get("hasContent", [])):
        count = 0
        for row in _list(hc.get("tabular", {}).get("rows", [])):
            for val in (row.get("values") or {}).values():
                if "Redaction" in _str(val):
                    count += 1
        hc["_has_redactions"] = count > 0
        total_redacted += count
    if total_redacted > 0:
        _w(rid, "F5", f"[TXT] {total_redacted} tabular cell(s) contain redacted values")
        STATS["F5_redacted_cells_TXT"] += total_redacted

    # F6: validate and lowercase tabular column inferredType
    for hc in _list(out.get("hasContent", [])):
        for col in _list(hc.get("tabular", {}).get("columns", [])):
            ct = _str(col.get("inferredType", "")).lower()
            if ct not in _F6_VALID_TYPES:
                _w(rid, "F6", f"[TXT] Unknown inferredType '{ct}' for column '{col.get('name','?')}'")
            col["inferredType"] = ct

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
# Public entry point
# ===========================================================================

def apply_all_rules(rec, idx, file_type):
    """
    Apply all enabled rules in order.
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
