from models.analytics import AnalyticsEvent
from repositories.base import BaseRepository

class AnalyticsEventRepository(BaseRepository[AnalyticsEvent]):
    pass
analytics_event_repo = AnalyticsEventRepository(AnalyticsEvent)
