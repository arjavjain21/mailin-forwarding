import os
import re
import time
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests
import streamlit as st


API_BASE = "https://api.mailin.ai/api/v1/public"
DEFAULT_APP_PASSWORD = "hyperkeMA"

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)


st.set_page_config(
    page_title="Mailin Redirect Manager",
    page_icon="↗",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ---------- Styling ----------
st.markdown(
    """
    <style>
    .block-container {max-width: 1200px; padding-top: 2.2rem;}
    div[data-testid="stMetric"] {
        border: 1px solid rgba(128,128,128,.22);
        padding: 14px 16px;
        border-radius: 12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------- Secrets / auth ----------
def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        if name in st.secrets:
            value = st.secrets[name]
            return str(value) if value is not None else default
    except Exception:
        pass
    return os.getenv(name, default)


def check_app_password() -> bool:
    expected = get_secret("APP_PASSWORD", DEFAULT_APP_PASSWORD)

    if st.session_state.get("authenticated") is True:
        return True

    st.title("Mailin Redirect Manager")
    st.caption("Internal Hyperke utility")

    with st.form("login_form", clear_on_submit=False):
        password = st.text_input("Password", type="password", placeholder="Enter password")
        submitted = st.form_submit_button("Continue", type="primary", use_container_width=True)

    if submitted:
        if password == expected:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")

    return False


if not check_app_password():
    st.stop()


MAILIN_TOKEN = get_secret("MAILIN_API_TOKEN")
if not MAILIN_TOKEN:
    st.error(
        "MAILIN_API_TOKEN is not configured. Add it to Streamlit Secrets or as an environment variable."
    )
    st.code(
        'MAILIN_API_TOKEN = "your_mailin_bearer_token"\nAPP_PASSWORD = "hyperkeMA"',
        language="toml",
    )
    st.stop()


# ---------- Helpers ----------
def normalize_domain(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip().lower().rstrip(".")


def valid_domain(value: str) -> bool:
    return bool(DOMAIN_RE.fullmatch(value or ""))


def normalize_provider(value: Any) -> str:
    if value is None:
        return "auto"
    try:
        if pd.isna(value):
            return "auto"
    except Exception:
        pass

    value = str(value).strip().lower()
    if not value:
        return "auto"

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
    return aliases.get(value, "invalid")


def api_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {MAILIN_TOKEN}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Hyperke-Mailin-Redirect-Manager/1.0",
        }
    )
    return s


def response_message(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            if body.get("message"):
                return str(body["message"])
            return str(body)
        return str(body)
    except Exception:
        return (resp.text or f"HTTP {resp.status_code}")[:1000]


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params=None,
    payload=None,
    timeout: int = 30,
    retries: int = 3,
) -> requests.Response:
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = session.request(
                method,
                url,
                params=params,
                json=payload,
                timeout=timeout,
            )

            if resp.status_code not in {429, 500, 502, 503, 504}:
                return resp

            if attempt < retries:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = min(float(retry_after), 15)
                    except Exception:
                        delay = min(2 ** attempt, 8)
                else:
                    delay = min(2 ** attempt, 8)
                time.sleep(delay)
            else:
                return resp

        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= retries:
                raise
            time.sleep(min(2 ** attempt, 8))

    if last_exc:
        raise last_exc
    raise RuntimeError("Unexpected request failure.")


def exact_azure_match(
    session: requests.Session, domain: str
) -> Tuple[Optional[int], str]:
    resp = request_with_retry(
        session,
        "GET",
        f"{API_BASE}/azure-orders",
        params={"domain": domain, "per_page": 100},
    )

    if resp.status_code != 200:
        return None, f"Azure lookup failed: {resp.status_code} {response_message(resp)}"

    data = resp.json()
    rows = data.get("data", []) if isinstance(data, dict) else []

    exact = []
    for row in rows:
        row_domain = normalize_domain(
            row.get("domain")
            or row.get("domain_name")
            or row.get("name")
        )
        if row_domain == domain:
            exact.append(row)

    if len(exact) == 1:
        try:
            return int(exact[0]["id"]), ""
        except Exception:
            return None, "Azure order matched but did not contain a usable id."

    if len(exact) > 1:
        return None, "Multiple exact Azure order matches found."

    return None, ""


def exact_general_domain_match(
    session: requests.Session, domain: str
) -> Tuple[Optional[int], str]:
    resp = request_with_retry(
        session,
        "GET",
        f"{API_BASE}/domains",
        params={"name": domain, "per_page": 100},
    )

    if resp.status_code != 200:
        return None, f"Domain lookup failed: {resp.status_code} {response_message(resp)}"

    data = resp.json()
    rows = data.get("data", []) if isinstance(data, dict) else []

    exact = [row for row in rows if normalize_domain(row.get("name")) == domain]

    if len(exact) == 1:
        try:
            return int(exact[0]["id"]), ""
        except Exception:
            return None, "Domain matched but did not contain a usable id."

    if len(exact) > 1:
        return None, "Multiple exact general-domain matches found."

    return None, ""


def resolve_domain(
    session: requests.Session,
    domain: str,
    requested_provider: str,
) -> Dict[str, Any]:

    if requested_provider == "azure":
        order_id, error = exact_azure_match(session, domain)
        if error:
            return {
                "resolved_provider": "",
                "resource_id": None,
                "resolution": "ERROR",
                "detail": error,
            }
        if order_id is None:
            return {
                "resolved_provider": "",
                "resource_id": None,
                "resolution": "NOT FOUND",
                "detail": "No exact Azure/Microsoft order found.",
            }
        return {
            "resolved_provider": "azure",
            "resource_id": order_id,
            "resolution": "READY",
            "detail": "Azure/Microsoft order",
        }

    if requested_provider == "domain":
        domain_id, error = exact_general_domain_match(session, domain)
        if error:
            return {
                "resolved_provider": "",
                "resource_id": None,
                "resolution": "ERROR",
                "detail": error,
            }
        if domain_id is None:
            return {
                "resolved_provider": "",
                "resource_id": None,
                "resolution": "NOT FOUND",
                "detail": "No exact general Mailin domain found.",
            }
        return {
            "resolved_provider": "domain",
            "resource_id": domain_id,
            "resolution": "READY",
            "detail": "General Mailin domain",
        }

    # Auto mode: check Azure first, then standard Mailin domains.
    azure_id, azure_error = exact_azure_match(session, domain)
    if azure_error:
        return {
            "resolved_provider": "",
            "resource_id": None,
            "resolution": "ERROR",
            "detail": azure_error,
        }

    if azure_id is not None:
        return {
            "resolved_provider": "azure",
            "resource_id": azure_id,
            "resolution": "READY",
            "detail": "Auto-detected Azure/Microsoft",
        }

    domain_id, domain_error = exact_general_domain_match(session, domain)
    if domain_error:
        return {
            "resolved_provider": "",
            "resource_id": None,
            "resolution": "ERROR",
            "detail": domain_error,
        }

    if domain_id is not None:
        return {
            "resolved_provider": "domain",
            "resource_id": domain_id,
            "resolution": "READY",
            "detail": "Auto-detected general domain",
        }

    return {
        "resolved_provider": "",
        "resource_id": None,
        "resolution": "NOT FOUND",
        "detail": "No exact Azure or general Mailin domain found.",
    }


def update_redirect(
    session: requests.Session,
    provider: str,
    resource_id: int,
    forward_to: str,
) -> Dict[str, Any]:

    if provider == "azure":
        url = f"{API_BASE}/azure-orders/set-redirect-domain/{resource_id}"
        payload = {"forward_to": forward_to}
    else:
        url = f"{API_BASE}/domains/forward"
        payload = {
            "domain_id": int(resource_id),
            "forward_to": forward_to,
        }

    try:
        resp = request_with_retry(
            session,
            "POST",
            url,
            payload=payload,
        )
    except requests.RequestException as exc:
        return {
            "result": "FAILED",
            "http_status": "",
            "message": str(exc),
        }

    success = 200 <= resp.status_code < 300
    return {
        "result": "SUCCESS" if success else "FAILED",
        "http_status": resp.status_code,
        "message": response_message(resp),
    }


def detect_domain_column(columns) -> Optional[str]:
    aliases = ["domain", "domain_name", "domains", "website"]
    lower_map = {str(c).strip().lower(): c for c in columns}
    for alias in aliases:
        if alias in lower_map:
            return lower_map[alias]
    return None


# ---------- App ----------
top_left, top_right = st.columns([6, 1])
with top_left:
    st.title("Mailin Redirect Manager")
    st.caption("Bulk update domain redirects and Microsoft/Azure master domains from CSV.")
with top_right:
    if st.button("Log out", use_container_width=True):
        st.session_state.clear()
        st.rerun()


with st.expander("How routing works"):
    st.markdown(
        """
**Microsoft / Azure domains**
- Resolve the exact domain through `GET /azure-orders?domain=...`
- Update with `POST /azure-orders/set-redirect-domain/{order_id}`

**Other Mailin domains**
- Resolve through `GET /domains?name=...`
- Update with `POST /domains/forward`

`auto` checks Azure first, then the general domain inventory. Only exact domain matches are accepted.
        """
    )


with st.expander("CSV examples"):
    st.markdown("**One redirect target for every uploaded domain**")
    st.code(
        "domain\nalpha.com\nbeta.com\ngamma.com",
        language="csv",
    )
    st.markdown("**Different targets per row**")
    st.code(
        "domain,forward_to\nalpha.com,brand.com\nbeta.com,otherbrand.com",
        language="csv",
    )
    st.markdown("**Optional provider override**")
    st.code(
        "domain,forward_to,provider\n"
        "alpha.com,brand.com,azure\n"
        "beta.com,brand.com,domain\n"
        "gamma.com,brand.com,auto",
        language="csv",
    )


session = api_session()

with st.sidebar:
    st.header("Status")
    st.success("Mailin token loaded")
    st.caption("API token is read server-side from Streamlit Secrets/environment.")
    st.divider()
    if st.button("Test Mailin connection", use_container_width=True):
        with st.spinner("Testing..."):
            try:
                resp = request_with_retry(
                    session,
                    "GET",
                    f"{API_BASE}/domains",
                    params={"per_page": 1},
                )
                if resp.status_code == 200:
                    st.success("Connected")
                else:
                    st.error(f"{resp.status_code}: {response_message(resp)}")
            except Exception as exc:
                st.error(str(exc))


uploaded = st.file_uploader(
    "Upload domains CSV",
    type=["csv"],
    help="Required: a domain column. Optional: forward_to and provider.",
)

default_forward = normalize_domain(
    st.text_input(
        "Redirect all rows to",
        placeholder="example.com",
        help="Optional if your CSV already has a forward_to column.",
    )
)

if default_forward and not valid_domain(default_forward):
    st.error("Redirect target must be a bare domain such as example.com, not https://example.com/path.")


if uploaded is None:
    st.info("Upload a CSV to begin.")
    st.stop()


try:
    raw = pd.read_csv(uploaded, dtype=str).fillna("")
except Exception as exc:
    st.error(f"Could not read CSV: {exc}")
    st.stop()


domain_col = detect_domain_column(raw.columns)
if domain_col is None:
    st.error("No domain column found. Use a column named `domain`.")
    st.stop()


work = raw.copy()
work.columns = [str(c).strip().lower() for c in work.columns]

if "domain" not in work.columns:
    # Renames aliases such as domain_name / domains / website.
    original_lower = str(domain_col).strip().lower()
    work = work.rename(columns={original_lower: "domain"})

work["domain"] = work["domain"].map(normalize_domain)

if "forward_to" not in work.columns:
    work["forward_to"] = ""
work["forward_to"] = work["forward_to"].map(normalize_domain)

if default_forward:
    work.loc[work["forward_to"] == "", "forward_to"] = default_forward

if "provider" not in work.columns:
    work["provider"] = "auto"
work["provider"] = work["provider"].map(normalize_provider)

work["input_status"] = "VALID"
work["input_detail"] = ""

bad_domain = ~work["domain"].map(valid_domain)
work.loc[bad_domain, "input_status"] = "INVALID"
work.loc[bad_domain, "input_detail"] = "Invalid source domain."

bad_target = ~work["forward_to"].map(valid_domain)
work.loc[bad_target, "input_status"] = "INVALID"
work.loc[bad_target, "input_detail"] = work.loc[bad_target, "input_detail"].apply(
    lambda x: (x + " " if x else "") + "Missing or invalid redirect target."
)

bad_provider = work["provider"] == "invalid"
work.loc[bad_provider, "input_status"] = "INVALID"
work.loc[bad_provider, "input_detail"] = work.loc[bad_provider, "input_detail"].apply(
    lambda x: (x + " " if x else "") + "Provider must be auto, azure, or domain."
)

# Block conflicting duplicates but tolerate exact duplicate rows.
for domain, group in work.groupby("domain", dropna=False):
    if not domain:
        continue
    combos = set(zip(group["forward_to"], group["provider"]))
    if len(group) > 1 and len(combos) > 1:
        work.loc[group.index, "input_status"] = "INVALID"
        work.loc[group.index, "input_detail"] = work.loc[group.index, "input_detail"].apply(
            lambda x: (x + " " if x else "") + "Duplicate domain has conflicting instructions."
        )


st.subheader("1. Validate")
st.dataframe(
    work[
        ["domain", "forward_to", "provider", "input_status", "input_detail"]
    ],
    use_container_width=True,
    hide_index=True,
)

m1, m2, m3 = st.columns(3)
m1.metric("Rows", len(work))
m2.metric("Valid", int((work["input_status"] == "VALID").sum()))
m3.metric("Invalid", int((work["input_status"] != "VALID").sum()))


if st.button(
    "Resolve domains",
    type="primary",
    use_container_width=True,
    disabled=(work["input_status"] == "VALID").sum() == 0,
):
    resolved_rows = []
    cache = {}

    progress = st.progress(0, text="Resolving domains...")
    total = len(work)

    for position, (_, row) in enumerate(work.iterrows(), start=1):
        if row["input_status"] != "VALID":
            resolved_rows.append(
                {
                    "resolved_provider": "",
                    "resource_id": None,
                    "resolution": "SKIPPED",
                    "detail": row["input_detail"],
                }
            )
        else:
            key = (row["domain"], row["provider"])
            if key not in cache:
                cache[key] = resolve_domain(
                    session,
                    row["domain"],
                    row["provider"],
                )
            resolved_rows.append(cache[key])

        progress.progress(
            position / total,
            text=f"Resolving {position}/{total}",
        )

    progress.empty()

    resolved = work.reset_index(drop=True).copy()
    resolved_extra = pd.DataFrame(resolved_rows)
    for col in resolved_extra.columns:
        resolved[col] = resolved_extra[col]

    st.session_state["resolved"] = resolved
    # Clear stale result data whenever resolution is re-run.
    st.session_state.pop("results", None)


resolved = st.session_state.get("resolved")

if resolved is not None:
    st.subheader("2. Review")
    st.dataframe(
        resolved[
            [
                "domain",
                "forward_to",
                "provider",
                "resolved_provider",
                "resource_id",
                "resolution",
                "detail",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

    ready_count = int((resolved["resolution"] == "READY").sum())
    unresolved_count = len(resolved) - ready_count

    m1, m2 = st.columns(2)
    m1.metric("Ready", ready_count)
    m2.metric("Skipped / unresolved", unresolved_count)

    st.download_button(
        "Download resolution CSV",
        data=resolved.to_csv(index=False).encode("utf-8"),
        file_name="mailin_redirect_resolution.csv",
        mime="text/csv",
    )

    st.subheader("3. Apply")
    st.warning(
        "This changes live Mailin redirect settings. Only rows marked READY will be submitted."
    )

    confirmation = st.checkbox(
        f"I reviewed the table and approve updating {ready_count} domain(s)."
    )

    if st.button(
        "Apply redirect updates",
        type="primary",
        use_container_width=True,
        disabled=(not confirmation or ready_count == 0),
    ):
        ready = resolved[resolved["resolution"] == "READY"].copy()
        results = []

        progress = st.progress(0, text="Updating redirects...")
        total = len(ready)

        for position, (_, row) in enumerate(ready.iterrows(), start=1):
            outcome = update_redirect(
                session,
                row["resolved_provider"],
                int(row["resource_id"]),
                row["forward_to"],
            )

            results.append(
                {
                    "domain": row["domain"],
                    "forward_to": row["forward_to"],
                    "provider": row["resolved_provider"],
                    "resource_id": int(row["resource_id"]),
                    **outcome,
                }
            )

            progress.progress(
                position / total,
                text=f"Updating {position}/{total}",
            )

        progress.empty()
        st.session_state["results"] = pd.DataFrame(results)


results = st.session_state.get("results")

if results is not None:
    st.subheader("4. Results")
    st.dataframe(results, use_container_width=True, hide_index=True)

    success_count = int((results["result"] == "SUCCESS").sum())
    failed_count = len(results) - success_count

    m1, m2 = st.columns(2)
    m1.metric("Successful", success_count)
    m2.metric("Failed", failed_count)

    st.download_button(
        "Download results CSV",
        data=results.to_csv(index=False).encode("utf-8"),
        file_name="mailin_redirect_results.csv",
        mime="text/csv",
    )
