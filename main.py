import json
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from openai.types.chat.chat_completion import ChatCompletion


@register("r1-filter", "Hina", "過濾推理模型思維鏈，並在需要偵錯時使用 /think 顯示思維鏈", "3.0.1", 'https://github.com/Magstic/astrbot_plugin_r1_filter_hina')
class R1Filter(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # --- 極簡化配置 ---
        self.enable_persistence = self.config.get('enable_persistence', True)
        self.max_think_length = self.config.get('max_think_length', 800)
        self.log_rotation_count = self.config.get('log_rotation_count', 20)
        
        # --- 統一的儲存與快取機制 ---
        storage_dir_config = self.config.get('storage_dir', '')
        self.storage_dir = Path(storage_dir_config) if storage_dir_config else Path(__file__).parent / 'hina_thoughts_data'
        self.cache_file = self.storage_dir / 'hina_thoughts_cache.json'
        
        # --- 內存記錄與異步鎖 ---
        # 結構: { "user_key": { ...record... } }
        self.records: Dict[str, Dict] = {}
        self._cache_lock = asyncio.Lock()
        self._log_lock = asyncio.Lock()
        self._loaded = asyncio.Event()

        # --- 初始化 ---
        if self.enable_persistence:
            self._init_storage()
            asyncio.create_task(self._load_data_from_cache())
        else:
            self._loaded.set() # 如果不啟用持久化，直接標記為已加載

    def _init_storage(self):
        """初始化儲存目錄"""
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"R1Filter: 儲存目錄位於: {self.storage_dir}")
        except Exception as e:
            self.logger.error(f"R1Filter: 儲存目錄初始化失敗: {e}")
            self.enable_persistence = False

    async def _load_data_from_cache(self):
        """從單一快取檔案異步載入所有記錄，完成後設置事件。"""
        try:
            if self.cache_file.exists() and self.cache_file.stat().st_size > 0:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    self.records = json.load(f)
                self.logger.info(f"R1Filter: 成功從快取載入 {len(self.records)} 條記錄")
            else:
                self.logger.info("R1Filter: 快取檔案不存在或為空，跳過載入。")
        except (json.JSONDecodeError, IOError, OSError) as e:
            self.logger.error(f"R1Filter: 載入快取檔案失敗: {e}")
        finally:
            self._loaded.set() # 無論成功或失敗，都標記為加載完成

    async def _log_thought(self, record: Dict):
        """將單條思維鏈記錄附加到輪轉的日誌檔案中。"""
        await self._loaded.wait()
        if not self.enable_persistence or self.log_rotation_count <= 0:
            return

        async with self._log_lock:
            # 1. 尋找當前的日誌檔案
            try:
                log_files = sorted(
                    self.storage_dir.glob('hina_thoughts_log_*.json'),
                    key=lambda p: int(p.stem.split('_')[-1])
                )
            except (ValueError, IndexError):
                self.logger.warning("R1Filter: Found malformed log filenames. Starting new log.")
                log_files = []

            current_log_file_path = None
            log_data = []

            if log_files:
                latest_log_file = log_files[-1]
                try:
                    with open(latest_log_file, 'r', encoding='utf-8') as f:
                        existing_data = json.load(f)
                    
                    if isinstance(existing_data, list) and len(existing_data) < self.log_rotation_count:
                        current_log_file_path = latest_log_file
                        log_data = existing_data
                    else:
                        # 檔案已滿或格式不對，創建新檔案
                        new_index = int(latest_log_file.stem.split('_')[-1]) + 1
                        current_log_file_path = self.storage_dir / f"hina_thoughts_log_{new_index}.json"
                except (IOError, json.JSONDecodeError, ValueError, IndexError):
                    # 檔案損壞或無法解析，創建新檔案
                    self.logger.warning(f"R1Filter: Could not read or parse {latest_log_file.name}, creating a new log file.")
                    new_index = int(latest_log_file.stem.split('_')[-1]) + 1
                    current_log_file_path = self.storage_dir / f"hina_thoughts_log_{new_index}.json"
            else:
                # 尚無任何日誌檔案
                current_log_file_path = self.storage_dir / "hina_thoughts_log_1.json"

            # 2. 附加新記錄
            log_data.append(record)

            # 3. 原子寫入
            try:
                temp_file = current_log_file_path.with_suffix('.json.tmp')
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(log_data, f, ensure_ascii=False, indent=4)
                os.replace(temp_file, current_log_file_path)
                self.logger.debug(f"R1Filter: Logged thought to {current_log_file_path.name}")
            except (IOError, TypeError) as e:
                self.logger.error(f"R1Filter: Failed to write to log file {current_log_file_path.name}: {e}")

    async def _save_data_to_cache(self):
        """將所有記錄異步、安全地保存到單一快取檔案（原子寫入）。"""
        await self._loaded.wait() # 確保在保存前，初始加載已完成
        if not self.enable_persistence:
            return
        
        temp_file = self.cache_file.with_suffix('.json.tmp')
        
        async with self._cache_lock:
            try:
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(self.records, f, ensure_ascii=False, indent=4)
                os.replace(temp_file, self.cache_file)
            except (IOError, TypeError) as e:
                self.logger.error(f"R1Filter: 保存快取檔案失敗: {e}")
                if temp_file.exists():
                    try:
                        os.remove(temp_file)
                    except OSError:
                        pass

    async def _get_user_key(self, event: AstrMessageEvent) -> str:
        """生成使用者+會話的唯一鍵"""
        uid = event.unified_msg_origin
        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            return f"{uid}_{curr_cid}"
        except Exception:
            return str(uid) # 備份方案

    def _extract_reasoning(self, response: LLMResponse) -> Optional[str]:
        """從 LLM 響應中提取推理內容 (reasoning_content)"""
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
        await self._loaded.wait() # 等待初始數據加載完成
        
        reasoning_content = self._extract_reasoning(response)
        if not reasoning_content:
            return

        user_key = await self._get_user_key(event)
        
        record = {
            'user_key': user_key, # For context in log files
            'reasoning': reasoning_content,
            'response': response.completion_text or "",
            'user_message': getattr(event, 'message_str', ''),
            'timestamp': datetime.now().isoformat(),
            'user_id': str(event.unified_msg_origin)
        }
        
        # 更新會話的最新記錄 (用於 /think 指令 和 重啟後恢復狀態)
        self.records[user_key] = record

        # 異步保存會話快照與寫入永久日誌
        asyncio.create_task(self._save_data_to_cache())
        asyncio.create_task(self._log_thought(record))

    @filter.command("think", alias={'思考', '思維鏈'})
    async def think_command(self, event: AstrMessageEvent):
        """顯示上次對話的思維鏈，並將其匯出為JSON檔案。"""
        await self._loaded.wait() # 等待初始數據加載完成
        
        user_key = await self._get_user_key(event)
        record = self.records.get(user_key)

        if not record:
            yield event.plain_result("過度的思考或許是毒藥呢……")
            return

        reasoning = record.get('reasoning', '空的思維……')
        
        display_reasoning = reasoning
        if len(reasoning) > self.max_think_length:
            display_reasoning = reasoning[:self.max_think_length] + "\n\n...(……)"
        
        yield event.plain_result(f"秘神流雛的内心：\n\n{display_reasoning}")

        if self.enable_persistence:
            temp_output_path = None
            try:
                now = datetime.now()
                user_id = record.get('user_id', 'unknown_user')
                filename = f"thought_{user_id}_{now.strftime('%Y%m%d_%H%M%S')}.json"
                output_path = self.storage_dir / filename
                temp_output_path = output_path.with_suffix('.json.tmp')

                with open(temp_output_path, 'w', encoding='utf-8') as f:
                    json.dump(record, f, ensure_ascii=False, indent=4)
                
                os.replace(temp_output_path, output_path)
                
                self.logger.info(f"已成功將思維鏈匯出到: {output_path}")
                yield event.plain_result(f"（優雅地旋舞）")

            except (IOError, TypeError) as e:
                self.logger.error(f"匯出思維鏈到JSON檔案失敗: {e}")
                yield event.plain_result("（優雅地旋轉）")
                if temp_output_path and temp_output_path.exists():
                    try:
                        os.remove(temp_output_path)
                    except OSError:
                        pass