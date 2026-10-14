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
SOURCE_COLUMN_ALIASES = [
    "email",
    "email_address",
    "email address",
    "inbox",
    "inbox_email",
    "sender",
    "sender_email",
    "mailbox",
    "account",
    "domain",
    "domain_name",
    "domains",
    "website",
]

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


def extract_domain(value: Any) -> str:
    """Extract a domain from either an inbox/email address or a domain-like value."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""

    raw = str(value).strip()
    if not raw:
        return ""

    # Common CSV formats sometimes wrap addresses like "Name <user@domain.com>".
    angle_match = re.search(r"<\s*[^<>\s@]+@([^<>\s@]+)\s*>", raw)
    if angle_match:
        raw = angle_match.group(1)
    elif "@" in raw:
        raw = raw.rsplit("@", 1)[1]

    raw = raw.strip().lower()

    # Be forgiving if a URL was supplied instead of a domain.
    raw = re.sub(r"^[a-z][a-z0-9+.-]*://", "", raw)
    raw = raw.split("/", 1)[0]
    raw = raw.split("?", 1)[0]
    raw = raw.split("#", 1)[0]
    raw = raw.split(":", 1)[0]
    return raw.rstrip(".")


def valid_domain(value: str) -> bool:
    return bool(DOMAIN_RE.fullmatch(value or ""))


def norm_redirect(value: Any) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value).strip()


def valid_redirect(value: str) -> bool:
    return bool(value) and len(value) <= 2048 and not any(c in value for c in ("\r", "\n", "\x00"))


def norm_provider(value: Any) -> str:
    v = "auto" if value is None else str(value).strip().lower()
    if not v:
        v = "auto"
    aliases = {
        "auto": "auto",
        "azure": "azure",
        "microsoft": "azure",
        "m365": "azure",
        "ms365": "azure",
        "outlook": "azure",
        "domain": "domain",
        "smtp": "domain",
        "general": "domain",
        "standard": "domain",
    }
    return aliases.get(v, "invalid")


def detect_source_column(columns) -> Optional[str]:
    normalized = {str(c).strip().lower(): c for c in columns}
    for alias in SOURCE_COLUMN_ALIASES:
        if alias in normalized:
            return normalized[alias]
    return None


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Hyperke-Mailin-Redirect-Manager/3.0",
        }
    )
    return s


def req(s: requests.Session, method: str, url: str, *, params=None, payload=None) -> requests.Response:
    for attempt in range(4):
        try:
            r = s.request(method, url, params=params, json=payload, timeout=30)
            if r.status_code not in {429, 500, 502, 503, 504} or attempt == 3:
                return r
            retry_after = r.headers.get("Retry-After")
            try:
                delay = min(float(retry_after), 15) if retry_after else min(2**attempt, 8)
            except ValueError:
                delay = min(2**attempt, 8)
            time.sleep(delay)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(min(2**attempt, 8))
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
    matches = [
        x
        for x in rows_from(r)
        if norm_domain(x.get("domain") or x.get("domain_name") or x.get("name")) == domain
    ]
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
    matches = [
        x
        for x in rows_from(r)
        if norm_domain(x.get("name") or x.get("domain")) == domain
    ]
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
            return {
                "resolved_provider": "azure",
                "resource_id": rid,
                "resolution": "READY",
                "detail": "Azure/Microsoft order",
            }
        if provider == "azure":
            return {
                "resolved_provider": "",
                "resource_id": None,
                "resolution": "NOT FOUND",
                "detail": "No exact Azure/Microsoft order found.",
            }

    if provider in {"auto", "domain"}:
        rid, err = find_domain(s, domain)
        if err:
            return {"resolved_provider": "", "resource_id": None, "resolution": "ERROR", "detail": err}
        if rid is not None:
            return {
                "resolved_provider": "domain",
                "resource_id": rid,
                "resolution": "READY",
                "detail": "General Mailin domain",
            }

    return {
        "resolved_provider": "",
        "resource_id": None,
        "resolution": "NOT FOUND",
        "detail": "No exact Mailin match found.",
    }


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
    st.caption("Upload inboxes or domains. Inboxes are automatically collapsed to unique domains before any Mailin API calls.")
with right:
    if st.button("Log out", use_container_width=True):
        st.session_state.clear()
        st.rerun()

with st.expander("How it works"):
    st.markdown(
        """
- Upload a CSV containing **email addresses/inboxes or domains**.
- `person@alpha.com`, `sales@alpha.com`, and `ops@alpha.com` are automatically reduced to one `alpha.com` job.
- **Microsoft / Azure:** exact domain lookup via `/azure-orders`, then `POST /azure-orders/set-redirect-domain/{order_id}`.
- **Standard / SMTP:** exact domain lookup via `/domains`, then `POST /domains/forward`.
- `auto` checks Azure first, then standard Mailin domains.
- Redirect values can be domains, full URLs, paths, or query-string-only suffixes.
"""
    )

s = session()
with st.sidebar:
    st.success("Mailin token loaded")
    if st.button("Test Mailin connection", use_container_width=True):
        r = req(s, "GET", f"{API_BASE}/domains", params={"per_page": 1})
        st.success("Connected") if r.status_code == 200 else st.error(f"{r.status_code}: {message(r)}")

uploaded = st.file_uploader(
    "Upload inboxes or domains CSV",
    type=["csv"],
    help="Accepted source columns include email, email_address, inbox, sender, mailbox, account, or domain.",
)

default_target = norm_redirect(
    st.text_input(
        "Redirect all unique domains to",
        placeholder=COMMON_SUFFIX,
        help="Accepts query strings, paths, full URLs, or domains. Leave blank when CSV contains forward_to.",
    )
)
st.caption("Common Hyperke redirect suffix, kept here for copy/paste reference:")
st.code(COMMON_SUFFIX, language="text")

if default_target and not valid_redirect(default_target):
    st.error(
        "Invalid redirect target. It may be a query string, path, full URL, or domain, but cannot contain line breaks or exceed 2048 characters."
    )

if uploaded is None:
    st.info("Upload a CSV to begin.")
    st.stop()

try:
    raw = pd.read_csv(uploaded, dtype=str).fillna("")
except Exception as exc:
    st.error(f"Could not read CSV: {exc}")
    st.stop()

original_row_count = len(raw)
source_col = detect_source_column(raw.columns)
if source_col is None:
    st.error(
        "Could not find an inbox/domain column. Use a column such as `email`, `email_address`, `inbox`, `sender`, `mailbox`, `account`, or `domain`."
    )
    st.stop()

# Normalize headers only after identifying the original source column.
raw.columns = [str(c).strip().lower() for c in raw.columns]
source_col = str(source_col).strip().lower()

rows = raw.copy()
rows["source_value"] = rows[source_col].astype(str).str.strip()
rows["domain"] = rows["source_value"].map(extract_domain)

if "forward_to" not in rows.columns:
    rows["forward_to"] = ""
rows["forward_to"] = rows["forward_to"].map(norm_redirect)
if default_target:
    rows.loc[rows["forward_to"] == "", "forward_to"] = default_target

if "provider" not in rows.columns:
    rows["provider"] = "auto"
rows["provider"] = rows["provider"].map(norm_provider)

rows["row_status"] = "VALID"
rows["row_detail"] = ""

bad_domain = ~rows["domain"].map(valid_domain)
rows.loc[bad_domain, "row_status"] = "INVALID"
rows.loc[bad_domain, "row_detail"] = "Could not extract a valid domain from this value."

bad_target = ~rows["forward_to"].map(valid_redirect)
rows.loc[bad_target, "row_status"] = "INVALID"
rows.loc[bad_target, "row_detail"] = rows.loc[bad_target, "row_detail"].apply(
    lambda x: (x + " " if x else "") + "Missing or malformed redirect target."
)

bad_provider = rows["provider"] == "invalid"
rows.loc[bad_provider, "row_status"] = "INVALID"
rows.loc[bad_provider, "row_detail"] = rows.loc[bad_provider, "row_detail"].apply(
    lambda x: (x + " " if x else "") + "Provider must be auto, azure, or domain."
)

valid_rows = rows[rows["row_status"] == "VALID"].copy()
invalid_rows = rows[rows["row_status"] != "VALID"].copy()

# Detect contradictory instructions for the same extracted domain.
conflicting_domains = set()
for domain, group in valid_rows.groupby("domain"):
    instructions = set(zip(group["forward_to"], group["provider"]))
    if len(instructions) > 1:
        conflicting_domains.add(domain)

if conflicting_domains:
    conflict_mask = valid_rows["domain"].isin(conflicting_domains)
    conflicts = valid_rows[conflict_mask].copy()
    conflicts["row_status"] = "INVALID"
    conflicts["row_detail"] = "Same domain has conflicting forward_to/provider instructions."
    invalid_rows = pd.concat([invalid_rows, conflicts], ignore_index=True)
    valid_rows = valid_rows[~conflict_mask].copy()

# The key optimization: one row per unique domain before any API resolution.
if not valid_rows.empty:
    unique_work = (
        valid_rows.groupby(["domain", "forward_to", "provider"], as_index=False)
        .agg(
            inbox_count=("source_value", "size"),
            example_inboxes=("source_value", lambda x: ", ".join(list(dict.fromkeys(x))[:3])),
        )
        .sort_values("domain")
        .reset_index(drop=True)
    )
else:
    unique_work = pd.DataFrame(
        columns=["domain", "forward_to", "provider", "inbox_count", "example_inboxes"]
    )

unique_domain_count = len(unique_work)
deduped_count = max(len(valid_rows) - unique_domain_count, 0)

st.subheader("1. Extract & deduplicate")
a, b, c, d = st.columns(4)
a.metric("Uploaded rows", original_row_count)
b.metric("Valid inboxes/rows", len(valid_rows))
c.metric("Unique domains", unique_domain_count)
d.metric("Duplicates removed", deduped_count)

if unique_domain_count:
    st.dataframe(
        unique_work[["domain", "inbox_count", "example_inboxes", "forward_to", "provider"]],
        use_container_width=True,
        hide_index=True,
    )
    if original_row_count > unique_domain_count:
        st.success(
            f"Optimization: {original_row_count:,} uploaded rows collapse to {unique_domain_count:,} unique domain operation(s)."
        )

if not invalid_rows.empty:
    with st.expander(f"Invalid/conflicting rows ({len(invalid_rows)})"):
        st.dataframe(
            invalid_rows[["source_value", "domain", "forward_to", "provider", "row_detail"]],
            use_container_width=True,
            hide_index=True,
        )

if unique_work.empty:
    st.error("No valid unique domains are available to process.")
    st.stop()

if st.button("Resolve unique domains", type="primary", use_container_width=True):
    out = []
    bar = st.progress(0, text="Resolving unique domains...")
    total = len(unique_work)

    for i, (_, row) in enumerate(unique_work.iterrows(), 1):
        result = resolve(s, row.domain, row.provider)
        out.append(result)
        bar.progress(i / total, text=f"Resolving domain {i}/{total}")

    bar.empty()
    resolved = unique_work.copy()
    extra = pd.DataFrame(out)
    for col in extra.columns:
        resolved[col] = extra[col]
    st.session_state.resolved = resolved
    st.session_state.pop("results", None)

resolved = st.session_state.get("resolved")
if resolved is not None:
    st.subheader("2. Review unique-domain operations")
    cols = [
        "domain",
        "inbox_count",
        "forward_to",
        "provider",
        "resolved_provider",
        "resource_id",
        "resolution",
        "detail",
    ]
    st.dataframe(resolved[cols], use_container_width=True, hide_index=True)

    ready_count = int((resolved.resolution == "READY").sum())
    represented_inboxes = int(resolved.loc[resolved.resolution == "READY", "inbox_count"].sum()) if ready_count else 0
    a, b, c = st.columns(3)
    a.metric("Unique domains ready", ready_count)
    b.metric("Inboxes represented", represented_inboxes)
    c.metric("Skipped / unresolved", len(resolved) - ready_count)

    st.download_button(
        "Download resolution CSV",
        resolved.to_csv(index=False).encode(),
        "mailin_redirect_resolution.csv",
        "text/csv",
    )

    st.subheader("3. Apply")
    st.warning(
        "This changes live Mailin redirect settings. Each READY domain is submitted exactly once, regardless of how many inboxes used that domain."
    )
    approved = st.checkbox(
        f"I reviewed the table and approve updating {ready_count} unique domain(s)."
    )

    if st.button(
        "Apply redirect updates",
        type="primary",
        use_container_width=True,
        disabled=not approved or ready_count == 0,
    ):
        ready = resolved[resolved.resolution == "READY"].copy()
        result_rows = []
        bar = st.progress(0, text="Updating unique domains...")
        total = len(ready)

        for i, (_, row) in enumerate(ready.iterrows(), 1):
            result = apply_redirect(
                s,
                row.resolved_provider,
                int(row.resource_id),
                row.forward_to,
            )
            result_rows.append(
                {
                    "domain": row.domain,
                    "inbox_count": int(row.inbox_count),
                    "forward_to": row.forward_to,
                    "provider": row.resolved_provider,
                    "resource_id": int(row.resource_id),
                    **result,
                }
            )
            bar.progress(i / total, text=f"Updating domain {i}/{total}")

        bar.empty()
        st.session_state.results = pd.DataFrame(result_rows)

results = st.session_state.get("results")
if results is not None:
    st.subheader("4. Results")
    st.dataframe(results, use_container_width=True, hide_index=True)

    successes = int((results.result == "SUCCESS").sum())
    represented = int(results.loc[results.result == "SUCCESS", "inbox_count"].sum()) if successes else 0
    a, b, c = st.columns(3)
    a.metric("Domains updated", successes)
    b.metric("Inboxes covered", represented)
    c.metric("Failed domains", len(results) - successes)

    st.download_button(
        "Download results CSV",
        results.to_csv(index=False).encode(),
        "mailin_redirect_results.csv",
        "text/csv",
    )
