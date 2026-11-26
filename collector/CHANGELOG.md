# Changelog

## 0.1.2

- Fixes the blocked fetches of 0.1.1: from 2026-11-23 every run got an Incapsula "Request
  unsuccessful" page ("OPowerToken not found: ... bot-challenge page"). `browser_mode: auto` is
  headless again and the default user agent is back to the macOS Chrome/131 string, the
  combination that worked on 2026-11-22 (0.1.0). Confirmed on one run on 2026-11-24 by setting
  those two options by hand. The windowed mode stays as `browser_mode: window`.

## 0.1.1

- Browser fingerprint: the fetch browser now reports its own platform's user agent (no
  hard-coded macOS string, no "HeadlessChrome"), and runs as a windowed Chrome on a virtual
  display (Xvfb) instead of headless, which Duquesne's bot protection blocks less often.
- New options `browser_mode` (`auto`, `window`, `headless`) and `user_agent` (override).
- A timed-out fetch now kills the whole browser process tree; one Xvfb is shared by all fetches in a run.
- A blocked fetch now logs what the browser received (title, kind, start of the text).

## 0.1.0

- First release: daily Duquesne Light fetch (hourly and daily usage, bill PDFs with
  supplier and supply rate) through headless Chromium, served as a read-only JSON API
  (`/usage`, `/bills`, `/status`, `/health`) with an optional bearer token.
