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
        # Compared exactly (not with the rounded-down percent), so RM80.00 of
        # a RM100.00 budget always counts as 80%
        reached_warning = spent * 100 >= budget.amount * BUDGET_WARNING_PERCENT
        status.append({
            "category": budget.category,
            "budget": budget.amount,
            "spent": spent,
            "remaining": budget.amount - spent,
            "percent": percent,
            "over": over,
            "near_limit": not over and reached_warning,
            "description": budget.description,
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
        flash(f"Income of RM{income.amount:,.2f} from {income.source} updated.", "success")
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
        flash(f"Expense of RM{expense.amount:,.2f} for {expense.category} updated.", "success")
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
