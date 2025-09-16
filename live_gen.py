#!/usr/bin/env python3
"""
Async sport-stream scraper – final unified version.
- decodes the two encoded JSON sources
- ingests the new plain-text “streaming.txt” (with <url …> tags)
- fetches every *.m3u8 to verify it is really alive
- merges everything into the same JSON schema
- NEW: *appends* plain .m3u8 links (normalized names for better matching)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from rapidfuzz import fuzz

# --------------------------------------------------------------------------- #
# Logging                                                                     #
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sport-scraper")

# --------------------------------------------------------------------------- #
# Decoder – streaming, zero-copy                                              #
# --------------------------------------------------------------------------- #
_CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrst"
_CHAR2VAL = {c: i for i, c in enumerate(_CHARSET)}


def decode_payload(text: str) -> str:
    """Decode custom base-32(ish) into UTF-8."""
    text = re.sub(r"[=\n\r\s]", "", text.strip())
    if not text:
        return ""

    buffer, bits = 0, 0
    out = bytearray()

    for ch in text:
        val = _CHAR2VAL.get(ch)
        if val is None:  # skip unknown chars
            continue

        buffer = (buffer << 5) | val
        bits += 5

        while bits >= 8:
            bits -= 8
            out.append((buffer >> bits) & 0xFF)
            buffer &= (1 << bits) - 1

    return out.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Async fetch helpers                                                         #
# --------------------------------------------------------------------------- #
async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        return await resp.text()


# --------------------------------------------------------------------------- #
# NEW: normalize keys for better matching                                     #
# --------------------------------------------------------------------------- #
def normalize_key(name: str) -> str:
    """Guinea-Bissau Vs Djibouti → guineabissauvsdjibouti"""
    # Remove all non-alphanumeric characters and convert to lowercase
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    # Handle common special characters and variations
    normalized = normalized.replace("ğ", "g").replace("ı", "i").replace("ş", "s").replace("ç", "c").replace("ö", "o").replace("ü", "u")
    return normalized


# --------------------------------------------------------------------------- #
# NEW: parse the plain-text “streaming.txt” (with <url …> tags)               #
# --------------------------------------------------------------------------- #
def parse_plain_streaming(text: str) -> Dict[str, List[str]]:
    """
    Parse streaming.txt and preserve all entries, including numbered ones.
    """
    buckets: Dict[str, List[str]] = {}
    current_key = ""

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            raw = line.replace("name:", "").strip()
            current_key = raw  # Keep the original name with numbers
        elif line.startswith("url:") and current_key:
            urls = re.findall(r"https://\S+\.m3u8", line)
            for u in urls:
                if current_key not in buckets:
                    buckets[current_key] = []
                buckets[current_key].append(u)
    return buckets


# --------------------------------------------------------------------------- #
# NEW: lightweight HEAD check for m3u8                                        #
# --------------------------------------------------------------------------- #
async def url_is_alive(session: aiohttp.ClientSession, url: str) -> bool:
    try:
        async with session.head(url, timeout=8) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


async def filter_alive_urls(
    session: aiohttp.ClientSession, urls: List[str]
) -> List[str]:
    """Return only reachable URLs (run in parallel)."""
    tasks = [asyncio.create_task(url_is_alive(session, u)) for u in urls]
    results = await asyncio.gather(*tasks)
    return [u for u, ok in zip(urls, results) if ok]


# --------------------------------------------------------------------------- #
# Channel list builder  (old encoded JSONs only)                             #
# --------------------------------------------------------------------------- #
async def build_channel_map(session: aiohttp.ClientSession) -> Dict[str, str]:
    """
    Merge only the *old encoded JSONs* -> {name: url}.
    Keep only **first alive** URL per name.
    """
    merged: Dict[str, List[str]] = {}

    # old encoded JSONs
    old_urls = [
        "https://streamweb-bay.vercel.app/sports.json",
        "https://streamweb-bay.vercel.app/channels1.json",
    ]
    for u in old_urls:
        log.info("Downloading JSON (encoded) %s", u)
        raw = await fetch_text(session, u)
        decoded = decode_payload(raw)
        channels: List[Dict[str, str]] = json.loads(decoded)
        for ch in channels:
            name = ch.get("name", "").strip().lower()
            url = ch.get("hlsUrl", "").strip()
            if name and url:
                merged.setdefault(name, []).append(url)

    # collapse to a single **alive** URL per name
    cleaned: Dict[str, str] = {}
    for name, urls in merged.items():
        alive = await filter_alive_urls(session, urls)
        if alive:
            cleaned[name] = alive[0]
    log.info("Total unique channels after merge: %d", len(cleaned))
    return cleaned


# --------------------------------------------------------------------------- #
# Match parser                                                                #
# --------------------------------------------------------------------------- #
MATCH_REGEX = re.compile(
    r"🏟️ Match:\s*(?P<home>.+?)\s*Vs\s*(?P<away>.+?)\s*$"
)
TIME_REGEX = re.compile(r"🕒 Start:\s*(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2})")
TOURNAMENT_REGEX = re.compile(r"📍 Tournament:\s*(?P<t>.+?)\s*$")
CHANNELS_REGEX = re.compile(r"📺 Channels:\s*(?P<c>.+?)\s*$")
HOME_LOGO_REGEX = re.compile(r"🖼️ Home Logo:\s*(?P<url>\S+)")
AWAY_LOGO_REGEX = re.compile(r"🖼️ Away Logo:\s*(?P<url>\S+)")
SCORE_REGEX = re.compile(r"⚽ Score:\s*(?P<s>\d+\s*\|\s*\d+)")


def parse_matches(text: str) -> List[Dict[str, Any]]:
    """Parse the big text blob into list of match dicts."""
    matches: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        m = MATCH_REGEX.match(line)
        if m:
            if cur:
                matches.append(cur)
            cur = {"home": m["home"], "away": m["away"]}
            continue

        m = TIME_REGEX.search(line)
        if m:
            cur["date"], cur["time"] = m["date"], m["time"]
            continue

        m = TOURNAMENT_REGEX.match(line)
        if m:
            cur["tournament"] = m["t"]
            continue

        m = CHANNELS_REGEX.search(line)
        if m:
            cur["channels"] = [c.strip() for c in m["c"].split(",") if c.strip()]
            continue

        m = HOME_LOGO_REGEX.search(line)
        if m:
            cur["home_logo"] = m["url"]
            continue

        m = AWAY_LOGO_REGEX.search(line)
        if m:
            cur["away_logo"] = m["url"]
            continue

        m = SCORE_REGEX.search(line)
        if m:
            cur["score"] = m["s"].replace(" ", "")
            continue

    if cur:
        matches.append(cur)

    log.info("Parsed %d matches", len(matches))
    return matches


# --------------------------------------------------------------------------- #
# Channel matcher  (legacy channels)                                         #
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=2048)
def _fuzzy(url: str, name: str) -> Optional[str]:
    """Cached fuzzy helper."""
    score = fuzz.ratio(name, url)
    return url if score > 85 else None


def attach_stream_urls(matches: List[Dict[str, Any]], channel_map: Dict[str, str]) -> None:
    """In-place attach stream objects to every match."""
    for m in matches:
        streams: List[Dict[str, str]] = []
        for ch in m.get("channels", []):
            ch_low = ch.lower()
            url = channel_map.get(ch_low) or _fuzzy(channel_map.get(ch_low, ""), ch_low)
            if url:
                streams.append({"name": ch, "url": url})
        m["streams"] = streams


# --------------------------------------------------------------------------- #
# JSON builder                                                                #
# --------------------------------------------------------------------------- #
def build_final_json(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in matches:
        mid = f"{m['home'].lower().replace(' ', '_')}_vs_{m['away'].lower().replace(' ', '_')}_{m['date']}"
        out.append(
            {
                "matchId": mid,
                "startDate": m["date"],
                "startTime": m["time"],
                "teams": {
                    "left": {
                        "id": m["home"].lower().replace(" ", "_"),
                        "name": m["home"],
                        "logoUrl": m.get("home_logo", ""),
                    },
                    "right": {
                        "id": m["away"].lower().replace(" ", "_"),
                        "name": m["away"],
                        "logoUrl": m.get("away_logo", ""),
                    },
                },
                "tournament": m.get("tournament", ""),
                "venue": "",
                "streams": m.get("streams", []),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# NEW: *append* plain .m3u8 links with normalization                         #
# --------------------------------------------------------------------------- #
async def merge_plain_m3u8(
    session: aiohttp.ClientSession,
    matches: List[Dict[str, Any]], 
    plain_buckets: Dict[str, List[str]]
) -> None:
    """
    Match streams by trying multiple key variations.
    """
    for m in matches:
        match_name = f"{m['home']} Vs {m['away']}"
        base_match_name = re.sub(r'\s+\d+$', '', match_name)  # Base name without numbers
        
        # Try multiple matching strategies
        possible_keys = [
            match_name,  # Exact match with "Vs"
            f"{m['home']} vs {m['away']}",  # Lowercase "vs"
            f"{m['home']} v {m['away']}",   # Short "v"
            base_match_name,  # Base name without numbers
        ]
        
        # Also try normalized versions
        possible_keys.extend([normalize_key(key) for key in possible_keys])
        
        # Remove duplicates
        possible_keys = list(set(possible_keys))
        
        for key in possible_keys:
            urls = plain_buckets.get(key, [])
            if urls:
                alive = await filter_alive_urls(session, urls)
                existing_urls = {s["url"] for s in m.get("streams", [])}
                for u in alive:
                    if u not in existing_urls:
                        # Use the original stream name from plain_buckets
                        stream_name = f"Direct Stream {len([s for s in m.get('streams', []) if s['name'].startswith('Direct Stream')]) + 1}"
                        m.setdefault("streams", []).append({"name": stream_name, "url": u})
                break  # Stop after first successful match


# --------------------------------------------------------------------------- #
# Tiny helper to collect the plain buckets once
# --------------------------------------------------------------------------- #
async def get_plain_buckets(session: aiohttp.ClientSession) -> Dict[str, List[str]]:
    text = await fetch_text(
        session,
        "https://raw.githubusercontent.com/lyfe05/Temp/refs/heads/main/streaming.txt",
    )
    return parse_plain_streaming(text)


# --------------------------------------------------------------------------- #
# Async pipeline                                                              #
# --------------------------------------------------------------------------- #
async def main() -> List[Dict[str, Any]]:
    async with aiohttp.ClientSession() as session:
        channel_map = await build_channel_map(session)          # old JSON sources
        raw_matches = await fetch_text(
            session,
            "https://raw.githubusercontent.com/lyfe05/lyfe05/refs/heads/main/matches.txt",
        )
        matches = parse_matches(raw_matches)
        attach_stream_urls(matches, channel_map)                # legacy channels

        # ---- NEW: append ALL alive plain .m3u8 when available ----
        plain_buckets = await get_plain_buckets(session)
        await merge_plain_m3u8(session, matches, plain_buckets)

        return build_final_json(matches)


# --------------------------------------------------------------------------- #
# CLI entry-point                                                             #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    try:
        final = asyncio.run(main())
        print(json.dumps(final, indent=2, ensure_ascii=False))
    except KeyboardInterrupt:
        log.warning("Aborted by user")
