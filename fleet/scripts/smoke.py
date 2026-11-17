#!/usr/bin/env python3
"""Anonymous production-contract checks; never fetch private fleet data. The unsigned card is the anonymous frame, or
the full card where `fleet/wrangler.toml` sets `FLEET_CARD_PUBLIC = "1"` (nothing on it is private, Ryan 11-16)."""
from argparse import ArgumentParser
from pathlib import Path
import re
from json import loads
from urllib.error import HTTPError
from urllib.request import Request, urlopen


# The tier an unsigned `/og/fleet.png` answers with, by this deployment's config.
PUBLIC = re.search(r'^FLEET_CARD_PUBLIC\s*=\s*"1"', (Path(__file__).parent.parent / 'wrangler.toml').read_text(), re.M)
CARD_TIER = 'full (public)' if PUBLIC else 'anon'


def check(origin: str) -> None:
    for path, method, expected in [('/', 'GET', 200), ('/api/fleet', 'GET', 401), ('/api/fleet/slack', 'POST', 401), ('/og/fleet.png', 'GET', 200)]:
        # Cloudflare's bot rules refuse Python's default `Python-urllib/…` agent with a 403.
        request = Request(origin.rstrip('/') + path, method=method, headers={'User-Agent': 'disky-fleet-smoke/1'})
        try:
            response = urlopen(request, timeout=60)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            if response.status != expected:
                raise RuntimeError(f'{method} {path}: expected {expected}, got {response.status}')
            if expected == 401 and loads(body) != {'error': 'unauthenticated'}:
                raise RuntimeError(f'{method} {path}: unexpected anonymous response')
            if path == '/og/fleet.png':
                if response.headers.get('x-og-tier') != CARD_TIER or body[:8] != b'\x89PNG\r\n\x1a\n':
                    raise RuntimeError(f"unsigned card: want a PNG at tier {CARD_TIER!r}, got {response.headers.get('x-og-tier')!r}")
            print(f'{method} {path}: {expected}')


if __name__ == '__main__':
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('origin')
    check(parser.parse_args().origin)
