# How the AI-Readiness Evaluation Works

The evaluation answers two separate questions:

1. **Substrate:** Is the repository structured so an AI agent can understand,
   change, and validate it reliably?
2. **Operation:** Does the repository actually use agents and automation in a
   safe, observable, repeatable operating model?

Each axis receives a score from 0 to 100. The headline composite score is their
geometric mean, so one strong axis cannot hide a weak one.

```text
composite score = sqrt(substrate score * operation score)
```

A repository is reported as **AI-Ready** only when **both** axis scores are at
least 80.

## Evaluation pipeline

One evaluation runs these stages:

```text
repository sync or local checkout
  -> deterministic substrate scan
  -> deterministic evidence pack
  -> GitHub or Azure DevOps API evidence
  -> independent Copilot judge panel
  -> deterministic consensus, floors, and ceilings
  -> composite report and CSV exports
```

The scanner and API collectors gather evidence. The judge panel may interpret
only that evidence pack; it does not browse the repository independently.
Missing evidence is treated conservatively as absent.

## 1. Substrate score: repository foundations

The substrate axis is deterministic. Seven repository pillars are assigned a
level from **L1 to L5** by ordered, first-match-wins rules.

| Pillar | Weight | Examples of evidence |
| --- | ---: | --- |
| Testing | 23% | Test files, test configuration, end-to-end tests, discoverable test command |
| Code Quality | 20% | Navigable structure, entry points, file size, coupling, modularity |
| Build System | 15% | Build configuration, CI, committed dependency lock files |
| Dev Environment | 12% | Dev container, setup scripts, environment template |
| Style & Validation | 12% | Linter, formatter, type checking, local validation hooks |
| Documentation | 10% | README, architecture docs, agent instructions, harness configuration, versioned specs |
| Security & Governance | 8% | Ignore rules for secrets, secret detection, static analysis |

Each pillar contributes:

```text
pillar points = (pillar level / 5) * pillar weight
substrate score = sum of all pillar points
```

For example, Testing at L4 contributes:

```text
(4 / 5) * 23 = 18.4 points
```

### Advanced-analysis adjustment

Selected deterministic analyses can adjust their related pillar after the base
rule is evaluated. An analysis score maps to a level adjustment:

| Analysis score | Adjustment |
| ---: | ---: |
| 75-100 | +1.0 |
| 65-74 | +0.5 |
| 45-64 | 0 |
| 20-44 | -0.5 |
| 0-19 | -1.0 |

The applicable adjustments are added to the base pillar level, rounded to an
integer, and clamped to L1-L5. The report preserves both the rule-assigned level
and the adjusted level so the change remains explainable.

The score maps to a substrate maturity level:

| Substrate score | Level |
| ---: | --- |
| 81-100 | L5 Autonomous |
| 61-80.99 | L4 Delegated |
| 41-60.99 | L3 Collaborative |
| 21-40.99 | L2 Assisted |
| 0-20.99 | L1 Manual |

There are also minimum-pillar safeguards:

- An L5 claim requires every pillar to be at least L3.
- An L4 claim requires every pillar to be at least L2.
- If the requirement is not met, the maturity level is reduced by one level.
  The numeric substrate score is not changed.

## 2. Operation score: agentic operating model

The operation axis uses the embedded rubric. Each judge scores nine dimensions
from **0 to 4**:

| Raw score | Meaning |
| ---: | --- |
| 0 | Absent |
| 1 | Ad hoc |
| 2 | Repeatable |
| 3 | Integrated |
| 4 | Verified autonomous or closed-loop |

Score 4 is deliberately difficult: it requires evidence of a bounded,
validated closed loop, not merely documentation, generic CI, scheduled jobs, or
the presence of an MCP configuration.

| Dimension | Weight | What it asks |
| --- | ---: | --- |
| Agent Interface & Operability | 9 | Can an agent understand, bootstrap, change, validate, and submit compliant work? |
| Automation Coverage | 13 | Is routine maintenance automated and machine-triggerable? |
| Self-Healing CI/CD | 18 | Can failures be detected, remediated, validated, and rolled back? |
| Continuous Improvement Loops | 13 | Are findings repeatedly converted into validated improvements or governed learned rules? |
| Agent Execution Surfaces | 9 | Are useful skills, MCP servers, CLIs, APIs, workflows, or sandboxes available to agents? |
| Policy, Safety, and Change Guardrails | 10 | Are automated changes bounded by enforced review, permissions, policies, and safe outputs? |
| Observability of Agentic Work | 9 | Are agent actions and outcomes visible through checks, labels, artifacts, audit trails, or dashboards? |
| Human-on-the-Loop Integration | 9 | Can humans supervise, approve, route, and handle exceptions? |
| Agentic Change Throughput | 10 | Is agent-attributable contribution measurable, recurring, validated, and material? |

Each dimension contributes:

```text
dimension points = (effective raw score / 4) * dimension weight
operation score = sum of all dimension points
```

For example, Self-Healing CI/CD at raw score 3 contributes:

```text
(3 / 4) * 18 = 13.5 points
```

The operation total maps to these tiers:

| Operation score | Tier |
| ---: | --- |
| 90-100 | Closed-Loop Autonomous |
| 75-89.99 | Agent-Integrated |
| 60-74.99 | Agent-Enabled |
| 40-59.99 | Bot-Assisted |
| 20-39.99 | Scripted |
| 0-19.99 | Manual |

## 3. How judge consensus is calculated

The default panel uses three Copilot models. Each model scores all nine
dimensions independently from the same evidence pack.

For each dimension, the evaluator then applies this deterministic sequence:

1. Take the median score across the contributing models.
2. Apply a minority veto when the lowest score is at least 2 points below the
   median. The adjusted center becomes the higher of the lowest score and
   `median - 1`, preventing one optimistic majority from hiding a strongly
   supported low assessment without letting one dissenting score dictate the
   result completely.
3. Apply evidence-derived floors. For example, verified PR validation
   establishes a minimum Self-Healing detection score, while agent
   instructions and machine-operable surfaces can establish minimum
   operability scores.
4. Apply evidence-derived ceilings. Agentic Change Throughput is capped when
   agent authorship is only metadata-based and lacks API-confirmed execution
   provenance.
5. Convert the resulting effective score to weighted points.

A dimension is marked **contested** when the model spread is at least 2 points
and the result was not already resolved by a floor or ceiling. The report keeps
each model's score and rationale instead of blending them into one explanation.

## 4. Composite score, quadrant, and rung

The final score uses a geometric mean:

```text
composite score = round(sqrt(substrate * operation), 1)
```

Example:

```text
substrate = 72
operation = 64
composite = sqrt(72 * 64) = 67.9
```

This repository is not AI-Ready because neither axis reaches 80, even though
the composite score is reasonably high.

The two 80-point axis thresholds also define a quadrant:

| Substrate | Operation | Quadrant |
| --- | --- | --- |
| >=80 | >=80 | Q1 AI-Ready |
| >=80 | <80 | Q2 Verifiable-but-manual |
| <80 | >=80 | Q3 Agentic-but-illegible |
| <80 | <80 | Q4 Manual |

The report also publishes a five-step composite rung:

| Rung | Name |
| ---: | --- |
| R1 | Manual |
| R2 | Legible |
| R3 | Verifiable |
| R4 | Delegable |
| R5 | Self-operating |

The rung is the lower of the substrate maturity level and the operation tier
mapped onto R1-R5. This is another guard against a strong result on one axis
masking weakness on the other.

## How to interpret the result

- **The score evaluates a repository system, not an individual engineer.**
- A high substrate score means agents can work in the repository reliably; it
  does not prove that agents are used in production workflows.
- A high operation score means agentic practices are evidenced; it does not
  compensate for poor tests, setup, documentation, or code navigability.
- Generic CI, Dependabot/Renovate, raw PR volume, design-only claims, and
  unverified AI labels are not treated as proof of closed-loop agentic
  operation.
- Scores describe the evaluated commit and evidence window. Local uncommitted
  changes are included when a local checkout is evaluated.
- API evidence is required by default because branch protection, merged PR
  attribution, and runtime pipeline behavior cannot be established reliably
  from files alone.
