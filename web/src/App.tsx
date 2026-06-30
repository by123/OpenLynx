import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import type { Lens, Scope, ScopesResponse, Selection, Turn } from "./types";
import { MemoryGrid } from "./components/MemoryGrid";
import { MemoryDetail } from "./components/MemoryDetail";
import { SettingsPanel } from "./components/SettingsPanel";
import { HelpModal } from "./components/HelpModal";
import { useI18n } from "./i18n";

export default function App() {
  const { t, lang, setLang } = useI18n();
  const [scopes, setScopes] = useState<ScopesResponse | null>(null);
  const [scope, setScope] = useState<Scope>("global");
  const [lens, setLens] = useState<Lens>("memory");
  const [selection, setSelection] = useState<Selection | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    const stored = typeof localStorage !== "undefined" ? localStorage.getItem("cm-theme") : null;
    if (stored === "light" || stored === "dark") return stored;
    if (typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: light)").matches) {
      return "light";
    }
    return "dark";
  });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("cm-theme", theme);
    } catch {
      /* ignore */
    }
  }, [theme]);

  useEffect(() => {
    api.scopes().then((s) => {
      setScopes(s);
      // Default to the cwd's project when it has memories, else the global store.
      const cur = s.scopes.find((x) => x.id === s.current_id);
      setScope(cur && cur.kind === "project" && cur.turn_count > 0 ? cur.id : "global");
    });
  }, []);

  // Re-fetch the tab list after the Settings panel rescans or toggles visibility,
  // keeping the current selection if it's still visible.
  const reloadScopes = () => {
    api.scopes().then((s) => {
      setScopes(s);
      setScope((cur) => {
        const visible = s.scopes.filter((x) => !x.hidden);
        if (visible.some((x) => x.id === cur)) return cur;
        if (visible.some((x) => x.id === s.current_id)) return s.current_id;
        return "global";
      });
    });
  };

  const visibleScopes = useMemo(() => scopes?.scopes.filter((s) => !s.hidden) ?? [], [scopes]);
  const activeScope = useMemo(
    () => scopes?.scopes.find((s) => s.id === scope) ?? null,
    [scopes, scope],
  );
  const globalPath = scopes?.global_dir ?? "";

  useEffect(() => {
    document.title =
      activeScope && activeScope.kind === "project" ? `Openlynx · ${activeScope.name}` : "Openlynx";
  }, [activeScope]);

  const changeScope = (s: Scope) => {
    setScope(s);
    setSelection(null);
  };
  const changeLens = (l: Lens) => {
    setLens(l);
    setSelection(null);
  };

  const selectedId = selection ? (selection.kind === "memory" ? selection.turn.id : selection.item.id) : null;

  return (
    <div className="layout">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" />
          <span className="brand-name">Openlynx</span>
          <span className="brand-sub">{t("brand.sub")}</span>
        </div>

        <div className="view-switch" role="tablist" aria-label="view">
          <button
            role="tab"
            className={`view-pill${lens === "memory" ? " active" : ""}`}
            onClick={() => changeLens("memory")}
            title={t("view.memory.title")}
          >
            {t("view.memory")}
          </button>
          <button
            role="tab"
            className={`view-pill${lens === "retrieval" ? " active" : ""}`}
            onClick={() => changeLens("retrieval")}
            title={t("view.retrieval.title")}
          >
            {t("view.retrieval")}
          </button>
        </div>

        <div className="topbar-right">
          <button
            className="icon-btn lang-btn"
            onClick={() => setLang(lang === "en" ? "zh" : "en")}
            title={t("lang.title")}
            aria-label={t("lang.title")}
          >
            {lang === "en" ? "EN" : "中"}
          </button>
          <button
            className="icon-btn"
            onClick={() => setHelpOpen(true)}
            title={t("action.help")}
            aria-label={t("action.help.aria")}
          >
            ?
          </button>
          <button
            className="icon-btn"
            onClick={() => setSettingsOpen(true)}
            title={t("action.settings")}
            aria-label={t("action.settings.aria")}
          >
            ⚙
          </button>
          <button
            className="theme-toggle"
            onClick={() => setTheme((th) => (th === "dark" ? "light" : "dark"))}
            title={theme === "dark" ? t("theme.toLight") : t("theme.toDark")}
            aria-label={t("theme.aria")}
          >
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </div>
      </header>

      <div className="scope-bar">
        <span className="topbar-label">{t("scope.label")}</span>
        <div className="scope-switch scope-tabs" role="tablist" aria-label={t("scope.label")}>
          {visibleScopes.map((s) => (
            <button
              key={s.id}
              role="tab"
              aria-selected={scope === s.id}
              className={`scope-btn${scope === s.id ? " active" : ""}${
                s.is_current && s.kind === "project" ? " current" : ""
              }`}
              onClick={() => changeScope(s.id)}
              title={s.kind === "global" ? globalPath : s.root ?? s.dir}
            >
              <span className="scope-dot" />
              <span className="scope-name">{s.kind === "global" ? t("scope.all") : s.name}</span>
              <span className="scope-count">{s.turn_count}</span>
            </button>
          ))}
        </div>
      </div>

      <SettingsPanel
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onProjectsChanged={reloadScopes}
      />
      {helpOpen && <HelpModal onClose={() => setHelpOpen(false)} />}

      <MemoryGrid
        scope={scope}
        lens={lens}
        scopesReady={scopes !== null}
        refreshKey={refreshKey}
        selectedId={selectedId}
        onSelect={setSelection}
        onCountsChanged={reloadScopes}
      />

      {selection && (
        <Drawer onClose={() => setSelection(null)}>
          <MemoryDetail
            selection={selection}
            scope={scope}
            onChanged={() => setRefreshKey((k) => k + 1)}
            onDeleted={() => {
              setSelection(null);
              setRefreshKey((k) => k + 1);
              // a delete changes the tab's memory count — refresh the badges too
              reloadScopes();
            }}
            onOpenMemory={(turn: Turn) => setSelection({ kind: "memory", turn })}
          />
        </Drawer>
      )}
    </div>
  );
}

function Drawer({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  const { t } = useI18n();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <button className="drawer-close" onClick={onClose} aria-label={t("close")}>
          ×
        </button>
        {children}
      </aside>
    </div>
  );
}
