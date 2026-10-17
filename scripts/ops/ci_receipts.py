#!/usr/bin/env python3
"""CI receipts: immutable per-run records in the artifact object store.

A receipt is a fact one CI run recorded about a commit or a run that a later gate
reads: the Hosted Live QA verdict, the engine-compat result, the runtime image
publication, the canary verification, the OCI archive seal. They used to be
GitHub Actions artifacts, which are metered storage on a private repository and
reachable only through GitHub. They now live in object storage under

    <prefix>/<kind>/<subject>/<run_id>-<attempt>/<file>
    <prefix>/<kind>/<subject>/<run_id>-<attempt>/_receipt.json     the stamp

`subject` is what the receipt is about (a commit SHA, or the publishing run's
id). The stamp carries the writing run's identity from GitHub's own environment;
it is written last, so a receipt without one is invisible, and an existing stamp
is never overwritten. The store cannot vouch for who wrote an object, so a reader
that needs authenticity must resolve the stamped run through the GitHub API and
check the run record (workflow path, branch, repository, conclusion), as
promotion_gates.py does.

Debug evidence is not a receipt: it stays on the runner that produced it.

Standard library only (SigV4 over urllib): the gate scripts run under a bare
`python3` on a laptop and on every runner.

  ci_receipts.py put KIND SUBJECT FILE...                    write (inside a GitHub Actions run)
  ci_receipts.py get KIND SUBJECT --file NAME [--instance I | --max-attempt N]
  ci_receipts.py get KIND SUBJECT --dir DEST [--instance I | --max-attempt N]
  ci_receipts.py list KIND SUBJECT

Configuration (ordinary environment values, never a secret manager):
  CI_RECEIPTS_S3_ENDPOINT, CI_RECEIPTS_S3_BUCKET, CI_RECEIPTS_S3_PREFIX,
  CI_RECEIPTS_S3_ACCESS_KEY_ID, CI_RECEIPTS_S3_SECRET_ACCESS_KEY [, CI_RECEIPTS_S3_REGION]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Callable

STAMP_SCHEMA = "longhouse.ci-receipt.v1"
STAMP_NAME = "_receipt.json"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_INSTANCE = re.compile(r"^([1-9][0-9]*)-([1-9][0-9]*)$")
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_ENV = "CI_RECEIPTS_S3_"


class ReceiptError(Exception):
    """A receipt could not be read or written."""


def _safe(label: str, value: str) -> str:
    if not _SAFE.match(value) or ".." in value:
        raise ReceiptError(f"unsafe receipt {label}: {value!r}")
    return value


class S3:
    """Path-style S3 over urllib with SigV4: put, get, list. Enough for receipts."""

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "us-east-1") -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region

    def _sign(self, method: str, path: str, query: dict[str, str], payload: bytes, now: dt.datetime) -> dict[str, str]:
        host = urllib.parse.urlsplit(self.endpoint).netloc
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()
        canonical_query = "&".join(
            f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in sorted(query.items())
        )
        headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        signed = ";".join(sorted(headers))
        canonical = "\n".join(
            [method, urllib.parse.quote(path, safe="/-_.~"), canonical_query]
            + [f"{k}:{headers[k]}" for k in sorted(headers)]
            + ["", signed, payload_hash]
        )
        scope = f"{day}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])

        def mac(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        key = mac(mac(mac(mac(("AWS4" + self.secret_key).encode(), day), self.region), "s3"), "aws4_request")
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        return {
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, SignedHeaders={signed}, Signature={signature}",
        }

    def _request(self, method: str, key: str, query: dict[str, str] | None = None, body: bytes = b"") -> tuple[int, bytes]:
        path = f"/{self.bucket}/{key}" if key else f"/{self.bucket}"
        query = query or {}
        headers = self._sign(method, path, query, body, dt.datetime.now(dt.timezone.utc))
        url = self.endpoint + urllib.parse.quote(path, safe="/-_.~")
        if query:
            url += "?" + "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in sorted(query.items()))
        request = urllib.request.Request(url, data=body if method == "PUT" else None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - operator-configured endpoint
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReceiptError(f"receipt store unreachable ({self.endpoint}): {exc}") from exc

    def put(self, key: str, data: bytes) -> None:
        status, body = self._request("PUT", key, body=data)
        if status not in (200, 201):
            raise ReceiptError(f"receipt store refused PUT {key}: HTTP {status} {body[:200]!r}")

    def get(self, key: str) -> bytes | None:
        status, body = self._request("GET", key)
        if status == 404:
            return None
        if status != 200:
            raise ReceiptError(f"receipt store refused GET {key}: HTTP {status} {body[:200]!r}")
        return body

    def list(self, prefix: str, delimiter: str = "") -> tuple[list[str], list[str]]:
        """(object keys, common prefixes) under prefix, following continuation tokens."""
        keys: list[str] = []
        prefixes: list[str] = []
        token = ""
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if delimiter:
                query["delimiter"] = delimiter
            if token:
                query["continuation-token"] = token
            status, body = self._request("GET", "", query)
            if status != 200:
                raise ReceiptError(f"receipt store refused LIST {prefix}: HTTP {status} {body[:200]!r}")
            root = ET.fromstring(body)
            keys += [e.findtext(f"{_S3_NS}Key") or "" for e in root.findall(f"{_S3_NS}Contents")]
            prefixes += [e.findtext(f"{_S3_NS}Prefix") or "" for e in root.findall(f"{_S3_NS}CommonPrefixes")]
            if (root.findtext(f"{_S3_NS}IsTruncated") or "").lower() != "true":
                return keys, prefixes
            token = root.findtext(f"{_S3_NS}NextContinuationToken") or ""
            if not token:
                raise ReceiptError("receipt store listing is truncated without a continuation token")


@dataclass(frozen=True)
class Receipt:
    kind: str
    subject: str
    instance: str  # "<run_id>-<attempt>"
    stamp: dict[str, Any]
    _read: Callable[[str], bytes]
    files: tuple[str, ...]

    @property
    def run_id(self) -> int:
        return int(self.stamp["run_id"])

    @property
    def attempt(self) -> int:
        return int(self.stamp["run_attempt"])

    def read(self, name: str) -> bytes:
        return self._read(name)

    def json(self, name: str) -> Any:
        try:
            return json.loads(self.read(name))
        except ValueError as exc:
            raise ReceiptError(f"receipt {self.kind}/{self.subject}/{self.instance}/{name} is not JSON: {exc}") from exc


class ReceiptStore:
    def __init__(self, s3: S3, prefix: str) -> None:
        prefix = prefix.strip("/")
        if not prefix or any(part in {"", ".", ".."} for part in prefix.split("/")):
            raise ReceiptError("receipt prefix must be a non-empty safe relative path")
        self.s3 = s3
        self.prefix = prefix

    def _base(self, kind: str, subject: str) -> str:
        return f"{self.prefix}/{_safe('kind', kind)}/{_safe('subject', subject)}"

    def put(self, kind: str, subject: str, files: dict[str, bytes], *, identity: dict[str, str]) -> str:
        """Write a receipt and return its instance name. Never overwrites a stamped receipt."""
        run_id, attempt = identity["run_id"], identity["run_attempt"]
        if not (run_id.isdigit() and attempt.isdigit() and int(run_id) > 0 and int(attempt) > 0):
            raise ReceiptError(f"a receipt needs a GitHub run identity (run_id={run_id!r} attempt={attempt!r})")
        instance = f"{run_id}-{attempt}"
        base = f"{self._base(kind, subject)}/{instance}"
        if self.s3.get(f"{base}/{STAMP_NAME}") is not None:
            raise ReceiptError(f"receipt {kind}/{subject}/{instance} already exists; receipts are write-once")
        for name, data in files.items():
            if name == STAMP_NAME:
                raise ReceiptError(f"{STAMP_NAME} is reserved")
            self.s3.put(f"{base}/{_safe('file name', name)}", data)
        stamp = {
            "schema": STAMP_SCHEMA,
            "kind": kind,
            "subject": subject,
            "run_id": int(run_id),
            "run_attempt": int(attempt),
            "repository": identity.get("repository", ""),
            "workflow_ref": identity.get("workflow_ref", ""),
            "sha": identity.get("sha", ""),
            "files": sorted(files),
            "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        self.s3.put(f"{base}/{STAMP_NAME}", json.dumps(stamp, sort_keys=True).encode())
        return instance

    def instances(self, kind: str, subject: str) -> list[Receipt]:
        """Stamped receipts for the subject, newest first. A receipt with a malformed stamp is skipped."""
        base = self._base(kind, subject) + "/"
        _, prefixes = self.s3.list(base, "/")
        found: list[Receipt] = []
        for full in prefixes:
            instance = full[len(base) :].strip("/")
            if not _INSTANCE.match(instance):
                continue
            raw = self.s3.get(f"{full.rstrip('/')}/{STAMP_NAME}")
            if raw is None:
                continue
            try:
                stamp = json.loads(raw)
                if stamp.get("schema") != STAMP_SCHEMA or f"{stamp['run_id']}-{stamp['run_attempt']}" != instance:
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            found.append(self._receipt(kind, subject, instance, stamp))
        found.sort(key=lambda r: (str(r.stamp.get("written_at")), r.run_id, r.attempt), reverse=True)
        return found

    def _receipt(self, kind: str, subject: str, instance: str, stamp: dict[str, Any]) -> Receipt:
        base = f"{self._base(kind, subject)}/{instance}"

        def read(name: str) -> bytes:
            data = self.s3.get(f"{base}/{_safe('file name', name)}")
            if data is None:
                raise ReceiptError(f"receipt {kind}/{subject}/{instance} has no {name}")
            return data

        return Receipt(kind, subject, instance, stamp, read, tuple(stamp.get("files") or ()))


def store_from_environment(env: dict[str, str] | None = None) -> ReceiptStore:
    env = os.environ if env is None else env
    values = {name: env.get(_ENV + name, "") for name in ("ENDPOINT", "BUCKET", "PREFIX", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")}
    missing = [_ENV + name for name, value in values.items() if not value]
    if missing:
        raise ReceiptError(f"receipt store is not configured: set {', '.join(missing)}")
    s3 = S3(values["ENDPOINT"], values["BUCKET"], values["ACCESS_KEY_ID"], values["SECRET_ACCESS_KEY"], env.get(_ENV + "REGION") or "us-east-1")
    return ReceiptStore(s3, values["PREFIX"])


def identity_from_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ if env is None else env
    return {
        "run_id": env.get("GITHUB_RUN_ID", ""),
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT", ""),
        "repository": env.get("GITHUB_REPOSITORY", ""),
        "workflow_ref": env.get("GITHUB_WORKFLOW_REF", ""),
        "sha": env.get("GITHUB_SHA", ""),
    }


def select(receipts: list[Receipt], *, instance: str | None, max_attempt: int | None) -> Receipt:
    if instance is not None:
        chosen = [r for r in receipts if r.instance == instance]
    elif max_attempt is not None:
        chosen = sorted((r for r in receipts if r.attempt <= max_attempt), key=lambda r: r.attempt, reverse=True)
    else:
        chosen = receipts
    if not chosen:
        raise ReceiptError("no matching receipt")
    return chosen[0]


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    put = sub.add_parser("put")
    put.add_argument("kind")
    put.add_argument("subject")
    put.add_argument("files", nargs="+", type=Path)
    for name in ("get", "list"):
        p = sub.add_parser(name)
        p.add_argument("kind")
        p.add_argument("subject")
        if name == "get":
            p.add_argument("--file")
            p.add_argument("--dir", type=Path)
            p.add_argument("--instance")
            p.add_argument("--max-attempt", type=int)
    args = parser.parse_args(argv)
    try:
        store = store_from_environment()
        if args.command == "put":
            instance = store.put(args.kind, args.subject, {p.name: p.read_bytes() for p in args.files}, identity=identity_from_environment())
            print(instance)
        elif args.command == "list":
            print(json.dumps([r.stamp for r in store.instances(args.kind, args.subject)], indent=2))
        else:
            if bool(args.file) == bool(args.dir):
                parser.error("get needs exactly one of --file or --dir")
            receipt = select(store.instances(args.kind, args.subject), instance=args.instance, max_attempt=args.max_attempt)
            if args.file:
                sys.stdout.buffer.write(receipt.read(args.file))
            else:
                args.dir.mkdir(parents=True, exist_ok=True)
                for name in receipt.files:
                    (args.dir / name).write_bytes(receipt.read(name))
                print(receipt.instance)
    except ReceiptError as exc:
        print(f"ci_receipts: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
