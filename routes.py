"""
SaveSip - Routes.

Every query that reads or writes financial records is filtered by
current_user.id, so users only ever see and change their own data.
"""

import calendar
import csv
import hashlib
import io
import re
import time
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from flask import Response, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFError
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app import app, bcrypt, db
from forms import (
    BUDGET_YEARS,
    BudgetForm,
    ExpenseForm,
    IncomeForm,
    LoginForm,
    NotificationSettingsForm,
    PlannedExpenseForm,
    RegistrationForm,
    RequestResetForm,
    ResetPasswordForm,
    local_today,
)
from models import (
    CATEGORIES,
    EXPENSE_TYPE_LABELS,
    EXPENSE_TYPES,
    INCOME_SOURCES,
    PAYMENT_METHODS,
    SOCIAL_MEDIA_INFLUENCE_OPTIONS,
    SOCIAL_MEDIA_PLATFORMS,
    Budget,
    Expense,
    Income,
    PlannedExpense,
    User,
)


ZERO = Decimal("0.00")

# Budgets at or above this percentage of their limit trigger a warning
BUDGET_WARNING_PERCENT = 80

# Used when a login email does not exist, so the response takes as long as a
# real password check and does not reveal which emails are registered.
_DUMMY_PASSWORD_HASH = bcrypt.generate_password_hash("savesip-dummy-password").decode("utf-8")

PLATFORM_TIPS = {
    "TikTok": (
        "TikTok Shop videos are designed to make you buy on the spot. Try adding "
        "items to your cart and waiting 24 hours before checking out."
    ),
    "Instagram": (
        "Influencer posts and sponsored ads make products feel like must-haves. "
        "Consider muting shopping accounts that often tempt you."
    ),
    "Shopee Live": (
        "Live-stream flash deals and countdown timers push quick decisions. Set a "
        "spending limit before joining a live session and stick to it."
    ),
    "Other": (
        "Sponsored posts and in-app shops on any platform are built to make buying "
        "effortless. Turn off shopping notifications and unfollow accounts that "
        "keep tempting you."
    ),
}

# How each platform is named inside a sentence ("Other" is any platform not listed)
PLATFORM_NAMES = {"Other": "other social media"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def month_bounds(year, month):
    """Return (first day of the month, first day of the next month)."""
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def shift_month(year, month, offset):
    """Move (year, month) forward or backward by offset months."""
    index = year * 12 + (month - 1) + offset
    return index // 12, index % 12 + 1


def requested_period():
    """Read ?month=&year= from the URL, falling back to the current month."""
    today = local_today()
    month = request.args.get("month", type=int)
    year = request.args.get("year", type=int)
    if month is None or year is None or not 1 <= month <= 12 or not 2000 <= year <= 2100:
        return today.year, today.month
    return year, month


def requested_range():
    """Read a custom date range ?from=YYYY-MM-DD&to=YYYY-MM-DD from the URL.

    Returns (first day, last day), both included, or None when no range was
    asked for. An incomplete or impossible range shows a warning and
    returns None, so the page falls back to the month view.
    """
    raw_from, raw_to = request.args.get("from"), request.args.get("to")
    if not raw_from and not raw_to:
        return None
    try:
        first, last = date.fromisoformat(raw_from), date.fromisoformat(raw_to)
    except (TypeError, ValueError):
        flash("Please choose both a From date and a To date.", "warning")
        return None
    if first > last:
        flash("The From date must be on or before the To date.", "warning")
        return None
    if first.year < 2000 or last.year > 2100:
        flash("Please choose dates between the years 2000 and 2100.", "warning")
        return None
    return first, last


# The custom range last applied on the Dashboard, kept in the session so the
# Dashboard reopens on it after visiting other pages. Choosing a month
# clears it, and so does logging in or out (the session is cleared).
RANGE_SESSION_KEY = "dashboard_range"


def saved_range():
    """The custom date range saved in the session, as (first, last), or None."""
    stored = session.get(RANGE_SESSION_KEY)
    try:
        first, last = (date.fromisoformat(value) for value in stored)
    except (TypeError, ValueError):
        session.pop(RANGE_SESSION_KEY, None)
        return None
    return (first, last) if first <= last else None


def format_months(first, last):
    """The months a date range touches, e.g. "October – November 2026"."""
    if (first.year, first.month) == (last.year, last.month):
        return f"{calendar.month_name[last.month]} {last.year}"
    first_name = calendar.month_name[first.month]
    if first.year != last.year:
        first_name += f" {first.year}"
    return f"{first_name} – {calendar.month_name[last.month]} {last.year}"


def format_range(first, last):
    """A date range for headings, e.g. "25 Oct – 25 Nov 2026"."""
    if first == last:
        return first.strftime("%d %b %Y")
    first_format = "%d %b" if first.year == last.year else "%d %b %Y"
    return f"{first.strftime(first_format)} – {last.strftime('%d %b %Y')}"


def to_money(value):
    """Convert a database total (Decimal, float or None) to a 2dp Decimal."""
    if value is None:
        return ZERO
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(Decimal("0.01"))


@app.template_filter("rm")
def format_rm(value):
    """Format an amount as Malaysian Ringgit, e.g. RM1,234.50 or -RM5.00."""
    amount = to_money(value)
    sign = "-" if amount < 0 else ""
    return f"{sign}RM{abs(amount):,.2f}"


def total_for(model, start, end, *criteria):
    """Sum of the current user's amounts for model between start and end."""
    stmt = select(func.sum(model.amount)).where(
        model.user_id == current_user.id,
        model.date >= start,
        model.date < end,
        *criteria,
    )
    return to_money(db.session.scalar(stmt))


def totals_by(model, column, start, end, keys):
    """Current user's totals grouped by column, with every key in keys present."""
    stmt = (
        select(column, func.sum(model.amount))
        .where(model.user_id == current_user.id, model.date >= start, model.date < end)
        .group_by(column)
    )
    totals = {key: ZERO for key in keys}
    for key, amount in db.session.execute(stmt):
        totals[key] = to_money(amount)
    return totals


def expenses_for(start, end):
    """Current user's expenses between start and end, newest first."""
    stmt = (
        select(Expense)
        .where(Expense.user_id == current_user.id, Expense.date >= start, Expense.date < end)
        .order_by(Expense.date.desc(), Expense.id.desc())
    )
    return db.session.scalars(stmt).all()


def incomes_for(start, end):
    """Current user's incomes between start and end, newest first."""
    stmt = (
        select(Income)
        .where(Income.user_id == current_user.id, Income.date >= start, Income.date < end)
        .order_by(Income.date.desc(), Income.id.desc())
    )
    return db.session.scalars(stmt).all()


def budget_status(year, month, spent_by_category):
    """Compare the current user's budgets for a month with what they spent."""
    budgets = db.session.scalars(
        select(Budget).where(
            Budget.user_id == current_user.id,
            Budget.month == month,
            Budget.year == year,
        )
    ).all()
    budgets.sort(key=lambda budget: CATEGORIES.index(budget.category))
    return [
        _budget_row(budget.category, budget.amount, spent_by_category.get(budget.category, ZERO),
                    budget.description, budget_id=budget.id)
        for budget in budgets
    ]


def budget_status_for_range(first, last, spent_by_category):
    """Budgets for a custom date range: each category's budgets for every
    month the range touches, added together, compared with what was spent
    in the range. E.g. 25 Oct - 30 Nov combines the October and November
    budgets."""
    # Months counted as year * 12 + month, so "between" works across years
    month_index = Budget.year * 12 + Budget.month
    budgets = db.session.scalars(
        select(Budget).where(
            Budget.user_id == current_user.id,
            month_index.between(first.year * 12 + first.month, last.year * 12 + last.month),
        ).order_by(Budget.year, Budget.month)
    ).all()

    combined = {}
    for budget in budgets:
        entry = combined.setdefault(budget.category, {"amount": ZERO, "notes": [], "months": []})
        entry["amount"] += budget.amount
        entry["months"].append(calendar.month_abbr[budget.month])
        if budget.description:
            entry["notes"].append(budget.description)

    return [
        _budget_row(
            category, entry["amount"], spent_by_category.get(category, ZERO),
            " · ".join(entry["notes"]) or None,
            months=" + ".join(entry["months"]),
        )
        for category, entry in sorted(combined.items(), key=lambda item: CATEGORIES.index(item[0]))
    ]


def _budget_row(category, amount, spent, description, budget_id=None, months=None):
    """One budget compared with what was spent, as shown in the budget lists.
    months names the months combined in a custom range (e.g. "Oct + Nov")."""
    percent = int(spent / amount * 100)
    over = spent > amount
    # Compared exactly (not with the rounded-down percent), so RM80.00 of
    # a RM100.00 budget always counts as 80%
    reached_warning = spent * 100 >= amount * BUDGET_WARNING_PERCENT
    return {
        "id": budget_id,
        "category": category,
        "budget": amount,
        "spent": spent,
        "remaining": amount - spent,
        "percent": percent,
        "over": over,
        "near_limit": not over and reached_warning,
        "description": description,
        "months": months,
    }


def chart_series(totals, labels=None):
    """Turn a {key: Decimal} dict into JSON-friendly lists for Chart.js."""
    return {
        "labels": labels or [str(key) for key in totals],
        "values": [float(amount) for amount in totals.values()],
    }


def safe_next_url(target):
    """Allow only relative paths on this site as ?next= redirects."""
    if not target:
        return None
    target = target.replace("\\", "/")
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def sum_amounts(expenses):
    return sum((expense.amount for expense in expenses), ZERO)


def share_of(amount, total):
    return round(amount / total * 100) if total else 0


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    flash("Your form expired or could not be verified. Please try again.", "warning")
    if request.endpoint in (None, "logout"):
        return redirect(url_for("index"))
    return redirect(request.path)


@app.errorhandler(404)
def page_not_found(error):
    return render_template(
        "error.html",
        code=404,
        title="Page not found",
        message="The page you are looking for does not exist.",
    ), 404


@app.errorhandler(500)
def internal_server_error(error):
    db.session.rollback()
    return render_template(
        "error.html",
        code=500,
        title="Something went wrong",
        message="An unexpected error occurred. Please try again.",
    ), 500


# ---------------------------------------------------------------------------
# Core routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/dashboard")
@login_required
def dashboard():
    # The period being viewed: a custom date range (?from=&to=, e.g. income
    # received on 25 Oct that lasts until 25 Nov), otherwise a month
    # (?month=&year=, the current month by default). start is included,
    # end is not.
    #
    # An applied range is saved in the session (sticky): a plain /dashboard
    # visit, e.g. from the sidebar, reopens it. Choosing a month clears it.
    today = local_today()
    custom_range = requested_range()
    if custom_range:
        session[RANGE_SESSION_KEY] = [custom_range[0].isoformat(), custom_range[1].isoformat()]
    elif "month" in request.args or "year" in request.args:
        session.pop(RANGE_SESSION_KEY, None)
    elif not request.args:
        custom_range = saved_range()

    if custom_range:
        first, last = custom_range
        start, end = first, last + timedelta(days=1)
        year, month = last.year, last.month  # the month selector shows the To month
        period_label = format_range(first, last)
    else:
        year, month = requested_period()
        start, end = month_bounds(year, month)
        period_label = f"{calendar.month_name[month]} {year}"

    income_by_source = totals_by(Income, Income.source, start, end, INCOME_SOURCES)
    total_income = sum(income_by_source.values(), ZERO)
    spent_by_category = totals_by(Expense, Expense.category, start, end, CATEGORIES)
    total_expenses = sum(spent_by_category.values(), ZERO)
    type_totals = totals_by(Expense, Expense.expense_type, start, end, EXPENSE_TYPES)
    balance = total_income - total_expenses
    spent_by_influence = totals_by(
        Expense, Expense.social_media_influence, start, end, SOCIAL_MEDIA_INFLUENCE_OPTIONS
    )
    social_media_by_platform = {
        platform: spent_by_influence[platform] for platform in SOCIAL_MEDIA_PLATFORMS
    }
    social_media_total = sum(social_media_by_platform.values(), ZERO)

    # Every income and expense in the period, newest first
    incomes = incomes_for(start, end)
    expenses = expenses_for(start, end)

    # Budgets and planned expenses. In a custom range: the budgets of every
    # month the range touches (combined) and the plans due within the range.
    # In the month view: that month's budgets and the plans due from today.
    if custom_range:
        budgets = budget_status_for_range(first, last, spent_by_category)
        budget_label = format_months(first, last)
        plans = upcoming_planned(first=first, last=last)
    else:
        budgets = budget_status(year, month, spent_by_category)
        budget_label = f"{calendar.month_name[month]} {year}"
        plans = upcoming_planned()

    prev_year, prev_month = shift_month(year, month, -1)
    next_year, next_month = shift_month(year, month, 1)

    return render_template(
        "dashboard.html",
        month=month,
        month_name=calendar.month_name[month],
        year=year,
        period_label=period_label,
        is_range=custom_range is not None,
        # "Back to this month" sends the month explicitly, which clears a saved range
        this_month={"year": today.year, "month": today.month},
        # The custom range inputs start on the period being shown
        range_from=start.isoformat(),
        range_to=(end - timedelta(days=1)).isoformat(),
        is_current_month=custom_range is None and (year, month) == (today.year, today.month),
        prev_period={"year": prev_year, "month": prev_month},
        next_period={"year": next_year, "month": next_month},
        total_income=total_income,
        total_expenses=total_expenses,
        balance=balance,
        balance_share=share_of(max(balance, ZERO), total_income),
        income_source_count=sum(1 for amount in income_by_source.values() if amount),
        type_totals=type_totals,
        social_media_total=social_media_total,
        # Small donut charts beside the three summary figures
        summary_charts={
            "income": chart_series(income_by_source),
            "expenses": chart_series(
                type_totals, labels=[EXPENSE_TYPE_LABELS[key] for key in type_totals]
            ),
            "balance": chart_series(
                {"Spent": total_expenses, "Left": max(balance, ZERO)}
            ),
        },
        budget_status=budgets,
        budget_label=budget_label,
        # The 5 most recent records of the period being viewed
        recent_expenses=expenses[:5],
        recent_incomes=incomes[:5],
        category_chart=chart_series(spent_by_category),
        social_media_chart=chart_series(social_media_by_platform),
        planned=plans[:5],
        planned_count=len(plans),
        planned_total=sum((item["plan"].amount for item in plans), ZERO),
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    # obj=current_user fills the switches with the saved preferences on GET
    form = NotificationSettingsForm(obj=current_user)
    if form.validate_on_submit():
        current_user.notify_budget = form.notify_budget.data
        current_user.notify_social = form.notify_social.data
        current_user.notify_monthly = form.notify_monthly.data
        db.session.commit()
        flash("Your notification settings have been saved.", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", form=form)


# ---------------------------------------------------------------------------
# Authentication routes
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(username=form.username.data, email=form.email.data)
        user.set_password(form.password.data)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            # Another sign-up took the same username/email at the same moment
            db.session.rollback()
            flash("That username or email is already registered.", "danger")
        else:
            flash("Account created! Please log in.", "success")
            return redirect(url_for("login"))

    return render_template("register.html", form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = db.session.scalar(select(User).where(User.email == form.email.data.lower()))

        if user is None:
            bcrypt.check_password_hash(_DUMMY_PASSWORD_HASH, form.password.data)
        elif user.check_password(form.password.data):
            next_url = safe_next_url(request.args.get("next"))

            # Start a fresh session so nothing from before login carries over
            session.clear()
            login_user(user, remember=False)
            # Remember Me: keep the session cookie after the browser closes.
            # The 30-minute inactivity timeout applies either way.
            session.permanent = form.remember.data
            session["last_active"] = time.time()

            flash(f"Welcome back, {user.username}!", "success")
            return redirect(next_url or url_for("dashboard"))

        flash("Invalid email or password.", "danger")

    return render_template("login.html", form=form)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Forgot password
# ---------------------------------------------------------------------------
# SaveSip has no email service, so sending the reset email is simulated: the
# link that would be emailed is shown in a message on the login page instead.

RESET_SENT_MESSAGE = "If an account exists for that email, a password reset link has been sent to it."


@app.route("/reset_request", methods=["GET", "POST"])
def reset_request():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    form = RequestResetForm()
    if form.validate_on_submit():
        user = db.session.scalar(select(User).where(User.email == form.email.data.lower()))
        if user:
            reset_url = url_for("reset_password", token=user.get_reset_token())
            # Markup lets the message contain a link; Markup.format escapes the URL
            flash(Markup(
                "{} <strong>For this demo</strong> (no real email is sent), "
                '<a href="{}" class="alert-link">click here to reset your password</a>. '
                "The link works once and expires in 30 minutes."
            ).format(RESET_SENT_MESSAGE, reset_url), "info")
        else:
            flash(RESET_SENT_MESSAGE, "info")
        return redirect(url_for("login"))

    return render_template("reset_request.html", form=form)


@app.route("/reset_password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    user = User.verify_reset_token(token)
    if user is None:
        flash("That reset link is invalid, has expired or has already been used. "
              "Please request a new one.", "warning")
        return redirect(url_for("reset_request"))

    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        db.session.commit()
        flash("Your password has been reset. You can now log in with your new password.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", form=form)


# ---------------------------------------------------------------------------
# Action routes
# ---------------------------------------------------------------------------

@app.route("/add_income", methods=["GET", "POST"])
@login_required
def add_income():
    form = IncomeForm()
    if form.validate_on_submit():
        income = Income(
            user_id=current_user.id,
            amount=form.amount.data,
            source=form.source.data,
            payment_method=form.payment_method.data,
            date=form.date.data,
            description=form.description.data or None,
        )
        db.session.add(income)
        db.session.commit()
        flash(f"Income of RM{income.amount:,.2f} from {income.source} "
              f"on {income.date.strftime('%d %b %Y')} added.", "success")
        return redirect(dashboard_url_for(income.date))

    return render_template("add_income.html", form=form)


@app.route("/add_expense", methods=["GET", "POST"])
@login_required
def add_expense():
    form = ExpenseForm()
    if form.validate_on_submit():
        expense = Expense(
            user_id=current_user.id,
            amount=form.amount.data,
            category=form.category.data,
            payment_method=form.payment_method.data,
            social_media_influence=form.social_media_influence.data,
            expense_type=form.expense_type.data,
            date=form.date.data,
            description=form.description.data or None,
        )
        db.session.add(expense)
        db.session.commit()
        flash(f"Expense of RM{expense.amount:,.2f} for {expense.category} "
              f"on {expense.date.strftime('%d %b %Y')} added.", "success")
        return redirect(dashboard_url_for(expense.date))

    return render_template("add_expense.html", form=form)


def dashboard_url_for(day):
    """A Dashboard view that includes day, so a newly added record is
    visible straight after saving it: the saved custom range if day is in
    it, otherwise the month containing day."""
    custom_range = saved_range()
    if custom_range:
        if custom_range[0] <= day <= custom_range[1]:
            return url_for("dashboard")
    elif (day.year, day.month) == (local_today().year, local_today().month):
        return url_for("dashboard")
    return url_for("dashboard", month=day.month, year=day.year)


def _owned_or_404(model, record_id):
    """Fetch one of the current user's records, or show 404 for anyone else's."""
    record = db.session.get(model, record_id)
    if record is None or record.user_id != current_user.id:
        abort(404)
    return record


def _back_url():
    """Where to return after editing or deleting: the page the user came from
    (?next= or the form's "next" field, if it is a page on this site), else
    the Dashboard."""
    return safe_next_url(request.values.get("next")) or url_for("dashboard")


@app.route("/edit_income/<int:income_id>", methods=["GET", "POST"])
@login_required
def edit_income(income_id):
    income = _owned_or_404(Income, income_id)
    form = IncomeForm(obj=income)  # pre-fills the form on GET
    if form.validate_on_submit():
        income.amount = form.amount.data
        income.source = form.source.data
        income.payment_method = form.payment_method.data
        income.date = form.date.data
        income.description = form.description.data or None
        db.session.commit()
        flash(f"Income of RM{income.amount:,.2f} from {income.source} "
              f"on {income.date.strftime('%d %b %Y')} updated.", "success")
        return redirect(_back_url())

    return render_template(
        "add_income.html",
        form=form,
        editing=True,
        form_action=url_for("edit_income", income_id=income.id, next=request.args.get("next")),
        cancel_url=_back_url(),
    )


@app.route("/delete_income/<int:income_id>", methods=["POST"])
@login_required
def delete_income(income_id):
    income = _owned_or_404(Income, income_id)
    message = f"Deleted income of RM{income.amount:,.2f} from {income.source}."
    db.session.delete(income)
    db.session.commit()
    flash(message, "info")
    return redirect(_back_url())


@app.route("/edit_expense/<int:expense_id>", methods=["GET", "POST"])
@login_required
def edit_expense(expense_id):
    expense = _owned_or_404(Expense, expense_id)
    form = ExpenseForm(obj=expense)  # pre-fills the form on GET
    if form.validate_on_submit():
        expense.amount = form.amount.data
        expense.category = form.category.data
        expense.payment_method = form.payment_method.data
        expense.social_media_influence = form.social_media_influence.data
        expense.expense_type = form.expense_type.data
        expense.date = form.date.data
        expense.description = form.description.data or None
        db.session.commit()
        flash(f"Expense of RM{expense.amount:,.2f} for {expense.category} "
              f"on {expense.date.strftime('%d %b %Y')} updated.", "success")
        return redirect(_back_url())

    return render_template(
        "add_expense.html",
        form=form,
        editing=True,
        form_action=url_for("edit_expense", expense_id=expense.id, next=request.args.get("next")),
        cancel_url=_back_url(),
    )


@app.route("/delete_expense/<int:expense_id>", methods=["POST"])
@login_required
def delete_expense(expense_id):
    expense = _owned_or_404(Expense, expense_id)
    message = f"Deleted expense of RM{expense.amount:,.2f} for {expense.category}."
    db.session.delete(expense)
    db.session.commit()
    flash(message, "info")
    return redirect(_back_url())


@app.route("/set_budget", methods=["GET", "POST"])
@login_required
def set_budget():
    form = BudgetForm()

    if form.validate_on_submit():
        year, month = form.year.data, form.month.data
        budget = db.session.scalar(
            select(Budget).where(
                Budget.user_id == current_user.id,
                Budget.category == form.category.data,
                Budget.month == month,
                Budget.year == year,
            )
        )
        if budget:
            budget.amount = form.amount.data
            budget.description = form.description.data or None
            action = "updated"
        else:
            db.session.add(Budget(
                user_id=current_user.id,
                category=form.category.data,
                amount=form.amount.data,
                month=month,
                year=year,
                description=form.description.data or None,
            ))
            action = "set"

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("That budget was just changed. Please try again.", "warning")
        else:
            flash(
                f"{form.category.data} budget for {calendar.month_name[month]} {year} "
                f"{action} to RM{form.amount.data:,.2f}.",
                "success",
            )
        return redirect(url_for("set_budget", month=month, year=year))

    # Show the budgets for the month being viewed (?month=&year=, default now)
    year, month = requested_period()
    if request.method == "GET":
        form.month.data = month
        if year in BUDGET_YEARS:
            form.year.data = year

    return _budget_page(form, year, month)


def _budget_page(form, year, month, **extra):
    """The Set Budget page: the form, plus the budgets for year/month."""
    start, end = month_bounds(year, month)
    spent_by_category = totals_by(Expense, Expense.category, start, end, CATEGORIES)
    return render_template(
        "set_budget.html",
        form=form,
        month=month,
        month_name=calendar.month_name[month],
        year=year,
        budget_status=budget_status(year, month, spent_by_category),
        **extra,
    )


def _budget_back_url(budget):
    """After editing or deleting a budget: the page the user came from, else
    the Set Budget page for that budget's month."""
    return safe_next_url(request.values.get("next")) or url_for(
        "set_budget", month=budget.month, year=budget.year
    )


@app.route("/edit_budget/<int:budget_id>", methods=["GET", "POST"])
@login_required
def edit_budget(budget_id):
    budget = _owned_or_404(Budget, budget_id)
    form = BudgetForm(obj=budget)  # pre-fills the form on GET

    if form.validate_on_submit():
        category, month, year = form.category.data, form.month.data, form.year.data
        # One budget per category per month: moving this one onto a category
        # and month that already has a budget would clash with it
        clash = db.session.scalar(
            select(Budget).where(
                Budget.user_id == current_user.id,
                Budget.category == category,
                Budget.month == month,
                Budget.year == year,
                Budget.id != budget.id,
            )
        )
        if clash:
            form.category.errors.append(
                f"You already have a {category} budget for {calendar.month_name[month]} "
                f"{year}. Edit that one instead."
            )
        else:
            budget.category = category
            budget.amount = form.amount.data
            budget.month = month
            budget.year = year
            budget.description = form.description.data or None
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("That budget was just changed. Please try again.", "warning")
            else:
                flash(f"{category} budget for {calendar.month_name[month]} {year} "
                      f"updated to RM{budget.amount:,.2f}.", "success")
            return redirect(_budget_back_url(budget))

    return _budget_page(
        form,
        budget.year,
        budget.month,
        editing=True,
        editing_id=budget.id,
        form_action=url_for("edit_budget", budget_id=budget.id, next=request.args.get("next")),
        cancel_url=_budget_back_url(budget),
    )


@app.route("/delete_budget/<int:budget_id>", methods=["POST"])
@login_required
def delete_budget(budget_id):
    budget = _owned_or_404(Budget, budget_id)
    back_url = _budget_back_url(budget)
    message = (f"Deleted the {budget.category} budget for "
               f"{calendar.month_name[budget.month]} {budget.year}.")
    db.session.delete(budget)
    db.session.commit()
    flash(message, "info")
    return redirect(back_url)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@app.route("/reports")
@login_required
def reports():
    year, month = requested_period()
    start, end = month_bounds(year, month)

    income_by_source = totals_by(Income, Income.source, start, end, INCOME_SOURCES)
    expense_by_category = totals_by(Expense, Expense.category, start, end, CATEGORIES)
    expense_by_payment = totals_by(
        Expense, Expense.payment_method, start, end, PAYMENT_METHODS
    )
    expense_by_influence = totals_by(
        Expense, Expense.social_media_influence, start, end, SOCIAL_MEDIA_INFLUENCE_OPTIONS
    )
    type_totals = totals_by(Expense, Expense.expense_type, start, end, EXPENSE_TYPES)

    days = [start + timedelta(days=offset) for offset in range((end - start).days)]
    daily_spending = totals_by(Expense, Expense.date, start, end, days)

    # Income vs expenses for the selected month and the 5 months before it
    trend_labels, trend_income, trend_expenses = [], [], []
    for offset in range(-5, 1):
        trend_year, trend_month = shift_month(year, month, offset)
        trend_start, trend_end = month_bounds(trend_year, trend_month)
        trend_labels.append(f"{calendar.month_abbr[trend_month]} {trend_year}")
        trend_income.append(float(total_for(Income, trend_start, trend_end)))
        trend_expenses.append(float(total_for(Expense, trend_start, trend_end)))

    total_income = sum(income_by_source.values(), ZERO)
    total_expenses = sum(expense_by_category.values(), ZERO)

    charts = {
        "income_by_source": chart_series(income_by_source),
        "expense_by_category": chart_series(expense_by_category),
        "expense_by_payment": chart_series(expense_by_payment),
        "expense_by_influence": chart_series(expense_by_influence),
        "daily_spending": chart_series(
            daily_spending, labels=[str(day.day) for day in days]
        ),
        "trend": {
            "labels": trend_labels,
            "income": trend_income,
            "expenses": trend_expenses,
        },
    }

    prev_year, prev_month = shift_month(year, month, -1)
    next_year, next_month = shift_month(year, month, 1)

    return render_template(
        "reports.html",
        month=month,
        year=year,
        month_name=calendar.month_name[month],
        prev_period={"year": prev_year, "month": prev_month},
        next_period={"year": next_year, "month": next_month},
        total_income=total_income,
        total_expenses=total_expenses,
        balance=total_income - total_expenses,
        income_by_source=income_by_source,
        expense_by_category=expense_by_category,
        expense_by_payment=expense_by_payment,
        expense_by_influence=expense_by_influence,
        type_totals=type_totals,
        budget_status=budget_status(year, month, expense_by_category),
        expenses=expenses_for(start, end),
        incomes=incomes_for(start, end),
        charts=charts,
    )


def _spreadsheet_safe(text):
    """Stop user text from running as a formula when the file is opened in
    Excel (CSV injection): text starting with = + - @ is prefixed with '."""
    text = text or ""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.route("/reports/export")
@login_required
def export_report():
    """Download the month's transactions as a CSV file that opens in Excel."""
    year, month = requested_period()
    start, end = month_bounds(year, month)
    expenses = expenses_for(start, end)
    incomes = incomes_for(start, end)

    rows = []
    for expense in expenses:
        rows.append((expense.date, expense.id, [
            expense.date.isoformat(), "Expense", expense.category, expense.type_label,
            expense.payment_method,
            expense.social_media_influence if expense.is_social_media_influenced else "",
            _spreadsheet_safe(expense.description), f"{expense.amount:.2f}",
        ]))
    for income in incomes:
        rows.append((income.date, income.id, [
            income.date.isoformat(), "Income", income.source, "", income.payment_method, "",
            _spreadsheet_safe(income.description), f"{income.amount:.2f}",
        ]))
    rows.sort(key=lambda row: (row[0], row[1]))

    total_income = sum((income.amount for income in incomes), ZERO)
    total_expenses = sum_amounts(expenses)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([f"SaveSip transactions - {calendar.month_name[month]} {year}"])
    writer.writerow([])
    writer.writerow(["Date", "Type", "Category / Source", "Expense Type", "Payment Method",
                     "Social Media", "Notes", "Amount (RM)"])
    writer.writerows(row[2] for row in rows)
    writer.writerow([])
    writer.writerow(["", "", "", "", "", "", "Total income", f"{total_income:.2f}"])
    writer.writerow(["", "", "", "", "", "", "Total expenses", f"{total_expenses:.2f}"])
    writer.writerow(["", "", "", "", "", "", "Balance", f"{total_income - total_expenses:.2f}"])

    filename = f"SaveSip-Transactions-{year}-{month:02d}.csv"
    # The byte-order mark makes Excel read the file as UTF-8
    return Response(
        "\ufeff" + buffer.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Planned expenses
# ---------------------------------------------------------------------------

# Planned expenses due within this many days are highlighted as reminders
REMINDER_DAYS = 7


def _due_label(days_left):
    if days_left < -1:
        return f"Was due {-days_left} days ago"
    if days_left == -1:
        return "Was due yesterday"
    if days_left == 0:
        return "Due today"
    if days_left == 1:
        return "Due tomorrow"
    return f"Due in {days_left} days"


def upcoming_planned(limit=None, first=None, last=None):
    """The current user's planned expenses, soonest first.

    By default those due today or later. With first and last (a custom
    date range on the Dashboard), those due between the two dates instead,
    including ones already past.

    Each item is a dict with the PlannedExpense ("plan"), days_left, a
    due_label and is_reminder (due within the next REMINDER_DAYS).
    """
    today = local_today()
    if first and last:
        due = PlannedExpense.target_date.between(first, last)
    else:
        due = PlannedExpense.target_date >= today
    stmt = (
        select(PlannedExpense)
        .where(PlannedExpense.user_id == current_user.id, due)
        .order_by(PlannedExpense.target_date, PlannedExpense.id)
    )
    if limit:
        stmt = stmt.limit(limit)

    items = []
    for plan in db.session.scalars(stmt):
        days_left = (plan.target_date - today).days
        items.append({
            "plan": plan,
            "days_left": days_left,
            "due_label": _due_label(days_left),
            "is_reminder": 0 <= days_left <= REMINDER_DAYS,
        })
    return items


@app.route("/add_planned_expense", methods=["GET", "POST"])
@login_required
def add_planned_expense():
    form = PlannedExpenseForm()
    if form.validate_on_submit():
        plan = PlannedExpense(
            user_id=current_user.id,
            title=form.title.data,
            amount=form.amount.data,
            category=form.category.data,
            target_date=form.target_date.data,
            description=form.description.data or None,
        )
        db.session.add(plan)
        db.session.commit()
        flash(f'Planned "{plan.title}" for {plan.target_date.strftime("%d %b %Y")} '
              f"(RM{plan.amount:,.2f}).", "success")
        return redirect(url_for("dashboard"))

    return _planned_page(form)


def _planned_page(form, **extra):
    """The Planned Expenses page: the form, plus every upcoming plan."""
    planned = upcoming_planned()
    return render_template(
        "planned_expense.html",
        form=form,
        planned=planned,
        planned_total=sum((item["plan"].amount for item in planned), ZERO),
        today=local_today().isoformat(),
        **extra,
    )


def _planned_back_url():
    """After editing or deleting a plan: the page the user came from, else the
    Planned Expenses page."""
    return safe_next_url(request.values.get("next")) or url_for("add_planned_expense")


@app.route("/edit_planned/<int:plan_id>", methods=["GET", "POST"])
@login_required
def edit_planned(plan_id):
    plan = _owned_or_404(PlannedExpense, plan_id)
    form = PlannedExpenseForm(obj=plan)  # pre-fills the form on GET
    if form.validate_on_submit():
        plan.title = form.title.data
        plan.amount = form.amount.data
        plan.category = form.category.data
        plan.target_date = form.target_date.data
        plan.description = form.description.data or None
        db.session.commit()
        flash(f'Updated "{plan.title}": {plan.target_date.strftime("%d %b %Y")} '
              f"(RM{plan.amount:,.2f}).", "success")
        return redirect(_planned_back_url())

    return _planned_page(
        form,
        editing=True,
        editing_id=plan.id,
        form_action=url_for("edit_planned", plan_id=plan.id, next=request.args.get("next")),
        cancel_url=_planned_back_url(),
    )


@app.route("/delete_planned/<int:plan_id>", methods=["POST"])
@login_required
def delete_planned(plan_id):
    plan = _owned_or_404(PlannedExpense, plan_id)
    message = f'Removed the planned expense "{plan.title}".'
    db.session.delete(plan)
    db.session.commit()
    flash(message, "info")
    return redirect(_planned_back_url())


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

NOTIFICATION_LIMIT = 3

# Days before the end of the month when the "review your report" reminder
# appears (only for users who turned on Monthly report reminders)
MONTHLY_REMINDER_DAYS = 7

# Reasons a user can write in an expense's notes. A Want, or anything found
# through social media, whose note matches one of these was planned, so it
# is never flagged as an impulse buy. Checked in this order. Each entry is
# (label shown in the interface, words to look for, phrase for the message).
NOTE_REASONS = [
    ("Emergency", ["emergency", "urgent", "broken", "repair", "replace", "replacement"],
     "this was an emergency"),
    ("Health", ["medicine", "medical", "clinic", "hospital", "doctor", "pharmacy", "sick"],
     "this was for your health"),
    ("Planned gift", ["gift", "gifts", "present", "hadiah", "mom", "mum", "mother",
                      "dad", "father", "parents", "family"],
     "this was a planned gift"),
    ("Study", ["study", "studying", "revision", "class", "lecture", "assignment", "exam",
               "library", "textbook", "book", "books", "course", "project"],
     "this was for your studies"),
    ("Work", ["work", "interview", "internship"], "this was for work"),
    ("Clear purpose", ["cafe", "café", "birthday", "planned", "needed", "required"], "you had a clear reason"),
]
_NOTE_REASON_PATTERNS = [
    (label, re.compile(r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\b", re.IGNORECASE), phrase)
    for label, words, phrase in NOTE_REASONS
]


def note_reason(expense):
    """The reason found in an expense's notes as (label, phrase), or None."""
    if not expense.description:
        return None
    for label, pattern, phrase in _NOTE_REASON_PATTERNS:
        if pattern.search(expense.description):
            return label, phrase
    return None


def _suggestion(level, title, message, expenses=None, notify=False, url=None, kind=None):
    """One suggestion card.

    level is "positive", "warning", "info", "context" (a reason found in the
    user's notes) or "reminder" (mapped to colours in the template).
    notify=True also shows it under the navbar bell, linking to url (the
    Suggestions page by default). kind marks budget alerts ("budget_over",
    "budget_near") so the bell can list them first.
    """
    return {"level": level, "title": title, "message": message,
            "expenses": expenses or [], "notify": notify, "url": url, "kind": kind}


def _spent_on(expense):
    """What an expense was spent "on", for the start of a note card: the
    platform that influenced a want or unplanned cost, otherwise the category."""
    if expense.is_social_media_influenced and expense.expense_type != "Essential":
        platform = expense.social_media_influence
        return "social media" if platform == "Other" else platform
    return "miscellaneous items" if expense.category == "Other" else expense.category


def _note_card(expense, reason):
    """A suggestion card for one expense that has a note, quoting the note
    exactly as the user wrote it.

    The card keeps its parts separate so the template can highlight the
    note: lead ("We noticed you spent ..."), note (the user's own words) and
    verdict (what SaveSip made of it). message joins them into one sentence.
    """
    note = expense.description.strip()
    lead = f"We noticed you spent RM{expense.amount:,.2f} on {_spent_on(expense)}."
    platform = expense.social_media_influence

    if expense.expense_type == "Essential":
        level, label, outcome, flagged = "positive", "Essential", "Never flagged", False
        title = f"{expense.category}: a need, not a want"
        verdict = (f"Since {reason[1]} and you marked it as Essential, "
                   if reason else "You marked it as Essential, so ")
        verdict += "it counts as a need, not an impulse buy. Well done!"
        if expense.is_social_media_influenced:
            verdict += f" The {PLATFORM_NAMES.get(platform, platform)} influence is ignored for essentials."
    elif expense.expense_type == "Situational":
        level, label, outcome, flagged = "info", "Unplanned", "Never flagged", False
        title = f"Unplanned {expense.category} cost"
        if reason:
            verdict = (f"Since {reason[1]}, this is exactly the kind of surprise a small "
                       f"Savings cushion is for. Unplanned costs are never counted as impulse buys.")
        else:
            verdict = ("You marked it as Unplanned, so it isn't counted as an impulse buy. "
                       "Putting a little into Savings each month helps with the next surprise.")
    elif reason:
        level, label, outcome, flagged = "context", reason[0], "Not an impulse buy", False
        title = f"{reason[0]}: not an impulse buy"
        verdict = f"Since {reason[1]}, we won't flag it as an impulse buy!"
    else:
        level, label, outcome, flagged = "warning", "No clear reason", "Counted as an impulse buy", True
        if expense.is_social_media_influenced:
            where = "social media" if platform == "Other" else platform
            title = f"{where[0].upper() + where[1:]} purchase counted as an impulse buy"
            counted = f"it counts toward your {where} impulse buys"
        else:
            title = "Counted as a want"
            counted = "it counts as a want this month"
        verdict = (f"We couldn't find a reason such as a gift, your studies or an emergency "
                   f"in it, so {counted}. If it was planned, edit the note to say why.")

    after_quote = "" if note[-1] in ".!?" else "."
    card = _suggestion(level, title, f"{lead} Your note says “{note}”{after_quote} {verdict}", [expense])
    card.update({"expense": expense, "lead": lead, "note": note, "verdict": verdict,
                 "label": label, "outcome": outcome, "flagged": flagged})
    return card


def build_suggestions(year, month):
    """Personalised suggestions from the current user's expenses in a month.

    Every expense's notes (description) are read:
    - Each expense with a note gets its own card that quotes the note word
      for word and says how it was treated. A Want whose note gives a reason
      (NOTE_REASONS, e.g. "Bought a gift for my mom") is never flagged as an
      impulse buy, even when social media influenced it.
    - The note also decides whether a Want counts as an impulse buy in the
      summary cards below.

    Summary cards:
    - Essentials: positive reinforcement; social media influence is ignored
      for them.
    - Wants influenced by a platform (with no reason in their notes): an
      impulse-buying warning for that platform. Remaining wants: a general
      impulse-buying warning.
    - Unplanned (Situational): a tip about keeping a cushion for surprises;
      never treated as impulse buying.
    - Budgets: a warning when exceeded, and another at 80% or more of the
      budget. Both always reach the navbar bell (when budget alerts are on).
    - Monthly report reminder in the last week of the month.

    The user's notification settings switch off social media warnings
    (notify_social), budget alerts (notify_budget) and the monthly reminder
    (notify_monthly).

    Used by the Suggestions page and the navbar notifications. Returns the
    summary items, the note cards and the month's summary figures.
    """
    start, end = month_bounds(year, month)
    expenses = expenses_for(start, end)
    show_social = current_user.notify_social
    show_budget = current_user.notify_budget

    needs, unplanned, other_wants = [], [], []
    impulse_by_platform = {platform: [] for platform in SOCIAL_MEDIA_PLATFORMS}
    note_cards = []

    for expense in expenses:
        reason = note_reason(expense)
        if expense.description and expense.description.strip():
            note_cards.append(_note_card(expense, reason))

        if expense.expense_type == "Essential":
            needs.append(expense)
        elif expense.expense_type == "Situational":
            unplanned.append(expense)
        elif reason:
            pass  # a planned want: its note card explains why it is not flagged
        elif expense.is_social_media_influenced:
            impulse_by_platform[expense.social_media_influence].append(expense)
        else:
            other_wants.append(expense)

    total_spent = sum_amounts(expenses)
    impulse_total = sum(
        (sum_amounts(items) for items in impulse_by_platform.values()), ZERO
    )
    items = []

    if needs:
        needs_total = sum_amounts(needs)
        by_category = {}
        for expense in needs:
            by_category[expense.category] = (
                by_category.get(expense.category, ZERO) + expense.amount
            )
        top_categories = sorted(by_category, key=by_category.get, reverse=True)[:3]
        items.append(_suggestion(
            "positive",
            "Great job prioritising your essentials",
            f"You spent RM{needs_total:,.2f} on {plural(len(needs), 'essential')} "
            f"this month, mainly on {', '.join(top_categories)}. "
            f"That is {share_of(needs_total, total_spent)}% of your spending going "
            f"to things you actually need. Keep it up!",
            needs,
        ))

    if show_social:
        for platform, platform_expenses in impulse_by_platform.items():
            if not platform_expenses:
                continue
            platform_total = sum_amounts(platform_expenses)
            name = PLATFORM_NAMES.get(platform, platform)
            items.append(_suggestion(
                "warning",
                f"Watch out for {name} impulse buying",
                f"You bought {plural(len(platform_expenses), 'want')} "
                f"influenced by {name} this month, totalling RM{platform_total:,.2f} "
                f"({share_of(platform_total, total_spent)}% of your spending). "
                f"{PLATFORM_TIPS[platform]}",
                platform_expenses,
                notify=True,
            ))

    if other_wants:
        other_total = sum_amounts(other_wants)
        items.append(_suggestion(
            "warning",
            "Think twice before buying wants",
            f"You spent RM{other_total:,.2f} on "
            f"{plural(len(other_wants), 'other want')} this month. "
            f"Social media is not the only trigger for impulse buying. Before your next "
            f"want, ask yourself whether you would still buy it in a week.",
            other_wants,
            notify=True,
        ))

    if unplanned:
        unplanned_total = sum_amounts(unplanned)
        items.append(_suggestion(
            "info",
            "Unplanned costs this month",
            f"You had {plural(len(unplanned), 'unplanned expense')} totalling "
            f"RM{unplanned_total:,.2f} ({share_of(unplanned_total, total_spent)}% of your "
            f"spending). Surprises happen. Putting a little into Savings each month "
            f"gives you a cushion for the next one.",
            unplanned,
        ))

    if show_social and expenses and impulse_total == ZERO:
        items.append(_suggestion(
            "positive",
            "No social media impulse buys",
            "None of your wants this month were impulse buys from TikTok, "
            "Instagram, Shopee Live or any other platform. Nice self-control!",
        ))

    if show_budget:
        spent_by_category = totals_by(Expense, Expense.category, start, end, CATEGORIES)
        for status in budget_status(year, month, spent_by_category):
            if status["over"]:
                items.append(_suggestion(
                    "warning",
                    f"{status['category']} budget exceeded",
                    f"You have spent RM{status['spent']:,.2f} of your "
                    f"RM{status['budget']:,.2f} {status['category']} budget "
                    f"({status['percent']}%). Try to hold off on {status['category']} "
                    f"spending for the rest of the month.",
                    notify=True,
                    kind="budget_over",
                ))
            elif status["near_limit"]:
                items.append(_suggestion(
                    "warning",
                    f"{status['category']} budget {status['percent']}% used",
                    f"You have used {status['percent']}% of your {status['category']} "
                    f"budget, with RM{status['remaining']:,.2f} left for this month. "
                    f"Slow down on {status['category']} so you don't go over.",
                    notify=True,
                    kind="budget_near",
                ))

    today = local_today()
    days_left = (end - today).days - 1
    if current_user.notify_monthly and (today.year, today.month) == (year, month) \
            and days_left <= MONTHLY_REMINDER_DAYS:
        month_name = calendar.month_name[month]
        items.append(_suggestion(
            "reminder",
            f"Time to review your {month_name} report",
            f"{month_name} ends in {plural(days_left, 'day') if days_left else 'less than a day'}. "
            f"Take a minute to look over your report and plan next month's budgets.",
            notify=True,
            url=url_for("reports"),
        ))

    return {
        "items": items,
        "note_cards": note_cards,
        "total_spent": total_spent,
        "impulse_total": impulse_total,
        "expense_count": len(expenses),
    }


def _notification_rank(item):
    """Bell order: budget alerts first (exceeded, then 80% used), then
    reminders (planned expenses, monthly review), then other warnings."""
    if item["kind"] == "budget_over":
        return 0
    if item["kind"] == "budget_near":
        return 1
    return {"reminder": 2, "warning": 3}.get(item["level"], 4)


@app.context_processor
def inject_notifications():
    """Navbar bell: budget alerts, reminders and the user's top warnings."""
    if not current_user.is_authenticated:
        return {}
    try:
        today = local_today()
        items = build_suggestions(today.year, today.month)["items"]
        reminders = [
            _suggestion(
                "reminder",
                f"{item['plan'].title}: {item['due_label'].lower()}",
                f"RM{item['plan'].amount:,.2f} planned for {item['plan'].category}. "
                f"Set the money aside now.",
                notify=True,
                url=url_for("add_planned_expense"),
            )
            for item in upcoming_planned() if item["is_reminder"]
        ]
    except SQLAlchemyError:
        # Never let the bell break a page (including error pages)
        db.session.rollback()
        return {"notifications": [], "notification_count": 0, "notification_key": ""}

    alerts = reminders + [item for item in items if item["notify"]]
    alerts.sort(key=_notification_rank)
    # Fingerprint of the current alerts. Once the user opens the bell, the
    # badge stays hidden until this changes, i.e. until something new
    # happens (see base.html).
    key = hashlib.sha256(
        f"{current_user.id}|{'|'.join(item['title'] for item in alerts)}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "notifications": alerts[:NOTIFICATION_LIMIT],
        "notification_count": len(alerts),
        "notification_key": key if alerts else "",
    }


@app.route("/suggestions")
@login_required
def suggestions():
    today = local_today()
    result = build_suggestions(today.year, today.month)
    return render_template(
        "suggestions.html",
        month_name=calendar.month_name[today.month],
        year=today.year,
        suggestions=result["items"],
        note_cards=result["note_cards"],
        note_reasons=[(label, words[:4]) for label, words, phrase in NOTE_REASONS],
        total_spent=result["total_spent"],
        impulse_total=result["impulse_total"],
        expense_count=result["expense_count"],
    )
