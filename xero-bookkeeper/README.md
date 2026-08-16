# xerobk

A local-first bookkeeping companion for Xero: reporting, invoice and bill
review, bank-statement coding, and an Australian month-end / BAS close
checklist.

It runs on one machine. There is no server to deploy, no account to create, and
**zero runtime dependencies** — the live API client is built on `urllib`, the UI
is served by `http.server`, and storage is `sqlite3`, all from the standard
library. `pip install -e .` pulls nothing.

```
xerobk status                 # headline position
xerobk ui                     # dashboard in your browser
xerobk reconcile bank.csv     # coding suggestions for a bank statement
xerobk post bank.csv          # write those decisions back to Xero
xerobk close                  # month-end / BAS checklist
xerobk audit                  # everything this tool has sent to Xero
```

---

## Quick start

```bash
cd xero-bookkeeper
pip install -e .

# Works immediately against the bundled synthetic demo org:
xerobk status --snapshot data/demo
xerobk close  --snapshot data/demo
xerobk reconcile data/demo/bank_lines.csv --snapshot data/demo -v
xerobk ui     --snapshot data/demo
```

No credentials are needed for any of the above. The demo data is invented and
is deliberately seeded with problems — a duplicate contact, a duplicate invoice
pair, an invoice whose GST does not recompute — so the checklist has something
to find.

## Connecting a real Xero organisation

The app is read-only until you say otherwise, and it never stores a client
secret.

1. Go to <https://developer.xero.com/app/manage> and create an app.
2. Choose the **Mobile or desktop application** type. This is the PKCE flow: it
   issues no client secret, which is the right choice for software that runs on
   an end user's machine and therefore cannot keep one.
3. Add the redirect URI `http://localhost:8720/callback`.
4. Export the client id and connect:

```bash
export XERO_CLIENT_ID=<your client id>
xerobk connect                 # read-only scopes
xerobk connect --allow-writes  # also request write scopes
```

Tokens are written to `~/.local/share/xerobk/token.json` with `0600`
permissions (`%LOCALAPPDATA%\xerobk` on Windows). Override the location with
`XEROBK_HOME`. Refresh tokens rotate on every use and are persisted before the
new access token is handed back, so an interrupted refresh cannot strand you.

Once connected, drop the `--snapshot` flag and the same commands run against
your live ledger.

### Read-only vs read/write

| | Read-only (default) | With `--allow-writes` |
|---|---|---|
| Scopes | `accounting.*.read` | adds `accounting.transactions`, `accounting.contacts` |
| Reports, ageing, close checklist | yes | yes |
| Bank coding suggestions | yes | yes |
| Recording decisions locally | yes | yes |
| Posting payments / transactions / invoices to Xero | no | yes |

Write scopes are opt-in per connection, so an accidental run can never mutate
the ledger.

## Writing to Xero

Posting needs a bank account to write against — Xero rejects payments and bank
transactions without one:

```bash
xerobk accounts              # list the chart of accounts
xerobk accounts --use 090    # choose the account the money moves through
```

Then:

```bash
xerobk post statement.csv --dry-run   # show what would be sent
xerobk post statement.csv             # send it
```

Two different writes come out of a reconciliation run, and the distinction
matters:

- an invoice **match** becomes a **Payment** against that invoice. This is what
  marks it paid. Recording a bank transaction instead would book the money but
  leave the invoice sitting in the aged report forever.
- a coded line becomes a **BankTransaction** (`SPEND` or `RECEIVE`) against the
  coding account, for money that is not settling a document.

Creating documents:

```bash
xerobk invoice --contact "Acme Pty Ltd" --description "Consulting" \
               --amount 1000 --account 200
xerobk invoice --file draft.json --bill        # a supplier bill
```

Invoices are always created as `DRAFT`. Approving one puts it in the ledger and
on the BAS, which should be a deliberate second step in Xero.

### What protects you

**Nothing posts twice.** Every bank line has a stable content-derived id, and
lines already posted are skipped — re-running after a partial failure resumes
rather than duplicating.

**Ambiguous matches are never posted.** If a payment could settle any of several
same-priced invoices, it is skipped for a human to decide. Marking the wrong
invoice paid is worse than leaving it.

**Payments never exceed the invoice balance.** A batch payment is split across
its invoices, each capped at what that invoice actually owes.

**Failures are per-line.** One rejected payment does not abandon the batch, and
Xero's own error text is recorded against the line that failed.

**Everything is logged before it is sent:**

```bash
xerobk audit
```

The audit row is written *before* the request leaves, not after. If the process
dies mid-request the worst case is a row marked `pending` that needs checking —
recoverable — rather than a change in Xero with no local record. `xerobk audit`
calls out pending rows explicitly.

---

## What it does

### Dashboard (`xerobk status`, `xerobk ui`)

Cash, receivables, payables, net position, aged profile, top debtors, and
what's falling due in the next fortnight. `xerobk ui` opens the same figures as
a browser dashboard with receivables detail, the reconciliation workspace, and
the close checklist as tabs.

### Bank coding (`xerobk reconcile`)

Import a bank statement CSV and get a suggestion per line, each with a
confidence score and the reasons behind it.

Two kinds of suggestion come out: **matches** (this receipt pays DEMO-1024 —
the amount is exact, the invoice number is in the narrative, the date is close)
and **codes** (this is a bank fee, code it to 404). The matcher also spots
**batch payments**, where several invoice balances sum exactly to one transfer.

Ambiguity is reported, not hidden. When a payment could settle any of three
same-priced invoices, all three are surfaced rather than one being guessed at —
silently picking wrong there means chasing a customer who has already paid.

```
xerobk reconcile statement.csv          # suggest only
xerobk reconcile statement.csv --apply  # record the confident ones locally
```

`--apply` records decisions in the local database; it does not post to Xero.
Those decisions then feed rule learning: a merchant coded the same way three
times, and never differently, becomes a rule for next month.

Statement formats are sniffed rather than mandated — header row present or
absent, a signed `Amount` column or separate `Debit`/`Credit` columns,
`DD/MM/YYYY` (the Australian default) or ISO dates, and amounts written as
`$1,234.56`, `(1234.56)`, or `1234.56 CR`.

### Close checklist (`xerobk close`)

Thirteen checks against the period being closed, graded blocker / warning /
note:

| Check | Looks for |
|---|---|
| `cash_vs_receivables` | zero cash beside a live debtor book — usually a dead bank feed |
| `stale_bank_lines` | uncoded bank activity inside the period |
| `overdue_debt` | invoices past the escalation threshold |
| `debtor_concentration` | one customer holding too much of the book |
| `duplicate_contacts` | the same entity entered twice, splitting its balance |
| `duplicate_invoices` | same contact, same total, same reference, within 14 days |
| `draft_invoices` | drafts inside the period — revenue and GST not yet in the ledger |
| `gst_consistency` | invoices whose recorded tax does not recompute from the lines |
| `missing_due_dates` | outstanding invoices that can never age |
| `contacts_missing_email` | debtors who cannot be sent a statement |
| `large_transactions` | outsized items worth an eyeball before sign-off |
| `gst_summary` | indicative G1 / 1A / 1B for the period |
| `aging_profile` | the aged split, as context |

Exit code is `1` when there are blockers, so it drops into a scheduled job.

Australian periods are built in: the financial year runs 1 July – 30 June, and
BAS quarters follow it (Q1 Jul–Sep, Q2 Oct–Dec, Q3 Jan–Mar, Q4 Apr–Jun). Use
`--frequency monthly|quarterly|annual`.

**On the GST figures:** these are arithmetic, not tax advice. The summary is
computed on an accruals basis from invoice dates and excludes payroll,
adjustments, and anything not raised as an invoice. Cross-check against Xero's
own GST Reconciliation report before lodging. The due dates shown are the ATO's
standard self-lodgement dates — lodging through a registered agent generally
earns a later one, so treat them as "earliest".

### Invoice and bill drafting

`xerobk.drafting` builds and validates Xero invoice payloads. Validation runs
before anything is sent: a rejected POST costs a round trip, but an *accepted*
POST with the wrong tax treatment becomes a correcting journal later. Drafts are
created with `Status: DRAFT` unless authorising is explicitly requested —
approving an invoice puts it in the ledger and on the BAS, which should be a
deliberate second step.

### Snapshots

`xerobk snapshot <dir>` dumps the ledger to local JSON. Useful for working
offline, for reproducible testing, and for keeping the API call count down.

```bash
xerobk snapshot ~/xero-snapshots/2026-08
xerobk close --snapshot ~/xero-snapshots/2026-08
```

A snapshot contains real financial data. Keep it out of version control.

---

## Architecture

```
src/xerobk/
  money.py          Decimal money, AU GST arithmetic, ROUND_HALF_UP
  models.py         domain models matching the shapes Xero actually returns
  aging.py          bucket rules, shared by every provider
  autax.py          AU financial year and BAS quarter periods
  rules.py          coding rules + learning from history
  reconcile.py      bank line <-> invoice matching, confidence scoring
  close.py          the close checks
  drafting.py       invoice/bill payload construction and validation
  posting.py        turning decisions into Xero writes, with an audit trail
  reports.py        dashboard assembly
  store.py          SQLite: coding history, close runs, audit log
  server.py         loopback UI server
  cli.py            argparse CLI
  providers/
    __init__.py     the XeroProvider protocol
    snapshot.py     offline JSON provider
    live.py         Xero Accounting API over OAuth 2.0
    oauth.py        PKCE flow and token storage
    bankcsv.py      bank statement CSV sniffing
    factory.py      provider selection
```

The application talks to `XeroProvider` and never to Xero directly, so the
offline and live paths are the same code. A provider that cannot write raises
`ReadOnlyError` rather than silently doing nothing — a caller can never believe
it posted something it did not.

Aging is computed in one place. The live provider does *not* use Xero's
per-contact aged report, which would cost one API call per customer; it fetches
outstanding invoices once and buckets them with the same code the offline
provider uses, so the two cannot drift apart. The bucket boundaries were
verified against a real Xero aged receivables report rather than inferred from
the labels — note that "3 months" is the 91–120 day band and anything past 120
days falls into "3+ months".

### Money

Every monetary value is a `Decimal`, never a float, and rounding is
`ROUND_HALF_UP` — Python's default banker's rounding disagrees with Xero and the
ATO at the half-cent. Values crossing the API boundary are converted through
`str()` so `0.1` becomes `Decimal("0.1")` rather than its binary approximation.

### Security

The UI holds financial data, so "it's only localhost" is not treated as a
security model:

- the socket binds to `127.0.0.1`, never `0.0.0.0`;
- a random session token is minted per run and required on every request,
  compared in constant time, and injected into the page rather than the URL so
  it never reaches browser history or a `Referer` header;
- requests whose `Host` header is not a loopback literal are rejected, which
  blocks DNS rebinding;
- a strict CSP allows no external resources of any kind.

Writes to Xero are logged to the local audit table *before* they are sent. If a
POST succeeds and the process dies, an after-the-fact log would lose the only
local record of a change that exists in Xero.

---

## Development

```bash
pip install -e ".[dev]"
pytest                       # test suite
ruff check src tests         # lint
mypy src                     # types (strict)
python data/demo/generate.py # regenerate the demo snapshot
```

## Limitations

Worth knowing before relying on it:

- **Bank feeds.** Xero exposes live statement lines only through the Bank Feeds
  API, which needs a partner-level app. CSV import is the supported route here.
- **Contact merging** is not possible via the API at all — Xero offers no merge
  endpoint, so duplicates are reported but must be merged in the Xero UI.
- **The write paths have unit tests but no integration tests.** Payload shapes
  are verified against a recording fake; they have not been exercised against a
  live Xero tenant. Consider a Xero demo organisation for the first run.
- **Cash basis.** GST figures are accruals-basis only.
- **Multi-currency.** Models carry currency and rate, but reports total in the
  base currency without revaluation.
- **Payroll.** Out of scope. Wages appear as bank lines to be coded, nothing more.
- **Tracking categories.** Carried on draft lines, not yet reported on.
- **One organisation** per connection; the first connected tenant is used.

## Licence

MIT.
