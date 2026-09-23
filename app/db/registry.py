"""Model registry for Alembic autogenerate.

Importing a model class registers it on Base.metadata. Alembic compares that
metadata against the live database, so every model must be imported somewhere
before autogenerate runs -- this module is that place.

It is deliberately separate from app/db/base.py: models import Base from there,
so importing models *into* base.py would create a circular import.

Extend the imports below as you add models.
"""

from app.db.base import Base
from app.models.billing import CreditPurchase, CreditTransaction, Subscription, Wallet
from app.models.lead import Company, DecisionMaker
from app.models.lead_search import LeadSearchEvent, LeadSearchRun
from app.models.notification import Notification
from app.models.notification_preference import NotificationPreference
from app.models.radar_monitor import RadarMonitor
from app.models.user import EmailVerificationToken, PasswordResetToken, User
from app.models.workflow_error import WorkflowError

__all__ = [
    "Base",
    "Company",
    "CreditPurchase",
    "CreditTransaction",
    "DecisionMaker",
    "EmailVerificationToken",
    "LeadSearchEvent",
    "LeadSearchRun",
    "Notification",
    "NotificationPreference",
    "PasswordResetToken",
    "RadarMonitor",
    "Subscription",
    "User",
    "Wallet",
    "WorkflowError",
]
