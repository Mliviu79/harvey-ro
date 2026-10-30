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
    """Install all dependencies including Playwright browsers."""
    _check_python_version()
    print("\n  Installing Harvey dependencies...\n")

    # Install Python packages
    print("  [1/2] Installing Python packages...")
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

    # Install Playwright browsers
    print("  [2/2] Installing Playwright browsers...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=600,
        )
        playwright_ok = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        playwright_ok = False
    if not playwright_ok:
        print("\n  Playwright browser install failed (optional — needed for LinkedIn).")
    else:
        print("  ✓ Playwright browsers installed.\n")

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
            print("  Prospecting:          PAUSED")
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
                  "with its best contact where one was found)\n")
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
    """Read what companies' own websites say about them. Free, no Claude calls."""
    from harvey.state import StateManager
    from harvey.pipeline import run_profile_stage

    async def _profile():
        state = StateManager()
        await state.init_db()

        pending = await state.count_companies_needing_profile(args.stale_days)
        if not pending:
            print("\n  Nothing to profile. Every company with a website has "
                  "been read recently.\n")
            return

        print(f"\n  {pending} companies need profiling. Reading up to "
              f"{args.limit}...\n")
        try:
            from harvey.config import load_config
            config = load_config()
        except Exception:
            config = None   # still profiles; no role is singled out as hiring intent
        companies, observations, _ = await run_profile_stage(
            state, limit=args.limit, stale_days=args.stale_days, config=config)
        print(f"  Read {companies} sites → {observations} observations (free)")
        print("\n  See what turned up: harvey dashboard → Signals\n")

    asyncio.run(_profile())


async def backfill_contacts(make_scout, state, limit: int) -> tuple[int, int]:
    """Run Scout's known-companies backfill until the backlog or ``limit`` runs out.

    Returns (companies checked, contacts found). A fresh Scout per batch, as a
    heartbeat cycle would have, so per-cycle caches don't pile up. Stops when
    a batch holds only companies this run already handed to Scout: those
    weren't marked checked, and asking again would loop forever.
    """
    from harvey.agents.scout import MAX_COMPANIES_PER_CYCLE

    seen: set[str] = set()
    found = 0
    while len(seen) < limit:
        batch = await state.companies_needing_prospects(limit=MAX_COMPANIES_PER_CYCLE)
        new = {c["id"] for c in batch} - seen
        if not new:
            break
        seen |= new
        scout = make_scout()
        try:
            found += await scout._prospect_via_known_companies()
        finally:
            close = getattr(scout, "close_browser", None)
            if close is not None:
                await close()
    return len(seen), found


def cmd_contacts(args):
    """Find contacts at companies already on file. Writes and sends nothing."""
    from harvey.brain import Brain
    from harvey.config import load_config, load_env
    from harvey.state import StateManager
    from harvey.agents.scout import Scout

    async def _contacts():
        config = load_config()
        env = load_env()
        state = StateManager()
        brain = Brain(state, config.brain)
        await state.init_db()

        print(f"\n  Looking for contacts at up to {args.limit} companies on file...\n")
        checked, found = await backfill_contacts(
            lambda: Scout(brain, state, config, env), state, args.limit)
        if not checked:
            print("  Nothing to check. Every company with a website has been "
                  "looked at recently.\n")
            return
        print(f"  Checked {checked} companies → {found} contacts found")
        print("  Get the list: harvey export --companies  (or --all for every contact)\n")

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


def _print_load_progress(step, rows):
    if rows is None:
        print(f"    {step}...", flush=True)
    else:
        print(f"    {step}: {rows:,} rows read", flush=True)


async def _ensure_registry(path: Path, data_dir: Path, name: str = "latest",
                           with_people: bool = True, force: bool = False,
                           offline_ok: bool = False) -> dict:
    """The registry at ``path`` holding dataset ``name`` ("latest": the
    newest on data.gov.ro), downloading any missing file into
    data_dir/<date>/ and building it when it isn't loaded yet. With
    ``offline_ok``, a registry already loaded is used as it is when
    data.gov.ro can't be reached; without one, the newest dataset already
    downloaded into data_dir is loaded instead. Returns its meta."""
    import httpx

    from harvey import registry
    from harvey.collectors import onrc

    roles = [r for r in onrc.FILES if r != "people" or with_people]
    have = registry.status(path)
    if have and have.get("schema") != registry.SCHEMA_VERSION:
        print(f"  {path.name} was loaded by an older Harvey; loading it again.")
        have, force = None, True
    local = onrc.local_dataset(data_dir, name, roles) if name != "latest" else None
    if local:
        dataset, files = local
    else:
        timeout = httpx.Timeout(60.0, read=300.0)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                dataset = await (onrc.latest_dataset(client) if name == "latest"
                                 else onrc.dataset_named(client, name))
                if have and have.get("dataset") == dataset.name and not force:
                    return have

                def progress(role, filename, size):
                    mb = f"{size / 1e6:.0f} MB" if size else "size unknown"
                    print(f"    downloading {filename} ({mb}), once per dataset...",
                          flush=True)

                files = await onrc.fetch_files(client, dataset, data_dir, roles=roles,
                                               progress=progress)
        except httpx.HTTPError as e:
            why = f"data.gov.ro unreachable ({e.__class__.__name__})"
            if have and offline_ok:
                print(f"  {why}; using the registry already loaded ({have.get('dataset')})")
                return have
            local = _newest_local_dataset(data_dir, roles) if name == "latest" else None
            if not local:
                print(f"\n  {why}, and no complete dataset is downloaded in {data_dir}."
                      + (f" The registry holds {have.get('dataset')}."
                         if have else "") + " Try again when online.\n")
                raise SystemExit(2)
            dataset, files = local
            print(f"  {why}; using {dataset.name}, already downloaded")
    if have and have.get("dataset") == dataset.name and not force:
        return have
    print(f"  Loading {dataset.name} (registry as of {dataset.date}) into {path.name}; "
          "a few minutes, once per dataset...", flush=True)
    try:
        return registry.build(files, dataset, path, progress=_print_load_progress)
    except registry.RegistryBusy as e:
        if have and offline_ok:
            print(f"  Another load is running; using the registry already loaded "
                  f"({have.get('dataset')})")
            return have
        print(f"\n  {e}.\n")
        raise SystemExit(2)


def _newest_local_dataset(data_dir: Path, roles):
    """The newest dataset fully downloaded under data_dir/<date>/, or None."""
    from datetime import date

    from harvey.collectors import onrc

    folders = sorted((p.name for p in Path(data_dir).glob("????-??-??") if p.is_dir()),
                     reverse=True)
    for folder in folders:
        try:
            when = date.fromisoformat(folder)
        except ValueError:
            continue
        found = onrc.local_dataset(data_dir, f"firme-{when:%d-%m-%Y}", roles)
        if found:
            return found
    return None


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


# registry pull copies at most this many firms unless --limit says how many.
PULL_WITHOUT_LIMIT = 10_000


def _pull_progress(dry_run: bool):
    verb = "checked" if dry_run else "saved"
    return lambda done, total: print(f"  {verb} {done:,} of {total:,}...", flush=True)


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
            print(f"\n  ONRC open data: {args.dataset} dataset")
            meta = await _ensure_registry(
                path, _onrc_dir(args), args.dataset,
                with_people=args.administrators == "all", force=args.force)
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

    if action == "search":
        found = registry.search(con, words, all_statuses=args.all_statuses,
                                limit=args.limit, offset=args.offset)
        if args.json:
            print(_json.dumps({"query": words, "total": found["total"],
                               "more": found["more"], "offset": args.offset,
                               "firms": found["rows"]}, ensure_ascii=False, indent=2))
        else:
            _print_registry_rows(found["total"], found["rows"], words, args.offset,
                                 found["more"])
        return

    if action == "pull":
        from harvey.config import live_do_not_contact, load_config
        from harvey.state import StateManager

        q = registry.resolve(con, words, all_statuses=args.all_statuses)
        if not q.match:
            print("\n  Say what to pull: words, a CAEN code, a county (BV) or a town.\n")
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
            report = await registry.pull(
                state, con, q, limit=args.limit, with_administrators=with_admins,
                dry_run=args.dry_run, do_not_contact=live_do_not_contact(load_config()),
                progress=_pull_progress(args.dry_run))
            print(f"\n  {report.in_area:,} firm{'' if report.in_area == 1 else 's'} "
                  f"matching \"{words}\" from {meta.get('dataset')}"
                  + (f" (first {args.limit:,})" if args.limit else ""))
            _print_pull_report(report)
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
    from harvey.config import live_do_not_contact, load_config
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
        if not classes and not names and not localities:
            print("\n  Every activity in the whole country is millions of companies: "
                  "give a --county or a --locality, or an activity.\n")
            raise SystemExit(2)
        with_admins = args.administrators == "all"

        path = _registry_path(args)
        meta = await _ensure_registry(path, _onrc_dir(args), "latest",
                                      with_people=with_admins, offline_ok=True)
        print(f"\n  ONRC open data: {meta.get('dataset')} (registry as of {meta.get('date')})")
        if with_admins and meta.get("with_administrators") != "1":
            print("  (this registry was loaded without administrators: "
                  "harvey registry load --force)")

        state = StateManager()
        await state.init_db()
        con = registry.connect(path)
        q = registry.resolve(con, "", classes=classes, counties=names or [],
                             localities=localities, all_statuses=args.include_inactive)
        where = f"{args.locality}, {county}" if args.locality else county
        what = ", ".join(onrc.class_labels(classes)) if classes else "every activity"
        print(f"  Looking for {what} in {where}"
              f"{'' if args.include_inactive else ' (operating companies only)'}...")
        report = await registry.pull(
            state, con, q, with_administrators=with_admins, dry_run=args.dry_run,
            do_not_contact=live_do_not_contact(load_config()), area=where,
            progress=_pull_progress(args.dry_run))
        print(f"  {report.in_area:,} match\n")
        _print_pull_report(report)
        print("\n  Source: ONRC open data, data.gov.ro (CC-BY 4.0)")
        if report.dry_run:
            print("  Dry run: nothing was saved. Re-run without --dry-run to import.\n")
        elif report.errors:
            raise SystemExit(1)
        else:
            print("  Next: harvey contacts (reads the sites of those with one)\n")

    _require_module("onrc", "harvey import-onrc")
    asyncio.run(_import())


def cmd_discover(args):
    """Find businesses. The only stage that spends money, so it estimates first."""
    from harvey.state import StateManager
    from harvey.config import load_config, load_env
    from harvey.collectors.discover import (
        PROVIDERS, build_queries, preset_problem, provider_menu, run_discovery,
    )

    from harvey.modules import switches

    async def _discover():
        enabled = await _modules_on()
        if args.providers:
            print("\n  Discovery providers")
            print("  " + "=" * 68)
            for m in provider_menu(load_env().model_dump(), enabled):
                mark = "ready" if m["configured"] else "needs setup"
                if not m["enabled"]:
                    mark = f"off: harvey modules enable {m['module']}"
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

        if args.provider not in PROVIDERS:
            print(f"\n  Unknown provider {args.provider!r}. "
                  f"See: harvey discover --providers\n")
            return
        off = switches.discovery_problem(enabled, args.provider,
                                         getattr(args, "preset", "") or "")
        if off:
            print(f"\n  {off}\n")
            raise SystemExit(2)

        # Fail before collection; estimates need no credentials.
        # An unconfigured provider used to end in one quiet "stopped
        # early" line and exit code 0: a silent no-op that reads as
        # success.
        provider = PROVIDERS[args.provider]
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
        problem = preset_problem(preset, args.provider, config=config)
        if problem:
            print(f"\n  {problem}\n")
            sys.exit(2)
        queries = build_queries(config, cities=cities,
                                depth=args.depth, limit=args.limit, preset=preset)

        report = await run_discovery(state, config, args.provider, queries,
                                     max_spend=args.max_spend, dry_run=True)
        print(f"\n  {len(queries)} queries via {PROVIDERS[args.provider].label}")
        for q in queries[:8]:
            print(f"    - {q.keyword()}" + ("" if q.coordinate else
                  "   (no coordinates — add icp.geo_coordinates for radius search)"))
        if len(queries) > 8:
            print(f"    ... and {len(queries) - 8} more")
        print(f"\n  Estimated cost: ${report.estimated_cost:.4f}"
              f"   (cap: ${args.max_spend:.2f})")

        if args.estimate:
            print("\n  Estimate only. Re-run without --estimate to collect.\n")
            return

        print("\n  Running...\n")
        from harvey.pipeline import run_prospecting

        result = await run_prospecting(
            state, config, args.provider, queries,
            max_spend=args.max_spend, profile=not args.no_profile,
        )
        r = result.discover or {}
        ran = r.get("queries_ran", 0)
        print(f"  DISCOVER — {r.get('found', 0)} results across "
              f"{ran} of {r.get('queries', 0)} queries")
        print(f"    {r.get('new_companies', 0)} new companies, "
              f"{r.get('known_companies', 0)} already known")
        print(f"    {r.get('junk', 0)} filtered out as directories/aggregators")
        # Only when it happened: a "0 skipped" line every run teaches people to
        # stop reading the block, and this is the line worth noticing.
        if r.get("excluded"):
            print(f"    {r['excluded']} skipped — on your do-not-contact list")
        print(f"    {r.get('observations', 0)} observations recorded")
        print(f"    actual cost: ${r.get('actual_cost', 0):.4f}")
        if r.get("stopped"):
            print(f"    stopped early: {r['stopped']}")

        if args.no_profile:
            print("\n  PROFILE — skipped (--no-profile)")
        else:
            print(f"\n  PROFILE — {result.profiled_companies} sites read, "
                  f"{result.profile_observations} observations  (free)")

        for err in result.errors[:5]:
            print(f"    ! {err}")

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
    "name": "same name in the same town",
    "different_websites": "different websites, so the name alone doesn't count",
}
_MERGE_REFUSED = {
    "different_cui": "they have different tax codes (CUI): two companies, never merged",
    "different_reg": "they have different registration numbers: two companies, never merged",
    "already_merged": "one of them is already merged; undo that first",
    "survivor_merged": "the company it joined has since been merged itself; undo that first",
    "not_merged": "that company isn't merged",
    "not_found": "no such company",
    "same_company": "that is the same company twice",
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


def cmd_duplicates(args):
    """Possible duplicates: pairs the resolver wasn't sure enough to merge,
    and merges in force. Merge, keep apart, or undo a merge."""
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
        bits += c.get("domains") or []
        return " · ".join(bits) + f"  [{c['id']}, {', '.join(c.get('sources') or []) or '?'}]"

    async def _duplicates():
        state = StateManager()
        await state.init_db()
        try:
            if args.merge:
                keep, merged = args.merge
                await state.merge_companies(keep, merged)
                print(f"\n  Merged {merged} into {keep}. Undo with: harvey duplicates --undo {merged}\n")
                return
            if args.separate:
                await state.separate_companies(*args.separate)
                print("\n  Kept apart: they won't be suggested again.\n")
                return
            if args.undo:
                done = await state.undo_merge(args.undo)
                print(f"\n  Undone: {done['merged']} is its own company again, "
                      f"apart from {done['survivor']}.\n")
                return
        except MergeRefused as e:
            print(f"\n  Can't: {_MERGE_REFUSED.get(e.code, e.code)}.\n")
            sys.exit(1)

        pending = await state.duplicates("pending", args.limit)
        print(f"\n  Possible duplicates — {len(pending)} to decide")
        print("  " + "=" * 66)
        for p in pending:
            print(f"\n  {round(p['score'] * 100)}%  {_match_reasons(p['reasons'])}")
            print(f"    A  {side(p['a'])}")
            print(f"    B  {side(p['b'])}")
            print(f"    same:      harvey duplicates --merge {p['company_a']} {p['company_b']}")
            print(f"    different: harvey duplicates --separate {p['company_a']} {p['company_b']}")
        merged = await state.merges(args.limit)
        if merged:
            print(f"\n  Merged — {len(merged)} (newest first)")
            for m in merged:
                who = "automatically" if m["decided_by"] == "auto" else "by you"
                print(f"\n    {side(m['b'])}")
                print(f"      joined {side(m['a'])} {who}: {_match_reasons(m['reasons'])}")
                print(f"      undo: harvey duplicates --undo {m['merged']}")
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
                     help="Discovery provider (default: osm — free, no account)")
    sub.add_argument("--providers", action="store_true",
                     help="List every provider with cost and setup, then exit")
    sub.add_argument("--estimate", action="store_true",
                     help="Print projected spend and exit without calling anything")
    sub.add_argument("--city", default="",
                     help='Semicolon-separated cities, e.g. "Denver, CO;Dallas, TX" '
                          '(default: icp.geography)')
    sub.add_argument("--depth", type=int, default=30,
                     help="SERP depth (default: 30 — depth 100 costs 10x since Sept 2025)")
    sub.add_argument("--limit", type=int, default=100,
                     help="Max records per query (default: 100)")
    sub.add_argument("--max-spend", type=float, default=1.0,
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
                     help="Don't profile what was found (profiling is free)")
    sub.set_defaults(func=cmd_discover)

    sub = subparsers.add_parser(
        "profile", help="Read discovered companies' websites (free, no Claude)")
    sub.add_argument("--limit", type=int, default=200,
                     help="Max sites to read (default: 200)")
    sub.add_argument("--stale-days", type=int, default=90,
                     help="Re-read a site after this many days (default: 90)")
    sub.set_defaults(func=cmd_profile)

    sub = subparsers.add_parser(
        "contacts",
        help="Find contacts at companies already on file (reads their sites; "
             "no Claude calls, writes and sends nothing)",
        description="Reads each company's own website for its owner or team and "
                    "resolves their email. No Claude calls and nothing is written "
                    "or sent, but email verification uses your Reoon/ZeroBounce/"
                    "Hunter credits when those keys are set.")
    sub.add_argument("--limit", type=int, default=200,
                     help="Max companies to check, in batches of 10 (default: 200)")
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
    _registry_common(r)
    sub.set_defaults(func=cmd_registry)

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
    sub.add_argument("--data-dir", default="",
                     help="Where the dataset files are kept (default: data/onrc)")
    sub.add_argument("--administrators", choices=("all", "none"), default="all",
                     help="all: name each company's administrators (when CONTACT_FOUND "
                          "is confirmed; the default); none: leave them out")
    sub.add_argument("--keep-download", action="store_true",
                     help=argparse.SUPPRESS)     # downloads are always kept now
    sub.add_argument("--registry", default="", help=argparse.SUPPRESS)
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
        "duplicates", help="Companies that may be on file twice: merge, keep apart, undo")
    group = sub.add_mutually_exclusive_group()
    group.add_argument("--merge", nargs=2, metavar=("KEEP", "MERGED"),
                       help="Same company: MERGED's facts move onto KEEP")
    group.add_argument("--separate", nargs=2, metavar=("A", "B"),
                       help="Different companies: stop suggesting the pair")
    group.add_argument("--undo", metavar="MERGED",
                       help="Undo the merge that took this company")
    sub.add_argument("--limit", type=int, default=50, help="How many to list (default 50)")
    sub.set_defaults(func=cmd_duplicates)

    sub = subparsers.add_parser(
        "retry-writes", help="Retry prospects the Writer couldn't write an email for")
    sub.set_defaults(func=cmd_retry_writes)

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
