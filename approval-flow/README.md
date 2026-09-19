# apflow

Approval workflows and spend control for a Xero ledger: approval matrices,
budget checking, delegation, and an audit trail that notices when a document is
edited after someone approved it.

It runs on one machine. There is no server to deploy, no account to create, and
**zero runtime dependencies** — the Xero client is built on `urllib`, the
dashboard is served by `http.server`, mail goes through `smtplib`, and storage
is `sqlite3`, all from the standard library. `pip install -e .` pulls nothing.

```
apflow pull                    # fetch draft documents and route them
apflow inbox --as dana         # what is waiting on one person
apflow approve AR-0004 --as dana
apflow push                    # authorise the approved ones in Xero
apflow ui                      # approver dashboard in your browser
apflow audit                   # everything that has happened, and why
```

---

## Quick start

```bash
cd approval-flow
pip install -e .

# Works immediately against the bundled synthetic demo org:
apflow matrix  --snapshot data/demo        # the approval matrix, validated
apflow pull    --snapshot data/demo --requested-by priya
apflow queue   --snapshot data/demo
apflow inbox   --snapshot data/demo --as dana
apflow show    --snapshot data/demo AR-0003
apflow ui      --snapshot data/demo
```

No credentials are needed for any of the above. The demo org is invented and is
deliberately seeded with the situations the workflow exists to handle: a bill
below the auto-approval band, a USD bill whose base-currency value crosses a
limit its face value does not, a travel bill that breaches its budget line, a
contractor budget already 92% committed, and a supplier whose email address
matches one of the approvers.

When `--snapshot DIR` is used, `matrix.json`, `users.json` and `budget.json` are
read from that directory if they exist, which is why the demo needs one flag.

---

## How routing works

An **approval matrix** is an ordered list of rules. The first rule whose
condition matches a document selects the workflow for it; the workflow is a list
of **steps**, run in order, each with its own approvers and its own optional
condition.

```json
{
  "name": "Supplier bills — standard",
  "when": { "kinds": ["bill", "purchase_order"], "amount_max": "25000.00" },
  "steps": [
    { "name": "Budget holder", "approvers": ["dana"], "sla_days": 2 },
    { "name": "Finance", "approvers": ["sam"], "when": { "amount_min": "2500.00" }, "sla_days": 2 }
  ]
}
```

Two levels of condition, because they answer different questions. **Rule
conditions** pick the workflow — capital expenditure goes through the capex
chain, everything else through the standard one. **Step conditions** tier it —
the CFO step applies only above $50,000. Collapsing those into one level forces
a separate rule per amount band, and then every change to the base workflow has
to be made in four places, which is how a matrix drifts out of line with the
delegation of authority it is supposed to encode.

A step's `quorum` is `any` (default) or `all`. Amount bands are `min <= amount <
max`, half-open, so adjacent bands written as 0–10,000 and 10,000–50,000 cannot
both claim a $10,000 bill. Every amount is compared in the organisation's base
currency: USD 9,000 sits inside a AUD 10,000 limit at face value and outside it
at the booked rate.

A document matching no rule is an error, not an implicit approval. It is
reported unrouted, and `apflow matrix --check` warns at configuration time if no
catch-all rule exists.

### What `matrix --check` catches

```
apflow matrix --check
```

- an approver who is not in the directory, or who is marked inactive
- a rule that matches everything but is not last, and so shadows the rest
- an amount band that can never match
- **a step whose approvers cannot clear the amounts it will see** — an unbounded
  rule staffed only by limited approvers stalls on the first large bill, and it
  stalls at 4pm on a Friday

That last check found a real defect in this project's own demo matrix while it
was being written.

---

## Controls

Every refusal below is a control an auditor will ask to see, and each one is a
single setting that can be turned off deliberately rather than a behaviour
buried in code.

| Control | Setting | What it refuses |
|---|---|---|
| Separation of duties | `separation_of_duties` | Whoever submitted a document cannot also approve it |
| Delegated authority | `enforce_approval_limits` | An approver cannot clear above their limit — and the refusal names who can |
| Related-party spend | `block_supplier_self_approval` | An approver whose email matches the counterparty cannot approve |
| Sequence | always | Step 3 cannot be approved while step 1 is open |
| One vote each | always | The same person cannot vote twice on one version |
| Reasoned rejection | always | A rejection without a comment is refused; the requester has to know what to fix |
| Withdrawal | always | An approver cannot withdraw a request instead of rejecting it on the record |

Out-of-office substitution is resolved at routing time, not at decision time: an
approver who goes on leave *after* a request is raised still owns it. The
substitution is recorded on the decision (`acting_for`), never hidden.

---

## Changes after approval

An approval is consent to a specific document, not to a document number.

Every document carries a fingerprint — a hash over the counterparty, amounts,
dates, coding and every line. Every decision records the fingerprint it was made
against, and only decisions matching the document's current fingerprint count.
Edit an approved bill and its approvals stop counting the moment the change is
pulled, automatically, with no reliance on anyone noticing.

```
$ apflow pull
  superseded      AR-0002   Northwind Media B-2202   $7,700.00   document changed after approval started

$ apflow show AR-0002
   ..  1. Marketing lead: Ben Attwood
        approve ben  2026-09-19T20:07:53   [superseded version]
          "campaign approved"
```

Ben's approval is kept and labelled. It is never deleted, and it never counts
again.

If the change is large enough to alter the workflow itself — a bill that grows
past the CFO threshold — the request is superseded by a new one with the new
routing, rather than having its steps rewritten underneath its own decision
history. Both requests record the link.

The same check runs again immediately before posting, because the gap between
the last approval and the write to Xero is exactly where an approved-then-edited
bill would otherwise slip through:

```
$ apflow push
  changed     AR-0006  Meridian Legal B-2206   $9,000.00   not posted — total 9000.00 -> 14000.00; approvals reset
```

---

## Budgets

A budget is a set of lines keyed by `(period, account, tracking)`. A document is
checked by allocating each of its lines to the matching key — ex-tax, in base
currency — and asking whether the remaining balance covers it.

What counts as *spent* is the part that matters. Checking against actuals alone
is the standard way to blow a budget with every control passing: two approvers
work the same queue, each sees $50,000 remaining, each clears a $40,000 bill.
So the balance here is consumed by **commitments** — every request that is
pending or approved, whether or not it has reached the ledger — and a pending
request holds its allocation from the moment it is raised. Reject or withdraw it
and the allocation comes back.

```
$ apflow budget
  line                                     budget    committed    remaining  used
  Marketing · 2026-09                  $12,000.00    $7,320.00    $4,680.00  61%
  Travel · 2026-09                      $4,000.00    $6,300.00   -$2,300.00  158% !
  Contractors · 2026-09                $60,000.00   $55,000.00    $5,000.00  92%
```

Enforcement is a policy setting, not a property of the budget:

| `budget_enforcement` | Behaviour |
|---|---|
| `off` | No budget check at all |
| `warn` (default) | The breach is recorded, shown to every approver, and let through |
| `block` | Submission is refused |

Most finance teams want `warn`. A hard block on a budget line is how shadow
approval processes get started.

Annual and quarterly budgets are keyed on the financial year, not the calendar
year — `fy_start_month` defaults to July.

---

## Connecting a real Xero organisation

The app is read-only until you say otherwise, and it never stores a client
secret.

1. Go to <https://developer.xero.com/app/manage> and create an app.
2. Choose the **Mobile or desktop application** type. This is the PKCE flow: it
   issues no client secret, which is the right choice for software that runs on
   an end user's machine and therefore cannot keep one.
3. Add the redirect URI `http://localhost:8721/callback`.
4. Export the client id and connect:

```bash
export XERO_CLIENT_ID=<your client id>
apflow connect                 # read-only scopes
apflow connect --allow-writes  # also request write scopes
```

Tokens are written to `~/.local/share/apflow/token.json` with `0600` permissions
(`%LOCALAPPDATA%\apflow` on Windows). Override the location with `APFLOW_HOME`.
Refresh tokens rotate on every use and are persisted before the new access token
is handed back, so an interrupted refresh cannot strand you.

Once connected, drop the `--snapshot` flag and the same commands run against
your live ledger.

### Read-only vs read/write

| | Read-only (default) | With `--allow-writes` |
|---|---|---|
| Scopes | `accounting.transactions.read` | `accounting.transactions` |
| Pull, route, approve, reject | yes | yes |
| Budget checking, reporting, audit | yes | yes |
| `apflow push --dry-run` | yes | yes |
| Authorising documents in Xero | no | yes |

Write scopes are opt-in per connection, so an accidental run can never authorise
a bill.

### Posting back

`apflow push` sets the ledger status of every approved, unposted document whose
fingerprint still matches. It changes one field and names the document by id —
posting a full document body back would silently overwrite any edit made in Xero
since the pull, including reinstating a version an approver rejected.

The audit row is written *before* the API call, never after. If a POST succeeds
and the process dies before recording it, an after-the-fact log loses the only
local record of a change that now exists in the ledger. Anything left `pending`
in `apflow audit` was sent without a recorded outcome and should be checked in
Xero before retrying.

---

## Notifications

Approval systems fail quietly: the request is raised, nobody is told, and three
weeks later someone asks why the supplier has not been paid.

Every routing event writes an addressed message. With no SMTP host configured
they are written to `~/.local/share/apflow/outbox` as `.eml` files and recorded
in the database — complete and inspectable, with nothing leaving the machine.

```bash
apflow remind            # write reminders for everything past its SLA
apflow remind --send     # deliver them over SMTP
```

The threshold is the step's own `sla_days` when it has one, otherwise
`reminder_after_days`. Past `escalation_after_days` the message also names the
approver's escalation contact — naming them rather than silently re-routing,
because moving someone's authority without telling them is how these systems
lose the trust of the people in them. At most one chase per step per day.

```bash
apflow config --set smtp_host=smtp.example.com --set smtp_from=apflow@example.com
export APFLOW_SMTP_USER=apflow@example.com
export APFLOW_SMTP_PASSWORD=...        # never written to disk
```

---

## The dashboard

```bash
apflow ui
```

A single page: the queue, one request in full (lines, budget impact, workflow,
audit trail), and approve/reject with a comment.

It binds to 127.0.0.1 only, mints a random session token per run and requires it
on every request, and rejects any request whose `Host` header is not a loopback
literal — "it's only localhost" is not a security model, because every process
on the machine can reach it and so can any website the browser visits.

**The "acting as" selector is a convenience, not authentication.** Anyone who
can reach this process can act as anyone in the directory, exactly as they can
from the command line. That is the correct threat model for a tool on one
finance person's machine, and the wrong one for a shared deployment.

---

## Configuration

Three JSON files. They are governance artefacts — a delegation of authority is
not an application preference — so they live wherever you keep reviewed
configuration and are passed with `--matrix`, `--users` and `--budget`, or set
once in `config.json`.

**`users.json`**

```json
{
  "users": [
    { "id": "dana", "name": "Dana Whitfield", "email": "dana@example.com",
      "role": "Operations manager", "approval_limit": "25000.00", "escalates_to": "cfo" },
    { "id": "sam", "name": "Sam Okafor", "email": "sam@example.com",
      "approval_limit": "50000.00", "away_until": "2026-09-26", "delegate_to": "jo" },
    { "id": "cfo", "name": "Alex Mercer", "email": "alex@example.com",
      "approval_limit": null }
  ]
}
```

`approval_limit: null` is unlimited authority. `active: false` retires someone
without deleting them, so their past decisions keep their name.

**`matrix.json`** — `apflow matrix --init` writes a starter one. Rule and step
conditions accept `kinds`, `amount_min`, `amount_max`, `suppliers` (glob),
`accounts` (glob), `tracking` (`Category:Option`), `currencies` and
`requires_purchase_order`.

**`budget.json`**

```json
{
  "granularity": "monthly",
  "fy_start_month": 7,
  "lines": [
    { "period": "2026-09", "account": "420", "amount": "12000.00", "name": "Marketing" }
  ]
}
```

**Settings** live in `~/.local/share/apflow/config.json`:

```bash
apflow config                                      # show everything
apflow config --set budget_enforcement=block
apflow config --set separation_of_duties=false
```

---

## Commands

| Command | What it does |
|---|---|
| `status` | Queue overview: value awaiting approval, SLA breaches, whose desk |
| `pull` | Fetch draft documents and route anything new. Idempotent — run it on a timer |
| `queue` | Everything awaiting approval, with age and step |
| `inbox --as USER` | What is waiting on one person |
| `show REF` | One request in full, including the audit trail |
| `approve REF --as USER` | Record an approval |
| `reject REF --as USER --comment ...` | Record a rejection |
| `cancel REF --as USER` | Withdraw a request |
| `matrix` | Show and validate the approval matrix; `--init` writes a starter |
| `users` | The approver directory, with limits and delegations |
| `budget` | Budget lines, commitments and headroom |
| `push` | Authorise approved documents in Xero. `--dry-run` shows what would go |
| `remind` | Chase overdue approvals; `--send` delivers over SMTP |
| `audit` | Workflow events and every write sent to the ledger |
| `report turnaround\|spend` | Decision turnaround per approver; spend in flight |
| `connect` | Authorise this machine against a Xero organisation |
| `config` | Show or change settings |
| `ui` | The dashboard |

Every command takes `--json`, so the tool drives from a scheduled job as easily
as from a terminal.

---

## What it deliberately does not do

- **Capture invoices.** No OCR, no supplier email inbox. Documents come from the
  ledger as drafts.
- **Pay anything.** Approval ends at authorising the document. Payment runs stay
  where the bank controls are.
- **Authenticate people.** There are no passwords, because there is no server.
  The audit trail is exactly as strong as the trust placed in whoever holds the
  keyboard.
- **Run as a shared service.** One machine, one SQLite file. Multi-user
  deployment needs real authentication, which is a different product.
- **Float money.** Every amount is a `Decimal`, rounded `ROUND_HALF_UP`, never a
  float, not even transiently.

---

## Where things live

| | |
|---|---|
| Approval history | `~/.local/share/apflow/apflow.sqlite3` |
| OAuth tokens | `~/.local/share/apflow/token.json` (`0600`) |
| Settings | `~/.local/share/apflow/config.json` |
| Unsent messages | `~/.local/share/apflow/outbox/*.eml` |

Everything is under one directory, so it is trivial to back up or delete. Set
`APFLOW_HOME` to move it. Nothing sensitive is ever written into the repository.

## Development

```bash
pip install -e ".[dev]"
ruff check src tests
mypy src
pytest -q
python data/demo/generate.py    # regenerate the demo org
```

## Licence

MIT.
