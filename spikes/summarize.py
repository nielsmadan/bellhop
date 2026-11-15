import json
import sys
from collections import defaultdict
from pathlib import Path

ORDER = ("S1", "S2-boundary", "S2-interrupt", "S3", "S3b", "S7", "S10")


def cell(rows: list[dict]) -> str:
    counted = [r for r in rows if r["pass"] is not None]
    if not counted and all(r.get("not_measured") for r in rows):
        return "not measured"
    if not counted:
        reasons = {r.get("na") or r.get("error") or "n/a" for r in rows}
        return "n/a" if all(r.get("na") for r in rows) else "error: " + "; ".join(sorted(reasons))
    good = sum(1 for r in counted if r["pass"])
    timing = sorted({r["timing"] for r in counted if r.get("timing")})
    suffix = f" ({', '.join(timing)})" if timing else ""
    errors = len(rows) - len(counted)
    extra = f" +{errors} err" if errors else ""
    return f"{good}/{len(counted)}{suffix}{extra}"


def main() -> int:
    rows = [json.loads(line) for path in sys.argv[1:] for line in Path(path).read_text().splitlines() if line.strip()]
    latest = {(r["harness"], r["candidate"], r["rep"], r["scenario"]): r for r in rows}
    grouped: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in latest.values():
        grouped[(row["harness"], row["candidate"])][row["scenario"]].append(row)
    print("| Harness | Candidate | " + " | ".join(ORDER) + " |")
    print("|---|---|" + "---|" * len(ORDER))
    for (harness, candidate), scenarios in sorted(grouped.items()):
        cells = [cell(scenarios[s]) if s in scenarios else "-" for s in ORDER]
        print(f"| {harness} | {candidate} | " + " | ".join(cells) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
