"""Validate the configured Instagram token and print expiry guidance."""

from __future__ import annotations

import sys

from social.instagram_adapter import (
    clear_instagram_token_alert_state,
    InstagramAdapter,
    InstagramAPIError,
    instagram_token_error_guidance,
    instagram_token_expiry_warning,
    load_instagram_config,
)


def main() -> int:
    """Check whether the configured Instagram token can authenticate."""
    try:
        config = load_instagram_config(required=True)
    except ValueError as exc:
        print(f"Instagram config error: {exc}", file=sys.stderr)
        return 1

    assert config is not None
    warning = instagram_token_expiry_warning(config)
    if warning:
        print(f"Warning: {warning}")
    elif config.access_token_expires_at is None:
        print(
            "Warning: INSTAGRAM_ACCESS_TOKEN_EXPIRES_AT is not set; Copierbot cannot warn before expiry."
        )

    adapter = InstagramAdapter(config)
    try:
        account = adapter.verify_account()
    except InstagramAPIError as exc:
        print(f"Instagram token check failed: {exc}", file=sys.stderr)
        print(instagram_token_error_guidance(str(exc)), file=sys.stderr)
        return 1

    clear_instagram_token_alert_state()
    username = str(account.get("username", "")).strip() or "unknown"
    print(f"Instagram token is valid for @{username}.")
    if config.access_token_expires_at is not None:
        print(f"Configured expiry: {config.access_token_expires_at.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
