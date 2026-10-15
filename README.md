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

## Photos (article photo instead of a card)

`images.article_images` in `config.yaml` (needs `images.cards_only: false`): every post gets the **lead photo of the source article** (og:image / twitter:image / schema.org image, then the feed thumbnail). The photo is checked (not a logo/placeholder, at least 800x450, normal shape, not used recently), cropped only if it is outside 4:5 .. 1.91:1, shrunk to 1600 px, gets a small brand badge, and the post ends with `📷 الصورة: <publisher>`.

- `skip_agencies: true` skips photos whose page credits Getty / Reuters / AP / AFP / PA / Shutterstock / Alamy and similar agencies. Most big outlets use them, so many stories will fall back to the headline card. `false` removes the filter (and keeps the legal risk: article photos belong to the publisher or agency, a credit line is not a licence).
- `fallback`: `card` (headline card), `commons` (the older stock-photo search) or `skip` (don't publish the story).
- Test: Run workflow -> **check_article_images** shows, for the top stories, which photo each would get (saved in the `preview-images` artifact) and how many would fall back.
- Back to cards only: `cards_only: true`.

## Post look

- **Text posts:** headline line first (starts with one emoji), blank line, then 2-4 short paragraphs opened by symbols (🔹 📌 💬 📊 ⏰ 🔙). Statements of coaches/players are pushed up in the ranking (`priority.statement_boost`) and quoted between « ».
- **Headline cards:** two sizes picked at random, never the same size three times in a row (`images.card_sizes`): 4:5 big (1080x1350) and the classic landscape (1200x630), 6 designs, large text; statement cards get a big quote mark.
- **"أهم مباريات اليوم" cards:** 1080x1350 (4:5), date on its own pill, up to 3 cards in one post; matches of the clubs in `fixtures.highlight` are tinted gold.

## Match data sources (daily "today's matches" post)

Several sources, tried in the order of `fixtures.sources` in `config.yaml`; for each league the first source that returns matches wins, so one blocked source never stops the post:

| source | key | notes |
|---|---|---|
| `espn` | none | unofficial; sometimes answers 403 to GitHub's servers |
| `sofascore` | none | unofficial; covers every league incl. Egypt, Saudi and CAF |
| `footballdata` | secret `FOOTBALL_DATA_KEY` (free at football-data.org) | top-5 leagues + Champions League + World Cup; skipped when no key |
| `thesportsdb` | built-in free test key (optional secret `THESPORTSDB_KEY`) | many leagues; smaller free-tier coverage |

Run **check_fixtures** to see, per league, which source served it and how many requests of each source worked or failed.

## Testing the daily matches post

Actions → post-football-news → Run workflow → tick **post_fixtures**:
- with **dry_run** ticked (default): builds today's matches cards and uploads them as the `preview-images` artifact, nothing is published;
- with **dry_run** unticked: really publishes the post to the page right now. It does not count as the day's morning post, so the scheduled 10:00 post still goes out normally.

## Tuning

Everything lives in `config.yaml`: sources and weights, daily cap, posts per run, quiet hours, models, writing styles.
`state/state.json` (committed by the bot) stores seen stories so nothing is posted twice.
Check the current Gemini model names and free-tier limits in AI Studio; if a model is renamed, change `gemini_model` only.
