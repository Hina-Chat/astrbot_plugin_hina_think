import os
import time
import json
import asyncio
import logging
import tempfile
import traceback
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Tuple


from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.provider import LLMResponse
from .r2_upload import upload_file_to_r2
from . import qr_generator
from .persistence import PersistenceManager
from openai.types.chat.chat_completion import ChatCompletion

class R1Filter(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.context = context

        # --- Load non-persistence configs ---
        general_config = self.config.get('general', {})
        self.think_cooldown_seconds = general_config.get('think_cooldown_seconds', 30)
        self.memohina_cooldown_seconds = general_config.get('memohina_cooldown_seconds', 600)
        self.memohina_export_record_count = general_config.get('memohina_export_record_count', 100)
        self.max_think_length = general_config.get('max_think_length', 800)

        # --- Setup persistence ---
        # The data path is relative to this file's location, which is a robust way.
        plugin_data_path = Path(__file__).parent / "data"
        plugin_data_path.mkdir(exist_ok=True)  # Ensure the directory exists

        self.persistence = PersistenceManager(
            config=self.config,
            logger=self.logger,
            data_path=plugin_data_path
        )

        
        # --- Cooldown tracking ---
        self._think_last_used: Dict[str, float] = {}
        self._memohina_last_used: Dict[str, float] = {}

    @property
    def storage_dir(self) -> Path:
        """A shortcut to the storage directory managed by PersistenceManager."""
        return self.persistence.storage_dir

    def on_before_stop(self):
        """AstrBot is shutting down. Perform cleanup and save data."""
        self.logger.info("R1Filter: Termination signal received. Saving all data...")
        final_save_task = self.persistence.terminate()
        if final_save_task:
            # In some environments, we might need to run this to completion
            try:
                # Create a temporary event loop to run the final save
                asyncio.run(final_save_task)
                self.logger.info("R1Filter: Final data save completed successfully.")
            except Exception as e:
                self.logger.error(f"R1Filter: Final save failed during shutdown: {e}")

    def _extract_reasoning(self, response: LLMResponse) -> Optional[str]:
        """從 LLM 響應中提取推理內容 (reasoning_content)"""
        # 优先通过 getattr 检查动态添加的属性（我们为 Gemini 开辟的新通道）
        reasoning = getattr(response, 'reasoning_content', None)
        if reasoning:
            return reasoning

        # 如果新通道没内容，则回退到旧的、解析原始响应的逻辑（兼容 DeepSeek）
        try:
            if not isinstance(response.raw_completion, ChatCompletion):
                return None
            message = response.raw_completion.choices[0].message
            if hasattr(message, 'reasoning_content') and message.reasoning_content:
                return message.reasoning_content
            if hasattr(message, 'reasoning') and message.reasoning:
                return message.reasoning
        except (AttributeError, IndexError):
            pass
        return None

    @filter.on_llm_response()
    async def resp(self, event: AstrMessageEvent, response: LLMResponse):
        """攔截LLM響應，提取並保存思維鏈及對話記錄。"""
        reasoning_content = self._extract_reasoning(response)
        if not reasoning_content:
            return

        user_key, trigger_user_id = await self._get_user_key(event)

        record = {
            "user_key": user_key,  # session_id/scene
            "trigger_user_id": trigger_user_id, # The actual user who sent the message
            "reasoning": reasoning_content,
            "response": response.completion_text or "",
            "user_message": event.get_message_str(),
            "timestamp": datetime.now().isoformat(),
            "session_id": event.get_session_id(), # For backward compatibility or analysis
        }
        await self.persistence.log_thought(record)

    async def _get_user_key(self, event: AstrMessageEvent) -> Tuple[str, str]:
        """
        生成用於存儲和檢索的會話密鑰，並返回消息的觸發者ID。

        :return: A tuple containing (user_key, trigger_user_id)
                 - user_key (str): The key for the conversation log, e.g., 'GROUP_ID/group'.
                 - trigger_user_id (str): The ID of the user who sent the message.
        """
        session_id = str(event.get_session_id())
        trigger_user_id = str(event.get_sender_id())
        scene = "group" if "group" in str(event.unified_msg_origin).lower() else "dm"

        # The key is always based on the session ID to group conversations correctly.
        user_key = f"{session_id}/{scene}"

        return user_key, trigger_user_id

    @filter.command("think", alias={'思考', '思維'})
    async def think_command(self, event: AstrMessageEvent):
        user_key, trigger_user_id = await self._get_user_key(event)

        # --- Cooldown Check (based on the session) ---
        if self.think_cooldown_seconds > 0:
            now = time.time()
            last_used = self._think_last_used.get(user_key, 0)
            if now - last_used < self.think_cooldown_seconds:
                remaining = self.think_cooldown_seconds - (now - last_used)
                yield event.plain_result(f"{remaining:.1f} 後，可一窺本質。")
                return
            self._think_last_used[user_key] = now

        # Get the last record from the entire session's cache
        last_record = self.persistence.get_last_thought(user_key)

        if not last_record:
            yield event.plain_result("我還沒有思考過什麼……")
            return

        reasoning = last_record.get('reasoning', '這次我沒有留下思考的痕跡。')
        if len(reasoning) > self.max_think_length:
            reasoning = reasoning[:self.max_think_length] + "..."
        
        yield event.plain_result(f"""Hina 的思考:
---
{reasoning}""")

    @filter.command("memohina", alias={'導出hina思考', '導出hina記憶'})
    async def memohina_command(self, event: AstrMessageEvent):
        """Exports the user's thought records, providing an R2 download link and QR code."""
        temp_log_path = None
        try:
            user_key, trigger_user_id = await self._get_user_key(event)

            # 1. Cooldown Check (based on the session)
            if self.memohina_cooldown_seconds > 0:
                now = time.time()
                last_used = self._memohina_last_used.get(user_key, 0)
                if now - last_used < self.memohina_cooldown_seconds:
                    remaining = self.memohina_cooldown_seconds - (now - last_used)
                    yield event.plain_result(f"記憶之匣尚在冷卻，請於 {remaining:.1f} 秒後再試。")
                    return
                self._memohina_last_used[user_key] = now

            # 2. Get last export breakpoint from persistence
            last_upload_info = self.persistence.get_last_upload_info(user_key) or {}
            breakpoint_timestamp = last_upload_info.get('breakpoint_timestamp')

            records = await self.persistence.get_records_since(
                user_key,
                last_timestamp_iso=breakpoint_timestamp,
                limit=self.memohina_export_record_count
            )

            # 3. Handle no new records
            if not records:
                if breakpoint_timestamp:
                    yield event.plain_result("我們之間還沒有新的記憶呢……")
                else:
                    yield event.plain_result("我們之間沒有更多記憶呢……")
                return

            # 4. Write records to a temporary file
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".json", encoding='utf-8') as temp_f:
                temp_log_path = Path(temp_f.name)
                json.dump(records, temp_f, ensure_ascii=False, indent=4)
            
            # 5. Determine the new breakpoint from the last record fetched
            new_breakpoint = records[-1].get('timestamp')

            # 6. Upload to R2
            r2_config = self.config.get('r2', {})
            if not all([r2_config.get('r2_account_id'), r2_config.get('r2_access_key_id'), r2_config.get('r2_secret_access_key'), r2_config.get('r2_bucket_name')]):
                self.logger.error("R1Filter: R2 configuration is incomplete. Cannot upload file.")
                yield event.plain_result("嗯…… R2 似乎有所不妥。")
                return

            # 7. Create a stable and unique R2 object key using the new path logic
            now_str = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{now_str}.json"

            try:
                session_id, scene = user_key.split('/', 1)
                if scene == 'group':
                    r2_path_prefix = f"GP/{session_id}"
                elif scene == 'dm':
                    r2_path_prefix = f"DM/{session_id}"
                else:
                    # Fallback for unknown scenes, preserving original user_key but sanitizing it
                    r2_path_prefix = f"other/{user_key.replace('/', '_')}"
            except ValueError:
                # Fallback for malformed user_key, sanitizing it
                r2_path_prefix = f"malformed/{user_key.replace('/', '_')}"

            object_key = f"hina_memory/{r2_path_prefix}/{filename}"
            self.logger.info(f"R1Filter: Uploading to R2 with stable object key: {object_key} for user {user_key}")

            r2_url = await asyncio.to_thread(
                upload_file_to_r2,
                local_path=temp_log_path,
                object_key=object_key,
                r2_account_id=r2_config['r2_account_id'],
                r2_access_key_id=r2_config['r2_access_key_id'],
                r2_secret_access_key=r2_config['r2_secret_access_key'],
                r2_bucket_name=r2_config['r2_bucket_name'],
                r2_custom_domain=r2_config.get('r2_custom_domain')
            )

            if not r2_url:
                yield event.plain_result("R2 的通信時斷時續……")
                return
            
            # 8. Update cache in persistence layer with the new breakpoint
            self.logger.info(f"R1Filter: Updating cache for user {user_key} with new breakpoint {new_breakpoint}")
            self.persistence.update_last_upload_info(user_key, r2_url, new_breakpoint)

            # 9. Generate and send QR Code
            with tempfile.TemporaryDirectory() as temp_dir_str:
                temp_dir = Path(temp_dir_str)
                async for result_type, content in qr_generator.generate_qr_code(
                    url=r2_url, 
                    qr_config=self.config.get('qrcode', {}), 
                    logger=self.logger, 
                    storage_dir=self.storage_dir,
                    temp_dir=temp_dir
                ):
                    if result_type == 'image':
                        yield event.image_result(content)
                    else:
                        yield event.plain_result(content)

        except Exception as e:
            self.logger.error(f"R1Filter: /memohina command failed: {e}\n{traceback.format_exc()}")
            yield event.plain_result(f"嗯……？: {e}")
        finally:
            # 清理臨時檔案
            if temp_log_path and temp_log_path.exists():
                try:
                    os.remove(temp_log_path)
                except OSError as e:
                    self.logger.error(f"R1Filter: Error cleaning up temp log file {temp_log_path}: {e}")


