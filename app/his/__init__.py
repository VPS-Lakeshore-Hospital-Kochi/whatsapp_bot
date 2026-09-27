from app.config import Settings
from app.his.base import HIS


def make_his(settings: Settings) -> HIS:
    if settings.his_adapter == "elider":
        from app.his.elider import EliderHIS

        return EliderHIS(settings.elider_base_url, settings.elider_api_key)
    from app.his.mock import MockHIS

    return MockHIS()
