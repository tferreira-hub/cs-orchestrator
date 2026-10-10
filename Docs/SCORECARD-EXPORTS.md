# Account scorecard exports — current state & native-Google follow-up

The per-account scorecard matches (and extends) Dan's Apps Script dashboard. Export
options on the scorecard toolbar:

| Button | What it does today | Native? |
|---|---|---|
| **Download PDF** | Browser print → Save as PDF of the full scorecard | ✅ no setup |
| **Download CSV** | Import-ready CSV of all metric rows + signals | ✅ no setup |
| **Open in Google Sheets** | Copies the scorecard as TSV to the clipboard and opens `sheets.new`; the user pastes once at A1 (falls back to CSV download if the clipboard is blocked) | ⚠️ one paste step |
| **Export to Slides** | Opens a clean 16:9 single-slide view to Save-as-PDF / print and drop into Slides/Keynote | ⚠️ not a native deck |

## Why Sheets/Slides aren't fully native yet
Dan's dashboard writes directly to Google Sheets/Slides because it runs **inside Google
Workspace as the signed-in user** (Apps Script has the user's Drive scope implicitly). The
CS Platform is a standalone server — it has **no Google credentials**, so it cannot create
a Sheet or Slides deck in a user's Drive without an explicit **Google OAuth** consent flow.

## Follow-up to make them fully native (Level B)
To create a real Google Sheet / Slides deck in the user's Drive in one click:
1. Create a Google Cloud project + OAuth 2.0 client (Web), scopes:
   `drive.file`, `spreadsheets`, `presentations`.
2. Add a Google sign-in/consent step (separate from the existing Cognito SSO) and store the
   per-user token.
3. Add backend endpoints that call the Sheets API (`spreadsheets.create` + `values.update`)
   and Slides API (`presentations.create` + `batchUpdate`) with the scorecard payload.

This is a self-contained piece of work gated on the Google OAuth client + consent screen
being provisioned. Until then, the Level-A exports above cover the same output with one
extra click and **no external setup**.
