# Deploy Bangkok Smart Bus (public website)

Models and BMTA cache live **on the server**. Anyone can open the URL without this repo on their device.

Latest deploy-ready commit is on local `main` (includes `render.yaml`, `Dockerfile`, `models.joblib`).

## 1. Create a GitHub repo and push

**A. In the browser**

1. Go to [https://github.com/new](https://github.com/new)
2. Name: `bangkok-smart-bus` (public)
3. **Do not** add a README (repo already has commits)
4. Create the repository

**B. In Terminal** (from this project folder)

```bash
cd "/Users/kwankaochuaphanich/Desktop/Home/External School Work/Everything/bangkok_bus"

git remote add origin https://github.com/<YOUR_GITHUB_USERNAME>/bangkok-smart-bus.git
git push -u origin main
```

Or with GitHub CLI (if installed):

```bash
gh repo create bangkok-smart-bus --public --source=. --remote=origin --push
```

## 2. Deploy on Render

1. Sign up at [https://render.com](https://render.com) with **GitHub**.
2. **New → Blueprint** → select `bangkok-smart-bus` (uses [`render.yaml`](render.yaml)),  
   **or** **New → Web Service**:
   - Runtime: **Python 3**
   - Build command: `pip install -r requirements_bangkok_bus.txt`
   - Start command: `gunicorn -b 0.0.0.0:$PORT -w 1 --timeout 180 app:app`
   - Plan: **Free**
3. Wait for the first deploy (2–5 minutes).
4. Open `https://<your-service-name>.onrender.com`.

Optional Environment variables: `GOOGLE_MAPS_API_KEY`, `ORS_API_KEY`.

### Free tier note

The free service **sleeps after ~15 minutes idle**. The next visit can take 30–60s while it wakes. Upgrade later for always-on.

## 3. Local production-style run

```bash
.venv_bangkok/bin/pip install -r requirements_bangkok_bus.txt
PORT=5050 .venv_bangkok/bin/gunicorn -b 0.0.0.0:5050 -w 1 --timeout 180 app:app
```

## What ships with the deploy

| Included in git | Not in git |
|---|---|
| `data/cache/*.json`, `historical.csv`, `models.joblib` | `data/cache/*.zip` (~40MB GTFS) |
| Flask UI + optimiser + Gunicorn config | `.env` / API secrets |

With cache present, the server boots without re-downloading GTFS.
