import sqlite3
import time
from pathlib import Path

SICILY_HOME = Path.home() / ".sicily"
DB_PATH = SICILY_HOME / "Data" / "usage.db"

# Prices per 1M tokens (including cached input where available)
MODEL_PRICING = {
    "gpt-4o-mini": {
        "input": 0.15 / 1_000_000,
        "cached_input": 0.075 / 1_000_000,
        "output": 0.60 / 1_000_000
    },
    "gpt-5.4-mini": {
        "input": 0.75 / 1_000_000,
        "cached_input": 0.075 / 1_000_000,
        "output": 4.50 / 1_000_000
    },
    "gpt-5.4-nano": {
        "input": 0.20 / 1_000_000,
        "cached_input": 0.020 / 1_000_000,
        "output": 1.25 / 1_000_000
    },
    "gpt-5.6-luna": {
        "input": 0.20 / 1_000_000,
        "cached_input": 0.020 / 1_000_000,
        "output": 1.20 / 1_000_000
    },
    "gpt-5-nano": {
        "input": 0.05 / 1_000_000,
        "cached_input": 0.010 / 1_000_000,
        "output": 0.40 / 1_000_000
    },
    "gpt-4o-mini-transcribe": {
        "input": 1.25 / 1_000_000,
        "output": 5 / 1_000_000
    },
}


def get_cost(model_name: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
    """Calculate cost with support for cached input tokens."""
    rates = MODEL_PRICING.get(model_name, MODEL_PRICING.get("gpt-5.4-mini", {}))
    
    input_cost = (input_tokens - cached_input_tokens) * rates.get("input", 0.75 / 1_000_000)
    cached_cost = cached_input_tokens * rates.get("cached_input", 0.075 / 1_000_000)
    output_cost = output_tokens * rates.get("output", 4.50 / 1_000_000)
    
    return input_cost + cached_cost + output_cost


def init_db():
    """Initialize DB and ensure all columns exist for backward compatibility."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        # Create table if it doesn't exist
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                dimension TEXT,
                session_id TEXT,
                model_name TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cached_input_tokens INTEGER DEFAULT 0,
                cost REAL,
                message_id TEXT UNIQUE
            )
        """)

        try:
            conn.execute("ALTER TABLE token_usage ADD COLUMN cached_input_tokens INTEGER DEFAULT 0")
            print("Added missing column: cached_input_tokens")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                print(f"Warning adding column: {e}")
        
        conn.commit()


def record_usage(dimension: str, session_id: str, model_name: str, 
                 input_tokens: int, output_tokens: int, 
                 cached_input_tokens: int = 0, message_id: str = None):
    init_db()
    cost = get_cost(model_name, input_tokens, output_tokens, cached_input_tokens)
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO token_usage 
            (timestamp, dimension, session_id, model_name, 
             input_tokens, output_tokens, cached_input_tokens, cost, message_id) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (time.time(), dimension, session_id, model_name,
             input_tokens, output_tokens, cached_input_tokens, cost, message_id)
        )


def cleanup_old_records():
    """Keep only the last 30 days of usage."""
    cutoff = time.time() - (30 * 24 * 60 * 60)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM token_usage WHERE timestamp < ?", (cutoff,))


def get_usage_report(timeframe="week") -> list[dict]:
    """
    Returns aggregated usage data.
    timeframe can be: 'session', 'day', or 'week'.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if timeframe == "session":
            cursor.execute("SELECT session_id FROM token_usage ORDER BY timestamp DESC LIMIT 1")
            row = cursor.fetchone()
            if not row:
                return []
            
            cursor.execute("""
                SELECT dimension, model_name, 
                       SUM(input_tokens) as in_tokens, 
                       SUM(output_tokens) as out_tokens, 
                       SUM(cost) as total_cost 
                FROM token_usage 
                WHERE session_id = ?
                GROUP BY dimension, model_name
            """, (row["session_id"],))
        else:
            days = 1 if timeframe == "day" else 7
            cutoff = time.time() - (days * 24 * 60 * 60)
            cursor.execute("""
                SELECT dimension, model_name, 
                       SUM(input_tokens) as in_tokens, 
                       SUM(output_tokens) as out_tokens, 
                       SUM(cost) as total_cost 
                FROM token_usage 
                WHERE timestamp >= ?
                GROUP BY dimension, model_name
            """, (cutoff,))

        return [dict(row) for row in cursor.fetchall()]
