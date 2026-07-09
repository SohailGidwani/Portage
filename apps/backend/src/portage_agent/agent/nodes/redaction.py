"""Secret redaction (Phase 7, rev-C §2.3 — launch-gating).

Public repos still contain committed secrets. Two mechanisms, applied where repo content
leaves the sandbox boundary (prompts, reports, retry context):

  * **Path deny-list** — files that exist to hold credentials (.env*, *.pem, id_rsa*,
    credentials*) never enter prompt context or the repo listing at all.
  * **Pattern scrub** — known credential shapes (AWS keys, GitHub/Slack/OpenAI tokens,
    private-key blocks, generic `secret=...` assignments) are replaced with
    `[REDACTED:<kind>]` in context files, failing-test output, and report diffs.

Honest limitation (documented, not hidden): the file *being migrated* is sent to the LLM
with pattern-scrubbed content; if a secret literal is load-bearing for behaviour (a test
asserts on it), the migration of that file may fail — acceptable for a public-repo demo,
and strictly better than shipping the secret to the model. Redaction happens at the
context/report seams, not in the workspace: the repo on disk is never modified.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

# Files that exist to hold credentials — never shown to the model, never listed.
DENY_GLOBS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "id_ecdsa*",
    "credentials*", "*credentials.json", "service-account*.json",
    ".netrc", ".npmrc", ".pypirc", "htpasswd", "*.htpasswd",
)

# (kind, pattern) — ordered specific -> generic; scrub replaces with [REDACTED:<kind>].
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL)),
    ("aws-access-key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("url-credentials", re.compile(r"://[^/\s:@]{3,}:([^@/\s]{6,})@")),
    # generic assignment: password/secret/token/api_key = "…literal…" (>=8 chars, not a
    # placeholder). Group 2 (the literal) is what gets replaced.
    ("secret-assignment", re.compile(
        r"""(?ix)\b(password|passwd|secret|api_key|apikey|auth_token|access_token
            |secret_key|private_key|client_secret)\s*[:=]\s*
            (["'][^"']{8,}["'])""")),
)

_PLACEHOLDER_HINTS = ("example", "changeme", "placeholder", "your-", "xxxx", "dummy",
                      "<", "{", "$")


def is_denied_path(rel_path: str) -> bool:
    """True if this repo-relative path must never enter prompts/listings."""
    name = PurePosixPath(rel_path.replace("\\", "/")).name.lower()
    return any(fnmatch.fnmatch(name, g) for g in DENY_GLOBS)


def scrub(text: str) -> str:
    """Replace credential-shaped content with [REDACTED:<kind>] markers."""
    if not text:
        return text

    for kind, pattern in _PATTERNS:
        if kind == "secret-assignment":
            def _assign(m: re.Match[str], kind: str = kind) -> str:
                literal = m.group(2).strip("\"'").lower()
                if any(h in literal for h in _PLACEHOLDER_HINTS):
                    return m.group(0)  # obvious placeholder — leave it
                return m.group(0).replace(m.group(2), f'"[REDACTED:{kind}]"')

            text = pattern.sub(_assign, text)
        elif kind == "url-credentials":
            def _url(m: re.Match[str], kind: str = kind) -> str:
                return m.group(0).replace(m.group(1), f"[REDACTED:{kind}]")

            text = pattern.sub(_url, text)
        else:
            text = pattern.sub(f"[REDACTED:{kind}]", text)
    return text
