from models.site_version import SiteVersion
from repositories.base import BaseRepository

class SiteVersionRepository(BaseRepository[SiteVersion]):
    pass
site_version_repo = SiteVersionRepository(SiteVersion)
