#!/usr/bin/env python3
"""Generate markdown remediation task specs from CodeQL batch files."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, List


def parse_args() -> argparse.Namespace:
    today = dt.date.today().isoformat()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batches-dir",
        default=f".codeql/batches/{today}",
        help="Directory containing batch-XX.json files",
    )
    parser.add_argument(
        "--outdir",
        default=f".codeql/task-specs/{today}",
        help="Directory for generated markdown specs",
    )
    parser.add_argument(
        "--batch",
        default=None,
        help="Optional batch file name (for example: batch-01.json). Generates all if omitted.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def list_batch_files(batches_dir: Path, only_batch: str | None) -> List[Path]:
    if only_batch:
        batch_path = batches_dir / only_batch
        if not batch_path.exists():
            raise RuntimeError(f"Batch file not found: {batch_path}")
        return [batch_path]
    return sorted(batches_dir.glob("batch-*.json"))


def alert_line(alert: Dict[str, Any]) -> str:
    number = alert.get("alert_number")
    rule = alert.get("rule_id")
    severity = alert.get("severity")
    file_path = alert.get("file_path") or "(location unavailable)"
    start_line = alert.get("start_line")
    end_line = alert.get("end_line")
    line_range = f"L{start_line}" if start_line else "line unknown"
    if end_line and end_line != start_line:
        line_range += f"-L{end_line}"
    return f"- #{number} | `{rule}` | `{severity}` | `{file_path}` ({line_range})"


def render_spec(batch_name: str, payload: Dict[str, Any]) -> str:
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise RuntimeError(f"Invalid batch payload for {batch_name}: missing alerts list")

    batch_id = batch_name.replace(".json", "")
    branch_name = f"codeql-fix-{batch_id}"
    commit_message = f"fix(codeql): remediate {batch_id} alerts"

    lines = [
        f"# CodeQL Remediation Task Spec: {batch_id}",
        "",
        "## Alerts in scope",
        *[alert_line(a) for a in alerts if isinstance(a, dict)],
        "",
        "## Strict implementation rules",
        "- Apply minimal, targeted changes only for alerts in this batch.",
        "- Do not use blanket suppressions (`# noqa`, disabling rules, or mass ignores).",
        "- Add tests only if the change introduces logic that needs coverage.",
        "- Preserve existing formatting/style and avoid unrelated refactors.",
        "",
        "## Verification",
        "1. Run unit tests.",
        "2. Run lint/static checks used by this repository.",
        "3. Confirm CodeQL no longer reports these alert numbers after CI completes.",
        "",
        "## PR instructions",
        f"- Branch name: `{branch_name}`",
        f"- Commit message: `{commit_message}`",
        "- PR title: `CodeQL remediation: {batch_id}`",
        "- PR body template:",
        "",
        "```markdown",
        f"## Summary\n- Remediated alerts from `{batch_id}`.\n- Scope limited to listed files/locations.",
        "",
        "## Validation",
        "- [ ] Unit tests pass",
        "- [ ] Lint/static checks pass",
        "- [ ] CodeQL scan no longer flags the batch alerts",
        "```",
        "",
        "## Source batch file",
        f"- `{batch_name}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    batches_dir = Path(args.batches_dir)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    batch_files = list_batch_files(batches_dir, args.batch)
    if not batch_files:
        raise RuntimeError(f"No batch-*.json files found in {batches_dir}")

    created = 0
    for batch_file in batch_files:
        payload = load_json(batch_file)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Invalid JSON object in {batch_file}")
        md_name = batch_file.name.replace(".json", ".md")
        (outdir / md_name).write_text(render_spec(batch_file.name, payload), encoding="utf-8")
        created += 1

    print(f"Task spec generation complete: {created} markdown spec(s) written to {outdir}.")


if __name__ == "__main__":
    main()
