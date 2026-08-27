"""
client_workspace.py
-------------------
Client isolation.

A run resolves exactly one ClientWorkspace before it touches the filesystem,
and that object is the only thing in the process that knows an output path.
Every byte CHIA writes goes through `write_text` / `write_json`, which refuse
any destination outside the selected client's outputs directory.

Two clients can never be mixed because a process only ever holds one
workspace, it is constructed before any I/O, and it is never mutated.

Layout:

    clients/<slug>/
        config/client_config.yaml
        input/*.csv
        outputs/            <- the only writable location in a client run
"""

import json
import re
from pathlib import Path

from chia_errors import OutputIsolationError, WorkspaceError


REPO_ROOT = Path(__file__).resolve().parent

CONFIG_FILENAME = "client_config.yaml"

# Directories the engine reads from but must never write into, regardless of
# what a path argument claims.
PROTECTED_DIRS = (
    REPO_ROOT / "sample_data",
    REPO_ROOT / "config",
)


def slugify(value):
    """
    Reduce arbitrary client text to a safe single path segment.

    Account names arrive from a customer CRM export and are not trustworthy as
    filenames. A name containing a separator, a drive letter, or `..` must not
    be able to steer a write, so everything outside [a-z0-9-] is collapsed to a
    hyphen rather than escaped.
    """
    if value is None:
        raise OutputIsolationError(
            "Cannot build an output filename from an empty account name.",
            "Check the account_name column in crm_accounts.csv.",
        )

    slug = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower())
    slug = slug.strip("-")

    if not slug:
        raise OutputIsolationError(
            f"Account name {value!r} contains no characters usable in a "
            f"filename.",
            "Give the account a name containing letters or digits in "
            "crm_accounts.csv.",
        )

    return slug


class ClientWorkspace:
    """
    A resolved, validated client workspace and the run's only write gate.

    Construct with `ClientWorkspace.resolve()`. The constructor is deliberately
    plain so the validation cannot be bypassed by accident.
    """

    def __init__(self, root, slug):
        self.root = root
        self.slug = slug
        self.config_dir = root / "config"
        self.config_path = self.config_dir / CONFIG_FILENAME
        self.input_dir = root / "input"
        self.outputs_dir = root / "outputs"
        self.written_files = []

    # -- discovery ----------------------------------------------------------

    @classmethod
    def resolve(cls, raw_path):
        """
        Locate and validate a client workspace from a command-line path.

        STOP conditions enforced here: missing client directory, missing input
        directory, missing client configuration file, and any path that would
        make a protected directory writable.
        """
        if not raw_path or not str(raw_path).strip():
            raise WorkspaceError(
                "No client workspace was given.",
                "Run: uv run python main.py clients/<client-name>",
            )

        root = Path(str(raw_path).strip()).expanduser()

        if not root.is_absolute():
            root = Path.cwd() / root

        root = root.resolve()

        if not root.exists():
            raise WorkspaceError(
                f"Client directory does not exist: {root}",
                "Create the workspace by copying clients/_template, or check "
                "the path you passed on the command line.",
            )

        if not root.is_dir():
            raise WorkspaceError(
                f"Client path is not a directory: {root}",
                "Pass the client workspace directory, not a file inside it.",
            )

        cls._reject_unsafe_root(root)

        workspace = cls(root, root.name)

        if not workspace.input_dir.exists():
            raise WorkspaceError(
                f"Client input directory does not exist: "
                f"{workspace.input_dir}",
                "Create an input/ directory in the client workspace and place "
                "crm_accounts.csv inside it.",
            )

        if not workspace.input_dir.is_dir():
            raise WorkspaceError(
                f"Client input path is not a directory: "
                f"{workspace.input_dir}",
                "Replace it with a directory containing the client CSV "
                "exports.",
            )

        if not workspace.config_path.exists():
            raise WorkspaceError(
                f"Client configuration not found: {workspace.config_path}",
                f"Create config/{CONFIG_FILENAME} in the client workspace. "
                f"clients/_template has a documented starting point.",
            )

        # outputs/ is the one directory the engine may create, and it is
        # created lazily on first write. Doing it here would mean
        # --validate-only left a directory behind, and "writes nothing" has to
        # mean nothing.
        workspace.outputs_dir = cls._resolve_outputs_dir(workspace.outputs_dir)

        return workspace

    @staticmethod
    def _resolve_outputs_dir(outputs_dir):
        """
        Resolve outputs/ whether or not it exists yet.

        Path.resolve() on Windows returns the correct absolute path for a
        missing directory, but the parent is resolved explicitly so a symlinked
        client root cannot produce a containment check against the wrong base.
        """
        return outputs_dir.parent.resolve() / outputs_dir.name

    @staticmethod
    def _reject_unsafe_root(root):
        """
        Refuse workspace roots that would make a shared directory writable.

        Pointing a client run at the repository root or at sample_data would
        turn the write gate into a licence to overwrite shared assets, so those
        are rejected before anything else happens.
        """
        if root == REPO_ROOT:
            raise WorkspaceError(
                "The repository root cannot be used as a client workspace.",
                "Pass a directory under clients/, for example clients/acme.",
            )

        for protected in PROTECTED_DIRS:
            if root == protected or protected in root.parents:
                raise WorkspaceError(
                    f"{root} is inside a protected directory ({protected}) "
                    f"and cannot be used as a client workspace.",
                    "Client workspaces belong under clients/. sample_data and "
                    "config are read-only shared assets.",
                )

    # -- the write gate -----------------------------------------------------

    def output_path(self, filename):
        """
        Resolve a filename inside this client's outputs directory.

        Rejects anything that is not a single bare filename, then re-checks
        containment after resolution so symlinks and `..` cannot escape.
        """
        if not filename or not str(filename).strip():
            raise OutputIsolationError(
                "An output filename was empty.",
                "This is an internal error. Report it with the run log.",
            )

        name = str(filename).strip()

        if (
            "/" in name
            or "\\" in name
            or name in (".", "..")
            or Path(name).is_absolute()
            or Path(name).name != name
        ):
            raise OutputIsolationError(
                f"Refusing to write {name!r}: output filenames must be a "
                f"single name with no path separators.",
                "This is an internal error. Report it with the run log.",
            )

        candidate = (self.outputs_dir / name).resolve()

        try:
            candidate.relative_to(self.outputs_dir)
        except ValueError:
            raise OutputIsolationError(
                f"Refusing to write outside the client outputs directory.\n"
                f"  Attempted: {candidate}\n"
                f"  Permitted: {self.outputs_dir}",
                "This is an internal error. Report it with the run log.",
            ) from None

        return candidate

    def write_text(self, filename, content):
        """The single choke point for every file CHIA produces."""
        path = self.output_path(filename)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        if path not in self.written_files:
            self.written_files.append(path)

        return path

    def write_json(self, filename, payload):
        return self.write_text(
            filename, json.dumps(payload, indent=2) + "\n"
        )

    # -- helpers ------------------------------------------------------------

    def report_filename(self, account_name):
        return f"{slugify(account_name)}-health-intelligence.md"

    def input_path(self, filename):
        return self.input_dir / filename

    def relative_outputs(self):
        """Output paths relative to the workspace, for the run manifest."""
        return [
            str(path.relative_to(self.root)).replace("\\", "/")
            for path in self.written_files
        ]

    def display_root(self):
        try:
            return str(self.root.relative_to(REPO_ROOT)).replace("\\", "/")
        except ValueError:
            return str(self.root)
