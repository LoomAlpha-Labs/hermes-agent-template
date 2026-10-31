# Historical official X bookmark reader (inactive)

The fixed hourly worker now uses Bird cookies regardless of a stale
`X_BOOKMARK_PROVIDER=oauth` setting. That setting needs no separate change or
restart. This document describes the retained, inactive OAuth module and
ledger; its activation instructions below are historical and do not activate
OAuth in the current worker. Do not delete the persistent ledger.

The earlier OAuth candidate replaced the worker's Bird subprocess with an opt-in reader.
The hourly worker and research schedule, 100-post window, existing snapshot
format, overlap checks and checkpoint/delivery logic are unchanged. A missing
OAuth configuration or failed/partial response produces a failed snapshot;
it never falls back to cookies, skips a gap, or treats failure as no new posts.

## Approved economics and activation

The approved additional bookmark allowance is at most $5 per UTC calendar month,
existing credits only, no auto-topups. This code does not buy credits or change
provider billing settings. The local SQLite ledger pessimistically reserves the
entire request cost before sending it and keeps reservations after all errors,
including crashes. In the final120 seconds of a UTC month, each request is
reserved against both that month and the following month atomically, protecting
against requests crossing the billing boundary. Neither reservation is refunded.
It does not trust provider daily deduplication or release
unused reservation based on incomplete response billing information.

A full bookmark request reserves 100 post resources, even when fewer return.
It requests no author expansions. A persistent author-ID cache supplies actual
names; only missing author IDs are looked up, with $0.010 reserved per requested
user before lookup. No fake handles or partial snapshot are emitted if author
lookup fails or its reservation cannot fit. Cached handles represent the last
successful lookup and can become stale if users rename themselves.

At verified owned-read post pricing of $0.001 a bookmark call reserves $0.10.
100 initially unknown authors cost at most $1 extra; each additional previously
unknown author costs $0.01. With $5, 40 hourly full-window calls fit after that
initial author enrichment. This does NOT sustain hourly operation for a month. The owner explicitly chose
"Keep hourly; stop when $5 is reached." Preserve the hourly schedule and let the
budget stop collection; do not shrink the100-post window or switch to daily.
Daily 100-post reads over31 days plus100 initially unknown authors would reserve
$4.10, excluding further new authors and commissioning costs. A daily schedule
has NOT been implemented or authorized here: the existing wiki snapshot gate
rejects snapshots older than3900s and would need a compatible policy first.

At standard $0.005/post each bookmark call reserves $0.50. Reservations are
upper bounds, not measured provider charges. Cadence and cap are now approved;
private provisioning, price verification and live commissioning remain required.
No schedule change is included.

Provider prices must be reverified before activation. This reservation policy
cannot protect against unannounced provider pricing changes, other applications,
or direct theft of the credential. Provider-side spend controls remain separate.

## Private provisioning

Use user-consented OAuth2 Authorization Code + PKCE with precisely
`bookmark.read tweet.read users.read offline.access`. The copied `x_oauth.py`
contains the loopback receiver, PKCE generation and exchange helpers. Bootstrap
must be produced by that flow and include the verified numeric `user_id`.
It is not an app-only bearer token. Never paste tokens into chat or Git.

Railway variables for the fixed worker:

- `X_BOOKMARK_PROVIDER=oauth` (opt-in; absence blocks collection)
- `X_BOOKMARK_USER_ID`: verified consenting owner numeric ID
- `X_OAUTH_CLIENT_ID`: authorized app client ID
- `X_OAUTH_CLIENT_SECRET`: optional; omit for the public client used in the
  successful authorization flow. Confidential clients require their secret.
- `X_BOOKMARK_OAUTH_BOOTSTRAP`: private JSON token record with `user_id`,
  `client_id`, `access_token`, `refresh_token`, `scope`, `expires_at` (Unix seconds)
- `X_BOOKMARK_MONTHLY_BUDGET_USD=5` (values above5 rejected)
- `X_BOOKMARK_POST_READ_USD=0.001` only after owned pricing verification;
  otherwise default `0.005`
- `X_BOOKMARK_OWNED_READS_VERIFIED=1` required for discounted rate

Keep existing `X_BOOKMARK_REVIEW_ENABLED` and review job ID unchanged. Remove
revoked `X_BIRD_AUTH_TOKEN`/`X_BIRD_CT0` variables. On first start bootstrap writes
`/data/.hermes/state/x-bookmark-oauth/tokens.json` privately (0600); subsequent
starts use the rotated persistent token and ignore the old bootstrap. After
successful commissioning remove the bootstrap variable from Railway. If a token
is revoked, reauthorization requires deliberate replacement of the persistent
token file; changing bootstrap does not silently overwrite a refreshed token.

The directory also holds the refresh lock, author cache, and durable budget.sqlite3.
Commissioning `/users/me` cost $0.01 this UTC month outside the worker: provision
that charge exactly once before enabling, using `reserve` with cost10000 (micro
USD) and the correct month. Keep a provisioning receipt to avoid double seeding;
this is an already incurred charge and must not be omitted from the $5 cap. Preserve
both tokens and budget across deployments. Do not delete/reset the ledger to
unblock collection. Refresh is serialized across processes and new refresh
credentials are atomically persisted before continuing. No automatic API retry.

Startup removes OAuth credentials before starting the server/agent and excludes
them from JEV. Worker Git/signature subprocesses also receive scrubbed environments.
These are environment boundaries, not OS-user isolation: processes sharing the
same user and volume may still access private files. Existing runtime sandbox
controls must continue protecting the private directory.

## Verification status

Unit tests exercise concurrent cap reservations, failure retention, monthly
rollover, pricing guards, normal/partial/malformed response normalization, token
refresh persistence, malformed refresh rejection, secret environment stripping,
and existing checkpoint/delivery behavior. No live OAuth exchange, paid API call,
image build or deployment has been performed. Source base is repository commit
4252f37; it has not been proven to equal the currently deployed Railway image.
