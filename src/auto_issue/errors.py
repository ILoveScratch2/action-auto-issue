


class AutoIssueError(Exception):
    """Base class for every error raised by this action."""


class ConfigurationError(AutoIssueError):
    """Raised when inputs or config.json are unusable."""


class ApiError(AutoIssueError):
    """Raised when an HTTP call to the AI provider fails for a non-filtering reason."""


class TruncatedResponseError(AutoIssueError):
    """Raised when the model output was cut off before it finished."""


class ContentFilterError(AutoIssueError):
    """Raised when the provider's safety filter rejected the request or the output.

    This is a decision signal, not a transient failure: callers close the content
    instead of retrying or failing open.
    """

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details
