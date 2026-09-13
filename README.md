# NIGHTWATCH

**Autonomous Incident Commander Swarm** — *your AI incident commander for the 2 AM production failure.*

A production incident fires. Five specialised agents investigate it in parallel across
PagerDuty, Datadog and GitHub. A deterministic scorer weighs competing root-cause
hypotheses against the evidence. A commander issues one recommendation with a confidence
score. **Then it stops and asks a human.** Only after approval does it roll back, verify
recovery, and close the incident.

---

## Run it in 60 seconds

```bash
pip install -r requirements.txt
python validate.py          # 11 pass/fail checks, no API keys needed
python main.py demo         # the scripted demo, in your terminal
python main.py serve        # live dashboard at http://127.0.0.1:8000
```

No credentials are required. Every integration NIGHTWATCH cannot reach falls back to a
deterministic fixture world, and the complete workflow still runs end to end.

To see a **real** rollback change **real** state:

```bash
python demo/payment-service/app.py     # terminal 1 — the breakable service
python main.py serve                   # terminal 2 — NIGHTWATCH
python demo/trigger_incident.py        # terminal 3 — ship the bad deploy and page
```

Then click **Approve Rollback** on the dashboard and watch the service go from a 45%
error rate back to healthy.

---

## Which APIs do I need?

```bash
python setup.py            # interactive wizard: what each key is for and where to get it
python setup.py --check    # what is configured right now
python setup.py --verify   # call each API to prove the credentials actually work
```

| System | Purpose | Cost | Needed keys |
|---|---|---|---|
| **PagerDuty** | Incident trigger (entry point) | Free dev account | `PAGERDUTY_API_KEY`, `PAGERDUTY_FROM_EMAIL` |
| **Datadog** | Logs + metrics | 14-day trial | `DD_API_KEY`, `DD_APP_KEY` (needs `logs_read_data`) |
| **GitHub** | PRs, commits, changed files | Free | `GITHUB_TOKEN` (fine-grained), `GITHUB_REPO` |
| **Slack** | Human approval surface | Free workspace | `SLACK_BOT_TOKEN`, `SLACK_CHANNEL` |
| **Groq** *or* **Gemini** | Narrative summaries only | Free tier | `GROQ_API_KEY` or `GEMINI_API_KEY` |

Four external systems, which satisfies the usual "integrate 3+ services" bar.
Webhooks need a public URL in development — use `ngrok http 8000` and point PagerDuty at
`/webhooks/pagerduty` and Slack Interactivity at `/webhooks/slack`.

**The LLM is not load-bearing.** It writes the human-readable summary. Hypothesis scoring
is deterministic code, so the verdict is reproducible and auditable with or without a key.

---

## Architecture

```
PagerDuty ──webhook──> FastAPI ──> LangGraph orchestrator
                                          │
                        ┌─────────────────┴─────────────────┐
                        │                                   │
                  Log Detective                       Metrics Analyst
                    (Datadog)                            (Datadog)
                        │                                   │
                 Code Archaeologist                         │
                     (GitHub)                               │
                        └─────────────────┬─────────────────┘
                                    Timeline Detective
                                          │
                                   Correlation Agent      (weighted signal scoring)
                                          │
                                   Incident Commander     (risk + one recommendation)
                                          │
                                        Slack             (Block Kit, Approve/Reject)
                                          │
                                  ══ HUMAN APPROVAL ══
                                          │
                                     Action Agent         (verify → rollback → verify → close)
```

Agents run in parallel where they are genuinely independent. Code Archaeologist runs
*after* Log Detective because the dependency is real — you cannot match changed files
against a stack trace you do not have yet. Timeline is a deferred join node, so it fires
once after every branch completes rather than once per arriving edge.

The approval gate is a **process boundary**, not an in-memory pause. Phase one ends after
the Slack post; state is persisted to SQLite; the human decision arrives later over a
webhook and re-enters the same graph at the Action Agent. The server can restart in
between and the approval still resumes against the exact evidence that was analysed.

### Agent roster

| Agent | Codename | Source | Responsibility |
|---|---|---|---|
| Incident Investigator | Sentinel | PagerDuty | Parses the alert: service, time window, error type, suspected deploy |
| Log Detective | Log Hunter | Datadog | Deduplicates logs into error *patterns*, extracts stack traces |
| Code Archaeologist | Code Detective | GitHub | Matches recent PRs' changed files against the failing stack trace |
| Metrics Analyst | Metrics | Datadog | Separates metrics that *stepped* from metrics merely elevated |
| Timeline Detective | Timeline | Correlated | Builds one clock, establishes deploy→error causality |
| Correlation Agent | — | All agents | Scores competing hypotheses against weighted signals |
| Incident Commander | Commander | Correlation | Risk assessment, one recommendation, confidence |
| Action Agent | Operator | Slack approval | 7-step verification, rollback, health check, close-out |

---

## Why this isn't "an LLM reads logs and guesses"

Correlation is a **weighted signal model**, not a prompt. Each hypothesis declares named
signals with fixed weights, each signal is a pure function over the evidence pool, and
each one records *why* it matched. The output is a table you can audit line by line:

```
  91%  Bad deployment
       + Deployment shipped before the first error (0.25)
         deployment shipped 4.3 min before the first error
       + A changed file appears in the failing stack trace (0.25)
         PR #1842 changed src/main/java/com/acme/payments/PaymentValidator.java
       + Error signature is new since the deployment (0.18)
       + Deployment targeted the affected service (0.15)
       + Service metrics degraded after the deployment (0.08)
       - No competing infrastructure anomaly (0.09)
         competing signal present: elevated db.query.latency

  23%  Database failure
       + Database latency above baseline (0.23)
       - Database errors appear in logs (0.45)  — none found
       - Database latency stepped when errors began (0.32)
         already elevated before the window; it did not change when errors began

  17%  Traffic spike
       + Request volume above baseline at all (0.17)  — only +6%
       - Request volume spiked well above baseline (0.50)
       - Errors track request volume (0.33)
```

The database hypothesis is rejected *for a stated reason*, not ignored. Absence of data
never counts as evidence of absence: if no metrics were collected, the "no competing
signal" check fails rather than passing by default.

---

## The human gate

No remediation is ever automatic. On approval the Action Agent runs seven checks, and
checks 1–3 are preconditions — if any fails, **nothing executes**:

1. Approval came from an authorised source (`NIGHTWATCH_APPROVERS`)
2. Incident is still active (not already resolved)
3. Deployment ID matches the one that was analysed
4. Execute the rollback
5. Verify service recovery against baseline
6. Update the Slack thread with the outcome
7. Resolve the PagerDuty incident

Tests cover the failure modes directly: an unauthorised approver aborts, a tampered
deployment target aborts, a rejection executes nothing, and a second approval is refused.

---

## Validation

`python validate.py` runs the scenario from the design record and asserts specific,
checkable claims rather than "the AI is smart":

```
  1. Incident detected                  [PASS]
  2. Correct service identified         [PASS]  payment-service
  3. Correct error identified           [PASS]  NullPointerException x87
  4. PR #1842 identified                [PASS]  matched on PaymentValidator.java
  5. Root cause = deployment            [PASS]  top hypothesis 'Bad deployment'
  6. Confidence > 80%                   [PASS]  91%
  7. Rollback recommended               [PASS]  rollback of deployment 8421
  8. Human approval required            [PASS]  no action existed until a human decided
  9. Rollback executed                  [PASS]  approved by vara
 10. Health restored                    [PASS]  error rate 0.03% after rollback
 11. Rejection leaves prod untouched    [PASS]
```

`python -m pytest` adds 18 tests covering the scorer, the confidence floor, the decoy
PRs, and every abort path.

---

## Layout

```
main.py                     FastAPI app, webhooks, SSE, CLI (serve/demo/validate/status)
setup.py                    API key wizard — what you need and where to get it
validate.py                 the 11 pass/fail checks
config.py  models.py        settings; incident/evidence/hypothesis/action schema
Agents/                     sentinel, log_hunter, code_detective, metrics_agent,
                            timeline, correlation, commander, notifier, operator
orchestrator/               LangGraph workflow, shared state, event bus, runner
integrations/               pagerduty, datadog, github, slack, deploy — plain REST
LLM_Providers/              Groq, Gemini, Mock behind one interface
storage/db.py               SQLite: incidents, evidence, hypotheses, approvals, actions
static/dashboard.html       live dashboard, agents lighting up over SSE
demo/                       fixtures, breakable payment-service, load generator, trigger
tests/                      18 tests
```
