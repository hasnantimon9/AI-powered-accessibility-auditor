# AccessPilot

AI-powered web accessibility auditor. Give it a URL and it scans the live page with axe-core, asks an LLM to propose fixes for what it finds, applies each fix in a real browser, and re-scans to confirm the fix actually worked, before showing you anything.

---

## Table of contents

1. [Introduction](#introduction)
2. [Technologies](#technologies)
3. [How it works](#how-it-works)
4. [Project layout](#project-layout)
5. [Run locally](#run-locally)
6. [Run with Docker](#run-with-docker)
7. [Configuration](#configuration)
8. [Using the web UI](#using-the-web-ui)
9. [API reference](#api-reference)
10. [Security](#security)
11. [Testing and linting](#testing-and-linting)
12. [Known limitations](#known-limitations)
13. [Roadmap (v2 and beyond)](#roadmap-v2-and-beyond)

---

## Introduction

Most accessibility tools stop at a list of problems. AccessPilot goes one step further: for the issues it can handle, it proposes a concrete fix, applies it to the live page inside a headless browser, re-runs the audit, and keeps the fix only if the violation is really gone. Every fix in a report is therefore proven on that specific page in that specific run, not just generated and assumed correct.

What it does today (v1):

- Scans any public `http(s)` page against WCAG 2 A/AA rules using axe-core.
- Fixes a focused set of common issues automatically (missing form labels, icon-only buttons and links, image alt text, color contrast, missing page language).
- Flags page-structure issues (headings, landmarks, page title, and similar) for manual review, with axe's own guidance.
- Returns a JSON report, and ships with a simple web UI to run audits and browse results.

What it does not do: it does not change your website. The output is a report of verified fix suggestions that you apply to your own source code.

---

## Technologies

| Area | Technology |
|---|---|
| Language | Python 3.12+ |
| API framework | FastAPI, Pydantic, Uvicorn |
| Browser automation | Playwright (headless Chromium) |
| Accessibility engine | axe-core via `axe-playwright-python` |
| LLM access | Any OpenAI-compatible `/chat/completions` API through `requests` (default provider: Groq, default model: `openai/gpt-oss-20b`) |
| Image handling | Pillow (detects blank images so they can be marked decorative) |
| Config | `python-dotenv` (`.env` file) |
| Web UI | A single static HTML/CSS/JS page served by FastAPI, no build step |
| Testing and linting | pytest, httpx, ruff |
| Containers | Docker and Docker Compose |

---

## How it works

```
[Browser UI / curl / Postman]
            |
            v
   [FastAPI: POST /scan]
            |
            |-- validate the URL (http/https only, max 2048 chars)
            |-- SSRF guard: resolve the host and reject non-public addresses
            |-- concurrency limit (busy server answers 429)
            |
            v
   [Playwright: headless Chromium]
            |-- every request the page makes is checked by the same SSRF guard
            |-- load the page (30 s limit)
            |-- run axe-core on the rendered DOM
            |-- sort violations worst-first, keep the 20 most severe
            |
            |-- for each violation, pick a strategy:
            |      deterministic : code computes the fix (color contrast, page language)
            |      llm           : the model proposes a value, then sanity-checks itself
            |      manual        : page-structure issue, reported with axe's guidance
            |
            |-- apply the fix in the live DOM
            |-- re-run axe-core, keep the fix only if the violation cleared
            |   and no new violation was introduced (otherwise revert it)
            v
      [JSON report]
```

### Fix strategies

| Strategy | Rules | Notes |
|---|---|---|
| Deterministic | `color-contrast`, `html-has-lang` | Contrast fixes compute the nearest color that meets the required ratio. Page language falls back to `DEFAULT_PAGE_LANG`. No LLM call. |
| LLM (attribute) | `label`, `select-name`, `button-name`, `link-name`, `image-alt` | The model returns a short value (for example an `aria-label` or `alt` text) using the element and its surrounding context. A second self-check call is made unless `SANITY_CHECK=0`. |
| LLM (element) | Other fixable rules | The model returns a replacement element. It is rejected if the element's tag or inner content changes. |
| Manual | `heading-order`, `page-has-heading-one`, `document-title`, `region`, landmark rules, `tabindex`, `frame-title`, and others | Reported with a link to axe's guidance. No automatic fix is attempted. |

Image alt text is generated from the surrounding page context. A solid single-color image is marked decorative (`alt=""`) directly by code. The vision code path exists but is switched off by default (`SUPPORTS_IMAGES = False` in `app/fixer/llm.py`).

### Fix statuses

| Status | Meaning |
|---|---|
| `verified` | Fix applied and the rescan confirmed the violation cleared |
| `not_verified` | Fix applied but did not resolve the issue (reverted) |
| `fix_generation_failed` | The model returned nothing usable |
| `not_applicable` | The fix could not be applied safely (for example it changed the element's identity or content); reverted |
| `manual_review` | Needs a human; the report carries axe's guidance |
| `error` | The element could not be processed automatically |

A fix can also carry `needs_review: true` when it embeds an assumption worth a human look (such as generated alt text or an assumed page language).

### Limits that keep scans predictable

- At most 20 violating elements are processed per scan, most severe first. The full count is still reported as `total_violations_found`.
- A scan stops processing new elements after `SCAN_TIME_BUDGET_S` seconds (default 240).
- At most `MAX_CONCURRENT_SCANS` scans run at once (default 2); extra requests get HTTP 429.
- LLM calls retry up to 3 times on rate-limit and transient server errors, honoring `Retry-After`.

---

## Project layout

```
app/
  main.py               FastAPI app: GET /, GET /health, POST /scan
  security.py           SSRF guard (public-address check)
  static/index.html     Web UI (single file, served at /)
  fixer/
    fixer.py            The pipeline: load, scan, fix, verify, report
    rules.py            Registry: which axe rule uses which strategy, plus hints
    generator.py        Builds LLM prompts, calls the model, parses and validates results
    llm.py              Thin client for any OpenAI-compatible chat API
    wcag.py             Maps axe tags (wcag143) to dotted criteria (1.4.3)
tests/                  pytest suite (API, security, rules, LLM parsing, helpers)
test_pages/             Local HTML fixtures used during development
results/                Example reports from real sites
run_batch_scan.py       Helper that scans a list of sites and saves JSON reports
Dockerfile
docker-compose.yml
requirements.txt        Runtime dependencies
requirements-dev.txt    Adds pytest, httpx, ruff
.env.example            Configuration template
```

---

## Run locally

### Prerequisites

- Python 3.12 or newer
- An API key for an OpenAI-compatible LLM provider. The default is Groq; a free key is available at https://console.groq.com/keys

### 1. Create a virtual environment

macOS / Linux:
```bash
python3 -m venv venv
source venv/bin/activate
```

Windows (PowerShell):
```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
```

If PowerShell blocks the activation script, run this once and try again:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

On a fresh Linux machine, use `playwright install --with-deps chromium` so the system libraries Chromium needs are installed too.

### 3. Configure

macOS / Linux:
```bash
cp .env.example .env
```

Windows (PowerShell):
```powershell
copy .env.example .env
```

Open `.env` and set `LLM_API_KEY` to your real key. The other values are optional (see [Configuration](#configuration)).

### 4. Start the server

```bash
python -m uvicorn app.main:app --reload
```

Using `python -m uvicorn` works even when the plain `uvicorn` command is not on your PATH.

The server listens on http://127.0.0.1:8000.

- Web UI: http://127.0.0.1:8000
- Interactive API docs: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

### 5. Run an audit

Open the web UI, enter a URL, and click **Run audit**. Real sites can take 1 to 3 minutes; keep the tab open. Or call the API directly:

```bash
curl -X POST http://127.0.0.1:8000/scan \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

To scan several sites and save the reports under `results/`, start the server and run:

```bash
python run_batch_scan.py
```

Edit the `SITES` list at the top of that file to choose the sites.

---

## Run with Docker

### Prerequisites

Docker with Docker Compose (Docker Desktop on Windows and macOS).

### Start

```bash
cp .env.example .env        # Windows: copy .env.example .env
# edit .env and set LLM_API_KEY
docker compose up --build
```

Then open http://localhost:8000. Stop with `docker compose down`.

### What the container setup does

- The image is based on `python:3.12-slim`, installs the requirements and Chromium with its system libraries, copies only the `app/` folder, and runs as a non-root user.
- Chromium runs with `--no-sandbox` inside the container (`CHROMIUM_NO_SANDBOX=1`), because Chromium cannot start its own sandbox as a non-root user in Docker. The container itself is the isolation layer.
- A health check calls `/health` every 15 seconds.
- `docker-compose.yml` limits the container to 2 GB of memory and 2 CPUs, gives Chromium a 1 GB `/dev/shm`, restarts it unless stopped, and publishes the port on `127.0.0.1:8000` only, so it is not reachable from other machines by default.
- `.env` is passed in at runtime through `env_file` and is excluded from the image by `.dockerignore`.
- The image is large (around 1.5 GB) because it bundles Chromium.

If you change the published port or want to reach the app from other machines, edit the `ports` entry in `docker-compose.yml`, and read the [Security](#security) section first.

---

## Configuration

Set these in `.env` (local) or through `env_file` (Docker). Only `LLM_API_KEY` is required.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_API_KEY` | none (required) | API key for your LLM provider |
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | Any OpenAI-compatible host (OpenRouter, Cerebras, Fireworks, and others) |
| `LLM_MODEL` | `openai/gpt-oss-20b` | Model id for your provider |
| `LLM_TIMEOUT` | `60` | Seconds before an LLM call is considered failed |
| `SANITY_CHECK` | `1` | Set to `0` to skip the second (self-check) LLM call per violation |
| `DEFAULT_PAGE_LANG` | `en` | Fallback language for `html-has-lang` fixes |
| `MAX_CONCURRENT_SCANS` | `2` | Scans allowed to run at once before the API answers 429 |
| `SCAN_TIME_BUDGET_S` | `240` | Seconds after which a scan stops processing further elements |
| `CHROMIUM_NO_SANDBOX` | unset | Set to `1` to launch Chromium with `--no-sandbox`. The Dockerfile sets this; leave it unset on a normal local machine. |

Never commit your `.env` file. It is already listed in `.gitignore` and `.dockerignore`.

---

## Using the web UI

- Enter a URL (the page adds `https://` if you leave it off) and click **Run audit**.
- While the scan runs you see a timer and an estimated step list. The server does not stream progress, so the steps are an estimate.
- Results show summary counts, then one card per violation with the rule, WCAG criterion, severity, fix status, the original HTML, the suggested fix, and the explanation.
- Filter by fix status or severity, copy any code block, and download the whole report as JSON.
- Light and dark themes are supported.

---

## API reference

Interactive documentation is available at `/docs` while the server is running.

### `GET /health`

```json
{"status": "ok"}
```

### `POST /scan`

Runs the full pipeline synchronously and returns the complete report. The request stays open until the scan finishes.

Request:
```json
{"url": "https://example.com"}
```

Response `200`:
```json
{
  "url": "https://example.com",
  "wcag_level": "AA",
  "total_violations_found": 34,
  "violations_processed": 20,
  "violations": [
    {
      "issue_type": "image-alt",
      "wcag_criterion": "1.1.1",
      "selector": "img.hero-banner",
      "severity": "critical",
      "original_html": "<img src=\"hero.jpg\">",
      "fix": {
        "fix_code": "alt=\"Team collaborating in office\"",
        "explanation": "Added descriptive alt text summarizing the image content.",
        "llm_validated": true,
        "rescan_validated": true,
        "status": "verified",
        "needs_review": true
      }
    }
  ]
}
```

`llm_validated: false` on a deterministic fix is not a failure. Deterministic fixes never call the LLM, so read it as "not applicable".

Error responses (body is `{"error": "..."}`):

| Status | Meaning |
|---|---|
| `422` | Malformed or non-http(s) URL, or the URL (or something the page loads or redirects to) points at a non-public address |
| `429` | The server is already running its maximum number of scans; retry after the `Retry-After` header (30 seconds) |
| `502` | The LLM provider is unreachable, misconfigured, or rejected the API key, or the scan failed unexpectedly |
| `504` | The target page failed to load or did not finish loading within 30 seconds |

---

## Security

AccessPilot fetches user-supplied URLs with a real browser, which is a classic server-side request forgery (SSRF) risk, so guards are built in:

- **Address check before scanning.** The URL's hostname is resolved and rejected unless every resolved address is a public, globally routable one. Loopback, private, link-local (including the `169.254.169.254` cloud metadata address), reserved, and multicast ranges are blocked, and IPv4-mapped IPv6 addresses are unwrapped before checking.
- **Every browser request is checked.** During the scan, each request the page makes (redirects, scripts, images, frames) goes through the same check. A page that redirects to, or requires, an internal address is aborted and reported as `422`.
- **Generic error messages.** LLM and unexpected failures return short generic messages; details go to the server log only.
- **Bounded work.** URL length, page load time, total scan time, and concurrent scans are all capped.
- **Container hardening.** The Docker image runs as a non-root user, is resource-limited, and publishes only on `127.0.0.1`.

Important: there is currently no authentication and no per-user rate limiting. This is fine for local and demo use. Do not expose the service to the public internet as-is; put it behind authentication and a rate-limiting proxy first. Because Chromium runs with `--no-sandbox` in Docker, rendering untrusted pages there relies on the container boundary.

---

## Testing and linting

```bash
pip install -r requirements-dev.txt
pytest
ruff check .
```

The suite has around 37 tests covering the API endpoints, the SSRF guard, the rule registry and WCAG mapping, LLM response parsing and validation, and fixer helpers. None of them need a real LLM key or a network connection to a target site.

---

## Known limitations

- **No HTTP status or bot-block detection.** If a site returns a 404, a WAF stub, or another non-content page, the pipeline audits that page as if it were real, with no warning that the true content was never seen.
- **Contrast fixes ignore visual hierarchy.** A fix meets the WCAG ratio mechanically, but if a design intentionally uses the same shade at different weights or opacities, the fix can flatten that distinction. It passes the check but may need a design review.
- **Alt text comes from context, not from looking at the image.** The vision path is disabled by default, so generated alt text is marked `needs_review`.
- **Only the 20 most severe violating elements are fixed per scan.** The rest are counted but not processed.
- **Only single-frame, non-shadow-DOM targets get automatic fixes.** Elements inside iframes or shadow roots are not processed automatically.
- **One synchronous request per scan.** There is no job queue or progress streaming, so long scans hold the HTTP request open.
- **The web UI does not yet show a friendly label for the `error` status or a dedicated message for HTTP 429.** It falls back to the raw values.
- **Fixes are suggestions.** Nothing is written back to your site or repository.

---

## Roadmap (v2 and beyond)

Deliberately out of scope for v1:

**Product**
- User accounts and authentication
- Persistence (PostgreSQL): scan history, saved reports, and endpoints such as `GET /scans` and `PATCH /fixes/{id}/action` to accept or reject fixes
- Scan diffing to track accessibility progress over time and compare before/after
- Export formats: PDF, CSV, and SARIF for CI tools

**Scanning and fixing**
- HTTP status and bot-block detection, so error and challenge pages are reported instead of audited
- Multi-page and whole-site crawling, with sitemap support
- Authenticated page scanning (login flows, cookies)
- Vision-based alt text using a multimodal model
- Fixes that respect design intent (contrast fixes that preserve visual hierarchy)
- Broader rule coverage, including keyboard and focus-order checks, and iframe and shadow-DOM support
- Pull-request generation: map verified fixes back to source files and open a PR against the site's repository
- Raise or make configurable the 20-element cap

**Platform and operations**
- Background job queue with progress streaming (server-sent events or WebSockets) instead of one long request
- Per-user rate limiting and quotas
- A provider-agnostic LLM layer with easy swapping between providers and a paid tier
- Deployment to a hosting platform (Render or similar), CI pipeline, and structured logging and metrics
- A CI mode (CLI or GitHub Action) that fails a build when accessibility regresses

**UI**
- Friendly labels for the `error` status and HTTP 429, and a retry option
- Saved-report viewing, sharing, and side-by-side before/after previews
- Internationalization
