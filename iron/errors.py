"""User-facing error types. Anything raised as an IronError is shown without a traceback."""


class IronError(Exception):
    """Base class for errors that carry a message meant for the user."""


class NoRepository(IronError):
    """The location does not hold an Iron repository."""


class KeyMissing(IronError):
    """The repository is encrypted and no matching key is available."""

    def __init__(self, message, repo_id=None):
        super().__init__(message)
        self.repo_id = repo_id


class IntegrityError(IronError):
    """Stored data failed hash or authentication checks."""


class PushRejected(IronError):
    """A ref no longer has the value the push expected (compare-and-swap failed)."""


class PushRace(IronError):
    """Another writer appended to the log at the same moment; our record was withdrawn."""


class RemoteBusy(IronError):
    """Another device holds the push lock."""
