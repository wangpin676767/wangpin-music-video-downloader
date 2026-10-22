# Downloader conventions

- The main UI opens in the user's existing Brave session. Discovery may use a disposable headless Brave context; do not read existing profiles, cookies, or authorization credentials implicitly.
- Keep engine and browser discovery in owned subprocesses. Assign each process under the cancellation lock; cancel/shutdown must stop its entire process tree. Do not report terminal state until process cleanup has completed.
- Prefer source manifests to media fragments and obvious advertisement frames. Preserve original page title and stable identity across expiring CDN URLs. Never enable DRM bypass or promise all websites/qualities.
- Keep tokens, full media URLs with query credentials, and request headers out of displayed logs.
- Rebuild the windowed EXE with Playwright's driver included and verify changes through the packaged EXE API/UI and decoded media artifacts.
- Use VIDEO_DOWNLOADER_TEST_ROOT with an isolated EXE/tools root for integration/dynamic validation to preserve user settings/history. Independent resolver tests do not start the app.
