#!/usr/bin/env python3
"""
Async sport-stream scraper – final unified version.
- decodes the two encoded JSON sources
- ingests the new plain-text “streaming.txt” (with <url …> tags)
- fetches every *.m3u8 to verify it is really alive
- merges everything into the same JSON schema
- appends plain .m3u8 links (normalized names for better matching)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from rapidfuzz import process, fuzz

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
# Normalize keys for better matching                                          #
# --------------------------------------------------------------------------- #
def normalize_key(name: str) -> str:
    """Guinea-Bissau Vs Djibouti → guineabissauvsdjibouti"""
    return re.sub(r"[^a-z0-9]", "", name.lower())


# --------------------------------------------------------------------------- #
# Parse the plain-text “streaming.txt” (with <url …> tags)                    #
# --------------------------------------------------------------------------- #
def parse_plain_streaming(text: str) -> Dict[str, List[str]]:
    """
    name: Benfica Vs Qaraba
    url: <url …>https://….m3u8</url>
    -> {"benficavsqaraba": ["https://…/benfica_vs_qaraba_.m3u8",
                            "https://…/benfica_vs_qaraba__2_1.m3u8"]}
    """
    buckets: Dict[str, List[str]] = {}
    current_key = None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.lower().startswith("name:"):
            raw = line[5:].strip()
            current_key = normalize_key(raw)
            buckets.setdefault(current_key, [])  # ensure list exists

        elif line.lower().startswith("url:") and current_key:
            for url in re.findall(r"https?://\S+\.m3u8", line):
                buckets[current_key].append(url)  # keep every URL

    return buckets


# --------------------------------------------------------------------------- #
# Lightweight HEAD check for m3u8                                             #
# --------------------------------------------------------------------------- #
async def url_is_alive(session: aiohttp.ClientSession, url: str) -> bool:
    try:
        async with session.head(
            url, ssl=False, timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
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
# Channel list builder  (mixed JSON formats)                                  #
# --------------------------------------------------------------------------- #
async def build_channel_map(session: aiohttp.ClientSession) -> Dict[str, str]:
    """
    Merge both JSON sources -> {name: url}.
    - sports.json: new format with stream_urls array
    - channels1.json: old format with hlsUrl string
    Keep only **first alive** URL per name.
    """
    merged: Dict[str, List[str]] = {}

    # Process sports.json (NEW format)
    sports_url = "https://streamweb-bay.vercel.app/sports.json"
    log.info("Downloading JSON (new format) %s", sports_url)
    raw = await fetch_text(session, sports_url)
    decoded = decode_payload(raw)
    channels: List[Dict[str, Any]] = json.loads(decoded)
    
    for ch in channels:
        name = ch.get("name", "").strip().lower()
        stream_urls = ch.get("stream_urls", [])
        
        if name and stream_urls:
            # Take only the first m3u8 URL from stream_urls
            first_url = None
            for stream in stream_urls:
                url = stream.get("url", "").strip()
                if url and ".m3u8" in url:
                    first_url = url
                    break
            
            if first_url:
                merged.setdefault(name, []).append(first_url)

    # Process channels1.json (OLD format)
    channels_url = "https://streamweb-bay.vercel.app/channels1.json"
    log.info("Downloading JSON (old format) %s", channels_url)
    raw = await fetch_text(session, channels_url)
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
MATCH_REGEX = re.compile(r"🏟️\s*Match:\s*(.+?)\s+Vs\s+(.+?)\s*$", re.I)
TIME_REGEX = re.compile(r"🕒\s*Start:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", re.I)
TOURNAMENT_REGEX = re.compile(r"📍\s*Tournament:\s*(.+?)\s*$", re.I)
CHANNELS_REGEX = re.compile(r"📺\s*Channels:\s*(.+?)\s*$", re.I)
HOME_LOGO_REGEX = re.compile(r"🖼️\s*Home\s*Logo:\s*(\S+)", re.I)
AWAY_LOGO_REGEX = re.compile(r"🖼️\s*Away\s*Logo:\s*(\S+)", re.I)
SCORE_REGEX = re.compile(r"⚽\s*Score:\s*(\d+\s*\|\s*\d+)", re.I)


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
            cur = {"home": m.group(1), "away": m.group(2)}
            continue

        m = TIME_REGEX.search(line)
        if m:
            cur["date"], cur["time"] = m.groups()
            continue

        m = TOURNAMENT_REGEX.match(line)
        if m:
            cur["tournament"] = m.group(1)
            continue

        m = CHANNELS_REGEX.search(line)
        if m:
            cur["channels"] = [c.strip() for c in m.group(1).split(",") if c.strip()]
            continue

        m = HOME_LOGO_REGEX.search(line)
        if m:
            cur["home_logo"] = m.group(1)
            continue

        m = AWAY_LOGO_REGEX.search(line)
        if m:
            cur["away_logo"] = m.group(1)
            continue

        m = SCORE_REGEX.search(line)
        if m:
            cur["score"] = m.group(1).replace(" ", "")
            continue

    if cur:
        matches.append(cur)

    log.info("Parsed %d matches", len(matches))
    return matches


# --------------------------------------------------------------------------- #
# Channel matcher  (legacy channels)                                         #
# --------------------------------------------------------------------------- #
def attach_stream_urls(matches: List[Dict[str, Any]], channel_map: Dict[str, str]) -> None:
    """In-place attach stream objects to every match."""
    channels_lower = {k.lower(): v for k, v in channel_map.items()}
    for m in matches:
        streams: List[Dict[str, str]] = []
        for ch in m.get("channels", []):
            ch_low = ch.lower()
            url = channels_lower.get(ch_low)
            if not url:  # fuzzy fallback
                match, score, _ = process.extractOne(
                    ch_low, channels_lower.keys(), scorer=fuzz.ratio
                )
                if score > 85:
                    url = channels_lower[match]
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
# Append plain .m3u8 links with normalization                               #
# --------------------------------------------------------------------------- #
async def merge_plain_m3u8(
    session: aiohttp.ClientSession,
    matches: List[Dict[str, Any]],
    plain_buckets: Dict[str, List[str]],
) -> None:
    """
    If a match home-vs-away string (lower-cased + normalized) exists in plain_buckets,
    append **all alive** .m3u8 URLs to the already existing streams array.
    """
    for m in matches:
        key = normalize_key(f"{m['home']} Vs {m['away']}")
        urls = plain_buckets.get(key, [])
        if not urls:
            continue
        alive = await filter_alive_urls(session, urls)
        existing_urls = {s["url"] for s in m.get("streams", [])}
        for idx, u in enumerate(alive, 1):
            if u not in existing_urls:
                m.setdefault("streams", []).append(
                    {"name": f"{key}-{idx}", "url": u}
                )


# --------------------------------------------------------------------------- #
# Async pipeline                                                              #
# --------------------------------------------------------------------------- #
async def main() -> List[Dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=15)
    connector = aiohttp.TCPConnector(limit=100, limit_per_host=20)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        channel_map = await build_channel_map(session)
        raw_matches = await fetch_text(
            session,
            "https://raw.githubusercontent.com/lyfe05/lyfe05/refs/heads/main/matches.txt",
        )
        matches = parse_matches(raw_matches)
        attach_stream_urls(matches, channel_map)

        plain_text = await fetch_text(
            session,
            "https://raw.githubusercontent.com/lyfe05/Temp/refs/heads/main/streaming.txt",
        )
        plain_buckets = parse_plain_streaming(plain_text)
        await merge_plain_m3u8(session, matches, plain_buckets)

        final = build_final_json(matches)
        log.info("Scrape finished – %d matches, %d total streams",
                 len(final), sum(len(m["streams"]) for m in final))
        return final


# --------------------------------------------------------------------------- #
# CLI entry-point                                                             #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    try:
        final = asyncio.run(main())
        print(json.dumps(final, indent=2, ensure_ascii=False))
    except KeyboardInterrupt:
        log.warning("Aborted by user")
