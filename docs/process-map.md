# Process map and state model

The project brief asks for a "process map or state model of the agent, exported and committed to your
repo" (Project Brief (Revised), section 5). There are two diagrams here, both drawn from the code on
`main` plus the weekly agent run in PR #58:

1. **The weekly cycle:** who does what, from the machine's sales to the next week's order.
2. **The life of one purchase request:** every state a request can be in, and what moves it on.

Each diagram is exported as an SVG in `docs/process-map/`, with its Mermaid source folded below it. **If you change a diagram, change the Mermaid source and export it again.**

## 1. The weekly cycle

![The weekly cycle](process-map/weekly-cycle.svg)

<details>
<summary>Mermaid source (GitHub draws it here too)</summary>

```mermaid
flowchart TD
    M[("Vending machine and the DEX Bridge")]

    subgraph hourly["Sync worker, every hour"]
        S["sync_history: new polls and sales counters<br/>sync_requests: request status and receipts<br/>sync_catalog: what we may order"]
    end

    DB[("Our database<br/>polls, requests, receipts, ledger, action_log")]

    subgraph run["Agent run, once a week"]
        A0{"Halt switch on?"}
        AH["Stop and record the run as halted:<br/>nothing read, called or sent"]
        A1["Read the week: get_machine_state, get_sales_history,<br/>get_account_balance, get_purchase_requests,<br/>get_catalog, get_project_rules"]
        A2["The model decides the desired state<br/>for all 20 slots: product, price, fill"]
        A3["preview_desired_state:<br/>the planner checks every rule in code"]
        A4{"Refused?"}
        A5{"Submit or skip?"}
        A6["submit_desired_state:<br/>sends the previewed plan, once"]
        A7["Skip the week and record why"]
    end

    subgraph ops["Instructors and student worker"]
        O1{"The purchaser reviews the request"}
        O2["Buy at the named store,<br/>at or under the max pack price"]
        O3["Friday visit: load the slots, change the price tags,<br/>put the extra units in our closet"]
        O4["Report the receipt: units loaded,<br/>units to the closet, price paid"]
        O5["Rejected, with a reason"]
    end

    M --> S --> DB --> A0
    A0 -- yes --> AH
    A0 -- no --> A1 --> A2 --> A3 --> A4
    A4 -- "yes: fix the plan" --> A2
    A4 -- no --> A5
    A5 -- skip --> A7
    A5 -- submit --> A6
    A6 -- "purchase request" --> O1
    O1 -- approve --> O2 --> O3 --> O4
    O1 -- deny --> O5
    O4 -. "through the bridge; read by the next sync" .-> M
    O5 -. "through the bridge; read by the next sync" .-> M
```

</details>

How to read it:

- **The model is called only in the agent run**, and only to decide. Reading data, checking rules and
  doing arithmetic are plain code. The hourly sync never calls the model.
- **The planner is the gate.** Every plan, previewed or submitted, passes through the same checks:
  - the spend cap (`insufficient_balance`)
  - price bounds (`above_ceiling`, `below_floor`, `price_off_step`)
  - capacity (`over_capacity`, `unknown_capacity`)
  - the product being in the catalog (`product_unknown`)
  - store cutoffs (`past_cutoff`)
  - one open request at a time (`request_pending`, `submission_unresolved`)
  - the halt switch (`halted`)

  The model can only change its plan and try again. It can't skip a check.
- **Prices change only at a visit.** Friday's visit is free; a midweek visit is the same request
  with `off_cycle: true` and costs $25.
- **Our records change only when a receipt arrives**, not when a request is sent. A request is what
  we asked for; the receipt is what happened.
- **The halt switch** stops the run before every model request, and makes every write tool refuse.
  An instructor can turn it on without us (`docs/halt-for-instructors.md`).

## 2. The life of one purchase request

![The life of one purchase request](process-map/request-states.svg)

<details>
<summary>Mermaid source (GitHub draws it here too)</summary>

```mermaid
stateDiagram-v2
    [*] --> Previewed: preview_desired_state
    Previewed --> Previewed: refused, model fixes the plan
    Previewed --> Sending: submit_desired_state (same plan hash)
    Previewed --> [*]: skipped this week

    state "purchase_submissions (ours)" as ours {
        Sending --> Accepted: bridge answers with an ID
        Sending --> RefusedByBridge: bridge answers 4xx
        Sending --> NotFiled: never left (no connection)
        Sending --> Unknown: sent, no usable answer
        Unknown --> Accepted: a person runs submissions resolve with the ID
        Unknown --> NotFiled: a person runs submissions resolve --not-filed
    }

    state "purchase_requests (the bridge)" as theirs {
        Pending --> Rejected: purchaser denies
        Pending --> Fulfilled: worker reports the receipt
    }

    Accepted --> Pending
    RefusedByBridge --> [*]
    NotFiled --> [*]
    Rejected --> [*]
    Fulfilled --> [*]: ledger and stock updated
```

</details>

| State | Where it lives | What blocks while it lasts |
|---|---|---|
| `sending`, `unknown` | `purchase_submissions.status` | Every new preview and submit: `submission_unresolved`. A request that may exist must never be sent twice. |
| `pending` | `purchase_requests.status`, read hourly by `sync_requests` | Every new submit: `request_pending` |
| `fulfilled` | `purchase_requests`, plus one receipt row per line | Nothing. Stock, the closet and the ledger now include it. |
| `rejected` | `purchase_requests.status` and its `decision_note` | Nothing. The next run sees the rejection and its reason. |

## The agent run's own outcomes

Each run is one row in `agent_runs`, with every step in `agent_steps`:

| Outcome | When |
|---|---|
| `submitted` | `submit_desired_state` succeeded. The request ID is stored at once. |
| `skipped` | The model decided not to order, or it was a dry run. |
| `halted` | The switch was on at the start, or was turned on mid-run. |
| `failed` | It hit a spending cap, ran out of turns, or broke. Nothing half-sent: a submit either settled its row or left it `sending` for a person. |
