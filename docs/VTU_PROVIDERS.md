# VTU provider gateway (BT DataPlug)

Provider-agnostic purchase pipeline for airtime, data, cable TV and electricity, shared by the **mobile app API**,
the **website** and the **developer API**. Code: `vtu_app/gateway/`. Adapters: **Nellobytes** (full) and **Gongoz**
(conservative, see below). Adding another provider is described further down.

## How it works

```
view (mobile / web / developer API)
  1. engine.check_available(request)        refuse BEFORE any money moves if no provider can serve it
  2. TransactionService.process_debit(...)   atomic wallet debit + Pending Transaction (+ optional Idempotency-Key)
  3. engine.dispatch(transaction, request)
        for each eligible provider, best first:  PurchaseAttempt row -> adapter.purchase()
            ACCEPTED   -> stop, transaction stays Pending until the provider confirms it
            AMBIGUOUS  -> stop, stays Pending, NO failover, NO refund   (timeout / dropped / unknown reply)
            REJECTED   -> next provider only if the reason is provider-side (failover-safe)
        every attempt REJECTED -> ledger.settle(FAILED): wallet refunded exactly once
Later:  provider callback / history screen / `reconcile_vtu_orders` -> engine.reconcile_transaction -> ledger.settle
```

| Module | Responsibility |
|---|---|
| `adapters/<name>.py` | The *only* place that knows a provider's URLs, params and status strings. |
| `base.py`, `types.py` | `ProviderAdapter` contract; normalised `PurchaseRequest`, `ProviderResult`, `Outcome`, `ErrorCategory`, `OrderStatus`, `redact()`. |
| `config.py`, `registry.py` | `.env` loading + validation; adapter registry. |
| `router.py`, `health.py` | Provider choice (pluggable policy) and the circuit breaker (`ProviderState`). |
| `ledger.py` | Final-state changes: Pending -> Successful / Refunded, audit events, customer notification. |
| `engine.py` | Orchestration, reconciliation, webhook handling. |
| `flows.py` | Request builders and customer lookup shared by all three entry points. |

### The failover rule (money safety)

The next provider is tried **only** when the previous one returned `REJECTED` with a provider-side reason (`auth`,
`provider_balance`, `maintenance`, `rate_limit`, `unsupported`): it answered and confirmed it created no order. A
timeout, dropped connection, unparseable body, an unrecognised status word, or any reply carrying an order id other
than ORDER_RECEIVED is `AMBIGUOUS`: the order may exist, so the engine stops, keeps the money reserved and reconciles by
asking that provider. A bad recipient/amount (`invalid_request`) is not retried elsewhere - every provider would refuse it.

### Transaction states

`Pending -> Successful` (terminal) | `Pending -> Refunded` (wallet credited once; `refunded_at` set). Every change writes a
`TransactionEvent` (inline on the Transaction admin page, next to the provider attempts). If a provider reports success
*after* we refunded, nothing changes automatically: a `CRITICAL` log and an `ANOMALY` event are recorded for a human.

**Behaviour change:** an order the supplier merely *received* (Nellobytes `ORDER_RECEIVED`) used to be marked Successful
immediately. It now stays **Pending** until the supplier confirms completion, because a received order can still be
cancelled later. The mobile app's receipt/response shapes are unchanged; history shows "Pending" for a short while.
Developer-API orders go `pending` -> `successful` (webhook `transaction.pending` then `transaction.successful`).

## Configure a provider (.env)

See `.env.example`. Pattern `VTU_<NAME>_<SETTING>`. The old `NELLOBYTE_USER_ID` / `NELLOBYTE_API_KEY` / `CK_*` names
still work, so the existing deployment needs no `.env` change. A provider enabled but missing a required setting is
**never routed** and is reported by `python manage.py check` (`vtu_app.W001`). Secrets exist only in the environment:
never in the database, admin, API responses or logs.

Check it: `python manage.py vtu_providers` (add `--test` for a read-only auth/balance probe that places no order).

## Priority and failover

Effective priority (lower first): admin override (Django admin -> *Provider states*) > `VTU_<NAME>_PRIORITY` > adapter
default. A provider is skipped when disabled in `.env`, disabled in admin, misconfigured, unsupported for the request, or
suspended. After `VTU_FAILURE_THRESHOLD` (5) consecutive health-affecting failures it is suspended for
`VTU_SUSPEND_MINUTES` (10), then tried again; one more failure re-suspends immediately, one success resets it. Other
policies (lowest cost, best success rate): subclass `router.RoutingPolicy` and set `VTU_ROUTING_POLICY=dotted.path.Class`.

## Pending orders and reconciliation

A purchase leaves Pending through any of: the provider callback (`/api/v1/vtu/webhook/<provider>/`, sent as `CallBackURL`
when `PUBLIC_BASE_URL` is set), the user opening their history screen (their 3 newest Pending orders are checked), or the
sweep **`python manage.py reconcile_vtu_orders`** (schedule every few minutes; `reconcile_api_transactions` also keeps
working). All of them end in `engine.reconcile_transaction`, which asks the provider holding the order and settles only on
a definite SUCCESSFUL / FAILED. `UNKNOWN` / pending / query errors never refund. Unsigned callbacks are only a "check now"
hint, never the truth.

A Pending order that no provider ever received (the server died between the debit and the call) is refunded after 10
minutes - but **only for orders created by this gateway**. Older Pending rows from before the upgrade have no attempt
history, so their real outcome is unknowable and they are never auto-refunded: review them in the admin
(*Transactions -> filter "Pending over 10 min"*, then *Check selected Pending orders with the provider* or *Refund*).

## Idempotency

Mobile clients may send `Idempotency-Key: <8-100 chars [A-Za-z0-9._:-]>` (or `idempotency_key` in the body). The same key
never debits twice or submits twice; a repeat returns the original reference (`idempotent_replay: true`). The developer
API already required its own key and keeps doing so. Requests without a key behave exactly as before.

## Add a new provider

1. **Read the provider's official API docs.** Do not guess endpoints or status codes.
2. Create `vtu_app/gateway/adapters/<name>.py` implementing `ProviderAdapter` (`supports`, `purchase`, `query_status`,
   `check_connectivity`, optionally `parse_webhook` / `verify_customer`). `purchase` must return REJECTED only if the
   provider *answered and said no*; anything doubtful (timeout, reset, unparseable, unknown status) MUST be AMBIGUOUS.
   `query_status` returns SUCCESSFUL only on an explicit success, FAILED only on an explicit failure, UNKNOWN if unsure.
   Use `adapters/nellobytes.py` as the model; never log request URLs or credentials.
3. Register it in `adapters/__init__.py`, add its variables to `.env` and `.env.example`.
4. Test it like `test_gateway.py` / `test_gongoz.py` (mocked HTTP, every rejection class, timeout -> ambiguous, status
   mapping, no secrets in results). `python manage.py test vtu_app developer_api`.
5. `python manage.py vtu_providers --test`, then enable it.

Plan ids are provider-specific codes: every `DataPlan` / `CablePlan` has a `source` and a provider only serves plans of its
own source (Nellobytes plans are never sent to Gongoz and vice versa).

## Gongoz (`adapters/gongoz.py`)

Built from the vendor's Postman docs and dashboard id tables. Those docs show **no response bodies, no status vocabulary,
no reference/idempotency field and no webhook format**, so the adapter never guesses them:

* **Enable:** `VTU_GONGOZ_ENABLED=true`, `VTU_GONGOZ_API_KEY=<token>` (never the sample token printed in the public docs).
  Off by default. Optional `VTU_GONGOZ_PRIORITY` (default 20, so it is the fallback behind Nellobytes).
* **Accepted** only when Gongoz answers 200/201 with an order `id`; stored as `<endpoint>:<id>` (e.g. `topup:58`).
  401/403/429/400/404 are confirmed rejections (400 mentioning balance/insufficient = *our* Gongoz float is empty; its text
  is never shown to customers). Timeouts, 5xx, a reply without an id, non-JSON: **ambiguous** (no failover, no refund).
* **Completion is opt-in.** Orders stay Pending until you list Gongoz's real status words: make one small real purchase,
  `GET /api/topup/<id>` (or data/billpayment/cablesub) and read the `Status` value, then set `VTU_GONGOZ_SUCCESS_STATUSES`
  / `VTU_GONGOZ_FAILED_STATUSES` (comma-separated, case-insensitive). An unlisted word is logged and left unresolved.
* **Timeouts cannot be reconciled automatically.** Gongoz can be queried only by *its* id. If the submit reply never
  arrived, check the order in Gongoz's dashboard and settle it from the Transaction admin.
* **Webhooks are refused** (format undocumented): leave "Webhook URL" empty on Gongoz's dashboard.
* **Airtime** (MTN, GLO, 9MOBILE, AIRTEL) and **electricity** (11 discos, mapped from this app's own disco codes) work with
  the built-in id tables. **Data and cable** need Gongoz's plan ids: import them (below).
* Customer lookup (`validateiuc/`, `validatemeter/`) is tried in priority order and falls through to the next provider when
  one cannot confirm; an unrecognised reply shape is never reported as a valid name.

### Making Gongoz the primary provider (what the mobile app sees)

The Play Store app only talks to this backend, so no app update is needed.

1. `.env`: `VTU_GONGOZ_ENABLED=true`, `VTU_GONGOZ_API_KEY=...`, `VTU_GONGOZ_PRIORITY=1`, `VTU_NELLOBYTES_PRIORITY=50`
   (Nellobytes stays as airtime/electricity fallback, or set `VTU_NELLOBYTES_ENABLED=false`).
2. `python manage.py gongoz_import_plans` (preview), then `... --apply --margin 10 --deactivate-nellobytes`: Gongoz's plans
   become the plans the app lists (new ones priced at Gongoz cost + margin; existing imported plans keep admin prices;
   re-run any time to refresh costs and retire dropped plans). Without `--deactivate-nellobytes` both catalogues show.
   `--exclude-types` / `--types` filter plan types (default skips TALKMORE voice plans).
3. `seed_plans` / `sync_plans` only touch Nellobytes-sourced plans, so they can no longer delete or hide imported ones.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "temporarily under maintenance" (503) | all providers suspended/disabled, or provider float empty. `vtu_providers`; *Provider states* admin page. |
| `INVALID_CREDENTIALS` in the admin attempt log | wrong/rotated key or IP not whitelisted. `vtu_providers --test`. |
| Orders stay Pending | callbacks not arriving (check `PUBLIC_BASE_URL`) or the sweep is not scheduled: run `reconcile_vtu_orders`. |
| `vtu_app.W001` in `manage.py check` | an enabled provider is missing a required env var. |
| A plan vanished from the app | the supplier answered `INVALID_DATAPLAN`: the plan was auto-disabled; re-enable it in admin once fixed. |
| Log line `VTU ANOMALY` | provider says delivered after we refunded: reconcile manually with the provider. |
