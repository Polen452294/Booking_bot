"""Stop completed CI fixtures without removing their data or diagnostics."""

import argparse
from pathlib import Path

from booking_bot.deployment.manager import DeploymentManager


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    manager = DeploymentManager(args.root)
    for state in manager.list():
        manager.compose(state["slug"], "stop", "api", "worker", "postgres", "redis")
        print(f"Stopped completed fixture: {state['slug']}")


if __name__ == "__main__":
    main()
