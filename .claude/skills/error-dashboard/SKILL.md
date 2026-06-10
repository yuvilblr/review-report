---
name: error-dashboard
description: Pull recent error logs from Datadog and generate a self-contained HTML dashboard with root cause analysis. Usage - /error-dashboard [window] [service] [env]
argument-hint: [window] [service] [env]
allowed-tools: Bash(python3 *), Bash(test *), Bash(echo *), Read
---

## Generate Error Dashboard

Pull errors from Datadog for the given service/env/window and generate a self-contained HTML dashboard at `docs/error-dashboard.html`.

### Inputs

Parse `$ARGUMENTS` as positional args (space-separated). Apply defaults:
- arg 1: `window` — Datadog time-range expression like `2d`, `4h`, `7d` (default: `2d`)
- arg 2: `service` — Datadog service tag (default: `rqillp-lp`)
- arg 3: `env` — Datadog env tag (default: `rqillp-lp-preprod-eu`)

### Step 1 — Verify Datadog credentials are in env

Check `DD_API_KEY` and `DD_APP_KEY`:

```bash
test -n "$DD_API_KEY" && test -n "$DD_APP_KEY" && echo "credentials present" || echo "credentials MISSING"
```

If missing, tell the user:

> Datadog credentials not in env. Please export both before running this skill:
> ```bash
> export DD_API_KEY='<your 32-char API key>'
> export DD_APP_KEY='<your 40-char Application key>'
> ```
> Then re-run `/error-dashboard`.

DO NOT ask the user to paste keys in chat. Keys must come from env vars only.

### Step 2 — Run the generator script

```bash
python3 /workspace/lp-ui/.claude/skills/error-dashboard/generate-dashboard.py \
  --window "<window>" \
  --service "<service>" \
  --env "<env>" \
  --output "/workspace/lp-ui/docs/error-dashboard.html"
```

The script:
1. Fetches Pino `@level:50/40` logs and `status:error` logs (LD SDK), paginated
2. Classifies by source
3. Applies the embedded RCA catalog
4. Writes a self-contained HTML file (inline CSS, no external dependencies)
5. Lists any unknown patterns on stderr

### Step 3 — Handle unknown patterns

If the script reports unknown patterns on stderr:

> Found N new error patterns not in the catalog. The dashboard renders them under "Other / Uncategorized" with a placeholder RCA. Want me to investigate and add RCAs for them in [.claude/skills/error-dashboard/generate-dashboard.py](.claude/skills/error-dashboard/generate-dashboard.py)?

If the user says yes:
- Pull sample log lines for each unknown pattern (use `curl` to query Datadog)
- Identify the responsible context/class
- Add a new entry to the `CATALOG` constant in `generate-dashboard.py`
- Re-run the script

### Step 4 — Report

After successful generation, output:

> ✓ Dashboard regenerated: [docs/error-dashboard.html](docs/error-dashboard.html)
> 
> {total_events} events • {pattern_count} patterns • {high_count} high severity
>
> Open the file in your browser to view.

### Rules

- **NEVER paste API keys into chat or save them to files.** Always read from `$DD_API_KEY` / `$DD_APP_KEY` env vars.
- The dashboard MUST stay self-contained — inline CSS, no `<script src>`, no `<link href>`.
- If `service`/`env` yield zero events, relay that to the user instead of generating an empty dashboard.
- The output path stays at `docs/error-dashboard.html` unless the user explicitly asks for another location.
