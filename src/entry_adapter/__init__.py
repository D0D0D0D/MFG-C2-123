# PART: entry-adapter v0.4.0 (parts@de2fcb3)
from .auth import (
    SCOPE_KEYS,
    STG_DEFAULT_SCOPE_ENVS,
    AuthContext,
    AuthSource,
    Operation,
    authenticate,
)

__all__ = [
    "authenticate",
    "AuthContext",
    "AuthSource",
    "Operation",
    "SCOPE_KEYS",
    "STG_DEFAULT_SCOPE_ENVS",
]
