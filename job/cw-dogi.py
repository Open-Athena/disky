#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["click", "google-auth", "google-crc32c", "requests"]
# ///
"""Verify CW card privacy and temporary, promptly revoked detailed previews."""

from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
from runpy import run_path
from struct import unpack
import tomllib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from click import Choice, ClickException, Path as ClickPath, argument, group, option
from requests import RequestException, Response, Session

ROOT = Path(__file__).resolve().parents[1]
HOSTS = {"preview": "https://dev.cw.oa.dev", "production": "https://cw.oa.dev"}


class SafeSession(Session):
    def request(self, method: str, url: str, **kwargs: object) -> Response:
        kwargs.setdefault("allow_redirects", False)
        try:
            return super().request(method, url, **kwargs)
        except RequestException:
            raise ClickException("CW verification transport failed; credential URLs are not logged") from None


class MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "meta":
            return
        values = dict(attrs)
        key = values.get("property") or values.get("name")
        if key and (key.startswith("og:") or key.startswith("twitter:")):
            if key in self.values:
                raise ClickException(f"Duplicate metadata field: {key}")
            self.values[key] = values.get("content") or ""


def metadata(html: str) -> dict[str, str]:
    parser = MetaParser()
    parser.feed(html)
    return parser.values


def png(response: Response, tier: str) -> bytes:
    body = response.content
    if response.status_code != 200 or response.headers.get("content-type") != "image/png":
        raise ClickException(f"Card response failed: HTTP {response.status_code}")
    if body[:8] != b"\x89PNG\r\n\x1a\n" or unpack(">II", body[16:24]) != (1200, 630):
        raise ClickException("Card is not a 1200×630 PNG")
    if response.headers.get("x-og-tier") != tier:
        raise ClickException("Unexpected card disclosure tier")
    if tier == "full" and response.headers.get("cache-control") != "private, no-store":
        raise ClickException("Detailed card is not private/no-store")
    if tier == "anon" and response.headers.get("x-og-policy") != "generic":
        raise ClickException("Anonymous card policy is not generic")
    return body


@group()
def cli() -> None:
    pass


@cli.command("schema")
def schema() -> None:
    """Read only the CW migration/table names, never token rows."""
    helper = run_path(str(ROOT / "job/cw-isolation-credentials.py"))
    outputs = helper["load_outputs"]()
    config = tomllib.loads((ROOT / "site/wrangler.toml").read_text())
    database = config["d1_databases"][0]["database_id"]
    import os
    account = (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("OA_CF_ACCT", "")).strip()
    with SafeSession() as http:
        response = http.post(f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{database}/query",
                             headers={"Authorization": "Bearer " + outputs["isolation_pages_token"]},
                             json={"sql": "SELECT name FROM d1_migrations WHERE name = '0010_og_tokens.sql'; SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'og_tokens';"}, timeout=60)
    if response.status_code != 200 or response.json().get("success") is not True:
        raise ClickException(f"D1 schema check failed: HTTP {response.status_code}")
    results = [item["results"] for item in response.json()["result"]]
    if results != [[{"name": "0010_og_tokens.sql"}], [{"name": "og_tokens"}]]:
        raise ClickException("CW OG migration/table is missing or unexpected")
    print(json.dumps({"migration": "0010_og_tokens.sql", "table": "og_tokens", "applied": True}))


def viewer_grant() -> str:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    helper = run_path(str(ROOT / "job/cw-isolation-credentials.py"))
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"], quota_project_id=helper["PROJECT"])
    with AuthorizedSession(credentials) as http:
        response = http.get(f"https://secretmanager.googleapis.com/v1/projects/{helper['PROJECT']}/secrets/cw-s3-job-grant/versions/latest:access", timeout=60)
    if response.status_code != 200:
        raise ClickException(f"Viewer grant access failed: HTTP {response.status_code}")
    return helper["access_payload"](response.json()).decode().strip()


@cli.command("verify")
@option("-d", "--detailed", is_flag=True, help="Mint one temporary preview, verify it and revoke it in finally; never print its URL/token.")
@option("-o", "--output", type=ClickPath(path_type=Path), required=True, help="New private evidence directory.")
@argument("environment", type=Choice(["preview", "production"]))
def verify(
    detailed: bool,
    output: Path,
    environment: str,
) -> None:
    config = tomllib.loads((ROOT / "site/wrangler.toml").read_text())
    variables = config["vars"] if environment == "production" else config["env"]["preview"]["vars"]
    label = variables["ROOT_LABEL"]
    version = "6"
    origin = HOSTS[environment]
    image_path = f"/og/map.png?policy=generic&r={version}"
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {"environment": environment, "anonymous_pages": [], "anonymous_images": [], "detailed": "not-requested", "ok": False}
    with SafeSession() as http:
        http.headers["User-Agent"] = "cw-dogi-verification/1.0"
        for i, path in enumerate(["/", "/__og_privacy_probe__/absent?d=19000101&f=private", "/users", "/assignments", "/staged", "/meta", "/__og_privacy_probe__?section=diff&d=19000101-18990101"]):
            response = http.get(origin + path, timeout=90)
            if response.status_code != 200:
                raise ClickException(f"Anonymous page {i} failed: HTTP {response.status_code}")
            actual = metadata(response.text)
            expected = {"og:title": label, "og:description": "Private storage dashboard. Sign in to explore storage.",
                        "og:image": origin + image_path, "og:image:type": "image/png",
                        "og:url": origin + path.split("?", 1)[0], "twitter:title": label,
                        "twitter:description": "Private storage dashboard. Sign in to explore storage.", "twitter:image": origin + image_path}
            if {key: actual.get(key) for key in expected} != expected:
                raise ClickException(f"Anonymous page {i} metadata differs from the generic contract")
            (output / f"anonymous-page-{i}.html").write_bytes(response.content)
            report["anonymous_pages"].append({"path": path, "result": "exact-generic-metadata"})
        generic = png(http.get(origin + image_path, timeout=90), "anon")
        (output / "generic.png").write_bytes(generic)
        for i, path in enumerate([image_path, "/og/map.png?path=__og_privacy_probe__&d=19000101&f=private&policy=shape&publicLabels=1",
                                  "/og/map.png?sig=bad", "/og/staged.png", "/og/users.png", "/og/user.png?id=private", "/og/assignments.png", "/og/diff.png?path=private&d=19000101-18990101&sig=bad"]):
            body = png(http.get(origin + path, timeout=90), "anon")
            if body != generic:
                raise ClickException(f"Anonymous image {i} differs from the generic card")
            report["anonymous_images"].append({"result": "byte-identical-generic-PNG"})
        if detailed:
            grant = viewer_grant()
            auth = {"Authorization": "Bearer " + grant}
            response = http.post(origin + "/api/og/mint", headers=auth, json={"url": "/?c=t", "days": 1}, timeout=90)
            if response.status_code != 201:
                raise ClickException(f"Temporary preview mint failed: HTTP {response.status_code}")
            minted = response.json()
            token = minted["token"]
            signed_image = None
            try:
                page_url = urlsplit(minted["url"])
                if page_url.scheme + "://" + page_url.netloc != origin:
                    raise ClickException("Minted page is not same-origin")
                page = http.get(minted["url"], timeout=90, allow_redirects=False)
                if page.status_code != 200:
                    raise ClickException("Detailed preview page failed")
                signed_image = metadata(page.text)["og:image"]
                parsed = urlsplit(signed_image)
                query = dict(parse_qsl(parsed.query))
                if parsed.scheme + "://" + parsed.netloc != origin or parsed.path != "/og/map.png" or query.get("r") != version or not query.get("s"):
                    raise ClickException("Detailed image is not same-origin, versioned and scan-pinned")
                body = png(http.get(signed_image, timeout=90), "full")
                if body == generic or png(http.get(signed_image, timeout=90), "full") != body:
                    raise ClickException("Detailed cold/warm image is absent or inconsistent")
                (output / "detailed.png").write_bytes(body)
                query["sig"] = "AA" + query["sig"][2:]
                expired_image = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
                if png(http.get(expired_image, timeout=90), "anon") != generic:
                    raise ClickException("Expired image signature did not downgrade to generic")
            finally:
                revoked = http.delete(origin + "/api/og/tokens/" + token, headers=auth, timeout=90)
                if revoked.status_code != 200 or revoked.json() != {"revoked": 1}:
                    raise ClickException("Temporary preview revocation failed; investigate before continuing")
            if signed_image and png(http.get(signed_image, timeout=90), "anon") != generic:
                raise ClickException("Revoked warmed detailed image did not downgrade to generic")
            report["detailed"] = "verified-cold-warm-expired-revoked; temporary-token-revoked"
        report["generic_sha256"] = sha256(generic).hexdigest()
        report["ok"] = True
    (output / "_SUCCESS.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    cli()
