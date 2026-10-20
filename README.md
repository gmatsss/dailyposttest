# PlacidWay → Daily Facebook Post (n8n)

An n8n workflow that writes one Facebook post a day from public PlacidWay content. Each run reads a page, has Claude draft the post, checks the draft against the source page and either publishes it or emails it to a reviewer for approval first, depending on the mode.

```
Schedule (daily 09:00) ─┐
Manual run ─────────────┴─> Config ─> Plan run ─> Read post log ─> Fetch sitemap ─> Select item
   ─> wait 2 s ─> Fetch page ─> Extract content
   ─> Claude: write post ─> Claude: check post ─> Guardrails (rules) ─┬─ failed, tries left ─> back to writer with reasons
                                                                       └─> Log run ─> Route by mode
        ├─ approval ─> email reviewer [Approve / Reject] ─> publish or log rejected
        └─ auto     ─> publish
   publish = photo post (image + text) or link post (no image) ─> Log published

Any external step fails after 3 retries ─> Handle failure ─> log "failed" ─> email alert ─> mark execution failed
On workflow error (scheduled runs) ─> catch-all log + alert
```

File: [`workflow/placidway-daily-facebook-post.json`](workflow/placidway-daily-facebook-post.json)

## Demo
- **Test Facebook Page:** [The more we go](https://www.facebook.com/profile.php?id=61595653467392). Three posts there were published by this workflow.
- **Sample output:** [`samples/`](samples/) has the 3 posts with their source URLs and Facebook post IDs.
- **Screen recording:** _link to be added_

## How it works

### 1. Extract: the sitemap, not scraping listing pages
PlacidWay publishes a sitemap index (`/sitemap.xml`) with a separate sitemap for each content type. Each entry has the URL, a `lastmod` date and usually the featured image. The RSS endpoint returns 403.

Why sitemaps:
- **Polite:** one request returns thousands of URLs. A run makes only 2 requests to PlacidWay (the sitemap, then the chosen page), with a 2 s wait between them, well under the 1 request/second limit.
- **Stable:** the sitemap is a public, machine-readable contract. HTML listing pages change with redesigns.
- **Fresh:** `lastmod` lets us take the newest content first.
- **No headless browser:** each page's Open Graph tags (`og:title`, `og:description`, `og:image`) and its `<main>` element hold everything we need in the raw HTML, so plain HTTP is enough.

robots.txt allows everything we use. The workflow also skips any URL under a disallowed path.

**Fields captured:** title, URL, summary, body text (up to 12,000 characters, passed to the LLM and used for fact-checking) and the featured image. The image is PlacidWay's own 1200×630 share image, which is already the size Facebook wants.

### 2. Select: one new URL per run, never repeated
- **Variety rule:** the content type rotates by weekday. Mon/Thu articles, Tue/Fri treatment packages, Wed/Sat patient Q&A, Sun news.
- The workflow reads the log table and skips every URL that already has a row. A URL that only *failed* stays eligible, so it gets retried.
- From the unused URLs, it takes the one with the newest `lastmod`.

### 3. Generate: Claude returns structure, not free text
Claude (`claude-opus-5-5`) returns JSON (`hook`, 2–4 `value_lines`, `cta`, 3–5 `hashtags`) using the API's structured-output mode. The workflow assembles the final text itself: hook / lines / CTA / link / hashtags. That way the link is always exactly the source URL and the layout never drifts. The prompt tells Claude to use only facts from the page, with no prices and no medical claims.

### 4. Guardrails: an LLM reviewer, then hard rules
1. **Second Claude pass (reviewer).** It compares the draft to the source page and lists every `medical_claim`, `outcome_promise`, `price`, `unsupported_fact` or `tone` problem, quoting each one. It then returns the *smallest* edit that removes them. It may only delete or soften wording, never add anything.
2. **Deterministic rules (final say).** These run on the reviewer's version:
   - text before the link is no more than 500 characters
   - 2–4 lines and 3–5 hashtags
   - no banned claim words: cure, guarantee, 100%, risk-free, painless, miracle, best, #1, proven, no side effects, "will heal/fix/reverse", and urgency phrases
   - **no prices at all:** currency symbols or codes next to numbers, plus "cheap" and "discount"
   - **every number in the post must appear in the source page**

   **Content problems never stop the run.** They are fixed in three layers:
   1. **Auto-fix:** drop any line that breaks a rule, keep at most 4 lines and 5 hashtags, top up to 3 hashtags with safe ones, and drop the last line(s) until the post fits 500 characters. This only removes text or swaps in fixed safe wording, so it can't introduce a claim.
   2. **Rewrite:** if it still fails, Claude gets the exact reasons (for example "text before link is 546 chars (max 500)") and rewrites. Each rewrite goes through both checks again. Up to `MAX_ATTEMPTS` (3) tries.
   3. **Safe fallback:** if every AI attempt fails, the workflow publishes a plain post built only from the page's own title and summary, run through the same rules.

   So a post always goes out. The run stops and alerts a human only for things a human must fix: the site is down, an API key is invalid, the Facebook token has expired, and so on.

Why both: the LLM catches meaning, such as an implied outcome or a fact that isn't on the page. The rules are cheap, predictable and can't be talked out of a decision. Prices are banned outright, not just "checked", because package prices change and a stale price in a post is a real risk. The post points people to the page instead.

### 5. Publish
`PUBLISH_MODE` in the **Config** node:

| Mode | What happens |
|---|---|
| `approval` | Emails the reviewer the exact post, image and safety notes with **Approve & publish / Reject** buttons (n8n's Send Email "send and wait"). Posts only on Approve. A rejection or no answer within 24 h is logged as `rejected` / `approval_expired`. |
| `auto` | Posts immediately. |

With an image, the workflow uses `POST /{page-id}/photos` (url + message). Without one, it uses `POST /{page-id}/feed` (message + link, so Facebook shows a link preview).

### 6. Logging and errors
Every run writes one row to the n8n Data Table **`pw_post_log`**, which also works as a simple dashboard in n8n:

`run_id, run_at, mode, content_type, source_url, title, image_url, post_text, guard_status, guard_notes, fb_post_id, status, error`

Status values: `awaiting_approval`, `publishing`, `published`, `rejected`, `approval_expired`, `failed`. `guard_status` is `pass`, `pass_after_fix` or `fallback_template`.

- Every call to PlacidWay, Claude, Facebook or the mail server **retries 3 times, 5 s apart**.
- If a call still fails, it goes to **Handle failure**. That node turns the error into a plain-English cause: site down, Claude key invalid, Claude rate-limited, Facebook token expired (error 190), layout changed, and so on. It marks the run `failed` in the log, emails the alert address and marks the n8n execution as failed. Nothing fails silently.
- **On workflow error** (the workflow is set as its own error workflow) catches anything else on scheduled runs. Failures that already sent an alert are skipped, so nobody gets two emails.

## Setup

1. **Import:** n8n → Workflows → Import from file → `workflow/placidway-daily-facebook-post.json`.
2. **Data table:** n8n → Overview → Data tables → Create, name it exactly `pw_post_log`, and add the 13 columns listed above (all type *string*). The nodes find it by name, so nothing else needs changing.
3. **Credentials** (n8n → Credentials → Add):
   - **Anthropic:** an API key from console.anthropic.com. Select it in *Claude: write post* and *Claude: check post*.
   - **SMTP** (n8n's built-in *Send Email* node, so any mail provider works): select it in *Ask reviewer* and the three *Alert* nodes. For Gmail use `smtp.gmail.com`, port 465, SSL on, your address and a Google **app password**.
   - **Facebook Graph API:** a **Page access token** for your own test Page with `pages_manage_posts` and `pages_read_engagement`. Select it in the two Facebook nodes.
4. **Settings:** edit the **Config** node.

### Settings (Config node)

n8n blocks `$env` in Code nodes by default, so settings live in one Config node instead of environment variables. Secrets never go there; they stay in n8n credentials.

| Setting | Default | Meaning |
|---|---|---|
| `PUBLISH_MODE` | `approval` | `approval` / `auto` |
| `REVIEWER_EMAIL` | — | receives approval requests |
| `ALERT_EMAIL` | — | receives failure and blocked alerts |
| `FROM_EMAIL` | `Website Posting Agent <…>` | sender name and address (must be the SMTP login's address) |
| `FB_PAGE_ID` | — | your test Page's numeric ID (required for approval/auto) |
| `FB_GRAPH_VERSION` | `v26.0` | Graph API version |
| `CLAUDE_MODEL` | `claude-opus-5-5` | model for both the writer and the reviewer |
| `RELAY_URL` | empty | optional fetch relay (see above) |
| `USER_AGENT` | `PW-autopost-demo/1.0 …` | identifies the bot to PlacidWay |
| `MAX_CHARS_BEFORE_LINK` | `500` | length limit enforced by the rules |
| `MAX_ATTEMPTS` | `3` | write + rewrites before a failing post is blocked |
| `APPROVAL_WAIT_HOURS` | `24` | how long an approval email stays valid |
| `ROTATION` | weekday map | content type per weekday |
| `FORCE_TYPE` | empty | `article` / `package` / `answer` / `news` overrides the rotation for testing |

### Fetch relay (only if PlacidWay blocks your n8n server)
PlacidWay is behind Cloudflare, which returned `403` to the data-centre IP of the n8n host used for this demo, even with polite browser-style headers. From another server the same requests return `200`. [`relay/pw_fetch_relay.py`](relay/pw_fetch_relay.py) is a ~80-line Python fetcher (standard library only) that runs on a server Cloudflare doesn't block:
- one allowed host only (`ALLOWED_HOST`), https only
- refuses robots.txt-disallowed paths
- at most 1 upstream request per second
- needs a shared key (`X-Relay-Key`)

To use it, set `RELAY_URL` in Config and give the two *Fetch* nodes a **Header Auth** credential (`X-Relay-Key: <key>`). Leave `RELAY_URL` empty to fetch directly, in which case any value works for the Header Auth credential.

## Running it
- **Test:** open the workflow, click **Execute workflow** (the *Manual run* trigger), then check the log table and the execution view.
- **Daily:** toggle the workflow **Active**. The schedule runs at 09:00 in the instance timezone.
- **Reset:** delete rows from `pw_post_log` to make URLs eligible again.

## Sample output
See [`samples/`](samples/): posts generated by this workflow from real PlacidWay pages, with their source URLs.

## Trade-offs and next steps
- **Bounded rewrite loop plus a fallback.** At most 3 AI attempts keeps cost per run bounded. The template fallback means a bad generation day still produces a safe post, even if it's a blander one.
- **Newest-first selection** favours fresh content. Older evergreen pages only come up once the newer ones have been used.
- **Body text capped at 12k characters** keeps cost and latency low. The checker sees the same text, so it can't wrongly reject a fact that the writer never saw.
- **Next:** read back reactions and clicks (`/{post-id}/insights`) into the log, cross-post to Instagram from the same image, brand the image with a logo overlay.
