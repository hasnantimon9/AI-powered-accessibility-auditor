import json
import re
import time
from pathlib import Path

import requests

API_URL = "http://127.0.0.1:8000/scan"
RESULTS_DIR = Path("results")

SITES = [
    "https://demoqa.com/",
    "https://www.wikipedia.org/",
    "https://news.ycombinator.com/",
]


def safe_filename(url: str) -> str:
    name = re.sub(r"^https?://", "", url).rstrip("/")
    name = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    return name or "root"


def main() -> None:
    RESULTS_DIR.mkdir(exist_ok=True)

    print(f"Scanning {len(SITES)} site(s). Each may take 1-3 minutes.\n")

    for i, url in enumerate(SITES, start=1):
        print(f"[{i}/{len(SITES)}] {url} ... ", end="", flush=True)
        start = time.time()

        try:
            response = requests.post(API_URL, json={"url": url}, timeout=300)
        except requests.exceptions.RequestException as e:
            print(f"FAILED (couldn't reach API: {e})")
            continue

        elapsed = time.time() - start
        out_path = RESULTS_DIR / f"{safe_filename(url)}.json"

        try:
            body = response.json()
        except ValueError:
            body = {"raw_text": response.text}

        out_path.write_text(json.dumps(body, indent=2))

        if response.status_code == 200:
            total = body.get("total_violations_found", "?")
            processed = body.get("violations_processed", "?")
            statuses = {}
            for v in body.get("violations", []):
                s = v.get("fix", {}).get("status", "unknown")
                statuses[s] = statuses.get(s, 0) + 1
            status_summary = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
            print(
                f"200 OK in {elapsed:.0f}s -- found={total} processed={processed} "
                f"({status_summary}) -> {out_path}"
            )
        else:
            print(
                f"{response.status_code} in {elapsed:.0f}s -- "
                f"{body.get('error', body)} -> {out_path}"
            )

    print(f"\nDone. Full responses saved in {RESULTS_DIR.resolve()}/")


if __name__ == "__main__":
    main()
