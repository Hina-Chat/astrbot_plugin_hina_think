# R1 Filter Hina - AstrBot 思維鏈過濾插件

一個用於過濾和記錄 AI 思維鏈的 AstrBot 插件，支援持久化儲存和 Markdown 格式輸出。

該模組根據個人的需求客制化，因此未能遵循開發規範。

Powered by Claude 4s, All.

## 功能特色

- 🤔 可選擇是否顯示 AI 的思維過程
- 📝 支援 `/think` 指令查看上一次對話的思維鏈
- 💾 支援將思維記錄持久化儲存
- 📊 提供完整的思維鏈管理和統計功能
- 📘 支援 Markdown 格式匯出，便於閱讀

## 指令列表

- `/think` (別名: 思考、思維鏈) - 查看上次對話的思維過程
- `/think_clear` (別名: 清空思考、清理思維鏈) - 清空當前使用者的思維記錄
- `/think_status` (別名: 狀態) - 查看插件運行狀態
- `/think_export` (別名: 匯出思維、導出記錄) - 匯出思維記錄檔案
- `/think_stats` (別名: 儲存統計、空間統計) - 查看儲存空間使用統計

## 配置項目

```json
{
    "display_reasoning_text": true,    // 是否顯示思維過程
    "max_think_length": 600,          // /think 指令輸出的最大長度
    "enable_persistence": true,        // 是否啟用持久化儲存
    "save_as_markdown": true,         // 是否同時保存為 Markdown 格式
    "max_file_size_mb": 3,           // 單個檔案大小限制(MB)
    "records_per_file": 15,          // 每個檔案最大記錄數
    "max_records_per_user": 30,      // 每用戶最大保留檔案數
    "storage_dir": ""                // 自訂儲存目錄(可選)
}
```

## 儲存結構

```
r1_thoughts_data/
├── json/                 # JSON 格式原始數據
│   └── {user_id}/
│       └── YYYY-MM-DD.jsonl
├── markdown/             # Markdown 格式報告
│   └── {user_id}/
│       └── YYYY-MM-DD_Hina_Think.md
└── cache/               # 快取檔案
    └── last_reasoning.json
```

## 安裝方式

1. 將插件檔案複製到 AstrBot 的插件目錄
2. 重新載入 Bot 或重啟 AstrBot
3. 使用 `/think_status` 確認插件是否正常運行

## 注意事項

- 檔案會自動按日期和大小分割
- 超過限制的舊檔案會自動刪除
- 建議定期備份重要的思維記錄