#!/usr/bin/env python3
import asyncio
import aiohttp
import os
import re
import json
from datetime import datetime
from typing import List, Dict, Any


# ---------- HELPERS ----------
def normalize_key(name: str) -> str:
    """Turn match name into a comparable lowercase key (remove non-alphanum)."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    """Fetch raw text from a URL, return empty string on failure."""
    try:
        async with session.get(url, timeout=15) as r:
            if r.status == 200:
                return await r.text()
    except Exception:
        return ""
    return ""


async def filter_alive_urls(session: aiohttp.ClientSession, urls: List[str]) -> List[str]:
    """Check which URLs respond with HTTP 200."""
    alive: List[str] = []
    for u in urls:
        try:
            async with session.get(u, timeout=10) as r:
                if r.status == 200:
                    alive.append(u)
        except Exception:
            continue
    return alive


# ---------- PARSING ----------
def parse_plain_streaming(text: str) -> Dict[str, List[Dict[str, str]]]:
    """
    Parse streaming.txt into buckets.
    Keeps both the original `name:` and `url:` lines so duplicates stay separate.
    Example result:
      {"benficavsqaraba": [
          {"name": "Benfica Vs Qaraba", "url": "https://..."},
          {"name": "Benfica Vs Qaraba 2", "url": "https://..."}
      ]}
    """
    buckets: Dict[str, List[Dict[str, str]]] = {}
    current_key = ""
    current_name = ""

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            current_name = line.replace("name:", "").strip()
            current_key = normalize_key(current_name)
        elif line.startswith("url:") and current_key and current_name:
            urls = re.findall(r"https://\S+\.m3u8", line)
            for u in urls:
                buckets.setdefault(current_key, []).append({
                    "name": current_name,
                    "url": u
                })
    return buckets


def parse_matches(text: str) -> List[Dict[str, Any]]:
    """
    Parse matches.txt (very simple "home vs away" per line).
    """
    matches: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if " vs " in line.lower():
            parts = re.split(r"\s+vs\s+", line, flags=re.I)
            if len(parts) == 2:
                matches.append({
                    "home": parts[0].strip(),
                    "away": parts[1].strip(),
                    "streams": []
                })
    return matches


# ---------- MERGING ----------
async def merge_plain_m3u8(
    matches: List[Dict[str, Any]],
    plain_buckets: Dict[str, List[Dict[str, str]]]
) -> None:
    """
    Append **all alive** .m3u8 URLs from plain_buckets.
    Preserves the original "name:" labels from streaming.txt.
    """
    async with aiohttp.ClientSession() as session:
        for m in matches:
            key = normalize_key(f"{m['home']} Vs {m['away']}")
            entries = plain_buckets.get(key, [])
            if not entries:
                continue

            urls = [e["url"] for e in entries]
            alive = await filter_alive_urls(session, urls)
            existing_urls = {s["url"] for s in m.get("streams", [])}

            for e in entries:
                if e["url"] in alive and e["url"] not in existing_urls:
                    m.setdefault("streams", []).append({
                        "name": e["name"],  # use original label (keeps "2")
                        "url": e["url"]
                    })


# ---------- MAIN ----------
async def main():
    print("🚀 Starting M3U8 generator...")
    print("=" * 50)

    # --- config URLs ---
    matches_url = "https://raw.githubusercontent.com/lyfe05/lyfe05/refs/heads/main/matches.txt"
    plain_url = "https://raw.githubusercontent.com/lyfe05/Temp/refs/heads/main/streaming.txt"

    async with aiohttp.ClientSession() as session:
        matches_text, plain_text = await asyncio.gather(
            fetch_text(session, matches_url),
            fetch_text(session, plain_url)
        )

    if not matches_text:
        print("❌ Failed to fetch matches.txt")
        return
    if not plain_text:
        print("❌ Failed to fetch streaming.txt")
        return

    matches = parse_matches(matches_text)
    plain_buckets = parse_plain_streaming(plain_text)

    print(f"✅ Found {len(matches)} matches")
    print(f"✅ Parsed {sum(len(v) for v in plain_buckets.values())} stream entries")

    # Merge plain m3u8 into matches
    await merge_plain_m3u8(matches, plain_buckets)

    # Save result JSON
    os.makedirs("streams", exist_ok=True)
    outfile = os.path.join("streams", "merged.json")
    with open(outfile, "w", encoding="utf-8") as f:
        json.dump(matches, f, indent=2, ensure_ascii=False)

    print(f"✅ Saved merged JSON -> {outfile}")


if __name__ == "__main__":
    asyncio.run(main())
