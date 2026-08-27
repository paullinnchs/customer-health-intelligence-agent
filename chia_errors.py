"""
chia_errors.py
--------------
One exception family for every STOP condition in a client run.

A client run either produces a complete, trustworthy result or it stops with a
message that names the problem and the fix. There is no third outcome. These
exceptions are the mechanism: main() catches ClientRunError once, prints it,
and exits 1.

Nothing here is silently recoverable. If a condition can be safely worked
around, it belongs in the data-quality warnings, not in this file.
"""


class ClientRunError(Exception):
    """
    Base for every condition that must halt a client run.

    `remedy` is the operator-facing instruction. It is separated from the
    message so the console can present "what happened" and "what to do" as
    distinct blocks rather than one long sentence.
    """

    label = "Run stopped"

    def __init__(self, message, remedy=None):
        super().__init__(message)
        self.message = message
        self.remedy = remedy

    def render(self):
        lines = [f"{self.label}: {self.message}"]

        if self.remedy:
            lines.append("")
            lines.append(f"How to fix: {self.remedy}")

        return "\n".join(lines)


class WorkspaceError(ClientRunError):
    """The client workspace is missing, malformed, or unsafe to use."""

    label = "Client workspace error"


class ConfigError(ClientRunError):
    """The baseline or client configuration cannot be used as supplied."""

    label = "Configuration error"


class OverrideError(ConfigError):
    """A client override is unsupported, undocumented, or locked."""

    label = "Client override rejected"


class OutputIsolationError(ClientRunError):
    """
    A write was attempted outside the selected client's outputs directory.

    This is the last line of defence for client isolation. Reaching it means a
    filename or path escaped slugification, so it is treated as a hard stop
    rather than a sanitizable condition.
    """

    label = "Output isolation violation"
