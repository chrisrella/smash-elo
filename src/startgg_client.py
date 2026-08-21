"""
startgg_client.py
Shared start.gg GraphQL transport: auth, POST helper, retry/backoff on rate
limits and transient errors. Every collector module goes through this.
"""

import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("STARTGG_API_KEY")
ENDPOINT = "https://api.start.gg/gql/alpha"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

ULTIMATE_VIDEOGAME_ID = 1386


def require_api_key():
    if not API_KEY:
        raise SystemExit("STARTGG_API_KEY not set in .env")


def post(query, variables, retries=5):
    """POST a GraphQL query, retrying with backoff on rate limits / transient errors."""
    resp = None
    for attempt in range(retries):
        resp = requests.post(
            ENDPOINT, json={"query": query, "variables": variables}, headers=HEADERS
        )
        if resp.status_code == 200:
            body = resp.json()
            if "errors" in body:
                raise RuntimeError(f"GraphQL error: {body['errors']}")
            return body["data"]
        if resp.status_code in (429, 500, 502, 503, 520, 521, 522, 523, 524):
            wait = 2 ** attempt
            print(f"  ...got {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Failed after {retries} retries: {resp.status_code} {resp.text}")
