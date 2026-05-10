# Security policy

## Supported versions

Only the most recent minor release receives security fixes. Older versions
are out of scope.

| Version | Supported |
|---|---|
| 0.2.x | ✅ |
| 0.1.x and earlier | ❌ |

## Reporting a vulnerability

**Do not file public issues for security bugs.**

Report privately via one of the following channels (in order of preference):

1. GitHub Private Vulnerability Reporting — open the repository's
   "Security" tab and use "Report a vulnerability". This is the preferred
   channel because it produces an audit trail visible to maintainers without
   exposing the report publicly.
2. Email: x.jjtrader@gmail.com.

Include in the report:

- A description of the vulnerability and the affected component (data feed,
  execution, risk manager, etc.).
- Reproduction steps or proof-of-concept.
- The version / commit hash you tested against.
- Your assessment of severity and exploitability.
- Whether you have already disclosed to anyone else.

You will receive an acknowledgement within 72 hours and a substantive
response within 7 days. Coordinated disclosure target: 90 days from report,
shorter if active exploitation is observed.

## Out of scope

- Bugs that require already-compromised host access (root on the droplet,
  read access to `.env`) — those are operational issues, not vulnerabilities.
- Findings against deprecated versions.
- Polymarket / Polygon / upstream library bugs — please report those upstream.
- Theoretical issues with no demonstrated path to harm.

## In scope

- Anything that lets an unauthenticated party place, cancel, or modify orders.
- Anything that lets an unauthenticated party read trading state, fills, PnL,
  or wallet identifiers.
- Anything that lets an authenticated dashboard user escalate beyond their
  documented scope (once the dashboard ships).
- Secret-leak vectors in logs, error messages, metrics endpoints, telegram
  alerts, or discord webhooks.
- Supply-chain issues (typosquats, hijacked dependencies, compromised
  base-image digests).
- Container-escape vectors specific to this image.
