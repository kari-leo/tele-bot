#!/usr/bin/env python
"""Feishu webhook launcher for tele_bot."""

from __future__ import annotations

import sys

from tele_bot.config.paths import configure_runtime_root

configure_runtime_root(__file__)


def show_help() -> None:
    print(
        "tele_bot Feishu webhook launcher\n\n"
        "Usage:\n"
        "  python start.py [feishu-webhook]\n\n"
        "Examples:\n"
        "  python start.py\n"
        "  python start.py feishu-webhook\n"
    )


def start_feishu_webhook() -> None:
    from tele_bot.config.feishu import FeishuSettings

    try:
        settings = FeishuSettings.from_env()
    except Exception as exc:  # noqa: BLE001
        print(f"[X] Error: Failed to load Feishu config: {exc}")
        print("Configure Feishu in tele_bot/config/feishu/local.env")
        sys.exit(1)

    print("=" * 70)
    print("Mode: Feishu Webhook")
    print("=" * 70)
    print()
    print("[OK] Using Feishu event-subscription webhook")
    print(f"[OK] Local listener: http://{settings.webhook_host}:{settings.webhook_port}")
    print(f"[OK] Webhook path: {settings.webhook_path}")
    print("[OK] Public URL: https://feishu.windborne.cc/feishu/webhook")
    print()
    print("Feishu Admin Panel requirements:")
    print("  1. Event subscription mode: Request URL")
    print("  2. Request URL: https://feishu.windborne.cc/feishu/webhook")
    print("  3. Subscribed event: im.message.receive_v1")
    print()

    import uvicorn

    uvicorn.run(
        "app:app",
        host=settings.webhook_host,
        port=settings.webhook_port,
        log_level="info",
    )


def main() -> None:
    if len(sys.argv) < 2:
        start_feishu_webhook()
        return

    mode = sys.argv[1].lower()
    if mode in ["help", "-h", "--help"]:
        show_help()
    elif mode in ("feishu-webhook", "feishu"):
        start_feishu_webhook()
    else:
        print(f"[X] Unknown mode: {mode}")
        print("Use 'python start.py help' to see available modes.")
        sys.exit(1)


if __name__ == "__main__":
    main()
