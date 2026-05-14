"""Auto-updater via Git.

Update lifecycle
----------------
1. Fetch remote info (no checkout, read-only network call).
2. Determine whether a newer version exists:
   - ``release`` mode: compare latest semver tag on remote vs. current HEAD tag.
   - ``master``  mode: compare remote master commit hash vs. local HEAD.
3. If an update is available:
   a. Pull/checkout the appropriate ref.
   b. Run ``uv sync --no-dev`` to update the venv.
   c. Send ``SIGTERM`` to the own process so systemd can restart with new code.

Rate-limiting
-------------
Exactly one check per calendar day (UTC).  The date is stored in-memory;
no disk writes required.

Usage
-----
Create one ``AutoUpdater`` instance at startup and call
``await updater.check_and_update()`` at the appropriate trigger points.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import subprocess
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class UpdateMode(str, Enum):
    """Auto-update policy."""

    OFF = "off"
    """No automatic updates."""

    RELEASE = "release"
    """Update only when a new (semver) release tag is available."""

    MASTER = "master"
    """Update whenever the remote master branch has new commits."""


# ── Helpers ───────────────────────────────────────────────────────────────────

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[.\-].*)?$")


def _parse_semver(tag: str) -> Optional[tuple[int, int, int]]:
    """Return (major, minor, patch) tuple or ``None`` if not a semver tag."""
    m = _SEMVER_RE.match(tag)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


# ── AutoUpdater ───────────────────────────────────────────────────────────────


class AutoUpdater:
    """Handles fetch, comparison, pull, dep-sync and graceful restart.

    Parameters
    ----------
    mode:
        Update policy (off / release / master).
    repo_path:
        Root of the git repository.  Defaults to the current working directory.
    branch:
        Remote branch to track in ``master`` mode.  Defaults to ``"master"``.
    remote:
        Name of the git remote.  Defaults to ``"origin"``.
    uv_executable:
        Path to the ``uv`` binary used for dependency sync.
        ``None`` → auto-detect from PATH.
    """

    def __init__(
        self,
        mode: UpdateMode,
        *,
        repo_path: Optional[Path] = None,
        branch: str = "master",
        remote: str = "origin",
        uv_executable: Optional[str] = None,
    ) -> None:
        self.mode = mode
        self._branch = branch
        self._remote = remote
        self._uv = uv_executable or "uv"
        self._repo_path = repo_path or Path.cwd()
        self._last_check_date: Optional[date] = None
        self._repo = None  # lazy import

    # ── Public API ────────────────────────────────────────────────────────────

    async def check_and_update(self) -> bool:
        """Check for an available update and apply it if found.

        Returns ``True`` if an update was applied and a restart was requested.
        The method is safe to call repeatedly; it only performs a real check
        once per calendar day (UTC).
        """
        if self.mode is UpdateMode.OFF:
            return False

        today = date.today()
        if self._last_check_date == today:
            logger.debug("updater: already checked today (%s), skipping", today)
            return False
        self._last_check_date = today

        try:
            repo = self._get_repo()
        except Exception as exc:  # noqa: BLE001
            logger.warning("updater: cannot open git repo at %s: %s", self._repo_path, exc)
            return False

        # Fetch runs in a thread-pool to avoid blocking the event loop.
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._do_fetch, repo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("updater: fetch failed: %s", exc)
            return False

        if not self._is_on_expected_branch(repo):
            return False

        has_update, ref = self._detect_update(repo)
        if not has_update:
            return False

        logger.info("updater: update available → ref=%s, applying…", ref)
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._apply_update, repo, ref)
        except Exception as exc:  # noqa: BLE001
            logger.error("updater: failed to apply update (ref=%s): %s", ref, exc)
            return False

        # Sync venv dependencies in executor so we don't block the event loop.
        dep_ok = await asyncio.get_event_loop().run_in_executor(None, self._sync_dependencies)
        if not dep_ok:
            logger.warning("updater: dependency sync failed – restarting anyway")

        logger.info("updater: sending SIGTERM to self (PID %d) for systemd restart", os.getpid())
        self._request_restart()
        return True

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_repo(self):
        """Return (and cache) the gitpython Repo object."""
        if self._repo is None:
            from git import Repo  # noqa: PLC0415  – lazy import

            self._repo = Repo(self._repo_path, search_parent_directories=True)
        return self._repo

    def _do_fetch(self, repo) -> None:
        """Fetch remote tags + branch info (blocking, run in executor)."""
        remote = repo.remotes[self._remote]
        remote.fetch(tags=True)

    def _is_on_expected_branch(self, repo) -> bool:
        """Return True if it is safe to apply an update.

        In ``master`` mode the local branch must match ``self._branch``.
        In ``release`` mode the HEAD must either be on ``self._branch`` or
        in a detached state (i.e. already sitting on a release tag).
        Any other branch indicates the repo has been manually switched to a
        development or feature branch – do not touch it.
        """
        try:
            active = repo.active_branch.name
        except TypeError:
            # Detached HEAD – acceptable for release mode, not for master.
            if self.mode is UpdateMode.MASTER:
                logger.warning(
                    "updater: HEAD is detached, expected branch '%s' – skipping update",
                    self._branch,
                )
                return False
            return True  # detached HEAD on a tag is fine for release mode

        if active != self._branch:
            logger.warning(
                "updater: current branch '%s' does not match update branch '%s' – skipping update",
                active,
                self._branch,
            )
            return False
        return True

    def _detect_update(self, repo) -> tuple[bool, str]:
        """Detect whether an update is available.

        Returns ``(has_update, ref)`` where *ref* is the tag name or branch
        ref string to check out / pull.
        """
        if self.mode is UpdateMode.RELEASE:
            return self._check_new_release(repo)
        if self.mode is UpdateMode.MASTER:
            return self._check_master_update(repo)
        return False, ""

    def _check_new_release(self, repo) -> tuple[bool, str]:
        """Compare the latest semver tag on remote with the current HEAD tag."""
        # Collect all reachable tags with valid semver names
        all_tags = [t for t in repo.tags if _parse_semver(t.name)]
        if not all_tags:
            logger.debug("updater: no semver tags found in repo")
            return False, ""

        latest_tag = max(all_tags, key=lambda t: _parse_semver(t.name) or (0, 0, 0))

        # Determine what tag (if any) the current HEAD is on
        try:
            current_tag_name = next(t.name for t in repo.tags if t.commit == repo.head.commit)
        except StopIteration:
            current_tag_name = None

        if current_tag_name is None:
            logger.info(
                "updater: HEAD is not on any release tag; latest tag is %s → will update",
                latest_tag.name,
            )
            return True, latest_tag.name

        current_ver = _parse_semver(current_tag_name)
        latest_ver = _parse_semver(latest_tag.name)

        if latest_ver is not None and current_ver is not None and latest_ver > current_ver:
            logger.info("updater: new release found: %s > %s", latest_tag.name, current_tag_name)
            return True, latest_tag.name

        logger.debug("updater: already on latest release %s", current_tag_name or "(none)")
        return False, ""

    def _check_master_update(self, repo) -> tuple[bool, str]:
        """Compare remote master HEAD with local HEAD."""
        try:
            remote_ref = repo.remotes[self._remote].refs[self._branch]
        except (IndexError, AttributeError):
            logger.warning(
                "updater: remote ref %s/%s not found after fetch",
                self._remote,
                self._branch,
            )
            return False, ""

        remote_sha = remote_ref.commit.hexsha
        local_sha = repo.head.commit.hexsha

        if remote_sha != local_sha:
            logger.info(
                "updater: remote %s/%s is ahead (%s vs local %s)",
                self._remote,
                self._branch,
                remote_sha[:8],
                local_sha[:8],
            )
            return True, f"{self._remote}/{self._branch}"

        logger.debug("updater: already up-to-date with %s/%s", self._remote, self._branch)
        return False, ""

    def _apply_update(self, repo, ref: str) -> None:
        """Checkout tag or pull branch (blocking, run in executor)."""
        if self.mode is UpdateMode.RELEASE:
            logger.info("updater: checking out tag %s", ref)
            repo.git.checkout(ref)
        elif self.mode is UpdateMode.MASTER:
            logger.info("updater: pulling %s/%s", self._remote, self._branch)
            repo.remotes[self._remote].pull(self._branch)

    def _sync_dependencies(self) -> bool:
        """Run ``uv sync --no-dev`` to update the virtual environment."""
        try:
            env = os.environ.copy()
            # zeropythia has no home dir; ensure uv writes its cache to the
            # persistent cache directory created by install.sh, not ~/.cache/uv.
            env.setdefault("UV_CACHE_DIR", str(Path(self._repo_path) / ".uv-cache"))
            result = subprocess.run(  # noqa: S603
                [self._uv, "sync", "--no-dev"],
                cwd=self._repo_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if result.returncode == 0:
                logger.info("updater: dependency sync OK")
                return True
            logger.error(
                "updater: uv sync failed (rc=%d): %s", result.returncode, result.stderr.strip()
            )
            return False
        except FileNotFoundError:
            logger.warning("updater: uv not found at %r – skipping dep sync", self._uv)
            return False
        except subprocess.TimeoutExpired:
            logger.error("updater: uv sync timed out")
            return False

    @staticmethod
    def _request_restart() -> None:
        """Send SIGTERM to the own process so systemd triggers a restart."""
        os.kill(os.getpid(), signal.SIGTERM)
