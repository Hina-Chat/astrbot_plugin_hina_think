# AstrBot 插件開發知識庫

## 1. 簡介

本知識庫旨在為 AI 提供開發 AstrBot 插件所需的核心概念、架構理解和關鍵 API 參考。AstrBot 是一個基於事件驅動的機器人框架，支持多平台消息處理和強大的插件擴展能力。

## 2. 整體架構概覽

AstrBot 的核心由以下幾個主要組件構成：

-   **核心 (Core)**: 包含啟動、生命週期管理、消息管道、事件總線等基礎功能。
-   **API**: 為插件開發者提供統一的接口和工具，方便與 AstrBot 內部組件交互。
-   **平台 (Platform)**: 負責接入不同的消息平台（如 QQ、Telegram），將平台消息轉換為統一的 `AstrBotMessage` 對象，並將其提交到事件總線。
-   **供應商 (Provider)**: 管理大語言模型 (LLM) 供應商，提供 LLM 請求接口。
-   **插件 (Star)**: AstrBot 的擴展單元，通過繼承 `Star` 基類並註冊來實現特定功能。
-   **消息管道 (PipelineScheduler)**: 採用「洋蔥模型」處理消息事件，支持前置和後置處理。
-   **事件總線 (EventBus)**: 負責事件的分發和處理，將平台事件傳遞給消息管道。

## 3. 插件開發要點

### 3.1 插件類與註冊

-   所有插件必須繼承自 `astrbot.api.star.Star` 基類。
-   使用 `@astrbot.api.star.register` 裝飾器註冊插件，提供插件的元數據（名稱、作者、描述、版本等）。
    ```python
    from astrbot.api.star import Context, Star, register

    @register("my_plugin", "Your Name", "我的第一個 AstrBot 插件", "1.0.0")
    class MyPlugin(Star):
        def __init__(self, context: Context):
            super().__init__(context)
            # 插件初始化邏輯
    ```

### 3.2 上下文對象 (`Context`)

-   `Context` 對象在插件初始化時傳入，提供了訪問 AstrBot 核心組件的接口。
-   通過 `self.context` 訪問，例如：`self.context.get_config()`、`self.context.send_message()`。

### 3.3 事件處理

-   **事件對象**: `astrbot.api.event.AstrMessageEvent` 是所有消息事件的載體，包含了消息內容、發送者信息、平台元數據等。
-   **過濾器 (Filter)**: 使用 `astrbot.api.event.filter` 模塊提供的裝飾器來註冊事件處理函數 (Handler)。
    -   `@filter.command("指令名")`: 註冊指令處理器。
    -   `@filter.command_group("指令組名")`: 註冊指令組。
    -   `@filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)`: 根據消息類型過濾（私聊、群聊、所有）。
    -   `@filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)`: 根據平台類型過濾。
    -   `@filter.permission_type(filter.PermissionType.ADMIN)`: 限制管理員權限。
    -   **事件鉤子**:
        -   `@filter.on_astrbot_loaded()`: AstrBot 初始化完成時觸發。
        -   `@filter.on_llm_request()`: 收到 LLM 請求前觸發。
        -   `@filter.on_llm_response()`: LLM 請求完成後觸發。
        -   `@filter.on_decorating_result()`: 發送消息給平台前觸發，用於消息裝飾。
        -   `@filter.after_message_sent()`: 發送消息給平台後觸發。
-   **Handler 函數**:
    -   必須是異步函數 (`async def`)。
    -   第一個參數為 `self`，第二個參數為 `event: AstrMessageEvent`。
    -   可以使用 `yield` 返回 `MessageEventResult` 來發送消息。
    -   `event.stop_event()`: 停止事件傳播，阻止後續處理。

### 3.4 消息類型與結構

-   `astrbot.api.platform.MessageType`: 枚舉類型，表示消息是群聊 (`GROUP_MESSAGE`) 還是私聊 (`FRIEND_MESSAGE`)。
-   `astrbot.api.platform.AstrBotMessage`: 統一的消息對象，包含 `type`、`self_id`、`session_id`、`message_id`、`group_id`、`sender`、`message` (消息鏈)、`message_str` (純文本消息)、`raw_message` (原始平台消息)、`timestamp`。
-   `astrbot.api.event.MessageChain`: 消息鏈，一個有序的 `BaseMessageComponent` 列表。
-   `astrbot.api.message_components` (或 `astrbot.core.message.components`): 消息段組件，如 `Plain` (文本)、`Image` (圖片)、`At` (@消息)、`Record` (語音)、`Video` (視頻) 等。

### 3.5 發送消息

-   **通過 `yield` 返回**:
    -   `yield event.plain_result("文本消息")`: 發送純文本。
    -   `yield event.image_result("圖片 URL 或路徑")`: 發送圖片。
    -   `yield event.chain_result([Comp.Plain("文本"), Comp.Image.fromURL("URL")])`: 發送消息鏈。
-   **主動發送 (`context.send_message`)**:
    -   用於定時任務或非事件觸發的消息發送。
    -   `await self.context.send_message(event.unified_msg_origin, MessageChain().message("Hello!"))`
    -   `event.unified_msg_origin` 是會話的唯一標識符。

### 3.6 會話控制

-   使用 `astrbot.api.util.session_waiter` 模塊。
-   `@session_waiter(timeout=秒數)` 裝飾器用於註冊會話控制器。
-   `SessionController` 對象用於控制會話的生命週期（`keep()`、`stop()`）和獲取歷史消息。

### 3.7 LLM 交互

-   **獲取 LLM 供應商**: `self.context.get_using_provider()`。
-   **直接調用 LLM**:
    ```python
    llm_response = await self.context.get_using_provider().text_chat(
        prompt="你的問題",
        contexts=[], # 歷史對話上下文
        image_urls=[], # 圖片 URL 列表
        func_tool=self.context.get_llm_tool_manager(), # 函數工具管理器
        system_prompt=""
    )
    ```
-   **通過事件請求 LLM**:
    ```python
    yield event.request_llm(
        prompt="你的問題",
        session_id=curr_cid, # 對話 ID
        contexts=context, # 歷史對話上下文
        image_urls=[],
        conversation=conversation # 對話對象
    )
    ```

### 3.8 LLM 函數工具

-   使用 `@astrbot.api.llm_tool` 裝飾器註冊函數工具。
-   函數工具必須包含符合特定格式的 Docstring，用於描述函數功能和參數。
    ```python
    @llm_tool(name="get_weather")
    async def get_weather(self, event: AstrMessageEvent, location: str) -> MessageEventResult:
        '''獲取天氣信息。
        Args:
            location(string): 地點
        '''
        # 實現邏輯
    ```

### 3.9 插件配置

-   在插件目錄下創建 `_conf_schema.json` 文件，定義插件的配置 Schema。
-   Schema 支持 `string`, `text`, `int`, `float`, `bool`, `object`, `list` 等類型，以及 `description`, `hint`, `default` 等屬性。
-   配置會自動載入並傳入插件 `__init__` 方法的 `config: AstrBotConfig` 參數。
-   `self.config` 是一個類似字典的對象，支持 `self.config.save_config()`。

### 3.10 文字渲染與 HTML 渲染

-   `await self.text_to_image("文本內容")`: 將文本渲染為圖片。
-   `await self.html_render("HTML Jinja2 模板", {"data": "值"})`: 使用 HTML 和 Jinja2 模板渲染圖片。

### 3.11 插件生命週期 (`terminate`)

-   `async def terminate(self)`: 可選實現，在插件禁用、重載或 AstrBot 關閉時觸發，用於釋放資源、回滾修改等。

## 4. 關鍵類與 API 參考

### 4.1 `astrbot.api.platform.AstrMessageEvent`

-   **屬性**:
    -   `message_str`: 消息純文本。
    -   `message_obj`: `AstrBotMessage` 對象。
    -   `platform_meta`: `PlatformMetadata` 對象。
    -   `session_id`: 不包含平台的會話 ID。
    -   `unified_msg_origin`: 統一的會話 ID (`platform_name:message_type:session_id`)。
    -   `is_wake`: 機器人是否被喚醒。
    -   `call_llm`: 是否禁止默認的 LLM 請求。
-   **方法**:
    -   `get_message_str()`: 獲取消息純文本。
    -   `get_message_outline()`: 獲取消息概要（包含佔位符）。
    -   `get_messages()`: 獲取消息鏈列表。
    -   `get_message_type()`: 獲取消息類型 (`MessageType`)。
    -   `is_private_chat()`: 是否為私聊。
    -   `is_admin()`: 是否為管理員。
    -   `get_platform_name()`: 獲取平台名稱。
    -   `get_self_id()`: 獲取機器人自身 ID。
    -   `get_sender_id()`: 獲取發送者 ID。
    -   `get_sender_name()`: 獲取發送者昵稱。
    -   `get_group_id()`: 獲取群組 ID。
    -   `stop_event()`: 停止事件傳播。
    -   `request_llm(...)`: 創建 LLM 請求。
    -   `plain_result(...)`, `image_result(...)`, `chain_result(...)`: 創建 `MessageEventResult`。
    -   `send(message: MessageChain)`: 發送消息到當前會話。

### 4.2 `astrbot.api.platform.AstrBotMessage`

-   **屬性**:
    -   `type`: `MessageType`。
    -   `self_id`: 機器人自身 ID。
    -   `session_id`: 會話 ID。
    -   `message_id`: 消息 ID。
    -   `group_id`: 群組 ID。
    -   `sender`: `MessageMember` 對象。
    -   `message`: 消息鏈 (`List[BaseMessageComponent]`)。
    -   `message_str`: 消息純文本。
    -   `raw_message`: 原始平台消息對象。
    -   `timestamp`: 消息時間戳。

### 4.3 `astrbot.api.star.Context`

-   **屬性**:
    -   `provider_manager`: 供應商管理器。
    -   `platform_manager`: 平台管理器。
-   **方法**:
    -   `get_registered_star(star_name: str)`: 獲取插件元數據。
    -   `get_all_stars()`: 獲取所有插件元數據列表。
    -   `get_llm_tool_manager()`: 獲取函數工具管理器。
    -   `get_config()`: 獲取 AstrBot 配置。
    -   `get_db()`: 獲取數據庫對象。
    -   `get_event_queue()`: 獲取事件隊列。
    -   `get_platform(platform_type)`: 獲取指定平台適配器。
    -   `send_message(session: Union[str, MessageSesion], message_chain: MessageChain)`: 主動發送消息。
    -   `get_all_providers()`, `get_all_tts_providers()`, `get_all_stt_providers()`: 獲取所有供應商。
    -   `get_using_provider()`, `get_using_tts_provider()`, `get_using_stt_provider()`: 獲取當前使用的供應商。

### 4.4 `astrbot.api.star.Star`

-   **屬性**:
    -   `context`: `Context` 對象。
-   **方法**:
    -   `text_to_image(text: str, return_url=True)`: 將文本渲染為圖片。
    -   `html_render(tmpl: str, data: dict, return_url=True)`: 使用 HTML 模板渲染圖片。
    -   `terminate()`: 插件生命週期終止方法。

### 4.5 `astrbot.api.event.MessageChain` 與 `astrbot.core.message.components`

-   `MessageChain`: 消息鏈對象，用於構建複雜消息。
-   `BaseMessageComponent`: 消息段基類。
-   **常用消息段**: `Plain`, `Image`, `At`, `Record`, `Video`, `Reply`, `Forward`, `Node`, `Nodes`, `Poke`, `File`。

### 4.6 `astrbot.api.util.SessionController`

-   **方法**:
    -   `keep(timeout: float, reset_timeout: bool)`: 保持會話。
    -   `stop()`: 停止會話。
    -   `get_history_chains()`: 獲取歷史消息鏈。

## 5. 開發原則

-   **功能測試**: 確保插件功能經過充分測試。
-   **良好註釋**: 為代碼添加清晰的註釋。
-   **數據持久化**: 將持久化數據存儲在 `data` 目錄下，避免更新/重裝時被覆蓋。
-   **錯誤處理**: 實現健壯的錯誤處理機制，防止插件崩潰。
-   **代碼格式化**: 提交前使用 `ruff` 等工具格式化代碼。
-   **異步網絡請求**: 使用 `aiohttp`、`httpx` 等異步庫進行網絡請求。
-   **貢獻優先**: 如果是對現有插件的功能擴展，優先提交 PR 給原插件。

希望這份知識庫能幫助 AI 更好地理解和開發 AstrBot 插件。
