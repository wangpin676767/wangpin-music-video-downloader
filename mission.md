# 下載影片

需求：在此資料夾提供可雙擊啟動的小 app，輸入網頁或影片網址後下載播放的影片。

實作：Windows 執行檔 + 本機網頁介面（僅 127.0.0.1），yt-dlp 解析影片、FFmpeg 合併串流。介面繁體中文，主介面沿用既有 Brave 設定檔。2026-10-06 擴充：一般解析失敗時，以暫時的無介面 Brave 工作階段偵測公開播放器串流，不讀取使用者設定檔、cookies 或登入資訊。下載預設至本目錄的「影片」，支援更改輸出位置、最佳/1080p/720p/480p、進度、取消、重試、歷史及開啟資料夾。

可行限制：「所有類型網站」以擴大網站相容性為方向，無法保證所有網址；支援 yt-dlp 支援的網站、HTML video/嵌入、JavaScript/iframe 播放器可觀測的 MP4/WebM/HLS/DASH。DRM、登入/付費牆、CAPTCHA、特殊播放器或網站政策仍可能阻擋；不自動讀取使用者瀏覽器 cookies。下載單支影片而不是整份播放清單，直播不自動錄製。解析僅使用網站當下公開提供的畫質。

環境：Windows，Python 3.12 開發 venv，PyInstaller 打包，FFmpeg 隨附，JS runtime 優先隨附 Deno。已確認官方擴充列出 Brave 並可連線。

驗證：Brave 官方擴充操作真正 UI；本機 HTTP fixture 產生合法 MP4/HLS 及慢速串流，以檔案雜湊/ffmpeg 解碼驗證下載，測試錯誤、取消、重試、設定/歷史持久化、安全邊界與封裝後啟動。外部公開範例實際下載一次。結果記錄 evidence/validation.md。

此環境沒有 submit_plan/advance_project 工具，使用本機檔案及可呼叫的協作工具完成同樣的驗收與獨立審閱，不把不可用的 runtime 當作已執行。

## 能力與驗收對應
- 雙擊 EXE、Brave 既有設定檔、本機介面 → VAL-001
- HTTP(S) 網址、畫質、路徑、直接影片/HTML/HLS 下載、成品可播放 → VAL-002
- 進度/合併狀態/取消/重試/防止同時啟動 → VAL-003
- 空白/非法網址、無影片、網路/權限/工具錯誤 → VAL-004
- 輸出設定及成功歷史、重新啟動、開啟資料夾 → VAL-005
- localhost/Host/Origin/CSRF、無 shell URL 插值、HTML 防注入 → VAL-006
- 已知限制、來源說明、安裝/建置/使用說明 → VAL-007
- xgcartoon 指定網址、JavaScript/iframe 動態播放器、無副檔名 MIME 串流、Referer/UA、主清單與片段選擇、錯誤與取消 → VAL-008

## 2026-10-06 調查與驗證策略
yt-dlp 2026.08.19 對使用者指定 xgcartoon 網址回報 Unsupported URL；直接 HTTP 能讀網頁，Brave 可正常載入 iframe 並取得 HLS playlist。因此修補通用播放器偵測層，不硬寫單一集數 CDN。保留原有品質、歷史、取消及本機 API 邊界。驗證使用重打包 EXE 的真實 API、瀏覽器主介面，以及下載產物雜湊/音訊視訊解碼；對指定網址下載整集並檢查長度與解碼。沒有 runtime 調度工具，協作調查與獨立審閱使用可用 agent 工具。
