# Mailin Redirect Manager

Streamlit app for bulk-updating Mailin domain redirects from a CSV.

It supports:

- Standard Mailin / SMTP domains
- Microsoft / Azure orders
- Automatic provider detection
- CSV upload
- One global redirect destination or per-row destinations
- Preview before writes
- Password-gated UI
- API token stored server-side in Streamlit Secrets or an environment variable
- CSV result export

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
  "forward_to": "example.com"
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
  "forward_to": "example.com"
}
```

All API requests use:

```text
Authorization: Bearer YOUR_API_TOKEN
```

## CSV format

### Simplest

```csv
domain
domain1.com
domain2.com
domain3.com
```

Enter one redirect target in the app.

### Per-row redirects

```csv
domain,forward_to
domain1.com,brand.com
domain2.com,otherbrand.com
```

### Force provider if needed

```csv
domain,forward_to,provider
domain1.com,brand.com,azure
domain2.com,brand.com,domain
domain3.com,brand.com,auto
```

Accepted provider aliases:

- `azure`, `microsoft`, `m365`, `ms365`, `outlook`
- `domain`, `smtp`, `general`, `standard`
- `auto`

## Deploy on Streamlit Community Cloud

1. Create a new GitHub repository.
2. Upload the contents of this ZIP to the repo.
3. Do **not** create or commit `.streamlit/secrets.toml`.
4. Go to Streamlit Community Cloud and deploy `app.py`.
5. Open the app's **Settings → Secrets**.
6. Add:

```toml
MAILIN_API_TOKEN = "PASTE_YOUR_MAILIN_BEARER_TOKEN_HERE"
APP_PASSWORD = "hyperkeMA"
```

7. Save and reboot the app if Streamlit asks.

The app will now use the Mailin token automatically. Users never need to paste the API token into the UI.

## Deploy elsewhere

Set these environment variables:

```text
MAILIN_API_TOKEN=...
APP_PASSWORD=hyperkeMA
```

Then run:

```bash
pip install -r requirements.txt
streamlit run app.py
```

For Railway / Render:

```bash
streamlit run app.py --server.address 0.0.0.0 --server.port $PORT
```

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

- requires exact domain matches
- resolves resource IDs before writes
- previews resolved rows before changes
- blocks malformed domains
- blocks conflicting duplicate rows
- skips unresolved rows
- requires explicit confirmation before updates
- retries transient `429` and common `5xx` failures
- outputs a full results CSV
