# Morning brief automation

This repository includes a scheduled GitHub Actions workflow that sends a daily morning brief covering:

- today's Google Calendar events;
- important unread Gmail messages;
- starred Google Drive files that may need attention;
- open GitHub issues and pull requests involving you; and
- Airtable records due today.

The workflow lives at `.github/workflows/morning-brief.yml` and runs every day at `12:00 UTC`. Change the cron expression if you want a different delivery time. The script calculates "today" with `BRIEF_TIMEZONE`, which defaults to `America/New_York` in the workflow.

## Required GitHub secrets

Create these in **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Purpose |
| --- | --- |
| `GOOGLE_CLIENT_ID` | OAuth client ID for Gmail, Calendar, and Drive. |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret. |
| `GOOGLE_REFRESH_TOKEN` | Refresh token with the scopes listed below. |
| `BRIEF_RECIPIENT_EMAIL` | Email address that should receive the brief. |
| `GMAIL_SENDER_EMAIL` | Gmail account used to send the brief. Usually the same as the recipient. |
| `AIRTABLE_TOKEN` | Airtable personal access token. |
| `AIRTABLE_BASE_ID` | Airtable base ID containing records to check. |
| `GH_BRIEF_TOKEN` | Optional fine-grained GitHub token. The workflow falls back to `GITHUB_TOKEN` for repo-local checks. |

## Required Google OAuth scopes

Generate the Google refresh token with these scopes:

```text
https://www.googleapis.com/auth/calendar.readonly
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/drive.metadata.readonly
```

## Optional GitHub variables

Create these in **Settings → Secrets and variables → Actions → Variables** if the defaults need tuning:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_CALENDAR_IDS` | `primary` | Comma-separated calendar IDs to include. |
| `GMAIL_BRIEF_QUERY` | `in:inbox is:unread (is:important OR category:primary) newer_than:14d` | Gmail search query for email that should be surfaced. |
| `GOOGLE_DRIVE_BRIEF_QUERY` | `starred = true and trashed = false` | Drive query for files that should be surfaced. |
| `GH_BRIEF_USERNAME` | blank | GitHub username to check for assigned issues and requested PR reviews. |
| `AIRTABLE_TABLE_NAME` | blank | Airtable table to check for due records. Required for Airtable results. |
| `AIRTABLE_VIEW_NAME` | blank | Optional Airtable view filter. |
| `AIRTABLE_DUE_FIELD` | `Due Date` | Airtable date field used to find items due today. |
| `AIRTABLE_STATUS_FIELD` | `Status` | Airtable status field used to exclude completed records. |
| `AIRTABLE_DONE_STATUSES` | `Done,Complete,Completed` | Comma-separated statuses considered complete. |

## Manual test

After adding secrets and variables, open **Actions → Morning brief → Run workflow**. The run summary will contain the generated brief, and Gmail will send the same brief to `BRIEF_RECIPIENT_EMAIL`.

If a provider is not configured yet, the brief includes a skipped-provider note instead of failing for that provider.
