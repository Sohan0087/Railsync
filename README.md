# RailSync — Railway Maintenance Coordination & Possession Optimization

A working full-stack app: Flask + SQLite backend, an explainable optimization
engine, two real machine-learning pipelines (Kaggle-backed, with offline
fallbacks), and a dark control-room-style dashboard frontend (HTML/CSS/JS +
Chart.js).

## What it does

- Tracks rail assets (track, signal, switch, overhead line, bridge, platform),
  crews/machines, the train timetable per corridor, and safety buffer rules.
- Lets operators log maintenance requests (defect + severity + duration).
- **Priority scoring** — weighted, explainable 0–100 score from asset
  criticality, defect severity, asset condition, and how long a request has
  waited.
- **Window optimization** — for each request, scans the train timetable for
  gaps big enough for the job (respecting a safety buffer that depends on
  asset type), picks the lowest-disruption gap, checks a matching crew/machine
  is free, avoids double-booking the line or the resource, and — if a night is
  full — automatically rolls the job to the next available night. If nothing
  fits in 14 days it's flagged as a conflict for a human to resolve.
- **Human-in-the-loop** — nothing is executed automatically. Every
  recommendation has a plain-English explanation and sits as "Optimized"
  until a person clicks Approve (or Reject). "Re-optimize" re-runs the whole
  pipeline for everything not yet approved, e.g. after a new urgent request.
- **AI track-fault inspection** — trains an image classifier on the Kaggle
  *Railway Track Fault Detection* dataset (real photos of defective vs. sound
  track). Upload a photo (or run a demo sample) and get a defect probability
  that can feed back into an asset's record.
- **Live Indian Railways network data** — pulls real station/timetable data
  from the Kaggle *Indian Railways Dataset* you linked, ranks the busiest
  stations, and trains a small classifier that predicts a station's traffic
  tier from its zone/state.

Both ML pipelines are written so the whole app stays 100% functional even
without internet access: if the Kaggle download can't be reached, each one
automatically falls back to a clearly-labelled synthetic-but-realistic (or, for
the Indian Railways data, a small curated *real* subset) dataset, trains the
same model on that instead, and says so in the UI. Nothing silently breaks.

## Project layout

```
railsync/
├── backend/
│   ├── app.py              Flask app & all REST routes
│   ├── db.py                SQLite schema, connection helper, demo seed data
│   ├── optimizer.py         priority scoring + possession-window optimizer
│   ├── kaggle_config/        put your kaggle.json here (see Setup)
│   └── ml/
│       ├── model_pipeline.py   track-fault image classifier (Kaggle + fallback)
│       └── railways_data.py    Indian Railways station/timetable loader + classifier
├── frontend/
│   ├── templates/index.html
│   └── static/{css,js}/
├── requirements.txt
└── README.md
```

## Setup

1. **Python deps** (Python 3.10+):
   ```bash
   cd railsync
   python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
   pip install -r requirements.txt
   ```

2. **Kaggle credentials** (optional, but needed for the *real* datasets
   instead of the offline fallbacks):
   - Go to kaggle.com → Account → Create New API Token, which downloads a
     `kaggle.json`.
   - Place it at `backend/kaggle_config/kaggle.json`.
   - **Never commit this file or share it** — it's your personal API key.
     It's already in `.gitignore`.
   - You must also accept each dataset's terms once on kaggle.com before the
     API can download it:
     - https://www.kaggle.com/datasets/salmaneunus/railway-track-fault-detection
     - https://www.kaggle.com/datasets/sripaadsrinivasan/indian-railways-dataset

3. **Run it:**
   ```bash
   python3 backend/app.py
   ```
   Then open **http://localhost:5050**. The database is created and seeded
   automatically on first run (`backend/railsync.db`).

   To wipe and reseed the demo data at any time: `POST /api/reset-demo`.

## Using it

- **Optimize schedule** — runs the priority + window optimizer over every
  pending/optimized request.
- **Re-optimize** — clears not-yet-approved recommendations and reruns, useful
  after adding a new urgent request.
- Click the **▸** next to any request row to see the full explanation and
  priority breakdown.
- **Train / retrain model** (AI panel) — runs the track-fault classifier
  pipeline; shows whether it used the real Kaggle images or the offline
  fallback, plus accuracy/F1.
- **Run AI inspection** — upload a track photo (or leave it blank to score a
  demo sample) and, if you pick an asset, the result is saved to that asset.
- **Load real network data** — runs the Indian Railways pipeline; shows
  busiest stations, zone breakdown, and the traffic-tier classifier's
  accuracy.

## Notes on "real-time" data

Both ML features call out to Kaggle's API live when you run the app — that's
the real-time part. What they fetch is a static dataset snapshot (Kaggle
datasets aren't streaming feeds), so "real-time" here means *fetched live from
the API when you click the button*, not a continuously-updating feed. If you
later want a true live feed (e.g. live train running-status APIs), the same
`ml/` pattern — fetch → parse defensively → store → analyze — is the place to
plug it in.

## Security

- `backend/kaggle_config/kaggle.json`, the SQLite database, and all generated
  model/data files are git-ignored — don't remove them from `.gitignore`.
  Treat `kaggle.json` like a password.
- This is a demo/dev server (`app.run(debug=False)` by default). For a real
  deployment, put it behind a production WSGI server (gunicorn/uwsgi) and add
  authentication — there currently is none, by design, to keep the demo simple.
