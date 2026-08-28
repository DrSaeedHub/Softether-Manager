"""What version of the manager this is, and how two versions compare.

The number is not stored in the source. It comes from the release the code was
packaged into: CI writes a ``VERSION`` file next to the application tree from
``git describe --tags --exact-match``, and the installer ships that file with
everything else. A checkout that was never packaged has no such file, so the
version falls back to git, and then to ``0.0.0-dev``.

That fallback is why :func:`is_newer` refuses to compare a build that did not
come from a release: a development tree may well contain work no release has,
and telling its operator that a release is "newer" would send them backwards.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# app/version.py -> app -> <application root>
APP_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = APP_ROOT / "VERSION"

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)([-+].*)?$")


@lru_cache
def get_version() -> str:
    """The running build's stamp, resolved once."""
    env = os.getenv("SEM_VERSION", "").strip()
    if env:
        return env

    try:
        stamped = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        stamped = ""
    if stamped:
        return stamped

    described = _git_describe()
    if described:
        return described

    return "0.0.0-dev"


def _git_describe() -> str:
    """The exact tag on HEAD, or ``0.0.0-<short sha>`` when there is none.

    Mirrors what CI does, so a developer running from a checkout sees the same
    string the release job would have produced from that commit.
    """

    def git(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args],
                cwd=str(APP_ROOT),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    tag = git("describe", "--tags", "--exact-match")
    if tag:
        return tag
    sha = git("rev-parse", "--short", "HEAD")
    return f"0.0.0-{sha}" if sha else ""


@dataclass(frozen=True, order=False)
class Version:
    """A release tag broken into the parts that can be compared."""

    major: int
    minor: int
    patch: int
    #: Whatever followed the first ``-`` or ``+``; empty for a plain release tag.
    pre: str
    #: The string this was parsed from, with the leading ``v`` kept, so it can
    #: be shown back to an operator exactly as the release names it.
    raw: str

    @property
    def is_release(self) -> bool:
        """True for something the release job could have published.

        ``0.0.0-<sha>`` fails both halves of this: it has a prerelease suffix
        and it is all zeroes.
        """
        return not self.pre and (self.major > 0 or self.minor > 0 or self.patch > 0)

    def less_than(self, other: "Version") -> bool:
        if (self.major, self.minor, self.patch) != (other.major, other.minor, other.patch):
            return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)
        if self.pre == other.pre:
            return False
        # A release is newer than any prerelease carrying the same numbers.
        if not self.pre:
            return False
        if not other.pre:
            return True
        return self.pre < other.pre


def parse_version(text: str | None) -> Version | None:
    """Read ``vMAJOR.MINOR.PATCH`` with an optional suffix, or return None.

    Anything else is not a version this panel can reason about, and is reported
    as such rather than guessed at.
    """
    if not text:
        return None
    raw = text.strip()
    match = _VERSION_RE.match(raw)
    if not match:
        return None
    pre = (match.group(4) or "")[1:]
    return Version(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        pre=pre,
        raw=raw,
    )


def is_newer(current: str, candidate: str) -> bool:
    """Whether ``candidate`` is a release ``current`` should be offered.

    False whenever the question cannot be settled -- an unparseable tag, or a
    running build that did not come from a release. A wrong "yes" here nags an
    operator to install something that is not newer, or offers to replace a
    build they made themselves.
    """
    running = parse_version(current)
    if running is None or not running.is_release:
        return False
    target = parse_version(candidate)
    if target is None:
        return False
    return running.less_than(target)
