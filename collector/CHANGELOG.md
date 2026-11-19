# Changelog

## 0.1.1

- Browser fingerprint: the fetch browser now reports its own platform's user agent (no
  hard-coded macOS string, no "HeadlessChrome"), and runs as a windowed Chrome on a virtual
  display (Xvfb) instead of headless, which Duquesne's bot protection blocks less often.
- New options `browser_mode` (`auto`, `window`, `headless`) and `user_agent` (override).
- A blocked fetch now logs what the browser received (title, kind, start of the text).

## 0.1.0

- First release: daily Duquesne Light fetch (hourly and daily usage, bill PDFs with
  supplier and supply rate) through headless Chromium, served as a read-only JSON API
  (`/usage`, `/bills`, `/status`, `/health`) with an optional bearer token.
