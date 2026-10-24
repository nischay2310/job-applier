# Security

## Reporting a problem

Please report vulnerabilities privately through GitHub: **Security** tab,
**Report a vulnerability**. Don't open a public issue for them.

## What this app is, and isn't

Job Applier is a **single-user tool that runs on your own computer**. The
dashboard has no login, so:

- It listens on `127.0.0.1` only by default. Don't expose it to a network
  without a reverse proxy that adds authentication and HTTPS.
- It only answers requests that name this machine (`Host` header) and refuses
  cross-site writes, so other websites can't drive it through your browser.
  Behind a proxy, list your host and origin in `JOB_APPLIER_ALLOWED_HOSTS` and
  `JOB_APPLIER_ALLOWED_ORIGINS`.
- Your resume, answers and logins stay on your machine: `data/` and the Chrome
  profile are git-ignored. Resume reading uses a local model (Ollama).

## Never commit

API keys, `.env`, cookies, browser profiles, `data/*.json`, resumes, or the
tracker. GitHub secret scanning and push protection are on for this repository.
