from models.promotion import PromotionSetup, PromotionCampaign
from repositories.base import BaseRepository

class PromotionSetupRepository(BaseRepository[PromotionSetup]):
    pass
promotion_setup_repo = PromotionSetupRepository(PromotionSetup)

class PromotionCampaignRepository(BaseRepository[PromotionCampaign]):
    pass
promotion_campaign_repo = PromotionCampaignRepository(PromotionCampaign)
