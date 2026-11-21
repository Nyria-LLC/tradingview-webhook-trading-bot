"""One-off Schwab login: `python -m tvbot.schwab_login` writes the token file the bot reads.

Exit codes: 0 saved, 1 the login or the account lookup failed, 2 a setting or the extra is missing.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

DEFAULT_CALLBACK_URL = "https://127.0.0.1:8182"
DEFAULT_TOKEN_PATH = "schwab_token.json"


def _mask(account_number: object) -> str:
    return "..." + str(account_number)[-3:]


def main() -> int:
    load_dotenv(".env")  # same file and location as uvicorn's --env-file .env
    api_key = os.environ.get("SCHWAB_APP_KEY", "").strip()
    app_secret = os.environ.get("SCHWAB_APP_SECRET", "").strip()
    for name, value in (("SCHWAB_APP_KEY", api_key), ("SCHWAB_APP_SECRET", app_secret)):
        if not value:
            print(f"missing {name}")
            return 2
    callback_url = os.environ.get("SCHWAB_CALLBACK_URL", "").strip() or DEFAULT_CALLBACK_URL
    token_path = os.environ.get("SCHWAB_TOKEN_PATH", "").strip() or DEFAULT_TOKEN_PATH

    try:
        import schwab.auth
    except ImportError:
        print('install the Schwab extra: python -m pip install ".[schwab]"')
        return 2

    try:
        client = schwab.auth.client_from_manual_flow(api_key, app_secret, callback_url, token_path)
        response = client.get_account_numbers()
        if response.is_error:
            print(f"Schwab login failed: account lookup returned HTTP {response.status_code}")
            return 1
        accounts = [_mask(entry.get("accountNumber", "")) for entry in response.json()]
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Schwab login failed: {type(exc).__name__}")
        return 1

    print(
        f"Saved the Schwab token to {token_path}. Schwab tokens last seven days; "
        "run this command again when orders start failing with an auth error."
    )
    for account in accounts:
        print(account)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
