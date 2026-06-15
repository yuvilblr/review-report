#!/usr/bin/env python3
"""
Datadog Error Dashboard Generator
=================================

Pulls error logs from Datadog for a given service/env/window and generates a
self-contained HTML dashboard with Root Cause Analysis for each known pattern.

Usage:
    DD_API_KEY=xxx DD_APP_KEY=yyy python3 generate-dashboard.py \\
      --service rqillp-lp \\
      --env rqillp-lp-preprod-eu \\
      --window 2d \\
      --output /workspace/lp-ui/docs/error-dashboard.html

Credentials:
    Required via env vars:
      DD_API_KEY  - Datadog API key (32-char hex)
      DD_APP_KEY  - Datadog Application key (40-char or ddapp_-prefixed)

Datadog site:
    Defaults to datadoghq.com (US1). Override with --site if needed.
"""

import argparse
import html
import json
import os
import re
import sys
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone


# ----------------------------------------------------------------------------
# RCA CATALOG
# ----------------------------------------------------------------------------
# Each entry maps a pattern (substring or regex) to an analysis bundle.
# When new error patterns are encountered, the generator prints them to stderr
# so they can be added here.
#
# Fields:
#   match       - dict with 'type' ('contains'|'regex') and 'value' (str)
#   category    - grouping for the dashboard sections
#   severity    - 'high' | 'medium' | 'low'
#   title       - short human-readable title
#   rca         - root-cause analysis text
#   impact      - business/user impact
#   fix         - recommended remediation
#   sources     - which log streams to apply this to: 'nestjs_error',
#                 'nestjs_warn', 'ld_error', 'ld_warn'

CATALOG = [
    # --- LaunchDarkly Configuration ---
    {
        'match': {'type': 'contains', 'value': 'Unknown feature flag "maintenance"'},
        'category': 'LaunchDarkly Configuration',
        'severity': 'medium',
        'title': 'Unknown feature flag "maintenance"',
        'sources': ['ld_error'],
        'rca': "The app declares `MAINTENANCE: 'maintenance'` in `src/config/flags.ts` and evaluates it on every render of `MaintenanceGate`. The LaunchDarkly project for `rqi-preprod-eu` does NOT have this flag defined, so the SDK returns the fallback value AND logs this warning on every evaluation. The count scales with render volume across all pods.",
        'impact': "No functional impact — gate falls back to `MAINTENANCE_OFF` and the app works. But generates very high log volume, inflating error dashboards and consuming log ingestion budget.",
        'fix': "Create the flag in LaunchDarkly UI for the `rqi-preprod-eu` environment:\n  1. Flag `maintenance` — JSON variant, default `{\"enabled\":false,\"endsOnDate\":null,\"endsOnTime\":null}`\n  2. Flag `maintenanceAllowVpn` — boolean, default `false`\nOnce defined the SDK stops emitting this warning.",
    },
    {
        'match': {'type': 'contains', 'value': 'waitForInitialization timed out'},
        'category': 'LaunchDarkly Configuration',
        'severity': 'high',
        'title': 'LaunchDarkly waitForInitialization timed out',
        'sources': ['ld_error', 'nestjs_error'],
        'rca': "At application startup the LaunchDarkly SDK could not complete its bootstrap handshake with `stream.launchdarkly.com` within 10 seconds. Causes: slow/blocked egress, LD service degradation, or the timeout being too aggressive.",
        'impact': "Pod boots in DEGRADED MODE — all flag evaluations return fallback defaults until the connection eventually establishes. Feature-flagged behavior may be unavailable.",
        'fix': "1. Verify egress to `stream.launchdarkly.com:443` is allowed and stable from the cluster\n2. Check LaunchDarkly status page for incidents at this timestamp\n3. Consider bumping `waitForInitialization` timeout from 10s to 20-30s for cold-start tolerance\n4. Add a health check that probes a known flag and restarts pod if degraded mode persists",
    },
    {
        'match': {'type': 'contains', 'value': 'Received I/O error'},
        'category': 'LaunchDarkly Connectivity',
        'severity': 'medium',
        'title': 'LaunchDarkly streaming I/O errors',
        'sources': ['ld_warn'],
        'rca': "LaunchDarkly uses Server-Sent Events for real-time flag updates. The SDK is reporting that the long-lived stream connection drops periodically and reconnects. Likely cause: network proxy/NAT idle timeout terminating connections, firewall connection-timeout, or transient regional network glitches.",
        'impact': "During each disconnection window the pod won't receive real-time flag updates. Once reconnected, it gets the latest state. Brief windows of slightly stale flag values.",
        'fix': "1. Check NAT gateway TCP idle timeout (default 350s on AWS) — may need keepalive\n2. Verify firewall is not intercepting long-lived TCP connections\n3. If using a proxy, ensure SSE/HTTP-streaming is supported\n4. Consider switching SDK to polling mode if streaming continues to be unreliable",
    },

    # --- Cache / Valkey ---
    {
        'match': {'type': 'contains', 'value': 'Valkey client error'},
        'category': 'Cache / Valkey',
        'severity': 'high',
        'title': 'Valkey client error: connect ECONNREFUSED',
        'sources': ['nestjs_error'],
        'rca': "NestJS CacheModule could not connect to the Valkey instance. ECONNREFUSED means the server actively refused the TCP connection — usually meaning the process isn't listening, or a firewall/security group is blocking it. Clustering suggests brief pod restart or network policy update.",
        'impact': "During each ECONNREFUSED any code path using the cache fails. Errors propagate to LicenseController and WebSocket handlers. Users may see slower responses, failed license lookups, dropped WebSocket connections.",
        'fix': "1. Check Valkey pod/instance uptime around the timestamps — was there a restart, deployment, or scaling event?\n2. Verify the IP is the current Valkey endpoint — use a stable DNS name instead of a raw IP\n3. Add circuit-breaker around CacheModule calls so a single Valkey blip doesn't cascade\n4. Ensure CacheModule has connection retry with exponential backoff configured",
    },
    {
        'match': {'type': 'contains', 'value': 'Cache GET failed'},
        'category': 'Cache / Valkey',
        'severity': 'medium',
        'title': 'Cache GET failed: Connection is closed',
        'sources': ['nestjs_warn'],
        'rca': "Cache GET attempt failed because the underlying connection to Valkey was already closed. Downstream effect of ECONNREFUSED above — connection pool entered a closed state. Same root cause window.",
        'impact': "Falls back to re-fetching from the source. Adds latency but no functional break.",
        'fix': "Same root-cause fix as Valkey ECONNREFUSED. Auto-resolves when Valkey is healthy.",
    },

    # --- AWS WebSocket ---
    {
        'match': {'type': 'contains', 'value': 'AWS WebSocket error'},
        'category': 'AWS WebSocket',
        'severity': 'high',
        'title': 'AWS WebSocket error',
        'sources': ['nestjs_error'],
        'rca': "The application maintains an AWS WebSocket connection (likely for real-time course/license state). Errors logged with empty or minimal message body — the error object likely contained an `AggregateError [ECONNREFUSED]` that didn't serialize. Timestamps cluster with Valkey ECONNREFUSED errors, suggesting cascade: when Valkey went down, the WebSocket handler couldn't read its state and the connection dropped.",
        'impact': "Real-time course progress / WebSocket-pushed updates fail during the outage window. Users may not see live status updates until next poll/refresh.",
        'fix': "1. Improve error logging in the WebSocket error handler — serialize the full error including stack to the `msg` field\n2. Decouple WebSocket reconnection from Valkey availability (cache miss should not break WS)\n3. Add reconnection with exponential backoff",
    },
    {
        'match': {'type': 'contains', 'value': 'Disconnected from AWS'},
        'category': 'AWS WebSocket',
        'severity': 'medium',
        'title': 'AWS WebSocket disconnect → reconnect loop',
        'sources': ['nestjs_warn'],
        'rca': "AWS WebSocket client repeatedly disconnects and reconnects. Most disconnections recover on the first retry (5000ms backoff). High volume suggests this is constant background churn — likely server-side termination (AWS API Gateway WebSocket idle timeout, NAT gateway TCP idle timeout, or ALB connection settings) that the client recovers from.",
        'impact': "During each ~5s reconnect window, any push messages from AWS are missed. Sustained loss of real-time updates impacts users watching live course state.",
        'fix': "1. Check AWS API Gateway WebSocket idle timeout (default 10 min), NAT gateway TCP idle timeout, ALB connection settings\n2. Implement WebSocket-level keepalive pings to prevent idle timeout\n3. Add metrics tracking time-since-last-successful-message to detect silent drops\n4. Investigate if AWS API Gateway is in a degraded state during these intervals",
    },

    # --- Authentication ---
    {
        'match': {'type': 'regex', 'value': r'^Unauthorized$'},
        'category': 'Authentication',
        'severity': 'low',
        'title': 'Unauthorized',
        'sources': ['nestjs_warn'],
        'rca': "Generic 'Unauthorized' responses returned to clients. Most are expected: expired tokens, missing auth headers, or invalid credentials hitting protected endpoints. Without correlated request paths, hard to tell if any subset is anomalous.",
        'impact': "Expected. Users with expired sessions get prompted to re-login. No action needed unless rate spikes anomalously.",
        'fix': "Not a bug. To improve observability: log the request URL alongside `Unauthorized` so you can distinguish normal expired-token cases from auth misconfiguration.",
    },
    {
        'match': {'type': 'regex', 'value': r'(Refresh token is required|Token has expired|Invalid refresh token)'},
        'category': 'Authentication',
        'severity': 'low',
        'title': 'Refresh token required / expired',
        'sources': ['nestjs_warn'],
        'rca': "Normal session-lifecycle response — when an access token expires, the client should send a refresh token; if it doesn't or the refresh token itself expired, the app responds with these warnings.",
        'impact': "Expected. User is prompted to re-login.",
        'fix': "No fix needed. Could downgrade these from warn to info to reduce noise.",
    },
    {
        'match': {'type': 'contains', 'value': 'Launch token has already been used'},
        'category': 'Authentication',
        'severity': 'low',
        'title': 'Launch token already used',
        'sources': ['nestjs_warn'],
        'rca': "LTI launch tokens are single-use by design (replay protection). Repeated attempts to reuse a token. Could be users clicking the launch link twice, browser back button, or replay of a captured URL.",
        'impact': "User cannot launch the course via the reused link. Working as designed.",
        'fix': "No fix needed. If reuse rate spikes, investigate whether the launch UI is causing accidental double-clicks (button not disabled after click).",
    },

    # --- Self-Registration ---
    {
        'match': {'type': 'regex', 'value': r'(Self-registration failed|users/learners.*email already exists)'},
        'category': 'Self-Registration',
        'severity': 'low',
        'title': 'Self-registration: validation errors',
        'sources': ['nestjs_error', 'nestjs_warn'],
        'rca': "Users attempted self-registration but the upstream RQILLP service returned a validation error (most commonly: email already exists, returning 422). The NestJS controller logs both the controller error and the underlying AxiosError.",
        'impact': "Expected validation behavior. User gets a clear error message and can either log in with the existing account or use a different email.",
        'fix': "No fix needed. Could deduplicate the logs (log only the controller error, suppress the axios re-log).",
    },
    {
        'match': {'type': 'regex', 'value': r'(Invalid token type|Registration token has expired)'},
        'category': 'Self-Registration',
        'severity': 'medium',
        'title': 'Self-registration token errors',
        'sources': ['nestjs_error', 'nestjs_warn'],
        'rca': "Two distinct token errors: (1) 'Invalid token type' — wrong token type sent to self-registration endpoint. (2) 'Registration token has expired' — registration tokens have a TTL; user took too long to complete signup.",
        'impact': "User can't complete self-registration. They need a fresh token.",
        'fix': "1. UX: improve error messaging — tell the user 'Your registration link has expired, request a new one'\n2. Consider extending registration token TTL if expirations are frequent\n3. Track these as funnel-drop-off metrics",
    },

    # --- External Service ---
    {
        'match': {'type': 'regex', 'value': r'organizations/\d+/languages returned status 404'},
        'category': 'External Service / Org Data',
        'severity': 'medium',
        'title': 'Organization not found (404)',
        'sources': ['nestjs_error'],
        'rca': "Organization IDs returned 404 from RQILLP-service when fetching languages. The app handled gracefully by falling back to default languages, but the lookup itself failed. Possible causes: orgs deleted upstream but still referenced locally, data sync lag, or bad orgId being passed.",
        'impact': "Falls back to default language list. Users from those orgs may not see their org-specific languages. The companion 'Vendor not matched' warnings suggest the entire org lookup chain fails — multiple downstream features may be impacted.",
        'fix': "1. Audit org references — sync with RQILLP to detect orgs that no longer exist\n2. Add metric for `org_lookup_404` and alert if rate increases\n3. Consider lazy-cleanup: when 404 received, mark the org reference as inactive locally to avoid repeat lookups",
    },
    {
        'match': {'type': 'contains', 'value': 'Failed to fetch languages'},
        'category': 'External Service / Org Data',
        'severity': 'medium',
        'title': 'Failed to fetch languages — fallback used',
        'sources': ['nestjs_error'],
        'rca': "Same as Organization not found — controller-level log of the underlying 404. The app gracefully falls back to default languages.",
        'impact': "Same as above.",
        'fix': "Same as Organization not found.",
    },
    {
        'match': {'type': 'contains', 'value': 'RQI service call failed'},
        'category': 'External Service / RQI',
        'severity': 'high',
        'title': 'RQI service call retry cycle exhausted',
        'sources': ['nestjs_warn'],
        'rca': "A scheduled/triggered RQI service call failed all 3 retries within a single cycle and is waiting before retrying. The underlying error reason is not logged in the warning body — would need to look at correlated logs/traces.",
        'impact': "Background job didn't complete in cycle 1. If cycle 2+ also fail, downstream data may be stale.",
        'fix': "1. Log the specific error reason on each attempt failure — not just 'RQI service call failed'\n2. Add metrics for `rqi_sync_retry_cycle` count\n3. Alert if more than N cycles required (sustained outage, not transient)",
    },

    # --- Business Logic ---
    {
        'match': {'type': 'contains', 'value': 'User does not own this license'},
        'category': 'Business Logic',
        'severity': 'low',
        'title': 'User does not own this license',
        'sources': ['nestjs_warn'],
        'rca': "User attempted to access a license they don't own. Authorization check working correctly. Could be a deep link to a license that was transferred/revoked, or a bookmarked URL after license expired.",
        'impact': "Access denied. Expected behavior.",
        'fix': "No fix needed. UX could be improved to show 'this license is no longer accessible' instead of generic error.",
    },
    {
        'match': {'type': 'contains', 'value': 'Vendor not matched'},
        'category': 'Business Logic',
        'severity': 'low',
        'title': 'Vendor not matched',
        'sources': ['nestjs_warn'],
        'rca': "Same root cause as 'Organization not found' — vendor lookup yielded 0 organizations because the org IDs don't exist in RQILLP. Vendor lookup follows org lookup, so when org 404s, vendor count is 0.",
        'impact': "Vendor-specific features unavailable for these users.",
        'fix': "Resolves with the org-not-found fix above.",
    },
    {
        'match': {'type': 'contains', 'value': 'Cannot claim certificate'},
        'category': 'Business Logic',
        'severity': 'low',
        'title': 'Cannot claim certificate',
        'sources': ['nestjs_warn'],
        'rca': "User attempted to claim a certificate before completing the curriculum, or with a mismatched attemptId. Could be: attempting to claim too early, attempt state out of sync, or refresh during attempt invalidating attemptId.",
        'impact': "User can't download certificate. May be confused if they think they've completed everything.",
        'fix': "Disable claim button on UI until curriculum.allCompleted=true is server-confirmed. Improve error message to specify whether it's curriculum or attemptId mismatch.",
    },

    # --- Database / Access ---
    {
        'match': {'type': 'contains', 'value': 'User was denied access on the database'},
        'category': 'Database / Access',
        'severity': 'high',
        'title': 'Prisma: database access denied (eu_preprod_lp)',
        'sources': ['nestjs_error', 'nestjs_warn'],
        'rca': "Prisma queries (e.g. lp_user_licenses.findFirst, lp_apps.findUnique) are failing with 'User was denied access on the database eu_preprod_lp'. This is an AUTHORIZATION failure at the database layer, not a network outage — the role the app connects as has lost access to eu_preprod_lp. Likely causes: a revoked or changed GRANT, rotated/expired DB credentials that no longer match, a role/privilege change, or the user being dropped or renamed. This is almost certainly the root cause of the concurrent 'DB health check failed' and 'Service Unavailable Exception' errors in the same window.",
        'impact': "Every request that touches the database (license lookups, app lookups, core flows) fails. The service returns 503 and the DB health check flips unhealthy. User-facing: logins, license access, and registration fail while access is denied.",
        'fix': "1. Check the DB role the app uses against eu_preprod_lp — confirm GRANT/privileges were not revoked or changed\n2. Verify the DB credentials in the app secret match the current database password (an un-propagated rotation looks exactly like this)\n3. Confirm the role still exists and has CONNECT plus the needed SELECT/INSERT privileges on the schema\n4. Review recent DB migrations, infra, or credential-rotation changes around the first-seen time\n5. Health check and 503s clear automatically once access is restored",
    },
    {
        'match': {'type': 'contains', 'value': 'DB health check failed'},
        'category': 'Database / Access',
        'severity': 'high',
        'title': 'DB health check failed',
        'sources': ['nestjs_error', 'nestjs_warn'],
        'rca': "The application database health probe is failing. It clusters with the Prisma 'access denied on eu_preprod_lp' errors and 'Service Unavailable Exception' — the probe runs a query the database is rejecting, most likely because the app role lost access (see 'Prisma: database access denied'). A network/DB-down cause is possible, but the access-denied Prisma errors point to permissions/credentials.",
        'impact': "While failing, the service reports unhealthy and returns 503s; orchestration may restart pods or pull the instance out of rotation.",
        'fix': "Resolve the database access-denied root cause (see 'Prisma: database access denied'); the probe recovers once the app can query eu_preprod_lp. Log the underlying DB error inside the health check so the cause is visible without cross-referencing Prisma logs.",
    },
    {
        'match': {'type': 'contains', 'value': 'Service Unavailable Exception'},
        'category': 'Database / Access',
        'severity': 'high',
        'title': 'Service Unavailable Exception (503)',
        'sources': ['nestjs_error', 'nestjs_warn'],
        'rca': "The app is returning Service Unavailable (503). Timestamps cluster with 'DB health check failed' and the Prisma 'access denied on eu_preprod_lp' errors — the 503 is the user-facing symptom of the database being unreachable or denied. When the DB health check is down, DB-dependent requests short-circuit to 503.",
        'impact': "Users get 503 errors on database-backed endpoints during the window — a direct, user-facing outage for affected flows.",
        'fix': "Fix the underlying database access-denied issue (see 'Prisma: database access denied'). The 503 is a symptom, not the cause.",
    },

    # --- Application ---
    {
        'match': {'type': 'contains', 'value': "Content-Type doesn't match Reply body"},
        'category': 'Application',
        'severity': 'medium',
        'title': 'Content-Type does not match Reply body',
        'sources': ['nestjs_error', 'nestjs_warn'],
        'rca': "Fastify/NestJS is warning that a response Content-Type header does not match the serialized body — typically a handler returning a non-JSON body (string, stream, or error page) on a route declared as JSON without a custom ExceptionFilter. Often a downstream effect of an error path (e.g. a 503 or HTML error body) being returned through a JSON route.",
        'impact': "The response may be mis-serialized or rejected by clients expecting JSON. Frequently coincident with the DB/503 errors above — error responses leaving via the JSON path.",
        'fix': "1. Add a custom ExceptionFilter for non-JSON responses, or ensure error handlers always return JSON\n2. If these spike alongside the DB 503s, they clear when the database issue is fixed.",
    },

    # --- Authentication (additional) ---
    {
        'match': {'type': 'contains', 'value': 'missing required scope'},
        'category': 'Authentication',
        'severity': 'medium',
        'title': 'Missing required OAuth scope',
        'sources': ['nestjs_error', 'nestjs_warn'],
        'rca': "A request or app/integration token lacks a required OAuth scope (e.g. read:licenses). Either a client app was provisioned without the scope, or a token was issued before the scope existed. An authorization-configuration gap, not a server fault.",
        'impact': "The calling app/integration is denied the scoped operation (e.g. reading licenses). Affects only that client until its scopes are corrected.",
        'fix': "1. Identify the client from the request and grant the missing scope in its OAuth client config\n2. Re-issue tokens after updating scopes\n3. If unexpected, verify the client should have that scope at all.",
    },
    {
        'match': {'type': 'contains', 'value': 'Refresh token reuse detected'},
        'category': 'Authentication',
        'severity': 'medium',
        'title': 'Refresh token reuse detected',
        'sources': ['nestjs_error', 'nestjs_warn'],
        'rca': "A refresh token was presented more than once. Refresh-token rotation treats reuse as possible theft/replay and terminates the session (defense in depth). Usually benign — a client retrying with a stale token, a race between concurrent refreshes, or an app holding an old token after rotation — but a sustained spike can indicate token theft.",
        'impact': "The affected session is terminated and the user must log in again. Expected security behavior; only a concern if the rate is anomalously high.",
        'fix': "1. Usually no action — replay protection working as designed\n2. If frequent for legitimate users, check the client for concurrent/duplicate refresh calls or for not persisting the rotated refresh token\n3. If spiking, investigate possible token theft.",
    },
]


# ----------------------------------------------------------------------------
# HTTP CLIENT
# ----------------------------------------------------------------------------

def dd_post(site, path, payload, api_key, app_key):
    url = f"https://api.{site}{path}"
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url, data=data, method='POST',
        headers={
            'Content-Type': 'application/json',
            'DD-API-KEY': api_key,
            'DD-APPLICATION-KEY': app_key,
        }
    )
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            print(f"\nERROR HTTP {e.code} on {path}\n{body}", file=sys.stderr)
            return None
        except (TimeoutError, urllib.error.URLError) as e:
            last_err = e
            print(f"  retry {attempt+1}/3 on {path} (after {e})", file=sys.stderr)
    print(f"\nERROR: {path} failed after 3 attempts ({last_err})", file=sys.stderr)
    return None


# ----------------------------------------------------------------------------
# FETCH
# ----------------------------------------------------------------------------

def fetch_logs(site, api_key, app_key, query, from_, to_, max_pages=20):
    all_events = []
    cursor = None
    for page in range(max_pages):
        page_block = {'limit': 1000}
        if cursor:
            page_block['cursor'] = cursor
        payload = {
            'filter': {'query': query, 'from': from_, 'to': to_},
            'page': page_block,
            'sort': '-timestamp',
        }
        d = dd_post(site, '/api/v2/logs/events/search', payload, api_key, app_key)
        if not d: break
        events = d.get('data', [])
        all_events.extend(events)
        cursor = d.get('meta', {}).get('page', {}).get('after')
        if not cursor or len(events) < 1000:
            break
    return all_events


def fetch_http_spans(site, api_key, app_key, service, env, from_, to_):
    """Pull APM http.request spans for performance analysis."""
    payload = {
        'data': {
            'type': 'search_request',
            'attributes': {
                'filter': {
                    'query': f'env:{env} service:{service} operation_name:http.request',
                    'from': from_,
                    'to': to_,
                },
                'page': {'limit': 1000},
                'sort': '-timestamp',
            }
        }
    }
    d = dd_post(site, '/api/v2/spans/events/search', payload, api_key, app_key)
    if not d:
        return []
    return d.get('data', [])


def compute_slow_apis(spans, slow_threshold_sec=1.0, top_n=15, sample_per_endpoint=5):
    """
    Group spans by resource_name, compute count/avg/p50/p95/p99 in SECONDS,
    return top N where p95 > threshold, sorted by p99 desc.
    Also returns the SLOWEST sample spans per endpoint for the drill-down popup.
    """
    from datetime import datetime
    by_res = defaultdict(list)        # resource → list of duration_seconds
    samples_by_res = defaultdict(list)  # resource → list of sample dicts
    for e in spans:
        a = e.get('attributes', {})
        res = a.get('resource_name') or 'unknown'
        custom = a.get('custom') or {}
        dur_ns = custom.get('duration')
        if dur_ns is None:
            try:
                s = datetime.fromisoformat(a.get('start_timestamp').replace('Z', '+00:00'))
                e2 = datetime.fromisoformat(a.get('end_timestamp').replace('Z', '+00:00'))
                dur_ns = (e2 - s).total_seconds() * 1e9
            except Exception:
                continue
        if dur_ns is None:
            continue
        dur_sec = dur_ns / 1e9  # nanoseconds → seconds
        by_res[res].append(dur_sec)

        # Collect sample span info for the popup
        samples_by_res[res].append({
            'timestamp': a.get('start_timestamp', ''),
            'duration_sec': round(dur_sec, 3),
            'trace_id': a.get('trace_id', ''),
            'span_id': a.get('span_id', ''),
            'service': a.get('service', ''),
            'host': a.get('host', ''),
            'env': a.get('env', ''),
            'status': a.get('status', ''),
            'is_error': a.get('error', 0) == 1 if isinstance(a.get('error'), int) else bool(a.get('error')),
            'http_method': (custom.get('http') or {}).get('method', '') if isinstance(custom.get('http'), dict) else '',
            'http_status_code': (custom.get('http') or {}).get('status_code', '') if isinstance(custom.get('http'), dict) else '',
            'http_url': (custom.get('http') or {}).get('url', '') if isinstance(custom.get('http'), dict) else '',
        })

    def pctl(vals, p):
        vals = sorted(vals)
        if not vals: return 0
        k = int(len(vals) * p / 100)
        return vals[min(k, len(vals) - 1)]

    rows = []
    for res, durs in by_res.items():
        if not durs: continue
        # Keep up to N slowest sample spans per endpoint
        samples = sorted(samples_by_res[res], key=lambda x: -x['duration_sec'])[:sample_per_endpoint]
        rows.append({
            'resource': res,
            'count': len(durs),
            'avg': sum(durs) / len(durs),
            'p50': pctl(durs, 50),
            'p95': pctl(durs, 95),
            'p99': pctl(durs, 99),
            'max': max(durs),
            'samples': samples,
        })
    slow = [r for r in rows if r['p95'] > slow_threshold_sec or r['p99'] > slow_threshold_sec]
    slow.sort(key=lambda r: -r['p99'])
    return slow[:top_n]


# ----------------------------------------------------------------------------
# CLASSIFY
# ----------------------------------------------------------------------------

def classify(events):
    """
    Split events into:
      - nestjs_errors  (Pino @level:50)
      - nestjs_warns   (Pino @level:40)
      - ld_errors      (LaunchDarkly SDK, message starts with 'error:')
      - ld_warns       (LaunchDarkly SDK, message starts with 'warn:')
      - other          (unrecognized format)
    """
    out = {k: [] for k in ('nestjs_errors', 'nestjs_warns', 'ld_errors', 'ld_warns', 'other')}
    for e in events:
        a = e.get('attributes', {})
        nested = a.get('attributes', {}) or {}
        tags = a.get('tags', [])
        pod = next((t.split(':', 1)[1] for t in tags if t.startswith('pod_name:')), 'unknown')

        # Pino-structured (NestJS)
        if 'msg' in nested or 'context' in nested or 'level' in nested:
            entry = {
                'ts': a.get('timestamp'),
                'msg': (nested.get('msg') or '').strip(),
                'context': nested.get('context'),
                'trace_first': (nested.get('trace') or '').split('\n')[0] if nested.get('trace') else '',
                'host': a.get('host'),
                'pod': pod,
                'level': nested.get('level'),
            }
            lvl = nested.get('level')
            if lvl == 50: out['nestjs_errors'].append(entry)
            elif lvl == 40: out['nestjs_warns'].append(entry)
            else: out['other'].append(entry)
            continue

        # LaunchDarkly SDK stdout
        msg = (a.get('message') or '').strip()
        if msg.startswith('error:'):
            out['ld_errors'].append({'ts': a.get('timestamp'), 'msg': msg, 'host': a.get('host'), 'pod': pod})
        elif msg.startswith('warn:'):
            out['ld_warns'].append({'ts': a.get('timestamp'), 'msg': msg, 'host': a.get('host'), 'pod': pod})
        elif msg.startswith('info:'):
            pass  # info, ignore
        else:
            out['other'].append({'ts': a.get('timestamp'), 'msg': msg, 'host': a.get('host'), 'pod': pod})
    return out


# ----------------------------------------------------------------------------
# MATCH AGAINST CATALOG
# ----------------------------------------------------------------------------

def matches(entry_msg, rule):
    if rule['type'] == 'contains':
        return rule['value'] in entry_msg
    if rule['type'] == 'regex':
        return bool(re.search(rule['value'], entry_msg))
    return False


def apply_catalog(classified):
    """
    Group classified events under matched catalog entries.
    Returns: (matched, unmatched_msgs)
    """
    bucket_map = {id(rule): {'rule': rule, 'events': []} for rule in CATALOG}
    unmatched = []

    source_streams = {
        'nestjs_error': 'nestjs_errors',
        'nestjs_warn': 'nestjs_warns',
        'ld_error': 'ld_errors',
        'ld_warn': 'ld_warns',
    }

    for src_name, stream_key in source_streams.items():
        for entry in classified.get(stream_key, []):
            placed = False
            for rule in CATALOG:
                if src_name not in rule.get('sources', []):
                    continue
                if matches(entry['msg'], rule['match']):
                    bucket_map[id(rule)]['events'].append(entry)
                    placed = True
                    break
            if not placed:
                unmatched.append({'source': src_name, **entry})

    matched = []
    for rule_id, bucket in bucket_map.items():
        if not bucket['events']:
            continue
        events = bucket['events']
        rule = bucket['rule']
        timestamps = [e['ts'] for e in events if e.get('ts')]
        pods = Counter(e['pod'] for e in events if e.get('pod'))
        contexts = Counter(e['context'] for e in events if e.get('context'))
        matched.append({
            'rule': rule,
            'count': len(events),
            'first_seen': min(timestamps) if timestamps else '',
            'last_seen': max(timestamps) if timestamps else '',
            'pods': dict(pods),
            'contexts': dict(contexts),
            'sample_msg': events[0]['msg'][:200] if events else '',
        })
    # Fold UNCATALOGUED events into matched so every error is surfaced even without a
    # catalog entry: error-stream events default to HIGH (red), warn-stream to MEDIUM.
    # The catalog only refines this (e.g. downgrading known-benign errors). `unmatched`
    # is still returned for the "add these to the catalog" hint in main().
    uncat = {}
    for u in unmatched:
        src = u.get('source', '')
        sev = 'high' if src in ('nestjs_error', 'ld_error') else 'medium'
        norm = re.sub(r'\s+', ' ', (u.get('msg') or '').strip())[:120] or '(no message)'
        uncat.setdefault((norm, sev), []).append(u)
    for (msg_key, sev), evs in uncat.items():
        timestamps = [e['ts'] for e in evs if e.get('ts')]
        pods = Counter(e['pod'] for e in evs if e.get('pod'))
        level = 'error-level' if sev == 'high' else 'warning-level'
        matched.append({
            'rule': {
                'category': 'Other / Uncategorized',
                'severity': sev,
                'title': msg_key[:80] or '(no message)',
                'rca': f'Not yet in the RCA catalog — surfaced automatically because it is an {level} '
                       'log. Add a CATALOG entry to classify and explain it.',
                'impact': 'Unknown — investigate.',
                'fix': 'Add this pattern to CATALOG in generate-dashboard.py.',
                'match': {'type': 'contains', 'value': msg_key},
            },
            'count': len(evs),
            'first_seen': min(timestamps) if timestamps else '',
            'last_seen': max(timestamps) if timestamps else '',
            'pods': dict(pods),
            'contexts': {},
            'sample_msg': msg_key,
        })

    matched.sort(key=lambda x: -x['count'])
    return matched, unmatched


# ----------------------------------------------------------------------------
# HTML RENDER
# ----------------------------------------------------------------------------

CATEGORY_EMOJI = {
    'LaunchDarkly Configuration': '🎯',
    'LaunchDarkly Connectivity': '🌐',
    'Cache / Valkey': '💾',
    'AWS WebSocket': '🔌',
    'Authentication': '🔐',
    'Self-Registration': '📝',
    'External Service / Org Data': '🏢',
    'External Service / RQI': '🔗',
    'Business Logic': '⚙️',
    'Database / Access': '🗄️',
    'Application': '🧩',
    'Other / Uncategorized': '❓',
}


def esc(s):
    return html.escape(str(s)) if s else ''


def render_slow_apis_table(slow_apis):
    if not slow_apis:
        return ''

    def cell_class(sec):
        if sec > 5: return 'cell-critical'
        if sec > 2: return 'cell-warn'
        return 'cell-slow'

    def fmt(sec):
        if sec < 60:
            return f'{sec:.2f}s'
        if sec < 3600:
            return f'{int(sec // 60)}m {sec % 60:.0f}s'
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        return f'{h}h {m}m'

    rows = ''
    for idx, r in enumerate(slow_apis):
        # JSON-encode samples for the popup, escape for HTML attribute
        samples_json = html.escape(json.dumps(r.get('samples', [])), quote=True)
        resource_attr = html.escape(r['resource'], quote=True)
        rows += f'''
        <tr class="api-row" data-idx="{idx}" data-resource="{resource_attr}" data-samples="{samples_json}">
            <td class="api-name"><code>{esc(r["resource"])}</code> <span class="drill-icon">⤴</span></td>
            <td class="num">{r["count"]:,}</td>
            <td class="num">{fmt(r["avg"])}</td>
            <td class="num">{fmt(r["p50"])}</td>
            <td class="num {cell_class(r["p95"])}">{fmt(r["p95"])}</td>
            <td class="num {cell_class(r["p99"])}">{fmt(r["p99"])}</td>
            <td class="num">{fmt(r["max"])}</td>
        </tr>'''
    return f'''
    <section class="slow-apis-section">
        <h2 class="section-title">🐌 Top {len(slow_apis)} slow APIs <span class="section-stats">p95 or p99 &gt; 1s • click a row for span samples</span></h2>
        <div class="table-wrap">
            <table class="slow-table">
                <thead>
                    <tr>
                        <th>Endpoint</th>
                        <th class="num">Hits</th>
                        <th class="num">avg</th>
                        <th class="num">p50</th>
                        <th class="num">p95</th>
                        <th class="num">p99</th>
                        <th class="num">max</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </section>

    <!-- Span samples modal -->
    <div id="span-modal" class="modal-backdrop" style="display:none;" onclick="if(event.target===this)this.style.display='none'">
        <div class="modal">
            <div class="modal-header">
                <h3 id="modal-title">Span samples</h3>
                <button class="modal-close" onclick="document.getElementById('span-modal').style.display='none'">✕</button>
            </div>
            <div id="modal-body" class="modal-body"></div>
        </div>
    </div>

    <script>
    (function() {{
        const rows = document.querySelectorAll('.api-row');
        const modal = document.getElementById('span-modal');
        const modalTitle = document.getElementById('modal-title');
        const modalBody = document.getElementById('modal-body');

        function escapeHtml(s) {{
            return String(s||'').replace(/[&<>"']/g, c => ({{
                '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
            }})[c]);
        }}

        rows.forEach(row => {{
            row.addEventListener('click', () => {{
                const resource = row.getAttribute('data-resource');
                let samples;
                try {{ samples = JSON.parse(row.getAttribute('data-samples')); }}
                catch(e) {{ samples = []; }}

                modalTitle.textContent = 'Span samples — ' + resource;

                if (!samples.length) {{
                    modalBody.innerHTML = '<p class="no-samples">No span samples captured for this endpoint.</p>';
                }} else {{
                    const html = samples.map((s, i) => {{
                        const errBadge = s.is_error ? '<span class="span-error-badge">ERROR</span>' : '';
                        const statusCode = s.http_status_code ? ` <span class="span-status">${{escapeHtml(s.http_status_code)}}</span>` : '';
                        const method = s.http_method ? `<span class="span-method">${{escapeHtml(s.http_method)}}</span>` : '';
                        return `
                            <div class="span-card">
                                <div class="span-card-head">
                                    <span class="span-idx">#${{i+1}}</span>
                                    <span class="span-duration">${{s.duration_sec.toFixed(2)}}s</span>
                                    ${{method}}
                                    ${{statusCode}}
                                    ${{errBadge}}
                                </div>
                                <div class="span-card-body">
                                    <div class="span-row"><span class="span-label">Time:</span> <code>${{escapeHtml(s.timestamp)}}</code></div>
                                    <div class="span-row"><span class="span-label">Host:</span> <code>${{escapeHtml(s.host)}}</code></div>
                                    <div class="span-row"><span class="span-label">Trace ID:</span> <code>${{escapeHtml(s.trace_id)}}</code></div>
                                    <div class="span-row"><span class="span-label">Span ID:</span> <code>${{escapeHtml(s.span_id)}}</code></div>
                                    ${{s.http_url ? `<div class="span-row"><span class="span-label">URL:</span> <code>${{escapeHtml(s.http_url)}}</code></div>` : ''}}
                                    <div class="span-row">
                                        <a class="trace-link" target="_blank" href="https://app.datadoghq.com/apm/trace/${{encodeURIComponent(s.trace_id)}}">
                                            🔗 Open trace in Datadog
                                        </a>
                                    </div>
                                </div>
                            </div>`;
                    }}).join('');
                    modalBody.innerHTML = html;
                }}
                modal.style.display = 'flex';
            }});
        }});

        document.addEventListener('keydown', e => {{
            if (e.key === 'Escape') modal.style.display = 'none';
        }});
    }})();
    </script>'''


# ----------------------------------------------------------------------------
# RUM (frontend) errors
# ----------------------------------------------------------------------------

def fetch_rum_errors(site, api_key, app_key, app_id, rum_env, from_, to_, max_pages=20):
    """Fetch RUM error events for an application (+ optional env), paginated."""
    query = f'@application.id:{app_id} @type:error'
    if rum_env:
        query += f' env:{rum_env}'
    all_events, cursor = [], None
    for _ in range(max_pages):
        page = {'limit': 1000}
        if cursor:
            page['cursor'] = cursor
        payload = {'filter': {'query': query, 'from': from_, 'to': to_},
                   'page': page, 'sort': '-timestamp'}
        d = dd_post(site, '/api/v2/rum/events/search', payload, api_key, app_key)
        if not d:
            break
        events = d.get('data', [])
        all_events.extend(events)
        cursor = d.get('meta', {}).get('page', {}).get('after')
        if not cursor or len(events) < 1000:
            break
    return all_events


def _norm_rum_msg(msg):
    """Collapse a RUM error message to a stable single-line key for grouping."""
    if not msg:
        return '(no message)'
    line = re.sub(r'\s+', ' ', msg.strip().splitlines()[0])
    return line[:160] or '(no message)'


def aggregate_rum(events):
    """Group RUM error events by normalized message; also break down by source/page."""
    groups, by_source, views = {}, Counter(), Counter()
    for e in events:
        a = e.get('attributes', {})
        nested = a.get('attributes', {}) or {}
        err = nested.get('error', {}) if isinstance(nested.get('error'), dict) else {}
        view = nested.get('view', {}) if isinstance(nested.get('view'), dict) else {}
        msg = err.get('message') or ''
        source = err.get('source') or 'unknown'
        url = view.get('url') or ''
        key = _norm_rum_msg(msg)
        g = groups.setdefault(key, {'message': key, 'source': source, 'count': 0, 'urls': Counter()})
        g['count'] += 1
        if url:
            g['urls'][url] += 1
            views[url] += 1
        by_source[source] += 1
    rows = sorted(groups.values(), key=lambda x: -x['count'])
    return {'total': len(events), 'distinct': len(groups),
            'by_source': dict(by_source), 'top_views': views.most_common(10), 'messages': rows}


def render_rum_section(rum, meta):
    """Render the Frontend (RUM) tab body."""
    if not rum or not rum.get('total'):
        return ('<section class="category-section"><p style="color:#64748b;font-style:italic;">'
                'No RUM frontend errors found in this window.</p></section>')
    console_n = rum['by_source'].get('console', 0)
    source_chips = ''.join(
        f'<span class="pod-chip">{esc(s)}<span class="pod-count">{n:,}</span></span>'
        for s, n in sorted(rum['by_source'].items(), key=lambda x: -x[1])
    )
    rows = ''
    for m in rum['messages'][:40]:
        top_url = m['urls'].most_common(1)[0][0] if m['urls'] else ''
        url_cell = (f'<a href="{esc(top_url)}" target="_blank">{esc(top_url[:70])}</a>'
                    if top_url else '<span style="color:#94a3b8;">—</span>')
        rows += (f'<tr><td><code>{esc(m["message"][:130])}</code></td>'
                 f'<td>{esc(m["source"])}</td>'
                 f'<td class="num">{m["count"]:,}</td>'
                 f'<td>{url_cell}</td></tr>')
    return f'''
    <div class="stats-grid">
        <div class="stat-card high"><div class="stat-value">{rum["total"]:,}</div><div class="stat-label">Frontend errors</div></div>
        <div class="stat-card"><div class="stat-value">{rum["distinct"]:,}</div><div class="stat-label">Distinct messages</div></div>
        <div class="stat-card"><div class="stat-value">{console_n:,}</div><div class="stat-label">Console-source</div></div>
        <div class="stat-card"><div class="stat-value">{len(rum["top_views"])}</div><div class="stat-label">Pages with errors (top)</div></div>
    </div>
    <div class="sev-summary">
        <h3>Error sources</h3>
        <div class="pods-row">{source_chips}</div>
    </div>
    <section class="slow-apis-section">
        <h2 class="section-title">🌐 Top frontend errors <span class="section-stats">RUM · {esc(meta.get("rum_env", ""))} · grouped by message</span></h2>
        <div class="table-wrap">
            <table class="slow-table">
                <thead><tr><th>Error message</th><th>Source</th><th class="num">Count</th><th>Sample page</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </section>'''


def render_html(matched, unmatched, slow_apis, meta, rum_html=''):
    total_events = sum(m['count'] for m in matched)  # matched already includes uncatalogued events
    high = sum(m['count'] for m in matched if m['rule']['severity'] == 'high')
    med = sum(m['count'] for m in matched if m['rule']['severity'] == 'medium')
    low = sum(m['count'] for m in matched if m['rule']['severity'] == 'low')
    unique_pods = set()
    for m in matched:
        unique_pods.update(p for p in m.get('pods', {}).keys() if p and p != 'unknown')
    pod_count = len(unique_pods)

    # Group matched by category. `matched` already includes uncatalogued events (folded in
    # by apply_catalog with auto-severity: errors→high, warns→medium), so no separate handling.
    by_cat = defaultdict(list)
    for m in matched:
        by_cat[m['rule']['category']].append(m)

    # Ordered categories
    cat_order = list(CATEGORY_EMOJI.keys())
    sections_html = []
    for cat in cat_order:
        items = by_cat.get(cat, [])
        if not items: continue
        emoji = CATEGORY_EMOJI.get(cat, '❓')
        cat_total = sum(i['count'] for i in items)
        cards = []
        for m in items:
            r = m['rule']
            pods_html = ''
            if m.get('pods'):
                chips = ''.join(
                    f'<span class="pod-chip">{esc(p[:30])}<span class="pod-count">{n}</span></span>'
                    for p, n in sorted(m['pods'].items(), key=lambda x: -x[1])[:6]
                )
                pods_html = f'<div class="pods-row"><span class="meta-label">Pods:</span>{chips}</div>'
            ts_html = ''
            if m.get('first_seen') or m.get('last_seen'):
                ts_html = f'<div class="timestamps"><span class="meta-label">First:</span> <code>{esc(m["first_seen"] or "-")}</code><span class="meta-label">Last:</span> <code>{esc(m["last_seen"] or "-")}</code></div>'
            sev = r['severity']
            cards.append(f'''
            <div class="card sev-{sev}">
                <div class="card-header">
                    <div class="card-title-row">
                        <span class="sev-badge sev-{sev}">{sev.upper()}</span>
                        <h3 class="card-title">{esc(r["title"])}</h3>
                    </div>
                    <div class="card-count">{m["count"]:,}</div>
                </div>
                <div class="card-body">
                    <div class="pattern-row"><span class="meta-label">Pattern:</span><code class="pattern">{esc(m.get("sample_msg") or r["match"]["value"])}</code></div>
                    {ts_html}
                    {pods_html}
                    <div class="rca-block"><div class="rca-label">📊 Root Cause Analysis</div><div class="rca-text">{esc(r["rca"])}</div></div>
                    <div class="impact-block"><div class="impact-label">💥 Impact</div><div class="impact-text">{esc(r["impact"])}</div></div>
                    <div class="fix-block"><div class="fix-label">🔧 Recommended Fix</div><pre class="fix-text">{esc(r["fix"])}</pre></div>
                </div>
            </div>''')
        sections_html.append(f'''
        <section class="category-section">
            <h2 class="category-title">
                <span class="cat-emoji">{emoji}</span>
                {esc(cat)}
                <span class="cat-stats">{len(items)} pattern{"s" if len(items) > 1 else ""} • {cat_total:,} events</span>
            </h2>
            <div class="cards-grid">{"".join(cards)}</div>
        </section>''')

    sev_bar = f'''
        <div class="sev-bar">
            <div class="sev-segment sev-high" style="flex:{high}" title="High: {high:,}">{high if high > 50 else ""}</div>
            <div class="sev-segment sev-medium" style="flex:{med}" title="Medium: {med:,}">{med if med > 50 else ""}</div>
            <div class="sev-segment sev-low" style="flex:{low}" title="Low: {low:,}">{low if low > 50 else ""}</div>
        </div>'''

    toc_links = []
    for cat in cat_order:
        items = by_cat.get(cat, [])
        if not items: continue
        cat_total = sum(i['count'] for i in items)
        cat_high = sum(1 for i in items if i['rule']['severity'] == 'high')
        badge = f'<span class="toc-high">{cat_high}↑</span>' if cat_high else ''
        toc_links.append(f'''
        <a href="#cat-{cat.replace(" ", "-").replace("/", "-")}" class="toc-link">
            <span class="toc-emoji">{CATEGORY_EMOJI.get(cat, "❓")}</span>
            <span class="toc-name">{esc(cat)}</span>
            <span class="toc-count">{cat_total:,}</span>
            {badge}
        </a>''')

    pattern_count = sum(len(v) for v in by_cat.values())

    # Tabbed UI only when a Frontend (RUM) pane is supplied; otherwise render flat.
    if rum_html:
        tab_bar = ('<div class="tabs">'
                   '<button class="tab-btn active" data-tab="backend">🖥️ Backend · logs &amp; APM</button>'
                   '<button class="tab-btn" data-tab="frontend">🌐 Frontend · RUM</button>'
                   '</div>\n<div id="tab-backend" class="tab-pane active">')
        frontend_pane = f'</div>\n<div id="tab-frontend" class="tab-pane">{rum_html}</div>'
        tab_script = ("<script>\n"
                      "document.querySelectorAll('.tab-btn').forEach(function(btn){\n"
                      "  btn.addEventListener('click', function(){\n"
                      "    document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.remove('active');});\n"
                      "    document.querySelectorAll('.tab-pane').forEach(function(p){p.classList.remove('active');});\n"
                      "    btn.classList.add('active');\n"
                      "    document.getElementById('tab-'+btn.getAttribute('data-tab')).classList.add('active');\n"
                      "  });\n"
                      "});\n</script>")
    else:
        tab_bar = frontend_pane = tab_script = ''

    html_doc = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Error Dashboard — {esc(meta["env"])}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f8fafc; color: #1e293b; line-height: 1.5; font-size: 14px; }}
.container {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
header.dash-header {{ background: linear-gradient(135deg, #1e3a8a 0%, #312e81 100%); color: white; padding: 32px; border-radius: 12px; margin-bottom: 24px; }}
.header-grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 24px; align-items: center; }}
h1 {{ font-size: 28px; margin-bottom: 8px; }}
.subtitle {{ color: rgba(255,255,255,0.85); font-size: 14px; margin-bottom: 16px; }}
.header-meta {{ display: flex; gap: 24px; flex-wrap: wrap; font-size: 13px; }}
.header-meta div {{ display: flex; flex-direction: column; gap: 2px; }}
.header-meta .label {{ color: rgba(255,255,255,0.7); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }}
.header-meta .value {{ font-weight: 600; }}
.stats-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }}
.stat-card {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border-left: 4px solid #3b82f6; }}
.stat-card.high {{ border-left-color: #dc2626; }}
.stat-value {{ font-size: 28px; font-weight: 700; color: #0f172a; }}
.stat-label {{ font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 4px; }}
.sev-summary {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom: 24px; }}
.sev-summary h3 {{ font-size: 12px; text-transform: uppercase; color: #64748b; margin-bottom: 12px; letter-spacing: 0.5px; }}
.sev-bar {{ display: flex; height: 28px; border-radius: 6px; overflow: hidden; }}
.sev-segment {{ color: white; font-weight: 600; font-size: 13px; display: flex; align-items: center; justify-content: center; min-width: 30px; }}
.sev-high {{ background: #dc2626; }}
.sev-medium {{ background: #ea580c; }}
.sev-low {{ background: #65a30d; }}
.sev-legend {{ display: flex; gap: 16px; margin-top: 8px; font-size: 12px; color: #64748b; }}
.sev-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }}
.toc {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom: 24px; }}
.toc h3 {{ font-size: 12px; text-transform: uppercase; color: #64748b; margin-bottom: 12px; letter-spacing: 0.5px; }}
.toc-links {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }}
.toc-link {{ display: flex; align-items: center; gap: 8px; padding: 10px 12px; background: #f1f5f9; color: #1e293b; text-decoration: none; border-radius: 6px; font-size: 13px; transition: background 0.15s; }}
.toc-link:hover {{ background: #e2e8f0; }}
.toc-emoji {{ font-size: 16px; }}
.toc-name {{ flex: 1; }}
.toc-count {{ background: white; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 12px; }}
.toc-high {{ background: #dc2626; color: white; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
section.category-section {{ margin-bottom: 32px; }}
.category-title {{ display: flex; align-items: center; gap: 12px; font-size: 20px; color: #0f172a; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 2px solid #e2e8f0; }}
.cat-emoji {{ font-size: 24px; }}
.cat-stats {{ margin-left: auto; font-size: 12px; color: #64748b; font-weight: normal; }}
.cards-grid {{ display: grid; gap: 16px; }}
.card {{ background: white; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); overflow: hidden; border-left: 4px solid #cbd5e1; }}
.card.sev-high {{ border-left-color: #dc2626; }}
.card.sev-medium {{ border-left-color: #ea580c; }}
.card.sev-low {{ border-left-color: #65a30d; }}
.card-header {{ display: flex; justify-content: space-between; align-items: flex-start; padding: 16px 20px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; }}
.card-title-row {{ display: flex; align-items: center; gap: 12px; flex: 1; }}
.card-title {{ font-size: 16px; font-weight: 600; color: #0f172a; }}
.card-count {{ background: #1e293b; color: white; padding: 4px 12px; border-radius: 999px; font-weight: 700; font-size: 14px; }}
.sev-badge {{ padding: 3px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; letter-spacing: 0.5px; color: white; }}
.sev-badge.sev-high {{ background: #dc2626; }}
.sev-badge.sev-medium {{ background: #ea580c; }}
.sev-badge.sev-low {{ background: #65a30d; }}
.card-body {{ padding: 20px; }}
.meta-label {{ font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; margin-right: 8px; font-weight: 600; }}
.pattern-row, .timestamps, .pods-row {{ margin-bottom: 12px; font-size: 13px; }}
.pattern {{ display: inline-block; background: #f1f5f9; padding: 8px 12px; border-radius: 4px; font-family: "SF Mono", Monaco, Consolas, monospace; font-size: 12px; color: #0f172a; margin-top: 4px; word-break: break-all; }}
.timestamps code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 3px; font-size: 11px; margin-right: 12px; }}
.pods-row {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
.pod-chip {{ background: #eef2ff; color: #3730a3; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-family: "SF Mono", Monaco, Consolas, monospace; display: inline-flex; align-items: center; gap: 6px; }}
.pod-count {{ background: #3730a3; color: white; padding: 1px 6px; border-radius: 999px; font-size: 10px; font-weight: 600; }}
.rca-block, .impact-block, .fix-block {{ margin-top: 16px; padding: 12px 16px; border-radius: 6px; }}
.rca-block {{ background: #f0f9ff; border-left: 3px solid #0284c7; }}
.impact-block {{ background: #fef3c7; border-left: 3px solid #d97706; }}
.fix-block {{ background: #ecfdf5; border-left: 3px solid #059669; }}
.rca-label, .impact-label, .fix-label {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }}
.rca-label {{ color: #0284c7; }}
.impact-label {{ color: #d97706; }}
.fix-label {{ color: #059669; }}
.rca-text, .impact-text {{ font-size: 13px; color: #1e293b; }}
.fix-text {{ font-size: 13px; color: #1e293b; white-space: pre-wrap; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
footer {{ margin-top: 32px; padding: 24px; background: #1e293b; color: rgba(255,255,255,0.7); border-radius: 8px; font-size: 12px; text-align: center; }}
footer code {{ background: rgba(255,255,255,0.1); padding: 1px 6px; border-radius: 3px; font-size: 11px; }}
.section-title {{ display: flex; align-items: center; gap: 12px; font-size: 20px; color: #0f172a; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 2px solid #e2e8f0; }}
.section-stats {{ margin-left: auto; font-size: 12px; color: #64748b; font-weight: normal; }}
.slow-apis-section {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom: 24px; }}
.table-wrap {{ overflow-x: auto; }}
.slow-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.slow-table th {{ text-align: left; padding: 10px 12px; background: #f1f5f9; color: #475569; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; border-bottom: 1px solid #cbd5e1; }}
.slow-table th.num, .slow-table td.num {{ text-align: right; }}
.slow-table td {{ padding: 10px 12px; border-bottom: 1px solid #f1f5f9; }}
.slow-table tr:last-child td {{ border-bottom: none; }}
.slow-table tr.api-row {{ cursor: pointer; }}
.slow-table tr.api-row:hover td {{ background: #eef2ff; }}
.api-name code {{ background: #f1f5f9; padding: 4px 8px; border-radius: 4px; font-family: "SF Mono", Monaco, Consolas, monospace; font-size: 12px; color: #0f172a; }}
.drill-icon {{ color: #94a3b8; margin-left: 6px; font-size: 11px; opacity: 0; transition: opacity 0.15s; }}
.slow-table tr.api-row:hover .drill-icon {{ opacity: 1; }}
.cell-slow {{ color: #ca8a04; font-weight: 600; }}
.cell-warn {{ color: #ea580c; font-weight: 700; }}
.cell-critical {{ color: #dc2626; font-weight: 700; }}
.modal-backdrop {{ position: fixed; inset: 0; background: rgba(15,23,42,0.65); z-index: 1000; display: flex; align-items: flex-start; justify-content: center; padding: 60px 16px 16px; overflow-y: auto; }}
.modal {{ background: white; border-radius: 12px; width: 100%; max-width: 760px; box-shadow: 0 25px 50px rgba(0,0,0,0.3); }}
.modal-header {{ display: flex; justify-content: space-between; align-items: center; padding: 18px 24px; border-bottom: 1px solid #e2e8f0; }}
.modal-header h3 {{ font-size: 16px; font-weight: 600; color: #0f172a; word-break: break-all; }}
.modal-close {{ background: transparent; border: none; font-size: 18px; cursor: pointer; color: #64748b; padding: 4px 8px; border-radius: 4px; }}
.modal-close:hover {{ background: #f1f5f9; color: #0f172a; }}
.modal-body {{ padding: 16px 24px 24px; max-height: 70vh; overflow-y: auto; }}
.no-samples {{ color: #64748b; font-style: italic; }}
.span-card {{ border: 1px solid #e2e8f0; border-radius: 6px; margin-bottom: 12px; }}
.span-card-head {{ display: flex; align-items: center; gap: 8px; padding: 10px 14px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; border-radius: 6px 6px 0 0; }}
.span-idx {{ font-weight: 700; color: #64748b; font-size: 12px; }}
.span-duration {{ background: #1e293b; color: white; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
.span-method {{ background: #3730a3; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
.span-status {{ padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; background: #f1f5f9; color: #0f172a; }}
.span-error-badge {{ background: #dc2626; color: white; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; letter-spacing: 0.5px; }}
.span-card-body {{ padding: 12px 14px; font-size: 13px; }}
.span-row {{ margin-bottom: 6px; }}
.span-label {{ font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; margin-right: 6px; font-weight: 600; }}
.span-row code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 3px; font-size: 12px; font-family: "SF Mono", Monaco, Consolas, monospace; }}
.trace-link {{ color: #0284c7; text-decoration: none; font-size: 13px; font-weight: 600; }}
.trace-link:hover {{ text-decoration: underline; }}
.tabs {{ display: flex; gap: 4px; margin-bottom: 20px; border-bottom: 2px solid #e2e8f0; }}
.tab-btn {{ background: none; border: none; padding: 12px 20px; font-size: 15px; font-weight: 600; color: #64748b; cursor: pointer; border-bottom: 3px solid transparent; margin-bottom: -2px; }}
.tab-btn:hover {{ color: #1e293b; }}
.tab-btn.active {{ color: #1e3a8a; border-bottom-color: #1e3a8a; }}
.tab-pane {{ display: none; }}
.tab-pane.active {{ display: block; }}
@media (max-width: 768px) {{ .stats-grid {{ grid-template-columns: repeat(2, 1fr); }} .toc-links {{ grid-template-columns: 1fr; }} .header-grid {{ grid-template-columns: 1fr; }} }}
</style></head>
<body>
<div class="container">
<header class="dash-header">
<div class="header-grid">
<div>
<h1>🚨 Error Dashboard</h1>
<p class="subtitle">Errors & Root Cause Analysis from Datadog Logs</p>
<div class="header-meta">
<div><span class="label">Service</span><span class="value">{esc(meta["service"])}</span></div>
<div><span class="label">Environment</span><span class="value">{esc(meta["env"])}</span></div>
<div><span class="label">Window</span><span class="value">Last {esc(meta["window"])}</span></div>
<div><span class="label">Datadog Site</span><span class="value">{esc(meta["site"])}</span></div>
</div>
</div>
<div style="text-align:right;font-size:12px;color:rgba(255,255,255,0.7);">Generated<br><strong style="color:white;font-size:14px;">{esc(meta["generated_at"])}</strong></div>
</div>
</header>
{tab_bar}
<div class="stats-grid">
<div class="stat-card"><div class="stat-value">{total_events:,}</div><div class="stat-label">Total events</div></div>
<div class="stat-card"><div class="stat-value">{pattern_count}</div><div class="stat-label">Distinct patterns</div></div>
<div class="stat-card high"><div class="stat-value">{sum(1 for m in matched if m["rule"]["severity"] == "high")}</div><div class="stat-label">High severity patterns</div></div>
<div class="stat-card"><div class="stat-value">{pod_count}</div><div class="stat-label">Pods affected</div></div>
</div>
<div class="sev-summary">
<h3>Severity distribution (by event count)</h3>
{sev_bar}
<div class="sev-legend">
<span><span class="sev-dot sev-high"></span>High: {high:,}</span>
<span><span class="sev-dot sev-medium"></span>Medium: {med:,}</span>
<span><span class="sev-dot sev-low"></span>Low: {low:,}</span>
</div>
</div>
{render_slow_apis_table(slow_apis)}
<div class="toc">
<h3>Jump to category</h3>
<div class="toc-links">{"".join(toc_links)}</div>
</div>
{"".join(sections_html)}
{frontend_pane}
<footer>
<p>Generated from Datadog Logs API for <code>service:{esc(meta["service"])} env:{esc(meta["env"])}</code> over the last {esc(meta["window"])}.</p>
<p style="margin-top:8px;">{total_events:,} events across {pattern_count} patterns • {pod_count} pods affected</p>
</footer>
</div>
{tab_script}
</body></html>'''
    return html_doc


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def build_teams_card(matched, slow_apis, meta, total, dashboard_url):
    """Build a Microsoft Teams Adaptive Card (message envelope) summarizing
    severity counts and the top high-severity patterns ('what needs attention').
    `matched` is already sorted by event count descending."""
    def sev_total(s):
        return sum(m['count'] for m in matched if m['rule']['severity'] == s)
    high, med, low = sev_total('high'), sev_total('medium'), sev_total('low')
    high_patterns = [m for m in matched if m['rule']['severity'] == 'high'][:5]

    alert = high > 0
    body = [
        {'type': 'TextBlock',
         'text': ('🚨 ' if alert else '📊 ') + f"Error Dashboard — {meta['service']}",
         'weight': 'Bolder', 'size': 'Medium', 'wrap': True},
        {'type': 'TextBlock',
         'text': f"{meta['env']} · last {meta['window']} · {meta['generated_at']}",
         'isSubtle': True, 'spacing': 'None', 'wrap': True},
        {'type': 'FactSet', 'facts': [
            {'title': 'Total events', 'value': f"{total:,}"},
            {'title': '🔴 High', 'value': f"{high:,}"},
            {'title': '🟠 Medium', 'value': f"{med:,}"},
            {'title': '🟢 Low', 'value': f"{low:,}"},
            {'title': 'Patterns', 'value': str(len(matched))},
            {'title': 'Slow APIs (>1s)', 'value': str(len(slow_apis))},
        ]},
    ]

    if high_patterns:
        items = '\n'.join(
            f"- **{m['rule']['title']}** ({m['count']:,}) · {m['rule']['category']}"
            for m in high_patterns
        )
        body.append({
            'type': 'Container', 'style': 'attention', 'bleed': True, 'spacing': 'Medium',
            'items': [
                {'type': 'TextBlock', 'text': '⚠️ Needs attention', 'weight': 'Bolder',
                 'color': 'Attention', 'wrap': True},
                {'type': 'TextBlock', 'text': items, 'wrap': True},
            ],
        })
    else:
        body.append({'type': 'TextBlock', 'text': '✅ No high-severity patterns in this window.',
                     'color': 'Good', 'spacing': 'Medium', 'wrap': True})

    return {
        'type': 'message',
        'attachments': [{
            'contentType': 'application/vnd.microsoft.card.adaptive',
            'content': {
                'type': 'AdaptiveCard',
                '$schema': 'http://adaptivecards.io/schemas/adaptive-card.json',
                'version': '1.4',
                'body': body,
                'actions': [
                    {'type': 'Action.OpenUrl', 'title': 'Open dashboard', 'url': dashboard_url},
                ],
            },
        }],
    }


def main():
    p = argparse.ArgumentParser(description="Generate Datadog error dashboard")
    p.add_argument('--window', default='2d', help='Time window (Datadog syntax: 1h/4h/1d/2d/7d)')
    p.add_argument('--service', default='rqillp-lp', help='Service tag')
    p.add_argument('--env', default='rqillp-lp-preprod-eu', help='Environment tag')
    p.add_argument('--site', default='datadoghq.com', help='Datadog site (e.g. datadoghq.com or datadoghq.eu)')
    p.add_argument('--output', default='/workspace/lp-ui/docs/error-dashboard.html', help='Output HTML path')
    p.add_argument('--teams-card', default=None, help='If set, write a Microsoft Teams Adaptive Card (message JSON) summarizing severity + top high-severity patterns to this path')
    p.add_argument('--dashboard-url', default='https://yuvilblr.github.io/review-report/error-dashboard.html', help='URL the Teams card "Open dashboard" button links to')
    p.add_argument('--rum-app-id', default=None, help='RUM application.id; if set, adds a Frontend (RUM) tab analyzing frontend/console errors')
    p.add_argument('--rum-env', default='preprod-eulaerdallearning', help='RUM env tag used to filter frontend errors')
    args = p.parse_args()

    api_key = os.environ.get('DD_API_KEY')
    app_key = os.environ.get('DD_APP_KEY')
    if not api_key or not app_key:
        print("ERROR: DD_API_KEY and DD_APP_KEY must be set in the environment.", file=sys.stderr)
        sys.exit(2)

    from_ = f'now-{args.window}'
    to_ = 'now'

    print(f"Fetching events: service={args.service} env={args.env} window={args.window} ...", file=sys.stderr)

    # 1. Pino-structured (errors + warnings)
    nest_q = f'service:{args.service} env:{args.env} (@level:50 OR @level:40)'
    nest_events = fetch_logs(args.site, api_key, app_key, nest_q, from_, to_)
    print(f"  NestJS Pino events: {len(nest_events)}", file=sys.stderr)

    # 2. status:error (catches LD SDK and anything else)
    err_q = f'service:{args.service} env:{args.env} status:error'
    err_events = fetch_logs(args.site, api_key, app_key, err_q, from_, to_)
    print(f"  status:error events: {len(err_events)}", file=sys.stderr)

    # De-dupe by id
    seen_ids = set()
    all_events = []
    for e in nest_events + err_events:
        eid = e.get('id')
        if eid and eid not in seen_ids:
            seen_ids.add(eid)
            all_events.append(e)

    if not all_events:
        print("\nNo events found in window. Dashboard not generated.", file=sys.stderr)
        sys.exit(3)  # distinct from 1 (uncaught crash) and 2 (missing credentials)

    # 3. APM spans for slow-API table
    spans = fetch_http_spans(args.site, api_key, app_key, args.service, args.env, from_, to_)
    print(f"  APM http.request spans: {len(spans)}", file=sys.stderr)
    slow_apis = compute_slow_apis(spans, slow_threshold_sec=1.0, top_n=15)
    print(f"  Slow APIs (p95 or p99 > 1s): {len(slow_apis)}", file=sys.stderr)

    # Classify and match
    classified = classify(all_events)
    matched, unmatched = apply_catalog(classified)

    if unmatched:
        print(f"\n⚠️  {len(unmatched)} unmatched event(s) — patterns not in catalog:", file=sys.stderr)
        unmatched_patterns = Counter(u['msg'][:120] for u in unmatched)
        for pat, count in unmatched_patterns.most_common(10):
            print(f"    [{count:4}x]  {pat}", file=sys.stderr)
        print("  → Add these to CATALOG in generate-dashboard.py to get RCAs.", file=sys.stderr)

    # 4. RUM frontend errors (optional — only when --rum-app-id is provided)
    rum_html = ''
    if args.rum_app_id:
        print(f"Fetching RUM errors: app={args.rum_app_id} env={args.rum_env} ...", file=sys.stderr)
        rum_events = fetch_rum_errors(args.site, api_key, app_key, args.rum_app_id, args.rum_env, from_, to_)
        print(f"  RUM error events: {len(rum_events)}", file=sys.stderr)
        rum = aggregate_rum(rum_events)
        rum_html = render_rum_section(rum, {'rum_env': args.rum_env})

    # Render
    meta = {
        'service': args.service,
        'env': args.env,
        'window': args.window,
        'site': args.site,
        'rum_env': args.rum_env if args.rum_app_id else '',
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'),
    }
    html_doc = render_html(matched, unmatched, slow_apis, meta, rum_html=rum_html)

    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output, 'w') as f:
        f.write(html_doc)

    total = sum(m['count'] for m in matched)  # matched now includes uncatalogued events
    print(f"\n✓ Dashboard written: {args.output}")
    print(f"  Total events: {total:,}")
    print(f"  Matched patterns: {len(matched)}")
    print(f"  Unmatched patterns: {len(set(u['msg'][:120] for u in unmatched))}")

    # Optional: emit a Microsoft Teams Adaptive Card summarizing what needs attention.
    if args.teams_card:
        card = build_teams_card(matched, slow_apis, meta, total, args.dashboard_url)
        card_dir = os.path.dirname(args.teams_card)
        if card_dir:
            os.makedirs(card_dir, exist_ok=True)
        with open(args.teams_card, 'w') as f:
            json.dump(card, f)
        print(f"  Teams card written: {args.teams_card}")


if __name__ == '__main__':
    main()
