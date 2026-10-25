"""Always-available Base tools; invalid configuration never kills the bot."""
from tele_bot.bitable.client import BitableClient
from tele_bot.bitable.policy import BitablePolicy
from tele_bot.bitable.service import BitableService
from tele_bot.config.bitable import BitableSettings
from tele_bot.persistence.bitable_store import BitableStore


def build_bitable_service():
    try:
        settings = BitableSettings.from_env()
    except Exception:
        return BitableService(BitableSettings(), None, None, None, configuration_error=True)
    try:
        policy = (BitablePolicy.load(settings.targets_file, origin=settings.api_base_url) if settings.targets_file.exists()
                  else BitablePolicy([], origin=settings.api_base_url))
        store = BitableStore(settings.sqlite_path)
    except Exception:
        return BitableService(settings, None, None, None, configuration_error=True)
    return BitableService(settings, BitableClient(settings), policy, store)
