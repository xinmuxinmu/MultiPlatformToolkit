"""SQLite writer helpers for the extractor.

The storage core (schema, connection factory, upsert helpers) now lives in
`common.db`; this module is a thin compatibility layer that keeps the historical
import surface (`from extractor.models import get_db, init_db, upsert_user, ...`)
working for the scraper, exporter, video downloader, and the tools/ probes.
"""
from dy_common import db as _db
from dy_common import paths

# Writer connections enable WAL + foreign keys (unchanged behavior).
from dy_common.db import init_db, upsert_user, upsert_conversation, update_conversation_stats


def __getattr__(name):
    # Backward-compatible dynamic re-export (points at the active account).
    if name == "DB_PATH":
        return paths.DB_PATH
    raise AttributeError(name)


def get_db():
    return _db.connect(foreign_keys=True, wal=True)


def insert_message(conn, msg_id, conv_id, sender_uid, sender_name, content,
                    msg_type=1, media_url=None, media_local_path=None,
                    timestamp=None, raw_data=None):
    conn.execute(
        """INSERT OR IGNORE INTO messages
           (msg_id, conv_id, sender_uid, sender_name, content, msg_type,
            media_url, media_local_path, timestamp, raw_data)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (msg_id, conv_id, sender_uid, sender_name, content, msg_type,
         media_url, media_local_path, timestamp, raw_data),
    )


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {paths.DB_PATH}")
