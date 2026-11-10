import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import type { WorklogIndex, WorklogSearchItem } from "../types";
import { Markdown } from "./Markdown";
import { useI18n } from "../i18n";

type Tab = "days" | "projects" | "loops";

const STATUS_ICON: Record<string, string> = {
  done: "✅",
  "done-unverified": "🧪",
  uncommitted: "📝",
  "discussed-only": "💬",
  abandoned: "🗑️",
};

function statusIcon(s?: string | null): string {
  if (!s) return "";
  if (s.startsWith("blocked")) return "⛔";
  return STATUS_ICON[s] ?? "•";
}

function weekday(day: string, lang: string): string {
  const d = new Date(`${day}T12:00:00`);
  return d.toLocaleDateString(lang === "zh" ? "zh-CN" : "en-US", { weekday: "short" });
}

/** Machine-wide work journal: per-day logs, project cards, open loops, search. */
export function WorklogView() {
  const { t, lang } = useI18n();
  const [index, setIndex] = useState<WorklogIndex | null>(null);
  const [tab, setTab] = useState<Tab>("days");
  const [sel, setSel] = useState<string | null>(null);
  const [md, setMd] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<WorklogSearchItem[] | null>(null);

  useEffect(() => {
    api
      .worklogIndex()
      .then(setIndex)
      .catch((e) => setError(String(e)));
  }, []);

  const days = useMemo(
    () => Object.entries(index?.days ?? {}).sort((a, b) => b[0].localeCompare(a[0])),
    [index],
  );
  const projects = useMemo(
    () =>
      Object.entries(index?.projects ?? {}).sort(
        (a, b) => (b[1].updated_at ?? 0) - (a[1].updated_at ?? 0),
      ),
    [index],
  );

  const openTab = (next: Tab, target?: string | null) => {
    setTab(next);
    setResults(null);
    if (target !== undefined) {
      setSel(target);
    } else if (next === "days") {
      setSel(days[0]?.[0] ?? null);
    } else if (next === "projects") {
      setSel(projects[0]?.[1].slug ?? null);
    } else {
      setSel("open-loops");
    }
  };

  // first load → newest day (only on the Days tab: an empty Projects tab
  // also has sel === null and must not fetch a day as a project slug)
  useEffect(() => {
    if (index && tab === "days" && sel === null && days.length) setSel(days[0][0]);
  }, [index, days, sel, tab]);

  useEffect(() => {
    if (!sel) {
      setMd("");
      return;
    }
    let alive = true;
    setLoading(true);
    setError(null);
    const req =
      tab === "days"
        ? api.worklogDay(sel)
        : tab === "projects"
          ? api.worklogProject(sel)
          : api.worklogOpenLoops();
    req
      .then((r) => alive && setMd(r.markdown))
      .catch((e) => alive && setError(String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [sel, tab]);

  const onSearch = (e: FormEvent) => {
    e.preventDefault();
    const query = q.trim();
    if (!query) {
      setResults(null);
      return;
    }
    api
      .worklogSearch(query)
      .then((r) => setResults(r.items))
      .catch((err) => setError(String(err)));
  };

  if (index && !index.exists) {
    return (
      <main className="worklog worklog-empty">
        <div className="worklog-empty-card">
          <h2>{t("wl.empty.title")}</h2>
          <p>{t("wl.empty.body")}</p>
          <pre>
            lynx-memory worklog backfill{"\n"}lynx-memory schedule install
          </pre>
          <p className="muted">{index.dir}</p>
        </div>
      </main>
    );
  }

  return (
    <main className="worklog">
      <aside className="worklog-side">
        <form className="worklog-search" onSubmit={onSearch}>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={t("wl.search")}
            aria-label={t("wl.search")}
          />
        </form>
        <div className="worklog-tabs" role="tablist">
          {(["days", "projects", "loops"] as Tab[]).map((k) => (
            <button
              key={k}
              role="tab"
              aria-selected={tab === k && !results}
              className={tab === k && !results ? "active" : ""}
              onClick={() => openTab(k)}
            >
              {t(k === "days" ? "wl.days" : k === "projects" ? "wl.projects" : "wl.loops")}
            </button>
          ))}
        </div>
        <div className="worklog-list">
          {tab === "days" &&
            days.map(([day, info]) => (
              <button
                key={day}
                className={`worklog-item${sel === day && !results ? " active" : ""}`}
                onClick={() => openTab("days", day)}
              >
                <span className="worklog-item-head">
                  <span>
                    {day} <span className="muted">{weekday(day, lang)}</span>
                  </span>
                  <span className="worklog-count">
                    {info.turns} {t("wl.turns")}
                  </span>
                </span>
                <span className="worklog-item-sub">
                  {info.status === "partial" && <em>{t("wl.partial")} · </em>}
                  {info.projects.slice(0, 4).join(" · ")}
                  {info.projects.length > 4 ? ` +${info.projects.length - 4}` : ""}
                </span>
              </button>
            ))}
          {tab === "projects" &&
            projects.map(([key, p]) => (
              <button
                key={key}
                className={`worklog-item${sel === p.slug && !results ? " active" : ""}`}
                onClick={() => openTab("projects", p.slug)}
                title={key}
              >
                <span className="worklog-item-head">
                  <span>{p.name}</span>
                </span>
                <span className="worklog-item-sub">
                  {t("wl.through")} {p.through_day ?? "-"}
                </span>
              </button>
            ))}
        </div>
      </aside>

      <section className="worklog-main">
        {error && <div className="worklog-error">{error}</div>}
        {results ? (
          <div className="worklog-results">
            <div className="worklog-results-head">
              <span>{t("wl.search.count", { n: results.length })}</span>
              <button className="ghost-btn" onClick={() => setResults(null)}>
                {t("wl.search.clear")}
              </button>
            </div>
            {results.length === 0 && <p className="muted">{t("wl.search.none")}</p>}
            {results.map((r) => (
              <button key={r.id} className="worklog-hit" onClick={() => openTab("days", r.day)}>
                <span className="worklog-hit-meta">
                  {r.day} · {r.project_name ?? r.project_key} ·{" "}
                  {r.kind === "fact" ? `${t("wl.fact")}/${r.type ?? ""}` : `${t("wl.work")}/${r.type ?? ""}`}
                  {r.status ? ` · ${statusIcon(r.status)} ${r.status}` : ""}
                </span>
                {r.title && <strong>{r.title}</strong>}
                <span>{r.text}</span>
              </button>
            ))}
          </div>
        ) : loading && !md ? (
          <div className="worklog-loading">
            <span className="spinner" /> {t("loading")}
          </div>
        ) : (
          <article className="worklog-doc">
            <Markdown text={md} />
          </article>
        )}
      </section>
    </main>
  );
}
