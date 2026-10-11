# Football News Bot

RSS + Google News → Gemini (free tier) writes Arabic posts in mixed styles → Facebook Page (Graph API).
Runs hourly on GitHub Actions, up to 3 posts per run, 60 per Cairo day, quiet hours 01:00–07:00 Cairo.

## Setup

1. **Gemini key (free):** https://aistudio.google.com/apikey → create key.
2. **Facebook:**
   - developers.facebook.com → Create App (Business type) → add the Facebook Login product.
   - Graph API Explorer → select your app → permissions `pages_show_list`, `pages_manage_posts`, `pages_read_engagement` → generate a user token.
   - Exchange it for a long-lived user token (`/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN`).
   - Call `/me/accounts` with the long-lived token: the `access_token` returned for your page is a Page token that does not expire. Note the page `id` as well.
   - While the app is in Development mode and you are admin of the page, no App Review is needed for your own page.
3. **GitHub:** push this folder as a repo → Settings → Secrets and variables → Actions → add:
   `GEMINI_API_KEY`, `FB_PAGE_ID`, `FB_PAGE_TOKEN`.
   (Public repo = unlimited Actions minutes. Private = 2000 min/month; this uses roughly 1 min × ~18 runs/day.)

## First run (in this order)

1. Actions → post-football-news → Run workflow → tick **check_feeds** → see which feeds work. Remove or fix dead ones in `config.yaml`.
2. Run workflow again with **dry_run** ticked → the log shows the posts it would publish. Tune the styles/prompt.
3. Run with dry_run unticked for one real post, check it on the page. After that the hourly schedule takes over.

## Tuning

Everything lives in `config.yaml`: sources and weights, daily cap, posts per run, quiet hours, models, writing styles.
`state/state.json` (committed by the bot) stores seen stories so nothing is posted twice.
Check the current Gemini model names and free-tier limits in AI Studio; if a model is renamed, change `gemini_model` only.
