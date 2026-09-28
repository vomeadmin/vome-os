"""
vomeos/cli.py

    py -m vomeos.cli validate            the onboarding gate. CI runs this.
    py -m vomeos.cli list                every agent, division and tier
    py -m vomeos.cli describe <agent>    the manifest and composed prompt
    py -m vomeos.cli skills              the central skills list
    py -m vomeos.cli runs [agent]        recent runs from the trace
    py -m vomeos.cli scoreboard [days]   per-agent totals

`validate` exits 1 when anything blocking is found, so it can be the required
check that stands between writing an agent and scheduling one. It needs no API
key and no database.
"""

from __future__ import annotations

import sys

from vomeos import onboarding, tiers
from vomeos.composer import compose_system_prompt
from vomeos.config import HOME as ROOT
from vomeos.registry import get_agent, list_agents, list_skills, skill_users


def cmd_validate() -> int:
    findings, blocking = onboarding.check_all()
    agents = list_agents()

    if not findings:
        print(f"OK: {len(agents)} agent(s), no findings.")
        return 0

    print(f"Checked {len(agents)} agent(s), {len(list_skills())} skill(s).\n")
    for finding in findings:
        print(finding)

    advisory = len(findings) - blocking
    print(f"\n{blocking} blocking, {advisory} advisory.")
    if blocking:
        print(
            "\nBlocking findings must be fixed before this agent is "
            "scheduled. See RUNTIME.md."
        )
    return 1 if blocking else 0


def cmd_list() -> int:
    agents = list_agents()
    if not agents:
        print("No agents yet. See RUNTIME.md to hire one.")
        return 0
    width = max(len(a.qualified) for a in agents)
    print(f"{'AGENT'.ljust(width)}  TIER      CLIENT  TITLE")
    for a in agents:
        facing = "yes" if a.guards.client_facing else "no"
        print(
            f"{a.qualified.ljust(width)}  "
            f"{a.model.tier.ljust(8)}  {facing.ljust(6)}  "
            f"{a.identity.title or a.identity.name}"
        )
    print("\nBench:")
    for tier, model in tiers.describe().items():
        print(f"  {tier.ljust(9)} {model}")
    return 0


def cmd_describe(qualified: str) -> int:
    manifest = get_agent(qualified)
    print(f"{manifest.qualified} ({manifest.identity.title})")
    print(f"  division      {manifest.identity.division}")
    print(f"  reports to    {manifest.identity.reports_to or '(unset)'}")
    print(f"  hired         {manifest.identity.hired or '(unset)'}")
    print(
        f"  model         {manifest.model.tier} -> "
        f"{tiers.resolve(manifest.model.tier)}"
    )
    print(f"  max tokens    {manifest.model.max_tokens}")
    print(f"  output        {manifest.job.output}")
    print(f"  skills        {', '.join(manifest.job.skills) or '(none)'}")
    print(
        f"  context       "
        f"{', '.join(manifest.job.requires_context) or '(none)'}"
    )
    print(f"  guards        {', '.join(manifest.guards.output) or '(none)'}")
    print(f"  client facing {manifest.guards.client_facing}")
    print(f"  ceo approval  {manifest.approval.requires_ceo_approval}")
    print(f"  escalates to  {manifest.approval.escalate_to or '(unset)'}")

    findings = onboarding.check_agent(manifest)
    print("\nOnboarding gate:")
    if findings:
        for finding in findings:
            print(finding)
    else:
        print("  clear")

    prompt = compose_system_prompt(manifest)
    print(f"\nComposed system prompt: {len(prompt.split())} words\n")
    print(prompt)
    return 0


def cmd_skills() -> int:
    skills = list_skills()
    if not skills:
        print("No skills yet.")
        return 0
    for skill in skills:
        users = skill_users(skill.name)
        print(f"{skill.name}")
        print(f"  {skill.description or '(no description)'}")
        print(f"  owner: {skill.owner or '(unset)'}")
        print(f"  used by: {', '.join(users) or '(nobody)'}")
        print()
    return 0


def cmd_jobs() -> int:
    """What beat would schedule, and where it would send it.

    Loads the job modules the same way a worker does, so this shows what a
    real beat process would see rather than what the registry happens to hold
    in this interpreter.
    """
    from vomeos.worker import describe, list_jobs, load_job_modules

    load_job_modules()
    shape = describe()
    print(f"broker    {shape['broker']}")
    print(f"timezone  {shape['timezone']}")
    print(f"queues    {', '.join(shape['queues'])}")

    jobs = list_jobs()
    if not jobs:
        print(
            "\nNo jobs registered. Set VOMEOS_JOB_MODULES to the module "
            "that calls register_job (for example: support_jobs)."
        )
        return 0

    width = max(len(j.key) for j in jobs)
    header = "JOB".ljust(width)
    print(f"\n{header}  CRON             QUEUE     CLAIM")
    for job in jobs:
        print(
            f"{job.key.ljust(width)}  {job.cron.ljust(16)} "
            f"{job.queue.ljust(9)} {job.claim or 'none'}"
        )
    if shape["eager"]:
        print(
            "\nEager mode: no broker configured, so tasks run inline and "
            "beat is not driving anything. The in-process APScheduler in "
            "main.py still owns the schedule."
        )
    return 0


def cmd_runs(agent: str = "") -> int:
    from vomeos import trace

    rows = trace.recent(agent=agent, limit=30)
    if not rows:
        print("No runs recorded.")
        return 0
    for row in rows:
        stamp = str(row["created_at"])[:19]
        subject = f"{row['subject_type'] or '-'}:{row['subject_id'] or '-'}"
        print(
            f"{stamp}  {row['status'].ljust(7)}  {row['agent']}  "
            f"{subject}  {row['duration_ms']}ms  "
            f"{row['input_tokens']}in/{row['output_tokens']}out"
            + (f"  {row['error']}" if row["error"] else "")
        )
    return 0


def cmd_scoreboard(days: str = "7") -> int:
    from vomeos import trace

    rows = trace.summary(days=int(days))
    if not rows:
        print("No runs recorded.")
        return 0
    print(f"Last {days} day(s)\n")
    print("AGENT                          RUNS   OK  BLOCK  ERR   TOKENS")
    for row in rows:
        total = int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0)
        print(
            f"{row['agent'][:29].ljust(30)} "
            f"{str(row['runs']).rjust(4)} "
            f"{str(row['ok_runs']).rjust(4)} "
            f"{str(row['blocked_runs']).rjust(6)} "
            f"{str(row['error_runs']).rjust(4)} "
            f"{str(total).rjust(8)}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    command = args[0] if args else "validate"
    rest = args[1:]

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    if command == "validate":
        return cmd_validate()
    if command == "list":
        return cmd_list()
    if command == "describe":
        if not rest:
            print("usage: describe <division.agent>")
            return 2
        return cmd_describe(rest[0])
    if command == "skills":
        return cmd_skills()
    if command == "jobs":
        return cmd_jobs()
    if command == "runs":
        return cmd_runs(rest[0] if rest else "")
    if command == "scoreboard":
        return cmd_scoreboard(rest[0] if rest else "7")

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
