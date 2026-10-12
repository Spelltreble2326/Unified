# Unified Fantasy

Django app for signing in and saving ESPN and Sleeper fantasy football leagues. The UI is Django templates, HTMX, and Tailwind CSS.

Private ESPN leagues need `espn_s2` and `SWID` session cookies. The Chrome extension in `extension/` copies those cookies so you can paste them into the add-league form. Sleeper leagues only need the public league ID.

## Setup

```bash
uv sync
npm install
npm run css
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

League sync runs in the background. Install Redis, start it, and run a Celery worker in another terminal:

```bash
redis-server
uv run celery -A config worker --loglevel=info
```

The broker defaults to `redis://127.0.0.1:6379/1` and the queue name is `unified`, so this worker does not share the default Redis database with other apps on the machine. Override the broker with `CELERY_BROKER_URL` if Redis is elsewhere. The worker concurrency is 1 because the app stores data in SQLite.

Open http://127.0.0.1:8000/ and log in with the superuser you just created. Accounts use Django’s username field (not email).

Rebuild CSS while editing templates with `npm run css:watch`.

## Add a league

- **Sleeper:** paste the numeric league ID from the league URL. No cookie.
- **ESPN public:** paste the `leagueId` query parameter.
- **ESPN private:** check Private league, then paste both cookies from the extension.

Saving a league checks the id with ESPN or Sleeper and stores the league name when you leave the name blank. The roster, activity, matchup, and standings pull is queued after that, same as the Sync button. The page shows Syncing and refreshes when the worker finishes. If Redis is not running, the league is still saved and the sync control shows an error so you can retry.

## Load the extension

1. Open `chrome://extensions`.
2. Turn on Developer mode.
3. Choose **Load unpacked** and select the `extension/` directory in this repo.
4. Sign in at [fantasy.espn.com](https://fantasy.espn.com) in that same browser.
5. Click the extension icon and copy `espn_s2` and `SWID` into the add-league form.

The extension only reads cookies in the browser. It does not send them anywhere. You paste them into the app yourself.

Cookies are stored in the database as plaintext. That is fine for this local proof of concept and not fine for a shared server.
