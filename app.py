version: "1.0.0"

variables:
  patient_id:
    data_type: string
    required: true
    aliases: [pt_id, patid]
    description: Unique patient identifier

  encounter_id:
    data_type: string
    required: true
    aliases: [visit_id, enc_id]
    description: Encounter/visit identifier

  admission_dt:
    data_type: datetime
    required: true
    aliases: [admit_dt, adm_dt]
    description: Admission datetime

  discharge_dt:
    data_type: datetime
    required: false
    aliases: [dc_dt, dis_dt]
    description: Discharge datetime

  sbp_mmhg:
    data_type: float
    required: false
    range: {min: 50, max: 250}
    aliases: [SBP, sbp, systolic_bp]
    description: Systolic blood pressure (mmHg)

  dbp_mmhg:
    data_type: float
    required: false
    range: {min: 30, max: 150}
    aliases: [DBP, dbp, diastolic_bp]
    description: Diastolic blood pressure (mmHg)

  sex_code:
    data_type: category
    required: false
    allowed_values: [1, 2, 9]
    aliases: [sex, gender_code]
    description: Sex code (1=male,2=female,9=unknown)

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st
import yaml


# ----------------------------
# Finding model (for UI table)
# ----------------------------
@dataclass
class Finding:
    severity: str   # "ERROR" | "WARN" | "INFO"
    rule_id: str
    field: str
    message: str
    detected: Optional[Any] = None
    expected: Optional[Any] = None
    suggestion: Optional[str] = None


# ----------------------------
# Registry utilities
# ----------------------------
def load_registry(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        reg = yaml.safe_load(f)
    if not isinstance(reg, dict) or "variables" not in reg:
        raise ValueError("Registry YAML must have top-level key: variables")
    return reg


def build_alias_map(reg: Dict[str, Any]) -> Dict[str, str]:
    alias_map: Dict[str, str] = {}
    for canonical, meta in reg["variables"].items():
        alias_map[canonical] = canonical
        for a in (meta.get("aliases") or []):
            alias_map[str(a)] = canonical
    return alias_map


# ----------------------------
# Parsing helpers
# ----------------------------
def parse_datetime(v: Any) -> Tuple[Optional[datetime], Optional[str]]:
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None, None
    if isinstance(v, datetime):
        return v, None

    s = str(v).strip()
    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt), None
        except ValueError:
            continue
    return None, f"Unrecognized datetime format: {s}"


def parse_float(v: Any) -> Tuple[Optional[float], Optional[str]]:
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None, None
    try:
        return float(v), None
    except Exception:
        return None, f"Expected numeric value, got {v!r}"


def parse_string(v: Any) -> Tuple[Optional[str], Optional[str]]:
    if v is None:
        return None, None
    s = str(v)
    if s.strip() == "":
        return None, None
    return s, None


def parse_category(v: Any) -> Tuple[Optional[Any], Optional[str]]:
    # allow numeric categories
    s, err = parse_string(v)
    if err or s is None:
        return None, err
    if s.isdigit():
        return int(s), None
    return s, None


# ----------------------------
# Validation core
# ----------------------------
def normalize_keys(
    raw: Dict[str, Any],
    reg: Dict[str, Any],
    *,
    block_unknown: bool = True,
) -> Tuple[Dict[str, Any], List[Finding]]:
    findings: List[Finding] = []
    alias_map = build_alias_map(reg)
    out: Dict[str, Any] = {}

    for k, v in raw.items():
        if k in alias_map:
            canon = alias_map[k]
            if canon != k:
                findings.append(
                    Finding(
                        severity="INFO",
                        rule_id="VAR-NAME-ALIAS-001",
                        field=k,
                        message=f"Alias '{k}' mapped to canonical '{canon}'.",
                        detected=k,
                        expected=canon,
                        suggestion=f"Use '{canon}' going forward."
                    )
                )
            # collision if two different fields map to same canonical
            if canon in out and v not in (None, "", " "):
                if out[canon] not in (None, "", " ") and out[canon] != v:
                    findings.append(
                        Finding(
                            severity="WARN",
                            rule_id="VAR-NAME-COLLISION-001",
                            field=canon,
                            message=f"Multiple inputs map to '{canon}'. Keeping latest non-empty value.",
                            detected={"existing": out[canon], "new": v},
                            expected="Single source field",
                            suggestion="Remove duplicate aliases / columns."
                        )
                    )
            out[canon] = v
        else:
            sev = "ERROR" if block_unknown else "WARN"
            findings.append(
                Finding(
                    severity=sev,
                    rule_id="VAR-NAME-UNKNOWN-001",
                    field=k,
                    message=f"Unknown variable name '{k}' (not in registry).",
                    detected=k,
                    expected="Registered canonical name or alias",
                    suggestion="Fix the name or register this variable."
                )
            )
            if not block_unknown:
                out[k] = v

    return out, findings


def validate_record(
    rec: Dict[str, Any],
    reg: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Finding]]:
    findings: List[Finding] = []
    cleaned: Dict[str, Any] = {}
    vars_meta: Dict[str, Any] = reg["variables"]

    # Required checks
    for name, meta in vars_meta.items():
        if meta.get("required", False):
            if rec.get(name) in (None, "", " "):
                findings.append(
                    Finding(
                        severity="ERROR",
                        rule_id="REQ-001",
                        field=name,
                        message=f"Required field '{name}' is missing.",
                        detected=rec.get(name),
                        expected="Non-empty",
                        suggestion="Fill this field."
                    )
                )

    # Per-field type / allowed / range
    for name, meta in vars_meta.items():
        if name not in rec:
            continue

        raw_v = rec.get(name)
        dtype = meta.get("data_type")

        parsed = None
        err = None

        if dtype == "string":
            parsed, err = parse_string(raw_v)
        elif dtype == "datetime":
            parsed, err = parse_datetime(raw_v)
        elif dtype == "float":
            parsed, err = parse_float(raw_v)
        elif dtype == "category":
            parsed, err = parse_category(raw_v)
        else:
            parsed = raw_v
            findings.append(
                Finding(
                    severity="WARN",
                    rule_id="META-DTYPE-001",
                    field=name,
                    message=f"Unknown data_type '{dtype}' in registry; skipping type checks.",
                    detected=dtype,
                    expected="string|datetime|float|category",
                    suggestion="Fix registry metadata."
                )
            )

        if err:
            findings.append(
                Finding(
                    severity="ERROR",
                    rule_id="TYPE-001",
                    field=name,
                    message=f"Type/format error: {err}",
                    detected=raw_v,
                    expected=dtype,
                    suggestion="Correct the value format."
                )
            )
            cleaned[name] = raw_v
            continue

        cleaned[name] = parsed

        # Allowed values
        allowed = meta.get("allowed_values")
        if allowed is not None and parsed is not None:
            if parsed not in allowed:
                findings.append(
                    Finding(
                        severity="ERROR",
                        rule_id="CAT-001",
                        field=name,
                        message=f"Value not allowed for '{name}'.",
                        detected=parsed,
                        expected=allowed,
                        suggestion=f"Use one of: {allowed}"
                    )
                )

        # Range checks (warnings)
        r = meta.get("range")
        if r and parsed is not None and isinstance(parsed, (int, float)):
            mn, mx = r.get("min"), r.get("max")
            if mn is not None and parsed < mn:
                findings.append(
                    Finding(
                        severity="WARN",
                        rule_id="RANGE-LOW-001",
                        field=name,
                        message=f"Value below expected min for '{name}'.",
                        detected=parsed,
                        expected=f">= {mn}",
                        suggestion="Verify unit/entry."
                    )
                )
            if mx is not None and parsed > mx:
                findings.append(
                    Finding(
                        severity="WARN",
                        rule_id="RANGE-HIGH-001",
                        field=name,
                        message=f"Value above expected max for '{name}'.",
                        detected=parsed,
                        expected=f"<= {mx}",
                        suggestion="Verify unit/entry."
                    )
                )

    # Cross-field logic rules
    sbp = cleaned.get("sbp_mmhg")
    dbp = cleaned.get("dbp_mmhg")
    if sbp is not None and dbp is not None and isinstance(sbp, (int, float)) and isinstance(dbp, (int, float)):
        if sbp < dbp:
            findings.append(
                Finding(
                    severity="ERROR",
                    rule_id="LOGIC-BP-001",
                    field="sbp_mmhg, dbp_mmhg",
                    message="SBP must be >= DBP.",
                    detected={"sbp_mmhg": sbp, "dbp_mmhg": dbp},
                    expected="sbp_mmhg >= dbp_mmhg",
                    suggestion="Swap or correct values."
                )
            )

    adm = cleaned.get("admission_dt")
    dis = cleaned.get("discharge_dt")
    if isinstance(adm, datetime) and isinstance(dis, datetime) and dis < adm:
        findings.append(
            Finding(
                severity="ERROR",
                rule_id="LOGIC-DATE-001",
                field="admission_dt, discharge_dt",
                message="Discharge must be after admission.",
                detected={"admission_dt": str(adm), "discharge_dt": str(dis)},
                expected="discharge_dt >= admission_dt",
                suggestion="Fix encounter dates."
            )
        )

    return cleaned, findings


def counts(findings: List[Finding]) -> Dict[str, int]:
    out = {"ERROR": 0, "WARN": 0, "INFO": 0}
    for f in findings:
        out[f.severity] = out.get(f.severity, 0) + 1
    return out


# ----------------------------
# Streamlit UI (Results Window)
# ----------------------------
st.set_page_config(page_title="Data Integrity Check Window", layout="wide")
st.title("Data Integrity Check Results (Live During Data Entry)")

reg = load_registry("variable_registry.yaml")

st.sidebar.header("Policy")
block_unknown = st.sidebar.toggle("Block unknown variable names", value=True)

st.write("As you type, the integrity checks update automatically (variable naming + basic rules).")

# Example entry fields (use aliases to demonstrate mapping)
c1, c2, c3 = st.columns(3)

with c1:
    pt_id = st.text_input("pt_id (alias of patient_id)", value="")
    visit_id = st.text_input("visit_id (alias of encounter_id)", value="")
    sex = st.text_input("sex_code (allowed: 1, 2, 9)", value="")

with c2:
    admit_dt = st.text_input("admit_dt (YYYY-MM-DD HH:MM[:SS])", value="")
    dc_dt = st.text_input("dc_dt (optional)", value="")

with c3:
    SBP = st.text_input("SBP (alias of sbp_mmhg)", value="")
    DBP = st.text_input("DBP (alias of dbp_mmhg)", value="")

# Raw record (what the user entered)
raw = {
    "pt_id": pt_id,
    "visit_id": visit_id,
    "sex_code": sex,
    "admit_dt": admit_dt,
    "dc_dt": dc_dt,
    "SBP": SBP,
    "DBP": DBP,
    # Try adding an unknown variable to see the name-consistency check:
    # "weirdField": "123"
}

normalized, f_name = normalize_keys(raw, reg, block_unknown=block_unknown)
cleaned, f_rules = validate_record(normalized, reg)
findings = f_name + f_rules
cnt = counts(findings)

# Results banner
st.subheader("Results Window")
banner = f"Errors {cnt['ERROR']} | Warnings {cnt['WARN']} | Info {cnt['INFO']}"
if cnt["ERROR"] > 0:
    st.error(banner)
elif cnt["WARN"] > 0:
    st.warning(banner)
else:
    st.success(banner)

# Save button behavior example (block on errors)
can_save = (cnt["ERROR"] == 0)
save_clicked = st.button("Save Record", disabled=not can_save)
if save_clicked:
    st.success("Saved (example).")

# Show canonicalized record (what would be stored)
with st.expander("Canonicalized Record Preview", expanded=False):
    st.json({k: (str(v) if v is not None else None) for k, v in cleaned.items()})

# Findings table
if findings:
    # Build a display table without pandas dependency
    rows = []
    for f in findings:
        rows.append({
            "Severity": f.severity,
            "Rule": f.rule_id,
            "Field(s)": f.field,
            "Message": f.message,
            "Detected": f.detected,
            "Expected": f.expected,
            "Suggestion": f.suggestion,
        })

    tab1, tab2, tab3 = st.tabs(["❌ Errors", "⚠ Warnings", "ℹ Info"])

    def show(sev: str):
        subset = [r for r in rows if r["Severity"] == sev]
        if not subset:
            st.write("None.")
        else:
            st.dataframe(subset, use_container_width=True, hide_index=True)

    with tab1:
        show("ERROR")
    with tab2:
        show("WARN")
    with tab3:
        show("INFO")
else:
    st.write("No findings. ✅")
