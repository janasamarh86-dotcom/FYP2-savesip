"""
SaveSip - Flask-WTF forms.

All option lists come from models.py so the forms and the database always
accept exactly the same values. Every form inherits CSRF protection from
FlaskForm.
"""

import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask_wtf import FlaskForm
from sqlalchemy import func
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    EmailField,
    PasswordField,
    SelectChoice,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import (
    DataRequired,
    Email,
    EqualTo,
    Length,
    NumberRange,
    Optional,
    Regexp,
    StopValidation,
    ValidationError,
)

from models import (
    CATEGORIES,
    EXPENSE_TYPE_LABELS,
    EXPENSE_TYPES,
    INCOME_SOURCES,
    PAYMENT_METHODS,
    SOCIAL_MEDIA_INFLUENCE_OPTIONS,
    User,
)


# ---------------------------------------------------------------------------
# Shared settings
# ---------------------------------------------------------------------------

# Malaysia Time (UTC+8, no daylight saving). A fixed offset is used instead of
# zoneinfo because Windows has no built-in time zone database.
LOCAL_TZ = timezone(timedelta(hours=8), "MYT")

BUDGET_YEARS = [2026, 2027]

# bcrypt only uses the first 72 bytes of a password, and bcrypt 5.0 raises an
# error for anything longer.
BCRYPT_MAX_BYTES = 72

# Largest value that fits in a Numeric(10, 2) column
MAX_AMOUNT = Decimal("99999999.99")

def _with_placeholder(placeholder, choices):
    """Put an empty "Select ..." option first so nothing is pre-selected and
    the user has to pick on purpose. choices are values or SelectChoices."""
    return [SelectChoice(value="", label=placeholder)] + [
        choice if isinstance(choice, SelectChoice) else SelectChoice(value=choice, label=choice)
        for choice in choices
    ]


def _required_select(label, placeholder, choices, message):
    """A dropdown that starts empty and shows message (in red, under the
    field) if the form is saved without a choice. The message is also given
    to the browser (data-required-message) so the form is stopped before it
    is even sent; see base.html."""
    return SelectField(
        label,
        choices=_with_placeholder(placeholder, choices),
        validators=[DataRequired(message=message)],
        invalid_choice_message=message,
        render_kw={"data-required-message": message},
    )


# Essential / Want / Unplanned, stored as Essential / Luxury / Situational
EXPENSE_TYPE_CHOICES = [
    SelectChoice(value=value, label=EXPENSE_TYPE_LABELS[value]) for value in EXPENSE_TYPES
]


def local_today():
    """Today's date in Malaysia Time."""
    return datetime.now(LOCAL_TZ).date()


def current_month():
    return datetime.now(LOCAL_TZ).month


def current_budget_year():
    year = datetime.now(LOCAL_TZ).year
    return year if year in BUDGET_YEARS else BUDGET_YEARS[0]


def _strip(value):
    return value.strip() if isinstance(value, str) else value


# ---------------------------------------------------------------------------
# Custom validators
# ---------------------------------------------------------------------------

class MaxBytes:
    """Reject input longer than max_bytes once encoded as UTF-8.

    Length() counts characters, but bcrypt's limit is in bytes, and characters
    such as emoji or Chinese text take 3-4 bytes each.
    """

    def __init__(self, max_bytes, message=None):
        self.max_bytes = max_bytes
        self.message = message or f"Must be {max_bytes} bytes or fewer."

    def __call__(self, form, field):
        if field.data and len(field.data.encode("utf-8")) > self.max_bytes:
            raise ValidationError(self.message)


def finite_amount(form, field):
    """Reject "NaN" and "Infinity", which Python parses as valid Decimals.

    Stops the validation chain so later checks never compare against them.
    """
    if field.data is not None and not field.data.is_finite():
        raise StopValidation("Please enter a valid amount.")


def two_decimal_places(form, field):
    """Reject amounts with more than 2 decimal places (e.g. 12.345)."""
    if field.data is not None and field.data.as_tuple().exponent < -2:
        raise ValidationError("Amount can have at most 2 decimal places.")


def not_in_future(form, field):
    if field.data and field.data > local_today():
        raise ValidationError("Date cannot be in the future.")


def not_in_past(form, field):
    if field.data and field.data < local_today():
        raise ValidationError("Choose today or a future date.")


# Reused field definitions
def _amount_field():
    return DecimalField(
        "Amount (RM)",
        places=2,
        validators=[
            DataRequired(message="Please enter a valid amount."),
            finite_amount,
            NumberRange(min=Decimal("0.01"), max=MAX_AMOUNT,
                        message="Amount must be between RM0.01 and RM99,999,999.99."),
            two_decimal_places,
        ],
    )


def _date_field():
    return DateField(
        "Date",
        default=local_today,
        validators=[DataRequired(message="Please choose a date."), not_in_future],
    )


def _new_password_field(label="Password"):
    """Password rules for registering and for resetting a password."""
    return PasswordField(
        label,
        validators=[
            DataRequired(),
            Length(min=8, message="Password must be at least 8 characters."),
            MaxBytes(BCRYPT_MAX_BYTES, message="Password is too long (maximum 72 bytes)."),
            Regexp(r"^(?=.*[A-Za-z])(?=.*\d).+$",
                   message="Password must contain at least one letter and one number."),
        ],
    )


def _confirm_password_field(label="Confirm Password"):
    return PasswordField(
        label,
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )


# ---------------------------------------------------------------------------
# Authentication forms
# ---------------------------------------------------------------------------

class RegistrationForm(FlaskForm):
    username = StringField(
        "Username",
        filters=[_strip],
        validators=[
            DataRequired(),
            Length(min=3, max=30),
            Regexp(r"^[A-Za-z0-9_]+$",
                   message="Username can only contain letters, numbers and underscores."),
        ],
    )
    email = EmailField(
        "Email",
        filters=[_strip],
        validators=[DataRequired(), Email(), Length(max=120)],
    )
    password = _new_password_field()
    confirm_password = _confirm_password_field()
    submit = SubmitField("Register")

    def validate_username(self, field):
        exists = User.query.filter(
            func.lower(User.username) == field.data.lower()
        ).first()
        if exists:
            raise ValidationError("That username is already taken.")

    def validate_email(self, field):
        if User.query.filter_by(email=field.data.lower()).first():
            raise ValidationError("An account with that email already exists.")


class LoginForm(FlaskForm):
    email = EmailField(
        "Email",
        filters=[_strip],
        validators=[DataRequired(), Email(), Length(max=120)],
    )
    password = PasswordField(
        "Password",
        validators=[
            DataRequired(),
            MaxBytes(BCRYPT_MAX_BYTES, message="Invalid email or password."),
        ],
    )
    # Keeps the user signed in after closing the browser. The 30-minute
    # inactivity timeout still applies (see app.py).
    remember = BooleanField("Remember Me")
    submit = SubmitField("Log In")


class RequestResetForm(FlaskForm):
    """Forgot Password: the email of the account to reset."""
    email = EmailField(
        "Email",
        filters=[_strip],
        validators=[DataRequired(), Email(), Length(max=120)],
    )
    submit = SubmitField("Send Reset Link")


class ResetPasswordForm(FlaskForm):
    """Set a new password from a reset link (same rules as registration)."""
    password = _new_password_field("New Password")
    confirm_password = _confirm_password_field("Confirm New Password")
    submit = SubmitField("Reset Password")


# ---------------------------------------------------------------------------
# Transaction forms
# ---------------------------------------------------------------------------

class IncomeForm(FlaskForm):
    amount = _amount_field()
    source = _required_select(
        "Source", "Select a source", INCOME_SOURCES, "Please select an income source."
    )
    payment_method = SelectField(
        "Payment Method", choices=PAYMENT_METHODS, validators=[DataRequired()]
    )
    date = _date_field()
    description = TextAreaField(
        "Description",
        filters=[_strip],
        validators=[Optional(), Length(max=255)],
    )
    submit = SubmitField("Save Income")


class ExpenseForm(FlaskForm):
    amount = _amount_field()
    category = _required_select(
        "Category", "Select a category", CATEGORIES, "Please select a category."
    )
    payment_method = SelectField(
        "Payment Method", choices=PAYMENT_METHODS, validators=[DataRequired()]
    )
    social_media_influence = _required_select(
        "Social Media Influence",
        "Select a platform, or None",
        SOCIAL_MEDIA_INFLUENCE_OPTIONS,
        "Please select a social media influence (choose None if there was none).",
    )
    expense_type = _required_select(
        "Expense Type", "Select a type", EXPENSE_TYPE_CHOICES, "Please select an expense type."
    )
    date = _date_field()
    description = TextAreaField(
        "Notes / Description",
        filters=[_strip],
        validators=[Optional(), Length(max=255)],
    )
    submit = SubmitField("Save Expense")


class NotificationSettingsForm(FlaskForm):
    # Field names match the User columns, so NotificationSettingsForm(obj=user)
    # shows the saved preferences
    notify_budget = BooleanField("Budget alerts")
    notify_social = BooleanField("Social media impulse warnings")
    notify_monthly = BooleanField("Monthly report reminder")
    submit = SubmitField("Save preferences")


class PlannedExpenseForm(FlaskForm):
    title = StringField(
        "What is it for?",
        filters=[_strip],
        validators=[DataRequired(message="Give this planned expense a name."), Length(max=100)],
    )
    amount = _amount_field()
    category = SelectField(
        "Category", choices=CATEGORIES, validators=[DataRequired()]
    )
    target_date = DateField(
        "Due date",
        default=local_today,
        validators=[DataRequired(message="Please choose a date."), not_in_past],
    )
    description = TextAreaField(
        "Notes (optional)",
        filters=[_strip],
        validators=[Optional(), Length(max=255)],
    )
    submit = SubmitField("Save Planned Expense")


class BudgetForm(FlaskForm):
    category = SelectField(
        "Category", choices=CATEGORIES, validators=[DataRequired()]
    )
    amount = _amount_field()
    month = SelectField(
        "Month",
        coerce=int,
        choices=[
            SelectChoice(value=number, label=calendar.month_name[number])
            for number in range(1, 13)
        ],
        default=current_month,
        validators=[DataRequired()],
    )
    year = SelectField(
        "Year",
        coerce=int,
        choices=[SelectChoice(value=year, label=str(year)) for year in BUDGET_YEARS],
        default=current_budget_year,
        validators=[DataRequired()],
    )
    description = TextAreaField(
        "Note (optional)",
        filters=[_strip],
        validators=[Optional(), Length(max=255)],
    )
    submit = SubmitField("Save Budget")
