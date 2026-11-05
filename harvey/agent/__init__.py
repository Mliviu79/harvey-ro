"""Harvey's agent: a model that reads Harvey's data through tools Harvey
serves it, answers in the dashboard's chat, and queues work under a plan
the user approved.

    readonly.py   what it may read: a read-only connection, the excluded
                  tables and columns, and the one SELECT a query() runs
    views.py      the count views, schema(), estimate() and jobs()
    tools.py      the MCP server the Claude CLI starts (python -m
                  harvey.agent.tools), with every call recorded
    chat.py       the chats the user keeps: a live Claude CLI process
                  for each one in use, each answer streamed as it is made
    plans.py      the plans it proposes and only the user decides, and
                  their budgets
    actions.py    what it may do under an approved plan: save a set, and
                  queue the runs the pages' buttons queue, the same way
    wakes.py      Harvey waking it under an approved plan (a job on the
                  agent's lane) when a run finishes or at a time it chose
    memory.py     its notes and playbooks, kept between conversations
    outreach.py   campaigns under a plan: an audience and a draft made the
                  Campaigns page's way, a start asked of the user, replies
                  counted and answered through the Handler for review, the
                  do-not-contact list added to, and what a campaign tells
                  its plan

No tool sends or approves email, starts or resumes a campaign, undoes a
do-not-contact entry, merges, changes a setting, or approves a plan. The model runs on the
operator's Claude plan (claude -p), with only Harvey's tools
(claude_runtime.build_agent_command).
"""
