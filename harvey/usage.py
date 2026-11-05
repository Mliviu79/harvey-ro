"""Usage accounting.

Harvey's usage ledger is built entirely from its OWN Claude calls: every
`claude -p` invocation goes through the Brain (or the agent), which parses
the call's result and writes one usage_events row tagged with the agent and
task (see harvey/brain.py). That path is the single source of truth — it
never sees, and never records, your other Claude Code sessions.

(An earlier version also scanned every transcript in the Claude config
directory to "backfill" usage. That pulled in unrelated projects' sessions
and polluted Harvey's numbers, so it was removed: Harvey tracks only what
Harvey does. The plan's own usage gauge is harvey/integrations/quota.py,
from what the Claude CLI reports; Harvey never reads the Claude login.)
"""
