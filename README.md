# agentic-planner

A tool-execution planner where every argument has to prove where it came from.

The agent decides *which* tools to call and in what order; a separate
orchestrator runs them. That makes the deliverable a data structure rather than
an answer, and it means the plan can be checked before anything executes.

## The problem this is built around

A malformed plan announces itself. A well-formed plan containing one invented
number executes perfectly and returns a wrong answer, with nothing in the output
showing where the number came from.

Here is a plan that passes every structural check — correct tool, correct
argument name, correct type, valid graph:

```json
{
  "step": 1,
  "tool": "save_my_money_to_bank",
  "args": {
    "amount": { "v": 250000, "src": "user_literal", "ev": "transfer 2500 to savings" }
  }
}
```

The user actually said *"i've got too much money lying around"*. They never named
an amount and never asked for a transfer.

```
validation ok: False
  [evidence_not_found_in_user_message] step 1.args.amount:
  evidence 'transfer 2500 to savings' does not appear in the user's message
routed to:  clarify
rationale:  a value has no honest source; regenerating cannot invent one,
            so ask the user instead
```

The check is a substring comparison. The model is not trusted to be honest about
where a value came from; it is required to show its evidence, and code grades the
evidence.

## Design

**Argument provenance.** Every argument is `{v, src, ev}` rather than a bare
value. Four sources are legal, each with its own check:

| `src` | Meaning | How it is verified |
|---|---|---|
| `user_literal` | The user said this | `ev` must appear in their message, and any number in it must match `v` |
| `normalized` | Derived by a documented rule | The (`ev`, `v`) pair must exist in the time table |
| `step_ref` | Output of an earlier step | Must resolve backwards in the graph |
| `default` | A published catalog default | Read tools only |

A value with no legal source does not go in a plan. The correct output is a
question.

**The read/write asymmetry.** Arguments to `save_my_money_to_bank` may only use
`user_literal` or `step_ref`. A transfer amount is either a number the user gave
or a number the tools computed, never an inference. Reads may lean on defaults;
writes may not. The cost of guessing wrong is not symmetric, so the rules are not
either.

**Typed repair routing.** Failures are classified before they are corrected,
because different failures call for opposite responses:

| Family | Example | Action |
|---|---|---|
| structural | undeclared dependency, unknown parameter | regenerate against the same evidence |
| grounding | evidence not found in the message | ask the user; regeneration cannot invent a value |
| policy | default source on a write tool | refuse |
| consistency | `ok_with_assumptions` with no assumptions | regenerate |
| repair | the repair itself cheated | fail closed |

**The laundering guard.** Told that a `user_literal` has no supporting evidence,
the cheapest fix available to a model is to relabel it as a `default`. That passes
validation and lets the invented number straight through, turning the retry loop
into a mechanism for whitewashing the exact failure the grounding system exists to
catch. Repairs are therefore diffed against the original and rejected on any
source downgrade or status upgrade, whatever the model claims it did.

**Folds.** `calculator` takes two operands and the transaction count is unknown at
planning time. Rather than replanning mid-execution, a step may carry a
declarative fold that the orchestrator expands into a chain of real calculator
calls. Loop expansion over a list is a `for` loop, not work that needs a language
model.

## Layout

```
planner/
  catalog.py       tool signatures as data, plus the time normalization table
  types.py         plan contract (pydantic) and the error taxonomy
  validator.py     six check families, all deterministic
  router.py        typed routing and the repair-integrity diff
  tools.py         seeded mock tools
  orchestrator.py  topological execution, fold expansion, confirmation gate
scripts/demo.py    four end-to-end scenarios
tests/             74 tests, no network, no API key
```

## Running it

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
python -m scripts.demo
```

Everything is deterministic and offline. The mock tools are seeded, so the same
plan produces the same numbers on every run.

## Known limitations

**Conditional plans do not work.** "If I overspent, move the difference back out"
cannot be a static graph, because the branch depends on a value the planner never
sees. `needs_replan` is a pressure valve, not a solution.

**The provenance wrapper is verbose.** Every argument becomes an object rather
than a scalar, which costs output tokens on every request. It buys the only
mechanical defence against invented values, but the cost is real and paid
constantly.

**`calculator.operation` is exempted from the default check.** The operation is
derived from intent rather than defaulted, so labelling it `default` is loose. The
exemption is explicit in `catalog.py` rather than papered over with a fifth source
category carrying its weight for one field.

**Self-reported confidence is weakly calibrated** and should be treated as a
coarse routing signal, with the threshold tuned against a labelled set rather than
picked by intuition.

**Out of scope:** authentication, multi-user isolation, rate limiting, and
recovery when a tool fails partway through a plan that has already moved money.