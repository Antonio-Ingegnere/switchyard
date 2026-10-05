# V2-006 manual dogfood

`dogfood_bugfix.py` reuses `execute_v2_workflow` and runs one selected part once.
It makes **real provider calls**. Run offline tests first. Local model aliases are
read from a caller-owned JSON file, never from committed machine configuration.

```sh
.venv/bin/python examples/delivery/dogfood_bugfix.py --part a --model-aliases /path/to/local-aliases.json
.venv/bin/python examples/delivery/dogfood_bugfix.py --part b --model-aliases /path/to/local-aliases.json
```

Part A copies the priority/FIFO fixture to a retained temporary directory, verifies
the exact single failing baseline, then runs the unchanged canonical BUGFIX flow.
It checks durable artifacts, source modification, byte-identical independent tests
and the real command verifier. The natural human request comes from the existing
MicroSpec. Fake model processes are used only in the offline test.

Part B loads `examples/escalation/workflows-v2.yaml`. A fixed cache experiment is
initial factual evidence. The three bounded tasks use singleton return contracts
and explicit no-tools capabilities. There are no direct stage calls or provider
session transfers; ordinary runtime transitions perform escalation.

Both parts retain `target/`, `work/`, `events.jsonl`, `observations.jsonl`,
`baseline-files.json`, `changed-files.json` and `report.json`. Part A also retains
`baseline-verification.txt`. Reports contain binding/model/capability metadata,
artifact version/digest references and handoff identities, not full prompts or
provider transcripts. Admission observations use the existing context builder;
offline tests compare them to actual fake-provider inputs. Completion requires
inspection of workflow-produced reproduction evidence, not just the harness baseline.

No harness retry, artifact repair, source intervention, commit or push is performed
in the disposable project. Retained directories are never silently deleted on
success or failure. Use a fresh root for each separately authorized attempt.

The completed live proof is recorded in
[`docs/plans/v2-006-bugfix-dogfood.yaml`](../../docs/plans/v2-006-bugfix-dogfood.yaml).
Part A attempt 7 reached `READY_FOR_MANUAL_QA`, with an unchanged acceptance oracle
and three passing tests. Attempts 1–6 remain in the history. The first Part B run
reached `ASSESSED`: Haiku/medium returned `inconclusive`, Sonnet/high continued with
handoff #1 and returned `diagnosis_disproved`, then Codex/high started fresh with
`request` v1 and `reproduction_evidence` v1, no previous handoff, and returned `ready`.
All Part B capability sets were empty. The concrete Codex model is resolved through
external local configuration; no account-specific model is committed.

REPRODUCE had repo_read + command and no repo_write, and its durable evidence matched the
independently verified baseline.

Switchyard did not independently observe successful provider command-tool execution.
