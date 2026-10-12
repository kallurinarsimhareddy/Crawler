"""Refuse to ship secrets: scan a staged package folder before it is zipped.

    python scan_package.py <staged folder> [--real-values-from GLOB ...]

Fails (exit 1) on: any .env file that is not an *.example, database dumps / manifests, private
keys or certificates, credential-looking strings, and — with --real-values-from — any actual
secret VALUE (PASSWORD / SECRET / TOKEN / KEY / DATABASE_URL / REDIS_URL variables) taken from
the given local env files. Values are compared in memory and never printed.
"""

from __future__ import annotations

import argparse
import glob
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

FORBIDDEN_NAMES = [
    (re.compile(r"(^|[\\/])\.env($|\.(?!example$)[^\\/]*$)"), "an .env file"),
    (re.compile(r"\.(dump|backup|pem|key|p12|pfx)$", re.I), "a dump, key or certificate"),
    (re.compile(r"\.manifest\.json$", re.I), "a backup manifest"),
    (re.compile(r"(^|[\\/])(sanagtm\.env|tunnel\.token|postgres\.superuser|answers\.env)$", re.I), "an installed secret file"),
    (re.compile(r"(client_secret|service_account)[^\\/]*\.json$", re.I), "a Google credential"),
]
PATTERNS = {
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "anthropic key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "openai-style key": r"\bsk-[A-Za-z0-9]{32,}",
    "google api key": r"AIza[0-9A-Za-z_-]{30,}",
    "supabase secret key": r"sb_secret_[A-Za-z0-9]{20,}",
    "jwt": r"eyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}",
    "database url with password": r"postgres(?:ql)?://[A-Za-z0-9_.-]+:[^@\s\"'<>{}$]{12,}@[A-Za-z0-9.-]+",
    "cloudflare tunnel token": r"\beyJhIjoi[A-Za-z0-9_-]{40,}",
    "aws key": r"AKIA[0-9A-Z]{16}",
}
#: Known fake values that tests use on purpose.
ALLOWED = ("not-a-real-", "fake", "example", "0123456789abcdef")
TEXT_SUFFIXES = {".py", ".ps1", ".psm1", ".cmd", ".bat", ".json", ".md", ".txt", ".toml", ".yaml", ".yml", ".sql",
                 ".sh", ".js", ".css", ".html", ".xml", ".template", ".example", ".cfg", ".ini", ".csv", ""}


def real_values(patterns):
    values = set()
    for pattern in patterns:
        for path in glob.glob(pattern):
            if path.endswith(".example"):
                continue
            for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    value = value.strip().strip('"').strip("'")
                    if len(value) < 12 or not re.search(r"PASSWORD|SECRET|TOKEN|KEY|DATABASE_URL|REDIS_URL", key.upper()):
                        continue
                    if key.upper().endswith("_URL") and not urlsplit(value).password:
                        continue  # a connection URL without a password (e.g. redis://127.0.0.1:6390) is not a secret
                    values.add(value)
    return values


def scan(root: Path, values) -> list:
    problems = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        for rx, what in FORBIDDEN_NAMES:
            if rx.search(rel):
                problems.append(f"{rel}: {what}")
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 20 * 2**20:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name, rx in PATTERNS.items():
            for match in re.finditer(rx, text):
                if not any(a in match.group(0) for a in ALLOWED):
                    problems.append(f"{rel}:{text[:match.start()].count(chr(10)) + 1}: looks like a {name}")
        for value in values:
            if value in text:
                problems.append(f"{rel}: contains a real secret value from a local env file")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--real-values-from", nargs="*", default=[])
    args = parser.parse_args()
    values = real_values(args.real_values_from)
    problems = scan(args.folder, values)
    files = sum(1 for p in args.folder.rglob("*") if p.is_file())
    print(f"scanned {files} files; compared against {len(values)} real secret values; problems: {len(problems)}")
    for p in problems:
        print("  -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
