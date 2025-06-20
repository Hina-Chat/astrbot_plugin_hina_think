import json
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
import hashlib
import traceback
from collections import OrderedDict

from . import r2_upload
import qrcode
import io
import tempfile
import aiohttp
from PIL import Image
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers import (
    SquareModuleDrawer, GappedSquareModuleDrawer, CircleModuleDrawer, RoundedModuleDrawer
)
from qrcode.image.styles.colormasks import ImageColorMask

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

        # --- 分組加載配置 ---
        general_config = self.config.get('general', {})
        qrcode_config = self.config.get('qrcode', {})

        self.upload_cache_size = general_config.get('upload_cache_size', 1000)

        self.enable_persistence = general_config.get('enable_persistence', True)
        self.max_think_length = general_config.get('max_think_length', 800)
        self.log_rotation_count = general_config.get('log_rotation_count', 20)
        storage_dir_config = general_config.get('storage_dir', '')

        # --- QR Code 樣式配置 ---
        self.qr_box_size = qrcode_config.get('qr_box_size', 5)
        self.qr_border = qrcode_config.get('qr_border', 2)
        self.qr_module_drawer = qrcode_config.get('qr_module_drawer', 'square')
        self.qr_image_mask_path = qrcode_config.get('qr_image_mask_path', '')
        self.qr_logo_path = qrcode_config.get('qr_logo_path', '')
        self.storage_dir = Path(storage_dir_config) if storage_dir_config else Path(__file__).parent / 'hina_thoughts_data'
        self.cache_file = self.storage_dir / 'hina_thoughts_cache.json'
        
        # --- 內存記錄與異步鎖 ---
        # 結構: { "user_key": { ...record... } }
        self.records: Dict[str, Dict] = {}
        self._cache_lock = asyncio.Lock()
        self._log_lock = asyncio.Lock()
        self._loaded = asyncio.Event()

        # --- 用於追蹤 R2 上傳狀態 (LRU 快取) ---
        # 結構: { "user_key": {"hash": "...", "url": "..."} }
        self.last_uploaded_info: OrderedDict[str, Dict[str, str]] = OrderedDict()

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
        """將單條思維鏈記錄附加到會話專屬輪轉日誌檔案中。"""
        await self._loaded.wait()
        if not self.enable_persistence or self.log_rotation_count <= 0:
            return

        # 依據 user_key 分類到子資料夾
        user_key = record.get('user_key', 'unknown_session')
        simple_key = self._simplify_user_key(user_key)
        session_dir = self.storage_dir / 'session' / simple_key
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.logger.error(f"R1Filter: 無法建立會話資料夾 {session_dir}: {e}")
            return

        async with self._log_lock:
            # 1. 尋找會話子資料夾下的日誌檔案
            try:
                log_files = sorted(
                    session_dir.glob('hina_thoughts_log_*.json'),
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
                        current_log_file_path = session_dir / f"hina_thoughts_log_{new_index}.json"
                except (IOError, json.JSONDecodeError, ValueError, IndexError):
                    # 檔案損壞或無法解析，創建新檔案
                    self.logger.warning(f"R1Filter: Could not read or parse {latest_log_file.name}, creating a new log file.")
                    new_index = int(latest_log_file.stem.split('_')[-1]) + 1
                    current_log_file_path = session_dir / f"hina_thoughts_log_{new_index}.json"
            else:
                # 尚無任何日誌檔案
                current_log_file_path = session_dir / "hina_thoughts_log_1.json"

            # 2. 附加新記錄
            log_data.append(record)

            # 3. 原子寫入
            try:
                temp_file = current_log_file_path.with_suffix('.json.tmp')
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(log_data, f, ensure_ascii=False, indent=4)
                os.replace(temp_file, current_log_file_path)
                self.logger.debug(f"R1Filter: Logged thought to {current_log_file_path}")
            except (IOError, TypeError) as e:
                self.logger.error(f"R1Filter: Failed to write to log file {current_log_file_path}: {e}")

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

    def _simplify_user_key(self, user_key: str) -> str:
        """根據自定規則將 user_key 簡化為目錄安全、可讀的短 key"""
        mapping = {
            "qq_official_webhook:FriendMessage": "QOFM",
            "qq_official_webhook:GroupMessage": "QOGM"
        }
        try:
            # 先分離 uuid
            if "_" in user_key:
                prefix, uuid = user_key.rsplit("_", 1)
            else:
                prefix, uuid = user_key, ""
            prefix_parts = prefix.split(":")
            type_key = ":".join(prefix_parts[:2]) if len(prefix_parts) >= 2 else prefix
            id_key = prefix_parts[2] if len(prefix_parts) > 2 else ""
            short_type = mapping.get(type_key, "UNK")
            short_id = id_key[:10]
            uuid_no_dash = uuid.replace("-", "")
            short_uuid = uuid_no_dash[:4] + uuid_no_dash[-5:] if uuid_no_dash else ""
            return f"{short_type}{short_id}{short_uuid}"
        except Exception:
            return user_key.replace(":", "_").replace("-", "_")

    async def _get_user_key(self, event: AstrMessageEvent) -> str:
        """生成使用者+會話的唯一鍵（原始）"""
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

    def _get_module_drawer(self):
        """根據配置返回對應的碼點形狀繪製器。"""
        drawers = {
            "square": qrcode.image.styledpil.SquareModuleDrawer(),
            "gapped": qrcode.image.styledpil.GappedSquareModuleDrawer(),
            "circle": qrcode.image.styledpil.CircleModuleDrawer(),
            "rounded": qrcode.image.styledpil.RoundedModuleDrawer(),
        }
        drawer_name = self.qr_module_drawer.lower()
        return drawers.get(drawer_name, qrcode.image.styledpil.SquareModuleDrawer())

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
                    current_log_file_path = session_dir / f"hina_thoughts_log_{new_index}.json"
            except (IOError, json.JSONDecodeError, ValueError, IndexError):
                # 檔案損壞或無法解析，創建新檔案
                self.logger.warning(f"R1Filter: Could not read or parse {latest_log_file.name}, creating a new log file.")
                new_index = int(latest_log_file.stem.split('_')[-1]) + 1
                current_log_file_path = session_dir / f"hina_thoughts_log_{new_index}.json"
        else:
            # 尚無任何日誌檔案
            current_log_file_path = session_dir / "hina_thoughts_log_1.json"

        # 2. 附加新記錄
        log_data.append(record)

        # 3. 原子寫入
        try:
            temp_file = current_log_file_path.with_suffix('.json.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, ensure_ascii=False, indent=4)
            os.replace(temp_file, current_log_file_path)
            self.logger.debug(f"R1Filter: Logged thought to {current_log_file_path}")
        except (IOError, TypeError) as e:
            self.logger.error(f"R1Filter: Failed to write to log file {current_log_file_path}: {e}")

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

def _simplify_user_key(self, user_key: str) -> str:
    """根據自定規則將 user_key 簡化為目錄安全、可讀的短 key"""
    mapping = {
        "qq_official_webhook:FriendMessage": "QOFM",
        "qq_official_webhook:GroupMessage": "QOGM"
    }
    try:
        # 先分離 uuid
        if "_" in user_key:
            prefix, uuid = user_key.rsplit("_", 1)
        else:
            prefix, uuid = user_key, ""
        prefix_parts = prefix.split(":")
        type_key = ":".join(prefix_parts[:2]) if len(prefix_parts) >= 2 else prefix
        id_key = prefix_parts[2] if len(prefix_parts) > 2 else ""
        short_type = mapping.get(type_key, "UNK")
        short_id = id_key[:10]
        uuid_no_dash = uuid.replace("-", "")
        short_uuid = uuid_no_dash[:4] + uuid_no_dash[-5:] if uuid_no_dash else ""
        return f"{short_type}{short_id}{short_uuid}"
    except Exception:
        return user_key.replace(":", "_").replace("-", "_")

async def _get_user_key(self, event: AstrMessageEvent) -> str:
    """生成使用者+會話的唯一鍵（原始）"""
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

def _get_module_drawer(self):
    """根據配置返回對應的碼點形狀繪製器。"""
    drawers = {
        'square': SquareModuleDrawer(),
        'gapped': GappedSquareModuleDrawer(),
        'circle': CircleModuleDrawer(),
        'rounded': RoundedModuleDrawer(),
    }
    return drawers.get(self.qr_module_drawer, SquareModuleDrawer())

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
        
    yield event.plain_result(f"秘神流雛：\n\n{display_reasoning}")

    if self.enable_persistence:
        temp_output_path = None
        try:
            now = datetime.now()
            user_id = record.get('user_id', 'unknown_user')
            # 將檔案存放到 hina_thoughts_data/think/[user_id]/
            think_dir = self.storage_dir / 'think' / str(user_id)
            think_dir.mkdir(parents=True, exist_ok=True)
            filename = f"thought_{user_id}_{now.strftime('%Y%m%d_%H%M%S')}.json"
            output_path = think_dir / filename
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

@filter.command("memohina", alias={"記憶", "导出会话"})
async def memohina_command(self, event: AstrMessageEvent):
    """上載該用戶當前會話最新日誌檔案到 R2 並返回鏈接，如果內容無變更則返回快取鏈接。"""
    await self._loaded.wait()
    try:
        user_key = await self._get_user_key(event)
        simple_key = self._simplify_user_key(user_key)
        session_dir = self.storage_dir / 'session' / simple_key

        log_files = sorted(
            session_dir.glob('hina_thoughts_log_*.json'),
            key=lambda p: int(p.stem.split('_')[-1])
        )
        if not log_files:
            yield event.plain_result("我們之間沒有更多記憶呢……")
            return

        latest_log = log_files[-1]

        # 1. 計算檔案哈希值
        current_hash = self._calculate_file_hash(latest_log)
        if not current_hash:
            yield event.plain_result("無法處理您的記憶檔案，請檢查日誌。")
        # 2. 檢查快取，如果哈希值相同，直接返回舊的 URL
        last_upload = self.last_uploaded_info.get(user_key)
        if last_upload and last_upload.get('hash') == current_hash:
            self.logger.info(f"R1Filter: 檔案未變更 for user {user_key}，直接返回快取 URL。")
            # 將訪問的鍵移到末尾，標記為最新使用
            self.last_uploaded_info.move_to_end(user_key)
            yield event.plain_result("您的記憶沒有變化，這是之前的厄運之書……")
            async for result in self._generate_and_yield_qr_code(last_upload.get('url'), event):
                yield result
            return

        # 3. 內容已變更或首次上傳，執行上傳流程
        self.logger.info(f"R1Filter: 檔案已變更 for user {user_key}，開始上傳至 R2。")
        now = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_filename = f"{simple_key}_{now}.json"
        object_key = f"memory/{simple_key}/{safe_filename}"

        r2_config = self.config.get('r2', {})
        r2_account_id = r2_config.get('r2_account_id', '')
        r2_access_key_id = r2_config.get('r2_access_key_id', '')
        r2_secret_access_key = r2_config.get('r2_secret_access_key', '')
        r2_bucket_name = r2_config.get('r2_bucket_name', '')
        r2_custom_domain = r2_config.get('r2_custom_domain', '')

        url = await asyncio.to_thread(
            r2_upload.upload_file_to_r2,
            latest_log, object_key,
            r2_account_id, r2_access_key_id, r2_secret_access_key, r2_bucket_name, r2_custom_domain
        )
        # 4. 上傳成功後，更新快取
        self.last_uploaded_info[user_key] = {"hash": current_hash, "url": url}
        # 將剛剛訪問的鍵移到末尾
        self.last_uploaded_info.move_to_end(user_key)
        
        # 檢查快取大小，如果超過限制則移除最舊的條目
        if len(self.last_uploaded_info) > self.upload_cache_size:
            oldest_key, _ = self.last_uploaded_info.popitem(last=False)
            self.logger.info(f"R1Filter: 快取已滿，移除最舊的記錄: {oldest_key}")

        self.logger.info(f"R1Filter: 檔案上傳成功，更新快取 for user {user_key}")

        yield event.plain_result("請收好您的厄運之書……")
        async for result in self._generate_and_yield_qr_code(url, event):
            yield result

    except Exception as e:
        self.logger.error(f"/memohina 指令失敗: {e}\n{traceback.format_exc()}")
        yield event.plain_result(f"記憶導出失敗，後台日誌顯示錯誤: {type(e).__name__}")

async def _generate_and_yield_qr_code(self, url: str, event: AstrMessageEvent):
    """根據 URL 生成並發送 QR Code"""
    temp_image_path = None
    temp_logo_path = None
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=self.qr_box_size,
            border=self.qr_border,
        )
        qr.add_data(url)

        image_mask = None
        if self.qr_image_mask_path:
            mask_path = self.qr_image_mask_path
            try:
                if mask_path.lower().startswith(('http://', 'https://')):
                    async with aiohttp.ClientSession() as session:
                        async with session.get(mask_path) as response:
                            response.raise_for_status()
                            image_data = await response.read()
                            image_mask = Image.open(io.BytesIO(image_data)).convert('RGBA')
                elif Path(mask_path).is_file():
                    image_mask = Image.open(mask_path).convert('RGBA')
            except Exception as e:
                self.logger.error(f"R1Filter: 處理圖片遮罩失敗: {e}")

        embedded_logo_path = None
        if self.qr_logo_path:
            logo_path_str = self.qr_logo_path
            try:
                if logo_path_str.lower().startswith(('http://', 'https://')):
                    async with aiohttp.ClientSession() as session:
                        async with session.get(logo_path_str) as response:
                            response.raise_for_status()
                            image_data = await response.read()
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as temp_logo:
                                temp_logo.write(image_data)
                                temp_logo_path = temp_logo.name
                            embedded_logo_path = temp_logo_path
                elif Path(logo_path_str).is_file():
                    embedded_logo_path = logo_path_str
            except Exception as e:
                self.logger.error(f"R1Filter: 處理中心 Logo 失敗: {e}")

        make_image_args = {
            'image_factory': StyledPilImage,
            'module_drawer': self._get_module_drawer(),
        }
        if image_mask:
            make_image_args['color_mask'] = ImageColorMask(
                color_mask_image=image_mask,
                back_color=(255, 255, 255, 0)
            )
        if embedded_logo_path:
            make_image_args['embedded_image_path'] = embedded_logo_path

        qr_img = qr.make_image(**make_image_args)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_image:
            qr_img.save(temp_image, format='PNG')
            temp_image_path = temp_image.name
        
        yield event.image_result(temp_image_path)

    except Exception as e:
        self.logger.error(f"R1Filter: 生成 QR Code 時發生未知錯誤: {e}")
        yield event.plain_result("生成分享二維碼時出錯，請檢查日誌。")
    finally:
        if temp_image_path and os.path.exists(temp_image_path):
            try:
                os.remove(temp_image_path)
            except OSError as e:
                self.logger.error(f"R1Filter: 無法刪除臨時 QR Code 檔案: {temp_image_path}, error: {e}")
        if temp_logo_path and os.path.exists(temp_logo_path):
            try:
                os.remove(temp_logo_path)
            except OSError as e:
                self.logger.error(f"R1Filter: 無法刪除臨時 Logo 檔案: {temp_logo_path}, error: {e}")

def _calculate_file_hash(self, file_path: Path) -> str:
    """計算檔案的 SHA256 哈希值"""
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            # Read and update hash in chunks to handle large files
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except FileNotFoundError:
        self.logger.warning(f"R1Filter: 計算哈希值時找不到檔案: {file_path}")
        return ""
    except Exception as e:
        self.logger.error(f"R1Filter: 計算檔案哈希值時出錯: {e}")
        return ""
