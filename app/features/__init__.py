"""All features, in /help and menu order. Add a feature here after creating its package."""
from app.features.biorxiv.feature import FEATURE as BIORXIV
from app.features.cards.feature import FEATURE as CARDS
from app.features.expenses.feature import FEATURE as EXPENSES
from app.features.market.feature import FEATURE as MARKET
from app.features.placeholder.feature import FEATURE as PLACEHOLDER
from app.features.realestate.feature import FEATURE as REALESTATE
from app.features.vmhealth.feature import FEATURE as VMHEALTH

ALL = [MARKET, REALESTATE, CARDS, EXPENSES, BIORXIV, PLACEHOLDER, VMHEALTH]
