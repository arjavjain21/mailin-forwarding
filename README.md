# Mailin Redirect Manager

Streamlit app for bulk-updating Mailin domain redirects from a CSV.

It supports:

- Uploading inbox/email lists or domain lists
- Automatic inbox → domain extraction
- Deduplication to one Mailin operation per unique domain
- Standard Mailin / SMTP domains
- Microsoft / Azure orders
- Automatic provider detection
- One global redirect destination or per-row destinations
- Query-string-only redirects, paths, full URLs, and bare domains
- Preview before writes
- Password-gated UI
- API token stored server-side in Streamlit Secrets or an environment variable
- CSV result export

## Inbox deduplication

You can upload an inbox list directly, for example:

```csv
email
alice@alpha.com
bob@alpha.com
sales@alpha.com
john@beta.com
ops@beta.com
```

The app extracts the part after `@` and reduces that list to:

```text
alpha.com  → 3 inboxes
beta.com   → 2 inboxes
```

Only the **2 unique domains** are resolved and updated through Mailin. This means a 500-inbox upload containing only 5 unique domains results in roughly 5 domain resolution/update operations rather than processing 500 inbox rows individually.

Accepted source-column names include `email`, `email_address`, `inbox`, `sender`, `mailbox`, `account`, `domain`, and similar aliases.

The UI displays uploaded rows, valid inboxes/rows, unique domains, duplicates removed, and the number of inboxes represented by each domain.

## Common Hyperke redirect suffix

The app keeps this visible in the UI as a copy/paste reference:

```text
?utm_source=direct_outreach&utm_medium=email&utm_campaign=hyperke
```

The redirect field accepts and preserves values such as:

```text
?utm_source=direct_outreach&utm_medium=email&utm_campaign=hyperke
/landing?utm_source=direct_outreach&utm_medium=email&utm_campaign=hyperke
example.com/landing?utm_source=direct_outreach
https://example.com/landing?utm_source=direct_outreach
example.com
```

Only surrounding whitespace is removed. Path and query-string casing are not changed.

## Important security note

This repository is designed to be safe to publish publicly **as long as you do not commit `.streamlit/secrets.toml`**.

The Mailin token must be stored in Streamlit Cloud Secrets or your hosting provider's environment variables.

The default UI password is:

```text
hyperkeMA
```

Because that default is present in public source code, it only prevents casual access. For a real access control boundary, set `APP_PASSWORD` privately in Streamlit Secrets to a different value.

## Mailin endpoints used

### Azure / Microsoft

Lookup:

```text
GET /api/v1/public/azure-orders?domain=<domain>
```

Set master/redirect domain:

```text
POST /api/v1/public/azure-orders/set-redirect-domain/{order_id}
```

Body:

```json
{
  "forward_to": "?utm_source=direct_outreach&utm_medium=email&utm_campaign=hyperke"
}
```

### Standard Mailin domains

Lookup:

```text
GET /api/v1/public/domains?name=<domain>
```

Update redirect:

```text
POST /api/v1/public/domains/forward
```

Body:

```json
{
  "domain_id": 123,
  "forward_to": "/landing?utm_source=direct_outreach&utm_medium=email&utm_campaign=hyperke"
}
```

All API requests use:

```text
Authorization: Bearer YOUR_API_TOKEN
```

## CSV formats

### Inbox list

```csv
email
alice@domain1.com
bob@domain1.com
sales@domain2.com
```

Enter one redirect target in the app. The email addresses will be collapsed to `domain1.com` and `domain2.com` before Mailin is queried.

### Domain list

```csv
domain
domain1.com
domain2.com
domain3.com
```

### Per-row redirects

```csv
domain,forward_to
domain1.com,"?utm_source=direct_outreach&utm_medium=email&utm_campaign=hyperke"
domain2.com,"/landing?utm_source=direct_outreach&utm_medium=email&utm_campaign=hyperke"
```

### Force provider if needed

```csv
domain,forward_to,provider
domain1.com,"?utm_source=direct_outreach&utm_medium=email&utm_campaign=hyperke",azure
domain2.com,"/landing?utm_source=direct_outreach&utm_medium=email&utm_campaign=hyperke",domain
domain3.com,"https://example.com/offer?utm_source=direct_outreach",auto
```

Accepted provider aliases:

- `azure`, `microsoft`, `m365`, `ms365`, `outlook`
- `domain`, `smtp`, `general`, `standard`
- `auto`

## Deploy on Streamlit Community Cloud

1. Deploy this repository and select `app.py`.
2. Open the app's **Settings → Secrets**.
3. Add:

```toml
MAILIN_API_TOKEN = "PASTE_YOUR_MAILIN_BEARER_TOKEN_HERE"
APP_PASSWORD = "hyperkeMA"
```

4. Save and reboot the app if Streamlit asks.

The app uses the Mailin token automatically. Users never need to paste it into the UI.

## Local development

Create `.streamlit/secrets.toml`:

```toml
MAILIN_API_TOKEN = "your_token"
APP_PASSWORD = "hyperkeMA"
```

Then:

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Safety behavior

The application deliberately:

- extracts domains before Mailin lookups
- deduplicates to one operation per unique domain
- requires exact source-domain matches
- resolves resource IDs before writes
- previews resolved rows before changes
- blocks malformed source domains
- allows path/query/full-URL redirect targets
- blocks conflicting instructions for the same domain
- skips unresolved rows
- requires explicit confirmation before updates
- retries transient `429` and common `5xx` failures
- outputs a full results CSV
