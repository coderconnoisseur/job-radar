# Job Radar

Checks the careers feeds (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Recruiterflow,
MyNextHire, plus Amazon, Microsoft and Atlassian)
of ~110 companies every 10 minutes and pings your phone on Telegram when a new SWE intern / entry-level job opens in
India, or remote-from-anywhere. No dependencies beyond Python 3.11+.

## 1. Make a Telegram bot (2 min)

1. In Telegram, message **@BotFather** → `/newbot` → pick a name. It gives you a **bot token**.
2. Open your new bot and send it any message (e.g. "hi").
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser. Find `"chat":{"id":123456789`.
   That number is your **chat ID**.

Keep the token private. Anyone with it can control your bot.

## 2. Try it locally

```bash
python radar.py --list
```
Shows every job that matches your filters right now. Then test the alert (PowerShell):

```powershell
$env:TELEGRAM_BOT_TOKEN="<token>"; $env:TELEGRAM_CHAT_ID="<chat id>"; python radar.py --test-alert
```

## 3. Run it 24/7 for free on GitHub Actions

1. Create a new GitHub repo and push this folder to it.
   **Public repo** = unlimited free Actions minutes. A private repo gets 2,000 min/month,
   which isn't enough for every 15 min, so change the cron to `*/30` if you go private.
2. Repo → Settings → Secrets and variables → Actions → add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Actions tab → enable workflows → "Job Radar" → **Run workflow** once.
   The first run sends a "started" message and saves what's already open. After that you only hear about new jobs.

## 4. Make it run on time with cron-job.org

GitHub's built-in scheduler often runs hours late, so the workflow's own schedule is only an
hourly backup. cron-job.org (free) triggers it every 10 minutes instead:

1. GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token.
   Repository access: only `job-radar`. Permissions → Actions: **Read and write**. Nothing else.
2. cron-job.org → Create cronjob:
   - URL: `https://api.github.com/repos/coderconnoisseur/job-radar/actions/workflows/radar.yml/dispatches`
   - Schedule: every 10 minutes
   - Advanced → Request method `POST`, body `{"ref":"main"}`, headers:
     `Authorization: Bearer <your token>`, `Accept: application/vnd.github+json`,
     `Content-Type: application/json`
3. Save and "Test run". It should return HTTP 204, and a run appears in the Actions tab.

The token expires on the date you chose; make a new one and update the header when it does.

## Tuning

Everything lives in `config.toml`:

- **Add a company:** open one of its job links. The URL shows the hiring system and the
  company's name in it. Add the line, then verify with `python radar.py --check <slug>`.
- **Too noisy / too quiet:** edit the `role` / `exclude` regexes. E.g. drop `\bios\b`
  if you don't want iOS roles, or add `sde[- ]?(2|ii)` to `exclude` if you only want new-grad roles.

## When an alert hits

1. Open the job and paste the JD into Claude to get your resume bullets and a short note tailored to it.
2. Tap "Find referrers", then message 2–3 engineers there within the hour.
3. Apply on the portal. Being early + a referral beats being early alone.
