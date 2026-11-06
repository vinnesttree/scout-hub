# Deploying Scout Hub with Dockge on TrueNAS SCALE

The stack in [`scout-hub/`](scout-hub/) builds the image directly from this
GitHub repo — you don't need to clone anything onto the NAS.

Defaults assume your dataset is mounted at **`/mnt/ScoutData`**. If it's a
dataset inside a pool, the real path is `/mnt/<pool>/ScoutData` — check under
**Datasets** in the TrueNAS UI and set `SCOUTHUB_DATASET` accordingly.

## 1. Prepare the dataset (do this first!)

The container runs as TrueNAS's built-in **apps** user (UID/GID `568`), so it
must own the two folders it writes to. Open **System → Shell** in TrueNAS:

```bash
mkdir -p /mnt/ScoutData/data /mnt/ScoutData/media
chown -R 568:568 /mnt/ScoutData/data /mnt/ScoutData/media
```

> ⚠️ Don't skip this. If the folders don't exist, Docker creates them owned by
> `root` and Scout Hub exits on startup with a SQLite "unable to open database"
> error.

If the dataset uses the SMB permission preset (NFSv4 ACLs), instead use
**Datasets → ScoutData → Permissions → Edit** and add the `apps` user with
*Modify* access (apply recursively) — this also keeps the recordings reachable
from an SMB share.

Resulting layout:

```
/mnt/ScoutData/
  data/    SQLite database, overlay profiles, yt-dlp cache   (/data in container)
  media/   recordings + match clips, browsed by the folder picker (/media)
```

## 2. Create the stack in Dockge

1. In Dockge click **+ Compose**, name the stack `scout-hub`.
2. Paste the contents of [`scout-hub/compose.yaml`](scout-hub/compose.yaml) into
   the compose editor.
3. Paste [`scout-hub/.env.example`](scout-hub/.env.example) into the **.env**
   editor and adjust if needed (dataset path, port, timezone, RobotEvents
   token). The defaults already match `/mnt/ScoutData`, UID 568, port 8000.
4. Click **Deploy**.

(Alternatively copy the `scout-hub/` folder into Dockge's stacks directory —
whatever path you set when installing the Dockge app — and rename
`.env.example` to `.env`; it then shows up in Dockge.)

The first deploy builds the image (frontend + ffmpeg + tesseract) and takes a
few minutes. When the container shows **healthy**, open
`http://<truenas-ip>:8000` and continue from step 4 of the main
[README](../../README.md#run-with-docker-recommended) (RobotEvents token, import
event, start recording).

## Updating

Click **Update** in Dockge. The stack uses `pull_policy: build`, so this
rebuilds from the latest commit of `SCOUTHUB_GIT_REF` (default `main`); the
database and recordings on the dataset are untouched. To pin a version, set
`SCOUTHUB_GIT_REF` to a tag or commit.

Note that every Dockge **Deploy/Start** also runs a (cached, quick) rebuild, so
the NAS needs to reach github.com at that moment. Container restarts after a
reboot don't rebuild.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Exits with `unable to open database file` | Folder permissions — redo step 1, or set `PUID`/`PGID` to the dataset's owner (`ls -ln /mnt/ScoutData`). |
| `port is already allocated` | Change `SCOUTHUB_PORT` in `.env`. |
| Health shows `ocr: false` / `ffmpeg: false` | Open `http://<ip>:8000/api/health`; rebuild with **Update**. |
| Want to run as root instead | Set `PUID=0` and `PGID=0` (files on the dataset will be root-owned). |
