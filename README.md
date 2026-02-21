# yggdrasil_finops

Simple Python service/UI for reading CloudKitty project costs from OpenStack.

## What this provides

- `GET /api/projects/<project_id>/costs`
  - Returns **aggregate project cost now**.
  - Optionally returns a **time-series** for graphing.
- Web UI at `/` to query a project and render a line chart.
- Script to preconfigure CloudKitty hashmap pricing defaults.

## Prerequisites

- Python 3.11+
- Access to your OpenStack control plane
- Admin (or equivalent) credentials sourced in your environment
- CloudKitty service endpoint discoverable in Keystone catalog as service type `rating`

## Install

No external Python packages are required.

## Required environment variables

The service uses the standard OpenStack variables (from `openrc`):

- `OS_AUTH_URL` (for Keystone v3, e.g. `https://keystone:5000/v3`)
- `OS_USERNAME`
- `OS_PASSWORD`
- `OS_PROJECT_NAME` **or** `OS_PROJECT_ID`
- Optional:
  - `OS_USER_DOMAIN_NAME` (default: `Default`)
  - `OS_PROJECT_DOMAIN_NAME` (default: `Default`)
  - `OS_REGION_NAME`
  - `OS_INTERFACE` (default: `public`)
  - `CLOUDKITTY_ENDPOINT` (override endpoint discovery)
  - `CLOUDKITTY_CURRENCY` (default response currency: `USD`)
  - `OS_VERIFY=false` to disable TLS verification (not recommended)

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

## Configure default CloudKitty costs

If CloudKitty hashmap rating is not preconfigured, run (add `--debug` for step-by-step API call logging):

```bash
source admin-openrc.sh
python scripts/configure_cloudkitty_defaults.py --debug
```

Defaults created (if missing):

- `instance` service mappings: `small=0.03`, `medium=0.07`, `large=0.12`
- `volume` service mappings: `standard=0.10`, `ssd=0.18`
- `network.bw.out` service mappings: `default=0.02`

## Notes

- CloudKitty API versions vary by deployment; this implementation tries common summary endpoints in order.
- Endpoint and TLS behavior can be overridden via env vars as noted above.
