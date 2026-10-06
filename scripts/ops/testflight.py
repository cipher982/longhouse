#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyjwt[crypto]>=2"]
# ///
"""Drive the TestFlight side of App Store Connect for the Longhouse iOS app.

    testflight.py status                      app, recent builds, groups, public link
    testflight.py wait-build --build N        block until build N has finished processing
    testflight.py publish --build N           make build N reachable through the public link
                                              and the internal group
    testflight.py dev-certs                   ids of the API-created development certificates
    testflight.py revoke-dev-certs --keep F   revoke the API-created development certificates
                                              not listed in F (one id per line)

`publish` is idempotent: it upserts the tester-facing text and review details from
ios/testflight/beta.toml, attaches the build to the public beta group, submits it for
Beta App Review when that is still needed, and prints the public link. It also keeps an
internal group (every App Store Connect account holder/admin, with access to all builds):
internal testers get a processed build immediately, with no Beta App Review, and the
group's all-builds flag means no per-build attach exists or is allowed. The app record
itself cannot be created through the API; create it once in App Store Connect.

Environment (nothing is read from anywhere else):
    ASC_API_KEY_ID, ASC_API_ISSUER_ID, and ASC_API_KEY_P8_B64 (or ASC_API_KEY_PATH)
    TESTFLIGHT_FEEDBACK_EMAIL             where testers' feedback goes (shown to testers)
    TESTFLIGHT_REVIEW_CONTACT_FIRST/LAST/PHONE/EMAIL   Apple's reviewer contact (not public)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jwt

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "ios" / "testflight" / "beta.toml"
API = "https://api.appstoreconnect.apple.com"
# The landing page's Download on iOS buttons read this file; the README carries the same link.
LANDING_LINK_FILE = ROOT / "web" / "src" / "features" / "marketing" / "landing" / "links.ts"

# externalBuildState values after which nothing more needs submitting.
# App Store Connect roles whose users become internal testers.
INTERNAL_TESTER_ROLES = {"ACCOUNT_HOLDER", "ADMIN"}

PAST_SUBMISSION = {
    "WAITING_FOR_BETA_REVIEW",
    "IN_BETA_REVIEW",
    "BETA_APPROVED",
    "READY_FOR_BETA_TESTING",
    "IN_BETA_TESTING",
}

# `xcodebuild -allowProvisioningUpdates` with an API key signs the archive for development
# first, so every machine without a usable identity gets a fresh certificate with this name.
# A hosted runner's private key dies with the VM: each run leaks one unusable certificate
# until Apple's cap fails every later archive ("Your account has reached the maximum number
# of certificates"). The build snapshots these before archiving and revokes only its own.
API_DEV_CERT_NAME = "Apple Development: Created via API"


class AscError(RuntimeError):
    pass


def landing_link() -> str | None:
    """The TestFlight link the landing page advertises."""
    match = re.search(r'IOS_TESTFLIGHT_URL\s*=\s*"([^"]+)"', LANDING_LINK_FILE.read_text())
    return match.group(1) if match else None


def die(message: str) -> None:
    print(f"testflight: {message}", file=sys.stderr)
    raise SystemExit(1)


# --- auth and transport -------------------------------------------------------


def _private_key() -> str:
    if path := os.environ.get("ASC_API_KEY_PATH"):
        return Path(path).read_text()
    if b64 := os.environ.get("ASC_API_KEY_P8_B64"):
        return base64.b64decode(b64).decode()
    die("set ASC_API_KEY_P8_B64 (or ASC_API_KEY_PATH), ASC_API_KEY_ID and ASC_API_ISSUER_ID")
    raise AssertionError


def _token() -> str:
    key_id = os.environ.get("ASC_API_KEY_ID")
    issuer = os.environ.get("ASC_API_ISSUER_ID")
    if not key_id or not issuer:
        die("set ASC_API_KEY_ID and ASC_API_ISSUER_ID")
    now = int(time.time())
    return jwt.encode(
        {"iss": issuer, "iat": now, "exp": now + 15 * 60, "aud": "appstoreconnect-v1"},
        _private_key(),
        algorithm="ES256",
        headers={"kid": key_id, "typ": "JWT"},
    )


def _scrub(text: str) -> str:
    """Error text ends up in a public workflow log: no tester address may be in it."""
    text = re.sub(r"filter\[email\]=[^&\s]*", "filter[email]=<redacted>", text)
    return re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "<email>", text)


def call(method: str, path: str, body: dict | None = None, *, ok_conflict: bool = False) -> dict:
    """One API call with a fresh token and bounded retries on throttling/5xx."""
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    last: str = ""
    for attempt in range(4):
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raw = error.read().decode(errors="replace")
            if error.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            if error.code == 409 and ok_conflict:
                return {}
            try:
                detail = "; ".join(e.get("detail", e.get("title", "")) for e in json.loads(raw)["errors"])
            except Exception:
                detail = raw[:300]
            last = f"{method} {path} -> {error.code}: {detail}"
            break
        except urllib.error.URLError as error:
            last = f"{method} {path} -> {error.reason}"
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            break
    raise AscError(_scrub(last))


def call_all(path: str) -> list[dict]:
    """Every item of a list endpoint, following Apple's pagination."""
    items: list[dict] = []
    url: str | None = path
    while url:
        page = call("GET", url)
        items += page.get("data", [])
        url = page.get("links", {}).get("next")
    return items


def q(**params: str) -> str:
    return "?" + urllib.parse.urlencode(params, safe="[],")


# --- config -------------------------------------------------------------------


def load_config() -> dict:
    return tomllib.loads(CONFIG_PATH.read_text())


def require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        die("missing environment: " + ", ".join(missing))
    return {n: os.environ[n] for n in names}


# --- lookups ------------------------------------------------------------------


def find_app(config: dict) -> dict:
    result = call("GET", "/v1/apps" + q(**{"filter[bundleId]": config["bundle_id"]}))
    if not result.get("data"):
        die(f"no App Store Connect app record for {config['bundle_id']}. Create it once (Apps > New App); the API cannot.")
    return result["data"][0]


def find_build(app_id: str, build: str, marketing_version: str | None = None) -> dict | None:
    params = {"filter[app]": app_id, "filter[version]": build, "limit": "5", "sort": "-uploadedDate"}
    if marketing_version:
        params["filter[preReleaseVersion.version]"] = marketing_version
    result = call("GET", "/v1/builds" + q(**params))
    return result["data"][0] if result.get("data") else None


def marketing_version() -> str:
    text = (ROOT / "ios" / "XcodeHarness" / "Configs" / "Version.xcconfig").read_text()
    for line in text.splitlines():
        if line.startswith("MARKETING_VERSION"):
            return line.split("=", 1)[1].strip()
    die("no MARKETING_VERSION in Version.xcconfig")
    raise AssertionError


# --- commands -----------------------------------------------------------------


def cmd_status(_: argparse.Namespace) -> None:
    config = load_config()
    app = find_app(config)
    app_id = app["id"]
    print(f"app: {app['attributes']['name']} ({config['bundle_id']}) id={app_id}")
    builds = call(
        "GET",
        "/v1/builds" + q(**{"filter[app]": app_id, "limit": "5", "sort": "-uploadedDate", "include": "buildBetaDetail"}),
    )
    details = {i["id"]: i for i in builds.get("included", []) if i["type"] == "buildBetaDetails"}
    for b in builds.get("data", []):
        rel = b["relationships"]["buildBetaDetail"]["data"]
        external = details.get(rel["id"], {}).get("attributes", {}).get("externalBuildState") if rel else None
        a = b["attributes"]
        print(f"build {a['version']}: {a['processingState']} external={external} uploaded={a['uploadedDate']}")
    groups = call("GET", f"/v1/apps/{app_id}/betaGroups")
    for g in groups.get("data", []):
        a = g["attributes"]
        if a["isInternalGroup"]:
            testers = call_all(f"/v1/betaGroups/{g['id']}/betaTesters" + q(limit="200"))
            available = [b["attributes"]["version"] for b in call_all(f"/v1/betaGroups/{g['id']}/builds" + q(limit="200"))]
            print(
                f"group {a['name']!r}: internal all_builds={a.get('hasAccessToAllBuilds')} "
                f"testers={len(testers)} builds={sorted(available, key=int, reverse=True)}"
            )
            continue
        link = a.get("publicLink") if a.get("publicLinkEnabled") else "(public link off)"
        print(f"group {a['name']!r}: internal=False link={link}")


def _api_dev_certs() -> list[dict]:
    certs = call_all("/v1/certificates" + q(**{"filter[certificateType]": "DEVELOPMENT", "limit": "200"}))
    return [c for c in certs if c["attributes"].get("name") == API_DEV_CERT_NAME]


def cmd_dev_certs(_: argparse.Namespace) -> None:
    for cert in _api_dev_certs():
        print(cert["id"])


def cmd_revoke_dev_certs(args: argparse.Namespace) -> None:
    keep = set(Path(args.keep).read_text().split())
    revoked = 0
    for cert in _api_dev_certs():
        if cert["id"] not in keep:
            call("DELETE", f"/v1/certificates/{cert['id']}")
            revoked += 1
    print(json.dumps({"revoked_dev_certs": revoked, "kept": len(keep)}))


def cmd_wait_build(args: argparse.Namespace) -> None:
    config = load_config()
    app = find_app(config)
    deadline = time.monotonic() + args.timeout
    version = args.version or marketing_version()
    state = "not yet visible"
    while time.monotonic() < deadline:
        build = find_build(app["id"], args.build, version)
        if build:
            state = build["attributes"]["processingState"]
            if state == "VALID":
                print(f"build {args.build} ({version}) is VALID")
                return
            if state in ("FAILED", "INVALID"):
                die(f"build {args.build} ended {state}; check the email/App Store Connect for the reason")
        print(f"waiting for build {args.build}: {state}", file=sys.stderr)
        time.sleep(30)
    die(f"build {args.build} not VALID after {args.timeout}s (last state: {state})")


def _upsert_beta_localization(app_id: str, config: dict, feedback_email: str) -> None:
    loc = config["localization"]
    attributes = {
        "description": loc["description"].strip(),
        "feedbackEmail": feedback_email,
        "marketingUrl": loc["marketing_url"],
        "privacyPolicyUrl": loc["privacy_policy_url"],
    }
    locale = config["locale"]
    existing = call("GET", f"/v1/apps/{app_id}/betaAppLocalizations")
    match = next((i for i in existing.get("data", []) if i["attributes"]["locale"] == locale), None)
    if match:
        call(
            "PATCH",
            f"/v1/betaAppLocalizations/{match['id']}",
            {"data": {"type": "betaAppLocalizations", "id": match["id"], "attributes": attributes}},
        )
    else:
        call(
            "POST",
            "/v1/betaAppLocalizations",
            {
                "data": {
                    "type": "betaAppLocalizations",
                    "attributes": {**attributes, "locale": locale},
                    "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
                }
            },
        )


def _update_review_detail(app_id: str, config: dict, contact: dict[str, str]) -> None:
    detail = call("GET", f"/v1/apps/{app_id}/betaAppReviewDetail")["data"]
    call(
        "PATCH",
        f"/v1/betaAppReviewDetails/{detail['id']}",
        {
            "data": {
                "type": "betaAppReviewDetails",
                "id": detail["id"],
                "attributes": {
                    "contactFirstName": contact["TESTFLIGHT_REVIEW_CONTACT_FIRST"],
                    "contactLastName": contact["TESTFLIGHT_REVIEW_CONTACT_LAST"],
                    "contactPhone": contact["TESTFLIGHT_REVIEW_CONTACT_PHONE"],
                    "contactEmail": contact["TESTFLIGHT_REVIEW_CONTACT_EMAIL"],
                    "demoAccountRequired": False,
                    "notes": config["review"]["notes"].strip(),
                },
            }
        },
    )


def _upsert_whats_new(build_id: str, config: dict, whats_new: str) -> None:
    locale = config["locale"]
    existing = call("GET", f"/v1/builds/{build_id}/betaBuildLocalizations")
    match = next((i for i in existing.get("data", []) if i["attributes"]["locale"] == locale), None)
    if match:
        call(
            "PATCH",
            f"/v1/betaBuildLocalizations/{match['id']}",
            {"data": {"type": "betaBuildLocalizations", "id": match["id"], "attributes": {"whatsNew": whats_new}}},
        )
    else:
        call(
            "POST",
            "/v1/betaBuildLocalizations",
            {
                "data": {
                    "type": "betaBuildLocalizations",
                    "attributes": {"locale": locale, "whatsNew": whats_new},
                    "relationships": {"build": {"data": {"type": "builds", "id": build_id}}},
                }
            },
        )


def _ensure_public_group(app_id: str, config: dict) -> dict:
    groups = call("GET", f"/v1/apps/{app_id}/betaGroups")
    match = next(
        (g for g in groups.get("data", []) if g["attributes"]["name"] == config["group_name"]),
        None,
    )
    wanted = {
        "publicLinkEnabled": True,
        "publicLinkLimitEnabled": True,
        "publicLinkLimit": int(config["public_link_limit"]),
        "feedbackEnabled": True,
    }
    if match is None:
        created = call(
            "POST",
            "/v1/betaGroups",
            {
                "data": {
                    "type": "betaGroups",
                    "attributes": {"name": config["group_name"], "isInternalGroup": False, **wanted},
                    "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
                }
            },
        )
        return created["data"]
    attrs = match["attributes"]
    if any(attrs.get(k) != v for k, v in wanted.items()):
        call(
            "PATCH",
            f"/v1/betaGroups/{match['id']}",
            {"data": {"type": "betaGroups", "id": match["id"], "attributes": wanted}},
        )
    return call("GET", f"/v1/betaGroups/{match['id']}")["data"]


def _in_group(app_id: str, group_id: str, build: str) -> bool:
    """Whether the group can install this build number (a filtered read, so history length never matters)."""
    params = {"filter[app]": app_id, "filter[betaGroups]": group_id, "filter[version]": build, "limit": "5"}
    return bool(call("GET", "/v1/builds" + q(**params)).get("data"))


def _ensure_internal_group(app_id: str, config: dict) -> dict:
    """The internal group, with access to all builds (App Store Connect's automatic distribution)."""
    groups = call("GET", f"/v1/apps/{app_id}/betaGroups")
    match = next(
        (
            g
            for g in groups.get("data", [])
            if g["attributes"]["isInternalGroup"] and g["attributes"]["name"] == config["internal_group_name"]
        ),
        None,
    )
    wanted = {"hasAccessToAllBuilds": True, "feedbackEnabled": True}
    if match is None:
        created = call(
            "POST",
            "/v1/betaGroups",
            {
                "data": {
                    "type": "betaGroups",
                    "attributes": {"name": config["internal_group_name"], "isInternalGroup": True, **wanted},
                    "relationships": {"app": {"data": {"type": "apps", "id": app_id}}},
                }
            },
        )
        return created["data"]
    if any(match["attributes"].get(k) != v for k, v in wanted.items()):
        call(
            "PATCH",
            f"/v1/betaGroups/{match['id']}",
            {"data": {"type": "betaGroups", "id": match["id"], "attributes": wanted}},
        )
    return call("GET", f"/v1/betaGroups/{match['id']}")["data"]


def _ensure_internal_testers(group_id: str) -> int:
    """Every App Store Connect account holder/admin is in the group; returns how many testers it has."""
    users = call_all("/v1/users" + q(limit="200"))
    members = call_all(f"/v1/betaGroups/{group_id}/betaTesters" + q(limit="200"))
    have = {m["attributes"]["email"].lower() for m in members}
    for user in users:
        attrs = user["attributes"]
        email = attrs["username"]
        if not INTERNAL_TESTER_ROLES & set(attrs.get("roles", [])) or email.lower() in have:
            continue
        known = call("GET", "/v1/betaTesters" + q(**{"filter[email]": email}))["data"]
        if known:
            call(
                "POST",
                f"/v1/betaGroups/{group_id}/relationships/betaTesters",
                {"data": [{"type": "betaTesters", "id": known[0]["id"]}]},
                ok_conflict=True,
            )
        else:
            call(
                "POST",
                "/v1/betaTesters",
                {
                    "data": {
                        "type": "betaTesters",
                        "attributes": {
                            k: v for k, v in {"email": email, "firstName": attrs.get("firstName"), "lastName": attrs.get("lastName")}.items() if v
                        },
                        "relationships": {"betaGroups": {"data": [{"type": "betaGroups", "id": group_id}]}},
                    }
                },
            )
        have.add(email.lower())
    return len(have)


# States Apple passes through on its way to a state we can act on.
TRANSITIONAL = {"PROCESSING", "IN_EXPORT_COMPLIANCE_REVIEW", "NOT_APPLICABLE", None}
POLL_DELAY = 10
POLL_ATTEMPTS = 18


def _settled_external_state(build_id: str) -> str | None:
    """The build's external state once it is no longer in transit (bounded wait)."""
    state = None
    for attempt in range(POLL_ATTEMPTS):
        detail = call("GET", f"/v1/builds/{build_id}/buildBetaDetail")["data"]
        state = detail["attributes"].get("externalBuildState")
        if state not in TRANSITIONAL:
            return state
        if attempt < POLL_ATTEMPTS - 1:
            time.sleep(POLL_DELAY)
    die(f"build stayed in external state {state} for {POLL_ATTEMPTS * POLL_DELAY}s; check App Store Connect")
    raise AssertionError


def _default_whats_new() -> str:
    subject = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()
    return f"Version {marketing_version()}. {subject}".strip()[:4000]


def cmd_publish(args: argparse.Namespace) -> None:
    config = load_config()
    feedback = require_env("TESTFLIGHT_FEEDBACK_EMAIL")["TESTFLIGHT_FEEDBACK_EMAIL"]
    contact = require_env(
        "TESTFLIGHT_REVIEW_CONTACT_FIRST",
        "TESTFLIGHT_REVIEW_CONTACT_LAST",
        "TESTFLIGHT_REVIEW_CONTACT_PHONE",
        "TESTFLIGHT_REVIEW_CONTACT_EMAIL",
    )
    app = find_app(config)
    app_id = app["id"]
    version = args.version or marketing_version()

    build = find_build(app_id, args.build, version)
    if build is None:
        die(f"build {args.build} ({version}) not found; upload it first")
    if build["attributes"]["processingState"] != "VALID":
        die(f"build {args.build} is {build['attributes']['processingState']}; run wait-build first")
    build_id = build["id"]

    _upsert_beta_localization(app_id, config, feedback)
    _update_review_detail(app_id, config, contact)
    _upsert_whats_new(build_id, config, args.whats_new or _default_whats_new())

    beta_detail = call("GET", f"/v1/builds/{build_id}/buildBetaDetail")["data"]
    if not beta_detail["attributes"].get("autoNotifyEnabled"):
        call(
            "PATCH",
            f"/v1/buildBetaDetails/{beta_detail['id']}",
            {"data": {"type": "buildBetaDetails", "id": beta_detail["id"], "attributes": {"autoNotifyEnabled": True}}},
        )

    group = _ensure_public_group(app_id, config)
    call(
        "POST",
        f"/v1/betaGroups/{group['id']}/relationships/builds",
        {"data": [{"type": "builds", "id": build_id}]},
        ok_conflict=True,
    )

    external = _settled_external_state(build_id)
    submitted = False
    if external == "READY_FOR_BETA_SUBMISSION":
        call(
            "POST",
            "/v1/betaAppReviewSubmissions",
            {"data": {"type": "betaAppReviewSubmissions", "relationships": {"build": {"data": {"type": "builds", "id": build_id}}}}},
        )
        submitted = True
        external = call("GET", f"/v1/builds/{build_id}/buildBetaDetail")["data"]["attributes"].get("externalBuildState")
    elif external not in PAST_SUBMISSION:
        die(f"build {args.build} is in external state {external}; not submitting (see App Store Connect)")

    # Internal testers need no Beta App Review. The group's all-builds flag attaches the build
    # (an explicit attach is refused by App Store Connect), so this ensures the group and checks the build is in it.
    internal_testers = 0
    internal_error = None
    try:
        internal = _ensure_internal_group(app_id, config)
        internal_testers = _ensure_internal_testers(internal["id"])
        for attempt in range(POLL_ATTEMPTS):
            if _in_group(app_id, internal["id"], args.build):
                break
            if attempt == POLL_ATTEMPTS - 1:
                raise AscError(f"build {args.build} is not available to internal group {config['internal_group_name']!r} after {POLL_ATTEMPTS * POLL_DELAY}s")
            time.sleep(POLL_DELAY)
    except AscError as error:
        internal_error = str(error)

    group = call("GET", f"/v1/betaGroups/{group['id']}")["data"]
    result = {
        "build": args.build,
        "version": version,
        "external_build_state": external,
        "submitted_this_run": submitted,
        "internal_group_testers": internal_testers,
        "internal_build_available": internal_error is None,
        "public_link": group["attributes"].get("publicLink"),
    }
    print(json.dumps(result, indent=2))
    # Re-enabling a switched-off link, or a recreated group, can mint a new one; the buttons must not go dead silently.
    advertised = landing_link()
    if result["public_link"] != advertised:
        die(
            f"the group's public link is {result['public_link']} but the landing page advertises {advertised}; "
            "update IOS_TESTFLIGHT_URL in web/src/features/marketing/landing/links.ts and the link in README.md"
        )
    if internal_error:
        # The public work above is done and reported; this is the owner's fast lane, so it still fails the run.
        die(f"internal group: {internal_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").set_defaults(func=cmd_status)

    wait = sub.add_parser("wait-build")
    wait.add_argument("--build", required=True)
    wait.add_argument("--version", help="marketing version (default: Version.xcconfig)")
    wait.add_argument("--timeout", type=int, default=1800)
    wait.set_defaults(func=cmd_wait_build)

    publish = sub.add_parser("publish")
    publish.add_argument("--build", required=True)
    publish.add_argument("--version", help="marketing version (default: Version.xcconfig)")
    publish.add_argument("--whats-new", help="tester-facing change note (default: latest commit subject)")
    publish.set_defaults(func=cmd_publish)

    sub.add_parser("dev-certs").set_defaults(func=cmd_dev_certs)
    revoke = sub.add_parser("revoke-dev-certs")
    revoke.add_argument("--keep", required=True, help="file of certificate ids to keep, one per line")
    revoke.set_defaults(func=cmd_revoke_dev_certs)

    args = parser.parse_args()
    try:
        args.func(args)
    except AscError as error:
        die(str(error))


if __name__ == "__main__":
    main()
