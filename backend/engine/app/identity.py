"""Product identity for the hosted engine, and the one handle normaliser.

Identity is constant. The product is Starling and the engine version is the package
version the worker reports in its READY line. Nothing is read from a file or from
the environment: the gateway owns the browser-extension ids
(`STARLING_EXTENSION_IDS`), and a hosted engine has no native host, no updater and
no frozen build to describe.

The module stays free of app-level imports at import time, so intelligence.py and
conversation.py can import it without a cycle through main.py.
"""

from __future__ import annotations

from . import __version__

PRODUCT_NAME = "Starling"
ENGINE_VERSION: str = __version__


def normalize_handle(value: str | None) -> str:
    """An @handle reduced to the one form every comparison uses.

    THE ONE DEFINITION. There were four, and two had already diverged:

        identity.own_handle          .strip().lstrip("@").strip().lower()
        intelligence.py call site    .strip().lstrip("@").strip().lower()
        pack_ownership               .strip().lstrip("@").lower()   <- no 2nd strip
        test_own_handle_contract     re-derived inside an assertion

    Five characters of string work, four implementations. The ORDER matters and
    is not obvious: `.lstrip("@")` before `.strip()` does nothing to " @Name" --
    there is no "@" at position 0 until the space is gone, so the trim then
    exposes one that survives and the result is "@name" instead of "name". That
    shipped, and it is invisible until two callers with different padding
    compare against each other: the self-post guard stops matching, silently, on
    exactly the newline-then-at-sign shape X's User-Name DOM yields.

    The TRAILING strip is not redundant either -- it is what catches "@ name".
    """
    return str(value or "").strip().lstrip("@").strip().lower()


def own_handle() -> str:
    """The account's own @handle, lowercased without the @, or "".

    Lives here because three callers need it and identity.py is the one module
    with no app-level imports — main.py cannot be imported from intelligence.py
    or conversation.py without a cycle. The storage imports are function-local
    for the same reason.

    BOTH SOURCES, in this order. `get_account_profile` is what the user (or the
    extension) recorded; `infer_account_model` derives it from the archive when
    they never did. A previous copy of this in conversation.py consulted only the
    first, so on an install with an unset profile handle it returned "" while
    main.py's copy returned the handle — and conversation.py's caller is
    ownership detection, where "" means a reply to your own thread is not
    recognised as one.
    """
    from .account_model import infer_account_model
    from .storage import get_account_profile

    try:
        handle = (get_account_profile().get("handle")
                  or infer_account_model().get("handle") or "")
        # STRIP THE SPACE FIRST. This read `.lstrip("@").strip()`, which does
        # nothing to " @Name ": there is no "@" at position 0 to remove, so the
        # trim exposes one that then survives — the function returned
        # "@name" instead of "name". Callers compare a normalised parent handle
        # against this, so one padded value and one clean value stop matching
        # and the guard that keeps a draft from claiming the author's own
        # possessions on the account's own post fails silently, which is the only
        # way it can fail. Through the shared normalizer, so a caller comparing
        # against this can call the same function instead of restating its
        # character work.
        return normalize_handle(handle)
    except Exception:                                        # noqa: BLE001
        # Never raises: every caller is on a drafting path, and a profile that
        # cannot be read is not a reason to stop writing.
        return ""
