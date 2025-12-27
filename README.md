# Umamusume Tracker (Global)

FastAPI service that aggregates **Umamusume Global** banners + events and exposes them in a format the Home Page Dashboard can render.

## API

- `GET /api/events` - returns the dashboard payload (`split-slide`)
- `POST /api/refresh` - triggers a background refresh of the internal cache

## Raspberry Pi (systemd)

This repo includes unit files to:

- run the API continuously (`umamusume-tracker.service`)
- refresh the cache once per day (recommended) via `umamusume-tracker-refresh.timer`

### Install

```bash
sudo cp umamusume-tracker.service /etc/systemd/system/
sudo cp systemd/umamusume-tracker-refresh.service /etc/systemd/system/
sudo cp systemd/umamusume-tracker-refresh.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now umamusume-tracker.service
sudo systemctl enable --now umamusume-tracker-refresh.timer
```

Default refresh schedule is `00:01` daily.

### Logs

```bash
journalctl -u umamusume-tracker.service -f
journalctl -u umamusume-tracker-refresh.service -n 200 --no-pager
```
