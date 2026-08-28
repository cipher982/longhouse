# Security Policy

Longhouse handles raw CLI-agent transcripts, which routinely contain live
credentials. Vulnerability reports are welcome and taken seriously.

## Reporting a vulnerability

Email <support@longhouse.ai>, or open a private report through
[GitHub Security Advisories](https://github.com/cipher982/longhouse/security/advisories/new).

Please don't open a public issue for a vulnerability.

A useful report says what you did, what happened, and what an attacker gets out
of it. A short proof of concept helps; raw scanner output usually doesn't.

## What to expect

- An acknowledgement within 5 business days. If you hear nothing by then, send
  it again — mail gets lost.
- Longhouse is maintained by one person, so a fix normally takes longer than
  the acknowledgement. You get the real timeline once the report is triaged.
- We'll tell you when it ships, and credit you in the release notes if you want
  that.

There is no bug bounty, and no compliance program behind this file.

## Scope

In scope: this repository — the Runtime Host (`server/`), web UI (`web/`),
Machine Agent (`engine/`), runner, CLI, and the macOS and iOS apps — plus the
hosted service at longhouse.ai, which runs this code.

Out of scope: the provider CLIs Longhouse observes. Report those to their
vendors.

## Supported versions

The latest release only. Longhouse is pre-1.0 and keeps no maintenance
branches.
