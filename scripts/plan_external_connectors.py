"""Export source discovery work even when a company has no verified website."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from norway_company_agent.external_tasks import plan_external_tasks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    profiles = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line]
    tasks = [task for profile in profiles for task in plan_external_tasks(profile)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(task, ensure_ascii=False) + "\n" for task in tasks), encoding="utf-8")
    report = {"companies": len(profiles), "tasks": len(tasks), "by_connector": dict(Counter(task["connector"] for task in tasks)),
              "boundary": "Discovery work queue only. These tasks are not external evidence or completed connectors and earn no coverage points."}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
