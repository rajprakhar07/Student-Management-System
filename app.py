"""
Smart Student Management System
================================
A Flask + MongoDB web application for managing student records.

FIXED (v2):
  - calculate_stats()  : guards against None, non-list, corrupt items, div/0
  - enrich_student()   : normalises EVERY field before Jinja2 sees it
  - view_students()    : per-document try/except so one bad doc never crashes the page
  - edit_student()     : normalises student doc before passing to template
  - dashboard()        : uses safe enrich_student for counting
  - export_csv()       : uses same safe enrich_student
"""

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, Response,
)
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from bson.objectid import ObjectId
import csv
import io
from datetime import datetime

# ── App Setup ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = "student_mgmt_secret_key_2024"   # Change in production!

# ── MongoDB Connection ───────────────────────────────────────────────────────
import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()



try:
    client = MongoClient(os.getenv("MONGO_URI"))
    client.server_info()
    db = client["student_db"]
    students_col = db["students"]
    students_col.create_index([("student_id", ASCENDING)], unique=True)
    students_col.create_index([("enroll_no",  ASCENDING)], unique=True)
    print(" MongoDB connected successfully.")
except Exception as e:
    print(f" MongoDB connection failed: {e}")
    
    students_col = None

# ── Demo credentials ─────────────────────────────────────────────────────────
USERS = {
    "admin":   "admin123",
    "teacher": "teacher123",
}

# ── Helper Functions ──────────────────────────────────────────────────────────

def calculate_stats(subjects: list) -> dict:
    """
    Calculate total marks, percentage, and grade from subject marks.

    FIX Layer 1 — Guards against:
      * None / non-list input  -> safe N/A defaults
      * Empty list             -> safe N/A defaults
      * Non-numeric list items -> each skipped silently
      * Division by zero       -> percentage defaults to 0.0
    """
    # Guard: None or wrong type
    if not subjects or not isinstance(subjects, list):
        return {"total": 0, "max_marks": 0, "percentage": 0.0, "grade": "N/A"}

    # Strip non-numeric items that may have crept into the DB
    clean = []
    for m in subjects:
        try:
            clean.append(int(m))
        except (ValueError, TypeError):
            pass  # silently skip corrupt entries

    # Guard: every item was corrupt
    if not clean:
        return {"total": 0, "max_marks": 0, "percentage": 0.0, "grade": "N/A"}

    total     = sum(clean)
    max_marks = len(clean) * 100
    percentage = round((total / max_marks) * 100, 2) if max_marks > 0 else 0.0

    if percentage >= 75:
        grade = "A"
    elif percentage >= 60:
        grade = "B"
    elif percentage >= 40:
        grade = "C"
    else:
        grade = "Fail"

    return {
        "total":      total,
        "max_marks":  max_marks,
        "percentage": percentage,
        "grade":      grade,
    }


def eligibility(attendance: float) -> str:
    """Return 'Eligible' if attendance >= 75%, else 'Not Eligible'."""
    return "Eligible" if attendance >= 75 else "Not Eligible"


def enrich_student(s: dict) -> dict:
    """
    Normalise every field of a raw MongoDB document, then append
    computed fields (total, percentage, grade, eligibility).

    FIX Layer 2 — every field gets a safe default before Jinja2 touches it.
    Old / partial documents can never crash a template again.
    """
    # String fields
    s["student_id"] = str(s.get("student_id") or "N/A").strip()
    s["enroll_no"]  = str(s.get("enroll_no")  or "N/A").strip()
    s["name"]       = str(s.get("name")        or "Unknown").strip()
    s["semester"]   = str(s.get("semester")    or "—").strip()
    s["course"]     = str(s.get("course")      or "—").strip()

    # subjects — must be a list of ints clamped 0-100
    raw = s.get("subjects", [])
    if not isinstance(raw, list):
        raw = []
    clean_subjects = []
    for mark in raw:
        try:
            clean_subjects.append(max(0, min(100, int(mark))))
        except (ValueError, TypeError):
            pass
    s["subjects"] = clean_subjects

    # attendance — float 0-100, default 0.0
    try:
        att = float(s.get("attendance") or 0)
        s["attendance"] = max(0.0, min(100.0, att))
    except (ValueError, TypeError):
        s["attendance"] = 0.0

    # fees — float >= 0, default 0.0
    try:
        s["fees"] = max(0.0, float(s.get("fees") or 0))
    except (ValueError, TypeError):
        s["fees"] = 0.0

    # Computed fields
    stats            = calculate_stats(s["subjects"])
    s["total"]       = stats["total"]
    s["max_marks"]   = stats["max_marks"]
    s["percentage"]  = stats["percentage"]
    s["grade"]       = stats["grade"]
    s["eligibility"] = eligibility(s["attendance"])

    return s


def login_required(func):
    """Decorator: redirect to login if user is not in session."""
    from functools import wraps
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapper


def db_required(func):
    """Decorator: show error if MongoDB is unavailable."""
    from functools import wraps
    @wraps(func)
    def wrapper(*args, **kwargs):
        if students_col is None:
            flash("⚠️ Database unavailable. Please start MongoDB.", "danger")
            return redirect(url_for("dashboard"))
        return func(*args, **kwargs)
    return wrapper


def parse_subjects(form) -> list:
    """Extract and validate subject marks from a submitted form."""
    subjects = []
    for i in range(1, 6):
        val = form.get(f"subject_{i}", "").strip()
        if val:
            try:
                mark = int(val)
                if 0 <= mark <= 100:
                    subjects.append(mark)
                else:
                    raise ValueError(f"Mark out of range: {mark}")
            except ValueError:
                raise ValueError(f"Invalid mark for Subject {i}: '{val}'")
    return subjects

# ── Template Context ─────────────────────────────────────────────────────────

@app.context_processor
def inject_now():
    return {"now": datetime.utcnow().strftime("%d %b %Y")}

# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def login():
    if "user" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if username in USERS and USERS[username] == password:
            session["user"] = username
            flash(f"Welcome back, {username}! 👋", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid username or password.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    """Show stats: total students, pass/fail count, average percentage."""
    stats = {
        "total":      0,
        "pass":       0,
        "fail":       0,
        "avg_pct":    0.0,
        "db_offline": students_col is None,
    }

    if students_col is not None:
        percentages = []
        for s in students_col.find():
            try:
                enriched = enrich_student(s)           # safe normalisation
                percentages.append(enriched["percentage"])
                if enriched["grade"] in ("Fail", "N/A"):
                    stats["fail"] += 1
                else:
                    stats["pass"] += 1
                stats["total"] += 1
            except Exception as e:
                app.logger.warning(
                    f"dashboard: skipping bad doc {s.get('_id')}: {e}"
)
        

        if percentages:
            stats["avg_pct"] = round(sum(percentages) / len(percentages), 2)
        
    return render_template("dashboard.html", stats=stats, user=session["user"])

# ── Student CRUD ──────────────────────────────────────────────────────────────

@app.route("/students")
@login_required
@db_required
def view_students():
    """
    Display all students in a searchable table.

    FIX Layer 3:
      * enrich_student() normalises every field before Jinja2 sees it.
      * Per-document try/except: one corrupt record is logged and skipped;
        the rest of the page still renders correctly.
    """
    query       = request.args.get("q", "").strip()
    filter_dict = {}

    if query:
        filter_dict = {
            "$or": [
                {"name":       {"$regex": query, "$options": "i"}},
                {"student_id": {"$regex": query, "$options": "i"}},
                {"enroll_no":  {"$regex": query, "$options": "i"}},
            ]
        }

    raw_students = list(students_col.find(filter_dict).sort("name", 1))

    students = []
    for s in raw_students:
        try:
            students.append(enrich_student(s))
        except Exception as e:
            # One broken document must NEVER crash the whole page
            app.logger.warning(
                f"view_students: skipping malformed doc _id={s.get('_id')}: {e}"
            )
            continue

    return render_template(
        "students.html",
        students=students,
        query=query,
        user=session["user"],
    )


@app.route("/students/add", methods=["GET", "POST"])
@login_required
@db_required
def add_student():
    """Add a new student."""
    if request.method == "POST":
        try:
            subjects = parse_subjects(request.form)
            if len(subjects) < 4:
                flash("Please enter marks for at least 4 subjects.", "warning")
                return render_template(
                    "add_student.html", user=session["user"], form=request.form
                )

            doc = {
                "student_id": request.form["student_id"].strip(),
                "enroll_no":  request.form["enroll_no"].strip(),
                "name":       request.form["name"].strip(),
                "semester":   request.form["semester"].strip(),
                "course":     request.form["course"].strip(),
                "subjects":   subjects,
                "attendance": float(request.form["attendance"]),
                "fees":       float(request.form["fees"]),
                "created_at": datetime.utcnow(),
            }

            students_col.insert_one(doc)
            flash(f"✅ Student '{doc['name']}' added successfully!", "success")
            return redirect(url_for("view_students"))

        except DuplicateKeyError:
            flash("❌ Student ID or Enroll No already exists.", "danger")
        except ValueError as e:
            flash(f"❌ Invalid input: {e}", "danger")
        except Exception as e:
            flash(f"❌ Error: {e}", "danger")

        return render_template(
            "add_student.html", user=session["user"], form=request.form
        )

    return render_template("add_student.html", user=session["user"], form={})


@app.route("/students/edit/<student_id>", methods=["GET", "POST"])
@login_required
@db_required
def edit_student(student_id):
    """
    Edit an existing student (pre-filled form).

    FIX: raw doc is passed through enrich_student() on GET so the edit
    form pre-fills safely even when old records have missing fields.
    """
    raw = students_col.find_one({"_id": ObjectId(student_id)})
    if not raw:
        flash("Student not found.", "warning")
        return redirect(url_for("view_students"))

    # Normalise before sending to template so pre-fill never crashes
    student = enrich_student(raw)

    if request.method == "POST":
        try:
            subjects = parse_subjects(request.form)
            if len(subjects) < 4:
                flash("Please enter marks for at least 4 subjects.", "warning")
                return render_template(
                    "edit_student.html", student=student, user=session["user"]
                )

            update = {
                "$set": {
                    "student_id": request.form["student_id"].strip(),
                    "enroll_no":  request.form["enroll_no"].strip(),
                    "name":       request.form["name"].strip(),
                    "semester":   request.form["semester"].strip(),
                    "course":     request.form["course"].strip(),
                    "subjects":   subjects,
                    "attendance": float(request.form["attendance"]),
                    "fees":       float(request.form["fees"]),
                    "updated_at": datetime.utcnow(),
                }
            }

            students_col.update_one({"_id": ObjectId(student_id)}, update)
            flash("✅ Student updated successfully!", "success")
            return redirect(url_for("view_students"))

        except DuplicateKeyError:
            flash("❌ Student ID or Enroll No already exists.", "danger")
        except ValueError as e:
            flash(f"❌ Invalid input: {e}", "danger")
        except Exception as e:
            flash(f"❌ Error: {e}", "danger")

    return render_template(
        "edit_student.html", student=student, user=session["user"]
    )


@app.route("/students/delete/<student_id>", methods=["POST"])
@login_required
@db_required
def delete_student(student_id):
    """Delete a student by MongoDB _id."""
    result = students_col.delete_one({"_id": ObjectId(student_id)})
    if result.deleted_count:
        flash("🗑️ Student deleted successfully.", "success")
    else:
        flash("Student not found.", "warning")
    return redirect(url_for("view_students"))

# ── Export ────────────────────────────────────────────────────────────────────

@app.route("/students/export")
@login_required
@db_required
def export_csv():
    """
    Export all student data as a CSV file.
    enrich_student() ensures every row is fully normalised — no KeyErrors.
    """
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Student ID", "Enroll No", "Name", "Semester", "Course",
        "Subject 1", "Subject 2", "Subject 3", "Subject 4", "Subject 5",
        "Total", "Max Marks", "Percentage", "Grade",
        "Attendance (%)", "Eligibility", "Fees",
    ])

    for raw in students_col.find():
        try:
            s = enrich_student(raw)
        except Exception as e:
            app.logger.warning(
                f"export_csv: skipping bad doc {raw.get('_id')}: {e}"
            )
            continue

        subjects = s["subjects"] + [""] * (5 - len(s["subjects"]))  # pad to 5
        writer.writerow([
            s["student_id"], s["enroll_no"], s["name"],
            s["semester"],   s["course"],
            *subjects,
            s["total"], s["max_marks"], s["percentage"],
            s["grade"], s["attendance"], s["eligibility"],
            s["fees"],
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=students_export.csv"},
    )

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5000)