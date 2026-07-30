# byte-brief

A daily email digest of the top 10 Hacker News stories — headline, score, comment
count, a short excerpt of the article, and clickable links to both the article and
its HN discussion.

Pure Python standard library. No `pip install`, no virtualenv, nothing to break
when Task Scheduler runs it unattended six months from now.

## How it works

1. Pulls the ranked front page from the [official Hacker News API](https://github.com/HackerNews/API)
   (`topstories.json` → `item/<id>.json`). No HTML scraping, so there are no
   brittle CSS selectors to rot.
2. Fetches each linked article and extracts a summary from its own metadata
   (`og:description`, `twitter:description`, `<meta name="description">`), falling
   back to the first substantial paragraph of body text. Ask HN / Show HN text
   posts use the body HN already stores.
3. Renders an HTML email (with a plain-text alternative) and sends it over SMTP.

Job listings and polls are filtered out, so you get 10 actual articles.

## Setup

### 1. Create a Google app password

Your regular Google password will not work — Gmail requires an app password for
SMTP.

1. Turn on 2-Step Verification: https://myaccount.google.com/signinoptions/two-step-verification
2. Go to https://myaccount.google.com/apppasswords
3. Create one named `byte-brief` and copy the 16-character code.

### 2. Add it to the config

Open `config.json` and paste the app password into `app_password` (spaces in the
code are fine to remove):

```json
{
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 465,
  "sender": "you@gmail.com",
  "app_password": "abcdefghijklmnop",
  "recipients": ["you@gmail.com"],
  "story_count": 10,
  "subject_prefix": "Byte Brief"
}
```

`config.json` is gitignored so the password never gets committed.

Every setting can also come from an environment variable named
`BYTE_BRIEF_` + the uppercased key — `BYTE_BRIEF_APP_PASSWORD`,
`BYTE_BRIEF_SENDER`, `BYTE_BRIEF_RECIPIENTS`, and so on. Env vars override
`config.json`, and if there's no `config.json` at all the environment supplies
everything. That's how the GitHub Actions run works.

### 3. Preview without sending

```bash
python hn_brief.py --dry-run
```

Prints the digest and writes `out/preview.html` for you to open in a browser.

### 4. Send one for real

```bash
python hn_brief.py
```

### 5. Schedule it daily

Two independent ways to do this — GitHub Actions runs in the cloud, Task Scheduler
runs on your PC. Pick one.

#### Option A: GitHub Actions (runs even when your PC is off)

[`.github/workflows/daily-brief.yml`](.github/workflows/daily-brief.yml) runs the
script on GitHub's servers at 12:00 UTC daily. Add three repository secrets at
**Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
| --- | --- |
| `BYTE_BRIEF_SENDER` | the Gmail address you send from |
| `BYTE_BRIEF_RECIPIENTS` | where to deliver it (comma-separate for several) |
| `BYTE_BRIEF_APP_PASSWORD` | your 16-character Google app password |

These live in secrets rather than the workflow file because this repo is public —
secrets are never printed in logs and are not exposed to forks or PRs from others.

Then trigger a test run: **Actions → Daily Byte Brief → Run workflow**. If a
secret is missing the job fails on the first step and tells you which one.

Three things to know about Actions cron:

- **No timezone support.** `0 12 * * *` is 8:00 AM Eastern in summer and 7:00 AM
  once EST returns in November. To keep it at 8:00 AM year-round, change the hour
  to `13` in the winter.
- **Runs can be late.** GitHub queues scheduled jobs; 5–30 minutes of drift is
  normal and not a failure.
- **60-day inactivity pause.** GitHub disables cron on repos with no commits for
  60 days. You'll get an email, and one click in the Actions tab re-enables it.

#### Option B: Windows Task Scheduler (local)

```bash
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Time 08:00
```

Registers a Windows scheduled task that runs as you, in the background, with no
console window. If your machine is asleep or off at that time, it runs at the
next opportunity rather than skipping the day.

Test the scheduled task immediately:

```bash
Start-ScheduledTask -TaskName ByteBrief-DailyHN
```

Remove it:

```bash
powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Remove
```

## Options

| Command | Effect |
| --- | --- |
| `python hn_brief.py` | Build and send the digest |
| `python hn_brief.py --dry-run` | Print it and write `out/preview.html`, send nothing |
| `python hn_brief.py --count 5` | Override the story count for one run |
| `python hn_brief.py --verbose` | Debug logging |

Change the permanent story count via `story_count` in `config.json`. Add more
addresses to `recipients` to send to several inboxes.

## Logs

Everything is logged to `logs/byte-brief.log`, capped at 512 KB with two
rotations, which matters since the scheduled run is silent.

```bash
Get-Content .\logs\byte-brief.log -Tail 20
```

## Troubleshooting

**`535 Username and Password not accepted`** — the app password is wrong, or you
used your normal Google password. Regenerate it at
https://myaccount.google.com/apppasswords

**A story says "No preview available"** — that publisher blocked the fetch (often
HTTP 403) or exposes no description metadata. The headline and links still work;
the log records the specific reason. This is expected for a couple of sites and
never blocks the email.

**An excerpt reads like a site tagline** rather than the article — some sites set
one site-wide `og:description` for every page. Nothing to be done from the
outside; the link is still correct.

**The scheduled task didn't run** — confirm it exists and check its last result:

```bash
Get-ScheduledTaskInfo -TaskName ByteBrief-DailyHN
```

**The GitHub Actions run failed** — open the run under the Actions tab; the log
names the failing step. On failure the job also uploads `logs/` as a downloadable
artifact. If cron has gone quiet for weeks, check whether GitHub paused it for
inactivity.
