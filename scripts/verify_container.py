"""Probe a built image for the properties tests/test_container_hygiene.py asserts
about the Dockerfile (M8 Task 1).

The tests parse the Dockerfile. That catches a regression at review time, which is
where you want it caught, but it proves nothing about the artefact that ships: a
`USER` line is not evidence that the process runs unprivileged, and an absent
`build-essential` is not evidence that no compiler is on the PATH.

This runs against the image. Give it two tags and it prints a before/after table.

    python scripts/verify_container.py enterprise-ai-api:before enterprise-ai-api:after
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# One shell, so one container start per image rather than a dozen.
PROBE = r"""
echo "uid=$(id -u)"
echo "user=$(id -un 2>/dev/null || echo '?')"
echo "compilers=$(command -v gcc cc g++ make 2>/dev/null | tr '\n' ',')"
echo "torch=$(python -c 'import torch;print(torch.__version__)' 2>/dev/null || echo none)"
SITE=$(python -c 'import site;print(site.getsitepackages()[0])' 2>/dev/null || echo /usr/local/lib/python3.12/site-packages)
echo "sitepkg_kb=$(du -sk "$SITE" 2>/dev/null | cut -f1)"
echo "app_entries=$(ls /app 2>/dev/null | tr '\n' ',')"
echo "has_tests=$(test -d /app/tests && echo yes || echo no)"
echo "has_eval_results=$(test -d /app/tests/eval/results && echo yes || echo no)"
echo "has_scripts=$(test -d /app/scripts && echo yes || echo no)"
echo "has_dotenv=$(test -f /app/.env && echo yes || echo no)"
echo "has_git=$(test -d /app/.git && echo yes || echo no)"
MODEL=no
if [ -n "${HF_HOME:-}" ] && [ -d "${HF_HOME}" ]; then
  if find "${HF_HOME}" -name '*.safetensors' 2>/dev/null | grep -q . ; then MODEL=yes; fi
  if find "${HF_HOME}" -name 'pytorch_model.bin' 2>/dev/null | grep -q . ; then MODEL=yes; fi
fi
echo "model_cached=$MODEL"
"""


def run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode != 0:
        raise SystemExit(
            f"{' '.join(command)} failed ({result.returncode}):\n{result.stderr.strip()}"
        )

    return result.stdout.strip()


def image_bytes(tag: str) -> int:
    """Compressed content size -- what a registry push or pull moves."""
    return int(run(["docker", "image", "inspect", tag, "--format", "{{.Size}}"]))


def image_disk(tag: str) -> str:
    """Uncompressed on-disk size -- what the server's disk has to hold.

    Docker 29's containerd image store reports these separately and they differ by
    about 4x on a Python base. Reporting one number without saying which it is
    invites exactly the sort of unfalsifiable claim this rebuild exists to remove.
    """
    for line in run(["docker", "images", tag, "--format", "{{.Size}}"]).splitlines():
        if line.strip():
            return line.strip()

    return "?"


def probe(tag: str) -> dict[str, Any]:
    raw = run(["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", PROBE])

    facts: dict[str, Any] = {}

    for line in raw.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            facts[key.strip()] = value.strip().strip(",")

    facts["content_bytes"] = image_bytes(tag)
    facts["content_gb"] = round(facts["content_bytes"] / 1e9, 2)
    facts["disk_size"] = image_disk(tag)
    facts["sitepkg_gb"] = round(int(facts.get("sitepkg_kb") or 0) * 1024 / 1e9, 2)
    facts["runs_as_root"] = facts.get("uid") == "0"
    facts["has_compiler"] = bool(facts.get("compilers"))

    return facts


ROWS = (
    ("image size (on disk)", lambda f: f["disk_size"]),
    ("image size (pushed)", lambda f: f"{f['content_gb']} GB"),
    ("site-packages", lambda f: f"{f['sitepkg_gb']} GB"),
    ("runs as", lambda f: f"uid {f.get('uid')} ({f.get('user')})"),
    ("root?", lambda f: "YES" if f["runs_as_root"] else "no"),
    ("compiler on PATH?", lambda f: f.get("compilers") or "no"),
    ("torch build", lambda f: f.get("torch")),
    ("/app contains", lambda f: f.get("app_entries") or "(empty)"),
    ("ships tests/", lambda f: f.get("has_tests")),
    ("ships eval results", lambda f: f.get("has_eval_results")),
    ("ships scripts/", lambda f: f.get("has_scripts")),
    ("ships .env", lambda f: f.get("has_dotenv")),
    ("ships .git", lambda f: f.get("has_git")),
    ("model baked in", lambda f: f.get("model_cached")),
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tags", nargs="+", help="one or two image tags")
    ap.add_argument("--report", type=Path,
                    default=PROJECT_ROOT / "reports" / "m8_container_audit.json")
    args = ap.parse_args()

    results = {tag: probe(tag) for tag in args.tags}

    width = max(len(label) for label, _ in ROWS) + 2
    columns = [max(len(tag), 24) for tag in args.tags]

    header = "".ljust(width) + "".join(
        tag.ljust(w + 2) for tag, w in zip(args.tags, columns)
    )
    print(header)
    print("-" * len(header))

    for label, getter in ROWS:
        cells = "".join(
            str(getter(results[tag])).ljust(w + 2) for tag, w in zip(args.tags, columns)
        )
        print(label.ljust(width) + cells)

    if len(args.tags) == 2:
        first, last = args.tags[0], args.tags[-1]
        before = results[first]["content_bytes"]
        after = results[last]["content_bytes"]

        print(
            f"\npushed  : {before / 1e9:.2f} GB -> {after / 1e9:.2f} GB "
            f"({(after - before) / 1e9:+.2f} GB, {100 * (after - before) / before:+.1f}%)"
        )
        print(f"on disk : {results[first]['disk_size']} -> {results[last]['disk_size']}")

    failures = [
        f"{tag}: {reason}"
        for tag, facts in results.items()
        for reason in (
            (["runs as root"] if facts["runs_as_root"] else [])
            + ([f"compiler present: {facts['compilers']}"] if facts["has_compiler"] else [])
            + (["ships .env"] if facts.get("has_dotenv") == "yes" else [])
            + (["ships eval results"] if facts.get("has_eval_results") == "yes" else [])
        )
    ]

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps({"images": results, "failures": failures}, indent=2), encoding="utf-8"
    )

    print(f"\nreport: {args.report}")

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f"  {failure}")

    # Only the last tag is the one being shipped; a failing "before" is the point.
    last_facts = results[args.tags[-1]]

    if (
        last_facts["runs_as_root"]
        or last_facts["has_compiler"]
        or last_facts.get("has_dotenv") == "yes"
        or last_facts.get("has_eval_results") == "yes"
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
