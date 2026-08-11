"""
Checkpoint Manager — Transparent filesystem snapshots via shadow git repos.

Creates automatic snapshots of working directories before file-mutating
operations (write_file, patch), triggered once per conversation turn.
Provides rollback to any previous checkpoint.

This is NOT a tool — the LLM never sees it.  It's transparent infrastructure
controlled by the ``checkpoints`` config flag or ``--checkpoints`` CLI flag.

Architecture:
    ~/.leanflow/checkpoints/{sha256(abs_dir)[:16]}/   — shadow git repo
        HEAD, refs/, objects/                        — standard git internals
        LEANFLOW_WORKDIR                               — original dir path
        info/exclude                                 — default excludes

The shadow repo uses GIT_DIR + GIT_WORK_TREE so no git state leaks
into the user's project directory.
"""

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

from core.home import leanflow_home

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHECKPOINT_BASE = leanflow_home() / "checkpoints"

DEFAULT_EXCLUDES = [
    "node_modules/",
    "dist/",
    "build/",
    ".env",
    ".env.*",
    ".env.local",
    ".env.*.local",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "*.log",
    ".cache/",
    ".next/",
    ".nuxt/",
    "coverage/",
    ".pytest_cache/",
    ".venv/",
    "venv/",
    ".git/",
    # LeanFlow's source checkpoints protect user edits. Runtime telemetry,
    # worker artifacts, downloads, and cloned research corpora have their own
    # persistence lifecycles and can grow by gigabytes during one campaign.
    ".leanflow/workflow-state/activity/",
    ".leanflow/workflow-state/activity.jsonl",
    ".leanflow/workflow-state/outcomes.jsonl",
    ".leanflow/workflow-state/dispatch-jobs/",
    ".leanflow/downloads/",
    ".leanflow/workspace/",
    ".leanflow/cache/",
]

_RUNTIME_EXCLUDE_PATHS = [
    ".leanflow/workflow-state/activity",
    ".leanflow/workflow-state/activity.jsonl",
    ".leanflow/workflow-state/outcomes.jsonl",
    ".leanflow/workflow-state/dispatch-jobs",
    ".leanflow/downloads",
    ".leanflow/workspace",
    ".leanflow/cache",
]
_EXCLUDES_VERSION = "2"
_EXCLUDES_VERSION_FILE = "LEANFLOW_EXCLUDES_VERSION"
_PRUNES_SINCE_GC_FILE = "LEANFLOW_PRUNES_SINCE_GC"

# Git subprocess timeout (seconds).
_GIT_TIMEOUT: int = max(10, min(60, int(os.getenv("LEANFLOW_CHECKPOINT_TIMEOUT", "30"))))

# Max files to snapshot — skip huge directories to avoid slowdowns.
_MAX_FILES = 50_000

_EXCLUDED_DIRECTORY_NAMES = {
    ".cache",
    ".git",
    ".next",
    ".nuxt",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "venv",
}
_EXCLUDED_RUNTIME_DIRECTORIES = {
    ".leanflow/cache",
    ".leanflow/downloads",
    ".leanflow/workflow-state/activity",
    ".leanflow/workflow-state/dispatch-jobs",
    ".leanflow/workspace",
}
_EXCLUDED_RUNTIME_FILES = {
    ".leanflow/workflow-state/activity.jsonl",
    ".leanflow/workflow-state/outcomes.jsonl",
}

# ---------------------------------------------------------------------------
# Shadow repo helpers
# ---------------------------------------------------------------------------


def _shadow_repo_path(working_dir: str) -> Path:
    """Deterministic shadow repo path: sha256(abs_path)[:16]."""
    abs_path = str(Path(working_dir).resolve())
    dir_hash = hashlib.sha256(abs_path.encode()).hexdigest()[:16]
    return CHECKPOINT_BASE / dir_hash


def _git_env(shadow_repo: Path, working_dir: str) -> dict:
    """Build env dict that redirects git to the shadow repo."""
    env = os.environ.copy()
    env["GIT_DIR"] = str(shadow_repo)
    env["GIT_WORK_TREE"] = str(Path(working_dir).resolve())
    env.pop("GIT_INDEX_FILE", None)
    env.pop("GIT_NAMESPACE", None)
    env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
    return env


def _run_git(
    args: list[str],
    shadow_repo: Path,
    working_dir: str,
    timeout: int = _GIT_TIMEOUT,
    allowed_returncodes: set[int] | None = None,
) -> tuple:
    """Run a git command against the shadow repo.  Returns (ok, stdout, stderr).

    ``allowed_returncodes`` suppresses error logging for known/expected non-zero
    exits while preserving the normal ``ok = (returncode == 0)`` contract.
    Example: ``git diff --cached --quiet`` returns 1 when changes exist.
    """
    env = _git_env(shadow_repo, working_dir)
    cmd = ["git"] + list(args)
    allowed_returncodes = allowed_returncodes or set()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(Path(working_dir).resolve()),
        )
        ok = result.returncode == 0
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if not ok and result.returncode not in allowed_returncodes:
            logger.error(
                "Git command failed: %s (rc=%d) stderr=%s",
                " ".join(cmd),
                result.returncode,
                stderr,
            )
        return ok, stdout, stderr
    except subprocess.TimeoutExpired:
        msg = f"git timed out after {timeout}s: {' '.join(cmd)}"
        logger.error(msg, exc_info=True)
        return False, "", msg
    except FileNotFoundError:
        logger.error("Git executable not found: %s", " ".join(cmd), exc_info=True)
        return False, "", "git not found"
    except Exception as exc:
        logger.error("Unexpected git error running %s: %s", " ".join(cmd), exc, exc_info=True)
        return False, "", str(exc)


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace one shadow-repository control file atomically."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _refresh_shadow_excludes(shadow_repo: Path, working_dir: str) -> str | None:
    """Install current exclusions and untrack newly excluded runtime paths."""
    info_dir = shadow_repo / "info"
    info_dir.mkdir(exist_ok=True)
    _atomic_write_text(info_dir / "exclude", "\n".join(DEFAULT_EXCLUDES) + "\n")

    version_path = shadow_repo / _EXCLUDES_VERSION_FILE
    try:
        current_version = version_path.read_text(encoding="utf-8").strip()
    except OSError:
        current_version = ""
    if current_version == _EXCLUDES_VERSION:
        return None

    head_ok, _, _ = _run_git(
        ["rev-parse", "--verify", "HEAD"],
        shadow_repo,
        working_dir,
        allowed_returncodes={128},
    )
    if head_ok:
        ok, _, error = _run_git(
            ["rm", "-r", "--cached", "--ignore-unmatch", "--", *_RUNTIME_EXCLUDE_PATHS],
            shadow_repo,
            working_dir,
            timeout=_GIT_TIMEOUT * 2,
        )
        if not ok:
            return f"Could not migrate checkpoint exclusions: {error}"

    _atomic_write_text(version_path, _EXCLUDES_VERSION + "\n")
    return None


def _init_shadow_repo(shadow_repo: Path, working_dir: str) -> str | None:
    """Initialise shadow repo if needed.  Returns error string or None."""
    if not (shadow_repo / "HEAD").exists():
        shadow_repo.mkdir(parents=True, exist_ok=True)

        ok, _, err = _run_git(["init"], shadow_repo, working_dir)
        if not ok:
            return f"Shadow repo init failed: {err}"

        _run_git(["config", "user.email", "leanflow@local"], shadow_repo, working_dir)
        _run_git(["config", "user.name", "LeanFlow Checkpoint"], shadow_repo, working_dir)
        (shadow_repo / "LEANFLOW_WORKDIR").write_text(
            str(Path(working_dir).resolve()) + "\n", encoding="utf-8"
        )
        logger.debug("Initialised checkpoint repo at %s for %s", shadow_repo, working_dir)

    return _refresh_shadow_excludes(shadow_repo, working_dir)


def _dir_file_count(path: str) -> int:
    """Count checkpoint-eligible files, stopping once the safety limit is crossed."""
    count = 0
    base = Path(path)
    try:
        for root, directories, filenames in os.walk(base):
            relative_root = Path(root).relative_to(base)
            retained_directories: list[str] = []
            for directory in directories:
                relative = relative_root / directory
                normalized = relative.as_posix()
                if directory in _EXCLUDED_DIRECTORY_NAMES:
                    continue
                if normalized in _EXCLUDED_RUNTIME_DIRECTORIES:
                    continue
                retained_directories.append(directory)
            directories[:] = retained_directories

            for filename in filenames:
                relative = (relative_root / filename).as_posix()
                if relative in _EXCLUDED_RUNTIME_FILES:
                    continue
                if filename == ".DS_Store" or filename.endswith((".log", ".pyc", ".pyo")):
                    continue
                if filename == ".env" or filename.startswith(".env."):
                    continue
                count += 1
                if count > _MAX_FILES:
                    return count
    except (PermissionError, OSError):
        pass
    return count


# ---------------------------------------------------------------------------
# CheckpointManager
# ---------------------------------------------------------------------------


class CheckpointManager:
    """Manages automatic filesystem checkpoints.

    Designed to be owned by AIAgent.  Call ``new_turn()`` at the start of
    each conversation turn and ``ensure_checkpoint(dir, reason)`` before
    any file-mutating tool call.  The manager normally deduplicates to one
    snapshot per directory per turn; lifecycle boundaries may pass
    ``force=True`` to capture newer same-turn edits.

    Parameters
    ----------
    enabled : bool
        Master switch (from config / CLI flag).
    max_snapshots : int
        Keep at most this many checkpoints per directory.
    """

    def __init__(self, enabled: bool = False, max_snapshots: int = 50):
        self.enabled = enabled
        self.max_snapshots = max(1, int(max_snapshots))
        self._checkpointed_dirs: set[str] = set()
        self._git_available: bool | None = None  # lazy probe

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    def new_turn(self) -> None:
        """Reset per-turn dedup.  Call at the start of each agent iteration."""
        self._checkpointed_dirs.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_checkpoint(
        self, working_dir: str, reason: str = "auto", *, force: bool = False
    ) -> bool:
        """Take a checkpoint when enabled and eligible.

        ``force`` bypasses per-turn deduplication for lifecycle snapshots such
        as pre-exit checkpoints, while retaining the normal safety, size, and
        no-filesystem-change checks. Returns whether a new snapshot was taken.
        Never raises; failures are logged at debug level.
        """
        if not self.enabled:
            return False

        # Lazy git probe
        if self._git_available is None:
            self._git_available = shutil.which("git") is not None
            if not self._git_available:
                logger.debug("Checkpoints disabled: git not found")
        if not self._git_available:
            return False

        abs_dir = str(Path(working_dir).resolve())

        # Skip root, home, and other overly broad directories
        if abs_dir in ("/", str(Path.home())):
            logger.debug("Checkpoint skipped: directory too broad (%s)", abs_dir)
            return False

        # Ordinary mutation guards snapshot once per turn. Lifecycle boundaries
        # must still capture a verified edit made after that guard snapshot.
        if not force and abs_dir in self._checkpointed_dirs:
            return False

        self._checkpointed_dirs.add(abs_dir)

        try:
            return self._take(abs_dir, reason)
        except Exception as e:
            logger.debug("Checkpoint failed (non-fatal): %s", e)
            return False

    def list_checkpoints(self, working_dir: str) -> list[dict]:
        """List available checkpoints for a directory.

        Returns a list of dicts with keys: hash, short_hash, timestamp, reason,
        files_changed, insertions, deletions.  Most recent first.
        """
        abs_dir = str(Path(working_dir).resolve())
        shadow = _shadow_repo_path(abs_dir)

        if not (shadow / "HEAD").exists():
            return []

        ok, stdout, _ = _run_git(
            ["log", "--format=%H|%h|%aI|%s", "-n", str(self.max_snapshots)],
            shadow,
            abs_dir,
        )

        if not ok or not stdout:
            return []

        results = []
        for line in stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                entry = {
                    "hash": parts[0],
                    "short_hash": parts[1],
                    "timestamp": parts[2],
                    "reason": parts[3],
                    "files_changed": 0,
                    "insertions": 0,
                    "deletions": 0,
                }
                # Get diffstat for this commit
                stat_ok, stat_out, _ = _run_git(
                    ["diff", "--shortstat", f"{parts[0]}~1", parts[0]],
                    shadow,
                    abs_dir,
                    allowed_returncodes={128, 129},  # first commit has no parent
                )
                if stat_ok and stat_out:
                    self._parse_shortstat(stat_out, entry)
                results.append(entry)
        return results

    @staticmethod
    def _parse_shortstat(stat_line: str, entry: dict) -> None:
        """Parse git --shortstat output into entry dict."""
        import re

        m = re.search(r"(\d+) file", stat_line)
        if m:
            entry["files_changed"] = int(m.group(1))
        m = re.search(r"(\d+) insertion", stat_line)
        if m:
            entry["insertions"] = int(m.group(1))
        m = re.search(r"(\d+) deletion", stat_line)
        if m:
            entry["deletions"] = int(m.group(1))

    def diff(self, working_dir: str, commit_hash: str) -> dict:
        """Show diff between a checkpoint and the current working tree.

        Returns dict with success, diff text, and stat summary.
        """
        abs_dir = str(Path(working_dir).resolve())
        shadow = _shadow_repo_path(abs_dir)

        if not (shadow / "HEAD").exists():
            return {"success": False, "error": "No checkpoints exist for this directory"}

        # Verify the commit exists
        ok, _, err = _run_git(
            ["cat-file", "-t", commit_hash],
            shadow,
            abs_dir,
        )
        if not ok:
            return {"success": False, "error": f"Checkpoint '{commit_hash}' not found"}

        # Stage current state to compare against checkpoint
        _run_git(["add", "-A"], shadow, abs_dir, timeout=_GIT_TIMEOUT * 2)

        # Get stat summary: checkpoint vs current working tree
        ok_stat, stat_out, _ = _run_git(
            ["diff", "--stat", commit_hash, "--cached"],
            shadow,
            abs_dir,
        )

        # Get actual diff (limited to avoid terminal flood)
        ok_diff, diff_out, _ = _run_git(
            ["diff", commit_hash, "--cached", "--no-color"],
            shadow,
            abs_dir,
        )

        # Unstage to avoid polluting the shadow repo index
        _run_git(["reset", "HEAD", "--quiet"], shadow, abs_dir)

        if not ok_stat and not ok_diff:
            return {"success": False, "error": "Could not generate diff"}

        return {
            "success": True,
            "stat": stat_out if ok_stat else "",
            "diff": diff_out if ok_diff else "",
        }

    def restore(self, working_dir: str, commit_hash: str, file_path: str = None) -> dict:
        """Restore files to a checkpoint state.

        Uses ``git checkout <hash> -- .`` (or a specific file) which restores
        tracked files without moving HEAD — safe and reversible.

        Parameters
        ----------
        file_path : str, optional
            If provided, restore only this file instead of the entire directory.

        Returns dict with success/error info.
        """
        abs_dir = str(Path(working_dir).resolve())
        shadow = _shadow_repo_path(abs_dir)

        if not (shadow / "HEAD").exists():
            return {"success": False, "error": "No checkpoints exist for this directory"}

        # Verify the commit exists
        ok, _, err = _run_git(
            ["cat-file", "-t", commit_hash],
            shadow,
            abs_dir,
        )
        if not ok:
            return {
                "success": False,
                "error": f"Checkpoint '{commit_hash}' not found",
                "debug": err or None,
            }

        # Take a checkpoint of current state before restoring (so you can undo the undo)
        self._take(
            abs_dir,
            f"pre-rollback snapshot (restoring to {commit_hash[:8]})",
            prune=False,
        )

        # Restore — full directory or single file
        restore_target = file_path if file_path else "."
        ok, stdout, err = _run_git(
            ["checkout", commit_hash, "--", restore_target],
            shadow,
            abs_dir,
            timeout=_GIT_TIMEOUT * 2,
        )

        if not ok:
            return {"success": False, "error": f"Restore failed: {err}", "debug": err or None}

        # Prune only after checkout so the oldest retained hash remains usable
        # even when the protective pre-rollback snapshot crosses the limit.
        self._prune(shadow, abs_dir)

        # Get info about what was restored
        ok2, reason_out, _ = _run_git(
            ["log", "--format=%s", "-1", commit_hash],
            shadow,
            abs_dir,
        )
        reason = reason_out if ok2 else "unknown"

        result = {
            "success": True,
            "restored_to": commit_hash[:8],
            "reason": reason,
            "directory": abs_dir,
        }
        if file_path:
            result["file"] = file_path
        return result

    def get_working_dir_for_path(self, file_path: str) -> str:
        """Resolve a file path to its working directory for checkpointing.

        Walks up from the file's parent to find a reasonable project root
        (directory containing Lean, git, or build-system root markers).
        Falls back to the file's parent directory.
        """
        path = Path(file_path).resolve()
        if path.is_dir():
            candidate = path
        else:
            candidate = path.parent

        # Walk up looking for project root markers
        markers = {
            ".git",
            ".hg",
            "pyproject.toml",
            "package.json",
            "Cargo.toml",
            "go.mod",
            "Makefile",
            "pom.xml",
            "Gemfile",
            "lakefile.lean",
            "lakefile.toml",
            "lean-toolchain",
        }
        check = candidate
        while check != check.parent:
            if any((check / m).exists() for m in markers):
                return str(check)
            check = check.parent

        # No project root found — use the file's parent
        return str(candidate)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _take(self, working_dir: str, reason: str, *, prune: bool = True) -> bool:
        """Take a snapshot.  Returns True on success."""
        shadow = _shadow_repo_path(working_dir)

        # Init if needed
        err = _init_shadow_repo(shadow, working_dir)
        if err:
            logger.debug("Checkpoint init failed: %s", err)
            return False

        # Quick size guard — don't try to snapshot enormous directories
        if _dir_file_count(working_dir) > _MAX_FILES:
            logger.debug("Checkpoint skipped: >%d files in %s", _MAX_FILES, working_dir)
            return False

        # Stage everything
        ok, _, err = _run_git(
            ["add", "-A"],
            shadow,
            working_dir,
            timeout=_GIT_TIMEOUT * 2,
        )
        if not ok:
            logger.debug("Checkpoint git-add failed: %s", err)
            return False

        # Check if there's anything to commit
        ok_diff, diff_out, _ = _run_git(
            ["diff", "--cached", "--quiet"],
            shadow,
            working_dir,
            allowed_returncodes={1},
        )
        if ok_diff:
            # No changes to commit
            logger.debug("Checkpoint skipped: no changes in %s", working_dir)
            return False

        # Commit
        ok, _, err = _run_git(
            ["commit", "-m", reason, "--allow-empty-message"],
            shadow,
            working_dir,
            timeout=_GIT_TIMEOUT * 2,
        )
        if not ok:
            logger.debug("Checkpoint commit failed: %s", err)
            return False

        logger.debug("Checkpoint taken in %s: %s", working_dir, reason)

        # Prune old snapshots
        if prune:
            self._prune(shadow, working_dir)

        return True

    def _prune(self, shadow_repo: Path, working_dir: str) -> None:
        """Keep recent hashes via a shallow boundary and periodically pack objects."""
        ok, stdout, _ = _run_git(
            ["rev-list", "--first-parent", "--count", "HEAD"],
            shadow_repo,
            working_dir,
        )
        if not ok:
            return

        try:
            count = int(stdout)
        except ValueError:
            return

        if count <= self.max_snapshots:
            return

        ok, retained, _ = _run_git(
            [
                "rev-list",
                "--first-parent",
                f"--max-count={self.max_snapshots}",
                "HEAD",
            ],
            shadow_repo,
            working_dir,
        )
        retained_hashes = retained.splitlines() if ok else []
        if len(retained_hashes) != self.max_snapshots:
            logger.debug(
                "Checkpoint prune skipped: expected %d retained hashes, got %d",
                self.max_snapshots,
                len(retained_hashes),
            )
            return

        # The shallow boundary preserves every retained commit hash while
        # making its older parents unreachable. Unlike rebasing, this keeps
        # hashes already exposed by list_checkpoints valid.
        shallow_path = shadow_repo / "shallow"
        previous_shallow: str | None
        try:
            previous_shallow = shallow_path.read_text(encoding="utf-8")
        except OSError:
            previous_shallow = None
        _atomic_write_text(shallow_path, retained_hashes[-1] + "\n")

        verified, bounded_count, _ = _run_git(
            ["rev-list", "--first-parent", "--count", "HEAD"],
            shadow_repo,
            working_dir,
        )
        if not verified or bounded_count != str(self.max_snapshots):
            if previous_shallow is None:
                shallow_path.unlink(missing_ok=True)
            else:
                _atomic_write_text(shallow_path, previous_shallow)
            logger.error("Checkpoint retention boundary validation failed; restored prior boundary")
            return

        removed = count - self.max_snapshots
        gc_counter_path = shadow_repo / _PRUNES_SINCE_GC_FILE
        try:
            pending_gc = int(gc_counter_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            pending_gc = 0
        pending_gc += removed
        _atomic_write_text(gc_counter_path, f"{pending_gc}\n")
        gc_interval = max(5, min(25, self.max_snapshots // 4 or 5))
        if pending_gc < gc_interval:
            return

        _run_git(
            ["reflog", "expire", "--expire=now", "--all"],
            shadow_repo,
            working_dir,
        )
        # One packing thread and bounded delta windows prevent checkpoint
        # maintenance from competing with Lean/research workers for RAM.
        packed, _, _ = _run_git(
            [
                "-c",
                "pack.threads=1",
                "-c",
                "pack.windowMemory=16m",
                "-c",
                "pack.deltaCacheSize=16m",
                "-c",
                "core.bigFileThreshold=1m",
                "gc",
                "--prune=now",
                "--quiet",
            ],
            shadow_repo,
            working_dir,
            timeout=max(30, min(120, _GIT_TIMEOUT * 2)),
        )
        if packed:
            _atomic_write_text(gc_counter_path, "0\n")
        logger.debug(
            "Checkpoint history retained %d commits and packing %s",
            self.max_snapshots,
            "completed" if packed else "will retry",
        )
