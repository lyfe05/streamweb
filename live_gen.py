#!/usr/bin/env python3
"""
Async sport-stream scraper – updated:
- Parse numeric match IDs from matches.txt (🆔 Match ID: 2632484)
- Top-level generatedDate YYYY-MM-DD
- Use matchId from input (numeric). If missing, matchId will be null.
- Parse streaming.txt and collapse trailing-number variants (Real Betis ... 2 -> same key)
- Append all .m3u8 links (no alive checks)
- Sort matches by kickoff datetime
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
from datetime import datetime

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
# Decoder (kept, not changed)                                                 #
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
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        return await resp.text()


# --------------------------------------------------------------------------- #
# Normalization / parsing helpers                                             #
# --------------------------------------------------------------------------- #
def normalize_key(name: str) -> str:
    """
    Normalize a match name for matching:
    "Guinea-Bissau Vs Djibouti" -> "guineabissauvsdjibouti"
    Also remove trailing digits later when parsing streaming.txt.
    """
    s = name.lower()
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


def parse_plain_streaming(text: str) -> Dict[str, List[str]]:
    """
    Parse streaming.txt style text where entries are:
      name: <match name>
      url: <...m3u8>

    Collapses trailing digits on the normalized key, so "Real Betis ... 2"
    becomes the same bucket as "Real Betis ...".
    """
    buckets: Dict[str, List[str]] = {}
    current_key = ""

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if line.lower().startswith("name:"):
            raw = line[5:].strip()
            norm = normalize_key(raw)
            # remove trailing digits (e.g., "...2" -> "")
            norm = re.sub(r"\d+$", "", norm)
            current_key = norm

        elif line.lower().startswith("url:") and current_key:
            # match all m3u8 urls in the line
            urls = re.findall(r"https?://\S+?\.m3u8\b", line)
            if urls:
                buckets.setdefault(current_key, []).extend(urls)

    # remove duplicates but preserve order
    for k, lst in list(buckets.items()):
        seen = set()
        new = []
        for u in lst:
            if u not in seen:
                seen.add(u)
                new.append(u)
        buckets[k] = new

    return buckets


# --------------------------------------------------------------------------- #
# Lightweight channel-map builder (old encoded JSONs)                         #
# --------------------------------------------------------------------------- #
async def build_channel_map(session: aiohttp.ClientSession) -> Dict[str, str]:
    """
    Merge only the *old encoded JSONs* -> {name: url}.
    Keep only the **first** URL per name (we won't probe liveness here).
    """
    merged: Dict[str, List[str]] = {}

    old_urls = [
        "https://streamweb-bay.vercel.app/sports.json",
        "https://streamweb-bay.vercel.app/channels1.json",
    ]
    for u in old_urls:
        log.info("Downloading JSON (encoded) %s", u)
        raw = await fetch_text(session, u)
        decoded = decode_payload(raw)
        try:
            channels = json.loads(decoded)
        except Exception as e:
            log.warning("Failed to parse decoded JSON from %s: %s", u, e)
            continue

        for ch in channels:
            name = ch.get("name", "").strip().lower()
            url = ch.get("hlsUrl", "").strip()
            if name and url:
                merged.setdefault(name, []).append(url)

    # collapse to first URL per name
    cleaned: Dict[str, str] = {}
    for name, urls in merged.items():
        if urls:
            cleaned[name] = urls[0]

    log.info("Total unique channels after merge: %d", len(cleaned))
    return cleaned


# --------------------------------------------------------------------------- #
# Parsing matches text (matches.txt)                                          #
# --------------------------------------------------------------------------- #
# Regexes to parse your textual matches format
MATCH_REGEX = re.compile(r"🏟️ Match:\s*(?P<home>.+?)\s*Vs\s*(?P<away>.+?)\s*$")
MATCH_ID_REGEX = re.compile(r"🆔 Match ID:\s*(?P<id>\d+)")
TIME_REGEX = re.compile(r"🕒 Start:\s*(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2})")
TOURNAMENT_REGEX = re.compile(r"📍 Tournament:\s*(?P<t>.+?)\s*$")
CHANNELS_REGEX = re.compile(r"📺 Channels:\s*(?P<c>.+?)\s*$")
HOME_LOGO_REGEX = re.compile(r"🖼️ Home Logo:\s*(?P<url>\S+)")
AWAY_LOGO_REGEX = re.compile(r"🖼️ Away Logo:\s*(?P<url>\S+)")
SCORE_REGEX = re.compile(r"⚽ Score:\s*(?P<s>\d+\s*\|\s*\d+)")

def parse_matches(text: str) -> List[Dict[str, Any]]:
    """Parse the big text blob into list of match dicts and capture numeric match_id."""
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

        m = MATCH_ID_REGEX.match(line)
        if m and cur:
            try:
                cur["match_id"] = int(m["id"])
            except Exception:
                cur["match_id"] = None
            continue

        m = TIME_REGEX.search(line)
        if m and cur:
            cur["date"], cur["time"] = m["date"], m["time"]
            continue

        m = TOURNAMENT_REGEX.match(line)
        if m and cur:
            cur["tournament"] = m["t"]
            continue

        m = CHANNELS_REGEX.search(line)
        if m and cur:
            cur["channels"] = [c.strip() for c in m["c"].split(",") if c.strip()]
            continue

        m = HOME_LOGO_REGEX.match(line)
        if m and cur:
            cur["home_logo"] = m["url"]
            continue

        m = AWAY_LOGO_REGEX.match(line)
        if m and cur:
            cur["away_logo"] = m["url"]
            continue

        m = SCORE_REGEX.match(line)
        if m and cur:
            cur["score"] = m["s"].replace(" ", "")
            continue

    if cur:
        matches.append(cur)

    log.info("Parsed %d matches", len(matches))
    return matches


# --------------------------------------------------------------------------- #
# Channel matcher / attach legacy stream urls                                 #
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=2048)
def _fuzzy(url: str, name: str) -> Optional[str]:
    """Cached fuzzy helper (keeps old behavior)."""
    score = fuzz.ratio(name, url)
    return url if score > 85 else None


def attach_stream_urls(matches: List[Dict[str, Any]], channel_map: Dict[str, str]) -> None:
    """Attach legacy mapped streams to matches (in-place)."""
    for m in matches:
        streams: List[Dict[str, str]] = []
        for ch in m.get("channels", []):
            ch_low = ch.lower()
            url = channel_map.get(ch_low) or _fuzzy(channel_map.get(ch_low, ""), ch_low)
            if url:
                streams.append({"name": ch, "url": url})
        m["streams"] = streams


# --------------------------------------------------------------------------- #
# Build final JSON using match_id from parsed data                             #
# --------------------------------------------------------------------------- #
def build_final_json(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    for m in matches:
        # use numeric match_id if present; otherwise null (do NOT auto-generate)
        mid_val = m.get("match_id")
        if mid_val is None:
            match_id_field = None
        else:
            # ensure int
            try:
                match_id_field = int(mid_val)
            except Exception:
                match_id_field = None

        # attempt to compute team ids (normalized)
        left_id = m["home"].lower().replace(" ", "_")
        right_id = m["away"].lower().replace(" ", "_")

        out.append(
            {
                "matchId": match_id_field,
                "startDate": m.get("date", ""),
                "startTime": m.get("time", ""),
                "teams": {
                    "left": {
                        "id": left_id,
                        "name": m["home"],
                        "logoUrl": m.get("home_logo", ""),
                    },
                    "right": {
                        "id": right_id,
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
# Append plain .m3u8 buckets (NO liveness check — include ALL links)          #
# --------------------------------------------------------------------------- #
async def merge_plain_m3u8(matches: List[Dict[str, Any]], plain_buckets: Dict[str, List[str]]) -> None:
    """
    Append plain .m3u8 links found in streaming.txt.
    For naming: use normalized key + increasing index per appended URL.
    """
    for m in matches:
        key = normalize_key(f"{m['home']} Vs {m['away']}")
        # also remove trailing digits on the match-side key (consistency)
        key = re.sub(r"\d+$", "", key)
        urls = plain_buckets.get(key, [])
        if not urls:
            continue
        existing_urls = {s["url"] for s in m.get("streams", [])}
        for idx, u in enumerate(urls, 1):
            if u not in existing_urls:
                name = f"{key}-{idx}"
                m.setdefault("streams", []).append({"name": name, "url": u})


# --------------------------------------------------------------------------- #
# Helper to fetch plain buckets from raw GitHub file                          #
# --------------------------------------------------------------------------- #
async def get_plain_buckets() -> Dict[str, List[str]]:
    async with aiohttp.ClientSession() as session:
        text = await fetch_text(
            session,
            "https://raw.githubusercontent.com/lyfe05/Temp/refs/heads/main/streaming.txt",
        )
        return parse_plain_streaming(text)


# --------------------------------------------------------------------------- #
# Main pipeline                                                                #
# --------------------------------------------------------------------------- #
async def main() -> Dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        # 1) legacy channel map
        channel_map = await build_channel_map(session)

        # 2) load matches.txt (text dump that includes 🆔 Match ID)
        raw_matches = await fetch_text(
            session,
            "https://raw.githubusercontent.com/lyfe05/lyfe05/refs/heads/main/matches.txt",
        )
        matches = parse_matches(raw_matches)

        # 3) attach legacy streams
        attach_stream_urls(matches, channel_map)

        # 4) append plain .m3u8 buckets (no liveness checks)
        plain_buckets = await get_plain_buckets()
        await merge_plain_m3u8(matches, plain_buckets)

        # 5) sort matches by start datetime (fallback to string compare)
        def parse_dt(m: Dict[str, Any]) -> datetime:
            try:
                return datetime.strptime(f"{m.get('date','')} {m.get('time','')}", "%Y-%m-%d %H:%M")
            except Exception:
                # fallback far future so missing dates end up last
                return datetime.max

        matches.sort(key=parse_dt)

        # 6) build final json list using the numeric matchId captured
        final_matches = build_final_json(matches)

        # top-level generated date in YYYY-MM-DD (UTC date)
        generated_date = datetime.utcnow().strftime("%Y-%m-%d")

        return {"generatedDate": generated_date, "matches": final_matches}


# --------------------------------------------------------------------------- #
# CLI entry
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    try:
        final = asyncio.run(main())
        print(json.dumps(final, indent=2, ensure_ascii=False))
    except KeyboardInterrupt:
        log.warning("Aborted by user")
