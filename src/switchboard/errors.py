"""Exception hierarchy shared by the core and connectors."""


class SwitchboardError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(SwitchboardError):
    """The configuration file is missing, malformed, or inconsistent."""


class SecretError(SwitchboardError):
    """A secret reference could not be resolved safely."""


class ToolError(SwitchboardError):
    """A tool handler failed in a way the caller should see.

    Handlers raise this for expected problems (bad mailbox name, unreachable server). The dispatcher
    turns it into an error result without a traceback. Any other exception is reported only by type.
    """


class HttpError(SwitchboardError):
    """A network-level HTTP failure (DNS, TLS, timeout, oversized response, refused redirect)."""
