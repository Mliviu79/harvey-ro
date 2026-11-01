"""Harvey CLI — simple commands to install, setup, run, and manage Harvey."""

import argparse
import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path


MIN_PYTHON = (3, 11)


def _check_python_version():
    """Refuse to install on a Python that cannot run Harvey.

    `python3 -m venv .venv` on macOS builds the venv from /usr/bin/python3,
    which is 3.9 — old enough that Harvey's `X | Y` type syntax fails at
    import. The install itself appears to succeed and the failure surfaces
    later as an unrelated-looking ImportError, so check up front and name the
    fix.
    """
    if sys.version_info >= MIN_PYTHON:
        return

    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    want = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    print(f"\n  Harvey needs Python {want} or newer. This is Python {have}.")
    print(f"  ({sys.executable})\n")
    print("  On macOS, `python3` is usually the system 3.9, so a venv built")
    print("  with it is 3.9 too. Build the venv from a newer Python instead:\n")
    print("    brew install python@3.13")
    print("    rm -rf .venv")
    print("    $(brew --prefix)/bin/python3.13 -m venv .venv")
    print("    source .venv/bin/activate && pip install -e .\n")
    sys.exit(1)


def cmd_install(args):
    """Install Harvey's Python dependencies."""
    _check_python_version()
    print("\n  Installing Harvey dependencies...\n")

    # Install Python packages
    print("  Installing Python packages...")
    requirements = Path(args._project_root) / "requirements.txt"
    if requirements.exists():
        pip_args = ["-r", "requirements.txt"]
    else:
        # Fall back to an editable install from pyproject.toml
        pip_args = ["-e", "."]
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", *pip_args],
        cwd=args._project_root,
    )
    if result.returncode != 0:
        print("\n  Failed to install Python packages.")
        print("  Tip: make sure you're inside a virtualenv "
              "(python3 -m venv .venv && source .venv/bin/activate).")
        sys.exit(1)
    print("  ✓ Python packages installed.\n")

    _ensure_importable(args._project_root)
    print("  Harvey is installed. Run 'harvey setup' next.\n")


def _ensure_importable(project_root: str):
    """Make sure `import harvey` works outside the repo directory.

    Python 3.13 silently skips .pth files carrying the macOS 'hidden'
    file flag, and some Macs propagate that flag to everything inside
    dot-directories like .venv — which breaks editable installs. When
    that happens, fall back to symlinking the package into site-packages
    (imports don't check the hidden flag; only .pth parsing does).
    """
    check = subprocess.run(
        [sys.executable, "-c", "import harvey"],
        cwd="/", capture_output=True,
    )
    if check.returncode == 0:
        return

    try:
        import site
        site_packages = Path(site.getsitepackages()[0])
        link = site_packages / "harvey"
        target = Path(project_root) / "harvey"
        if not link.exists() and target.is_dir():
            link.symlink_to(target)
            recheck = subprocess.run(
                [sys.executable, "-c", "import harvey"],
                cwd="/", capture_output=True,
            )
            if recheck.returncode == 0:
                print("  ✓ Fixed package visibility (editable .pth was being "
                      "ignored; linked the package directly).\n")
                return
    except OSError as e:
        print(f"  Could not apply import fix: {e}")

    print("\n  Warning: 'import harvey' fails outside the project directory.")
    print("  Run harvey commands from the project root, or reinstall with:")
    print("    pip install -e . --config-settings editable_mode=compat\n")


def cmd_setup(args):
    """Run the interactive setup wizard."""
    from harvey.setup import run_setup

    asyncio.run(run_setup())


def cmd_run(args):
    """Start Harvey's heartbeat loop."""
    from harvey.main import main

    main()


def cmd_train(args):
    """Train Harvey on a website."""
    from harvey.trainer import Trainer

    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
        print(f"  No scheme given — using {url}")

    if args.max_pages < 1:
        print("  max_pages must be at least 1.")
        sys.exit(2)

    from pydantic import ValidationError
    from harvey.config import BrainConfig, ConfigError, load_brain_config

    try:
        brain_config = load_brain_config()
        overrides = {
            k: v for k, v in (
                ("provider", args.provider),
                ("model", args.model),
                ("reasoning_effort", args.reasoning_effort),
            ) if v is not None
        }
        if overrides:
            if "provider" in overrides and overrides["provider"] != brain_config.provider:
                # A different provider never inherits the other one's model.
                brain_config = BrainConfig(**overrides)
            else:
                brain_config = BrainConfig(**{**brain_config.model_dump(), **overrides})
    except (ConfigError, ValidationError) as e:
        print(f"  {e}")
        sys.exit(2)

    trainer = Trainer(brain_config=brain_config)
    asyncio.run(trainer.train(url, max_pages=args.max_pages))


def cmd_dashboard(args):
    """Launch the local web dashboard."""
    from harvey.dashboard import DashboardSetupError, dashboard_url, start_dashboard
    from harvey.dashboard_auth import TokenError

    if not 1 <= args.port <= 65535:
        print(f"  Invalid port: {args.port}. Must be 1-65535.")
        sys.exit(2)

    try:
        if getattr(args, "url", False):
            # The unlock link for an already-running dashboard.
            print(dashboard_url(host=args.host, port=args.port))
            return
        start_dashboard(host=args.host, port=args.port)
    except (DashboardSetupError, TokenError) as e:
        print(f"\n  Dashboard not started: {e}\n")
        sys.exit(2)


def cmd_status(args):
    """Show current pipeline status."""
    from harvey.state import StateManager

    async def _status():
        state = StateManager()
        await state.init_db()
        summary = await state.get_state_summary()

        print("\n  Harvey Pipeline Status")
        print("  " + "=" * 40)
        print(f"  Prospects:            {summary['prospects']}")
        print(f"  Ready to write for:   {summary.get('writable_prospects', 0)} "
              "(new, with a verified or catch-all email)")
        write_failed = (summary.get("prospects") or {}).get("write_failed", 0)
        if write_failed:
            print(f"  Couldn't write for:   {write_failed} (the model's drafts kept coming back "
                  "unusable; `harvey retry-writes` tries again)")
        print(f"  Draft campaigns:      {summary['draft_campaigns']}")
        print(f"  Active campaigns:     {summary['active_campaigns']}")
        print(f"  Open conversations:   {summary['open_conversations']}")
        by_provider = summary.get("usage_today_by_provider", {})
        print(f"  Claude calls today:   {by_provider.get('claude', summary['usage_today'])}")
        print(f"  Codex calls today:    {by_provider.get('codex', 0)}")
        from harvey.config import compliance_notice, load_config, sending_notice
        loaded = None
        try:
            loaded = load_config()
            notice = sending_notice(loaded)
            address_notice = compliance_notice(loaded)
        except Exception:
            # Status must still print when harvey.yaml is broken; that's
            # when people run it. The --check path reports config errors.
            notice = address_notice = ""
        from harvey.main import unusable_backlog_notice
        backlog = unusable_backlog_notice(summary)
        if backlog:
            print("  Writing:              NOTHING USABLE")
            print(textwrap.fill(backlog, width=72, initial_indent="                        ",
                                subsequent_indent="                        "))
        if notice:
            print("  Sending:              HELD")
            print(textwrap.fill(notice, width=72, initial_indent="                        ",
                                subsequent_indent="                        "))
        hold = await state.current_sends_held()
        if hold:
            from harvey.sending_mode import held_message
            waiting = len(await state.get_outbox(status="approved", limit=500))
            _, detail = held_message(hold, waiting=waiting)
            print("  Sending:              NOT SENDING (couldn't read your inbox)")
            print(textwrap.fill(detail, width=72, initial_indent="                        ",
                                subsequent_indent="                        "))
        from harvey.integrations.precleared import current_hold, hold_message
        precleared_hold = await current_hold(state, loaded)
        if precleared_hold:
            message = hold_message(precleared_hold)
            print(f"  Precleared:           HOLDING COLD EMAIL since {precleared_hold.get('since', '?')}")
            print(textwrap.fill(f"{message['title']}. {message['detail']}", width=72,
                                initial_indent="                        ",
                                subsequent_indent="                        "))
        if address_notice:
            print("  Postal address:       MISSING")
            print(textwrap.fill(address_notice, width=72, initial_indent="                        ",
                                subsequent_indent="                        "))
        from harvey.brain import BRAIN_STATUS_KEY, Brain, active_brain_status
        from harvey.config import ConfigError, load_brain_config
        try:
            brain_config = load_brain_config()
        except ConfigError as e:
            brain_config, config_error = None, e
        # Only the configured provider's health: an old Codex pause says
        # nothing once harvey.yaml has switched to Claude.
        brain_status = active_brain_status(
            await state.get_setting(BRAIN_STATUS_KEY),
            brain_config.provider if brain_config else "")
        healthy = True
        if getattr(args, "check", False):
            if brain_config is None:
                print(f"  Brain:                config error: {config_error}")
                healthy = False
            else:
                brain = Brain(state, brain_config)
                problem = await brain.preflight()
                label = brain.provider_label + (f" ({brain.model})" if brain.model else "")
                if problem:
                    healthy = False
                    print(f"  Brain:                {label} NOT READY")
                    print(f"                        {problem}")
                elif brain.provider == "codex":
                    print(f"  Brain:                {label} ready (CLI, isolation, login, "
                          "model and account checked; no prompt sent)")
                else:
                    print(f"  Brain:                {label} CLI isolation flags present "
                          "(login not checked: Claude is checked by its first call)")
        elif brain_status.get("status") == "paused":
            label = {"claude": "Claude", "codex": "Codex"}.get(brain_status.get("provider"), "Brain")
            print(f"  Brain:                {label} PAUSED since {brain_status.get('since', '?')}")
            print(f"                        {brain_status.get('reason', '')}")
        print()
        return healthy

    if not asyncio.run(_status()):
        # `harvey status --check` failing is something scripts can act on.
        raise SystemExit(1)


def cmd_usage(args):
    """Show brain usage: active provider's quota, totals, breakdowns."""
    from harvey.state import StateManager
    from harvey.config import ConfigError, load_brain_config

    async def _usage():
        state = StateManager()
        await state.init_db()

        try:
            brain_config = load_brain_config()
        except ConfigError as e:
            print(f"  {e}")
            brain_config = None

        # Live quota for the ACTIVE provider only (best-effort). Each
        # subscription's gauge is read from its own source and labelled as
        # such; one is never shown as the other.
        windows = None
        provider = brain_config.provider if brain_config else None
        try:
            if provider == "codex":
                from harvey.codex import CodexClient, CodexQuotaClient
                windows = await CodexQuotaClient(
                    CodexClient(model=brain_config.model)).get_utilization()
            elif provider == "claude":
                from harvey.integrations.quota import QuotaClient
                windows = await QuotaClient().get_utilization()
        except Exception:
            pass

        label = {"claude": "Claude", "codex": "Codex"}.get(provider, "Brain")
        print(f"\n  {label} Usage")
        print("  " + "=" * 52)
        if windows:
            labels = {"five_hour": "5-hour window", "seven_day": "Weekly",
                      "limit_reached": "Usage limit"}
            for key, w in windows.items():
                resets = f"  (resets {w['resets_at']})" if w.get("resets_at") else ""
                print(f"  {labels.get(key, key):<16} {w['utilization']:5.1f}% used{resets}")
        elif provider == "codex":
            print("  Quota gauge unavailable (check 'codex login', the Codex CLI "
                  "version, and network).")
        elif provider == "claude":
            print("  Quota gauge unavailable (run 'claude login' or check network).")

        totals = await state.usage_totals()
        print()
        print(f"  {'Period':<10} {'Calls':>7} {'Input':>12} {'Output':>10} {'Cache read':>12}")
        for label, key in (("Today", "today"), ("7 days", "week"), ("30 days", "month")):
            t = totals.get(key) or {}
            print(
                f"  {label:<10} {t.get('calls', 0):>7} "
                f"{t.get('input_tokens', 0):>12,} {t.get('output_tokens', 0):>10,} "
                f"{t.get('cache_read_tokens', 0):>12,}"
            )

        by_provider = await state.usage_by_provider(days=args.days)
        if len(by_provider) > 1 or (by_provider and by_provider[0]["provider"] != "claude"):
            print(f"\n  By provider (last {args.days} days):")
            for row in by_provider:
                print(
                    f"    {row['provider']:<14} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )

        by_agent = await state.usage_by_agent(days=args.days)
        if by_agent:
            print(f"\n  By agent (last {args.days} days):")
            for row in by_agent:
                print(
                    f"    {row['agent']:<14} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )

        by_task = await state.usage_by_task(days=args.days)
        if by_task:
            print(f"\n  By task (last {args.days} days):")
            for row in by_task[:10]:
                print(
                    f"    {row['task']:<22} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )
        print(
            "\n  Subscription plans aren't billed per token — these are usage"
            "\n  counts, not costs.\n"
        )

    asyncio.run(_usage())


def cmd_export(args):
    """Export the prospect list as a sequencer-ready CSV."""
    from harvey.state import StateManager
    from harvey.export import export_companies_csv, export_prospects_csv

    async def _export():
        state = StateManager()
        await state.init_db()

        if args.companies:
            out = args.out or "companies.csv"
            count, _ = await export_companies_csv(state, out_path=out)
            print(f"\n  Exported {count} companies to {out}  (one row per business, "
                  "with its best contact where one was found)")
            from harvey.export import collect_companies, source_notes
            ids = [r["id"] for r in await collect_companies(state) if r.get("id")]
            for note in await source_notes(state, ids):
                print(f"  Source: {note}")
            print()
            return

        args.out = args.out or "prospects.csv"
        email_statuses = None
        if args.email_status:
            email_statuses = [s.strip() for s in args.email_status.split(",") if s.strip()]
        statuses = None
        if args.status:
            statuses = [s.strip() for s in args.status.split(",") if s.strip()]

        count, _ = await export_prospects_csv(
            state,
            out_path=args.out,
            email_statuses=email_statuses,
            min_score=args.min_score,
            statuses=statuses,
            include_all=args.all,
        )
        scope = "all prospects" if args.all else (
            f"email status {email_statuses or ['verified', 'risky']}"
            + (f", score >= {args.min_score}" if args.min_score else "")
        )
        print(f"\n  Exported {count} prospect(s) to {args.out}  ({scope})")
        if count == 0 and not args.all:
            print("  Tip: no deliverable emails yet? Add a REOON_API_KEY to .env so")
            print("  Harvey can verify addresses, or use --all for the raw list.\n")
        else:
            print("  The CSV imports directly into Instantly, Smartlead, or any sequencer.\n")

    asyncio.run(_export())


def cmd_gmail(args):
    """Gmail provider utilities (auth / test)."""
    from harvey.config import load_env

    env = load_env()
    if args.gmail_action == "auth":
        from harvey.integrations.gmail import run_auth_flow
        ok = run_auth_flow(env.gmail_client_id, env.gmail_client_secret)
        sys.exit(0 if ok else 1)

    if args.gmail_action == "test":
        from harvey.config import load_config
        from harvey.integrations.gmail import GmailProvider

        async def _test():
            provider = GmailProvider(load_config(), env)
            ok, detail = await provider.test_connection()
            print(f"\n  {'✓' if ok else '✗'} {detail}\n")
            sys.exit(0 if ok else 1)
        asyncio.run(_test())


def cmd_microsoft(args):
    """Microsoft 365 provider utilities (auth / test)."""
    from harvey.config import load_env

    env = load_env()
    if args.microsoft_action == "auth":
        from harvey.integrations.microsoft import run_auth_flow
        ok = run_auth_flow(env.m365_client_id, env.m365_tenant_id)
        sys.exit(0 if ok else 1)

    if args.microsoft_action == "test":
        from harvey.config import load_config
        from harvey.integrations.microsoft import MicrosoftProvider

        async def _test():
            provider = MicrosoftProvider(load_config(), env)
            ok, detail = await provider.test_connection()
            print(f"\n  {'✓' if ok else '✗'} {detail}\n")
            sys.exit(0 if ok else 1)
        asyncio.run(_test())


def cmd_outbox(args):
    """Review and approve queued outgoing emails."""
    from harvey.state import StateManager

    async def _outbox():
        state = StateManager()
        await state.init_db()

        if args.approve_all:
            n = await state.approve_outbox()
            print(f"\n  Approved {n} email(s). They'll send on schedule.")
            held = await state.count_held_for_review()
            if held:
                print(f"  {held} email(s) Harvey held for review were left out: read each one,")
                print("  then approve it with --approve <id>.")
            print()
            return
        if args.approve:
            n = await state.approve_outbox(args.approve)
            print(f"\n  {'Approved.' if n else 'No pending item with that id.'}\n")
            return
        if args.reject:
            n = await state.reject_outbox_item(args.reject)
            print(f"\n  {'Rejected.' if n else 'Nothing waiting to send with that id (already sending or sent?).'}\n")
            return

        paused = await state.get_setting("sending_paused")
        if paused:
            print(f"\n  ⚠ SENDING PAUSED: {paused}")
            print("  Resume with: harvey sending resume")

        pending = await state.get_outbox(status="pending_review", limit=50)
        approved = await state.get_outbox(status="approved", limit=10)
        print(f"\n  Outbox — {len(pending)} awaiting approval, "
              f"{len(approved)}+ approved/scheduled")
        print("  " + "=" * 60)
        for item in pending:
            print(f"\n  [{item['id']}] step {item['step']} ({item['kind']}) "
                  f"→ {item['to_email']}  (send {item['send_at'][:16]})")
            print(f"  Subject: {item['subject']}")
            body_preview = (item["body"][:200] + "...") if len(item["body"]) > 200 else item["body"]
            for line in body_preview.splitlines():
                print(f"    {line}")
        if pending:
            print("\n  Approve: harvey outbox --approve <id>   |   all: harvey outbox --approve-all")
            print("  Reject:  harvey outbox --reject <id>\n")
        else:
            print("  Nothing awaiting review.\n")

    asyncio.run(_outbox())


def cmd_profile(args):
    """Read what companies' own websites say about them. Free, no Claude calls.
    Each company is a job on the website reader's lane."""
    from harvey.state import StateManager

    async def _profile():
        from harvey.modules import jobs
        from harvey.modules.job_kinds import enqueue_site_reads

        state = StateManager()
        await state.init_db()

        due = await state.companies_needing_profile(limit=args.limit or -1,
                                                    stale_days=args.stale_days)
        if not due:
            print("\n  Nothing to profile. Every company with a website has "
                  "been read recently.\n")
            return
        batch_id = await jobs.new_batch(state, "site_read", "Read sites")
        ids, dupes = await enqueue_site_reads(state, [c["id"] for c in due], batch_id=batch_id,
                                              stale_days=args.stale_days)
        print(f"\n  {len(due)} companies need profiling: {len(ids)} queued"
              + (f", {len(dupes)} already queued" if dupes else "") + "...\n")
        if getattr(args, "background", False) or not ids:
            await _queued_note(state, batch_id, len(ids))
            return
        found = await _wait_for(state, batch_id)
        read = [j for j in found if j.status == jobs.DONE]
        observations = sum(int(j.result.get("observations") or 0) for j in read)
        print(f"  Read {len(read)} sites → {observations} observations (free)")
        _job_failures(found)
        print("\n  See what turned up: harvey dashboard → Signals\n")

    asyncio.run(_profile())


async def _print_finder_status(state) -> None:
    from harvey import finder

    b = await finder.backlog(state)
    waiting = b["jobs"].get("queued", 0) + b["jobs"].get("running", 0)
    print(f"\n  Companies with no contact:    {b['without_contact']:,}")
    print(f"    with a website to read:     {b['site_to_read']:,}  (read in the background, free)")
    print(f"    looked at by the finder:    {b['searched']:,}")
    day = b["last_day"]
    print(f"  Last 24 hours:                {day['companies']:,} companies, "
          f"{day['found']:,} contacts found, ${day['cost']:.4f}")
    print(f"  Finder jobs waiting:          {waiting:,}\n")


def cmd_contacts(args):
    """The contact finder: every company with no contact is a record to
    search (harvey/finder.py). Writes and sends nothing but contacts."""
    from harvey.state import StateManager

    async def _contacts():
        from harvey import finder
        from harvey.config import load_env
        from harvey.modules import jobs, switches
        from harvey.modules.job_kinds import enqueue_contact_search

        state = StateManager()
        await state.init_db()
        if args.action == "status":
            await _print_finder_status(state)
            return
        from harvey.contacts import ContactFilter

        f = ContactFilter(county=args.county, locality=args.locality, website=args.website,
                          q=args.q)
        require = [c.strip().upper() for c in args.require.split(",") if c.strip()]
        exclude = [c.strip().upper() for c in args.exclude.split(",") if c.strip()]
        if require or exclude:
            f.cohort = state._verdict_sql(require, exclude, [], 0.0)
        selected = await finder.select(state, f, force=args.force, limit=args.limit)
        if not selected:
            print("\n  Nothing to search: every company that matches has a contact "
                  "already (--force searches them again).\n")
            return
        est = finder.estimate(selected, await switches.enabled_ids(state),
                              load_env().model_dump())
        print(f"\n  {est['companies']:,} companies: {est['with_site']:,} with a website to "
              f"read (free), {est['to_look_up']:,} to look up by name and town")
        if est["to_look_up"] and not args.max_spend:
            print("  No --max-spend: the website searches are skipped (nothing is paid for).")
        elif est["to_look_up"]:
            print(f"  Website searches: about ${est['estimated_cost']:.4f} "
                  f"(cap: ${args.max_spend:.2f}; at most ${est['most']:.4f} when the "
                  "cheapest finds nothing and the next is tried)")
        try:
            batch_id, ids, dupes = await enqueue_contact_search(
                state, [c["id"] for c in selected], max_spend=args.max_spend, force=args.force)
        except jobs.Duplicate as e:
            print(f"\n  {e}\n")
            raise SystemExit(2)
        if dupes:
            print(f"  {len(dupes):,} already waiting, not queued twice")
        if args.background:
            await _queued_note(state, batch_id, len(ids))
            return
        found = await _wait_for(state, batch_id)
        done = [j for j in found if j.kind == "find_contacts" and j.status == jobs.DONE]
        contacts_found = sum(int(j.result.get("contacts") or 0) for j in done)
        cost = sum(float(j.result.get("cost") or 0.0) for j in done)
        print(f"\n  Looked at {len(done):,} companies → {contacts_found:,} contacts found"
              f" (${cost:.4f})")
        _job_failures(found)
        print("  See them: the dashboard's Contacts page, or harvey export --all\n")

    asyncio.run(_contacts())


def cmd_caen(args):
    """What CAEN classes mean: a code's name in every revision, or the
    classes some words point to. A reference, not a filter: to find
    companies, harvey search. Offline, no Claude."""
    from harvey.caen import lookup, revision_label, search

    words = " ".join(args.words)
    tokens = words.replace(",", " ").split()
    if tokens and all(t.isdigit() and len(t) <= 4 for t in tokens):
        matches = [m for t in tokens for m in lookup(t)]
        if not matches:
            print(f"\n  No CAEN class {words}.\n")
            return
    else:
        matches = search(words, limit=args.limit)
    if not matches:
        print(f"\n  No CAEN class matches \"{words}\". Try another word, in "
              "English or Romanian (e.g. dentist, farmacie, laborator).\n")
        return
    print()
    for m in matches:
        name = f"{m.name_ro} ({m.name_en})" if m.name_en else m.name_ro
        old = "  (pre-2008, old registrations)" if m.version < 2 else ""
        print(f"  {m.code}  {revision_label(m.version):<5}  {name}{old}")
        if m.why != "code":
            print(f"               {m.why}")
    print(f"\n  To find companies doing this: harvey search {words}\n")


def _activity_lines(activities: list[dict], show_all: bool = False) -> tuple[list[str], int]:
    """One line per class ("8623 — Activități de asistență stomatologică
    (Dental practice)  Rev.2/3"), the matched ones only when any matched.
    Returns the lines and how many classes were left out."""
    from harvey.caen import revision_label

    chosen = activities if show_all or not any(a.get("matched") for a in activities) \
        else [a for a in activities if a.get("matched")]
    lines: dict[tuple[str, str], list[int]] = {}
    for a in chosen:
        name = a.get("name") or ""
        if a.get("name_en"):
            name = f"{name} ({a['name_en']})" if name else a["name_en"]
        lines.setdefault((a["code"], name), []).append(a["version"])
    out = []
    for (code, name), versions in lines.items():
        labels = sorted(versions)
        rev = ("Rev." + "/".join(str(v) for v in labels)) if set(labels) <= {2, 3} \
            else "/".join(revision_label(v) for v in labels)
        out.append(f"{code} — {name}  {rev}" if name else f"{code}  {rev}")
    shown_codes = {a["code"] for a in chosen}
    return out, len({a["code"] for a in activities} - shown_codes)


def cmd_search(args):
    """Companies on file, by what they do (CAEN code or words) and where,
    searched locally. No Claude, no network."""
    import json as _json

    from harvey.state import StateManager

    words = " ".join(args.words)

    async def _search():
        state = StateManager()
        await state.init_db()
        total, rows = await state.search_companies(words, limit=args.limit,
                                                   offset=args.offset)
        if args.json:
            print(_json.dumps({"query": words, "total": total, "offset": args.offset,
                               "companies": rows}, ensure_ascii=False, indent=2))
            return
        if not rows:
            print(f"\n  No company on file matches \"{words}\". Search by name, "
                  "activity (in English or Romanian, or a CAEN code like 8623) or town.\n")
            return
        print(f"\n  {total} compan{'y' if total == 1 else 'ies'} match \"{words}\""
              + (f", showing {args.offset + 1}-{args.offset + len(rows)}"
                 if total > len(rows) else "") + "\n")
        for row in rows:
            ident = f"  CUI {row['cui']}" if row.get("cui") else ""
            print(f"  {row['name']}{ident}")
            details = [x for x in (row.get("location"), row.get("phone"),
                                   row.get("website") or row.get("domain")) if x]
            if details:
                print("    " + " · ".join(details))
            lines, more = _activity_lines(row.get("activities") or [])
            for line in lines:
                print(f"    {line}")
            if more:
                print(f"    + {more} other activit{'y' if more == 1 else 'ies'}")
            if not lines and row.get("industry"):
                print(f"    {row['industry']}")
            print()

    asyncio.run(_search())


def _registry_path(args) -> Path:
    from harvey.registry import REGISTRY_PATH
    return Path(getattr(args, "registry", "") or REGISTRY_PATH)


def _onrc_dir(args) -> Path:
    from harvey.paths import PROJECT_ROOT
    return Path(getattr(args, "data_dir", "") or PROJECT_ROOT / "data" / "onrc")


def _print_registry_status(meta: dict | None, path: Path):
    if not meta:
        print(f"\n  No registry loaded at {path}. harvey registry load\n")
        return
    size = int(meta.get("bytes", 0)) / 1e9
    print(f"\n  {meta.get('dataset')} (registry as of {meta.get('date')})")
    print(f"  loaded {meta.get('loaded_at')} UTC in {meta.get('load_seconds')} s, "
          f"{size:.2f} GB at {meta.get('path')}")
    print(f"  {int(meta.get('firms', 0)):,} registrations, "
          f"{int(meta.get('operating', 0)):,} operating")
    print(f"  {int(meta.get('activities', 0)):,} authorised activities (CAEN classes)")
    if meta.get("with_administrators") == "1":
        print(f"  {int(meta.get('administrators', 0)):,} administrators (name and role)")
    else:
        print("  no administrators (loaded without the representatives file)")
    print(f"  Source: ONRC open data, {meta.get('page')} (CC-BY 4.0)\n")


def _print_registry_rows(total: int, rows: list[dict], words: str, offset: int,
                         more: bool = False):
    if not rows:
        print(f"\n  No registry firm matches \"{words}\". Search by name, activity "
              "(dentist, farmacie, 8623), county (BV, Brașov) or town.\n")
        return
    print(f"\n  {'More than ' if more else ''}{total:,} firm{'' if total == 1 else 's'} "
          f"match \"{words}\""
          + (f", showing {offset + 1}-{offset + len(rows)}" if total > len(rows) else "")
          + "\n")
    for row in rows:
        ident = f"  CUI {row['cui']}" if row.get("cui") else ""
        print(f"  {row['name']}{ident}  {row['reg']}")
        place = ", ".join(x for x in (row.get("address"), row.get("locality"),
                                      row.get("county")) if x)
        details = [x for x in (place, row.get("website"),
                               None if row.get("operating") else row.get("status")) if x]
        if details:
            print("    " + " · ".join(details))
        figures = _figures_line(row.get("financials"))
        if figures:
            print(f"    {figures}")
        lines, more = _activity_lines(row.get("activities") or [])
        for line in lines[:4]:
            print(f"    {line}")
        more += max(0, len(lines) - 4)
        if more:
            print(f"    + {more} other activit{'y' if more == 1 else 'ies'}")
        print()


def _print_pull_report(report, where: str = ""):
    if report.shared_domain:
        print(f"  {report.shared_domain} list a website another business on file already "
              "has: kept as separate records, possible matches to review")
    if report.catch_all_only:
        print(f"  {report.catch_all_only} only on the old catch-all code 8690 Rev.2, so "
              "may be other health activities")
    if report.excluded:
        print(f"  {report.excluded} skipped — on your do-not-contact list")
    verb = "would be" if report.dry_run else "are"
    print(f"  {report.new_companies} {verb} new, {report.known_companies} already on file"
          + ("" if report.dry_run else f" ({report.updated_companies} filled in)"))
    print(f"  {report.administrators} administrators named")
    if not report.dry_run:
        print(f"  {report.activities} activities (CAEN classes) recorded, "
              "searchable with harvey search")
        print(f"  {report.observations} observations recorded")
    for code in report.unconfirmed:
        print(f"    {code} not confirmed, so not recorded "
              f"(harvey signals --confirm {code})")
    for err in report.errors[:5]:
        print(f"    ! {err}")


def _amount(raw: str) -> float:
    """A number as typed: 1000000, 1.5M, 500k, 2,5M (lei or people)."""
    text = (raw or "").strip().lower().replace(" ", "").replace("_", "")
    scale = 1
    if text[-1:] in ("k", "m"):
        scale = 1_000 if text[-1] == "k" else 1_000_000
        text = text[:-1]
    if text.count(",") == 1 and "." not in text:
        text = text.replace(",", ".")
    try:
        return float(text.replace(",", "")) * scale
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r} (1000000, 1.5M, 500k)")


def _fin_filter_args(p):
    g = p.add_argument_group(
        "financial statements (the latest year on file; harvey financials load)")
    g.add_argument("--min-turnover", type=_amount, metavar="LEI",
                   help="Net turnover at least this, in lei (1M, 500k)")
    g.add_argument("--max-turnover", type=_amount, metavar="LEI",
                   help="Net turnover at most this, in lei")
    g.add_argument("--min-employees", type=_amount, metavar="N",
                   help="At least this many employees (yearly average)")
    g.add_argument("--max-employees", type=_amount, metavar="N",
                   help="At most this many employees")
    g.add_argument("--unknown", dest="unknown_figures",
                   choices=("exclude", "include"), default="exclude",
                   help="Firms whose bounded figure is missing, turnover or headcount alike "
                        "(no statement, a sole trader, a new firm, an empty headcount cell): "
                        "left out (exclude, the default) or kept (include)")


def _yes_no(raw: str) -> bool:
    text = (raw or "").strip().lower()
    if text in ("yes", "da", "y", "true", "1"):
        return True
    if text in ("no", "nu", "n", "false", "0"):
        return False
    raise argparse.ArgumentTypeError(f"yes or no, not {raw!r}")


def _tax_filter_args(p):
    g = p.add_argument_group("the taxpayer file (harvey taxpayers load)")
    g.add_argument("--employer", type=_yes_no, metavar="yes|no",
                   help="Registered for wage tax (an employer), or explicitly not")
    g.add_argument("--vat", type=_yes_no, metavar="yes|no", help="VAT registered, or not")
    g.add_argument("--micro", type=_yes_no, metavar="yes|no",
                   help="On the micro-enterprise income tax, or not")


def _tax_filter(args):
    """The --employer/--vat/--micro conditions, or None. Refused (before
    anything is read) while the taxpayer file's module is off."""
    from harvey.modules.ro.mf_taxpayers.store import Filter

    tax = Filter(employer=getattr(args, "employer", None), vat=getattr(args, "vat", None),
                 micro=getattr(args, "micro", None))
    if not tax.active():
        return None
    _require_module("mf_taxpayers", "Filtering by employer, VAT or micro-enterprise tax")
    return tax


def _fin_filter(args):
    """The --min/--max turnover and employees bounds, or None. Refused
    (before anything is read) while the financial statements module is off."""
    from harvey.modules.ro.mf_financials.store import Filter

    fin = Filter(min_turnover=getattr(args, "min_turnover", None),
                 max_turnover=getattr(args, "max_turnover", None),
                 min_employees=getattr(args, "min_employees", None),
                 max_employees=getattr(args, "max_employees", None),
                 unknown=getattr(args, "unknown_figures", "exclude") or "exclude")
    if not fin.active():
        return None
    _require_module("mf_financials", "Filtering by turnover or employees")
    return fin


def _fin_display(con) -> None:
    """Show each firm's latest filed figures beside it when the statements
    are loaded and their module is on."""
    from harvey.modules.ro.mf_financials import store

    try:
        if store.FINANCIALS_PATH.is_file() and "mf_financials" in asyncio.run(_modules_on()):
            store.attach(con)
    except Exception:
        pass


def _figures_line(fin: dict | None) -> str:
    if not fin:
        return ""
    parts = []
    if fin.get("turnover") is not None:
        parts.append(f"turnover {fin['turnover']:,} lei")
    if fin.get("employees") is not None:
        parts.append(f"{fin['employees']:,} employees")
    return f"{fin.get('year')} ({fin.get('form')}): " + ", ".join(parts) if parts else ""


# registry pull copies at most this many firms unless --limit says how many.
PULL_WITHOUT_LIMIT = 10_000


def _pull_progress(dry_run: bool):
    verb = "checked" if dry_run else "saved"
    return lambda done, total: print(f"  {verb} {done:,} of {total:,}...", flush=True)


def _filter_params(flt) -> dict | None:
    from dataclasses import asdict

    return asdict(flt) if flt is not None else None


async def _run_pull(state, args, params: dict, label: str = ""):
    """Queue a registry pull, then wait for it (and the checks it queues
    for the firms it saved). None when it was only queued; SystemExit when
    it failed before saving anything."""
    from harvey.modules import jobs

    batch_id = await jobs.new_batch(state, "registry_pull", label or "Registry pull")
    try:
        await jobs.enqueue(state, "registry_pull", "onrc", params, module="onrc",
                           capability="registry_search", label=label or "Registry pull",
                           batch_id=batch_id)
    except jobs.Duplicate as e:
        print(f"\n  {e}\n")
        raise SystemExit(2)
    if getattr(args, "background", False):
        await _queued_note(state, batch_id, 1)
        return None
    found = await _wait_for(state, batch_id)
    pull = next((j for j in found if j.kind == "registry_pull"), None)
    if pull is None or pull.status != jobs.DONE:
        code = _job_failures(found)
        print()
        raise SystemExit(code or 1)
    return found


def _pull_report(found):
    """The pull's report (an onrc.ImportReport) from its job's result."""
    from harvey.collectors.onrc import ImportReport

    pull = next(j for j in found if j.kind == "registry_pull")
    data = pull.result.get("report") or {}
    return ImportReport(**{k: v for k, v in data.items()
                           if k in ImportReport.__dataclass_fields__})


def _print_follow_ups(found) -> None:
    """The checks a pull queued for the firms it saved (ANAF's), added up."""
    from harvey.modules import jobs
    from harvey.modules.runs import _new_check_report

    checks = [j for j in found if j.kind == "id_check"]
    for module in dict.fromkeys(j.module for j in checks):
        total = _new_check_report(module)
        for j in checks:
            if j.module != module:
                continue
            if j.status == jobs.FAILED:
                total["errors"].append(j.error)
                continue
            for key, value in j.result.items():
                if isinstance(value, int) and not isinstance(value, bool) and key in total:
                    total[key] += value
            total["unconfirmed"] = j.result.get("unconfirmed") or total["unconfirmed"]
            total["stopped"] = j.result.get("stopped") or total["stopped"]
        waiting = sum(1 for j in checks if j.module == module and j.status not in jobs.FINISHED)
        _print_check_report(total)
        if waiting:
            print(f"    {waiting} batch{'' if waiting == 1 else 'es'} still waiting: the worker "
                  f"finishes {'it' if waiting == 1 else 'them'} (harvey jobs list)")


def cmd_registry(args):
    """The whole trade registry in data/registry.db: load it once per
    dataset, search it, pull chosen firms into Harvey. No Claude."""
    import json as _json

    from harvey import registry

    path = _registry_path(args)
    action = args.registry_action

    if action == "status":
        meta = registry.status(path)
        if args.json:
            print(_json.dumps(meta, ensure_ascii=False, indent=2))
        else:
            _print_registry_status(meta, path)
        return
    # Before the download (load fetches ~1.5 GB) or any read of it.
    _require_module("onrc", f"harvey registry {action}")

    if action == "load":
        async def _load():
            from harvey.modules import jobs
            from harvey.state import StateManager

            print(f"\n  ONRC open data: {args.dataset} dataset")
            state = StateManager()
            await state.init_db()
            batch_id = await jobs.new_batch(state, "registry_load", f"Registry {args.dataset}")
            try:
                await jobs.enqueue(state, "registry_load", "onrc", {
                    "path": str(path), "data_dir": str(_onrc_dir(args)),
                    "dataset": args.dataset, "with_people": args.administrators == "all",
                    "force": args.force}, dedupe_key="registry_load", module="onrc",
                    capability="registry_sync", label=f"Load {args.dataset}",
                    batch_id=batch_id)
            except jobs.Duplicate as e:
                print(f"\n  {e}\n")
                raise SystemExit(2)
            if args.background:
                await _queued_note(state, batch_id, 1)
                return
            found = await _wait_for(state, batch_id)
            code = _job_failures(found)
            if code:
                print()
                raise SystemExit(code)
            meta = next((j.result.get("registry") for j in found if j.status == jobs.DONE), None)
            _print_registry_status(registry.status(path) or meta, path)
        asyncio.run(_load())
        return

    words = " ".join(args.words)
    try:
        con = registry.connect(path)
    except FileNotFoundError:
        print(f"\n  No registry loaded at {path}. Run harvey registry load first "
              "(downloads ~1.5 GB once per dataset).\n")
        raise SystemExit(2)

    fin = _fin_filter(args)
    tax = _tax_filter(args)
    _fin_display(con)
    if action == "search":
        try:
            found = registry.search(con, words, all_statuses=args.all_statuses,
                                    limit=args.limit, offset=args.offset, fin=fin, tax=tax)
        except FileNotFoundError as e:
            print(f"\n  {e} (the filter needs it).\n")
            raise SystemExit(2)
        if args.json:
            print(_json.dumps({"query": words, "total": found["total"],
                               "more": found["more"], "offset": args.offset,
                               "firms": found["rows"]}, ensure_ascii=False, indent=2))
        else:
            _print_registry_rows(found["total"], found["rows"], words, args.offset,
                                 found["more"])
        return

    if action == "pull":
        from harvey.state import StateManager

        q = registry.resolve(con, words, all_statuses=args.all_statuses, fin=fin, tax=tax)
        if not q.match:
            print("\n  Say what to pull: words, a CAEN code, a county (BV) or a town.\n")
            raise SystemExit(2)
        try:
            registry.count(con, q, 1)
        except FileNotFoundError as e:
            print(f"\n  {e} (the filter needs it).\n")
            raise SystemExit(2)
        if args.limit is None and not args.dry_run:
            n = registry.count(con, q, PULL_WITHOUT_LIMIT + 1)
            if n > PULL_WITHOUT_LIMIT:
                print(f"\n  More than {PULL_WITHOUT_LIMIT:,} firms match \"{words}\". "
                      "Narrow the words, or say how many with --limit N.\n")
                raise SystemExit(2)

        async def _pull():
            state = StateManager()
            await state.init_db()
            meta = registry.status(path) or {}
            with_admins = args.administrators == "all"
            if with_admins and meta.get("with_administrators") != "1":
                print("  (this registry was loaded without administrators: "
                      "harvey registry load --force)")
            found = await _run_pull(state, args, {
                "path": str(path), "words": words, "all_statuses": args.all_statuses,
                "fin": _filter_params(fin), "tax": _filter_params(tax),
                "limit": args.limit, "with_administrators": with_admins,
                "dry_run": args.dry_run}, label=words)
            if found is None:
                return
            report = _pull_report(found)
            print(f"\n  {report.in_area:,} firm{'' if report.in_area == 1 else 's'} "
                  f"matching \"{words}\" from {meta.get('dataset')}"
                  + (f" (first {args.limit:,})" if args.limit else ""))
            _print_pull_report(report)
            _print_follow_ups(found)
            print("\n  Source: ONRC open data, data.gov.ro (CC-BY 4.0)")
            if report.dry_run:
                print("  Dry run: nothing was saved.\n")
            elif report.errors:
                raise SystemExit(1)
            else:
                print("  Next: harvey search, or harvey contacts (reads their sites)\n")

        asyncio.run(_pull())


def cmd_import_onrc(args):
    """Companies (and their administrators) from Romania's trade registry,
    by area and activity: a shortcut for harvey registry load (when the
    latest dataset isn't loaded yet) and harvey registry pull."""
    from harvey import registry
    from harvey.collectors import onrc
    from harvey.state import StateManager

    async def _import():
        try:
            names = onrc.counties(args.county)
        except ValueError as e:
            print(f"\n  {e} (Brașov, Brasov, BV; Bucharest is B).\n")
            raise SystemExit(2)
        county = ", ".join(names) if names else "all of Romania"
        classes = onrc.parse_classes(args.activity or [], args.codes)
        if (args.activity or args.codes.strip()) and not classes:
            print("\n  No CAEN class matches that activity. Try harvey caen <words>, "
                  "or give --codes 8623.\n")
            raise SystemExit(2)
        localities = onrc._localities(args.locality)
        # Nothing narrowing it at all is every operating firm in Romania
        # (about 1.8 million): fine on purpose, not by a forgotten flag.
        narrowed = classes or names or localities or fin or tax
        if not (narrowed or args.dry_run or args.whole_country):
            print("\n  With no activity, area or filter this is every operating "
                  "company in Romania (about 1.8 million, roughly an hour to "
                  "import). Add --dry-run to count, or --whole-country to import "
                  "them all.\n")
            raise SystemExit(2)
        with_admins = args.administrators == "all"

        path = _registry_path(args)
        state = StateManager()
        await state.init_db()
        where = f"{args.locality}, {county}" if args.locality else county
        what = ", ".join(onrc.class_labels(classes)) if classes else "every activity"
        print(f"\n  Looking for {what} in {where}"
              f"{'' if args.include_inactive else ' (operating companies only)'}...")
        found = await _run_pull(state, args, {
            "path": str(path), "data_dir": str(_onrc_dir(args)), "ensure_latest": True,
            "words": "", "classes": [list(c) for c in classes or []],
            "counties": names or [], "localities": localities,
            "all_statuses": args.include_inactive, "fin": _filter_params(fin),
            "tax": _filter_params(tax), "with_administrators": with_admins,
            "dry_run": args.dry_run, "area": where}, label=f"{what} in {where}")
        if found is None:
            return
        pull = next((j for j in found if j.kind == "registry_pull"), None)
        meta = (pull.result.get("meta") if pull else None) or registry.status(path) or {}
        print(f"\n  ONRC open data: {meta.get('dataset')} (registry as of {meta.get('date')})")
        if with_admins and meta.get("with_administrators") != "1":
            print("  (this registry was loaded without administrators: "
                  "harvey registry load --force)")
        report = _pull_report(found)
        print(f"  {report.in_area:,} match\n")
        _print_pull_report(report)
        _print_follow_ups(found)
        print("\n  Source: ONRC open data, data.gov.ro (CC-BY 4.0)")
        if report.dry_run:
            print("  Dry run: nothing was saved. Re-run without --dry-run to import.\n")
        elif report.errors:
            raise SystemExit(1)
        else:
            print("  Next: harvey contacts (reads the sites of those with one)\n")

    _require_module("onrc", "harvey import-onrc")
    fin = _fin_filter(args)
    tax = _tax_filter(args)
    asyncio.run(_import())


FIN_MODULE = "mf_financials"


def _print_financials_status(meta: dict | None, updates: list[dict], checked_at: str,
                             settings: dict):

    if not meta:
        print("\n  No financial statements loaded. harvey financials load")
    else:
        size = int(meta.get("bytes", 0)) / 1e6
        print(f"\n  Financial statements for {meta.get('years', '').replace(' ', ', ')}")
        print(f"  loaded {meta.get('loaded_at')} UTC in {meta.get('load_seconds')} s, "
              f"{size:.0f} MB at {meta.get('path')}")
        print(f"  {int(meta.get('rows', 0)):,} yearly statements, "
              f"{int(meta.get('companies', 0)):,} companies")
        for f in meta.get("files") or []:
            print(f"    {f['year']} {f['form']}: {f['rows']:,} companies, published "
                  f"{(f.get('published') or '')[:10]} ({f.get('licence')})")
    if checked_at:
        print(f"  data.gov.ro last checked {checked_at} UTC")
    for u in updates:
        print(f"    {u['year']} {u['form']}: {u['why']}")
    if updates:
        print("  Load them: harvey financials load")
    print(f"  Keeps the latest {settings.get('years', 3)} years; checks every "
          f"{settings.get('check_days', 30)} days"
          + ("; updates on its own" if settings.get("auto_update") else
             " when asked (automatic updates are off)"))
    print(f"  Source: {_attribution(FIN_MODULE)}\n")


def cmd_financials(args):
    """The Ministry of Finance's annual statements in data/financials.db:
    load the latest years, show what is loaded, ask whether newer is out.
    No Claude."""
    import json as _json

    from harvey.modules import switches
    from harvey.modules.ro.mf_financials import source, store
    from harvey.modules.ro.mf_financials.capabilities import updates as pending
    from harvey.state import StateManager

    action = args.financials_action

    async def _settings():
        state = StateManager()
        await state.init_db()
        return state, await switches.module_settings(state, FIN_MODULE)

    if action == "status":
        state, settings = asyncio.run(_settings())
        meta = store.status()
        checked_at, found = source.load_catalog()
        todo = pending(found, meta, int(settings.get("years") or 3)) if found else []
        if args.json:
            print(_json.dumps({"loaded": meta, "checked_at": checked_at, "updates": todo,
                               "settings": settings}, ensure_ascii=False, indent=2))
        else:
            _print_financials_status(meta, todo, checked_at, settings)
        return
    _require_module(FIN_MODULE, f"harvey financials {action}")

    if action == "check":
        async def _check():
            state, settings = await _settings()
            found = await _run_source(state, args, "source_check", FIN_MODULE,
                                      "statements_check", "Financial statements: check")
            if found is None:
                return
            values = found[0].result.get("values") or {}
            checked_at, _ = source.load_catalog()
            _print_financials_status(store.status(), values.get("updates") or [],
                                     checked_at, settings)
        asyncio.run(_check())
        return

    async def _load():
        state, settings = await _settings()
        if args.years:
            years = 30 if args.years.strip().lower() == "all" else args.years
            try:
                settings["years"] = await switches.set_module_setting(
                    state, FIN_MODULE, "years", years)
            except ValueError as e:
                print(f"\n  {e}\n")
                raise SystemExit(2)
        print(f"\n  Ministry of Finance statements: the latest {settings['years']} "
              "published years (downloads only what is missing, ~90 MB a year)...",
              flush=True)
        found = await _run_source(state, args, "source_sync", FIN_MODULE, "statements_sync",
                                  "Financial statements: load", force=args.force)
        if found is None:
            return
        result = found[0].result
        values = result.get("values") or {}
        if values.get("offline"):
            print(f"  {values['offline']}: loaded the files already downloaded")
        if values.get("unchanged"):
            print("  Nothing new on data.gov.ro; already loaded (--force loads again).")
        checked_at, _ = source.load_catalog()
        _print_financials_status(values.get("financials") or store.status(), [], checked_at,
                                 settings)
        print(f"  {result.get('companies', 0):,} companies on file got figures, "
              f"{result.get('observations', 0):,} observations recorded")
        for module, codes in (result.get("unconfirmed") or {}).items():
            if module == FIN_MODULE and codes:
                print(f"    not recorded, not confirmed: {', '.join(codes)} "
                      f"(harvey signals --confirm {','.join(codes)})")
        for err in (result.get("errors") or [])[:3]:
            print(f"    ! {err}")
        print()

    asyncio.run(_load())


async def _run_source(state, args, kind_: str, module: str, capability: str, label: str,
                      force: bool = False):
    """Queue a load or check of a source's local copy and wait for it. None
    when only queued; SystemExit when it failed."""
    from harvey.modules import jobs
    from harvey.modules.job_kinds import enqueue_source

    try:
        batch_id, _ = await enqueue_source(state, kind_, module, capability, force=force,
                                           label=label)
    except jobs.Duplicate as e:
        print(f"\n  {e}\n")
        raise SystemExit(2)
    if getattr(args, "background", False):
        await _queued_note(state, batch_id, 1)
        return None
    found = await _wait_for(state, batch_id)
    if not found or found[0].status != jobs.DONE:
        print()
        code = _job_failures(found)
        print()
        raise SystemExit(code or 1)
    return found


TAX_MODULE = "mf_taxpayers"
ANAF_MODULE = "anaf"


def _print_taxpayers_status(meta: dict | None, change: dict | None, checked_at: str,
                            settings: dict):

    if not meta:
        print("\n  No taxpayer file loaded. harvey taxpayers load")
    else:
        size = int(meta.get("bytes", 0)) / 1e6
        print(f"\n  Taxpayer identification file {meta.get('year')} "
              f"(published {(meta.get('published') or '')[:10]})")
        print(f"  loaded {meta.get('loaded_at')} UTC in {meta.get('load_seconds')} s, "
              f"{size:.0f} MB at {meta.get('path')}")
        print(f"  {int(meta.get('rows', 0)):,} taxpayers: {int(meta.get('with_phone', 0)):,} "
              f"with a phone, {int(meta.get('vat', 0)):,} VAT registered, "
              f"{int(meta.get('employers', 0)):,} employers, "
              f"{int(meta.get('micro', 0)):,} on the micro-enterprise tax")
    if checked_at:
        print(f"  data.gov.ro last checked {checked_at} UTC")
    if change:
        print(f"  {change['year']}: {change['why']} on data.gov.ro. Load it: harvey taxpayers load")
    print(f"  Checks every {settings.get('check_days', 30)} days"
          + ("; updates on its own" if settings.get("auto_update") else
             " when asked (automatic updates are off)"))
    print(f"  Source: {_attribution(TAX_MODULE)}\n")


def cmd_taxpayers(args):
    """The Ministry of Finance's taxpayer identification file in
    data/taxpayers.db: load the newest, show what is loaded, ask whether a
    newer one is out. No Claude."""
    import json as _json

    from harvey.modules import switches
    from harvey.modules.ro.mf_taxpayers import source, store
    from harvey.modules.ro.mf_taxpayers.capabilities import update as pending
    from harvey.state import StateManager

    action = args.taxpayers_action

    async def _settings():
        state = StateManager()
        await state.init_db()
        return state, await switches.module_settings(state, TAX_MODULE)

    if action == "status":
        state, settings = asyncio.run(_settings())
        meta = store.status()
        checked_at, found = source.load_catalog()
        change = pending(found, meta)
        if args.json:
            print(_json.dumps({"loaded": meta, "checked_at": checked_at, "update": change,
                               "settings": settings}, ensure_ascii=False, indent=2))
        else:
            _print_taxpayers_status(meta, change, checked_at, settings)
        return
    _require_module(TAX_MODULE, f"harvey taxpayers {action}")

    if action == "check":
        async def _check():
            state, settings = await _settings()
            found = await _run_source(state, args, "source_check", TAX_MODULE,
                                      "taxpayers_check", "Taxpayer file: check")
            if found is None:
                return
            values = found[0].result.get("values") or {}
            checked_at, _ = source.load_catalog()
            _print_taxpayers_status(store.status(), values.get("update"), checked_at,
                                    settings)
        asyncio.run(_check())
        return

    async def _load():
        state, settings = await _settings()
        print("\n  Ministry of Finance taxpayer file: the newest year (downloads only what "
              "is missing, ~870 MB)...", flush=True)
        found = await _run_source(state, args, "source_sync", TAX_MODULE, "taxpayers_sync",
                                  "Taxpayer file: load", force=args.force)
        if found is None:
            return
        result = found[0].result
        values = result.get("values") or {}
        if values.get("offline"):
            print(f"  {values['offline']}: loaded the files already downloaded")
        if values.get("unchanged"):
            print("  Nothing new on data.gov.ro; already loaded (--force loads again).")
        checked_at, _ = source.load_catalog()
        _print_taxpayers_status(values.get("taxpayers") or store.status(), None, checked_at,
                                settings)
        print(f"  {result.get('companies', 0):,} companies on file got readings, "
              f"{result.get('observations', 0):,} observations recorded")
        for module, codes in (result.get("unconfirmed") or {}).items():
            if codes:
                print(f"    {module}: not recorded, not confirmed: {', '.join(codes)} "
                      f"(harvey signals --confirm {','.join(codes)})")
        for err in (result.get("errors") or [])[:3]:
            print(f"    ! {err}")
        print()

    asyncio.run(_load())


def _attribution(module_id: str) -> str:
    """The credit a module's data asks for, as its manifest gives it."""
    from harvey.modules import switches

    m = switches.by_id().get(module_id)
    return (m.attribution or m.name or m.id) if m else module_id


def _module_name(module_id: str) -> str:
    from harvey.modules import switches

    m = switches.by_id().get(module_id)
    return (m.name or m.id) if m else module_id


def _print_check_report(r: dict) -> None:
    name = _module_name(r["module"])
    print(f"  {name}: {r['companies']:,} compan{'y' if r['companies'] == 1 else 'ies'} "
          f"with a CUI; {r['checked']:,} asked in {r['requests']:,} request"
          f"{'' if r['requests'] == 1 else 's'}, {r['cached']:,} from answers kept, "
          f"{r['not_found']:,} unknown to it" + (f", {r['invalid']:,} with an invalid CUI"
                                                  if r["invalid"] else ""))
    print(f"  {r['observations']:,} observations recorded on {r['updated']:,} companies"
          + (f"; {r['phones_set']:,} got a phone" if r["phones_set"] else ""))
    if r["unconfirmed"]:
        print(f"    not recorded, not confirmed: {', '.join(r['unconfirmed'])} "
              f"(harvey signals --confirm {','.join(r['unconfirmed'])})")
    if r["stopped"] == "unconfirmed":
        print("    nothing asked: none of its signals is confirmed")
    elif r["stopped"] == "busy":
        print("    stopped: the service stayed busy or couldn't be reached; "
              "harvey anaf check --missing finishes later")
    elif r["stopped"]:
        print(f"    stopped: {r['stopped']}")
    for err in r["errors"][:3]:
        print(f"    ! {err}")


def cmd_anaf(args):
    """ANAF's public web service for the companies on file: VAT and
    e-Factura registration, fiscal inactivity, status, legal form, address
    and phone. One request a second, 100 companies a request; answers are
    kept for the module's cache_days. No Claude."""
    from harvey.modules.ro.anaf import service
    from harvey.state import StateManager

    if args.anaf_action == "status":
        kept = service.status()
        print(f"\n  ANAF answers kept: {kept['answers']:,} ({kept['found']:,} known to ANAF)"
              + (f", newest {kept['latest']}" if kept["latest"] else ""))
        print("  Check companies: harvey anaf check --missing | --all-on-file | --cui N\n")
        return
    _require_module(ANAF_MODULE, "harvey anaf check")
    cuis = [c for raw in args.cui or [] for c in raw.replace(",", " ").split()]
    if not cuis and not args.all_on_file and not args.missing:
        print("\n  Say which companies: --cui N (one or more), --missing (never checked) "
              "or --all-on-file.\n")
        raise SystemExit(2)

    async def _check():
        from harvey.modules import jobs
        from harvey.modules.job_kinds import enqueue_id_checks
        from harvey.modules.runs import _new_check_report

        state = StateManager()
        await state.init_db()
        ids = None
        if cuis:
            wanted = {service.clean_cui(c) or c for c in cuis}
            rows = await state.companies_with_id("cui")
            ids = [r["id"] for r in rows if r["value"] in wanted]
            on_file = {r["value"] for r in rows if r["value"] in wanted}
            for c in sorted(wanted - on_file):
                print(f"  {c}: no company on file has this CUI (import it first)")
            if not ids:
                print()
                raise SystemExit(2)
        batch_id = await jobs.new_batch(state, "id_check", "ANAF",
                                        params={"missing": args.missing})
        queued, dupes = await enqueue_id_checks(state, ANAF_MODULE, "company_check", ids,
                                                missing_only=args.missing, batch_id=batch_id)
        if dupes:
            print(f"  {len(dupes)} batch{'' if len(dupes) == 1 else 'es'} of these companies "
                  f"already queued (job {dupes[0].id}): not asked again")
        if args.background:
            await _queued_note(state, batch_id, len(queued))
            return
        print("\n  Asking ANAF (one request a second, up to 100 companies each)...", flush=True)
        found = await _wait_for(state, batch_id) if queued else []
        report = _new_check_report(ANAF_MODULE)
        report["companies"] = sum(len(j.params.get("company_ids") or []) for j in found)
        for job in found:
            if job.status == jobs.FAILED:
                report["errors"].append(job.error)
                continue
            for key, value in job.result.items():
                if isinstance(value, int) and not isinstance(value, bool) and key in report \
                        and key != "companies":
                    report[key] += value
            report["unconfirmed"] = job.result.get("unconfirmed") or report["unconfirmed"]
            report["stopped"] = job.result.get("stopped") or report["stopped"]
            if job.status == jobs.QUEUED and job.error:
                report["stopped"] = report["stopped"] or "busy"
        _print_check_report(report)
        print(f"  Source: {_attribution(ANAF_MODULE)}\n")
        if report["errors"] and not report["updated"] and not report["cached"]:
            raise SystemExit(1)

    asyncio.run(_check())


# ── Jobs: a command queues its work, then waits for it ──

def _background_arg(p):
    p.add_argument("--background", action="store_true",
                   help="Queue it and return at once: the dashboard's worker (or "
                        "harvey worker) runs it; follow it with harvey jobs list")


async def _queued_note(state, batch_id: str, n: int) -> None:
    from harvey.modules.worker import lease_holder

    print(f"\n  Queued: {n} job{'' if n == 1 else 's'} (run {batch_id}). "
          "Follow it on the dashboard's Queue page, or: harvey jobs list")
    if await lease_holder(state) is None:
        print("  No worker is running now: start the dashboard, or harvey worker.")
    print()


async def _wait_for(state, batch_id: str):
    """Wait for a run's jobs, printing their progress (run here when no
    worker is running, followed otherwise). Returns its jobs."""
    from harvey.modules.worker import wait_for_batch

    return await wait_for_batch(state, batch_id, say=lambda line: print(line, flush=True))


def _job_failures(found) -> int:
    """Print why jobs failed or were held; the exit status they call for."""
    from harvey.modules import jobs

    code = 0
    for job in found:
        if job.status == jobs.FAILED:
            print(f"  ! {job.error}" if job.kind not in ("discover", "site_read")
                  else f"  ! {job.label}: {job.error}")
            code = max(code, int(job.result.get("exit_code") or 1))
        elif job.status == jobs.PAUSED and job.paused_reason != jobs.SPEND_LIMIT:
            print(f"  {job.label or job.kind}: paused ({job.paused_reason or job.error}). "
                  f"harvey jobs resume {job.id}")
        elif job.status == jobs.QUEUED and job.error:
            print(f"  {job.label or job.kind}: waiting ({job.error})")
    return code


def cmd_discover(args):
    """Find businesses. The only stage that spends money, so it estimates first."""
    from harvey.state import StateManager
    from harvey.config import load_config, load_env
    from harvey.collectors.discover import (PROVIDERS, build_queries, is_discovery_source,
                                            preset_problem, provider_menu)
    from harvey.modules import switches
    from harvey.modules.runs import run_discovery

    async def _discover():
        enabled = await _modules_on()
        if args.providers:
            print("\n  Discovery providers")
            print("  " + "=" * 68)
            for m in provider_menu(load_env().model_dump(), enabled):
                mark = "ready" if m["configured"] else "needs setup"
                if not m["enabled"]:
                    mark = f"off: harvey modules enable {m['module']}"
                if m.get("lookup_only"):
                    mark += "; website lookups only, not a Discover source"
                print(f"\n  {m['label']}  [{m['key']}]  ({mark})")
                print(f"    {m['blurb']}")
                print(f"    Cost: {m['cost_note']}")
                print(f"    Free: {m['free_tier']}")
                if m["env_keys"]:
                    print(f"    Needs: {', '.join(m['env_keys'])} in .env  —  {m['signup_url']}")
                if m["caveat"]:
                    print(f"    Note: {m['caveat']}")
            print("\n  Run one with: harvey discover --provider <key> --estimate\n")
            return

        keys = list(dict.fromkeys(k.strip() for k in args.provider.split(",") if k.strip()))
        for key in keys:
            if key not in PROVIDERS:
                print(f"\n  Unknown provider {key!r}. See: harvey discover --providers\n")
                return
            if not is_discovery_source(key):
                from harvey.modules.job_kinds import lookup_only_message

                print(f"\n  {lookup_only_message(key)}.\n")
                raise SystemExit(2)
            off = switches.discovery_problem(enabled, key, getattr(args, "preset", "") or "")
            if off:
                print(f"\n  {off}\n")
                raise SystemExit(2)

            # Fail before collection; estimates need no credentials.
            # An unconfigured provider used to end in one quiet "stopped
            # early" line and exit code 0: a silent no-op that reads as
            # success.
            provider = PROVIDERS[key]
            missing = provider.missing_keys(load_env().model_dump())
            if missing and not args.estimate:
                names = ", ".join(k.upper() for k in missing)
                print("\n  " + "!" * 68)
                print(f"  ERROR: provider skipped: missing key - "
                      f"{provider.label} is not configured, 0 queries can run.")
                print(f"  Missing or empty: {names}")
                print("  Put a real value in .env (template placeholders don't"
                      " count), then retry.")
                print("  All providers and their keys: harvey discover --providers")
                print("  " + "!" * 68 + "\n")
                raise SystemExit(1)

        config = load_config()
        state = StateManager()
        await state.init_db()

        # Semicolons, not commas: "Denver, CO" is one city, not two.
        cities = [c.strip() for c in args.city.split(";") if c.strip()] or None
        preset = getattr(args, "preset", "") or ""
        plan, total = [], 0.0
        for key in keys:
            problem = preset_problem(preset, key, config=config)
            if problem:
                if len(keys) == 1:
                    print(f"\n  {problem}\n")
                    sys.exit(2)
                # One source of several that can't run this preset is left
                # out, saying so; the others run.
                print(f"\n  {PROVIDERS[key].label} left out: {problem}")
                continue
            provider = PROVIDERS[key]
            queries = build_queries(config, cities=cities,
                                    depth=args.depth, limit=args.limit, preset=preset)

            # --estimate asks a source that can count how many results each
            # search has (a small paid request each), so the price is exact.
            report = await run_discovery(state, config, key, queries,
                                         max_spend=args.max_spend, dry_run=True,
                                         count=args.estimate)
            print(f"\n  {len(queries)} queries via {provider.label}")
            for q in queries[:8]:
                print(f"    - {q.keyword()}" + ("" if q.coordinate else
                      "   (no coordinates — add icp.geo_coordinates for radius search)"))
            if len(queries) > 8:
                print(f"    ... and {len(queries) - 8} more")
            if report.counted:
                print(f"\n  {report.available:,} results in all"
                      f"   (counting them cost ${report.count_cost:.4f})")
            elif provider.can_count:
                print("\n  Not counted yet: each search is priced at one full page."
                      "\n  harvey discover --estimate counts them for the exact price.")
            for err in report.errors[:3]:
                print(f"    ! {err}")
            total += report.estimated_cost
            plan.append({"provider": key, "queries": queries})
        if not plan:
            print("\n  No source can run this preset.\n")
            sys.exit(2)
        print(f"\n  Estimated cost: ${total:.4f}   (cap: ${args.max_spend:.2f}"
              f"{', shared by every source' if len(plan) > 1 else ''})")
        find_websites = (None if getattr(args, "no_find_websites", False)
                         or getattr(args, "no_profile", False)
                         else getattr(args, "websites_max_spend", 0.0))
        if find_websites is not None:
            from harvey import finder

            found = finder.searchers(enabled, load_env().model_dump())
            free = sorted({s.module for s in found if s.estimate <= 0})
            paid = [s for s in found if s.estimate > 0]
            print(f"  Websites for companies found with none: up to ${find_websites:.2f} of "
                  "paid searches"
                  + (f" (from ${min(s.estimate for s in paid):.4f} a lookup)" if paid else "")
                  + (f"; free through {', '.join(free)}" if free else "")
                  + ("" if found else "; no search module that finds a website is set up"))
            if find_websites:
                print(f"  Run's cap, searches and websites together: "
                      f"${args.max_spend + find_websites:.2f}")
        if total > args.max_spend:
            print(f"  More than the cap: the run stops when ${args.max_spend:.2f} is "
                  "spent and says what was left.")

        if args.estimate:
            print("\n  Estimate only. Re-run without --estimate to collect.\n")
            return

        from harvey.modules import jobs
        from harvey.modules.job_kinds import enqueue_sources, merge_reports

        try:
            batch_id, ids, dupes = await enqueue_sources(
                state, plan, max_spend=args.max_spend, read_sites=not args.no_profile,
                find_websites=find_websites,
                label=" + ".join(PROVIDERS[part["provider"]].label for part in plan))
        except jobs.Duplicate as e:
            print(f"\n  {e}\n")
            raise SystemExit(2)
        for job in dupes:
            print(f"  Already queued, not asked again: {job.label} (job {job.id})")
        if getattr(args, "background", False):
            await _queued_note(state, batch_id, len(ids))
            return
        print("\n  Running...\n")
        found = await _wait_for(state, batch_id)
        searches = [j for j in found if j.kind == "discover"]
        sites = [j for j in found if j.kind == "site_read"]
        r = merge_reports([j.progress.get("report") or j.result for j in searches])
        ran = r.get("queries_ran", 0)
        print(f"  DISCOVER — {r.get('found', 0)} results across "
              f"{ran} of {len(searches)} queries")
        print(f"    {r.get('new_companies', 0)} new companies, "
              f"{r.get('known_companies', 0)} already known")
        print(f"    {r.get('junk', 0)} filtered out as directories/aggregators")
        # Only when it happened: a "0 skipped" line every run teaches people to
        # stop reading the block, and this is the line worth noticing.
        if r.get("excluded"):
            print(f"    {r['excluded']} skipped — on your do-not-contact list")
        print(f"    {r.get('observations', 0)} observations recorded")
        print(f"    actual cost: ${r.get('actual_cost', 0):.4f}")
        looking = [j for j in found if j.kind == "find_contacts"
                   and j.params.get("pool") == "websites"]
        if looking:
            print(f"    websites looked up for {len(looking)} companies found with none: "
                  f"{sum(1 for j in looking if (j.result or {}).get('website'))} found, "
                  f"${sum(float(j.actual_cost or 0) for j in looking):.4f}")
        batch = await jobs.get_batch(state, batch_id) or {}
        deferred = int((batch.get("params") or {}).get("websites_deferred") or 0)
        if deferred:
            print(f"    {deferred} more companies with no website are left to the background "
                  "search")
        limited = [j for j in searches if j.status == jobs.PAUSED
                   and j.paused_reason == jobs.SPEND_LIMIT]
        if r.get("stopped"):
            print(f"    stopped early: {r['stopped']}")
        if r.get("left_behind"):
            print(f"    {r['left_behind']}")
        if limited:
            # Paused, not lost: each carries on from its page.
            print(f"    {len(limited)} search{'' if len(limited) == 1 else 'es'} paused at the "
                  f"spend limit; to carry on from where they stopped: "
                  f"harvey jobs resume {limited[0].id} --max-spend N")

        if args.no_profile:
            print("\n  CONTACTS — skipped (--no-profile)")
        else:
            looked = [j for j in found if j.kind == "find_contacts" and j.status == jobs.DONE]
            read = [j for j in sites if j.status == jobs.DONE]
            print(f"\n  CONTACTS — {len(looked) + len(read)} companies looked at, "
                  f"{sum(int(j.result.get('contacts') or 0) for j in looked)} "
                  "contacts found")

        for err in r.get("errors", [])[:5]:
            print(f"    ! {err}")
        _job_failures([j for j in found if j.kind != "discover"])
        for job in searches:
            if job.status == jobs.FAILED:
                print(f"    ! {job.label}: {job.error}")

        # Empty successful searches are valid; no successful requests is not.
        # Partial success remains usable and exits zero.
        if r.get("queries_succeeded", 0) == 0:
            print("\n  " + "=" * 68)
            print(f"  DISCOVERY COMPLETED NO QUERIES ({r.get('stopped') or 'no queries succeeded'})"
                  " — exiting non-zero")
            for err in (r.get("errors") or [])[:3]:
                print(f"    - {err}")
            print("  " + "=" * 68 + "\n")
            raise SystemExit(1)

        print("\n  Next: harvey dashboard → Signals → cohort builder\n")

    asyncio.run(_discover())


def _job_line(job) -> str:
    """One job as `harvey jobs list` prints it."""
    from harvey.modules import jobs

    progress = job.progress
    bits = []
    if progress.get("pages"):
        bits.append(f"{progress['pages']} page{'' if progress['pages'] == 1 else 's'}")
    if progress.get("results"):
        bits.append(f"{progress['results']} results")
    if job.actual_cost:
        bits.append(f"${job.actual_cost:.4f}")
    if job.status == jobs.PAUSED:
        bits.append(f"paused: {job.paused_reason or job.error}")
    elif job.status == jobs.FAILED:
        bits.append(f"failed: {job.error}")
    elif job.status == jobs.QUEUED and job.error:
        bits.append(f"tries again{f' after {job.not_before} UTC' if job.not_before else ''}: "
                    f"{job.error}")
    label = job.label or job.kind.replace("_", " ")
    return f"    {job.id}  {job.status:<9} {label}" + (f"  ({'; '.join(bits)})" if bits else "")


def cmd_jobs(args):
    """The queue: what runs, waits, paused or failed, per lane; cancel,
    retry or resume a job; pause or resume a lane. No Claude."""
    from harvey.modules import jobs
    from harvey.modules.worker import lease_holder
    from harvey.state import StateManager

    action = args.jobs_action or "list"
    if action != "list" and not args.target:
        what = "a lane" if action in ("pause", "resume-lane") else "a job id"
        print(f"\n  Say which: harvey jobs {action} <{what}> (harvey jobs list shows them).\n")
        raise SystemExit(2)

    async def _go():
        state = StateManager()
        await state.init_db()
        if action == "list":
            counts = await jobs.counts(state)
            paused = await jobs.paused_lanes(state)
            holder = await lease_holder(state)
            print(f"\n  Jobs: {counts.get('running', 0)} running, {counts.get('queued', 0)} "
                  f"waiting, {counts.get('paused', 0)} paused, {counts.get('failed', 0)} failed")
            print("  Worker: " + (f"running (pid {holder.get('pid')} on {holder.get('host')})"
                                  if holder else "none running (start the dashboard, or "
                                                 "harvey worker)"))
            lanes = sorted(set(counts["lanes"]) | set(paused))
            if args.lane:
                lanes = [lane for lane in lanes if lane == args.lane]
            shown = (jobs.RUNNING, jobs.QUEUED, jobs.PAUSED, jobs.FAILED) if not args.all \
                else jobs.STATUSES
            for lane in lanes:
                n = counts["lanes"].get(lane, {})
                print(f"\n  {lane}" + (f"  [paused: {paused[lane]}]" if lane in paused else "")
                      + "  " + ", ".join(f"{v} {k}" for k, v in sorted(n.items())))
                found = await jobs.list_jobs(state, lane=lane, statuses=shown, limit=args.limit)
                # Running first, then in the order the worker takes them.
                for job in sorted(reversed(found), key=lambda j: j.status != jobs.RUNNING):
                    print(_job_line(job))
            print()
            return
        if action in ("pause", "resume-lane"):
            if not await jobs.known_lane(state, args.target):
                print(f"\n  No lane {args.target!r} (harvey jobs list shows them).\n")
                raise SystemExit(2)
            if action == "pause":
                await jobs.pause_lane(state, args.target)
                print(f"\n  Lane {args.target} paused: nothing new starts on it, and a running "
                      f"job stops after its page. harvey jobs resume-lane {args.target}\n")
            else:
                await jobs.resume_lane(state, args.target)
                print(f"\n  Lane {args.target} resumed.\n")
            return
        job = await jobs.find(state, args.target)
        if job is None:
            print(f"\n  No job {args.target!r} (harvey jobs list shows their ids).\n")
            raise SystemExit(2)
        try:
            if action == "cancel":
                job = await jobs.cancel(state, job.id)
                said = ("stops after the page it is on" if job.status == jobs.RUNNING
                        else "cancelled")
            elif action == "retry":
                job = await jobs.retry(state, job.id)
                said = "queued again, from where it got to"
            else:
                if args.max_spend is not None and job.batch_id:
                    await jobs.set_batch_cap(state, job.batch_id, args.max_spend,
                                             pool=(job.params or {}).get("pool") or "")
                    for other in await jobs.list_jobs(state, statuses=(jobs.PAUSED,),
                                                      batch_id=job.batch_id, limit=100000):
                        if other.id != job.id and other.paused_reason == jobs.SPEND_LIMIT:
                            await jobs.resume(state, other.id)
                job = await jobs.resume(state, job.id)
                said = "resumed: it carries on from where it stopped"
        except (jobs.Refused, jobs.Duplicate) as e:
            print(f"\n  {e}\n")
            raise SystemExit(2)
        print(f"\n  Job {job.id} ({job.label or job.kind}): {said}.")
        if job.status == jobs.QUEUED:
            if await lease_holder(state) is None:
                print("  No worker is running, so it waits until one starts: open the "
                      "dashboard, or run harvey worker.")
        print()

    asyncio.run(_go())


def _dollars(text: str) -> float:
    """A spend cap from the command line: a finite number, 0 or more."""
    import argparse
    import math

    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} isn't a number of dollars (0 or more)")
    return value


def cmd_worker(args):
    """Run the jobs (searches, site reads, loads, checks) on an install
    without the dashboard. One worker per database: with the dashboard
    running, this one waits until it stops."""
    import logging
    import signal

    from harvey.modules.worker import serve
    from harvey.state import StateManager

    async def _go():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        print("\n  Harvey's worker: running queued jobs (Ctrl-C stops it; a running job "
              "goes back to the queue and resumes from its page).\n", flush=True)
        await serve(StateManager(), stop)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s",
                        datefmt="%H:%M:%S")
    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        pass


def cmd_signals(args):
    """Review the signal vocabulary — Harvey proposes, you confirm.

    Nothing is collected until a signal is confirmed, so a fresh install
    prospects against nothing until someone makes these decisions.
    """
    from harvey.state import StateManager
    from harvey.signals import sync_vocabulary

    async def _signals():
        state = StateManager()
        await state.init_db()
        await sync_vocabulary(state)

        codes = [a.strip().upper() for a in (args.confirm or args.reject or "").split(",")
                 if a.strip()]
        # Signals of a module that is off are hidden: not listed, not decided on.
        visible = await state.get_signal_codes(available_only=True)
        if codes:
            status = "confirmed" if args.confirm else "rejected"
            if codes == ["ALL"]:
                codes = [c["code"] for c in visible]
            elif codes == ["FREE"]:
                # Only signals whose cost note STARTS with free/included. A
                # substring match would sweep in "1 credit (free tiers
                # available)", which is not free.
                codes = [c["code"] for c in visible
                         if (c["cost_note"] or "").lower().startswith(("free", "included"))]
            hidden = {c["code"]: c["module"] for c in await state.get_signal_codes()
                      if not c["available"]}
            for c in codes:
                if c in hidden:
                    print(f"\n  {c}: its module {hidden[c]} is off "
                          f"(harvey modules enable {hidden[c]}).")
            changed = sum(
                [1 for c in codes if c not in hidden and await state.set_signal_status(c, status)]
            )
            print(f"\n  {changed} signal(s) {status}.\n")
            return

        rows = visible
        states = await state.signal_states([r["code"] for r in rows])
        by_status = {"confirmed": 0, "proposed": 0, "rejected": 0}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1

        print(f"\n  Signals — {by_status['confirmed']} confirmed, "
              f"{by_status['proposed']} awaiting you, {by_status['rejected']} off")
        print("  " + "=" * 66)
        mark = {"confirmed": "[on] ", "proposed": "[ ? ]", "rejected": "[off]"}
        current = None
        for r in rows:
            if r["category"] != current:
                current = r["category"]
                print(f"\n  {current.upper()}")
            # Companies checked either way; the rest are not checked yet.
            st = states.get(r["code"], {})
            seen_txt = (f"  ({st['true']} yes, {st['false']} no, {st['unknown']} not checked)"
                        if st.get("true") or st.get("false") else "")
            print(f"    {mark.get(r['status'], '     ')} {r['code']:<22} "
                  f"{r['label']}{seen_txt}")
            print(f"          {r['cost_note']}")

        if by_status["proposed"]:
            print("\n  Confirm with: harvey signals --confirm CODE[,CODE...]")
            print("  Everything free: harvey signals --confirm free")
            print("  Details and descriptions: harvey dashboard → Signals\n")
        else:
            print("")

    asyncio.run(_signals())


# Why the resolver thinks two companies may be one, in words.
_MATCH_REASONS = {
    "cui": "same tax code (CUI) {value}",
    "reg": "same registration number {value}",
    "external": "same listing {value}",
    "domain": "same website {value}",
    "declared_domain": "the registry lists their website {value}",
    "phone": "same phone {value}",
    "email": "same email {value}",
    "name": "same name in the same town",
    "different_websites": "different websites, so the name alone doesn't count",
    "one_firm": "only this registered firm has it: a branch",
    "many_listings": "many listings share it with one firm: confirm them as a group",
    "shared_contact": "several firms share it, so it is only a question",
    "several_firms": "it could be any of several firms",
    "shared_site": "two records hold this website, so it is only a question",
}
_MERGE_REFUSED = {
    "different_cui": "they have different tax codes (CUI): two companies, never merged",
    "different_reg": "they have different registration numbers: two companies, never merged",
    "already_merged": "one of them is already merged; undo that first",
    "survivor_merged": "the company it joined has since been merged itself; undo that first",
    "not_merged": "that company isn't merged",
    "not_found": "no such company",
    "same_company": "that is the same company twice",
    "several_groups": "it kept more than one group; name one of the companies merged into it",
}


def _match_reasons(reasons) -> str:
    return "; ".join(_MATCH_REASONS.get(r.get("code"), r.get("code", ""))
                     .format(value=r.get("value", "")) for r in reasons or [])


def cmd_modules(args):
    """What Harvey is made of: every module's manifest, read and checked
    without importing any of them, and whether each is on in this install.
    ``enable``/``disable <id>`` turns one on or off."""
    import json

    from harvey.modules.loader import ManifestError, load_manifests

    try:
        manifests = load_manifests()
    except ManifestError as e:
        print("\n  Some module manifests are wrong:")
        for line in e.problems:
            print(f"    {line}")
        print()
        sys.exit(1)

    if args.modules_action in ("enable", "disable"):
        if not args.module_id:
            print(f"\n  Which module? harvey modules {args.modules_action} <id> "
                  "(see: harvey modules list)\n")
            sys.exit(2)
        _switch_module(args.module_id, args.modules_action == "enable")
        return

    on = asyncio.run(_modules_on())
    rows = [{**m.summary(), "enabled": m.id in on} for m in manifests]
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    print(f"\n  {'id':<18} {'set':<9} {'kind':<10} {'version':<8} {'signals':>7}  "
          f"{'default':<8} now")
    for r in rows:
        default = "on" if r["default_enabled"] else "off"
        now = "on" if r["enabled"] else "off"
        pack = "  (data pack)" if r["data_only"] else ""
        print(f"  {r['id']:<18} {r['set']:<9} {r['kind']:<10} {r['version']:<8} "
              f"{r['signals']:>7}  {default:<8} {now}{pack}")
    print(f"\n  {len(rows)} modules, {sum(r['signals'] for r in rows)} signals."
          "\n  Turn one on or off: harvey modules enable|disable <id>\n")


def _cost_text(cost: dict) -> str:
    """A capability's [cost] as people read it: "free", "$0.002/search"."""
    usd, per = cost.get("usd", 0.0), cost.get("per", "call")
    text = f"${usd:g}/{per}" if usd else "free"
    if cost.get("plus_usd"):
        text += f" + ${cost['plus_usd']:g}/{cost.get('plus_per', 'item')}"
    return text


def cmd_caps(args):
    """Every capability the module manifests declare: which module, what it
    takes and gives, what it costs, the lane it runs in, and whether its
    module is on."""
    import json

    from harvey.modules.loader import ManifestError, load_manifests

    try:
        manifests = load_manifests()
    except ManifestError as e:
        print("\n  Some module manifests are wrong:")
        for line in e.problems:
            print(f"    {line}")
        print()
        sys.exit(1)
    on = asyncio.run(_modules_on())
    rows = []
    for m in manifests:
        for cap in m.capabilities:
            lanes = [lane["id"] for lane in m.lanes]
            rows.append({
                "module": m.id, "capability": cap["id"], "op": cap.get("op", ""),
                "inputs": [list(alt) for alt in cap.get("inputs") or []],
                "outputs": dict(cap.get("outputs") or {}),
                "cost": dict(cap.get("cost") or {}), "cost_text": _cost_text(cap.get("cost") or {}),
                "lane": cap.get("lane") or (lanes[0] if lanes else ""),
                "enabled": m.id in on,
            })
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return

    def inputs(row):
        return " | ".join("+".join(alt) for alt in row["inputs"]) or "-"

    def outputs(row):
        out = row["outputs"]
        parts = [out["records"]] if out.get("records") else []
        parts += [f"{k}:{','.join(v)}" for k, v in out.items() if k != "records" and v]
        return " ".join(parts) or "-"

    print(f"\n  {'module':<18} {'capability':<20} {'op':<7} {'inputs':<22} {'cost':<34} "
          f"{'lane':<10} now")
    for r in rows:
        print(f"  {r['module']:<18} {r['capability']:<20} {r['op']:<7} {inputs(r):<22} "
              f"{r['cost_text']:<34} {r['lane'] or '-':<10} {'on' if r['enabled'] else 'off'}")
        print(f"  {'':<18} {'':<20} gives {outputs(r)}")
    print(f"\n  {len(rows)} capabilities in {len({r['module'] for r in rows})} modules."
          "\n  A module's switch turns all of its capabilities on or off: "
          "harvey modules enable|disable <id>\n")


async def _modules_on() -> set[str]:
    """What is on; a fresh install (no database yet) has the defaults, and
    reading them creates nothing."""
    from pathlib import Path

    from harvey import state as state_module
    from harvey.modules import switches
    from harvey.state import StateManager

    if not Path(state_module.DB_PATH).exists():
        return switches.resolve({})
    state = StateManager()
    await state.init_db()
    return await switches.enabled_ids(state)


def _require_module(module_id: str, what: str) -> None:
    """Stop before any work (a download, a database write) when the module
    a command belongs to is off; main() prints the reason, exit code 2."""
    from harvey.modules import switches

    if module_id not in asyncio.run(_modules_on()):
        raise switches.ModuleOff(module_id, what)


def _switch_module(module_id: str, on: bool):
    from harvey.modules import switches
    from harvey.state import StateManager

    async def _switch():
        state = StateManager()
        await state.init_db()
        try:
            await switches.set_enabled(state, module_id, on)
        except switches.SwitchRefused as e:
            print(f"\n  {e}\n")
            raise SystemExit(2)
        m = switches.by_id()[module_id]
        if on:
            print(f"\n  {m.name or m.id} is on.")
            if m.signals:
                print(f"  Its signals are back in the vocabulary, as you left them: "
                      f"{', '.join(s['code'] for s in m.signals)}")
        else:
            print(f"\n  {m.name or m.id} is off. Nothing was deleted: its data stays, "
                  "hidden, and comes back when you turn it on.")
        print()

    asyncio.run(_switch())


def cmd_doctor(args):
    """Check a schema 25 database's kept values against a full recount."""
    import sqlite3
    from pathlib import Path

    from harvey.overlay import recount, schema
    from harvey.state import DB_PATH

    path = Path(args.db or DB_PATH)
    if not args.recount:
        print("\n  Nothing to check: harvey doctor --recount compares every kept count "
              "with a full recount.\n")
        return
    if not path.is_file():
        print(f"\n  No database at {path}.\n")
        sys.exit(1)
    uri = f"file:{path}" + ("" if args.fix else "?mode=ro")
    con = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        found = schema.version(con)
        if found != schema.VERSION:
            print(f"\n  {path} is schema {found}; the recount checks schema {schema.VERSION} "
                  "(its kept counts). Nothing to do yet.\n")
            sys.exit(2)
        rows = recount.diff(con)
        wrong = {k: v for k, v in rows.items() if v}
        print()
        for name, n in rows.items():
            print(f"  {name:28} {n:>10,}")
        print(f"\n  {'Every kept value matches the recount.' if not wrong else f'{sum(wrong.values()):,} rows differ.'}")
        if wrong and args.fix:
            recount.fix(con)
            left = {k: v for k, v in recount.diff(con).items() if v}
            print("  Fixed: every kept value written again from a full recount."
                  if not left else f"  Still differing after the fix: {left}")
        print()
        if wrong and not args.fix:
            sys.exit(1)
    finally:
        con.close()


def cmd_duplicates(args):
    """Possible duplicates, as groups: companies that may be one, and the
    merges in force. Merge a group into one record, take companies out of a
    group, undo a merge (one member or the whole group), or look again."""
    from harvey.resolver import MergeRefused
    from harvey.state import StateManager

    def side(c) -> str:
        if not c:
            return "(gone)"
        bits = [c.get("name") or "(no name)"]
        if c.get("cui"):
            bits.append(f"CUI {c['cui']}")
        place = (c.get("location") or "").replace(", Romania", "")
        if place:
            bits.append(place)
        bits += c.get("phones") or []
        bits += c.get("emails") or []
        bits += c.get("domains") or []
        return " · ".join(bits) + f"  [{c['id']}, {', '.join(c.get('sources') or []) or '?'}]"

    async def _scan(state):
        from harvey.modules import jobs
        from harvey.modules.job_kinds import enqueue_duplicates_scan

        batch_id = await jobs.new_batch(state, "duplicates_scan", "Look for duplicates")
        try:
            await enqueue_duplicates_scan(state, batch_id=batch_id)
        except jobs.Duplicate as e:
            print(f"\n  {e}\n")
            raise SystemExit(2)
        found = await _wait_for(state, batch_id)
        code = _job_failures(found)
        if code:
            raise SystemExit(code)
        done = next((j.result.get("duplicates") for j in found if j.status == jobs.DONE), None)
        if done:
            print(f"\n  Looked again: {done['merged']} merged on their own (undoable), "
                  f"{done['review']} put up for review.")

    async def _duplicates():
        state = StateManager()
        await state.init_db()
        try:
            if args.merge:
                keep, *members = args.merge
                done = await state.merge_group(keep, members)
                print(f"\n  Merged {', '.join(done['merged'])} into {keep}.")
                for member in done["merged"]:
                    print(f"  Undo with: harvey duplicates --undo {member}")
                if len(done["merged"]) > 1:
                    print(f"  Undo them all: harvey duplicates --undo-group {done['merged'][0]}")
                print()
                return
            if args.separate:
                if args.separate_from:
                    await state.separate_group(args.separate, args.separate_from)
                else:
                    await state.separate_group(args.separate)
                print("\n  Kept apart: they won't be suggested as one company again.\n")
                return
            if args.undo:
                done = await state.undo_merge(args.undo)
                print(f"\n  Undone: {done['merged']} is its own company again, "
                      f"apart from {done['survivor']}.\n")
                return
            if args.undo_group:
                done = await state.undo_group(args.undo_group)
                print(f"\n  Undone: {len(done)} compan{'y is' if len(done) == 1 else 'ies are'} "
                      f"on their own again ({', '.join(d['merged'] for d in done)}).\n")
                return
        except MergeRefused as e:
            print(f"\n  Can't: {_MERGE_REFUSED.get(e.code, e.code)}.\n")
            sys.exit(1)
        if args.scan:
            await _scan(state)

        total, groups = await state.duplicate_groups(args.limit)
        print(f"\n  Possible duplicates — {total} group{'' if total == 1 else 's'} to decide")
        print("  " + "=" * 66)
        for g in groups:
            print(f"\n  {round(g['score'] * 100)}%  {len(g['members'])} companies: "
                  f"{_match_reasons(g['reasons'])}")
            keep = g["keeper"]
            same = [m["id"] for m in g["members"] if m["id"] != keep and not m["conflict"]]
            for m in g["members"]:
                mark = "keep" if m["id"] == keep else ("other firm" if m["conflict"] else "")
                print(f"    {mark:<10} {side(m)}")
            if same:
                print(f"    same:      harvey duplicates --merge {keep} {' '.join(same)}")
            ids = " ".join(m["id"] for m in g["members"])
            print(f"    different: harvey duplicates --separate {ids}")
            if len(g["members"]) > 2:
                print("    take one out: harvey duplicates --separate ID --from "
                      "<the rest of the group>")
        merged = await state.merges(args.limit)
        if merged:
            print(f"\n  Merged — {len(merged)} (newest first)")
            for m in merged:
                who = "automatically" if m["decided_by"] == "auto" else "by you"
                print(f"\n    {side(m['b'])}")
                print(f"      joined {side(m['a'])} {who}: {_match_reasons(m['reasons'])}")
                print(f"      undo: harvey duplicates --undo {m['merged']}"
                      + (f"   (whole group: --undo-group {m['merged']})"
                         if m.get("group_id") and sum(1 for x in merged
                                                      if x.get("group_id") == m["group_id"]) > 1
                         else ""))
        print("")

    asyncio.run(_duplicates())


def cmd_retry_writes(args):
    """Put prospects the Writer gave up on back in line for another try."""
    from harvey.agents.writer import MAX_WRITE_ATTEMPTS
    from harvey.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        count = await state.retry_write_failed()
        if count:
            print(f"\n  {count} prospect(s) are back in line. The Writer will try each "
                  f"up to {MAX_WRITE_ATTEMPTS} more times.\n")
        else:
            print("\n  No prospects are waiting on a retry.\n")

    asyncio.run(_run())


def cmd_warmup(args):
    """Check whether the sending domain is ready for cold email."""
    import os

    from dotenv import load_dotenv

    from harvey import deliverability as d
    from harvey.agents.handler import BOUNCE_COUNT_KEY
    from harvey.config import ConfigError, load_config
    from harvey.paths import PROJECT_ROOT
    from harvey.state import StateManager

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        config = load_config()
    except ConfigError as e:
        print(f"\n  {e}\n")
        raise SystemExit(1)
    email = config.channels.email
    smtp_user = os.getenv("SMTP_USERNAME", "").strip() if email.provider == "smtp" else ""
    sender = smtp_user if "@" in smtp_user else config.persona.email
    domain = (args.domain or sender.rpartition("@")[2]).strip().lower().rstrip(".")
    if not d.DOMAIN_RE.match(domain):
        print("\n  No valid sending domain found. Pass one: harvey warmup yourdomain.com\n")
        raise SystemExit(1)

    async def _history():
        state = StateManager()
        await state.init_db()
        return (await state.count_outbox_sent(),
                int(await state.get_setting(BOUNCE_COUNT_KEY) or 0))

    smtp_host = os.getenv("SMTP_HOST", "").strip() if email.provider == "smtp" else ""
    print(f"\n  Deliverability check - {domain}")
    print("  " + "=" * 56)
    sections = [
        ("Authentication", [d.check_mx(domain), d.check_spf(domain),
                            d.check_dkim(domain, email.warmup.dkim_selector),
                            d.check_dmarc(domain)]),
        ("Reputation", [d.check_domain_age(domain), *d.check_domain_blocklists(domain)]
         + (d.check_ip_blocklists(smtp_host) if smtp_host else [])),
        ("Warmup & sending", [d.check_warmup(email.warmup, email.max_daily_sends,
                                             provider=email.provider),
                              _warmup_service_check(sender, email.warmup),
                              d.check_send_history(*asyncio.run(_history()))]),
    ]
    icons = {d.OK: "OK  ", d.WARN: "WARN", d.FAIL: "FAIL", d.INFO: "info"}
    failed = warned = unknown = 0
    for title, checks in sections:
        print(f"\n  {title}")
        for c in checks:
            failed += c.status == d.FAIL
            warned += c.status == d.WARN
            unknown += c.unknown and title == "Authentication"
            print(f"    [{icons[c.status]}] {c.name:<26} {c.detail}")

    print("\n  " + "=" * 56)
    if failed:
        print(f"  NOT READY - fix the {failed} FAIL item(s) before any cold email.")
    elif unknown:
        print(f"  NOT CHECKED - {unknown} authentication lookup(s) failed; run again.")
    elif warned:
        print(f"  READY WITH CAVEATS - {warned} warning(s); keep volume low.")
    else:
        print("  READY - send within today's cap and watch bounces.")
    print("\n  What DNS can't tell you (the real reputation signal):")
    print("    - Google Postmaster Tools: https://postmaster.google.com - add the domain;")
    print("      it shows Gmail's spam rate and domain reputation once you send volume.")
    print("    - Inbox placement: send one email to a seed test (mail-tester.com is free)")
    print("      and aim for 9/10+. TrulyInbox shows the warmup inbox rate, and with it")
    print("      connected (harvey trulyinbox connect) Harvey reads it into this report.")
    print("      Wait for ~95%+ inbox for a week before relying on cold sends.\n")
    if failed or unknown:
        raise SystemExit(1)


def _warmup_service_check(address: str, warmup):
    """The 'Warmup service' line: TrulyInbox's view of the mailbox, or the recommendation."""
    from harvey import deliverability as d
    from harvey.integrations import trulyinbox as ti
    from harvey.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        health, error = await ti.current_health(address)
        other = health is None and not error and await ti.get_choice(state) == "other"
        return d.check_warmup_service(health, warmup, error=error, other_service=other)
    return asyncio.run(_run())


def cmd_trulyinbox(args):
    """Connect TrulyInbox, record another warmup service, or show the status."""
    import getpass

    from dotenv import load_dotenv

    from harvey import deliverability as d
    from harvey.config import ConfigError, load_config
    from harvey.integrations import trulyinbox as ti
    from harvey.paths import PROJECT_ROOT
    from harvey.state import StateManager

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        config = load_config()
    except ConfigError as e:
        print(f"\n  {e}\n")
        raise SystemExit(1)
    address = ti.sending_address(config)
    warmup = config.channels.email.warmup

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.trulyinbox_action == "other":
            await ti.set_choice(state, "other")
            print("\n  Noted: you use another warmup service. Harvey won't ask about "
                  "TrulyInbox again.\n")
            return 0
        if args.trulyinbox_action == "connect":
            print(f"\n  Connect TrulyInbox. Don't have it yet? {ti.AFFILIATE_URL} {ti.AFFILIATE_TAG}")
            print("  You need Starter or higher. In TrulyInbox, open Settings > API keys and")
            print("  create a workspace key with accounts:read, warmup:read, analytics:read")
            print("  and workspace:read.\n")
            if not address:
                print(f"\n  {ti.NO_ADDRESS}\n")
                return 1
            key = getpass.getpass("  TrulyInbox API key (hidden): ").strip()
            if not key:
                print("\n  No key entered.\n")
                return 1
            workspace = ti.key_from_env()[1]
            ti.forget_rejected(key)
            try:
                client = ti.TrulyInbox(key, workspace)
                health = await client.mailbox_health(address)
            except ti.TrulyInboxError as e:
                print(f"\n  {e}\n")
                return 1
            from harvey.dashboard import _write_env_file
            updates = {"TRULYINBOX_API_KEY": key}
            if not workspace:
                updates["TRULYINBOX_WORKSPACE_ID"] = client.workspace_id
            try:
                _write_env_file(updates)
            except OSError as e:
                print(f"\n  The key works, but Harvey couldn't save it to .env: {e}\n")
                return 1
            await ti.set_choice(state, "connected")
            print("\n  Connected. " + d.check_warmup_service(health, warmup).detail + "\n")
            return 0
        choice = await ti.get_choice(state)
        health, error = await ti.current_health(address)
        print(f"\n  TrulyInbox: {ti.describe_status(health, error, choice)}")
        check = d.check_warmup_service(health, warmup, error=error,
                                       other_service=choice == "other")
        print(f"  {check.detail}\n")
        return 0

    raise SystemExit(asyncio.run(_run()))


def cmd_sending(args):
    """Pause/resume the sending kill switch."""
    from harvey.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.sending_action == "pause":
            await state.set_setting("sending_paused", "paused manually")
            print("\n  Sending paused. Nothing will leave the outbox.\n")
        elif args.sending_action == "resume":
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")
            print("\n  Sending resumed (bounce counter reset).\n")
        else:
            paused = await state.get_setting("sending_paused")
            print(f"\n  Sending: {'PAUSED — ' + paused if paused else 'active'}\n")

    asyncio.run(_run())


def main():
    from harvey.textio import utf8_stdio
    utf8_stdio()
    from harvey.paths import PROJECT_ROOT
    project_root = str(PROJECT_ROOT)

    parser = argparse.ArgumentParser(
        prog="harvey",
        description="Harvey — Autonomous AI Sales Agent. Always Be Closing.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # harvey install
    sub = subparsers.add_parser("install", help="Install dependencies")
    sub.set_defaults(func=cmd_install)

    # harvey setup
    sub = subparsers.add_parser("setup", help="Run the interactive setup wizard")
    sub.set_defaults(func=cmd_setup)

    # harvey run
    sub = subparsers.add_parser("run", help="Start Harvey's heartbeat loop")
    sub.set_defaults(func=cmd_run)

    # harvey train <url>
    sub = subparsers.add_parser("train", help="Train Harvey on a website")
    sub.add_argument("url", help="Website URL to crawl and learn from")
    sub.add_argument(
        "max_pages",
        nargs="?",
        type=int,
        default=100,
        help="Max pages to crawl (default: 100)",
    )
    sub.add_argument(
        "--provider", choices=["claude", "codex"], default=None,
        help="Subscription to think with (default: the one in your config, else claude)",
    )
    sub.add_argument("--model", default=None, help="Model for this provider (default: provider default)")
    sub.add_argument(
        "--reasoning-effort", default=None,
        help="Codex reasoning effort (minimal/low/medium/high/xhigh)",
    )
    sub.set_defaults(func=cmd_train)

    # harvey dashboard
    sub = subparsers.add_parser("dashboard", help="Open the web dashboard")
    sub.add_argument("--host", default="127.0.0.1",
                     help="Loopback address to listen on (default: 127.0.0.1; "
                          "other addresses are refused)")
    sub.add_argument("--port", type=int, default=5555, help="Port (default: 5555)")
    sub.add_argument("--url", action="store_true",
                     help="Print the unlock link (with its token) and exit")
    sub.set_defaults(func=cmd_dashboard)

    # harvey status
    sub = subparsers.add_parser("status", help="Show pipeline status")
    sub.add_argument(
        "--check", action="store_true",
        help="Also check the configured brain now (Codex: CLI version, isolation "
             "and login; Claude: CLI isolation flags; no model call)",
    )
    sub.set_defaults(func=cmd_status)

    # harvey export
    sub = subparsers.add_parser(
        "export", help="Export prospects as a sequencer-ready CSV"
    )
    sub.add_argument("--out", default="",
                     help="Output file (default: prospects.csv, or companies.csv with --companies)")
    sub.add_argument(
        "--email-status", default="",
        help="Comma-separated statuses to include (default: verified,risky)",
    )
    sub.add_argument("--min-score", type=int, default=0, help="Minimum ICP score")
    sub.add_argument("--status", default="", help="Comma-separated pipeline statuses (e.g. new,queued); opted-out contacts stay excluded")
    sub.add_argument("--all", action="store_true", help="Export the raw list without filters, including opted-out contacts (not for outreach)")
    sub.add_argument("--companies", action="store_true",
                     help="One row per company (phone, location, best contact if any), "
                          "including companies with no email found; other filters don't apply")
    sub.set_defaults(func=cmd_export)

    # harvey gmail auth|test
    sub = subparsers.add_parser("gmail", help="Gmail provider setup")
    sub.add_argument("gmail_action", choices=["auth", "test"],
                     help="auth: one-time OAuth; test: verify connection")
    sub.set_defaults(func=cmd_gmail)

    # harvey microsoft auth|test
    sub = subparsers.add_parser("microsoft", help="Microsoft 365 provider setup")
    sub.add_argument("microsoft_action", choices=["auth", "test"],
                     help="auth: one-time sign-in; test: verify connection")
    sub.set_defaults(func=cmd_microsoft)

    # harvey outbox
    sub = subparsers.add_parser("outbox", help="Review/approve queued emails")
    sub.add_argument("--approve", metavar="ID", default="", help="Approve one item")
    sub.add_argument("--approve-all", action="store_true", help="Approve all pending")
    sub.add_argument("--reject", metavar="ID", default="", help="Reject one item")
    sub.set_defaults(func=cmd_outbox)

    # harvey sending pause|resume|status
    sub = subparsers.add_parser("discover", help="Find businesses matching your ICP")
    sub.add_argument("--provider", default="osm",
                     help="Discovery provider, or several separated by commas in one run "
                          "under one cap: serper,tinyfish (default: osm — free, no account)")
    sub.add_argument("--providers", action="store_true",
                     help="List every provider with cost and setup, then exit")
    sub.add_argument("--estimate", action="store_true",
                     help="Print projected spend and exit without calling anything")
    sub.add_argument("--city", default="",
                     help='Semicolon-separated cities, e.g. "Denver, CO;Dallas, TX" '
                          '(default: icp.geography)')
    sub.add_argument("--depth", type=int, default=None,
                     help="How deep a ranked search goes (default: as deep as the "
                          "source serves; each page of results is billed)")
    sub.add_argument("--limit", type=int, default=None,
                     help="Most records to take per query (default: every result; "
                          "the spend cap stops the run)")
    sub.add_argument("--max-spend", type=_dollars, default=1.0,
                     help="Hard cap in dollars (default: 1.00)")
    sub.add_argument("--preset", default="", choices=["", "yc", "hiring", "accelerator", "funding"],
                     help="A built-in search instead of your ICP's industries "
                          "(each needs a search provider like tinyfish, serper or dataforseo_serp). "
                          "yc: startups whose own site says they're YC-backed; "
                          "hiring: companies whose own careers pages list one of "
                          "your hiring_signals roles (else ICP titles); "
                          "accelerator: ones backed by Techstars, Antler, EF, SPC, 500 Global, "
                          "Neo, AI Grant, a16z speedrun or Plug and Play; "
                          "funding: companies announcing a raise on their own site in the last 18 months")
    sub.add_argument("--no-profile", action="store_true",
                     help="Don't look for contacts at what was found (reading sites is free)")
    sub.add_argument("--websites-max-spend", type=_dollars, default=0.0,
                     help="Also look up a website for each company found with none: free "
                          "searches always, paid ones up to this many dollars, on top of "
                          "--max-spend (default: 0, free searches only)")
    sub.add_argument("--no-find-websites", action="store_true",
                     help="Don't look up websites for the companies found with none")
    _background_arg(sub)
    sub.set_defaults(func=cmd_discover)

    sub = subparsers.add_parser(
        "profile", help="Read discovered companies' websites (free, no Claude)")
    sub.add_argument("--limit", type=int, default=None,
                     help="Most sites to queue (default: every one due)")
    sub.add_argument("--stale-days", type=int, default=90,
                     help="Re-read a site after this many days (default: 90)")
    _background_arg(sub)
    sub.set_defaults(func=cmd_profile)

    sub = subparsers.add_parser(
        "contacts",
        help="Find a way to reach companies on file with none (the contact finder; "
             "no Claude calls, sends nothing)",
        description="The contact finder. 'find' (the default) searches the companies "
                    "that match the filters and have no contact: their own website "
                    "first (free), then, within --max-spend, a search by name and town "
                    "for those with no website and one checked address per person "
                    "named there. 'status' shows the backlog.")
    sub.add_argument("action", nargs="?", default="find", choices=["find", "status"],
                     help="find (default) or status")
    sub.add_argument("--county", default="", help="Only companies in this county")
    sub.add_argument("--locality", default="", help="Only companies in this town")
    sub.add_argument("--website", default="", choices=["", "yes", "no"],
                     help="Only companies with (yes) or without (no) a website")
    sub.add_argument("--q", default="", help="Words in the company's name")
    sub.add_argument("--require", default="",
                     help="Signal codes a company must hold (a code not checked yet counts)")
    sub.add_argument("--exclude", default="", help="Signal codes a company must not hold")
    sub.add_argument("--limit", type=int, default=200,
                     help="Most companies to search, newest first (default: 200)")
    sub.add_argument("--max-spend", type=_dollars, default=0.0,
                     help="Cap in dollars for website searches and address checks "
                          "(default: 0, nothing paid: websites are read only)")
    sub.add_argument("--force", action="store_true",
                     help="Search again companies already searched (each search is "
                          "kept; the company drawer shows where and when)")
    _background_arg(sub)
    sub.set_defaults(func=cmd_contacts)

    sub = subparsers.add_parser(
        "caen", help="What a Romanian activity (CAEN) code means, or the codes "
                     "for some words: 'harvey caen 8623', 'harvey caen dentist'")
    sub.add_argument("words", nargs="+",
                     help="A CAEN code, or what the business does in English or Romanian")
    sub.add_argument("--limit", type=int, default=30,
                     help="Max classes to show (default: 30)")
    sub.set_defaults(func=cmd_caen)

    sub = subparsers.add_parser(
        "search", help="Search companies on file by activity (words or CAEN code), "
                       "name or town: 'harvey search stomatologie brasov'",
        description="Searches the companies already on file, locally: every word "
                    "must match the name, an activity (CAEN code, its Romanian or "
                    "English name, everyday words like dentist) or the place. "
                    "Accents don't matter. Nothing is fetched or sent.")
    sub.add_argument("words", nargs="+", help="Words, CAEN codes or towns")
    sub.add_argument("--limit", type=int, default=20,
                     help="Max companies to show (default: 20)")
    sub.add_argument("--offset", type=int, default=0, help="Skip this many first")
    sub.add_argument("--json", action="store_true", help="Print JSON")
    sub.set_defaults(func=cmd_search)

    sub = subparsers.add_parser(
        "registry",
        help="Romania's whole trade registry (ONRC open data), loaded once per dataset "
             "into data/registry.db: load, status, search, pull",
        description="Loads every firm in ONRC's open data (all statuses, every "
                    "authorised CAEN class, administrators' names and roles) into a "
                    "local database once per dataset, searches it in words, and pulls "
                    "the firms you choose into Harvey. Free; no Claude calls.")
    registry_sub = sub.add_subparsers(dest="registry_action", required=True)

    def _registry_common(p):
        p.add_argument("--registry", default="",
                       help="The registry database (default: data/registry.db)")

    r = registry_sub.add_parser("load", help="Download the dataset (if missing) and load it")
    r.add_argument("--dataset", default="latest",
                   help="latest (default) or a dataset name like firme-19-10-2026")
    r.add_argument("--administrators", choices=("all", "none"), default="all",
                   help="none: skip the representatives file (no administrators)")
    r.add_argument("--force", action="store_true",
                   help="Load again even when this dataset is already loaded")
    r.add_argument("--data-dir", default="",
                   help="Where the dataset files are kept (default: data/onrc)")
    _registry_common(r)
    _background_arg(r)
    r = registry_sub.add_parser("status", help="Which dataset is loaded, and its size")
    r.add_argument("--json", action="store_true", help="Print JSON")
    _registry_common(r)
    r = registry_sub.add_parser(
        "search", help="Search every firm: words, CAEN code, county (BV), town")
    r.add_argument("words", nargs="+",
                   help="All must match: name, activity (dentist, farmacie, 8623), county "
                        "(BV, Brașov) or town; -word excludes")
    r.add_argument("--all-statuses", action="store_true",
                   help="Also firms that aren't operating (struck off, insolvent...)")
    r.add_argument("--limit", type=int, default=20, help="Firms to show (default: 20)")
    r.add_argument("--offset", type=int, default=0, help="Skip this many first")
    r.add_argument("--json", action="store_true", help="Print JSON")
    _fin_filter_args(r)
    _tax_filter_args(r)
    _registry_common(r)
    r = registry_sub.add_parser(
        "pull", help="Copy the firms a search matches into Harvey (what import-onrc and Find use)")
    r.add_argument("words", nargs="+", help="The same words as registry search")
    r.add_argument("--limit", type=int, default=None,
                   help="Only the first N matches (default: all, up to 10,000)")
    r.add_argument("--administrators", choices=("all", "none"), default="all",
                   help="all: name each firm's administrators (when CONTACT_FOUND is "
                        "confirmed); none: leave them out")
    r.add_argument("--all-statuses", action="store_true",
                   help="Also firms that aren't operating")
    r.add_argument("--dry-run", action="store_true", help="Count; save nothing")
    _fin_filter_args(r)
    _tax_filter_args(r)
    _registry_common(r)
    _background_arg(r)
    sub.set_defaults(func=cmd_registry)

    sub = subparsers.add_parser(
        "financials",
        help="Romanian companies' annual financial statements (Ministry of Finance open "
             "data) in data/financials.db: load, status, check",
        description="Downloads the Ministry of Finance's yearly statement files from "
                    "data.gov.ro (only what is missing; kept in data/mf_financials/) "
                    "and loads the latest years into a local database: turnover, "
                    "employees, profit, equity and debts per company (CUI). Companies "
                    "on file get the figures as signals, and registry search, pull and "
                    "import-onrc can filter on turnover and employees before pulling "
                    "anything. Free; no Claude calls.")
    fin_sub = sub.add_subparsers(dest="financials_action", required=True)
    r = fin_sub.add_parser("load", help="Download what is missing and load the latest years")
    r.add_argument("--years", default="",
                   help="How many of the latest published years to keep loaded (saved as "
                        "the module's setting; default 3, 'all' for every year)")
    r.add_argument("--force", action="store_true",
                   help="Load again even when nothing new is published")
    _background_arg(r)
    r = fin_sub.add_parser("status", help="What is loaded, and whether data.gov.ro has newer")
    r.add_argument("--json", action="store_true", help="Print JSON")
    r = fin_sub.add_parser("check", help="Ask data.gov.ro now whether a new or revised year is out")
    _background_arg(r)
    sub.set_defaults(func=cmd_financials)

    sub = subparsers.add_parser(
        "taxpayers",
        help="Romanian taxpayers' identification file (Ministry of Finance open data) in "
             "data/taxpayers.db: load, status, check",
        description="Downloads the Ministry of Finance's yearly taxpayer identification "
                    "file from data.gov.ro (about 870 MB; kept in data/mf_taxpayers/) and "
                    "loads it into a local database: each taxpayer's address, phone, VAT "
                    "registration, and whether it is an employer or on the "
                    "micro-enterprise tax. Companies on file get them as signals. Free; "
                    "no Claude calls.")
    tax_sub = sub.add_subparsers(dest="taxpayers_action", required=True)
    r = tax_sub.add_parser("load", help="Download the newest file if missing and load it")
    r.add_argument("--force", action="store_true",
                   help="Load again even when nothing new is published")
    _background_arg(r)
    r = tax_sub.add_parser("status", help="What is loaded, and whether data.gov.ro has newer")
    r.add_argument("--json", action="store_true", help="Print JSON")
    r = tax_sub.add_parser("check", help="Ask data.gov.ro now whether a newer file is out")
    _background_arg(r)
    sub.set_defaults(func=cmd_taxpayers)

    sub = subparsers.add_parser(
        "anaf",
        help="Check companies on file at ANAF's public web service (VAT, e-Factura, "
             "inactive, status, legal form, address, phone): check, status",
        description="Asks ANAF's free public web service about the companies on file by "
                    "CUI: up to 100 a request, one request a second, answers kept for "
                    "30 days (the module's cache_days). Firms a registry pull brings in "
                    "are checked on their own while the module is on. No Claude calls.")
    anaf_sub = sub.add_subparsers(dest="anaf_action", required=True)
    r = anaf_sub.add_parser("check", help="Check companies on file")
    r.add_argument("--cui", action="append", default=[],
                   help="A company's CUI (repeat, or comma-separated); it must be on file")
    r.add_argument("--all-on-file", action="store_true",
                   help="Every company on file with a CUI (kept answers younger than "
                        "cache_days are reused)")
    r.add_argument("--missing", action="store_true",
                   help="Only companies ANAF was never asked about")
    _background_arg(r)
    anaf_sub.add_parser("status", help="How many ANAF answers are kept")
    sub.set_defaults(func=cmd_anaf)

    sub = subparsers.add_parser(
        "import-onrc",
        help="Import Romanian companies by county (and optionally activity) from "
             "the trade registry's open data (free; loads data/registry.db once per dataset)",
        description="Finds operating companies in a county, authorised for an "
                    "activity (CAEN class) or for any, in the local copy of ONRC's "
                    "open data (harvey registry; loaded first when the latest dataset "
                    "isn't), with every class each is authorised for (search "
                    "them with harvey search) and their administrators when "
                    "CONTACT_FOUND is confirmed. "
                    "A company already on file keeps what it has, with its gaps "
                    "filled, except the registered address, which the registry's "
                    "replaces. No Claude calls, and nothing is sent.")
    sub.add_argument("activity", nargs="*",
                     help="What the business does, in English or Romanian, one per "
                          "argument (dentist farmacie); quote a phrase: \"medic de familie\". "
                          "Omit for every activity in the --county/--locality")
    sub.add_argument("--county", default="",
                     help="County names or codes, comma-separated: Brașov, Brasov or BV "
                          "(Bucharest is B). Omit for the whole country")
    sub.add_argument("--locality", default="",
                     help="Only these towns or villages, comma-separated "
                          "(Brașov, Săcele); works with or without --county")
    sub.add_argument("--codes", default="",
                     help="CAEN codes instead of (or as well as) words, e.g. 8623,8622")
    sub.add_argument("--include-inactive", action="store_true",
                     help="Also companies that aren't operating (struck off, insolvent...)")
    sub.add_argument("--dry-run", action="store_true",
                     help="Count what would be imported (the registry is still "
                          "loaded); save nothing")
    sub.add_argument("--whole-country", action="store_true",
                     help="Import every operating company in Romania when no "
                          "activity, area or filter is given")
    sub.add_argument("--data-dir", default="",
                     help="Where the dataset files are kept (default: data/onrc)")
    sub.add_argument("--administrators", choices=("all", "none"), default="all",
                     help="all: name each company's administrators (when CONTACT_FOUND "
                          "is confirmed; the default); none: leave them out")
    sub.add_argument("--keep-download", action="store_true",
                     help=argparse.SUPPRESS)     # downloads are always kept now
    sub.add_argument("--registry", default="", help=argparse.SUPPRESS)
    _fin_filter_args(sub)
    _tax_filter_args(sub)
    _background_arg(sub)
    sub.set_defaults(func=cmd_import_onrc)

    sub = subparsers.add_parser(
        "signals", help="Review/confirm which signals Harvey prospects against")
    sub.add_argument("--confirm", metavar="CODES", default="",
                     help="Comma-separated codes to confirm ('all' or 'free' accepted)")
    sub.add_argument("--reject", metavar="CODES", default="",
                     help="Comma-separated codes to turn off")
    sub.set_defaults(func=cmd_signals)

    sub = subparsers.add_parser(
        "modules", help="The modules Harvey is made of, from their manifests")
    sub.add_argument("modules_action", nargs="?", default="list",
                     choices=["list", "enable", "disable"])
    sub.add_argument("module_id", nargs="?", default="", help="The module to turn on or off")
    sub.add_argument("--json", action="store_true", help="Print JSON")
    sub.set_defaults(func=cmd_modules)

    sub = subparsers.add_parser(
        "caps", help="Every capability the modules declare: inputs, outputs, cost, on/off")
    sub.add_argument("caps_action", nargs="?", default="list", choices=["list"])
    sub.add_argument("--json", action="store_true", help="Print JSON")
    sub.set_defaults(func=cmd_caps)

    sub = subparsers.add_parser(
        "duplicates", help="Companies that may be on file twice: merge, keep apart, undo")
    group = sub.add_mutually_exclusive_group()
    group.add_argument("--merge", nargs="+", metavar="ID",
                       help="Same company: KEEP MERGED [MERGED...]: every MERGED's facts move "
                            "onto KEEP, a different address becoming one of its locations")
    group.add_argument("--separate", nargs="+", metavar="ID",
                       help="Different companies: never suggest these as one again (with "
                            "--from: these are not any of those)")
    group.add_argument("--undo", metavar="MERGED",
                       help="Undo the merge that took this company")
    group.add_argument("--undo-group", metavar="MERGED",
                       help="Undo the whole group merge this company joined (or kept, "
                            "when it kept only one)")
    group.add_argument("--scan", action="store_true",
                       help="Look again among every company on file, then list")
    sub.add_argument("--from", dest="separate_from", nargs="+", metavar="ID", default=None,
                     help="With --separate: the rest of the group they are taken out of")
    sub.add_argument("--limit", type=int, default=50, help="How many groups to list (default 50)")
    sub.set_defaults(func=cmd_duplicates)

    sub = subparsers.add_parser(
        "doctor", help="Check the database: --recount compares kept counts with a full recount")
    sub.add_argument("--recount", action="store_true",
                     help="Count every kept value again from its rows and list what differs")
    sub.add_argument("--fix", action="store_true",
                     help="With --recount: write the recounted values where they differ")
    sub.add_argument("--db", default="", help="The database to check (default: data/harvey.db)")
    sub.set_defaults(func=cmd_doctor)

    sub = subparsers.add_parser(
        "retry-writes", help="Retry prospects the Writer couldn't write an email for")
    sub.set_defaults(func=cmd_retry_writes)

    sub = subparsers.add_parser(
        "jobs", help="The queue of background work: list, cancel, retry, resume; pause a lane",
        description="Searches, site reads, registry and public-data loads and ANAF checks run "
                    "as jobs, one lane per source: a lane runs its jobs in order, lanes run "
                    "side by side. The dashboard's worker runs them (or harvey worker).")
    sub.add_argument("jobs_action", nargs="?", default="list",
                     choices=("list", "cancel", "retry", "resume", "pause", "resume-lane"),
                     help="list (default); cancel/retry/resume ID; pause/resume-lane LANE")
    sub.add_argument("target", nargs="?", default="", help="A job id (or its start), or a lane")
    sub.add_argument("--lane", default="", help="list: only this lane")
    sub.add_argument("--all", action="store_true", help="list: done and cancelled jobs too")
    sub.add_argument("--limit", type=int, default=20, help="list: jobs per lane (default: 20)")
    sub.add_argument("--max-spend", type=_dollars, default=None,
                     help="resume: a new spend limit for the job's run (in dollars)")
    sub.set_defaults(func=cmd_jobs)

    sub = subparsers.add_parser(
        "worker", help="Run queued jobs without the dashboard (one worker per database)")
    sub.set_defaults(func=cmd_worker)

    sub = subparsers.add_parser("sending", help="Kill switch: pause/resume sending")
    sub.add_argument("sending_action", nargs="?", default="status",
                     choices=["pause", "resume", "status"])
    sub.set_defaults(func=cmd_sending)

    sub = subparsers.add_parser(
        "warmup", help="Check if your sending domain is set up and warmed up for cold email")
    sub.add_argument("domain", nargs="?", default="",
                     help="Domain to check (default: your sending mailbox's domain)")
    sub.set_defaults(func=cmd_warmup)

    sub = subparsers.add_parser(
        "trulyinbox", help="Warmup service: connect TrulyInbox so Harvey can read the inbox rate")
    sub.add_argument("trulyinbox_action", nargs="?", default="status",
                     choices=["status", "connect", "other"],
                     help="connect a key, 'other' if you use another warmup service, "
                          "or show the status (default)")
    sub.set_defaults(func=cmd_trulyinbox)

    # harvey usage
    sub = subparsers.add_parser("usage", help="Show brain usage and quota (Claude or Codex)")
    sub.add_argument("--days", type=int, default=30, help="Breakdown window (default: 30)")
    sub.set_defaults(func=cmd_usage)

    args = parser.parse_args()
    args._project_root = project_root

    if args.command is None:
        parser.print_help()
        print("\n  Quick start:")
        print("    harvey install   — Install dependencies")
        print("    harvey setup     — Configure Harvey (first time)")
        print("    harvey run       — Start closing deals")
        print("    harvey dashboard — Open the web dashboard")
        print()
        sys.exit(0)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n  Interrupted. Goodbye.")
        sys.exit(130)
    except Exception as e:
        # ConfigError and friends carry actionable messages — show them
        # cleanly instead of a raw traceback.
        from harvey.config import ConfigError
        from harvey.modules.switches import ModuleOff

        if isinstance(e, ModuleOff):
            print(f"\n  {e}\n")
            sys.exit(2)
        if isinstance(e, ConfigError):
            print(f"\n  Configuration problem:\n  {e}\n")
        else:
            print(f"\n  Error running 'harvey {args.command}': {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
