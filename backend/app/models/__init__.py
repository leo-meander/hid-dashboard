from app.models.branch import Branch
from app.models.kpi import KPITarget
from app.models.reservation import Reservation
from app.models.daily_metrics import DailyMetrics
from app.models.event import Event
from app.models.website_metrics import WebsiteMetrics
from app.models.angle import AdAngle
from app.models.ads import AdsPerformance
from app.models.ads_budget import AdsBudget
from app.models.marketing_budget import MarketingBudget
from app.models.yearly_plan import YearlyPlan
from app.models.ads_booking_match import AdsBookingMatch
from app.models.kol import KOLRecord, KOLBooking
from app.models.activity import MarketingActivity
from app.models.user import User
from app.models.creative import BranchKeypoint, AdCopy, AdMaterial, AdApproval, AdName

# Phase 4 — Creative Intelligence Library
from app.models.creative_angle import CreativeAngle
from app.models.creative_copy import CreativeCopy
from app.models.creative_material import CreativeMaterial
from app.models.ad_combo import AdCombo
from app.models.ad_analysis import AdAnalysisResult
from app.models.reservation_daily import ReservationDaily
from app.models.email_event import EmailEvent
from app.models.email_campaign_stats import EmailCampaignStats
from app.models.gov_visitor import GovVisitorData

# Alert System
from app.models.alert import AlertRule, AlertHistory, AlertNotificationLog

# Rate Plan Quota tracking
from app.models.rate_plan_quota import RatePlanQuota, RatePlanQuotaStatus

__all__ = [
    "Branch", "KPITarget", "Reservation", "DailyMetrics", "Event",
    "WebsiteMetrics", "AdAngle", "AdsPerformance", "AdsBudget",
    "MarketingBudget",
    "YearlyPlan",
    "AdsBookingMatch", "KOLRecord", "KOLBooking",
    "MarketingActivity", "User", "BranchKeypoint", "AdCopy", "AdMaterial",
    "AdApproval", "AdName",
    "CreativeAngle", "CreativeCopy", "CreativeMaterial", "AdCombo",
    "AdAnalysisResult", "ReservationDaily",
    "EmailEvent", "EmailCampaignStats",
    "GovVisitorData",
    "AlertRule", "AlertHistory", "AlertNotificationLog",
    "RatePlanQuota", "RatePlanQuotaStatus",
]
