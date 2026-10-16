# How this builder works

`builder/main.py` reads one or more brief files plus the tool API file and writes
a Freya workflow. One command, no manual step:

```
python builder/main.py briefs/brief-a.md
python builder/main.py briefs/brief-a.md briefs/brief-b.md --test
python builder/main.py hidden-brief.md --openapi hidden-openapi.json --slot hidden
```

It is a compiler, not an agent. It makes no model calls at build time, so a run
is reproducible and takes under a second. Everything it knows comes from the
brief and the tool API file it was given.

## The idea

The phone agent runs on a model that is good at talking and bad at counting
business days. So the builder splits the work:

| the model does | the code does |
|---|---|
| talks, asks, reads back | every date, deadline, floor and amount |
| notices that the customer has hardship | which ending the call may have |
| picks an amount the customer agreed to | the outcome and reason sent to the bank |

## Reading a brief

`builder/brief_parser.py` turns prose or markdown into a `PolicySpec`
(`builder/policy_spec.py`). Each extractor reads the brief; none of them holds a
value of any particular bank.

* **Holidays** - any date format, taken from a holiday or calendar section and
  from every window around the word.
* **Bands** - the `days_late -> deadline, attempts` table read by column, with a
  prose fallback for "1 to 7 days late ... 12 business days".
* **Identity** - the security questions are matched against the field values the
  tool API actually accepts, so a brief that asks for a postcode and a year of
  birth produces different questions from one that asks for a date of birth.
* **Outcomes** - the closing table is read by column. The outcome names come from
  the brief, so a brief that renames them still gets its own names sent to the
  bank. A reason word that could belong to two situations ("declined" under
  `not_verified` and under `no_promise`) is resolved by the group the rest of the
  rows in that outcome fall into.
* **Queues, floors, windows, sentences** - read from the brief, with the tool API
  descriptions used only as a fallback.

`tools/check_builder.py` is the regression test. It runs three briefs - the two
public ones and `tests/brief-prose.md`, a brief the builder has never seen,
written with another bank's name, other outcome and reason names, other
holidays, other bands, other identity questions and another tool API - and
asserts the values a reader would take out by hand.

## The workflow it writes

```
setup(code) -> greeting -> identity -> fetch_account(api) -> product_facts(code)
                                                        -> negotiate
                                     -> hardship_step / stop_step / human_step
                                     -> transfer_restructuring / transfer_human
                                     -> confirm_promise
                                     -> close_<situation> -> end_<situation>
```

Three engine behaviours shaped this, all found by reading real transcripts:

* **A conversation step entered silently never speaks.** The agent says nothing,
  the customer waits, and hangs up (`customer-ended-call`). Every step the router
  enters is therefore either a fixed `tts` line or the default model turn. Steps
  entered *before* the customer speaks (`greeting`, `third_party`, `identity`,
  `negotiate`) speak a fixed line, which is also the fastest and the most exact.
* **Routing is suppressed straight after a tool result** if the agent has not
  spoken yet ("Suppressed: Missing prior assistant message"). So the way out of a
  step is a condition on the customer's answer, not on the model deciding to move.
* **The model must not be asked to say a fixed sentence.** `product_facts` writes
  the whole opening line - product, amount, days late and the first question -
  into a variable, and `negotiate` speaks it as `tts`. The debt can never be
  stated from a half-remembered conversation.

* Closing is entirely engine-driven: a policy-selected route enters
  `close_<role>`, which makes exactly one `close_call` request with the outcome
  and reason already baked in. On success, a terminal node speaks the fixed
  farewell and gives the customer a two-second window for a brief goodbye. A
  failed close request gets an honest error farewell. The model is not asked to
  choose the outcome or repeat the goodbye, so closing cannot turn into a
  conversation loop.
* Multi-product confirmation does not expose `close_call` as a model tool.
  The brief's multi-product component enables a gate that is absent from
  single-product briefs. API post-hooks update a per-product ledger tracker
  only after successful `create_promise` or `add_note` results. The tracker
  enables close routing only when every present product has a successful
  required record; the close outcome is selected from those records and the
  brief's outcome table. If the component is disabled, no collection gate
  variables or routes are generated.
* Dates, floors and deadlines are computed in code and written into the prompt.
  `create_promise` has a pre-hook that moves a weekend day to the next business
  day, a late date to the deadline and an amount below the floor up to the floor.
  Those are the rules the brief itself gives.
* `verify_identity` has a pre-hook that turns "14 March 1988" or "14031988" into
  the ISO date the service reads, so a customer who says a date in words is not
  failed for saying it in words.
* The account result reaches the code node in several shapes depending on which
  engine path ran. `tests/test_product_shapes.py` runs the snippet against all
  four.

## Checking before publishing

`builder/main.py` refuses to publish a workflow that has a null value, a route to
a node that does not exist, a passthrough node without a final `always`, a tool
on the wrong kind of node, or a code node whose snippet still contains a template
placeholder. `tools/check_builder.py` runs the same check over three briefs.

## Testing

`--test` runs the practice customers against the agent the run created and
writes the result to `results/`. A builder run may do this; it is the only part
that needs the network.
