"""Structural stand-in for the openspec CLI, used by fake mode and tests.

    python -m foreman.fake_openspec validate <change> --strict
    python -m foreman.fake_openspec archive <change> --yes
"""
import re
import shutil
import sys
from datetime import date
from pathlib import Path

REQUIRED = ["proposal.md", "design.md", "tasks.md"]
REQUIREMENT = re.compile(r"^### Requirement:\s*(.+?)\s*$", re.M)
OPERATION = re.compile(r"^## (ADDED|MODIFIED|REMOVED|RENAMED) Requirements\s*$", re.M)


def requirement_blocks(body: str) -> dict[str, str]:
    matches = list(REQUIREMENT.finditer(body))
    blocks = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        blocks[m.group(1)] = body[m.start():end].rstrip() + "\n"
    return blocks


def parse_delta(text: str) -> dict[str, dict[str, str]]:
    parts = OPERATION.split(text)
    return {op: requirement_blocks(body) for op, body in zip(parts[1::2], parts[2::2])}


def delta_files(change_dir: Path) -> list[Path]:
    return sorted((change_dir / "specs").glob("*/spec.md"))


def validate(root: Path, change: str) -> list[str]:
    d = root / "openspec/changes" / change
    if not d.is_dir():
        return [f"change '{change}' not found"]
    errors = [f"{name} is missing" for name in REQUIRED if not (d / name).is_file()]
    deltas = delta_files(d)
    if not deltas:
        errors.append("no spec deltas under specs/<capability>/spec.md")
    for f in deltas:
        rel = f.relative_to(d)
        ops = parse_delta(f.read_text(encoding="utf-8"))
        if not ops:
            errors.append(f"{rel}: no ADDED/MODIFIED/REMOVED Requirements section")
        for op, blocks in ops.items():
            if op == "REMOVED":
                continue
            for name, block in blocks.items():
                if not re.search(r"\b(SHALL|MUST)\b", block):
                    errors.append(f"{rel}: requirement '{name}' needs SHALL or MUST")
                if "#### Scenario:" not in block:
                    errors.append(f"{rel}: requirement '{name}' needs at least one '#### Scenario:'")
    return errors


def archive(root: Path, change: str) -> None:
    d = root / "openspec/changes" / change
    for f in delta_files(d):
        capability = f.parent.name
        spec = root / "openspec/specs" / capability / "spec.md"
        current = requirement_blocks(spec.read_text(encoding="utf-8")) if spec.exists() else {}
        for op, blocks in parse_delta(f.read_text(encoding="utf-8")).items():
            if op in ("ADDED", "MODIFIED"):
                current.update(blocks)
            elif op == "REMOVED":
                for name in blocks:
                    current.pop(name, None)
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(f"# {capability} Specification\n\n## Requirements\n" + "\n".join(current.values()), encoding="utf-8", newline="\n")
    target = root / "openspec/changes/archive"
    target.mkdir(parents=True, exist_ok=True)
    shutil.move(str(d), str(target / f"{date.today().isoformat()}-{change}"))


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("validate", "archive"):
        print("usage: fake_openspec validate|archive <change> [--strict|--yes]")
        return 2
    command, change, root = argv[0], argv[1], Path.cwd()
    errors = validate(root, change)
    if command == "validate":
        print("\n".join(errors) if errors else f"change '{change}' is valid")
        return 1 if errors else 0
    if not (root / "openspec/changes" / change).is_dir():
        print(errors[0])
        return 1
    archive(root, change)
    print(f"archived '{change}'")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
