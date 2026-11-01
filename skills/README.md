# Skills

Skills are editable Markdown files that give Harvey foundational sales knowledge. Each agent loads the skills it needs before making decisions.

## How Skills Work

When an agent runs, it calls `brain.load_skills_for_agent("agent_name")` which concatenates the relevant skill files and injects them into the prompt. This means you can change Harvey's behavior by editing these files — no code changes needed.

## Skill → Agent Mapping

| Skill | Writer | Handler | Sender |
|-------|--------|---------|--------|
| `signal_playbook.md` | x | | |
| `email_frameworks.md` | x | | x |
| `sales_methodology.md` | x | x | |
| `offer_strategy.md` | x | x | |
| `objection_handling.md` | | x | |
| `product_knowledge.md` | x | x | x |
| `competitive_intel.md` | x | x | |
| `selling_to_startups.md` | x* | x* | |

\* Only for prospects whose company is a startup by its own site's account (the `YC_BACKED` or `ACCELERATOR` signal, or a `FUNDING_ROUND` from the last 18 months), not loaded for everyone.

## Built-in Skills

- **`selling_to_startups.md`** — Writing to early-stage founders: who reads, timing around a YC or accelerator batch and Demo Day, writing to companies that just raised (and when a raise is too old to mention), what not to do, handling their replies. Used automatically for YC-backed, accelerator-backed and recently funded prospects.
- **`signal_playbook.md`** — Turning buying signals (hiring, tech stack, funding) into the email's first line; which signal wins when there are several; using a job posting as the hook without guessing or implying inside knowledge
- **`email_frameworks.md`** — Cold email copywriting rules, banned AI patterns, 2026 reply-rate data (length, offer CTAs, cadence, no tracking), 5 frameworks (AIDA, PAS, BAB, QVC, 3Ps)
- **`sales_methodology.md`** — ABC Loop, conversation flow, tone calibration, ethical guidelines
- **`offer_strategy.md`** — Offer ladder by engagement level, closing mechanics, timing rules
- **`objection_handling.md`** — LAARC framework, 4 objection categories with responses

## Auto-Generated Skills

These are created by `harvey train <url>` and are specific to your product:

- **`product_knowledge.md`** — Features, benefits, pricing, use cases, pain points, buying triggers
- **`competitive_intel.md`** — Battle cards for each competitor, differentiation angles, migration paths

## Adding or Editing Skills

1. Edit any `.md` file in this directory
2. Changes take effect on the next heartbeat cycle (no restart needed)
3. To add a new skill, create the file and add it to the `skill_map` in `harvey/brain.py`
