# Architecture

Three views of the system. Mermaid renders natively on GitHub — no toolchain,
no build step, no generated images to fall out of date.

| Document | Answers |
|---|---|
| **[sdlc.md](sdlc.md)** | How does a change reach production, what checks it, and **what nothing checks**? |
| **[runtime.md](runtime.md)** | What does the deployed system actually do, on what schedule, with which model? |
| **[data.md](data.md)** | Where does state live, who reads it over what window, and how long does it survive? |

## Reading order

Start with **sdlc.md** if you are changing code — particularly the *"What
nothing checks"* section, which lists the six things this pipeline does not
verify. Start with **runtime.md** if you are debugging a published post. Start
with **data.md** if you are touching retention, dedup or the analytics tables.

## Keeping these honest

These are hand-maintained, so they can drift. Two guards:

- Every schedule, cron state and threshold quoted here was read out of
  `infrastructure/terraform/*.tf` at the time of writing, not from memory or
  from CLAUDE.md. When a diagram and the Terraform disagree, **the Terraform is
  right** — fix the diagram.
- `tests/test_terraform_schedule.py` and `tests/test_infrastructure.py` already
  assert on cron expressions, rule states and lifecycle rules, so the facts most
  likely to drift are pinned in CI rather than only in prose.

One deliberate omission worth restating: the GitHub OIDC role that runs
`terraform apply` is **not** defined in `infrastructure/terraform/`. It exists
only as `secrets.AWS_ROLE_ARN`. It is the most privileged credential in the
system and the one component these diagrams describe from the outside, because
the repo genuinely does not contain it.
