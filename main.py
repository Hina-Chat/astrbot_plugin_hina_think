import re
import os
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from openai.types.chat.chat_completion import ChatCompletion

@register("r1-filter", "Soulter", "可選擇是否過濾推理模型的思考內容，支援/think指令查看思維鏈，並可持久化保存", "1.2.1", 'https://github.com/Soulter/astrbot_plugin_r1_filter')
class R1Filter(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.display_reasoning_text = self.config.get('display_reasoning_text', True)
        self.max_think_length = self.config.get('max_think_length', 600)
        
        # 初始化日誌記錄器
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        
        # 持久化相關配置
        self.enable_persistence = self.config.get('enable_persistence', True)  # 是否啟用持久化
        self.save_as_markdown = self.config.get('save_as_markdown', True)      # 是否保存為Markdown
        self.max_records_per_user = self.config.get('max_records_per_user', 50)   # 每用戶最大記錄數
        self.max_file_size_mb = self.config.get('max_file_size_mb', 5)             # 單個檔案最大大小(MB)
        self.records_per_file = self.config.get('records_per_file', 20)           # 每個檔案最大記錄數
        
        # 修复存储目录配置逻辑
        storage_dir_config = self.config.get('storage_dir', '')
        if storage_dir_config and storage_dir_config.strip():
            self.storage_dir = Path(storage_dir_config)
        else:
            # 預設儲存在插件目錄下
            default_storage = Path(__file__).parent / 'r1_thoughts_data'
            self.storage_dir = default_storage
        
        # 記憶體中的思維鏈快取
        self.last_reasoning: Dict[str, str] = {}
        
        # 初始化儲存目錄
        self._init_storage()
        
        # 載入已有的思維鏈記錄
        if self.enable_persistence:
            asyncio.create_task(self._load_cached_reasoning())
    
    def _init_storage(self):
        """初始化儲存目錄結構"""
        if not self.enable_persistence:
            return
            
        try:
            # 創建主目錄
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            
            # 創建子目錄
            (self.storage_dir / 'json').mkdir(exist_ok=True)      # JSON格式原始數據
            (self.storage_dir / 'markdown').mkdir(exist_ok=True)  # Markdown格式報告
            (self.storage_dir / 'cache').mkdir(exist_ok=True)     # 快取檔案
            
            self.logger.info(f"R1Filter: 儲存目錄初始化完成: {self.storage_dir}")
        except Exception as e:
            self.logger.error(f"R1Filter: 儲存目錄初始化失敗: {e}")
            self.enable_persistence = False
    
    async def _load_cached_reasoning(self):
        """載入快取的思維鏈記錄"""
        if not self.enable_persistence:
            return
            
        cache_file = self.storage_dir / 'cache' / 'last_reasoning.json'
        try:
            if cache_file.exists():
                with open(cache_file, 'r', encoding='utf-8') as f:
                    self.last_reasoning = json.load(f)
                self.logger.info(f"R1Filter: 載入了 {len(self.last_reasoning)} 條快取記錄")
        except Exception as e:
            self.logger.error(f"R1Filter: 載入快取失敗: {e}")
    
    async def _save_reasoning_cache(self):
        """保存思維鏈快取到檔案"""
        if not self.enable_persistence:
            return
            
        cache_file = self.storage_dir / 'cache' / 'last_reasoning.json'
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.last_reasoning, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"R1Filter: 保存快取失敗: {e}")
    
    async def _save_reasoning_record(self, user_key: str, reasoning: str, response_text: str, event: AstrMessageEvent):
        """保存完整的思維鏈記錄到檔案"""
        if not self.enable_persistence or not reasoning:
            return
            
        try:
            timestamp = datetime.now()
            
            # 準備記錄數據
            record = {
                'timestamp': timestamp.isoformat(),
                'user_key': user_key,
                'user_id': str(event.unified_msg_origin),
                'reasoning': reasoning,
                'response': response_text,
                'message_content': getattr(event, 'message_str', ''),
                'platform': getattr(event, 'platform', 'unknown')
            }
            
            # 保存JSON格式
            await self._save_json_record(record, timestamp)
            
            # 保存Markdown格式
            if self.save_as_markdown:
                await self._save_markdown_record(record, timestamp)
                
        except Exception as e:
            self.logger.error(f"R1Filter: 保存思維記錄失敗: {e}")
    
    async def _save_json_record(self, record: dict, timestamp: datetime):
        """保存 JSON 格式記錄"""
        user_dir = self.storage_dir / 'json' / record['user_id']
        user_dir.mkdir(parents=True, exist_ok=True)
        
        # 按日期分組檔案
        date_str = timestamp.strftime('%Y-%m-%d')
        json_file = user_dir / f'{date_str}.jsonl'
        
        # 追加模式寫入JSONL格式
        with open(json_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        
        # 清理舊記錄，保持數量限制
        await self._cleanup_old_records(user_dir, 'json')
    
    async def _save_markdown_record(self, record: dict, timestamp: datetime):
        """保存 Markdown 記錄 - 按日期和檔案大小分割"""
        user_dir = self.storage_dir / 'markdown' / record['user_id']
        user_dir.mkdir(parents=True, exist_ok=True)
        
        # 按日期分組，但控制檔案大小
        date_str = timestamp.strftime('%Y-%m-%d')
        
        # 尋找合適的檔案
        file_index = 1
        while True:
            if file_index == 1:
                md_file = user_dir / f'{date_str}_Hina_Think.md'
            else:
                md_file = user_dir / f'{date_str}_Hina_Think_{file_index}.md'
            
            # 檢查檔案是否存在且大小是否超過限制
            if md_file.exists():
                file_size_mb = md_file.stat().st_size / (1024 * 1024)
                record_count = self._count_records_in_md_file(md_file)
                
                if file_size_mb >= self.max_file_size_mb or record_count >= self.records_per_file:
                    file_index += 1
                    continue
            
            break
        
        # 生成Markdown內容
        md_content = self._generate_markdown_content(record, file_index)
        
        # 追加模式寫入
        with open(md_file, 'a', encoding='utf-8') as f:
            if not md_file.exists() or md_file.stat().st_size == 0:
                f.write(f"# {record['user_id']} 的思維鏈記錄 - {date_str} (第{file_index}部分)\n\n")
            f.write(md_content)
    
    def _count_records_in_md_file(self, md_file: Path) -> int:
        """計算Markdown檔案中的記錄數量"""
        try:
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read()
                # 計算思維記錄標題的數量
                return content.count('## 思維記錄')
        except:
            return 0
    
    def _generate_markdown_content(self, record: dict, file_index: int = 1) -> str:
        """生成 Markdown 格式的記錄內容"""
        timestamp = datetime.fromisoformat(record['timestamp'])
        
        # 清理和格式化思維內容
        reasoning = self._format_reasoning_for_markdown(record['reasoning'])
        response = record['response'].strip()
        user_message = record['message_content'].strip()
        
        # 計算思維鏈長度統計
        reasoning_chars = len(reasoning)
        reasoning_lines = len(reasoning.split('\n'))
        
        md_content = f"""## 思維記錄 - {timestamp.strftime('%m月%d日 %H:%M:%S')}

> **統計資訊**: 思維鏈 {reasoning_chars} 字元，{reasoning_lines} 行

### 使用者
```
{user_message}
```

### 鍵山雛
<details>
<summary>💭 點擊展開思維鏈 ({reasoning_chars} 字元)</summary>

{reasoning}

</details>

### 💬 最終回覆
{response}

---

"""
        return md_content
    
    def _format_reasoning_for_markdown(self, reasoning: str) -> str:
        """格式化思維內容為 Markdown 友好格式"""
        # 移除多餘的空行
        lines = [line.rstrip() for line in reasoning.split('\n')]
        
        # 處理程式碼塊
        formatted_lines = []
        in_code_block = False
        
        for line in lines:
            # 檢測程式碼相關的行
            if any(keyword in line.lower() for keyword in ['```', 'code:', 'function', 'def ', 'class ', 'import ']):
                if not in_code_block and ('```' not in line):
                    formatted_lines.append('```')
                    in_code_block = True
                formatted_lines.append(line)
                if '```' in line and in_code_block:
                    in_code_block = False
            else:
                if in_code_block and line.strip() and not any(c.isalnum() for c in line):
                    formatted_lines.append('```')
                    in_code_block = False
                formatted_lines.append(line)
        
        if in_code_block:
            formatted_lines.append('```')
        
        return '\n'.join(formatted_lines)
    
    async def _cleanup_old_records(self, user_dir: Path, format_type: str):
        """清理舊記錄，保持數量限制"""
        try:
            if format_type == 'json':
                files = list(user_dir.glob('*.jsonl'))
            else:
                files = list(user_dir.glob('*.md'))
            
            if len(files) > self.max_records_per_user:
                # 按修改時間排序，刪除最舊的檔案
                files.sort(key=lambda x: x.stat().st_mtime)
                for old_file in files[:-self.max_records_per_user]:
                    try:
                        old_file.unlink()
                        self.logger.info(f"R1Filter: 已清理舊檔案 {old_file.name}")
                    except Exception as e:
                        self.logger.error(f"R1Filter: 清理檔案失敗 {old_file}: {e}")
        except Exception as e:
            self.logger.error(f"R1Filter: 清理舊記錄失敗: {e}")
    
    async def _get_user_conversation_key(self, event: AstrMessageEvent) -> str:
        """生成使用者+會話的唯一標識鍵"""
        uid = event.unified_msg_origin
        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(uid)
            return f"{uid}_{curr_cid}"
        except:
            return str(uid)
    
    def _extract_reasoning_content(self, response: LLMResponse) -> str:
        """從 LLM 響應中提取推理內容"""
        if not (response and response.raw_completion and isinstance(response.raw_completion, ChatCompletion)):
            return ""
        
        if not (len(response.raw_completion.choices) and response.raw_completion.choices[0].message):
            return ""
        
        message = response.raw_completion.choices[0].message
        reasoning_content = ""
        
        if hasattr(message, 'reasoning') and message.reasoning:
            reasoning_content = message.reasoning
        elif hasattr(message, 'reasoning_content') and message.reasoning_content:
            reasoning_content = message.reasoning_content
        
        return reasoning_content
    
    def _extract_reasoning_from_text(self, text: str) -> str:
        """從文字中提取被 <think> 標籤包圍的推理內容"""
        if not text:
            return ""
        
        think_pattern = r'<think>(.*?)</think>'
        matches = re.findall(think_pattern, text, flags=re.DOTALL)
        
        if matches:
            return '\n\n'.join(match.strip() for match in matches)
        
        return ""
    
    @filter.on_llm_response()
    async def resp(self, event: AstrMessageEvent, response: LLMResponse):
        user_key = await self._get_user_conversation_key(event)
        reasoning_content = ""
        final_response_text = ""
        
        if self.display_reasoning_text:
            if response and response.raw_completion and isinstance(response.raw_completion, ChatCompletion):
                if len(response.raw_completion.choices) and response.raw_completion.choices[0].message:
                    message = response.raw_completion.choices[0].message

                    if hasattr(message, 'reasoning') and message.reasoning:
                        reasoning_content = message.reasoning
                    elif hasattr(message, 'reasoning_content') and message.reasoning_content:
                        reasoning_content = message.reasoning_content

                    if reasoning_content:
                        self.last_reasoning[user_key] = reasoning_content
                        final_response_text = message.content if message.content else ""
                        response.completion_text = f"思考：{reasoning_content}\n\n{final_response_text}"
                    else:
                        final_response_text = message.content if message.content else response.completion_text
                        response.completion_text = final_response_text
        else: 
            completion_text = response.completion_text if response.completion_text else ""
            
            reasoning_from_raw = self._extract_reasoning_content(response)
            if reasoning_from_raw:
                reasoning_content = reasoning_from_raw
                self.last_reasoning[user_key] = reasoning_content
            else:
                reasoning_from_text = self._extract_reasoning_from_text(completion_text)
                if reasoning_from_text:
                    reasoning_content = reasoning_from_text
                    self.last_reasoning[user_key] = reasoning_content
            
            if '<think>' in completion_text or '</think>' in completion_text:
                completion_text = re.sub(r'<think>.*?</think>', '', completion_text, flags=re.DOTALL).strip()
                completion_text = completion_text.replace('<think>', '').replace('</think>', '').strip()
            
            final_response_text = completion_text
            response.completion_text = completion_text
        
        # 異步保存記錄
        if reasoning_content:
            asyncio.create_task(self._save_reasoning_record(
                user_key, reasoning_content, final_response_text, event))
        
        # 異步保存快取
        if self.last_reasoning:
            asyncio.create_task(self._save_reasoning_cache())
    
    @filter.command("think", alias={'思考', '思維鏈'})
    async def think_command(self, event: AstrMessageEvent):
        """顯示上次對話的思維鏈"""
        user_key = await self._get_user_conversation_key(event)
        
        if user_key in self.last_reasoning and self.last_reasoning[user_key]:
            reasoning = self.last_reasoning[user_key]
            if len(reasoning) > self.max_think_length:
                reasoning = reasoning[:self.max_think_length] + "\n\n...(暫且如此……)"
            
            yield event.plain_result(f"秘神流雛的内心世界：：\n\n{reasoning}")
        else:
            yield event.plain_result("過度的思考或許是毒藥呢……")
    
    @filter.command("think_clear", alias={'清空思考', '清理思維鏈'})
    async def think_clear_command(self, event: AstrMessageEvent):
        """清空當前使用者的思維鏈記錄"""
        user_key = await self._get_user_conversation_key(event)
        
        if user_key in self.last_reasoning:
            del self.last_reasoning[user_key]
            # 異步保存快取
            asyncio.create_task(self._save_reasoning_cache())
            yield event.plain_result("過度的思考或許是毒藥呢……")
        else:
            yield event.plain_result("災厄心自持，我思故我在……")
    
    @filter.command("think_status", alias={'狀態'})
    async def think_status_command(self, event: AstrMessageEvent):
        """顯示插件狀態資訊"""
        display_status = "ON" if self.display_reasoning_text else "OFF"
        persistence_status = "ON" if self.enable_persistence else "OFF"
        markdown_status = "ON" if self.save_as_markdown else "OFF"
        total_records = len(self.last_reasoning)
        user_key = await self._get_user_conversation_key(event)
        has_record = "存" if user_key in self.last_reasoning else "滅"
        
        status_info = f"""📊 R1思維鏈插件狀態：
• 思考內容顯示: {display_status}
• 持久化儲存: {persistence_status}
• Markdown保存: {markdown_status}
• 記憶體記錄數: {total_records}
• 您的思考記錄: {has_record}
• 儲存目錄: {self.storage_dir}
• 檔案大小限制: {self.max_file_size_mb}MB
• 每檔案記錄數: {self.records_per_file}條

指令:
• /think - 查看上次思考過程
• /think_clear - 清空思考記錄
• /think_status - 查看插件狀態
• /think_export - 匯出思維記錄
• /think_stats - 查看儲存統計"""
        
        yield event.plain_result(status_info)
    
    @filter.command("think_export", alias={'匯出思維', '導出記錄'})
    async def think_export_command(self, event: AstrMessageEvent):
        """匯出當前使用者的思維記錄檔案路徑"""
        if not self.enable_persistence:
            yield event.plain_result("若是沒有日記本，那怎麽記日記呢（笑）")
            return
        
        user_id = str(event.unified_msg_origin)
        json_dir = self.storage_dir / 'json' / user_id
        md_dir = self.storage_dir / 'markdown' / user_id
        
        json_files = list(json_dir.glob('*.jsonl')) if json_dir.exists() else []
        md_files = list(md_dir.glob('*.md')) if md_dir.exists() else []
        
        if not json_files and not md_files:
            yield event.plain_result("厄運尚未蒐集，又談何净化呢？")
            return
        
        export_info = f"""這裡是厄運的源頭，請務必小心……\n

JSON：
{json_dir}
- 厄運點: {len(json_files)}

Markdown：
{md_dir}  
- 厄運點: {len(md_files)}

翻閲厄運之書，以曉厄運之本。"""
        
        yield event.plain_result(export_info)
    
    @filter.command("think_stats", alias={'儲存統計', '空間統計'})
    async def think_stats_command(self, event: AstrMessageEvent):
        """查看儲存統計資訊"""
        if not self.enable_persistence:
            yield event.plain_result("若是沒有日記本，那怎麽記日記呢（笑）")
            return
        
        user_id = str(event.unified_msg_origin)
        user_json_dir = self.storage_dir / 'json' / user_id
        user_md_dir = self.storage_dir / 'markdown' / user_id
        
        # 統計檔案數量和大小
        json_files = list(user_json_dir.glob('*.jsonl')) if user_json_dir.exists() else []
        md_files = list(user_md_dir.glob('*.md')) if user_md_dir.exists() else []
        
        json_size = sum(f.stat().st_size for f in json_files) / 1024  # KB
        md_size = sum(f.stat().st_size for f in md_files) / 1024     # KB
        
        # 統計記錄數量
        total_records = 0
        for json_file in json_files:
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    total_records += len(f.readlines())
            except:
                pass
        
        stats_info = f"""厄運藏書舘：

厄運檔案：
• JSON: {len(json_files)} 個 ({json_size:.1f} KB)
• Markdown: {len(md_files)} 個 ({md_size:.1f} KB)
• Total: {total_records} 條

厄運歸宿：
• JSON: {user_json_dir}
• Markdown: {user_md_dir}

厄運制限：
• 單檔大小: {self.max_file_size_mb} MB
• 單檔條數: {self.records_per_file} 條
• 檔案個數: {self.max_records_per_user} 個"""
        
        yield event.plain_result(stats_info)