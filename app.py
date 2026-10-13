import os
import re
import time
from typing import Any, Optional

import pandas as pd
import requests
import streamlit as st

API_BASE = "https://api.mailin.ai/api/v1/public"
DEFAULT_PASSWORD = "hyperkeMA"
COMMON_SUFFIX = "?utm_source=direct_outreach&utm_medium=email&utm_campaign=hyperke"
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")

st.set_page_config(page_title="Mailin Redirect Manager", page_icon="↗", layout="wide")


def secret(name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.getenv(name, default)


def require_login() -> None:
    if st.session_state.get("authenticated"):
        return
    st.title("Mailin Redirect Manager")
    st.caption("Internal Hyperke utility")
    with st.form("login"):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Continue", type="primary", use_container_width=True)
    if submitted:
        if password == secret("APP_PASSWORD", DEFAULT_PASSWORD):
            st.session_state.authenticated = True
            st.rerun()
        st.error("Incorrect password.")
    st.stop()


require_login()
TOKEN = secret("MAILIN_API_TOKEN")
if not TOKEN:
    st.error("MAILIN_API_TOKEN is not configured in Streamlit Secrets or the environment.")
    st.code('MAILIN_API_TOKEN = "your_token"\nAPP_PASSWORD = "hyperkeMA"', language="toml")
    st.stop()


def norm_domain(value: Any) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value).strip().lower().rstrip(".")


def valid_domain(value: str) -> bool:
    return bool(DOMAIN_RE.fullmatch(value or ""))


def norm_redirect(value: Any) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value).strip()


def valid_redirect(value: str) -> bool:
    # Mailin can receive bare domains, full URLs, paths, or query-only suffixes.
    # Preserve the value exactly apart from surrounding whitespace.
    return bool(value) and len(value) <= 2048 and not any(c in value for c in ("\r", "\n", "\x00"))


def norm_provider(value: Any) -> str:
    v = "auto" if value is None else str(value).strip().lower()
    if not v:
        v = "auto"
    aliases = {
        "auto": "auto",
        "azure": "azure", "microsoft": "azure", "m365": "azure", "ms365": "azure", "outlook": "azure",
        "domain": "domain", "smtp": "domain", "general": "domain", "standard": "domain",
    }
    return aliases.get(v, "invalid")


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Hyperke-Mailin-Redirect-Manager/2.0",
    })
    return s


def req(s: requests.Session, method: str, url: str, *, params=None, payload=None) -> requests.Response:
    for attempt in range(4):
        try:
            r = s.request(method, url, params=params, json=payload, timeout=30)
            if r.status_code not in {429, 500, 502, 503, 504} or attempt == 3:
                return r
            retry_after = r.headers.get("Retry-After")
            try:
                delay = min(float(retry_after), 15) if retry_after else min(2 ** attempt, 8)
            except ValueError:
                delay = min(2 ** attempt, 8)
            time.sleep(delay)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("Request failed")


def message(r: requests.Response) -> str:
    try:
        body = r.json()
        if isinstance(body, dict) and body.get("message"):
            return str(body["message"])
        return str(body)
    except Exception:
        return (r.text or f"HTTP {r.status_code}")[:1000]


def rows_from(r: requests.Response) -> list:
    body = r.json()
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        data = body.get("data", [])
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
    return []


def find_azure(s: requests.Session, domain: str):
    r = req(s, "GET", f"{API_BASE}/azure-orders", params={"domain": domain, "per_page": 100})
    if r.status_code != 200:
        return None, f"Azure lookup failed ({r.status_code}): {message(r)}"
    matches = [x for x in rows_from(r) if norm_domain(x.get("domain") or x.get("domain_name") or x.get("name")) == domain]
    if len(matches) > 1:
        return None, "Multiple exact Azure order matches found."
    if not matches:
        return None, ""
    try:
        return int(matches[0]["id"]), ""
    except Exception:
        return None, "Azure order matched but has no usable id."


def find_domain(s: requests.Session, domain: str):
    r = req(s, "GET", f"{API_BASE}/domains", params={"name": domain, "per_page": 100})
    if r.status_code != 200:
        return None, f"Domain lookup failed ({r.status_code}): {message(r)}"
    matches = [x for x in rows_from(r) if norm_domain(x.get("name") or x.get("domain")) == domain]
    if len(matches) > 1:
        return None, "Multiple exact general-domain matches found."
    if not matches:
        return None, ""
    try:
        return int(matches[0]["id"]), ""
    except Exception:
        return None, "Domain matched but has no usable id."


def resolve(s: requests.Session, domain: str, provider: str) -> dict:
    if provider in {"auto", "azure"}:
        rid, err = find_azure(s, domain)
        if err:
            return {"resolved_provider": "", "resource_id": None, "resolution": "ERROR", "detail": err}
        if rid is not None:
            return {"resolved_provider": "azure", "resource_id": rid, "resolution": "READY", "detail": "Azure/Microsoft order"}
        if provider == "azure":
            return {"resolved_provider": "", "resource_id": None, "resolution": "NOT FOUND", "detail": "No exact Azure/Microsoft order found."}

    if provider in {"auto", "domain"}:
        rid, err = find_domain(s, domain)
        if err:
            return {"resolved_provider": "", "resource_id": None, "resolution": "ERROR", "detail": err}
        if rid is not None:
            return {"resolved_provider": "domain", "resource_id": rid, "resolution": "READY", "detail": "General Mailin domain"}

    return {"resolved_provider": "", "resource_id": None, "resolution": "NOT FOUND", "detail": "No exact Mailin match found."}


def apply_redirect(s: requests.Session, provider: str, rid: int, forward_to: str) -> dict:
    if provider == "azure":
        url = f"{API_BASE}/azure-orders/set-redirect-domain/{rid}"
        payload = {"forward_to": forward_to}
    else:
        url = f"{API_BASE}/domains/forward"
        payload = {"domain_id": int(rid), "forward_to": forward_to}
    try:
        r = req(s, "POST", url, payload=payload)
        return {
            "result": "SUCCESS" if 200 <= r.status_code < 300 else "FAILED",
            "http_status": r.status_code,
            "message": message(r),
        }
    except requests.RequestException as exc:
        return {"result": "FAILED", "http_status": "", "message": str(exc)}


left, right = st.columns([6, 1])
with left:
    st.title("Mailin Redirect Manager")
    st.caption("Bulk-update standard Mailin redirects and Microsoft/Azure master-domain redirects from CSV.")
with right:
    if st.button("Log out", use_container_width=True):
        st.session_state.clear()
        st.rerun()

with st.expander("How it works"):
    st.markdown("""
- **Microsoft / Azure:** exact domain lookup via `/azure-orders`, then `POST /azure-orders/set-redirect-domain/{order_id}`.
- **Standard / SMTP:** exact domain lookup via `/domains`, then `POST /domains/forward`.
- `auto` checks Azure first, then standard Mailin domains.
- Redirect values can be domains, full URLs, paths, or query-string-only suffixes.
""")

s = session()
with st.sidebar:
    st.success("Mailin token loaded")
    if st.button("Test Mailin connection", use_container_width=True):
        r = req(s, "GET", f"{API_BASE}/domains", params={"per_page": 1})
        st.success("Connected") if r.status_code == 200 else st.error(f"{r.status_code}: {message(r)}")

uploaded = st.file_uploader("Upload domains CSV", type=["csv"], help="Required: domain. Optional: forward_to and provider.")

default_target = norm_redirect(st.text_input(
    "Redirect all rows to",
    placeholder=COMMON_SUFFIX,
    help="Accepts query strings, paths, full URLs, or domains. Leave blank when CSV contains forward_to.",
))
st.caption("Common Hyperke redirect suffix, kept here for copy/paste reference:")
st.code(COMMON_SUFFIX, language="text")

if default_target and not valid_redirect(default_target):
    st.error("Invalid redirect target. It may be a query string, path, full URL, or domain, but cannot contain line breaks or exceed 2048 characters.")

if uploaded is None:
    st.info("Upload a CSV to begin.")
    st.stop()

try:
    df = pd.read_csv(uploaded, dtype=str).fillna("")
except Exception as exc:
    st.error(f"Could not read CSV: {exc}")
    st.stop()

df.columns = [str(c).strip().lower() for c in df.columns]
for alias in ("domain_name", "domains", "website"):
    if "domain" not in df.columns and alias in df.columns:
        df = df.rename(columns={alias: "domain"})
if "domain" not in df.columns:
    st.error("CSV must include a `domain` column.")
    st.stop()

work = df.copy()
work["domain"] = work["domain"].map(norm_domain)
if "forward_to" not in work.columns:
    work["forward_to"] = ""
work["forward_to"] = work["forward_to"].map(norm_redirect)
if default_target:
    work.loc[work["forward_to"] == "", "forward_to"] = default_target
if "provider" not in work.columns:
    work["provider"] = "auto"
work["provider"] = work["provider"].map(norm_provider)
work["input_status"] = "VALID"
work["input_detail"] = ""

bad = ~work["domain"].map(valid_domain)
work.loc[bad, ["input_status", "input_detail"]] = ["INVALID", "Invalid source domain."]
bad = ~work["forward_to"].map(valid_redirect)
work.loc[bad, "input_status"] = "INVALID"
work.loc[bad, "input_detail"] = work.loc[bad, "input_detail"].apply(lambda x: (x + " " if x else "") + "Missing or malformed redirect target.")
bad = work["provider"] == "invalid"
work.loc[bad, "input_status"] = "INVALID"
work.loc[bad, "input_detail"] = work.loc[bad, "input_detail"].apply(lambda x: (x + " " if x else "") + "Provider must be auto, azure, or domain.")

for domain, group in work.groupby("domain", dropna=False):
    if domain and len(set(zip(group["forward_to"], group["provider"]))) > 1:
        work.loc[group.index, "input_status"] = "INVALID"
        work.loc[group.index, "input_detail"] = "Duplicate domain has conflicting instructions."

st.subheader("1. Validate")
st.dataframe(work[["domain", "forward_to", "provider", "input_status", "input_detail"]], use_container_width=True, hide_index=True)
a, b, c = st.columns(3)
a.metric("Rows", len(work)); b.metric("Valid", int((work.input_status == "VALID").sum())); c.metric("Invalid", int((work.input_status != "VALID").sum()))

if st.button("Resolve domains", type="primary", use_container_width=True, disabled=not (work.input_status == "VALID").any()):
    out, cache = [], {}
    bar = st.progress(0, text="Resolving domains...")
    for i, (_, row) in enumerate(work.iterrows(), 1):
        if row.input_status != "VALID":
            out.append({"resolved_provider": "", "resource_id": None, "resolution": "SKIPPED", "detail": row.input_detail})
        else:
            key = (row.domain, row.provider)
            if key not in cache:
                cache[key] = resolve(s, row.domain, row.provider)
            out.append(cache[key])
        bar.progress(i / len(work), text=f"Resolving {i}/{len(work)}")
    bar.empty()
    resolved = work.reset_index(drop=True).copy()
    extra = pd.DataFrame(out)
    for col in extra.columns:
        resolved[col] = extra[col]
    st.session_state.resolved = resolved
    st.session_state.pop("results", None)

resolved = st.session_state.get("resolved")
if resolved is not None:
    st.subheader("2. Review")
    cols = ["domain", "forward_to", "provider", "resolved_provider", "resource_id", "resolution", "detail"]
    st.dataframe(resolved[cols], use_container_width=True, hide_index=True)
    ready_count = int((resolved.resolution == "READY").sum())
    a, b = st.columns(2)
    a.metric("Ready", ready_count); b.metric("Skipped / unresolved", len(resolved) - ready_count)
    st.download_button("Download resolution CSV", resolved.to_csv(index=False).encode(), "mailin_redirect_resolution.csv", "text/csv")

    st.subheader("3. Apply")
    st.warning("This changes live Mailin redirect settings. Only READY rows are submitted.")
    approved = st.checkbox(f"I reviewed the table and approve updating {ready_count} domain(s).")
    if st.button("Apply redirect updates", type="primary", use_container_width=True, disabled=not approved or ready_count == 0):
        ready = resolved[resolved.resolution == "READY"]
        rows = []
        bar = st.progress(0, text="Updating redirects...")
        for i, (_, row) in enumerate(ready.iterrows(), 1):
            result = apply_redirect(s, row.resolved_provider, int(row.resource_id), row.forward_to)
            rows.append({"domain": row.domain, "forward_to": row.forward_to, "provider": row.resolved_provider, "resource_id": int(row.resource_id), **result})
            bar.progress(i / len(ready), text=f"Updating {i}/{len(ready)}")
        bar.empty()
        st.session_state.results = pd.DataFrame(rows)

results = st.session_state.get("results")
if results is not None:
    st.subheader("4. Results")
    st.dataframe(results, use_container_width=True, hide_index=True)
    a, b = st.columns(2)
    successes = int((results.result == "SUCCESS").sum())
    a.metric("Successful", successes); b.metric("Failed", len(results) - successes)
    st.download_button("Download results CSV", results.to_csv(index=False).encode(), "mailin_redirect_results.csv", "text/csv")
