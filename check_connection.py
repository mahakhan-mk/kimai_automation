from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from kimai_client import KimaiClient, KimaiConfig, KimaiError


def main() -> int:
    load_dotenv()
    token = os.getenv("KIMAI_API_TOKEN", "").strip()
    base_url = os.getenv("KIMAI_BASE_URL", "").strip()
    if not token or token == "PASTE_YOUR_TOKEN_HERE" or not base_url:
        print("ERROR: fill KIMAI_BASE_URL and KIMAI_API_TOKEN in .env", file=sys.stderr)
        return 1
    verify = os.getenv("KIMAI_VERIFY_SSL", "true").lower() in {"1", "true", "yes", "on"}
    timeout = float(os.getenv("KIMAI_TIMEOUT_SECONDS", "30"))
    try:
        with KimaiClient(KimaiConfig(base_url, token, timeout, verify)) as client:
            print("API connection succeeded.")
            try:
                print("Version:", client.version())
            except KimaiError:
                print("Version endpoint was unavailable, but authentication may still be valid.")
            customers = client.customers()
            projects = client.projects()
            print(f"Readable customers: {len(customers)}")
            print(f"Readable projects: {len(projects)}")
        return 0
    except KimaiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
