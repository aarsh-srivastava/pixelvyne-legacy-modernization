
import importlib.metadata as metadata
from pathlib import Path


def clean_license_text(text: str) -> str:
    """Remove trailing whitespace without changing licence wording."""
    return "\n".join(line.rstrip() for line in text.splitlines()).strip() + "\n"


print("# Third-Party Notices\n")
print("This file inventories Python distributions installed in the PixelVyne API image.")
print("Versions and licence files are collected from the built image.\n")
print("Generated from the installed environment; verify against the exact")
print("deployment image before redistribution.\n")
print(
    "This inventory excludes operating-system packages and dependencies "
    "in other service images.\n"
)

for dist in sorted(
    metadata.distributions(),
    key=lambda d: (d.metadata.get("Name") or "").lower()
):
    name = dist.metadata.get("Name", "Unknown")
    version = dist.version

    print(f"\n## {name} {version}\n")

    files = [
        f for f in (dist.files or [])
        if any(
            term in str(f).lower()
            for term in ("license", "copying", "notice")
        )
    ]

    if not files:
        print("No licence text file found in the installed distribution.")
        print("Verify the upstream licence and applicable notices.\n")
        continue

    for file in files:
        path = Path(dist.locate_file(file))
        print(f"### {file}\n")

        try:
            content = clean_license_text(
                path.read_text(encoding="utf-8", errors="replace")
            )
            print(content, end="")
        except OSError as error:
            print(f"Could not read packaged file: {error}")

        print("\n")
