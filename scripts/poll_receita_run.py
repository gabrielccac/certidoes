"""Poll a GitHub Actions run and copy its generated Receita PDF locally."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def gh_json(*args: str) -> dict:
    result = subprocess.run(("gh", *args), check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="OWNER/REPOSITORY")
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--output-dir", type=Path, default=Path("runs/receita"))
    parser.add_argument("--poll-interval", type=float, default=10)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout
    while True:
        run = gh_json("run", "view", str(args.run_id), "--repo", args.repo,
                      "--json", "status,conclusion")
        if run["status"] == "completed":
            if run["conclusion"] != "success":
                raise SystemExit(f"GitHub run {args.run_id} finished with {run['conclusion']}")
            break
        if time.monotonic() >= deadline:
            raise SystemExit(f"Timed out waiting for GitHub run {args.run_id}")
        time.sleep(args.poll_interval)

    with tempfile.TemporaryDirectory(prefix=f"receita-{args.run_id}-") as temp_dir:
        subprocess.run(("gh", "run", "download", str(args.run_id), "--repo", args.repo,
                        "--dir", temp_dir), check=True)
        pdfs = list(Path(temp_dir).rglob("*.pdf"))
        if len(pdfs) != 1:
            raise SystemExit(f"Expected one PDF artifact, found {len(pdfs)}")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        destination = args.output_dir / pdfs[0].name
        shutil.copy2(pdfs[0], destination)

    print(json.dumps({"status": "success", "run_id": args.run_id,
                      "pdf_path": str(destination), "bytes": destination.stat().st_size}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
