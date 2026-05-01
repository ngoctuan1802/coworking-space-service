import atexit
import os

from apscheduler.schedulers.background import BackgroundScheduler
from flask import jsonify
from sqlalchemy import text

from config import app, db

port_number = int(os.environ.get("APP_PORT", 5153))


@app.route("/health_check")
def health_check():
    return "ok"


@app.route("/readiness_check")
def readiness_check():
    try:
        db.session.execute(text("SELECT COUNT(*) FROM tokens")).scalar()
    except Exception as e:
        app.logger.error(e)
        return "failed", 500
    return "ok"


def get_daily_visits():
    with app.app_context():
        result = db.session.execute(text("""
            SELECT DATE(created_at) AS date, COUNT(*) AS visits
            FROM tokens
            WHERE used_at IS NOT NULL
            GROUP BY DATE(created_at)
        """))
        return {str(row[0]): row[1] for row in result}


@app.route("/api/reports/daily_usage")
def daily_visits():
    return jsonify(get_daily_visits())


@app.route("/api/reports/user_visits")
def all_user_visits():
    try:
        result = db.session.execute(text("""
            SELECT t.user_id, t.visits, users.joined_at
            FROM (
                SELECT user_id, COUNT(*) AS visits
                FROM tokens
                GROUP BY user_id
            ) AS t
            LEFT JOIN users ON t.user_id = users.id
        """))
        return jsonify({
            row[0]: {"visits": row[1], "joined_at": str(row[2])}
            for row in result
        })
    except Exception as e:
        app.logger.error(e)
        return "failed", 500


def _scheduled_daily_visits():
    app.logger.info(get_daily_visits())


scheduler = BackgroundScheduler()
scheduler.add_job(_scheduled_daily_visits, "interval", seconds=30)
scheduler.start()
atexit.register(scheduler.shutdown)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=port_number)
