import { useEffect, useState } from "react";
import { api, DirListing } from "../api/client";

export default function FolderPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (path: string) => void;
}) {
  const [listing, setListing] = useState<DirListing | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [cwd, setCwd] = useState<string | undefined>(value || undefined);

  const load = (path?: string) => {
    setErr(null);
    api
      .listDirs(path)
      .then((l) => {
        setListing(l);
        setCwd(l.path);
      })
      .catch((e) => setErr(String(e.message ?? e)));
  };

  useEffect(() => {
    load(value || undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectHere = () => cwd && onChange(cwd);

  return (
    <div className="fp">
      <div className="fp-path">
        {listing?.path ?? "…"}
        {value === cwd && cwd ? "  ✓ selected" : ""}
      </div>
      {err && <div className="fp-path error">{err}</div>}
      <div className="fp-list">
        {listing?.parent && (
          <div className="fp-item" onClick={() => load(listing.parent!)}>
            <span>← ..</span>
          </div>
        )}
        {listing?.entries.length === 0 && (
          <div className="fp-item muted">no subfolders here</div>
        )}
        {listing?.entries.map((e) => (
          <div key={e.path} className="fp-item" onClick={() => load(e.path)}>
            <span>📁 {e.name}</span>
            {e.has_children && <span className="muted">›</span>}
          </div>
        ))}
      </div>
      <div className="fp-actions">
        <button type="button" className="sm primary" onClick={selectHere}>
          Use this folder
        </button>
        <input
          placeholder="new subfolder name"
          value={newName}
          onChange={(ev) => setNewName(ev.target.value)}
          // don't let Enter submit a surrounding form (e.g. start a recording)
          onKeyDown={(ev) => ev.key === "Enter" && ev.preventDefault()}
          style={{ flex: 1 }}
        />
        <button
          type="button"
          className="sm"
          disabled={!newName.trim() || !cwd}
          onClick={() =>
            cwd &&
            api
              .makeDir(cwd, newName.trim())
              .then((l) => {
                setListing(l);
                setNewName("");
              })
              .catch((e) => setErr(String(e.message ?? e)))
          }
        >
          Create
        </button>
      </div>
    </div>
  );
}
