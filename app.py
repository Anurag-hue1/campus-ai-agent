import os
import json
import random
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from google import genai

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "campus_agent_super_secret_session_key")
CORS(app)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DB_NAME = "campus.db"

# Temporary OTP Store: { "email": {"otp": "123456", "expires_at": timestamp} }
otp_storage = {}

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                password TEXT NOT NULL,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL,
                joined_at TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                title TEXT NOT NULL,
                subject TEXT NOT NULL,
                category TEXT NOT NULL,
                deadline TEXT NOT NULL,
                priority TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                FOREIGN KEY (user_email) REFERENCES users (email)
            )
        ''')
        # Seed Master Admin if not exists
        cursor.execute("SELECT * FROM users WHERE email = 'anniadmin@gmail.com'")
        if not cursor.fetchone():
            cursor.execute('''
                INSERT INTO users (email, password, full_name, role, joined_at)
                VALUES (?, ?, ?, ?, ?)
            ''', ('anniadmin@gmail.com', 'anni@225462', 'System Owner & Admin', 'admin', '2026-08-01 10:00:00'))
        conn.commit()

init_db()

def get_realtime_today():
    return datetime.now().strftime("%Y-%m-%d")

def get_current_user():
    email = session.get("user_email")
    if not email:
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()
        return dict(row) if row else None

def fallback_evaluate_all(user_tasks, curr_date_val):
    curr_dt = datetime.strptime(curr_date_val, "%Y-%m-%d").date()
    for t in user_tasks:
        d_date = datetime.strptime(t["deadline"], "%Y-%m-%d").date()
        days_left = (d_date - curr_dt).days
        cat = t.get("category", "Assignment")

        if cat == "Exam / Test":
            t["priority"] = "High" if days_left <= 7 else "Medium"
            t["reason"] = f"Exam in {days_left} day(s)"
        elif cat == "Lab Record Correction":
            t["priority"] = "High" if days_left <= 3 else "Medium"
            t["reason"] = f"Lab correction in {days_left} day(s)"
        else:
            if days_left <= 2:
                t["priority"] = "High"
                t["reason"] = f"Deadline in {days_left} day(s)"
            elif days_left <= 5:
                t["priority"] = "Medium"
                t["reason"] = f"Due in {days_left} day(s)"
            else:
                t["priority"] = "Low"
                t["reason"] = f"Sufficient time ({days_left} days left)"

def run_gemini_global_evaluation(user_tasks, curr_date_val):
    active_tasks = [t for t in user_tasks if not t.get("completed")]
    if not active_tasks:
        return

    if not GEMINI_API_KEY:
        fallback_evaluate_all(user_tasks, curr_date_val)
        return

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
        You are an intelligent Academic AI Prioritization Agent.
        Reference Date: {curr_date_val}
        
        Evaluate these academic tasks together and assign priorities:
        {json.dumps(active_tasks)}
        
        Rules:
        1. 'Exam / Test' gets highest priority (High if <= 7 days away).
        2. 'Lab Record Correction' has immediate academic weight (High if <= 3 days).
        3. Standard deadlines <= 2 days away MUST be 'High'. 3-5 days is 'Medium'. > 5 days is 'Low'.
        4. Re-rank priorities comparatively across all tasks and provide a concise 1-sentence reason for each.
        
        Return RAW JSON ONLY (no markdown fences):
        [
          {{
            "id": <number>,
            "priority": "High" | "Medium" | "Low",
            "reason": "<short justification>"
          }}
        ]
        """
        res = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        raw = res.text.strip().replace("```json", "").replace("```", "")
        eval_list = json.loads(raw)

        with get_db() as conn:
            cursor = conn.cursor()
            for item in eval_list:
                for t in user_tasks:
                    if t["id"] == item["id"]:
                        t["priority"] = item["priority"]
                        t["reason"] = item["reason"]
                        cursor.execute("UPDATE tasks SET priority = ?, reason = ? WHERE id = ?", 
                                       (item["priority"], item["reason"], item["id"]))
            conn.commit()
    except Exception:
        fallback_evaluate_all(user_tasks, curr_date_val)

def calculate_schedule(user_tasks):
    curr_date_val = get_realtime_today()
    curr_dt = datetime.strptime(curr_date_val, "%Y-%m-%d").date()
    priority_order = {"High": 1, "Medium": 2, "Low": 3}

    active_tasks = [t for t in user_tasks if not t.get("completed")]
    completed_tasks = [t for t in user_tasks if t.get("completed")]

    sorted_active = sorted(
        active_tasks,
        key=lambda x: (
            datetime.strptime(x["deadline"], "%Y-%m-%d").date(),
            priority_order.get(x.get("priority", "Medium"), 2)
        )
    )

    processed_active = []
    alerts = []

    for t in sorted_active:
        d_date = datetime.strptime(t["deadline"], "%Y-%m-%d").date()
        rem_days = (d_date - curr_dt).days

        offset = 2 if t.get("priority") == "High" else 1
        rem_date = d_date - timedelta(days=offset)
        r_diff = (rem_date - curr_dt).days

        if r_diff < 0:
            alerts.append(f"{t['title']}: Deadline is in {rem_days} day(s). Reminder date has passed.")
        elif r_diff == 0:
            alerts.append(f"{t['title']}: Reminder triggers today.")

        remaining_label = (
            f"{rem_days} day(s)" if rem_days > 0 
            else ("Due Today" if rem_days == 0 else f"{abs(rem_days)} days overdue")
        )

        processed_active.append({
            **t,
            "remaining_days": remaining_label,
            "reminder_date": rem_date.strftime("%Y-%m-%d")
        })

    return processed_active, completed_tasks, alerts, curr_date_val

# --- Routes ---
@app.route("/")
def home():
    user = get_current_user()
    if not user:
        return redirect(url_for("auth_page"))
    return render_template("index.html", user=user)

@app.route("/auth")
def auth_page():
    user = get_current_user()
    if user:
        return redirect(url_for("admin_page") if user.get("role") == "admin" else url_for("home"))
    return render_template("auth.html")

@app.route("/admin")
def admin_page():
    user = get_current_user()
    if not user or user.get("role") != "admin":
        return redirect(url_for("auth_page"))
    return render_template("admin.html", user=user)

# --- Authentication APIs ---
@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.json
    email = data.get("email", "").strip().lower()
    password = data.get("password", "").strip()
    full_name = data.get("full_name", "").strip()

    if not email or not password or not full_name:
        return jsonify({"error": "All fields are required."}), 400

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM users WHERE email = ?", (email,))
        if cursor.fetchone():
            return jsonify({"error": "An account with this email already exists. Please sign in."}), 400

        cursor.execute('''
            INSERT INTO users (email, password, full_name, role, joined_at)
            VALUES (?, ?, ?, 'student', ?)
        ''', (email, password, full_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

    session["user_email"] = email
    return jsonify({"success": True, "redirect": "/"})

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json
    email = data.get("email", "").strip().lower()
    password = data.get("password", "").strip()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()

    if not user:
        return jsonify({"error": "User is not available. Please create an account first."}), 404

    user = dict(user)
    if user["password"] != password:
        return jsonify({"error": "Incorrect password. Please check your password or use Forgot Password."}), 401

    session["user_email"] = email
    return jsonify({
        "success": True, 
        "redirect": "/admin" if user["role"] == "admin" else "/"
    })

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.pop("user_email", None)
    return jsonify({"success": True})

# --- Forgot Password & OTP APIs ---
@app.route("/api/auth/forgot-password/send-otp", methods=["POST"])
def send_otp():
    data = request.json
    email = data.get("email", "").strip().lower()

    if not email:
        return jsonify({"error": "Please enter your registered email address."}), 400

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()

    if not user:
        return jsonify({"error": "User is not available with this email address. Please create an account."}), 404

    if user["role"] == "admin":
        return jsonify({"error": "Admin password cannot be reset via public OTP. Change it inside the Admin Portal."}), 403

    otp = f"{random.randint(100000, 999999)}"
    otp_storage[email] = {
        "otp": otp,
        "expires_at": datetime.now() + timedelta(minutes=10)
    }

    return jsonify({
        "success": True,
        "message": f"OTP sent successfully to {email}.",
        "demo_otp": otp
    })

@app.route("/api/auth/forgot-password/verify-and-reset", methods=["POST"])
def verify_and_reset():
    data = request.json
    email = data.get("email", "").strip().lower()
    otp_entered = data.get("otp", "").strip()
    new_password = data.get("new_password", "").strip()

    if not email or not otp_entered or not new_password:
        return jsonify({"error": "All fields are required."}), 400

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()

    if not user or user["role"] == "admin":
        return jsonify({"error": "Unauthorized operation."}), 403

    saved_otp = otp_storage.get(email)
    if not saved_otp or datetime.now() > saved_otp["expires_at"]:
        otp_storage.pop(email, None)
        return jsonify({"error": "OTP has expired. Please request a new OTP."}), 400

    if saved_otp["otp"] != otp_entered:
        return jsonify({"error": "Invalid OTP entered."}), 400

    if len(new_password) < 6:
        return jsonify({"error": "New password must be at least 6 characters."}), 400

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET password = ? WHERE email = ?", (new_password, email))
        conn.commit()

    otp_storage.pop(email, None)
    return jsonify({"success": True, "message": "Password reset successfully!"})

# --- Admin APIs ---
@app.route("/api/admin/users", methods=["GET"])
def get_all_users():
    user = get_current_user()
    if not user or user.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users")
        all_users = [dict(u) for u in cursor.fetchall()]

        cursor.execute("SELECT * FROM tasks")
        all_tasks = [dict(t) for t in cursor.fetchall()]

    user_list = []
    for u in all_users:
        u_tasks = [t for t in all_tasks if t["user_email"] == u["email"]]
        total_tasks = len(u_tasks)
        completed_tasks = len([t for t in u_tasks if t["completed"]])
        user_list.append({
            "email": u["email"],
            "full_name": u["full_name"],
            "role": u["role"],
            "joined_at": u["joined_at"],
            "total_tasks": total_tasks,
            "active_tasks": total_tasks - completed_tasks,
            "completed_tasks": completed_tasks
        })
    return jsonify({"users": user_list})

@app.route("/api/admin/update-credentials", methods=["PUT"])
def update_admin_credentials():
    user = get_current_user()
    if not user or user.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json
    new_email = data.get("new_email", "").strip().lower()
    new_password = data.get("new_password", "").strip()
    current_password = data.get("current_password", "").strip()

    if current_password != user["password"]:
        return jsonify({"error": "Current admin password verification failed."}), 401

    if not new_email or not new_password or len(new_password) < 6:
        return jsonify({"error": "Valid new email and password (min 6 characters) required."}), 400

    old_email = user["email"]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET email = ?, password = ? WHERE email = ?", 
                       (new_email, new_password, old_email))
        cursor.execute("UPDATE tasks SET user_email = ? WHERE user_email = ?", 
                       (new_email, old_email))
        conn.commit()

    session["user_email"] = new_email
    return jsonify({"success": True, "message": "Admin credentials successfully updated."})

# --- Task APIs (Per User) ---
@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE user_email = ?", (user["email"],))
        user_tasks = [dict(row) for row in cursor.fetchall()]

    active, completed, alerts, curr_date = calculate_schedule(user_tasks)
    return jsonify({
        "active_tasks": active,
        "completed_tasks": completed,
        "submission_order": [t["title"] for t in active],
        "alerts": alerts,
        "current_date": curr_date,
        "user": {
            "full_name": user["full_name"],
            "email": user["email"],
            "role": user["role"]
        }
    })

@app.route("/api/tasks", methods=["POST"])
def add_task():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    curr_date = get_realtime_today()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO tasks (user_email, title, subject, category, deadline, priority, completed, reason)
            VALUES (?, ?, ?, ?, ?, 'Medium', 0, 'Evaluating with Gemini...')
        ''', (user["email"], data.get("title"), data.get("subject"), data.get("category", "Assignment"), data.get("deadline")))
        conn.commit()
        
        cursor.execute("SELECT * FROM tasks WHERE user_email = ?", (user["email"],))
        user_tasks = [dict(row) for row in cursor.fetchall()]

    run_gemini_global_evaluation(user_tasks, curr_date)
    active, completed, alerts, _ = calculate_schedule(user_tasks)
    return jsonify({
        "success": True,
        "active_tasks": active,
        "completed_tasks": completed,
        "submission_order": [t["title"] for t in active],
        "alerts": alerts
    })

@app.route("/api/tasks/<int:task_id>/deadline", methods=["PUT"])
def update_deadline(task_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    new_deadline = data.get("deadline")
    curr_date = get_realtime_today()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE tasks SET deadline = ? WHERE id = ? AND user_email = ?", 
                       (new_deadline, task_id, user["email"]))
        conn.commit()
        cursor.execute("SELECT * FROM tasks WHERE user_email = ?", (user["email"],))
        user_tasks = [dict(row) for row in cursor.fetchall()]

    run_gemini_global_evaluation(user_tasks, curr_date)
    active, completed, alerts, _ = calculate_schedule(user_tasks)
    return jsonify({
        "success": True,
        "active_tasks": active,
        "completed_tasks": completed,
        "submission_order": [t["title"] for t in active],
        "alerts": alerts
    })

@app.route("/api/tasks/<int:task_id>/toggle-complete", methods=["PUT"])
def toggle_complete(task_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE tasks SET completed = 1 - completed WHERE id = ? AND user_email = ?", 
                       (task_id, user["email"]))
        conn.commit()
        cursor.execute("SELECT * FROM tasks WHERE user_email = ?", (user["email"],))
        user_tasks = [dict(row) for row in cursor.fetchall()]
            
    active, completed, alerts, _ = calculate_schedule(user_tasks)
    return jsonify({
        "success": True,
        "active_tasks": active,
        "completed_tasks": completed,
        "submission_order": [t["title"] for t in active],
        "alerts": alerts
    })

@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tasks WHERE id = ? AND user_email = ?", (task_id, user["email"]))
        conn.commit()

    return jsonify({"success": True})

@app.route("/api/gemini", methods=["POST"])
def call_gemini():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    prompt = data.get("prompt", "")
    curr_date = get_realtime_today()

    if not GEMINI_API_KEY:
        return jsonify({"error": "Gemini API key not configured."}), 400

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE user_email = ? AND completed = 0", (user["email"],))
        active_list = [dict(row) for row in cursor.fetchall()]

    client = genai.Client(api_key=GEMINI_API_KEY)
    context = f"Student Name: {user['full_name']}. Active tasks: {active_list}. Current Date: {curr_date}."
    full_prompt = f"You are an academic advisor AI agent.\nContext: {context}\n\nStudent Query: {prompt}"

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=full_prompt
        )
        return jsonify({"response": response.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)