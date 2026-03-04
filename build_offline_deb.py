#!/usr/bin/env python3
"""
CopyU Offline Deb Package Builder

This script builds a deb package with pre-downloaded Python wheels
for offline installation of pynput and its dependencies.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# Configuration
PACKAGE_NAME = "copyu"
VERSION = "1.3.0"
ARCHITECTURES = {
    "amd64": "x86_64",
    "arm64": "aarch64",
    "mips64el": "mips64el",
}

# Wheels directory within the package
WHEELS_DIR = "deb_build/copyu/opt/copyu/wheels"


def run_command(cmd: list[str], cwd: str | None = None) -> str:
    """Run a shell command and return output."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        print(f"Error running command: {' '.join(cmd)}")
        print(f"stderr: {result.stderr}")
        sys.exit(1)
    return result.stdout


def download_wheels(platform: str, wheels_dir: str) -> None:
    """Download pynput and its dependencies as wheels."""
    print(f"Downloading wheels for platform: {platform}...")

    # Clean and recreate wheels directory
    if os.path.exists(wheels_dir):
        shutil.rmtree(wheels_dir)
    os.makedirs(wheels_dir, exist_ok=True)

    # Download pynput and dependencies
    # --only-binary=:all: ensures we only download wheels, not source distributions
    # --platform is used for cross-platform builds
    cmd = [
        sys.executable, "-m", "pip", "download",
        "pynput",
        "-d", wheels_dir,
        "--only-binary=:all:",
    ]

    # Add platform specification if not native
    if platform != "amd64":
        # Map deb arch to pip platform
        platform_map = {
            "arm64": "manylinux2014_aarch64",
            "mips64el": "linux_mips64el",
        }
        if platform in platform_map:
            cmd.extend(["--platform", platform_map[platform]])

    run_command(cmd)

    # List downloaded wheels
    wheels = os.listdir(wheels_dir)
    print(f"Downloaded {len(wheels)} wheel files:")
    for wheel in sorted(wheels):
        print(f"  - {wheel}")


def get_package_size(deb_dir: str) -> int:
    """Calculate installed package size in KB."""
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(deb_dir):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
    return total_size // 1024


def build_deb(platform: str, output_dir: str = ".") -> str:
    """Build the deb package."""
    deb_dir = "deb_build/copyu"
    deb_name = f"{PACKAGE_NAME}_{VERSION}_{platform}.deb"
    output_path = os.path.join(output_dir, deb_name)

    print(f"\nBuilding {deb_name}...")

    # Update control file with installed size
    installed_size = get_package_size(deb_dir)
    control_path = os.path.join(deb_dir, "DEBIAN/control")

    with open(control_path, "r") as f:
        control_content = f.read()

    # Add or update Installed-Size field
    if "Installed-Size:" in control_content:
        control_content = control_content.replace(
            f"Installed-Size: {control_content.split('Installed-Size: ')[1].split(chr(10))[0]}",
            f"Installed-Size: {installed_size}"
        )
    else:
        control_content = control_content.replace(
            "Architecture:",
            f"Installed-Size: {installed_size}\nArchitecture:"
        )

    # Update architecture
    control_content = control_content.replace(
        "Architecture: all",
        f"Architecture: {platform}"
    )

    with open(control_path, "w") as f:
        f.write(control_content)

    # Build the package
    print("Running dpkg-deb...")
    run_command(["dpkg-deb", "--build", deb_dir, output_path])

    print(f"Package built: {output_path}")

    # Get file size
    file_size = os.path.getsize(output_path) / (1024 * 1024)
    print(f"Package size: {file_size:.2f} MB")

    return output_path


def verify_package(deb_path: str) -> None:
    """Verify the built package."""
    print("\nVerifying package...")

    # Check package info
    info = run_command(["dpkg-deb", "-I", deb_path])
    print("Package info:")
    print(info)

    # List package contents
    contents = run_command(["dpkg-deb", "-c", deb_path])
    if "wheels" in contents:
        print("✓ Wheels directory found in package")
    else:
        print("✗ Wheels directory NOT found in package!")

    # Check for pynput wheel specifically
    if "pynput" in contents:
        print("✓ pynput wheel found in package")
    else:
        print("✗ pynput wheel NOT found in package!")


def main():
    parser = argparse.ArgumentParser(
        description="Build copyU deb package with offline dependencies"
    )
    parser.add_argument(
        "--platform",
        choices=list(ARCHITECTURES.keys()) + ["all"],
        default="amd64",
        help="Target platform architecture (default: amd64)"
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip downloading wheels (use existing ones)"
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Output directory for the deb package (default: current directory)"
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify the built package"
    )

    args = parser.parse_args()

    platforms = list(ARCHITECTURES.keys()) if args.platform == "all" else [args.platform]

    built_packages = []

    for platform in platforms:
        print(f"\n{'='*60}")
        print(f"Building for platform: {platform}")
        print(f"{'='*60}")

        if not args.skip_download:
            download_wheels(platform, WHEELS_DIR)
        else:
            print("Skipping wheel download (using existing wheels)")

        deb_path = build_deb(platform, args.output_dir)
        built_packages.append(deb_path)

        if args.verify:
            verify_package(deb_path)

    print(f"\n{'='*60}")
    print("Build complete!")
    print(f"{'='*60}")
    for pkg in built_packages:
        print(f"  - {pkg}")


if __name__ == "__main__":
    main()
