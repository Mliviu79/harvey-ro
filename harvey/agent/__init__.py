"""Harvey's agent: a model that reads Harvey's data through tools Harvey
serves it, and answers in the dashboard's chat.

    readonly.py   what it may read: a read-only connection, the excluded
                  tables and columns, and the one SELECT a query() runs
    views.py      the named views, schema(), estimate() and jobs()
    tools.py      the MCP server the Claude CLI starts (python -m
                  harvey.agent.tools), with every call recorded
    chat.py       the chat's messages, and the agent_turn job that runs
                  one turn of it

It can't act yet: no tool queues a job, spends, writes Harvey's data or
touches email. The model runs on the operator's Claude plan (claude -p),
with only Harvey's tools (claude_runtime.build_agent_command).
"""
