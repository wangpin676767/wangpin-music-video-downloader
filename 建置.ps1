$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    if (Get-Command uv -ErrorAction SilentlyContinue) { uv venv --python 3.12 .venv }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { py -3.12 -m venv .venv }
    else { throw '請先安裝 Python 3.12 或 uv。' }
    if ($LASTEXITCODE -ne 0) { throw '建立 Python 環境失敗。' }
}
$appPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Get-Command uv -ErrorAction SilentlyContinue) { uv pip install --python $appPython -r requirements-build.txt }
else { & $appPython -m pip install -r requirements-build.txt }
if ($LASTEXITCODE -ne 0) { throw '安裝建置依賴失敗。' }
& $appPython provision.py
if ($LASTEXITCODE -ne 0) { throw '下載官方工具失敗。' }
$appHtml = Join-Path $PSScriptRoot 'index.html'
& $appPython -m PyInstaller --noconfirm --clean --onefile --windowed --name '王品音樂影片下載器' --distpath '.' --workpath '.build' --specpath '.build' --collect-all playwright --add-data "$appHtml;." app.py
if ($LASTEXITCODE -ne 0) { throw '建置失敗。' }
Write-Host '完成：雙擊「王品音樂影片下載器.exe」即可使用。'
