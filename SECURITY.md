# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 1.x     | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

**⚠️ Please do NOT open a public GitHub issue for security vulnerabilities.**

If you discover a security vulnerability in AlphaBook, please report it
responsibly by emailing **[ball6911@ox.ac.uk](mailto:ball6911@ox.ac.uk)**.

### What to Include

Please provide as much of the following information as possible:

- A clear description of the vulnerability
- Steps to reproduce the issue
- The potential impact (e.g., data exposure, privilege escalation)
- Any suggested fixes or mitigations, if you have them

### What Constitutes a Security Issue

The following are examples of issues we consider security vulnerabilities:

- **Authentication bypass** — circumventing Firebase Auth or session cookie
  verification to access protected routes or impersonate users
- **Data leaks** — unauthorized access to Firestore user data, portfolios,
  trade history, or admin-only information
- **Cross-Site Scripting (XSS)** — injecting malicious scripts via order
  parameters, game names, news content, or other user-controlled input
- **Cross-Site Request Forgery (CSRF)** — tricking an authenticated user into
  performing unintended actions (e.g., placing orders, modifying game settings)
- **WebSocket hijacking** — unauthorized access to real-time order book streams
  or injection of malicious messages
- **Insecure Direct Object References (IDOR)** — accessing or modifying another
  user's resources by manipulating identifiers

### What Is NOT a Security Issue

- General bugs (use the [bug report template](https://github.com/snowy615/alphaBook/issues/new?template=bug_report.md))
- Feature requests
- Questions about setup or usage

## Response Timeline

- **Acknowledgment**: We will acknowledge your report within **48 hours**.
- **Assessment**: We will assess the severity and confirm whether the issue is
  a valid security vulnerability within **5 business days**.
- **Resolution**: We will work to release a fix as soon as practicable and will
  coordinate disclosure timing with you.

## Disclosure Policy

We follow a coordinated disclosure process. We ask that you:

1. Allow us a reasonable amount of time to address the vulnerability before
   making any information public.
2. Make a good-faith effort to avoid privacy violations, data destruction, and
   service disruption during your research.

We will credit reporters in release notes unless you prefer to remain anonymous.

## Thank You

We appreciate the security research community's efforts in helping keep
AlphaBook and its users safe.
