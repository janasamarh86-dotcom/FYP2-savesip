"""
SaveSip - Database models.

Defines the User, Income, Expense and Budget tables, the fixed option lists
from the FYP 1 proposal, and the Flask-Login user loader.
"""

from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, UniqueConstraint, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import validates

from app import bcrypt, db, login_manager


# ---------------------------------------------------------------------------
# Fixed option lists (from the FYP 1 proposal - do not change without review)
# ---------------------------------------------------------------------------

CATEGORIES = [
    "Food",
    "Groceries",
    "Transport",
    "Shopping",
    "Fees",
    "Subscriptions",
    "Savings",
    "Academic",
    "Social",
    "House Rent",  # added in FYP 2 for students renting off campus
]

SOCIAL_MEDIA_PLATFORMS = ["TikTok", "Instagram", "Shopee Live"]

# Values allowed in Expense.social_media_influence. "None" means the purchase
# was not influenced by social media.
SOCIAL_MEDIA_INFLUENCE_OPTIONS = SOCIAL_MEDIA_PLATFORMS + ["None"]

INCOME_SOURCES = [
    "Part-time Job",
    "Parents/Family",
    "PTPTN",
    "Scholarship",
    "Personal Savings",
    "Other",
]

PAYMENT_METHODS = ["Cash", "Cashless"]

# Expense types stored in Expense.expense_type, with the short labels shown
# in the interface
EXPENSE_TYPES = ["Essential", "Luxury", "Situational"]
EXPENSE_TYPE_LABELS = {
    "Essential": "Essential",
    "Luxury": "Want",
    "Situational": "Unplanned",
}


def utcnow():
    """Current UTC time as a naive datetime.

    Same value as datetime.utcnow(), which is deprecated from Python 3.12.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_today():
    """Current UTC date, used as the default for transaction dates."""
    return utcnow().date()


# SQLite ignores foreign key constraints (including ON DELETE CASCADE) unless
# they are switched on for every connection.
@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _require_option(field_name, value, options):
    """Raise ValueError if value is not one of the allowed options."""
    if value not in options:
        raise ValueError(
            f"Invalid {field_name} '{value}'. Allowed values: {', '.join(options)}"
        )
    return value


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(30), unique=True, index=True, nullable=False)
    email = db.Column(db.String(120), unique=True, index=True, nullable=False)
    # bcrypt hashes are always 60 characters
    password_hash = db.Column(db.String(60), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    # Notification preferences (Settings page), used by build_suggestions()
    notify_budget = db.Column(db.Boolean, nullable=False, default=True)
    notify_social = db.Column(db.Boolean, nullable=False, default=True)
    notify_monthly = db.Column(db.Boolean, nullable=False, default=False)

    incomes = db.relationship(
        "Income",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="desc(Income.date)",
    )
    expenses = db.relationship(
        "Expense",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="desc(Expense.date)",
    )
    budgets = db.relationship(
        "Budget",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan",
    )
    planned_expenses = db.relationship(
        "PlannedExpense",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="PlannedExpense.target_date",
    )

    @validates("username")
    def _normalise_username(self, key, value):
        return value.strip()

    @validates("email")
    def _normalise_email(self, key, value):
        return value.strip().lower()

    def set_password(self, password):
        """Hash and store a plain-text password with bcrypt."""
        self.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")

    def check_password(self, password):
        """Return True if the plain-text password matches the stored hash."""
        return bcrypt.check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.username}>"


@login_manager.user_loader
def load_user(user_id):
    """Reload the logged-in user from the ID stored in the session."""
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Income
# ---------------------------------------------------------------------------

class Income(db.Model):
    __tablename__ = "incomes"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_income_amount_positive"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    source = db.Column(db.String(30), nullable=False)
    payment_method = db.Column(db.String(30), nullable=False)
    date = db.Column(db.Date, default=utc_today, nullable=False, index=True)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    @validates("source")
    def _validate_source(self, key, value):
        return _require_option("income source", value, INCOME_SOURCES)

    @validates("payment_method")
    def _validate_payment_method(self, key, value):
        return _require_option("payment method", value, PAYMENT_METHODS)

    def __repr__(self):
        return f"<Income {self.source} RM{self.amount} on {self.date}>"


# ---------------------------------------------------------------------------
# Expense
# ---------------------------------------------------------------------------

class Expense(db.Model):
    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_expense_amount_positive"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    category = db.Column(db.String(30), nullable=False, index=True)
    payment_method = db.Column(db.String(30), nullable=False)
    # Which social media platform influenced the purchase, or "None".
    social_media_influence = db.Column(
        db.String(20), nullable=False, default="None", index=True
    )
    # Essential, Luxury (shown as Want) or Situational (shown as Unplanned).
    # Only Luxury expenses can be flagged as impulse buys on the Suggestions page.
    expense_type = db.Column(db.String(20), nullable=False, default="Luxury", index=True)
    date = db.Column(db.Date, default=utc_today, nullable=False, index=True)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    @validates("category")
    def _validate_category(self, key, value):
        return _require_option("category", value, CATEGORIES)

    @validates("payment_method")
    def _validate_payment_method(self, key, value):
        return _require_option("payment method", value, PAYMENT_METHODS)

    @validates("social_media_influence")
    def _validate_social_media_influence(self, key, value):
        return _require_option(
            "social media influence", value, SOCIAL_MEDIA_INFLUENCE_OPTIONS
        )

    @validates("expense_type")
    def _validate_expense_type(self, key, value):
        return _require_option("expense type", value, EXPENSE_TYPES)

    @property
    def is_social_media_influenced(self):
        return self.social_media_influence != "None"

    @property
    def is_essential(self):
        return self.expense_type == "Essential"

    @property
    def type_label(self):
        """Label shown in the interface: Essential, Want or Unplanned."""
        return EXPENSE_TYPE_LABELS[self.expense_type]

    def __repr__(self):
        return f"<Expense {self.category} RM{self.amount} on {self.date}>"


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class Budget(db.Model):
    __tablename__ = "budgets"
    __table_args__ = (
        # One budget per category per month for each user
        UniqueConstraint(
            "user_id", "category", "month", "year", name="uq_budget_user_category_period"
        ),
        CheckConstraint("amount > 0", name="ck_budget_amount_positive"),
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_budget_month_range"),
        CheckConstraint("year BETWEEN 2000 AND 2100", name="ck_budget_year_range"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category = db.Column(db.String(30), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    # Optional note on why this budget was set
    description = db.Column(db.String(255))

    @validates("category")
    def _validate_category(self, key, value):
        return _require_option("category", value, CATEGORIES)

    def __repr__(self):
        return f"<Budget {self.category} RM{self.amount} for {self.month}/{self.year}>"


# ---------------------------------------------------------------------------
# Planned expense
# ---------------------------------------------------------------------------

class PlannedExpense(db.Model):
    """A cost the user expects in the future, e.g. next semester's fees."""

    __tablename__ = "planned_expenses"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_planned_expense_amount_positive"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    category = db.Column(db.String(30), nullable=False)
    target_date = db.Column(db.Date, nullable=False, index=True)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    @validates("category")
    def _validate_category(self, key, value):
        return _require_option("category", value, CATEGORIES)

    def __repr__(self):
        return f"<PlannedExpense {self.title} RM{self.amount} on {self.target_date}>"


# ---------------------------------------------------------------------------
# Schema upgrades for existing databases
# ---------------------------------------------------------------------------

def upgrade_schema():
    """Bring an existing database up to date with the models above.

    db.create_all() creates missing tables but never changes existing ones,
    so column changes are applied here. Safe to run on every start: each
    step only runs when it is still needed.

    1. Expense.is_essential (True/False) was replaced by Expense.expense_type.
       Existing essential expenses become "Essential" and the rest
       become "Luxury" (Want).
    2. PlannedExpense gained an optional description.
    3. User gained notification preferences (budget and social media alerts
       on, monthly reminder off, as on the Settings page).
    4. Budget gained an optional description.
    """
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()

    def columns_of(table):
        return {column["name"] for column in inspector.get_columns(table)} if table in tables else None

    expense_columns = columns_of("expenses")
    planned_columns = columns_of("planned_expenses")
    user_columns = columns_of("users")
    budget_columns = columns_of("budgets")
    notification_defaults = {"notify_budget": 1, "notify_social": 1, "notify_monthly": 0}

    with db.engine.begin() as connection:
        if expense_columns is not None:
            if "expense_type" not in expense_columns:
                connection.execute(text(
                    "ALTER TABLE expenses ADD COLUMN expense_type VARCHAR(20) NOT NULL DEFAULT 'Luxury'"
                ))
                if "is_essential" in expense_columns:
                    connection.execute(text(
                        "UPDATE expenses SET expense_type = 'Essential' WHERE is_essential = 1"
                    ))
                connection.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_expenses_expense_type ON expenses (expense_type)"
                ))
            if "is_essential" in expense_columns:
                connection.execute(text("ALTER TABLE expenses DROP COLUMN is_essential"))

        if planned_columns is not None and "description" not in planned_columns:
            connection.execute(text("ALTER TABLE planned_expenses ADD COLUMN description VARCHAR(255)"))

        if user_columns is not None:
            for column, default in notification_defaults.items():
                if column not in user_columns:
                    connection.execute(text(
                        f"ALTER TABLE users ADD COLUMN {column} BOOLEAN NOT NULL DEFAULT {default}"
                    ))

        if budget_columns is not None and "description" not in budget_columns:
            connection.execute(text("ALTER TABLE budgets ADD COLUMN description VARCHAR(255)"))
