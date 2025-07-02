import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone
from collections import OrderedDict
from typing import Any, Dict, Optional, List, Coroutine

import aiofiles



class PersistenceManager:
    """Manages all data persistence, including file I/O and caching."""

    def __init__(self, config: dict, logger: logging.Logger, data_path: Path):
        self.config = config
        self.logger = logger
        self.data_path = data_path

        # --- Load config ---
        general_config = self.config.get('general', {})
        self.enable_persistence = general_config.get('enable_persistence', True)
        self.log_rotation_count = general_config.get('log_rotation_count', 20)
        self.save_interval_seconds = general_config.get('save_interval_seconds', 60)
        self.user_inactivity_timeout_seconds = general_config.get('user_inactivity_timeout_seconds', 300)
        self.upload_cache_size = general_config.get('upload_cache_size', 1000)
        storage_dir_config = general_config.get('storage_dir', 'hina_thoughts_data')
        self.storage_dir = self.data_path / storage_dir_config if storage_dir_config else self.data_path

        # --- Initialize state ---
        self.records: Dict[str, Dict[str, Any]] = {}
        self.last_uploaded_info: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self.user_save_tasks: Dict[str, asyncio.Task] = {}
        self.cache_file_path = self.storage_dir / 'cache.json'

        if self.enable_persistence:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            self._load_cache_from_disk()
            self.periodic_save_task = asyncio.create_task(self._save_inactive_user_data_periodically())
        else:
            self.periodic_save_task = None

    def _load_cache_from_disk(self):
        """從磁盤加載快取檔案 (cache.json)。"""
        if self.cache_file_path.exists():
            try:
                with open(self.cache_file_path, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    self.records = cache_data.get('records', {})
                    # Load as OrderedDict to preserve insertion order for cache eviction
                    self.last_uploaded_info = OrderedDict(cache_data.get('last_uploaded_info', []))
                    self.logger.info(f"R1Filter: Successfully loaded cache from {self.cache_file_path}")
            except (json.JSONDecodeError, IOError) as e:
                self.logger.error(f"R1Filter: Failed to load cache file: {e}")

    async def _save_cache_to_disk_async(self):
        """異步將內存中的快取數據寫入磁盤。"""
        if not self.enable_persistence:
            return
        try:
            cache_data = {
                'records': self.records,
                'last_uploaded_info': list(self.last_uploaded_info.items()) # Convert OrderedDict to list of tuples for JSON
            }
            temp_file = self.cache_file_path.with_suffix('.json.tmp')
            async with aiofiles.open(temp_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(cache_data, ensure_ascii=False, indent=4))
            await asyncio.to_thread(os.replace, temp_file, self.cache_file_path)
            self.logger.info(f"R1Filter: Cache saved successfully to {self.cache_file_path}")
        except (IOError, TypeError) as e:
            self.logger.error(f"R1Filter: Failed to save cache to disk: {e}")

    def _manage_user_save_task(self, user_key: str):
        """管理用戶數據的延遲保存任務，實現去抖動。"""
        if not self.enable_persistence:
            return
        if user_key in self.user_save_tasks and not self.user_save_tasks[user_key].done():
            self.user_save_tasks[user_key].cancel()
        
        async def _save_after_timeout():
            await asyncio.sleep(self.user_inactivity_timeout_seconds)
            await self._save_cache_to_disk_async()
            self.logger.info(f"R1Filter: User {user_key} data saved due to inactivity.")

        self.user_save_tasks[user_key] = asyncio.create_task(_save_after_timeout())

    async def _save_inactive_user_data_periodically(self):
        """定期檢查並保存所有非活躍用戶的數據。"""
        while True:
            await asyncio.sleep(self.save_interval_seconds)
            self.logger.debug("R1Filter: Running periodic check to save all user data.")
            await self._save_cache_to_disk_async()

    async def log_thought(self, record: dict):
        """將單條思維鏈記錄寫入對應的用戶日誌檔案中。"""
        if not self.enable_persistence:
            return

        user_key = record.get('user_key', 'unknown_session')

        # Update in-memory cache first for immediate availability to /think
        self.records[user_key] = record
        # Trigger a debounced save to persist the cache to disk later
        self._manage_user_save_task(user_key)
        # New path logic based on user_key (e.g., 'ID/scene')
        try:
            session_id, scene = user_key.split('/', 1)
        except ValueError:
            self.logger.warning(f"R1Filter: Malformed user_key '{user_key}', using fallback directory.")
            sanitized_key = user_key.replace('/', '_')
            session_dir = self.storage_dir / 'malformed' / sanitized_key
        else:
            if scene == 'group':
                session_dir = self.storage_dir / 'GP' / session_id
            elif scene == 'dm':
                session_dir = self.storage_dir / 'DM' / session_id
            else:
                self.logger.warning(f"R1Filter: Unknown scene '{scene}' in user_key, using fallback directory.")
                sanitized_key = user_key.replace('/', '_')
                session_dir = self.storage_dir / 'other' / sanitized_key
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.logger.error(f"R1Filter: Could not create session directory {session_dir}: {e}")
            return

        log_files = sorted(session_dir.glob('*.json'), key=os.path.getmtime, reverse=True)
        # Use the new filename format without 'export_' prefix.
        current_log_file_path = session_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        log_data = []

        if log_files:
            latest_log_file = log_files[0]
            try:
                async with aiofiles.open(latest_log_file, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    log_data = json.loads(content)
                current_log_file_path = latest_log_file
            except (IOError, json.JSONDecodeError) as e:
                self.logger.warning(f"R1Filter: Could not read or parse latest log file {latest_log_file}, creating a new one. Error: {e}")
        
        log_data.append(record)

        try:
            temp_file = current_log_file_path.with_suffix('.json.tmp')
            async with aiofiles.open(temp_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(log_data, ensure_ascii=False, indent=4))
            await asyncio.to_thread(os.replace, temp_file, current_log_file_path)
            self.logger.debug(f"R1Filter: Logged thought to {current_log_file_path}")
        except (IOError, TypeError) as e:
            self.logger.error(f"R1Filter: Failed to write to log file {current_log_file_path}: {e}")

        if len(log_files) >= self.log_rotation_count:
            for old_log in log_files[self.log_rotation_count-1:]:
                try:
                    os.remove(old_log)
                    self.logger.info(f"R1Filter: Rotated and removed old log file: {old_log}")
                except OSError as e:
                    self.logger.error(f"R1Filter: Error removing old log file {old_log}: {e}")

    def get_last_thought(self, user_key: str) -> Optional[Dict[str, Any]]:
        """從快取中獲取用戶的最新思維記錄。"""
        return self.records.get(user_key)

    def get_last_upload_info(self, user_key: str) -> Optional[Dict[str, Any]]:
        """從快取中獲取用戶的最後上傳信息。"""
        return self.last_uploaded_info.get(user_key)

    def update_last_upload_info(self, user_key: str, url: str, breakpoint_timestamp: str):
        """更新用戶的最後上傳信息並管理快取大小。"""
        self.last_uploaded_info[user_key] = {
            'url': url,
            'breakpoint_timestamp': breakpoint_timestamp
        }
        # Maintain cache size
        while len(self.last_uploaded_info) > self.upload_cache_size:
            self.last_uploaded_info.popitem(last=False)
        
        # Since this is a significant update, trigger a debounced save
        self._manage_user_save_task(user_key)

    async def get_records_since(self, user_key: str, last_timestamp_iso: str, limit: int) -> List[Dict[str, Any]]:
        """從用戶的日誌檔案中獲取指定時間戳之後的所有記錄。"""
        # New path logic based on user_key (e.g., 'ID/scene')
        try:
            session_id, scene = user_key.split('/', 1)
        except ValueError:
            self.logger.warning(f"R1Filter: Malformed user_key '{user_key}', using fallback directory.")
            sanitized_key = user_key.replace('/', '_')
            session_dir = self.storage_dir / 'malformed' / sanitized_key
        else:
            if scene == 'group':
                session_dir = self.storage_dir / 'GP' / session_id
            elif scene == 'dm':
                session_dir = self.storage_dir / 'DM' / session_id
            else:
                self.logger.warning(f"R1Filter: Unknown scene '{scene}' in user_key, using fallback directory.")
                sanitized_key = user_key.replace('/', '_')
                session_dir = self.storage_dir / 'other' / sanitized_key
        if not session_dir.exists():
            return []

        last_timestamp = datetime.fromisoformat(last_timestamp_iso) if last_timestamp_iso else datetime.min.replace(tzinfo=timezone.utc)

        all_records = []
        log_files = sorted(session_dir.glob('*.json'), key=os.path.getmtime, reverse=True)

        for log_file in log_files:
            try:
                async with aiofiles.open(log_file, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    records_in_file = json.loads(content)
                    all_records.extend(records_in_file)
            except (IOError, json.JSONDecodeError) as e:
                self.logger.warning(f"R1Filter: Could not read or parse log file {log_file}: {e}")
                continue
        
        # Sort all records by timestamp descending to find the newest ones first
        all_records.sort(key=lambda r: r.get('timestamp', ''), reverse=True)

        new_records = []
        for record in all_records:
            record_ts_str = record.get('timestamp')
            if not record_ts_str:
                continue
            record_ts = datetime.fromisoformat(record_ts_str)
            # Ensure timezone awareness for comparison
            if record_ts.tzinfo is None:
                record_ts = record_ts.replace(tzinfo=timezone.utc)
            if last_timestamp.tzinfo is None:
                last_timestamp = last_timestamp.replace(tzinfo=timezone.utc)

            if record_ts > last_timestamp:
                new_records.append(record)
            else:
                # Since records are sorted, we can stop once we are past the last timestamp
                break
        
        # Return the newest records, up to the limit, in chronological order
        return sorted(new_records, key=lambda r: r.get('timestamp', ''))[:limit]

    def terminate(self) -> Optional[Coroutine]:
        """清理並保存所有數據。"""
        if self.periodic_save_task:
            self.periodic_save_task.cancel()
        return self._save_cache_to_disk_async()
