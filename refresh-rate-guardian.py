#!/usr/bin/env python3

import argparse
import logging
import re
import subprocess
import time

INTERVAL = 10
GDCTL = "/usr/bin/gdctl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger("RefreshRateGuardian")


def command(args):
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=20
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip()
            or f"Command failed: {args}"
        )
    return result.stdout


def parse_monitor_output(output, include_modes=False):
    monitors = {}
    lines = output.splitlines()

    for index, line in enumerate(lines):
        header = re.search(
            r'[├└]──Monitor\s+(\S+)\s+\(', line
        )
        if not header:
            continue

        connector = header.group(1)
        end = len(lines)

        for j in range(index + 1, len(lines)):
            if (
                re.search(r'[├└]──Monitor\s+\S+\s+\(', lines[j])
                or "Logical monitors:" in lines[j]
            ):
                end = j
                break

        block = "\n".join(lines[index:end])
        info = {"modes": [], "current": None}

        current = re.search(
            r'Current mode\s*\n[^\n]*[├└]──(\d+x\d+@[0-9.]+)',
            block
        )

        if current:
            info["current"] = current.group(1)

        if include_modes:
            mode_section = re.search(
                r'Modes \(\d+\)(.*?)(?:Preferences:|$)',
                block,
                re.S
            )
            if mode_section:
                info["modes"] = re.findall(
                    r'(\d+x\d+@[0-9.]+)',
                    mode_section.group(1)
                )

        monitors[connector] = info

    return monitors


def parse_logical_monitors(output):
    lines = output.splitlines()
    groups = []

    for index, line in enumerate(lines):
        if "Logical monitor #" not in line:
            continue

        end = len(lines)

        for j in range(index + 1, len(lines)):
            if "Logical monitor #" in lines[j]:
                end = j
                break

        block = "\n".join(lines[index:end])

        position = re.search(
            r'Position:\s*\((-?\d+),\s*(-?\d+)\)', block
        )
        scale = re.search(r'Scale:\s*([0-9.]+)', block)
        transform = re.search(r'Transform:\s*(\S+)', block)
        primary = re.search(r'Primary:\s*(yes|no)', block)

        connectors = re.findall(
            r'[├└]──([A-Za-z0-9_.-]+)\s+\(', block
        )

        if not position or not scale or not transform or not connectors:
            raise RuntimeError(
                "Could not safely parse a logical monitor configuration."
            )

        groups.append({
            "x": position.group(1),
            "y": position.group(2),
            "scale": scale.group(1),
            "transform": transform.group(1),
            "primary": primary and primary.group(1) == "yes",
            "connectors": connectors
        })

    return groups


def mode_details(mode):
    match = re.fullmatch(
        r'(\d+)x(\d+)@([0-9.]+)', mode
    )
    if not match:
        raise ValueError(f"Unrecognised monitor mode: {mode}")

    return (
        int(match.group(1)),
        int(match.group(2)),
        float(match.group(3))
    )


def inspect_configuration():
    current_output = command([GDCTL, "show"])
    modes_output = command([GDCTL, "show", "--modes"])

    current = parse_monitor_output(current_output)
    available = parse_monitor_output(modes_output, True)
    groups = parse_logical_monitors(current_output)

    if not groups:
        raise RuntimeError("No active logical monitors were found.")

    changes = {}
    needs_change = False

    for group in groups:
        for connector in group["connectors"]:
            if connector not in current or connector not in available:
                raise RuntimeError(
                    f"Missing monitor information for {connector}."
                )

            active = current[connector]["current"]
            modes = available[connector]["modes"]

            if not active or not modes:
                raise RuntimeError(
                    f"Cannot safely determine modes for {connector}."
                )

            width, height, current_rate = mode_details(active)

            matching = [
                mode for mode in modes
                if mode_details(mode)[:2] == (width, height)
            ]

            if not matching:
                raise RuntimeError(
                    f"No matching resolution modes for {connector}."
                )

            best = max(
                matching,
                key=lambda mode: mode_details(mode)[2]
            )

            best_rate = mode_details(best)[2]

            changes[connector] = {
                "current": active,
                "target": best,
                "changed": best_rate > current_rate + 0.05
            }

            if changes[connector]["changed"]:
                needs_change = True

    if not needs_change:
        return None, changes

    args = [
        GDCTL, "set",
        "--layout-mode", "logical"
    ]

    for group in groups:
        args.append("--logical-monitor")

        for connector in group["connectors"]:
            args.extend([
                "--monitor", connector,
                "--mode", changes[connector]["target"]
            ])

        if group["primary"]:
            args.append("--primary")

        args.extend([
            "--scale", group["scale"],
            "--transform", group["transform"],
            "--x", group["x"],
            "--y", group["y"]
        ])

    return args, changes


def run_once(dry_run=False):
    args, changes = inspect_configuration()

    for connector, info in changes.items():
        log.info(
            "%s: %s -> %s%s",
            connector,
            info["current"],
            info["target"],
            " [MAXIMUM]" if not info["changed"] else ""
        )

    if args is None:
        log.info("All active monitors are already at maximum refresh rate.")
        return

    verify_args = args + ["--verify"]

    log.info("Validating proposed configuration...")
    command(verify_args)

    if dry_run:
        log.info("Dry run successful. No display settings were changed.")
        return

    log.info("Applying persistent maximum refresh-rate configuration...")
    output = command(args + ["--persistent"])

    if output.strip():
        log.info(output.strip())

    log.info("Configuration applied successfully.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args()

    if options.once:
        run_once(options.dry_run)
        return

    log.info("RefreshRate Guardian started.")

    while True:
        try:
            run_once()
        except Exception as error:
            log.error("Check failed: %s", error)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
