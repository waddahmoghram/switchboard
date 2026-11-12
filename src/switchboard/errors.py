"""Exception hierarchy shared by the core and connectors."""


class SwitchboardError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(SwitchboardError):
    """The configuration file is missing, malformed, or inconsistent."""


class AuditError(SwitchboardError):
    """The audit log contains an unreadable or malformed record."""


class SecretError(SwitchboardError):
    """A secret reference could not be resolved safely."""


class ToolError(SwitchboardError):
    """A tool handler failed in a way the caller should see.

    Handlers raise this for expected problems (bad mailbox name, unreachable server). The dispatcher
    turns it into an error result without a traceback. Any other exception is reported only by type.

    Raising ``ToolError`` asserts that the requested external effect did **not** happen. When that is
    not known, raise :class:`UncertainOutcomeError` instead.
    """


class UncertainOutcomeError(ToolError):
    """The handler cannot tell whether its external effect happened.

    The action journal records the attempt as ``uncertain`` and refuses an identical repeat until the
    operator reconciles it. Use this after a request may have reached a provider or device, for example
    when a write timed out, the connection dropped, or the confirmation could not be read.
    """


class HttpError(SwitchboardError):
    """A network-level HTTP failure (DNS, TLS, timeout, oversized response, refused redirect)."""
