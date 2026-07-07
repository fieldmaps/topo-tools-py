# Security Policy

## Supported Versions

`topo-tools` is pre-1.0; security fixes target the latest released version
only.

## Reporting a Vulnerability

Please do not open a public GitHub issue for security vulnerabilities.

Instead, use [GitHub Security Advisories](https://github.com/fieldmaps/topo-tools-py/security/advisories)
("Report a vulnerability" on the Security tab) to report privately.

Include, where possible:

- Type of vulnerability
- Affected file(s) and version
- Steps to reproduce
- Impact

We'll acknowledge reports and aim to follow up with an assessment; fixes
ship as a new release with details in `CHANGELOG.md`.

## Notes for users

- `topo-tools` reads and writes files at paths you provide, and downloads
  the DuckDB `spatial` extension over the network on first run (see
  [`README.md`](README.md#requirements)). Treat input file paths as
  trusted the same way you would for any file-processing tool.
- Keep `duckdb` and `topo-tools` updated to pick up upstream security
  fixes.
