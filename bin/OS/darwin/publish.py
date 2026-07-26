#!/usr/bin/env python3
"""Publish the built disk image to the download host, and verify it arrived intact.

Uploads over FTPS (the certificate is checked against the system roots) and
regenerates the download page from download_page.html, filling in the size, the
checksum and the install steps that match how the image is actually signed: a
notarized build just opens, an ad-hoc one has to be approved in System Settings.

Credentials come from the macOS keychain, never from the command line:

    security find-internet-password -s <host> -a <user> -w

Usage:
    python3 bin/OS/darwin/publish.py                 # upload and verify
    python3 bin/OS/darwin/publish.py --dry-run       # render the page, upload nothing
    python3 bin/OS/darwin/publish.py --page-only     # refresh the page, keep the image
"""

from __future__ import annotations

import argparse
import datetime
import ftplib
import hashlib
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST_DIR = HERE / "_build" / "dist"
TEMPLATE = HERE / "download_page.html"

FTP_HOST = "v108329.kasserver.com"
FTP_USER = "v108329"
REMOTE_DIR = "/chess/mac"
PUBLIC_URL = "https://bolzano.uk/chess/mac"

VERSION = "R 6.0.4"

NOTARIZED_STEPS = """<ol>
  <li>Open the disk image and drag <strong>Lucas Chess R6</strong> onto the Applications folder.</li>
  <li>Open it from Applications. That is all: the app is signed and notarized by Apple, so
      there is no security warning to work around.</li>
</ol>"""

UNSIGNED_STEPS = """<ol>
  <li>Open the disk image and drag <strong>Lucas Chess R6</strong> onto the Applications folder.</li>
  <li>Open the app from Applications. macOS will refuse the first time, saying it
      <em>&ldquo;could not verify this app is free of malware&rdquo;</em>. Click
      <strong>Done</strong> &mdash; not &ldquo;Move to Bin&rdquo;.</li>
  <li>Go to <strong>System Settings &rsaquo; Privacy &amp; Security</strong> and scroll down to
      <strong>Security</strong>. Next to the message about Lucas Chess R6, click
      <strong>Open Anyway</strong>, authenticate, and confirm.</li>
</ol>
<p class="meta">
  Only the first launch needs this. On macOS 14 and earlier you can instead right-click the
  app and choose Open; Apple removed that shortcut in macOS 15.<br>
  From a terminal, the same thing in one step:
  <code>xattr -dr com.apple.quarantine &quot;/Applications/Lucas Chess R6.app&quot;</code>
</p>

<div class="note">
  That warning appears because the build is not notarized with a paid Apple Developer
  certificate, not because anything is wrong with it. Everything in the download is built
  from published source, and the checksum above lets you confirm you got the same file.
</div>"""


def log(msg: str) -> None:
    print(msg, flush=True)


def keychain_password(host: str, account: str) -> str:
    result = subprocess.run(
        ["security", "find-internet-password", "-s", host, "-a", account, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"no keychain entry for {account}@{host}.\n"
            f"Add one with: security add-internet-password -s {host} -a {account} -w '<password>'"
        )
    return result.stdout.strip()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_notarized(dmg: Path) -> bool:
    """True when the image carries a stapled notarization ticket."""
    result = subprocess.run(["xcrun", "stapler", "validate", str(dmg)], capture_output=True, text=True)
    return result.returncode == 0


def count_engines() -> int:
    engines = HERE / "Engines"
    return sum(1 for path in engines.iterdir() if path.is_dir()) if engines.is_dir() else 0


def render_page(dmg: Path, notarized: bool) -> str:
    template = TEMPLATE.read_text()
    return (
        template.replace("{{VERSION}}", VERSION)
        .replace("{{DMG_NAME}}", dmg.name)
        .replace("{{SIZE_MB}}", f"{dmg.stat().st_size / 1e6:.0f}")
        .replace("{{SHA256}}", sha256_of(dmg))
        .replace("{{INSTALL_STEPS}}", NOTARIZED_STEPS if notarized else UNSIGNED_STEPS)
        .replace("{{ENGINE_COUNT}}", str(count_engines()))
        .replace("{{BUILD_DATE}}", datetime.date.today().isoformat())
    )


def upload(local: Path, remote_name: str, password: str) -> None:
    context = ssl.create_default_context()  # verifies the server certificate
    with ftplib.FTP_TLS(context=context) as ftp:
        ftp.connect(FTP_HOST, timeout=120)
        ftp.login(FTP_USER, password)
        ftp.prot_p()
        ftp.cwd(REMOTE_DIR)
        with local.open("rb") as fh:
            ftp.storbinary(f"STOR {remote_name}", fh, blocksize=1 << 20)


def verify_remote(url: str, expected_sha: str, expected_size: int) -> bool:
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(url, timeout=600) as response:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    ok = digest.hexdigest() == expected_sha and size == expected_size
    log(f"   downloaded {size} bytes, sha256 {digest.hexdigest()[:16]}... {'matches' if ok else 'DOES NOT MATCH'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dmg", type=Path, help="disk image to publish (default: the one in _build/dist)")
    parser.add_argument("--dry-run", action="store_true", help="render the page and print it, upload nothing")
    parser.add_argument("--page-only", action="store_true", help="upload only the download page")
    args = parser.parse_args()

    dmg = args.dmg or next(iter(sorted(DIST_DIR.glob("*.dmg"))), None)
    if dmg is None or not dmg.is_file():
        raise SystemExit(f"no disk image found in {DIST_DIR} (run package_app.py first)")

    notarized = is_notarized(dmg)
    log(f":: {dmg.name}  {dmg.stat().st_size / 1e6:.0f} MB  {'notarized' if notarized else 'NOT notarized'}")
    page = render_page(dmg, notarized)

    if args.dry_run:
        log(page)
        return 0

    password = keychain_password(FTP_HOST, FTP_USER)

    if not args.page_only:
        log(f":: uploading {dmg.name} to {FTP_HOST}{REMOTE_DIR}")
        upload(dmg, dmg.name, password)

    log(":: uploading the download page")
    page_file = HERE / "_build" / "index.html"
    page_file.write_text(page)
    upload(page_file, "index.html", password)

    if not args.page_only:
        log(":: verifying what the server actually serves")
        if not verify_remote(f"{PUBLIC_URL}/{dmg.name}", sha256_of(dmg), dmg.stat().st_size):
            log("   the hosted copy does not match the local build")
            return 1

    log(f"\n:: published -> {PUBLIC_URL}/")
    if not notarized:
        log("   note: this image is not notarized, so the page tells users how to approve it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
