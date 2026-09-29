#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyjwt[crypto]>=2"]
# ///
"""scripts/ops/testflight.py against a fake App Store Connect: first publish, re-publish."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import sys
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


FAKE = Fake()


def envelope(kind: str, id_: str, attributes: dict) -> dict:
    return {"type": kind, "id": id_, "attributes": attributes}


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
            return self._send(200, {"data": [envelope("builds", "B1", {"version": "42", "processingState": FAKE.processing})]})
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
            return self._send(200, {"data": [FAKE.group] if FAKE.group else []})
        if method == "POST" and path == "/v1/betaGroups":
            attrs = {**body["data"]["attributes"], "publicLink": "https://testflight.apple.com/join/ABC123"}
            FAKE.group = envelope("betaGroups", "G1", attrs)
            return self._send(201, {"data": FAKE.group})
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
        return self._send(404, {"errors": [{"detail": f"unhandled {method} {path}"}]})

    def do_GET(self) -> None:
        self.handle_any("GET")

    def do_POST(self) -> None:
        self.handle_any("POST")

    def do_PATCH(self) -> None:
        self.handle_any("PATCH")


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

        # 4. a build that has not finished processing is refused, never submitted
        FAKE.processing = "PROCESSING"
        try:
            run_publish(args)
        except SystemExit:
            pass
        else:
            raise AssertionError("publish must refuse an unprocessed build")
        FAKE.processing = "VALID"

        # 5. a missing app record explains the one manual step
        FAKE.app_exists = False
        try:
            testflight.find_app(testflight.load_config())
        except SystemExit:
            pass
        else:
            raise AssertionError("missing app record must stop with instructions")
        print("testflight.test: ok")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
