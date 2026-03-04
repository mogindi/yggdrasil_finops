# yggdrasil_finops

Simple Python service/UI for reading CloudKitty project costs from OpenStack.

## What this provides

- `GET /api/projects/<project_id>/costs`
  - Returns **aggregate project cost for the requested/default time range**.
  - Optionally returns a **time-series** for graphing.
- `GET /api/projects/<project_id>/costs/last-month`
  - Returns the same payload format as `/costs`.
  - Uses the previous calendar month window in UTC (`YYYY-MM-01T00:00:00Z` to month end `23:59:59Z`).
- `GET /api/projects/<project_id>/costs/<YYYY-MM>`
  - Returns the same payload format as `/costs`.
  - Uses the exact calendar month specified in UTC.
- `GET /api/projects/<project_id>/costs/monthly`
  - Returns one data point per month for all past months, excluding the current month (UTC).
  - Optimized for graphing with a single CloudKitty grouped query (`resolution=month` in response).
- `GET /api/projects/<project_id>/costs/monthly/graph`
  - Returns an HTML page with an inline SVG chart showing monthly cost usage over time.
- Web UI at `/` to query a project and render a line chart.

- `POST /api/projects/<project_id>/invoices`
  - Creates an invoice for a customer, scoped to a single project.
- `POST /api/projects/<project_id>/receipts`
  - Creates a receipt after payment and updates invoice status (`open` → `partially_paid`/`paid`).
- `GET /api/projects/<project_id>/invoices/<invoice_id>/file`
  - Generates a PDF invoice (optional `logo_path`), can return inline PDF, downloadable attachment, or HTML preview (`view=html`).
  - Optional `send_email=true` sends the generated PDF using Brevo API.
- `GET /api/projects/<project_id>/receipts/<receipt_id>/file`
  - Generates a PDF receipt with the same preview/download/email options.
- Script to preconfigure CloudKitty hashmap pricing defaults.
- Revolut Business order creation for invoice checkout links.

## Prerequisites

- Python 3.11+
- Access to your OpenStack control plane
- Admin (or equivalent) credentials sourced in your environment
- CloudKitty service endpoint discoverable in Keystone catalog as service type `rating`

## Install

No external Python packages are required.

## Required environment variables

The services print a startup summary showing each relevant variable, whether it came from the environment or a default, and then run early dependency checks.

### OpenStack / CloudKitty (required for `app.py` and `costs_usage_app.py`)

- **Required (must be set):**
  - `OS_AUTH_URL`
  - `OS_USERNAME`
  - `OS_PASSWORD`
  - One of: `OS_PROJECT_ID` or `OS_PROJECT_NAME`
- **Optional with defaults (do not need to be set unless you want override behavior):**
  - `OS_USER_DOMAIN_NAME` (default: `Default`)
  - `OS_PROJECT_DOMAIN_NAME` (default: `Default`)
  - `OS_INTERFACE` (default: `public`)
  - `OS_REGION_NAME` (no default; used only for endpoint matching)
  - `CLOUDKITTY_ENDPOINT` (no default; auto-discovered from Keystone catalog if not set)
  - `CLOUDKITTY_CURRENCY` (default: `DKK`)
  - `OS_VERIFY` (default: `true`)

**Startup validation performed:**
- Keystone/CloudKitty auth is attempted immediately.

### Payments service (`payments_app.py`)

- **Optional with defaults:**
  - `OPENSEARCH_URL` (default: `http://localhost:9200`)
  - `OS_VERIFY` (default: `true`)

**Startup validation performed:**
- `OPENSEARCH_URL` is validated as a proper URL.
- A reachability check is performed against OpenSearch (`GET /`).

### Checkout service (`checkout_app.py`)

- **Required (must be set):**
  - `REVOLUT_API_KEY`
- **Optional with defaults:**
  - `DOCUMENT_GENERATOR_SERVICE_URL` (default: `http://document_generator:8080`)
  - `REVOLUT_BUSINESS_API_URL` (default: `https://sandbox-merchant.revolut.com`)
  - `REVOLUT_ORDERS_PATH` (default: `/api/orders`)
  - `OS_VERIFY` (default: `true`)

**Startup validation performed:**
- `DOCUMENT_GENERATOR_SERVICE_URL` format + `/healthz` reachability check.
- `REVOLUT_BUSINESS_API_URL` format check.
- `REVOLUT_API_KEY` presence check.

### Gateway (`gateway_service.py`)

- **Optional with defaults:**
  - `COSTS_SERVICE_URL` (default: `http://costs_usage:8080`)
  - `DOCUMENT_GENERATOR_SERVICE_URL` (default: `http://document_generator:8080`)
  - `CHECKOUT_SERVICE_URL` (default: `http://checkout:8080`)
  - `PAYMENTS_SERVICE_URL` (default: `http://payments:8080`)

**Startup validation performed:**
- Each upstream service URL is validated and checked for `/healthz` reachability before gateway startup.

### Document generator (`document_generator_app.py`)

- **Optional with defaults:**
  - `BREVO_API_URL` (default: `https://api.brevo.com/v3/smtp/email`)
  - `BREVO_SENDER_EMAIL` (default: `noreply@example.com`)
  - `BREVO_SENDER_NAME` (default: `Yggdrasil FinOps`)
- **Conditionally required:**
  - `BREVO_API_KEY` is only required when calling invoice/receipt file endpoints with `send_email=true` (or CLI `--send-email`).

## Microservice deployment (Docker Compose)

The project now supports a microservice topology while keeping a **unified endpoint** and **unified CLI**:

- `gateway` (public entrypoint on `:8082`)
- `costs_usage` (CloudKitty costs + UI/static assets + graphing)
- `document_generator` (invoices/receipts + PDF/email generation)
- `checkout` (Revolut Business order creation endpoint)
- `payments` (OpenSearch payment ledger endpoints)

All API clients (including `yggdrasil_finops.py`) should continue to use a single base URL, e.g. `http://localhost:8082`.

Start everything:

```bash
docker compose up --build
```

This Compose file is configured for **host networking** instead of a bridge network:

- Runtime uses `network_mode: host` for every service.
- Build uses `build.network: host` for every service to avoid bridge-network dependency during `pip install` image build steps.

Services communicate via `localhost` on dedicated ports:

- `gateway`: `8082`
- `costs_usage`: `8083`
- `document_generator`: `8084`
- `checkout`: `8085`
- `payments`: `8086`

Then use the same API/CLI commands as before (pointing to gateway `:8082`).

Each backend now runs a dedicated application (`costs_usage_app.py`, `document_generator_app.py`, `checkout_app.py`, `payments_app.py`) with isolated HTTP handlers.

`checkout` now fetches invoice data from `document_generator` over REST (`/api/projects/<id>/invoices/<invoice_id>`) before creating Revolut orders, instead of reading in-process shared state.

`document_generator` no longer depends on OpenStack credentials; it only needs billing/PDF/Brevo configuration.

`checkout` no longer depends on OpenStack credentials; it only needs Revolut config plus `DOCUMENT_GENERATOR_SERVICE_URL` to fetch invoice data.

`payments` now operates directly on OpenSearch and does not require OpenStack credentials unless you choose to add external tenant-validation in front of it.



## Run the app

```bash
source admin-openrc.sh
python app.py
```

For very verbose logging (including every CloudKitty/Keystone API request), run:

```bash
python app.py --debug
```

Then browse to `http://localhost:8082`.

## CLI wrapper

You can call the API with a simple CLI wrapper:

```bash
python yggdrasil_finops.py --help
```

Examples using intuitive `setup/create/list/show` command patterns:

```bash
# project setup
python yggdrasil_finops.py project setup --project-id proj_123

# payment create/list/show
python yggdrasil_finops.py payment create \
  --project-id proj_123 \
  --event-id evt_001 \
  --invoice-id inv_001 \
  --amount 100.00 \
  --paid-at 2026-01-10T12:00:00Z
python yggdrasil_finops.py payment list --project-id proj_123
python yggdrasil_finops.py payment show --project-id proj_123 --event-id evt_001

# invoice create/list/show
python yggdrasil_finops.py invoice create \
  --project-id proj_123 \
  --amount-due 100.00 \
  --customer-name "Acme Corp" \
  --customer-email billing@acme.example \
  --due-at 2026-02-01T00:00:00Z
python yggdrasil_finops.py invoice list --project-id proj_123
python yggdrasil_finops.py invoice show --project-id proj_123 --invoice-id inv_001

# receipt create/list
python yggdrasil_finops.py receipt create \
  --project-id proj_123 \
  --invoice-id inv_001 \
  --amount-paid 100.00 \
  --paid-at 2026-01-11T14:30:00Z
python yggdrasil_finops.py receipt list --project-id proj_123
```

Set `YGGDRASIL_FINOPS_API_URL` (or pass `--api-url`) if your API is not on `http://localhost:8082`.

End-to-end customer lifecycle examples (CLI):

```bash
# 1) Customer onboarding (initialize project + first invoice)
python yggdrasil_finops.py project setup --project-id cust_acme_001
python yggdrasil_finops.py invoice create \
  --project-id cust_acme_001 \
  --amount-due 250.00 \
  --customer-name "Acme Corp" \
  --customer-email billing@acme.example \
  --due-at 2026-02-01T00:00:00Z \
  --description "Initial onboarding month"

# 2) Normal monthly usage + graphing
python yggdrasil_finops.py cost month \
  --project-id cust_acme_001 \
  --month 2026-01 \
  --resolution day \
  --include-series
python yggdrasil_finops.py cost monthly --project-id cust_acme_001
python yggdrasil_finops.py cost monthly-graph --project-id cust_acme_001 > monthly_graph.html

# 3) Customer off-boarding (final invoice + final receipt, then verify history)
python yggdrasil_finops.py invoice create \
  --project-id cust_acme_001 \
  --amount-due 89.50 \
  --customer-name "Acme Corp" \
  --customer-email billing@acme.example \
  --due-at 2026-03-01T00:00:00Z \
  --description "Final off-boarding charges"
python yggdrasil_finops.py receipt create \
  --project-id cust_acme_001 \
  --invoice-id <FINAL_INVOICE_ID> \
  --amount-paid 89.50 \
  --paid-at 2026-03-02T11:00:00Z
python yggdrasil_finops.py invoice list --project-id cust_acme_001
python yggdrasil_finops.py receipt list --project-id cust_acme_001
```


PDF commands in CLI:

```bash
# generate + download invoice PDF
python yggdrasil_finops.py invoice file   --project-id proj_123   --invoice-id inv_001   --logo-path ./logo.jpg   --download-path ./inv_001.pdf

# show invoice PDF as HTML (for browser rendering)
python yggdrasil_finops.py invoice file --project-id proj_123 --invoice-id inv_001 --html

# send receipt PDF using Brevo
python yggdrasil_finops.py receipt file --project-id proj_123 --receipt-id rcpt_001 --send-email
```

## API examples

### 1) Aggregate + time series

```bash
curl "http://localhost:8082/api/projects/<PROJECT_ID>/costs?resolution=day&include_series=true"
```

### 2) Aggregate only

```bash
curl "http://localhost:8082/api/projects/<PROJECT_ID>/costs?include_series=false"
```

### 3) Explicit date range

```bash
curl "http://localhost:8082/api/projects/<PROJECT_ID>/costs?start=2026-01-01T00:00:00Z&end=2026-01-31T23:59:59Z&resolution=day"
```

### 4) Previous calendar month

```bash
curl "http://localhost:8082/api/projects/<PROJECT_ID>/costs/last-month?resolution=day&include_series=true"
```

### 5) Specific calendar month

```bash
curl "http://localhost:8082/api/projects/<PROJECT_ID>/costs/2025-01?resolution=day&include_series=true"
```

### 6) Monthly history (excluding current month)

```bash
curl "http://localhost:8082/api/projects/<PROJECT_ID>/costs/monthly"
```

### 7) Monthly history graph (HTML page)

```bash
curl "http://localhost:8082/api/projects/<PROJECT_ID>/costs/monthly/graph"
```


### 8) Create an invoice

```bash
curl -X POST "http://localhost:8082/api/projects/<PROJECT_ID>/invoices" \
  -H "Content-Type: application/json" \
  -d '{
    "amount_due": 125.50,
    "currency": "DKK",
    "customer_name": "Acme Corp",
    "customer_email": "billing@acme.test",
    "description": "Monthly cloud bill"
  }'
```

### 9) Create a receipt for a paid invoice

```bash
curl -X POST "http://localhost:8082/api/projects/<PROJECT_ID>/receipts" \
  -H "Content-Type: application/json" \
  -d '{
    "invoice_id": "inv_xxx",
    "amount_paid": 125.50,
    "currency": "DKK",
    "payment_method": "wire_transfer",
    "payment_reference": "wire-2026-0001"
  }'
```


## End-to-end example for `proj_123`

This is a practical lifecycle walkthrough for a single tenant project (`proj_123`): onboarding, operating for a couple of months, then closing the account.

### 1) Onboarding month (`2026-01`)

1. Create OpenSearch payment index and mappings during onboarding:

```bash
curl -X POST "http://localhost:8082/api/projects/proj_123/payments/setup"
```

2. Record an initial customer deposit/payment event:

```bash
curl -X PUT "http://localhost:8082/api/projects/proj_123/payments/events/evt_onboard_001" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "proj_123",
    "invoice_id": "inv_2026_01",
    "amount": 150.00,
    "currency": "DKK",
    "direction": "inbound",
    "status": "succeeded",
    "paid_at": "2026-01-03T09:30:00Z",
    "metadata": {"note": "onboarding credit"}
  }'
```

3. Store the starting balance view:

```bash
curl -X PUT "http://localhost:8082/api/projects/proj_123/payments/balance" \
  -H "Content-Type: application/json" \
  -d '{"currency":"DKK","paid_total":150.00,"refunded_total":0.00,"net_paid":150.00}'
```

4. Check first-month cost trend:

```bash
curl "http://localhost:8082/api/projects/proj_123/costs/2026-01?resolution=day&include_series=true"
```

### 2) Active usage over a couple of months (`2026-02`, `2026-03`)

1. Reuse the same project payments index (no monthly setup needed):

```bash
curl -X POST "http://localhost:8082/api/projects/proj_123/payments/setup"
```

2. Bulk ingest recurring payments for February:

```bash
curl -X POST "http://localhost:8082/api/projects/proj_123/payments/events/bulk" \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {
        "event_id": "evt_feb_001",
        "project_id": "proj_123",
        "invoice_id": "inv_2026_02",
        "amount": 90.00,
        "currency": "DKK",
        "direction": "inbound",
        "status": "succeeded",
        "paid_at": "2026-02-05T08:00:00Z"
      },
      {
        "event_id": "evt_mar_001",
        "project_id": "proj_123",
        "invoice_id": "inv_2026_03",
        "amount": 95.00,
        "currency": "DKK",
        "direction": "inbound",
        "status": "succeeded",
        "paid_at": "2026-03-05T08:00:00Z"
      }
    ]
  }'
```

3. Inspect monthly cloud spend history:

```bash
curl "http://localhost:8082/api/projects/proj_123/costs/monthly"
```

4. Compare payments received:

```bash
curl "http://localhost:8082/api/projects/proj_123/payments/total-paid"
```

### 3) Closing account (deletion workflow)

There is no hard-delete endpoint for payment documents in this service. A practical close-out flow is:

1. Final billing check for the last full month:

```bash
curl "http://localhost:8082/api/projects/proj_123/costs/last-month?resolution=day&include_series=true"
```

2. Add a final refund/adjustment event if needed:

```bash
curl -X PUT "http://localhost:8082/api/projects/proj_123/payments/events/evt_close_001" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "proj_123",
    "invoice_id": "inv_close_2026_03",
    "amount": 25.00,
    "currency": "DKK",
    "direction": "outbound",
    "status": "succeeded",
    "paid_at": "2026-03-28T15:00:00Z",
    "metadata": {"reason": "account closure refund"}
  }'
```

3. Set the project balance to settled/closed (for example net `0`):

```bash
curl -X PUT "http://localhost:8082/api/projects/proj_123/payments/balance" \
  -H "Content-Type: application/json" \
  -d '{"currency":"DKK","paid_total":335.00,"refunded_total":335.00,"net_paid":0.00}'
```

4. Verify closed balance:

```bash
curl "http://localhost:8082/api/projects/proj_123/payments/balance"
```


## Billing module isolation (microservice-ready)

Invoice and receipt logic is intentionally separated into `billing_service.py` (domain + storage adapter) so it can be moved behind a separate process later with minimal API-layer changes.

Current design:
- `BillingService`: business rules for invoice and receipt creation.
- `InMemoryBillingRepository`: storage adapter used by the monolith now, replaceable with DB/OpenSearch adapter later.
- HTTP layer (`app.py`) only maps requests to service DTOs and responses.

## OpenSearch payment storage per project

The service now exposes OpenSearch-backed payment endpoints under `/api/projects/<PROJECT_ID>/payments` and maps the template field to `project_id`.

- `POST /api/projects/<PROJECT_ID>/payments/setup`
  - Creates `payments_template`, a payments index, and `project-balances`.
  - Creates a long-lived per-project index `payments-project-<project_id>` for one-time onboarding.
  - Template compatibility note: `metadata` is stored as a non-indexed object (`enabled: false`) so setup works on older OpenSearch clusters that do not support `flattened` mappings.
- `PUT /api/projects/<PROJECT_ID>/payments/events/<EVENT_ID>`
  - Upserts a payment event using event id as document `_id` (idempotent).
- `POST /api/projects/<PROJECT_ID>/payments/events/bulk`
  - Bulk ingests events (`{"events": [...]}`).
- `GET /api/projects/<PROJECT_ID>/payments/events/<EVENT_ID>`
  - Fetches a payment event by id.
- `GET /api/projects/<PROJECT_ID>/payments`
  - Lists payments for the project sorted by `paid_at desc`.
- `GET /api/projects/<PROJECT_ID>/payments/invoices/<INVOICE_ID>`
  - Lists successful invoice payments for the project.
- `GET /api/projects/<PROJECT_ID>/payments/total-paid`
  - Returns OpenSearch sum aggregation for successful inbound payments.
- `PUT /api/projects/<PROJECT_ID>/payments/balance`
  - Upserts a current balance doc (`paid_total`, `refunded_total`, `net_paid`, `currency`).
- `GET /api/projects/<PROJECT_ID>/payments/balance`
  - Reads current balance doc.
- `GET /api/projects/<PROJECT_ID>/payments/mapping`
- `GET /api/projects/<PROJECT_ID>/payments/settings`
- `POST /api/projects/<PROJECT_ID>/payments/refresh`

Example setup call:

```bash
curl -X POST "http://localhost:8082/api/projects/proj_123/payments/setup"
```

If setup still fails, verify `OPENSEARCH_URL` points to the expected cluster and check your OpenSearch version supports composable index templates (`/_index_template`).

## Configure default CloudKitty costs

If CloudKitty hashmap rating is not preconfigured, run (add `--debug` for step-by-step API call logging):

```bash
source admin-openrc.sh
python scripts/configure_cloudkitty_defaults.py --debug
```

The script supports configurable pricing through `--pricing-config`:

```bash
python scripts/configure_cloudkitty_defaults.py --pricing-config ./cloudkitty-pricing.json
```

`cloudkitty-pricing.json` format:

```json
{
  "instance": [
    {"value": "m1.tiny", "cost": 0.0125},
    {"value": "m1.small", "cost": 0.025},
    {"value": "m1.medium", "cost": 0.06},
    {"value": "m1.large", "cost": 0.10},
    {"value": "m1.xlarge", "cost": 0.20},
    {"value": "m2.tiny", "cost": 0.0125}
  ],
  "volume": [
    {"value": "standard", "cost": 0.08},
    {"value": "ssd", "cost": 0.15}
  ],
  "network.bw.out": []
}
```

Default placeholders are intentionally set slightly cheaper than Azure list pricing for compute/storage, and networking egress is explicitly left empty (`[]`).

Before applying rates, the script also checks OpenStack flavors (`openstack flavor list`) and warns if any existing flavor name matches an `instance` mapping value that will be rated.

## Notes

- CloudKitty API versions vary by deployment; this implementation tries common summary endpoints in order.
- Endpoint and TLS behavior can be overridden via env vars as noted above.


### 10) Create a Revolut Business order for an invoice

```bash
curl -X POST "http://localhost:8082/api/projects/<PROJECT_ID>/payments/revolut/order" \
  -H "Content-Type: application/json" \
  -d '{
    "invoice_id": "inv_xxx",
    "success_url": "https://your-portal.example/payments/success"
  }'
```

The endpoint reads the invoice, calculates the remaining amount (`amount_due - amount_paid`), and creates a Revolut order that can be used for checkout.
