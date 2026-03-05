# Request Flows Between Systems

This document captures end-to-end request flows for common operations.

## 1) Retrieve project costs

```mermaid
sequenceDiagram
    participant Client
    participant Gateway
    participant CostsUsage
    participant Keystone
    participant CloudKitty

    Client->>Gateway: GET /api/projects/{id}/costs
    Gateway->>CostsUsage: Forward same request
    CostsUsage->>Keystone: Authenticate / discover endpoint
    CostsUsage->>CloudKitty: Query summarized costs
    CloudKitty-->>CostsUsage: Cost payload
    CostsUsage-->>Gateway: JSON response
    Gateway-->>Client: JSON response
```

## 2) Generate invoice file (with optional email)

```mermaid
sequenceDiagram
    participant Client
    participant Gateway
    participant DocGen as DocumentGenerator
    participant Brevo

    Client->>Gateway: GET /api/projects/{id}/invoices/{invoice_id}/file
    Gateway->>DocGen: Forward request
    DocGen->>DocGen: Build PDF (or HTML preview)
    alt send_email=true
        DocGen->>Brevo: Send email with invoice attachment
        Brevo-->>DocGen: Delivery API response
    end
    DocGen-->>Gateway: File/preview response
    Gateway-->>Client: File/preview response
```

## 3) Create Revolut checkout order

```mermaid
sequenceDiagram
    participant Client
    participant Gateway
    participant Checkout
    participant DocGen as DocumentGenerator
    participant Revolut

    Client->>Gateway: POST /api/projects/{id}/payments/revolut/order
    Gateway->>Checkout: Forward request
    Checkout->>DocGen: Fetch invoice details via REST
    DocGen-->>Checkout: Invoice payload
    Checkout->>Revolut: Create order
    Revolut-->>Checkout: Hosted checkout/order data
    Checkout-->>Gateway: JSON response
    Gateway-->>Client: JSON response
```

## 4) Record/lookup payments

```mermaid
sequenceDiagram
    participant Client
    participant Gateway
    participant Payments
    participant OpenSearch

    Client->>Gateway: POST/GET /api/projects/{id}/payments...
    Gateway->>Payments: Forward request
    Payments->>OpenSearch: Index/search payment docs
    OpenSearch-->>Payments: Query/index response
    Payments-->>Gateway: JSON response
    Gateway-->>Client: JSON response
```

## Operational Notes

- Gateway remains stateless and forwards headers/body with response passthrough.
- Downstream failures are surfaced as gateway upstream errors (`502` for connectivity issues).
- Service-level health checks are integral to startup validation in the microservice setup.
