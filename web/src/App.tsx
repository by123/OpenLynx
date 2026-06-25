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
      if (s.project && s.project_turn_count > 0) setScope("project");
      else setScope("global");
    });
  }, []);

  const projectRoot = useMemo(() => {
    const dir = scopes?.project_dir?.replace(/[\\/]+$/, "");
    if (!dir) return "";
    return dir.endsWith(`${dir.includes("\\") ? "\\" : "/"}.lynx-memory`)
      ? dir.replace(/[\\/]\.lynx-memory$/, "")
      : dir;
  }, [scopes?.project_dir]);

  const projectName = useMemo(() => {
    if (!projectRoot) return "";
    return projectRoot.split(/[\\/]/).pop() ?? "";
  }, [projectRoot]);

  useEffect(() => {
    document.title = scope === "project" && projectName ? `Openlynx · ${projectName}` : "Openlynx";
  }, [projectName, scope]);

  const projectPath = projectRoot || "no project marker found";
  const globalPath = scopes?.global_dir ?? "";

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
          <div className="scope-group">
            <span className="topbar-label">{t("scope.label")}</span>
            <div className="scope-switch" role="tablist" aria-label={t("scope.label")}>
              <button
                className={`scope-btn${scope === "project" ? " active" : ""}`}
                disabled={!scopes?.project}
                onClick={() => changeScope("project")}
                data-tooltip={projectPath}
              >
                <span className="scope-dot" /> {projectName || t("scope.project")}
              </button>
              <button
                className={`scope-btn${scope === "global" ? " active" : ""}`}
                onClick={() => changeScope("global")}
                data-tooltip={globalPath}
              >
                <span className="scope-dot" /> {t("scope.all")}
              </button>
            </div>
          </div>

          <span className="topbar-divider" aria-hidden="true" />

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

      <SettingsPanel open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      {helpOpen && <HelpModal onClose={() => setHelpOpen(false)} />}

      <MemoryGrid
        scope={scope}
        lens={lens}
        scopesReady={scopes !== null}
        refreshKey={refreshKey}
        selectedId={selectedId}
        onSelect={setSelection}
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
