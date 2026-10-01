# kbar_download

Shioaji 歷史一分鐘 K 棒的一次性回補工具。Python 直接呼叫 SDK，保存完整回傳欄位到 CSV，固定一次回補的截止時間，補完後結束。額度不足會保存進度、等待重置並續傳；沒有每日新增行情排程，也沒有下單功能。

## 下載範圍

| 類別 | 發現方式與範圍 |
| --- | --- |
| 大台／小台／微台期貨 | 驗證 `TXFR1`／`MXFR1`／`TMFR1`；只用 R1，不取 R2 |
| 台積電股票 | 驗證 `2330` 股票合約 |
| 台積電個股期貨 | 依股票標的發現可用產品根代碼，再驗證各 R1；標準／小型等規格分開保存 |
| 台積電選擇權 | 列舉 SDK 選擇權 roots，取得每個 root 的明細，只接受 `underlying_code == "2330"`；全部可用到期日、履約價、買權與賣權，含列出的調整／小型規格，各合約分開保存 |

當前清單**不代表所有已到期契約**。查不到的合約、沒有可確認查詢起點的選擇權及空回應會列入報告，不會改抓台指選擇權，也不聲稱擁有選擇權全歷史。R1 是近月連續歷史，不是全部到期合約；不自行還原權息或調整轉倉價格。

## 環境與安裝

目標 **Ubuntu 20.04 是規劃假設，尚未實機驗證**，CPU 架構未知。核心使用 Python 3.8 以上的標準函式庫；Linux/macOS 使用 `fcntl`，Windows 使用 `msvcrt` 檔案鎖。Windows 10 x86-64 為新增相容目標，尚未實機驗證。SDK 固定 `shioaji==1.7.7`，依據目前 skill 與官方 Contract V2 文件，不使用舊 `api.Contracts`。

2026-10-01 核對 [PyPI 1.7.7](https://pypi.org/project/shioaji/1.7.7/)：套件標示 Python ≥3.7，提供 `cp37-abi3` 的 Linux x86-64（glibc ≥2.17）及 ARM64（glibc ≥2.28）wheel。這是發行檔規格，不是目標主機可執行證明；仍須核對架構、Python、libc、SDK 相依套件及二進位載入。[Ubuntu 官方公告](https://lists.ubuntu.com/archives/ubuntu-announce/2020-April/000256.html) 記載 20.04 預設 Python 3.8。本專案不要求替換系統 Python。

公開儲存庫可直接透過 HTTPS clone，不需要 GitHub 登入或 SSH key。在 Ubuntu 終端機執行：

```bash
git clone https://github.com/evan891119/kbar_download.git
cd kbar_download
```

主機需備有 `git`、`python3`、`python3-venv` 與 `python3-pip`。若尚未安裝，可執行 `sudo apt update` 後再執行 `sudo apt install git python3 python3-venv python3-pip`。

在專案根目錄建立獨立環境：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install '.[live]'
python -m kbar_download doctor
```

若只想跑離線核心，不安裝 SDK：

```bash
python -m pip install .
python -m unittest discover -s tests -v
```

原始碼也能直接 `python3 -m kbar_download doctor`，不需安裝。`doctor` 只列本機版本，**不匯入 SDK、不登入、不讀憑證**。SDK 二進位可另在目標主機以 `python -c 'import shioaji; print("SDK import OK")'` 檢查，仍不代表帳戶 API 可用。

## Windows 10（PowerShell）

先安裝 Git 與 64 位元 CPython（建議以 Python 3.11 作為首輪驗收版本）。目前固定的 Shioaji 1.7.7 在 [PyPI](https://pypi.org/project/shioaji/1.7.7/) 提供 `win_amd64` wheel；不代表已在你的 Win10 驗證，也不保證 32 位元或 ARM 原生執行。

```powershell
git clone https://github.com/evan891119/kbar_download.git
cd kbar_download
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install ".[live]"
.\.venv\Scripts\python.exe -m kbar_download doctor
Copy-Item config.example.json config.local.json
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m kbar_download status --config config.local.json
```

直接呼叫 venv 的 Python，不需啟用 PowerShell 腳本或修改 ExecutionPolicy。若使用其他相容 Python 版本，調整 `py` 的版本參數。JSON 輸出路徑可填 `C:/kbar-data`；建議先使用本機 NTFS 磁碟，避免同步／網路目錄。

實際下載時以隱藏輸入設定本次程序環境，以下命令會登入並消耗額度：

```powershell
try {
    $env:SJ_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'API Key' -AsSecureString)).Password
    $env:SJ_SEC_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'Secret Key' -AsSecureString)).Password
    .\.venv\Scripts\python.exe -m kbar_download download --config config.local.json
} finally {
    Remove-Item Env:SJ_API_KEY, Env:SJ_SEC_KEY -ErrorAction SilentlyContinue
}
```

不要把金鑰貼進命令或設定 JSON。續傳沿用相同命令與輸出目錄，可加 `--no-wait` 或 `--retry-unresolved`。若 CSV 被 Excel 等程式占用，先關閉再重跑；保留 journal 與進度檔，不手動刪除。

Windows 保留檔案 fsync、同目錄替換及 journal 恢復，但不執行 POSIX 目錄 fsync；因此不承諾與 POSIX 相同的突然斷電耐久性。平台分支模擬測試與本機跨程序測試不能替代 Win10 實機驗收。

## 用 .env 保存金鑰（Ubuntu／Windows 共用）

把 `.env.example` 複製成設定 JSON 同目錄的 `.env`，在本機文字編輯器填入：

```dotenv
SJ_API_KEY=你的APIKey
SJ_SEC_KEY=你的SecretKey
```

Ubuntu 首次建立（已有 `.env` 請直接編輯，不要覆蓋）：

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

PowerShell 使用 `Copy-Item .env.example .env`，再用 `notepad .env` 編輯。不要把真實內容貼到聊天或提交 Git。

存好後，不必每次輸入金鑰：

```bash
python -m kbar_download download --config config.local.json
```

Windows 把 `python` 換成 `.\.venv\Scripts\python.exe`。預設只讀設定 JSON 同目錄的 `.env`，不向父目錄搜尋；也可用 `--env-file /path/to/credentials.env` 指定路徑。既有環境變數優先（包含空值），如需改用檔案請先移除舊變數。缺少預設 `.env` 時仍可沿用環境變數。

格式僅支援這兩個欄位、空行、整行 `#` 註解，以及值外圍配對的單／雙引號；不支援 `export`、行尾註解、多行值或變數展開。`$`、`#`、反斜線等值內容保持原樣，不執行任何指令。`.env` 已被 Git 忽略，只有空白 `.env.example` 可提交；自訂檔名／路徑須自行確保不納入 Git。

只有需要登入的 `download` 才讀取憑證；`doctor`、`status` 和已完成且不要求重試的執行都不讀取。憑證不寫入進度或報告。以下互動輸入方式仍可使用。

## 設定與執行

複製 `config.example.json` 為 `config.local.json`，後者已忽略於 Git。`output_dir` 相對於設定檔所在目錄解析，而不是依目前 shell 位置變動。

```bash
cp config.example.json config.local.json
python -m kbar_download status --config config.local.json
```

`status` 不登入，會驗證既有資料校驗值、恢復未完成落盤交易並重建報告。沒有進度時顯示「尚無進度」。

真正執行前，於目標主機私下設定環境變數 `SJ_API_KEY`、`SJ_SEC_KEY`；`download` 會自動讀取設定 JSON 同目錄的 `.env`，不需要 CA 或交易權限設定。不要將憑證放在命令列參數、設定 JSON、README 或 Git。範例在 Bash 以不回顯方式輸入，不把實際金鑰寫進 shell 歷史：

```bash
read -rsp 'API Key: ' SJ_API_KEY; echo
read -rsp 'Secret Key: ' SJ_SEC_KEY; echo
export SJ_API_KEY SJ_SEC_KEY
python -m kbar_download download --config config.local.json
unset SJ_API_KEY SJ_SEC_KEY
```

**只有 `download` 會以 `simulation=True` 登入並使用行情額度。** 首次在目標主機先做小範圍驗收，不要直接啟動多年下載。可用 `start_overrides` 為六個類別設定較近日期（必須是已過去日期），使用獨立輸出目錄；這種執行在報告中標明不涵蓋全歷史。類別鍵為 `txf`、`mxf`、`tmf`、`tsmc_stock`、`tsmc_future`、`tsmc_option`。

正常續傳重跑相同命令、相同設定與輸出目錄即可。已完成的區段不重新查；已結束且未要求重試的執行不登入。可用 Ctrl-C 停止，下次先恢復未完成檔案交易。

```bash
# 額度不足時結束，之後自行重跑續傳
python -m kbar_download download --config config.local.json --no-wait

# 重試商品發現、空回應或失敗區段，仍沿用原截止時間，會消耗流量
python -m kbar_download download --config config.local.json --retry-unresolved
```

`--retry-unresolved` 也重新發現商品，只加入原截止時間內的可用契約，不刪除既有契約。一般重跑不反覆查未解決空區段，避免浪費額度。商品發現受限也會寫入報告。若合約已過期而無法再由 SDK 取得，程式會保留舊資料並報受限。

退出碼：`0` 代表本次已排定查詢完成（**不等於市場全歷史完整**）；`2` 代表受限、未解決或錯誤，查看報告；`3` 是 `--no-wait` 額度暫停；`130` 是使用者中止。

## 主要設定

| 設定 | 預設／意義 |
| --- | --- |
| `output_dir` | `data`，可配置到資料磁碟 |
| `cutoff` | `null`：首次啟動時取台灣時間前一完整分鐘，之後永遠沿用儲存值；可填含時區的 ISO 時間，不能在未來 |
| `chunk_days` | 7；各合約第一段先取 1 日，單次最多 30 個日曆日 |
| `reserve_bytes`、`reserve_ratio` | 50 MiB 與額度的 10%，取較大者保留 |
| `initial_daily_bytes`、`safety_factor` | 每日初估 1 MiB、乘 2；再依 API 使用量差額提高估計 |
| `request_interval_seconds` | 1 秒，循序歷史查詢 |
| `quota_poll_seconds` | 1800 秒；08:00 後尚未確認額度恢復時的檢查間隔 |
| `retry_attempts`、`retry_backoff_seconds` | 可辨識的連線／逾時最多 3 次，5 秒起指數退避；未知 SDK 錯誤停止，不盲目重試 |
| `timeout_ms` | 30000 |
| `start_overrides` | 預設空；可刻意縮短查詢範圍，不能在同一回補中變更 |
| `confirmed_empty` | 預設空；有可靠無資料證據後才使用，見下例 |

無資料證據格式（只適用 API 空回應，不能跳過下載或掩蓋回應錯誤）：

```json
{
  "contract_id": "STK_TSE_2330",
  "start": "2026-01-01",
  "end": "2026-01-01",
  "evidence": "請填入已核對的交易所休市日來源及說明，不要直接使用此示例作證據"
}
```

未確認的空回應不能因為是週末就判成休市，期貨夜盤尤其不能如此推論。證據區間必須涵蓋整個查詢區段；不要把只證明一天的休市資料套用到七天。

## 額度與固定截止

使用 `api.usage()` 真實 `bytes`、`limit_bytes`、`remaining_bytes`，不使用 CSV 大小估算帳戶剩餘流量。每段查詢前／後觀測，保留緩衝及下一段預估流量；不足時先縮為一日，再等待。預估不能保證上游回應不超額，共用帳戶的其他程式也可能改變用量。

官方重置時間為**每個交易日早上 08:00**。工具排定下一個平日 08:00 作為候選檢查時間，假日不會假定真的重置；只有 API 顯示額度改善且足夠才繼續。沒有內建假日曆，假日只會低頻查額度。`--no-wait` 則保存等待狀態後結束。額度查詢失敗時停止，不把缺值當充足。

截止時間在第一次建立進度時固定，跨日續傳不會每天加新資料。當日尾段仍可能尚未發布／修訂，報告會明示此限制；若需要可驗證的已完成歷史，設定截止至已結束的交易日期並盤後執行。本工具不以即時輪詢追逐最新行情。

## 資料與恢復保證

```text
data/
  <category>/<security_type>_<exchange>_<code>/<YYYY-MM>.csv
  progress.json
  metadata.json
  report.json
  report.md
```

- 保存所有來源欄位，不只 OHLCV；原始 `ts` 以整數奈秒保留，不先轉浮點。月檔以 SDK 的台灣牆鐘時間分組，不加 8 小時、不把日曆日誤作交易日。超出請求日曆邊界的資料會報錯，供實機驗證日期語義，不靜默丟棄夜盤。
- CSV 非空儲存格以 JSON 值編碼：一般數字可直接看，字串／巢狀欄位保留型別；`null` 與空字串有別，空白格表示該版本缺欄。用 `csv.DictReader` 後對非空儲存格 `json.loads` 即可精確讀回。不要用 Excel 的浮點匯入結果驗證奈秒精度。
- 新欄位會併入標頭；原始欄位不丟棄。資料依 `ts` 排序，同鍵同值去重；同鍵異值保留舊檔並報衝突，不靜默修改歷史。缺分鐘不補造 K 棒，也不以每日固定筆數判斷完整。
- 所有月檔先寫暫存、fsync，建立 journal 後逐檔原子替換，再保存進度；崩潰可向前完成同一交易。每次重啟驗證已登記 CSV 的 SHA-256。被手動修改／刪除則停止，請從備份還原相同資料與進度，或另開空目錄重抓；不要手改校驗值。
- 同一目錄有排他鎖，不能讓兩個程序同時下載。請使用支援原子 rename、fsync 與 advisory lock 的本機檔案系統；網路磁碟語義尚未驗證。
- `.gitignore` 排除預設資料、CSV、進度、報告、暫存、環境及憑證。自訂輸出路徑若在其他儲存庫內，仍須設定該儲存庫的忽略規則。

## 測試與驗證界線

執行 `python3 -m unittest discover -s tests -v`。離線假資料涵蓋欄位精度、跨月分檔、續傳去重、固定截止、空回應、流量耗盡與 08:00 後 API 恢復、分段縮小、有限重試、校驗失敗、排他鎖、跨月 rename 中斷、資料落盤後進度寫入失敗，以及台積電選擇權／R1 篩選。

2026-09-28 本機驗證：25 項離線測試通過；Python 3.8 語法解析通過（不是 Python 3.8 runtime 實測）；隔離虛擬環境套件安裝成功；macOS 本機可離線匯入實際 Shioaji 1.7.6，合約方法簽章與 Rust enum 序列化核對通過，未建立 SDK client／登入。

此處未登入使用者帳戶、未下載真實行情；Ubuntu 20.04 主機、實際架構、帳戶權限、實際契約清單、夜盤日期邊界與最早可得資料仍需目標主機/API 驗證。非空 API 回應只能證明保存了回傳資料，不能證明市場歷史每分鐘皆完整。

## 查證來源

- [Shioaji 歷史行情](https://sinotrade.github.io/zh/tutor/market_data/historical/)：股票候選起點 2020-03-02、期貨 2020-03-22；單次不超過 30 天；到期契約／連續期貨限制。個別商品受上市日及實際資料限制。
- [Shioaji 使用限制](https://sinotrade.github.io/tutor/limit/)：基本額度 500 MB、usage 欄位、交易日 08:00 重置與盤後快取建議。
- [Contract V2](https://sinotrade.github.io/tutor/contract/)：`get`、`info`、`futures_by_underlying`、`option_roots`、`options`。
- [期交所商品代碼](https://www.taifex.com.tw/cht/4/contractName)、[股票選擇權結算資料](https://www.taifex.com.tw/cht/5/sSOFSP)：台積電產品、標的 2330 與調整契約；實際下載清單仍由 SDK 查證。
- [期交所 2024 年 8 月雙月刊](https://www.taifex.com.tw/file/taifex/CHINESE/10/moth_all/202408_all.pdf)：微型臺指期貨 2024-07-29 上市，作為微台 R1 候選查詢下限，並非保證 API 自該日有資料。

### 登入失敗診斷

`login_failed` 現在附上登入耗時與固定分類線索，例如 `線索=timeout`、
`connection`、`clock`、`rate_limit`、`authentication` 或 `unknown`。
分類依例外類型或訊息關鍵字判斷，只供排查，不代表已確認根因；
`unknown` 不表示憑證一定錯誤，也不表示 SDK 沒有錯誤訊息。
診斷同時提供允許清單中的 SDK 例外類型（如 `TokenError`、`ResponseRecvError`），
或 Python 基底例外類型（如 `TypeError`）；未知自訂類別名稱不會輸出。
新增類型線索包括伺服器維護、回應通道關閉、資料解碼、參數與權限錯誤。工具不輸出原始例外、API key、secret 或帳戶資訊，
也不自動重試登入。此保護只涵蓋工具產生的診斷，SDK 自身的日誌不在此範圍。
若需回報問題，提供工具的 `停止：login_failed（…）` 一行即可，不要附 `.env`。

### 從 Shioaji 1.7.6 升級

2026-10-01 將依賴與執行時版本檢查同步升至 `1.7.7`。
[官方 PyPI](https://pypi.org/project/shioaji/1.7.7/) 已撤回 `1.7.6`，
原因涉及快取登入與訂閱狀態；這不代表已確認本次 HTTP 400 的根因。
在已啟用的虛擬環境、更新專案後執行：

```bash
python -m pip install --upgrade '.[live]'
python -m kbar_download doctor
python -m kbar_download download --config config.local.json --env-file .env
```

`doctor` 的 installed 與 expected 應皆為 `1.7.7`。
升級不需要更改 `.env` 或刪除既有下載進度；實際登入結果仍須目標主機驗證。


### 行情下載的登入模式

工具固定使用 `simulation=True`，不要求 token 具備 production 登入權限。
[官方模擬環境文件](https://sinotrade.github.io/tutor/simulation/) 列出 `kbars`、`ticks`、`snapshots` 可用。
工具只查詢行情，不下單；商品可用性、歷史涵蓋與額度仍以 API 實際回傳為準。
不需修改 `.env`，也不需刪除既有進度。
