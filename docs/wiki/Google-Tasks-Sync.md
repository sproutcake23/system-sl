# Google Tasks Sync

The SYSTEM can import your upcoming Google Tasks as quests.

## How it works

The sync engine (in `system_sl/core/sync_service.py`) uses a **TasksProvider**
that reads incomplete tasks due in the next six months from all of your
Google task lists. Each one is saved as a local quest; tasks you already have
are skipped.

Only one read-only scope is requested:

```
https://www.googleapis.com/auth/tasks.readonly
```

## Which OAuth client is used

An OAuth client identifies the app to Google. The SYSTEM uses the first one
it finds:

1. `credentials.json` in the system-sl data directory: your own client
2. The app's own client, bundled with the release AppImage and Windows
   builds

Release builds therefore need no setup. Running from source or a pip
install has no bundled client, so you set up your own (below).

## Who can sign in

Google only lets an account sign in if it is listed as a **test user** on the
OAuth client being used, while that client's consent screen is in **Testing**
mode. Any other account gets Google's "Access blocked" page.

The app's bundled client is currently in Testing, so it only works for
accounts the maintainer has added. Anyone else can set up their own client
below; with your own client, you add your own Google account in step 4.

## Using your own OAuth client

Create one in your own Google Cloud project. This is free and takes about
five minutes, and it overrides the bundled client.

1. **Create a project.** Open the [Google Cloud Console](https://console.cloud.google.com/),
   pick the project dropdown at the top, and choose **New project**.
2. **Enable the Tasks API.** Go to **APIs & Services → Library**, search for
   **Google Tasks API**, and click **Enable**.
3. **Configure the consent screen.** Go to **Google Auth Platform** (labelled
   **OAuth consent screen** in some consoles) and click **Get started**:
   - App name: anything, for example `System-SL`
   - Audience: **External**
   - Fill in the support and contact email fields with your own address.
4. **Add yourself as a test user.** Under **Audience → Test users**, add the
   Google account whose tasks you want to sync.
5. **Create the OAuth client.** Under **Clients**, click **Create client**,
   choose application type **Desktop app**, and create it. Click
   **Download JSON**.
6. **Install the file.** Save it as `credentials.json` in the system-sl data
   directory:
   - Linux/macOS: `~/.config/system-sl/credentials.json`
   - Windows: `%APPDATA%\system-sl\credentials.json`

   Google names the download `client_secret_<id>.apps.googleusercontent.com.json`.
   Some file managers hide the `.json` extension while renaming, which leaves
   you with `credentials.json.json`; check the full name if sync says the
   credentials were not found.

   This file identifies your OAuth client, not your Google account, but keep
   it private anyway: never commit it or paste its contents anywhere.

## First sync

Google sync is off by default. Tick **Google Tasks Sync** in the main window
to show the **Google** button, then click it. Your browser opens a Google sign-in page:

1. Choose a Google account that is a test user on the client in use.
2. Google warns that it **hasn't verified this app**. That is expected while
   the client is unverified: click **Continue**.
3. Allow read access to your tasks. The browser then says the authentication
   flow has completed, and the app shows how many tasks were imported.

You have two minutes to finish the sign-in. If no browser opens, the sign-in
URL is printed to the terminal the app was started from.

The resulting token is stored in your system keyring (KDE Wallet, GNOME
Keyring, macOS Keychain or Windows Credential Manager), or in an encrypted
file if no keyring is available. It is never written to disk in plaintext.

## Weekly re-sign-in in Testing mode

While the client's consent screen is in **Testing**, Google expires the
token after 7 days. The app notices and opens the sign-in page again. This
applies to the bundled client too until the maintainer publishes it. If you
use your own client, you can avoid it: go to **Google Auth Platform →
Audience** and click **Publish app**. You'll still see the unverified-app warning at sign-in, but
the token no longer expires weekly. Google review is only needed if you share
the client with more than 100 people.

## Running a sync from code

```python
from system_sl.core.sync_service import GoogleSyncEngine, TasksProvider

engine = GoogleSyncEngine()
imported, skipped = engine.execute_sync(TasksProvider(engine.client))
```
