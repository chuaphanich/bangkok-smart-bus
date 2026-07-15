# Deploy Bangkok Smart Bus (public website)

Models and BMTA cache live **on the server**. Anyone can open the URL without this repo on their device.

## 1. Push to GitHub

```bash
# from this project folder
gh repo create bangkok-smart-bus --public --source=. --remote=origin --push
```

Or create a repo on github.com, then:

```bash
git remote add origin https://github.com/<you>/bangkok-smart-bus.git
git push -u origin main
```

## 2. Deploy on Render (recommended)

1. Sign up at [https://render.com](https://render.com) (GitHub login).
2. **New → Blueprint** and select this repo (uses `render.yaml`),  
   **or** **New → Web Service**:
   - Runtime: **Python 3**
   - Build: `pip install -r requirements_bangkok_bus.txt`
   - Start: `gunicorn -b 0.0.0.0:$PORT -w 1 --timeout 180 app:app`
   - Instance: **Free**
3. Deploy. First build may take a few minutes (deps + model load).
4. Open `https://<your-service>.onrender.com`.

Optional env vars (Render → Environment): `GOOGLE_MAPS_API_KEY`, `ORS_API_KEY`.

### Free tier note

The free service **sleeps after ~15 minutes idle**. The next visit can take 30–60s while it wakes and loads models. Upgrade to a paid plan for always-on.

## 3. Local production-style run

```bash
.venv_bangkok/bin/pip install -r requirements_bangkok_bus.txt
PORT=5050 HOST=0.0.0.0 .venv_bangkok/bin/gunicorn -b 0.0.0.0:5050 -w 1 --timeout 180 app:app
```

## What ships with the deploy

| Included | Not in git |
|---|---|
| `data/cache/*.json`, `historical.csv`, `models.joblib` | `data/cache/*.zip` (GTFS, ~40MB) |
| Flask UI + optimiser | `.env` / API secrets |

If cache is present, the server does **not** need to re-download GTFS at boot.
