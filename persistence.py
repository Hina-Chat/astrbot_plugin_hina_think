import asyncio
import logging
from pathlib import Path
from collections import OrderedDict
from typing import Any, Dict, Optional, List, Coroutine

import aiosqlite



class PersistenceManager:
    """Manages all data persistence using SQLite, plus minimal in-memory cache for fast access."""

    def __init__(self, config: dict, logger: logging.Logger, data_path: Path):
        self.config = config
        self.logger = logger
        self.data_path = data_path

        # --- Load config ---
        general_config = self.config.get('general', {})
        self.enable_persistence = general_config.get('enable_persistence', True)
        self.upload_cache_size = general_config.get('upload_cache_size', 1000)
        # Persist directly under the plugin's official data directory (no extra subdir)
        self.storage_dir = self.data_path

        # --- Initialize state ---
        self.records: Dict[str, Dict[str, Any]] = {}
        self.last_uploaded_info: OrderedDict[str, Dict[str, Any]] = OrderedDict()

        # SQLite database path
        self.db_path = self.storage_dir / 'hina_thoughts.db'
        self.db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()

        if self.enable_persistence:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            # lazy init on first DB use to avoid event-loop issues during construction
            pass

    async def _ensure_db(self):
        """Ensure SQLite database is initialized and schema created."""
        if not self.enable_persistence:
            return
        if self.db is not None:
            return
        async with self._db_lock:
            if self.db is not None:
                return
            self.db = await aiosqlite.connect(self.db_path)
            # Pragmas for better concurrency and durability
            try:
                await self.db.execute("PRAGMA journal_mode=WAL;")
                await self.db.execute("PRAGMA synchronous=NORMAL;")
            except Exception as e:
                # Some environments may not support setting PRAGMAs; continue safely
                self.logger.debug(f"R1Filter: SQLite pragmas set failed: {e}")
            await self._init_db_schema()

    async def _init_db_schema(self):
        assert self.db is not None
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS thoughts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_key TEXT NOT NULL,
                trigger_user_id TEXT,
                reasoning TEXT,
                response TEXT,
                user_message TEXT,
                timestamp TEXT,
                session_id TEXT
            );
            """
        )
        await self.db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_thoughts_user_ts
            ON thoughts(user_key, timestamp);
            """
        )
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS upload_breakpoints (
                user_key TEXT PRIMARY KEY,
                url TEXT,
                breakpoint_timestamp TEXT
            );
            """
        )
        await self.db.commit()

    async def _update_last_upload_info_db(self, user_key: str, url: str, breakpoint_timestamp: str):
        if not self.enable_persistence:
            return
        await self._ensure_db()
        assert self.db is not None
        await self.db.execute(
            """
            INSERT INTO upload_breakpoints(user_key, url, breakpoint_timestamp)
            VALUES (?, ?, ?)
            ON CONFLICT(user_key) DO UPDATE SET
                url=excluded.url,
                breakpoint_timestamp=excluded.breakpoint_timestamp
            ;
            """,
            (user_key, url, breakpoint_timestamp),
        )
        await self.db.commit()

    async def _fetch_records_since_db(
        self, user_key: str, last_timestamp_iso: Optional[str], limit: int
    ) -> List[Dict[str, Any]]:
        if not self.enable_persistence:
            return []
        await self._ensure_db()
        assert self.db is not None
        params: list[Any] = [user_key]
        where_clause = ""
        if last_timestamp_iso:
            where_clause = "AND timestamp > ?"
            params.append(last_timestamp_iso)
        query = (
            "SELECT user_key, trigger_user_id, reasoning, response, user_message, timestamp, session_id "
            "FROM thoughts WHERE user_key = ? "
            f"{where_clause} "
            "ORDER BY timestamp ASC LIMIT ?"
        )
        params.append(int(limit))
        rows: list[tuple] = []
        async with self.db.execute(query, params) as cur:
            rows = await cur.fetchall()
        records: List[Dict[str, Any]] = []
        for (
            u_key,
            trig,
            reasoning,
            response,
            user_message,
            ts,
            session_id,
        ) in rows:
            records.append(
                {
                    "user_key": u_key,
                    "trigger_user_id": trig,
                    "reasoning": reasoning,
                    "response": response,
                    "user_message": user_message,
                    "timestamp": ts,
                    "session_id": session_id,
                }
            )
        return records


    async def log_thought(self, record: dict):
        """Insert a single thought record into SQLite and update in-memory cache."""
        if not self.enable_persistence:
            return

        user_key = record.get('user_key', 'unknown_session')

        # Update in-memory cache first for immediate availability to /think
        self.records[user_key] = record

        await self._ensure_db()
        assert self.db is not None
        await self.db.execute(
            """
            INSERT INTO thoughts(user_key, trigger_user_id, reasoning, response, user_message, timestamp, session_id)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get('user_key'),
                record.get('trigger_user_id'),
                record.get('reasoning'),
                record.get('response'),
                record.get('user_message'),
                record.get('timestamp'),
                record.get('session_id'),
            ),
        )
        await self.db.commit()

    def get_last_thought(self, user_key: str) -> Optional[Dict[str, Any]]:
        """從快取中獲取用戶的最新思維記錄。"""
        return self.records.get(user_key)

    async def get_last_thought_async(self, user_key: str) -> Optional[Dict[str, Any]]:
        """從 SQLite 中獲取用戶最新一條思維記錄（重啟後可用）。"""
        if not self.enable_persistence:
            return None
        await self._ensure_db()
        assert self.db is not None
        async with self.db.execute(
            (
                "SELECT user_key, trigger_user_id, reasoning, response, user_message, timestamp, session_id "
                "FROM thoughts WHERE user_key = ? ORDER BY timestamp DESC LIMIT 1"
            ),
            (user_key,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            (
                u_key,
                trig,
                reasoning,
                response,
                user_message,
                ts,
                session_id,
            ) = row
            record = {
                "user_key": u_key,
                "trigger_user_id": trig,
                "reasoning": reasoning,
                "response": response,
                "user_message": user_message,
                "timestamp": ts,
                "session_id": session_id,
            }
            # 回填內存快取
            self.records[user_key] = record
            return record

    def get_last_upload_info(self, user_key: str) -> Optional[Dict[str, Any]]:
        """從快取中獲取用戶的最後上傳信息。"""
        return self.last_uploaded_info.get(user_key)

    async def get_last_upload_info_async(self, user_key: str) -> Optional[Dict[str, Any]]:
        """從 SQLite 獲取用戶的最後上傳信息。若內存未命中，將嘗試讀取數據庫。"""
        # 先讀內存快取
        info = self.last_uploaded_info.get(user_key)
        if info is not None:
            return info
        if not self.enable_persistence:
            return None
        await self._ensure_db()
        assert self.db is not None
        async with self.db.execute(
            "SELECT url, breakpoint_timestamp FROM upload_breakpoints WHERE user_key = ?",
            (user_key,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                url, bp = row
                info = {"url": url, "breakpoint_timestamp": bp}
                # 更新內存快取
                self.last_uploaded_info[user_key] = info
                return info
        return None

    def update_last_upload_info(self, user_key: str, url: str, breakpoint_timestamp: str):
        """Update last upload info, keep memory cache and persist to SQLite asynchronously."""
        self.last_uploaded_info[user_key] = {
            'url': url,
            'breakpoint_timestamp': breakpoint_timestamp
        }
        # Maintain cache size
        while len(self.last_uploaded_info) > self.upload_cache_size:
            self.last_uploaded_info.popitem(last=False)
        # Persist asynchronously (fire-and-forget)
        if self.enable_persistence:
            asyncio.create_task(self._update_last_upload_info_db(user_key, url, breakpoint_timestamp))

    async def get_records_since(self, user_key: str, last_timestamp_iso: str, limit: int) -> List[Dict[str, Any]]:
        """Fetch records from SQLite newer than last_timestamp_iso (if provided)."""
        return await self._fetch_records_since_db(user_key, last_timestamp_iso, limit)

    def terminate(self) -> Optional[Coroutine]:
        """Cleanup and close SQLite connection."""
        async def _close():
            if self.db is not None:
                try:
                    await self.db.commit()
                except Exception:
                    pass
                await self.db.close()
                self.db = None
        return _close()
