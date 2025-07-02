# Hina Think Plugin for AstrBot

Powered by Claude 4s & Gemini 2.5 Pro, **All**.

一個 AstrBot 模組，專注於捕獲並匯出 AI 的思維鏈。

本插件基於 [R1-filter](https://github.com/Soulter/astrbot_plugin_r1_filter) 二次開發，提供從開發調試到成果展示的全鏈路解決方案。

## ✨ v2.0.0 核心架構升級

v2.0.0 版本引入了全新的 **增量快照 (Incremental Snapshot)** 雲端歸檔機制，取代了舊有的覆蓋式存儲模型。這是一次核心架構的重大升級，旨在提供更高效、更安全、更具追溯性的數據管理體驗。

- **效率革命**: 每次匯出不再是上傳完整的對話歷史，而是 **僅上傳自上次匯出以來的新增記錄**。這極大地節約了開銷。
- **數據保險箱**: 每次匯出都會在雲端創建一個帶時間戳的獨立 JSON 檔案，形成一個不可變的、版本化的歷史檔案庫。告別因意外覆蓋導致的風險。
- **精準回溯**: 清晰的版本歷史讓您可以輕鬆審計和研究任何一個特定時間點的對話記錄。

## 🚀 功能亮點

- **🧠 全自動思維鏈捕獲**: 在背景靜默運行，自動捕獲並將包含思維鏈（Reasoning）的對話保存為結構化的本地日誌。
- **💾 增量式雲端歸檔**: 使用 `/memohina` 命令，可將 **新增的** 會話記錄一鍵增量上傳至 Cloudflare R2，並生成公開訪問鏈接及個性化 QR CODE。
- **⚡️ 即時偵錯與回溯**: 使用 `/think` 命令，可立即在聊天窗口中查看模型的 “上一次” 思考過程，便於快速診斷和調試。
- **🎨 高度個性化QR CODE**: 生成分享連結時，附帶的 QR CODE 支援豐富的自定義選項，包括碼點形狀、圖片蒙版和中心 Logo，讓每一次分享都與眾不同。
- **🛡️ 雙重防護機制**: 結合了命令冷卻與增量檢查，能智能處理用戶的連續請求，杜絕任何不必要的資源浪費。

## 📋 命令列表

- `/think`
  - **功能**: 顯示上一次對話的思維鏈。如果內容過長，會自動截斷並將完整內容保存為本地 `.json` 檔案。
  - **冷卻**: 默認冷卻時間為 30 秒。

- `/memohina`
  - **功能**: **增量匯出**。將自上次匯出以來所有**新增的**對話記錄上傳至 R2，並返回一個指向本次增量快照的分享鏈接和個性化 QR CODE。單次匯出的記錄上限可在配置中設定。
  - **冷卻**: 默認冷卻時間為 600 秒 (10 分鐘)。

## ⚙️ 配置詳解

所有配置項均位於插件配置檔案的 `Hina Think` 分區下。

### 1. 通用設定 (`general`)

- `think_cooldown_seconds` (int, 默認: `30`): `/think` 命令的冷卻時間（秒）。
- `memohina_cooldown_seconds` (int, 默認: `600`): `/memohina` 命令的冷卻時間（秒）。
- `memohina_export_record_count` (int, 默認: `100`): 使用 `/memohina` 命令單次匯出的最大記錄數量。
- `max_think_length` (int, 默認: `800`): `/think` 命令在聊天窗口中顯示思維鏈的最大字符數，超出部分會被截斷。

### 2. 持久化設定 (`persistence`)

- `enable_persistence` (bool, 默認: `true`): 是否啟用對話記錄的持久化存儲。
- `storage_dir` (string, 默認: `hina_thoughts_data`): 存儲日誌和臨時檔案的本地目錄名，位於插件數據資料夾內。
- `log_rotation_count` (int, 默認: `20`): 每個用戶會話保留的輪轉日誌檔案數量。
- `save_interval_seconds` (int, 默認: `60`): 自動保存非活躍用戶數據的檢查間隔（秒）。
- `user_inactivity_timeout_seconds` (int, 默認: `300`): 判斷用戶為“非活躍”狀態的超時時間（秒）。
- `upload_cache_size` (int, 默認: `1000`): R2 上傳歷史記錄的緩存大小（條）。

### 3. R2 存儲設定 (`r2`)

- `r2_account_id` (string): Cloudflare R2 的 Account ID。
- `r2_access_key_id` (string): R2 的 Access Key ID。
- `r2_secret_access_key` (string): R2 的 Secret Access Key。
- `r2_bucket_name` (string): 用於存儲日誌的 R2 存儲桶名稱。
- `r2_custom_domain` (string, 可選): R2 存儲桶綁定的公共訪問域名。

### 4. QR Code 樣式 (`qrcode`)

- `qr_box_size` (int, 默認: `5`): QR 碼每個模塊（碼點）的像素大小。
- `qr_border` (int, 默認: `2`): QR 碼四周空白邊框的寬度。
- `qr_module_drawer` (string, 默認: `square`): 碼點的形狀。可選值: `square`, `gapped`, `circle`, `rounded`。
- `qr_image_mask_path` (string, 可選): 圖片蒙版的路徑，支援**本地檔案**或**網絡 URL**。
- `qr_logo_path` (string, 可選): 中心 Logo 的路徑，支援**本地檔案**或**網絡 URL**。

> [!NOTE]
> 當使用圖片蒙版時，背景會被自動設為透明。您可以同時使用圖片蒙版和中心 Logo，創造出獨一無二的視覺效果。

## 📦 依賴與許可

本插件使用了以下第三方庫：

| 庫         | 授權       | 倉庫地址                                           |
|------------|------------|----------------------------------------------------|
| boto3      | Apache 2.0 | https://github.com/boto/boto3                      |
| qrcode[pil]| BSD        | https://github.com/lincolnloop/python-qrcode       |
| Pillow     | HPND       | https://github.com/python-pillow/Pillow            |
