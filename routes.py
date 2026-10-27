"""
SaveSip - Routes.

Every query that reads or writes financial records is filtered by
current_user.id, so users only ever see and change their own data.
"""

import calendar
import re
import time
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from flask import abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFError
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
    local_today,
)
from models import (
    CATEGORIES,
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
}


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
    stmt = (
        select(Expense)
        .where(Expense.user_id == current_user.id, Expense.date >= start, Expense.date < end)
        .order_by(Expense.date.desc(), Expense.id.desc())
    )
    return db.session.scalars(stmt).all()


def incomes_for(start, end):
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

    status = []
    for budget in budgets:
        spent = spent_by_category.get(budget.category, ZERO)
        percent = int(spent / budget.amount * 100)
        over = spent > budget.amount
        status.append({
            "category": budget.category,
            "budget": budget.amount,
            "spent": spent,
            "remaining": budget.amount - spent,
            "percent": percent,
            "over": over,
            "near_limit": not over and percent >= BUDGET_WARNING_PERCENT,
        })
    return status


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
    today = local_today()
    start, end = month_bounds(today.year, today.month)

    total_income = total_for(Income, start, end)
    spent_by_category = totals_by(Expense, Expense.category, start, end, CATEGORIES)
    total_expenses = sum(spent_by_category.values(), ZERO)
    type_totals = totals_by(Expense, Expense.expense_type, start, end, EXPENSE_TYPES)
    spent_by_influence = totals_by(
        Expense, Expense.social_media_influence, start, end, SOCIAL_MEDIA_INFLUENCE_OPTIONS
    )
    social_media_by_platform = {
        platform: spent_by_influence[platform] for platform in SOCIAL_MEDIA_PLATFORMS
    }
    social_media_total = sum(social_media_by_platform.values(), ZERO)

    recent_expenses = db.session.scalars(
        select(Expense)
        .where(Expense.user_id == current_user.id)
        .order_by(Expense.date.desc(), Expense.id.desc())
        .limit(5)
    ).all()
    recent_incomes = db.session.scalars(
        select(Income)
        .where(Income.user_id == current_user.id)
        .order_by(Income.date.desc(), Income.id.desc())
        .limit(5)
    ).all()

    return render_template(
        "dashboard.html",
        month_name=calendar.month_name[today.month],
        year=today.year,
        total_income=total_income,
        total_expenses=total_expenses,
        balance=total_income - total_expenses,
        type_totals=type_totals,
        social_media_total=social_media_total,
        budget_status=budget_status(today.year, today.month, spent_by_category),
        recent_expenses=recent_expenses,
        recent_incomes=recent_incomes,
        category_chart=chart_series(spent_by_category),
        social_media_chart=chart_series(social_media_by_platform),
        planned=upcoming_planned(limit=5),
        planned_total=to_money(db.session.scalar(
            select(func.sum(PlannedExpense.amount)).where(
                PlannedExpense.user_id == current_user.id,
                PlannedExpense.target_date >= today,
            )
        )),
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
        flash(f"Income of RM{income.amount:,.2f} from {income.source} added.", "success")
        return redirect(url_for("dashboard"))

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
        flash(f"Expense of RM{expense.amount:,.2f} for {expense.category} added.", "success")
        return redirect(url_for("dashboard"))

    return render_template("add_expense.html", form=form)


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
            action = "updated"
        else:
            db.session.add(Budget(
                user_id=current_user.id,
                category=form.category.data,
                amount=form.amount.data,
                month=month,
                year=year,
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

    start, end = month_bounds(year, month)
    spent_by_category = totals_by(Expense, Expense.category, start, end, CATEGORIES)

    return render_template(
        "set_budget.html",
        form=form,
        month=month,
        month_name=calendar.month_name[month],
        year=year,
        budget_status=budget_status(year, month, spent_by_category),
    )


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


# ---------------------------------------------------------------------------
# Planned expenses
# ---------------------------------------------------------------------------

# Planned expenses due within this many days are highlighted as reminders
REMINDER_DAYS = 7


def _due_label(days_left):
    if days_left == 0:
        return "Due today"
    if days_left == 1:
        return "Due tomorrow"
    return f"Due in {days_left} days"


def upcoming_planned(limit=None):
    """The current user's planned expenses due today or later, soonest first.

    Each item is a dict with the PlannedExpense ("plan"), days_left, a
    due_label and is_reminder (due within REMINDER_DAYS).
    """
    today = local_today()
    stmt = (
        select(PlannedExpense)
        .where(PlannedExpense.user_id == current_user.id, PlannedExpense.target_date >= today)
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
            "is_reminder": days_left <= REMINDER_DAYS,
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

    planned = upcoming_planned()
    return render_template(
        "planned_expense.html",
        form=form,
        planned=planned,
        planned_total=sum((item["plan"].amount for item in planned), ZERO),
        today=local_today().isoformat(),
    )


@app.route("/planned_expense/<int:plan_id>/delete", methods=["POST"])
@login_required
def delete_planned_expense(plan_id):
    plan = db.session.get(PlannedExpense, plan_id)
    if plan is None or plan.user_id != current_user.id:
        abort(404)
    db.session.delete(plan)
    db.session.commit()
    flash(f'Removed the planned expense "{plan.title}".', "info")
    return redirect(url_for("add_planned_expense"))


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


def _quote(text, limit=120):
    """A user's note, tidied and shortened for use inside a sentence."""
    text = " ".join(text.split()).rstrip(".")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _suggestion(level, title, message, expenses=None, notify=False, url=None):
    """One suggestion card.

    level is "positive", "warning", "info", "context" (a reason found in the
    user's notes) or "reminder" (mapped to colours in the template).
    notify=True also shows it under the navbar bell, linking to url (the
    Suggestions page by default).
    """
    return {"level": level, "title": title, "message": message,
            "expenses": expenses or [], "notify": notify, "url": url}


def _context_note(expense, reason):
    """One entry for the Spending Context panel: a note we read and what it meant."""
    if expense.expense_type == "Essential":
        label, outcome, flagged = "Essential", "Never flagged", False
    elif expense.expense_type == "Situational":
        label, outcome, flagged = "Unplanned", "Never flagged", False
    elif reason:
        label, outcome, flagged = reason[0], "Not an impulse buy", False
    else:
        label, outcome, flagged = "No clear reason", "Counted as a want", True
    return {"expense": expense, "label": label, "outcome": outcome, "flagged": flagged}


def build_suggestions(year, month):
    """Personalised suggestions from the current user's expenses in a month.

    - Essentials: positive reinforcement; social media influence is ignored
      for them.
    - Wants whose notes give a reason (NOTE_REASONS, e.g. "gift for mom",
      "study cafe", "emergency"): one card per expense that quotes the note
      and explains why it is not an impulse buy, even when social media
      influenced it.
    - Other wants influenced by a platform: an impulse-buying warning for
      that platform. Remaining wants: a general impulse-buying warning.
    - Unplanned (Situational): a tip about keeping a cushion for surprises;
      never treated as impulse buying.
    - Budgets: a warning when exceeded, a note at 80% or more.
    - Monthly report reminder in the last week of the month.

    The user's notification settings switch off social media warnings
    (notify_social), budget alerts (notify_budget) and the monthly reminder
    (notify_monthly).

    Used by the Suggestions page and the navbar notifications. Returns the
    suggestion items, the notes that were read (for the Spending Context
    panel) and the month's summary figures.
    """
    start, end = month_bounds(year, month)
    expenses = expenses_for(start, end)
    show_social = current_user.notify_social
    show_budget = current_user.notify_budget

    needs, unplanned, other_wants, explained_wants = [], [], [], []
    impulse_by_platform = {platform: [] for platform in SOCIAL_MEDIA_PLATFORMS}
    context_notes = []

    for expense in expenses:
        reason = note_reason(expense)
        if expense.description and (expense.expense_type == "Luxury" or expense.is_social_media_influenced):
            context_notes.append(_context_note(expense, reason))

        if expense.expense_type == "Essential":
            needs.append(expense)
        elif expense.expense_type == "Situational":
            unplanned.append(expense)
        elif reason:
            explained_wants.append((expense, reason))
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

    # One card per explained want, quoting the user's own note
    for expense, (label, phrase) in explained_wants:
        where = expense.social_media_influence if expense.is_social_media_influenced else expense.category
        items.append(_suggestion(
            "context",
            f"{label}: not an impulse buy",
            f"We noticed you spent RM{expense.amount:,.2f} on {where}. "
            f"Your note says “{_quote(expense.description)}”. "
            f"Since {phrase}, we won't flag it as an impulse buy!",
            [expense],
        ))

    if show_social:
        for platform, platform_expenses in impulse_by_platform.items():
            if not platform_expenses:
                continue
            platform_total = sum_amounts(platform_expenses)
            items.append(_suggestion(
                "warning",
                f"Watch out for {platform} impulse buying",
                f"You bought {plural(len(platform_expenses), 'want')} "
                f"influenced by {platform} this month, totalling RM{platform_total:,.2f} "
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
            "None of your wants this month were impulse buys from "
            "TikTok, Instagram or Shopee Live. Nice self-control!",
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
                ))
            elif status["near_limit"]:
                items.append(_suggestion(
                    "info",
                    f"{status['category']} budget {status['percent']}% used",
                    f"You have used {status['percent']}% of your {status['category']} "
                    f"budget, with RM{status['remaining']:,.2f} left for this month.",
                    notify=True,
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
        "context_notes": context_notes,
        "total_spent": total_spent,
        "impulse_total": impulse_total,
        "expense_count": len(expenses),
    }


@app.context_processor
def inject_notifications():
    """Navbar bell: reminders and the user's top warnings."""
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
        return {"notifications": [], "notification_count": 0}

    # Reminders first (planned expenses, monthly review), then warnings
    # (exceeded budgets, impulse buys), then notes (budget at 80%)
    order = {"reminder": 0, "warning": 1}
    alerts = reminders + [item for item in items if item["notify"]]
    alerts.sort(key=lambda item: order.get(item["level"], 2))
    return {"notifications": alerts[:NOTIFICATION_LIMIT], "notification_count": len(alerts)}


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
        context_notes=result["context_notes"],
        total_spent=result["total_spent"],
        impulse_total=result["impulse_total"],
        expense_count=result["expense_count"],
    )
