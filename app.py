import os
import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from google import genai

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "campus_agent_super_secret_session_key")
CORS(app)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DB_NAME = "campus.db"

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                password TEXT NOT NULL,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL,
                joined_at TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                subject TEXT NOT NULL,
                category TEXT NOT NULL,
                deadline TEXT NOT NULL,
                priority TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                user_id TEXT PRIMARY KEY,
                subscription_json TEXT NOT NULL
            )
        ''')
        # Seed Default Administrator
        cursor.execute("SELECT * FROM users WHERE user_id = 'admin'")
        if not cursor.fetchone():
            cursor.execute('''
                INSERT INTO users (user_id, password, full_name, role, joined_at)
                VALUES (?, ?, ?, ?, ?)
            ''', ('admin', 'admin@123', 'System Administrator', 'admin', '2026-08-31 00:00:00'))
        conn.commit()

init_db()

def get_realtime_today():
    try:
        ist_now = datetime.now(ZoneInfo("Asia/Kolkata"))
        return ist_now.strftime("%Y-%m-%d")
    except Exception:
        utc_now = datetime.utcnow()
        ist_now = utc_now + timedelta(hours=5, minutes=30)
        return ist_now.strftime("%Y-%m-%d")

def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def fallback_evaluate_all(user_tasks, curr_date_val):
    curr_dt = datetime.strptime(curr_date_val, "%Y-%m-%d").date()
    with get_db() as conn:
        cursor = conn.cursor()
        for t in user_tasks:
            d_date = datetime.strptime(t["deadline"], "%Y-%m-%d").date()
            days_left = (d_date - curr_dt).days
            cat = t.get("category", "Assignment")

            if days_left < 0:
                t["priority"] = "High"
                t["reason"] = f"CRITICAL: Overdue by {abs(days_left)} day(s)! Submit immediately."
            elif cat == "Exam / Test":
                t["priority"] = "High" if days_left <= 7 else "Medium"
                t["reason"] = f"Exam scheduled in {days_left} day(s)."
            elif cat == "Lab Record Correction":
                t["priority"] = "High" if days_left <= 3 else "Medium"
                t["reason"] = f"Lab correction deadline in {days_left} day(s)."
            else:
                if days_left <= 2:
                    t["priority"] = "High"
                    t["reason"] = f"Urgent: Deadline in {days_left} day(s)."
                elif days_left <= 5:
                    t["priority"] = "Medium"
                    t["reason"] = f"Due in {days_left} day(s)."
                else:
                    t["priority"] = "Low"
                    t["reason"] = f"Sufficient time remaining ({days_left} days left)."

            cursor.execute("UPDATE tasks SET priority = ?, reason = ? WHERE id = ?",
                           (t["priority"], t["reason"], t["id"]))
        conn.commit()

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
        1. Overdue tasks (< 0 days left) MUST be 'High'.
        2. 'Exam / Test' gets highest priority (High if <= 7 days away).
        3. 'Lab Record Correction' has immediate academic weight (High if <= 3 days).
        4. Standard deadlines <= 2 days away MUST be 'High'. 3-5 days is 'Medium'. > 5 days is 'Low'.
        5. Provide a concise 1-sentence reason for each task.
        
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
    except Exception as e:
        print("Gemini API Error, using heuristic:", e)
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

        if rem_days == 1:
            alerts.append(f"⚠️ Critical: '{t['title']}' is due TOMORROW!")
        elif rem_days == 2:
            alerts.append(f"🔔 Reminder: '{t['title']}' deadline is in 2 days.")
        elif rem_days == 0:
            alerts.append(f"🚨 Urgent: '{t['title']}' is due TODAY!")
        elif rem_days < 0:
            alerts.append(f"⛔ Overdue: '{t['title']}' was due {abs(rem_days)} day(s) ago.")

        remaining_label = (
            f"{rem_days} day(s)" if rem_days > 0 
            else ("Due Today" if rem_days == 0 else f"{abs(rem_days)} days overdue")
        )

        processed_active.append({
            **t,
            "days_left": rem_days,
            "remaining_days": remaining_label,
            "reminder_date": rem_date.strftime("%Y-%m-%d")
        })

    return processed_active, completed_tasks, alerts, curr_date_val

# --- Page Routes ---
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
    user_id = data.get("user_id", "").strip().lower()
    password = data.get("password", "").strip()
    full_name = data.get("full_name", "").strip()

    if not user_id or not password or not full_name:
        return jsonify({"error": "All fields are required."}), 400

    if len(user_id) < 3:
        return jsonify({"error": "User ID / Roll Number must be at least 3 characters."}), 400

    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters long."}), 400

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if cursor.fetchone():
            return jsonify({"error": "This User ID is already taken. Please choose another."}), 400

        cursor.execute('''
            INSERT INTO users (user_id, password, full_name, role, joined_at)
            VALUES (?, ?, ?, 'student', ?)
        ''', (user_id, password, full_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

    session["user_id"] = user_id
    return jsonify({"success": True, "redirect": "/"})

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json
    user_id = data.get("user_id", "").strip().lower()
    password = data.get("password", "").strip()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()

    if not user:
        return jsonify({"error": "User ID not found. Please create an account first."}), 404

    user = dict(user)
    if user["password"] != password:
        return jsonify({"error": "Incorrect password. Please try again."}), 401

    session["user_id"] = user_id
    return jsonify({
        "success": True, 
        "redirect": "/admin" if user["role"] == "admin" else "/"
    })

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    return jsonify({"success": True})

# --- Push Notification Subscription ---
@app.route("/api/notifications/subscribe", methods=["POST"])
def subscribe_push():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    
    sub_data = request.json
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO push_subscriptions (user_id, subscription_json) VALUES (?, ?)", 
                       (user["user_id"], json.dumps(sub_data)))
        conn.commit()
    return jsonify({"success": True})

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
        u_tasks = [t for t in all_tasks if t["user_id"] == u["user_id"]]
        total_tasks = len(u_tasks)
        completed_tasks = len([t for t in u_tasks if t["completed"]])
        user_list.append({
            "user_id": u["user_id"],
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
    new_user_id = data.get("new_user_id", "").strip().lower()
    new_password = data.get("new_password", "").strip()
    current_password = data.get("current_password", "").strip()

    if current_password != user["password"]:
        return jsonify({"error": "Current admin password verification failed."}), 401

    if not new_user_id or not new_password or len(new_password) < 6:
        return jsonify({"error": "Valid new User ID and password (min 6 characters) required."}), 400

    old_user_id = user["user_id"]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET user_id = ?, password = ? WHERE user_id = ?", 
                       (new_user_id, new_password, old_user_id))
        cursor.execute("UPDATE tasks SET user_id = ? WHERE user_id = ?", 
                       (new_user_id, old_user_id))
        conn.commit()

    session["user_id"] = new_user_id
    return jsonify({"success": True, "message": "Admin credentials successfully updated."})

# --- Task APIs ---
@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE user_id = ?", (user["user_id"],))
        user_tasks = [dict(row) for row in cursor.fetchall()]

    curr_date = get_realtime_today()
    fallback_evaluate_all(user_tasks, curr_date)

    active, completed, alerts, curr_date = calculate_schedule(user_tasks)
    return jsonify({
        "active_tasks": active,
        "completed_tasks": completed,
        "submission_order": [t["title"] for t in active],
        "alerts": alerts,
        "current_date": curr_date,
        "user": {
            "full_name": user["full_name"],
            "user_id": user["user_id"],
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
            INSERT INTO tasks (user_id, title, subject, category, deadline, priority, completed, reason)
            VALUES (?, ?, ?, ?, ?, 'Medium', 0, 'Evaluating with Gemini...')
        ''', (user["user_id"], data.get("title"), data.get("subject"), data.get("category", "Assignment"), data.get("deadline")))
        conn.commit()
        
        cursor.execute("SELECT * FROM tasks WHERE user_id = ?", (user["user_id"],))
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
        cursor.execute("UPDATE tasks SET deadline = ? WHERE id = ? AND user_id = ?", 
                       (new_deadline, task_id, user["user_id"]))
        conn.commit()
        cursor.execute("SELECT * FROM tasks WHERE user_id = ?", (user["user_id"],))
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
        cursor.execute("UPDATE tasks SET completed = 1 - completed WHERE id = ? AND user_id = ?", 
                       (task_id, user["user_id"]))
        conn.commit()
        cursor.execute("SELECT * FROM tasks WHERE user_id = ?", (user["user_id"],))
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
        cursor.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user["user_id"]))
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
        cursor.execute("SELECT * FROM tasks WHERE user_id = ? AND completed = 0", (user["user_id"],))
        active_list = [dict(row) for row in cursor.fetchall()]

    client = genai.Client(api_key=GEMINI_API_KEY)
    context = f"Student Name: {user['full_name']} (User ID: {user['user_id']}). Active tasks: {active_list}. Current Date: {curr_date}."
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