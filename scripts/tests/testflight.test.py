#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyjwt[crypto]>=2"]
# ///
"""scripts/ops/testflight.py against a fake App Store Connect: first publish, re-publish, internal group."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]

spec = importlib.util.spec_from_file_location("testflight", ROOT / "scripts" / "ops" / "testflight.py")
testflight = importlib.util.module_from_spec(spec)
sys.modules["testflight"] = testflight
spec.loader.exec_module(testflight)


def envelope(kind: str, id_: str, attributes: dict) -> dict:
    return {"type": kind, "id": id_, "attributes": attributes}


class Fake:
    """Just enough of App Store Connect for the publish path."""

    def __init__(self) -> None:
        self.app_exists = True
        self.external = "READY_FOR_BETA_SUBMISSION"
        self.processing = "VALID"
        self.localizations: list[dict] = []
        self.group: dict | None = None
        self.attached = False
        self.submissions = 0
        self.review_detail: dict = {}
        self.whats_new: dict = {}
        self.auto_notify = False
        self.calls: list[str] = []
        self.transient_reads = 0
        # internal group (G2) and the people App Store Connect knows about
        self.internal: dict | None = None
        self.users = [
            {"id": "U1", "username": "owner@example.test", "firstName": "Ada", "lastName": "Owner", "roles": ["ACCOUNT_HOLDER", "ADMIN"]},
            {"id": "U2", "username": "books@example.test", "firstName": "Fin", "lastName": "Ance", "roles": ["FINANCE"]},
            {"id": "U3", "username": "late@example.test", "firstName": "Lee", "lastName": "Admin", "roles": ["ADMIN"]},
        ]
        self.testers: list[dict] = []  # every beta tester record, any group
        self.internal_members: list[str] = []  # tester ids in G2
        self.internal_hidden_reads = 0  # reads of G2's builds that do not list B1 yet
        self.internal_never_lists = False
        self.tester_lookup_fails = False
        # the owner's own identity, an API certificate from before the run, and a distribution certificate
        self.certificates = [
            envelope("certificates", "C-OWNER", {"certificateType": "DEVELOPMENT", "name": "Apple Development: Ada Owner"}),
            envelope("certificates", "C-OLD", {"certificateType": "DEVELOPMENT", "name": "Apple Development: Created via API"}),
            envelope("certificates", "C-DIST", {"certificateType": "DISTRIBUTION", "name": "Apple Distribution: Created via API"}),
        ]
        self.revoked: list[str] = []


FAKE = Fake()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D401
        pass

    def _send(self, status: int, body: dict | None = None) -> None:
        payload = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else {}

    def handle_any(self, method: str) -> None:
        url = urlparse(self.path)
        path = url.path
        FAKE.calls.append(f"{method} {path}")
        assert self.headers["Authorization"].startswith("Bearer "), "unauthenticated call"
        body = self._body() if method in ("POST", "PATCH") else {}

        if method == "GET" and path == "/v1/apps":
            data = [envelope("apps", "APP", {"name": "Longhouse: Agent Control", "bundleId": "ai.longhouse.ios"})]
            return self._send(200, {"data": data if FAKE.app_exists else []})
        if method == "GET" and path == "/v1/builds":
            query = parse_qs(url.query)
            assert query["filter[version]"] == ["42"]
            build = envelope("builds", "B1", {"version": "42", "processingState": FAKE.processing})
            if "filter[betaGroups]" in query:
                # "is this build in the internal group": a filtered read, never a walk of the group's history
                assert query["filter[betaGroups]"] == ["G2"]
                if FAKE.internal_never_lists:
                    return self._send(200, {"data": []})
                if FAKE.internal_hidden_reads > 0:
                    FAKE.internal_hidden_reads -= 1
                    return self._send(200, {"data": []})
                return self._send(200, {"data": [build] if FAKE.internal["attributes"]["hasAccessToAllBuilds"] else []})
            return self._send(200, {"data": [build]})
        if path == "/v1/apps/APP/betaAppLocalizations":
            return self._send(200, {"data": FAKE.localizations})
        if method == "POST" and path == "/v1/betaAppLocalizations":
            attrs = body["data"]["attributes"]
            assert body["data"]["relationships"]["app"]["data"]["id"] == "APP"
            FAKE.localizations.append(envelope("betaAppLocalizations", "L1", attrs))
            return self._send(201, {"data": FAKE.localizations[-1]})
        if method == "PATCH" and path == "/v1/betaAppLocalizations/L1":
            FAKE.localizations[0]["attributes"].update(body["data"]["attributes"])
            return self._send(200, {"data": FAKE.localizations[0]})
        if path == "/v1/apps/APP/betaAppReviewDetail":
            return self._send(200, {"data": envelope("betaAppReviewDetails", "RD", FAKE.review_detail)})
        if method == "PATCH" and path == "/v1/betaAppReviewDetails/RD":
            FAKE.review_detail.update(body["data"]["attributes"])
            return self._send(200, {"data": envelope("betaAppReviewDetails", "RD", FAKE.review_detail)})
        if path == "/v1/builds/B1/betaBuildLocalizations" and method == "GET":
            return self._send(200, {"data": [envelope("betaBuildLocalizations", "BL", FAKE.whats_new)] if FAKE.whats_new else []})
        if method == "POST" and path == "/v1/betaBuildLocalizations":
            FAKE.whats_new.update(body["data"]["attributes"])
            return self._send(201, {"data": envelope("betaBuildLocalizations", "BL", FAKE.whats_new)})
        if method == "PATCH" and path == "/v1/betaBuildLocalizations/BL":
            FAKE.whats_new.update(body["data"]["attributes"])
            return self._send(200, {"data": envelope("betaBuildLocalizations", "BL", FAKE.whats_new)})
        if path == "/v1/builds/B1/buildBetaDetail":
            state = FAKE.external
            if FAKE.transient_reads > 0:
                FAKE.transient_reads -= 1
                state = "PROCESSING"
            attrs = {"autoNotifyEnabled": FAKE.auto_notify, "externalBuildState": state}
            return self._send(200, {"data": envelope("buildBetaDetails", "BD", attrs)})
        if method == "PATCH" and path == "/v1/buildBetaDetails/BD":
            FAKE.auto_notify = body["data"]["attributes"]["autoNotifyEnabled"]
            return self._send(200, {"data": {}})
        if path == "/v1/apps/APP/betaGroups":
            return self._send(200, {"data": [g for g in (FAKE.group, FAKE.internal) if g]})
        if method == "POST" and path == "/v1/betaGroups":
            attrs = body["data"]["attributes"]
            if attrs["isInternalGroup"]:
                FAKE.internal = envelope("betaGroups", "G2", attrs)
                return self._send(201, {"data": FAKE.internal})
            attrs = {**attrs, "publicLink": "https://testflight.apple.com/join/ABC123"}
            FAKE.group = envelope("betaGroups", "G1", attrs)
            return self._send(201, {"data": FAKE.group})
        if path == "/v1/betaGroups/G2":
            if method == "PATCH":
                FAKE.internal["attributes"].update(body["data"]["attributes"])
            return self._send(200, {"data": FAKE.internal})
        if method == "POST" and path == "/v1/betaGroups/G2/relationships/builds":
            return self._send(422, {"errors": [{"detail": "Cannot add internal group to a build."}]})
        if method == "GET" and path == "/v1/betaGroups/G2/betaTesters":
            return self._send(200, {"data": [t for t in FAKE.testers if t["id"] in FAKE.internal_members]})
        if method == "POST" and path == "/v1/betaGroups/G2/relationships/betaTesters":
            FAKE.internal_members.extend(item["id"] for item in body["data"] if item["id"] not in FAKE.internal_members)
            return self._send(204)
        if method == "GET" and path == "/v1/users":
            # one user per page, so anything past the first page is only found by following links.next
            page = int(parse_qs(url.query).get("page", ["0"])[0])
            u = FAKE.users[page]
            body = {"data": [envelope("users", u["id"], {k: v for k, v in u.items() if k != "id"})], "links": {}}
            if page + 1 < len(FAKE.users):
                body["links"]["next"] = f"http://{self.headers['Host']}/v1/users?page={page + 1}"
            return self._send(200, body)
        if method == "GET" and path == "/v1/betaTesters":
            wanted = parse_qs(url.query)["filter[email]"][0]
            if FAKE.tester_lookup_fails:
                return self._send(403, {"errors": [{"detail": f"no access for {wanted}"}]})
            return self._send(200, {"data": [t for t in FAKE.testers if t["attributes"]["email"] == wanted]})
        if method == "POST" and path == "/v1/betaTesters":
            tester = envelope("betaTesters", f"T{len(FAKE.testers) + 1}", body["data"]["attributes"])
            FAKE.testers.append(tester)
            for group in body["data"]["relationships"]["betaGroups"]["data"]:
                assert group["id"] == "G2", "the fake only knows the internal group's testers"
                FAKE.internal_members.append(tester["id"])
            return self._send(201, {"data": tester})
        if path == "/v1/betaGroups/G1":
            if method == "PATCH":
                FAKE.group["attributes"].update(body["data"]["attributes"])
            return self._send(200, {"data": FAKE.group})
        if method == "POST" and path == "/v1/betaGroups/G1/relationships/builds":
            if FAKE.attached:
                return self._send(409, {"errors": [{"detail": "already attached"}]})
            FAKE.attached = True
            return self._send(204)
        if method == "POST" and path == "/v1/betaAppReviewSubmissions":
            FAKE.submissions += 1
            FAKE.external = "WAITING_FOR_BETA_REVIEW"
            return self._send(201, {"data": {}})
        if method == "GET" and path == "/v1/certificates":
            wanted = parse_qs(url.query).get("filter[certificateType]", [None])[0]
            return self._send(200, {"data": [c for c in FAKE.certificates if wanted in (None, c["attributes"]["certificateType"])]})
        if method == "DELETE" and path.startswith("/v1/certificates/"):
            cert_id = path.rsplit("/", 1)[1]
            FAKE.certificates = [c for c in FAKE.certificates if c["id"] != cert_id]
            FAKE.revoked.append(cert_id)
            return self._send(204)
        return self._send(404, {"errors": [{"detail": f"unhandled {method} {path}"}]})

    def do_GET(self) -> None:
        self.handle_any("GET")

    def do_POST(self) -> None:
        self.handle_any("POST")

    def do_PATCH(self) -> None:
        self.handle_any("PATCH")

    def do_DELETE(self) -> None:
        self.handle_any("DELETE")


def run_publish(args: argparse.Namespace) -> dict:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        testflight.cmd_publish(args)
    return json.loads(buffer.getvalue())


def main() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pkcs8 = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    os.environ.update(
        ASC_API_KEY_ID="KEYID12345",
        ASC_API_ISSUER_ID="00000000-0000-0000-0000-000000000000",
        ASC_API_KEY_P8_B64=base64.b64encode(pkcs8.encode()).decode(),
        TESTFLIGHT_FEEDBACK_EMAIL="beta@example.test",
        TESTFLIGHT_REVIEW_CONTACT_FIRST="Ada",
        TESTFLIGHT_REVIEW_CONTACT_LAST="Reviewer",
        TESTFLIGHT_REVIEW_CONTACT_PHONE="+15555550100",
        TESTFLIGHT_REVIEW_CONTACT_EMAIL="review@example.test",
    )

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    testflight.API = f"http://127.0.0.1:{server.server_port}"
    fake_landing = tempfile.NamedTemporaryFile("w", suffix=".ts", delete=False)
    fake_landing.write('export const IOS_TESTFLIGHT_URL = "https://testflight.apple.com/join/ABC123";\n')
    fake_landing.close()
    testflight.LANDING_LINK_FILE = Path(fake_landing.name)
    args = argparse.Namespace(build="42", version="0.1.56", whats_new="note for testers")

    try:
        # the token is a real ES256 JWT with the right claims
        import jwt

        token = testflight._token()
        header = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False})
        assert header["alg"] == "ES256" and header["kid"] == "KEYID12345", header
        assert claims["aud"] == "appstoreconnect-v1" and claims["exp"] - claims["iat"] <= 20 * 60, claims

        # 1. first publish: creates everything, submits once, returns the public link
        first = run_publish(args)
        assert first["submitted_this_run"] is True, first
        assert first["external_build_state"] == "WAITING_FOR_BETA_REVIEW", first
        assert first["public_link"] == "https://testflight.apple.com/join/ABC123", first
        assert FAKE.group["attributes"]["publicLinkEnabled"] is True
        assert FAKE.group["attributes"]["publicLinkLimit"] == 100
        assert FAKE.localizations[0]["attributes"]["feedbackEmail"] == "beta@example.test"
        assert FAKE.review_detail["demoAccountRequired"] is False
        assert "Explore the demo" in FAKE.review_detail["notes"]
        assert FAKE.whats_new["whatsNew"] == "note for testers" and FAKE.auto_notify is True
        assert FAKE.submissions == 1

        # 2. second publish: idempotent, no second submission, no duplicate group/localization
        second = run_publish(args)
        assert second["submitted_this_run"] is False, second
        assert FAKE.submissions == 1
        assert len(FAKE.localizations) == 1

        # 3. a link switched off in the console is switched back on
        FAKE.group["attributes"]["publicLinkEnabled"] = False
        run_publish(args)
        assert FAKE.group["attributes"]["publicLinkEnabled"] is True

        # 3b. Apple still moving the build between states is waited out, not fatal
        testflight.POLL_DELAY = 0
        FAKE.transient_reads = 3
        FAKE.external = "READY_FOR_BETA_SUBMISSION"
        FAKE.submissions = 0
        transient = run_publish(args)
        assert transient["submitted_this_run"] is True and FAKE.submissions == 1, transient

        # 3c. internal group: created with all-builds access, holds only the account holder/admin, and the build
        # reaches it with no per-build attach (App Store Connect refuses one) and no Beta App Review involved.
        assert FAKE.internal["attributes"]["isInternalGroup"] is True
        assert FAKE.internal["attributes"]["hasAccessToAllBuilds"] is True
        # the second admin sits on the second page of /v1/users; the finance user is never invited
        assert [t["attributes"]["email"] for t in FAKE.testers] == ["owner@example.test", "late@example.test"], FAKE.testers
        assert FAKE.testers[0]["attributes"]["firstName"] == "Ada"
        assert first["internal_build_available"] is True and first["internal_group_testers"] == 2, first
        assert "POST /v1/betaGroups/G2/relationships/builds" not in FAKE.calls
        assert FAKE.calls.count("POST /v1/betaTesters") == 2, "a second publish must not re-invite"
        assert FAKE.calls.count("POST /v1/betaGroups") == 2, "one public group and one internal group, once each"

        # 3d. all-builds access switched off in the console is switched back on; a tester removed from the group is
        # re-added from the existing tester record rather than invited again
        FAKE.internal["attributes"]["hasAccessToAllBuilds"] = False
        FAKE.internal_members.clear()
        repaired = run_publish(args)
        assert FAKE.internal["attributes"]["hasAccessToAllBuilds"] is True
        assert sorted(FAKE.internal_members) == ["T1", "T2"] and len(FAKE.testers) == 2, (FAKE.internal_members, FAKE.testers)
        assert repaired["internal_build_available"] is True

        # 3d'. an internal-group failure still fails the run, but the public result is printed first and no
        # tester address reaches the (public) log, whether it came from our URL or from Apple's error text
        import io
        from contextlib import redirect_stderr
        from contextlib import redirect_stdout

        FAKE.internal_members.clear()
        FAKE.tester_lookup_fails = True
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                testflight.cmd_publish(args)
            except SystemExit:
                pass
            else:
                raise AssertionError("an internal-group failure must fail the publish")
        FAKE.tester_lookup_fails = False
        printed = json.loads(out.getvalue())
        assert printed["internal_build_available"] is False and printed["public_link"], printed
        assert "internal group" in err.getvalue() and "example.test" not in err.getvalue() + out.getvalue(), err.getvalue()
        run_publish(args)
        assert sorted(FAKE.internal_members) == ["T1", "T2"]

        # 3e. a build the group lists late is waited for; one it never lists fails the publish
        FAKE.internal_hidden_reads = 2
        assert run_publish(args)["internal_build_available"] is True
        FAKE.internal_never_lists = True
        polls = testflight.POLL_ATTEMPTS
        testflight.POLL_ATTEMPTS = 2
        try:
            run_publish(args)
        except SystemExit:
            pass
        else:
            raise AssertionError("publish must fail when the internal group cannot see the build")
        testflight.POLL_ATTEMPTS = polls
        FAKE.internal_never_lists = False

        # 4. a build that has not finished processing is refused, never submitted
        FAKE.processing = "PROCESSING"
        try:
            run_publish(args)
        except SystemExit:
            pass
        else:
            raise AssertionError("publish must refuse an unprocessed build")
        FAKE.processing = "VALID"

        # 5. the link the landing page advertises is the group's link: a regenerated link fails the publish
        # instead of leaving the Download on iOS buttons pointing at a dead invite.
        advertised = ROOT / "web" / "src" / "features" / "marketing" / "landing" / "links.ts"
        testflight.LANDING_LINK_FILE = advertised
        real = testflight.landing_link()
        assert real and real.startswith("https://testflight.apple.com/join/"), real
        assert real in (ROOT / "README.md").read_text(), "README and links.ts must advertise the same link"
        with tempfile.TemporaryDirectory() as directory:
            landing = Path(directory) / "links.ts"
            landing.write_text('export const IOS_TESTFLIGHT_URL = "https://testflight.apple.com/join/ABC123";\n')
            testflight.LANDING_LINK_FILE = landing
            assert run_publish(args)["public_link"] == "https://testflight.apple.com/join/ABC123"
            FAKE.group["attributes"]["publicLink"] = "https://testflight.apple.com/join/REGEN99"
            try:
                run_publish(args)
            except SystemExit:
                pass
            else:
                raise AssertionError("publish must fail when the group's link is not the advertised one")
        testflight.LANDING_LINK_FILE = advertised

        # 6. a missing app record explains the one manual step
        FAKE.app_exists = False
        try:
            testflight.find_app(testflight.load_config())
        except SystemExit:
            pass
        else:
            raise AssertionError("missing app record must stop with instructions")

        # 7. the build revokes only the development certificate its own archive created: never the
        # owner's identity, a distribution certificate, or an API certificate that existed before it
        import io
        from contextlib import redirect_stdout

        snapshot = io.StringIO()
        with redirect_stdout(snapshot):
            testflight.cmd_dev_certs(argparse.Namespace())
        assert snapshot.getvalue().split() == ["C-OLD"], snapshot.getvalue()
        FAKE.certificates.append(
            envelope("certificates", "C-RUN", {"certificateType": "DEVELOPMENT", "name": "Apple Development: Created via API"})
        )
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as keep:
            keep.write(snapshot.getvalue())
            keep.flush()
            with redirect_stdout(io.StringIO()):
                testflight.cmd_revoke_dev_certs(argparse.Namespace(keep=keep.name))
        assert FAKE.revoked == ["C-RUN"], FAKE.revoked
        assert {c["id"] for c in FAKE.certificates} == {"C-OWNER", "C-OLD", "C-DIST"}, FAKE.certificates
        print("testflight.test: ok")
    finally:
        server.shutdown()
        os.unlink(fake_landing.name)


if __name__ == "__main__":
    main()
