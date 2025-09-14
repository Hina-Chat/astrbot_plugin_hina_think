# Hina Think

Powered by Claude 4s & Gemini 2.5 Pro, **All**.

一個基於 [R1-filter](https://github.com/Soulter/astrbot_plugin_r1_filter) 二次開發的 AstrBot 模組，專注於捕獲並匯出 AI 的思維鏈。

現在支援的模型：
- DeepSeek R1
- Gemini 2.5 Pro（[Gemini Patch](https://github.com/Hina-Chat/astrbot_plugin_gemini_patcher)）

## 功能亮點

- 在背景靜默運行，自動捕獲對話並保存至 SQLite（可用 `/memohina` 導出為 JSON）。
- 使用 `/memohina` 指令，可將 Session 記錄增量上載至 R2，並生成公開訪問的 QR Code。
- 使用 `/think` 指令，可立即檢視模型『本次』的思考過程，便於快速調試人格提示詞。
- QR CODE 支援有限的樣式自訂，包括碼點形狀、圖片蒙版和中心 Logo。
- 結合了指令冷卻與增量檢查，能合理處理用戶的連續請求，杜絕任何不必要的性能開銷。

## 指令列表

- `/think`
  - **功能**: 輸出上一次對話的思維鏈
  - **冷卻**: 預設冷卻時間為 30 秒

- `/memohina`
  - **功能**: 將所處會話的記錄增量上載至 R2，並生成公開訪問的 QR Code。
  - **冷卻**: 預設冷卻時間為 600 秒 (10 分鐘)。

## 配置詳解

### 1. 通用設定 (`general`)

| 參數 (Parameter)               | 類型 (Type) | 說明 (Description)                                 | 預設值 (Default) |
| ------------------------------ | ----------- | -------------------------------------------------- | ---------------- |
| `think_cooldown_seconds`       | `int`       | `/think` 指令的冷卻時間（秒）。                    | `30`             |
| `memohina_cooldown_seconds`    | `int`       | `/memohina` 指令的冷卻時間（秒）。                 | `600`            |
| `memohina_export_record_count` | `int`       | 使用 `/memohina` 指令單次匯出的最大記錄輪次。      | `100`            |
| `max_think_length`             | `int`       | `/think` 指令輸出的思維鏈的最大字符，超出部分會被截斷。 | `800`            |

### 2. 持久化與 R2 存儲設定 (`persistence` & `r2`)

| 參數 (Parameter)                    | 類型 (Type) | 說明 (Description)                                                     | 預設值 (Default)     |
| ----------------------------------- | ----------- | ---------------------------------------------------------------------- | -------------------- |
| `enable_persistence`                | `bool`      | 是否啟用對話記錄的持久化存儲（SQLite）。資料庫位於 AstrBot 為插件分配的專屬資料目錄下。 | `true`               |
| `upload_cache_size`                 | `int`       | R2 上載歷史記錄的快取大小（條）。                                      | `1000`               |
| `r2_account_id`                     | `string`    | Cloudflare 的 Account ID。                                             | `N/A`                |
| `r2_access_key_id`                  | `string`    | R2 的 Access Key ID。                                                  | `N/A`                |
| `r2_secret_access_key`              | `string`    | R2 的 Secret Access Key。                                              | `N/A`                |
| `r2_bucket_name`                    | `string`    | 用於存儲導出檔案的 R2 桶名稱。                                         | `N/A`                |
| `r2_custom_domain`                  | `string`    | R2 桶綁定的公共訪問域名 (可選)。                                       | `N/A`                |

### 3. QR Code 樣式 (`qrcode`)

| 參數 (Parameter)         | 類型 (Type) | 說明 (Description)                                           | 預設值 (Default) |
| ------------------------ | ----------- | ------------------------------------------------------------ | ---------------- |
| `qr_box_size`            | `int`       | QR 碼每個碼點的像素大小。                                    | `5`              |
| `qr_border`              | `int`       | QR 碼四周空白邊框的寬度。                                    | `2`              |
| `qr_module_drawer`       | `string`    | 碼點的形狀。可選值: `square`, `gapped`, `circle`, `rounded`。  | `square`         |
| `qr_image_mask_path`     | `string`    | 圖片蒙版的路徑，支援 **本地檔案** 或 **網路 URL** (可選)。    | `N/A`            |
| `qr_logo_path`           | `string`    | 中心 Logo 的路徑，支援 **本地檔案** 或 **網路 URL** (可選)。    | `N/A`            |

> [!NOTE]
> 當使用圖片蒙版時，背景會被自動設為透明。您可以同時使用圖片蒙版和中心 Logo，創造出獨一無二的視覺效果。

## 📦 依賴與許可

本插件使用了以下第三方庫：

| 庫         | 授權       | 倉庫地址                                           |
|------------|------------|----------------------------------------------------|
| boto3      | Apache 2.0 | https://github.com/boto/boto3                      |
| qrcode[pil]| BSD        | https://github.com/lincolnloop/python-qrcode       |
| Pillow     | HPND       | https://github.com/python-pillow/Pillow            |
