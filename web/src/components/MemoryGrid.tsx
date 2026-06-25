import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Lens, RetrievalSummary, Scope, SearchMode, Selection, TagInfo, Turn } from "../types";
import { fmtTs, preview } from "../utils/format";
import { useI18n } from "../i18n";

const PAGE_SIZE = 30;

/** A YYYY-MM-DD string → epoch-second range covering that local calendar day. */
function dayRange(d: string): { since?: number; until?: number } {
  if (!d) return {};
  const start = new Date(`${d}T00:00:00`);
  if (Number.isNaN(start.getTime())) return {};
  const since = Math.floor(start.getTime() / 1000);
  return { since, until: since + 86400 };
}

interface Props {
  scope: Scope;
  lens: Lens;
  scopesReady: boolean;
  refreshKey: number;
  selectedId: string | null;
  onSelect: (sel: Selection) => void;
}

type Source = "all" | "top";

export function MemoryGrid({ scope, lens, scopesReady, refreshKey, selectedId, onSelect }: Props) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [mode, setMode] = useState<SearchMode>("keyword");
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [source, setSource] = useState<Source>("all");
  const [date, setDate] = useState("");
  const [tags, setTags] = useState<TagInfo[]>([]);
  const [page, setPage] = useState(1);

  const [memItems, setMemItems] = useState<Turn[]>([]);
  const [retItems, setRetItems] = useState<RetrievalSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setQuery("");
    setSubmitted("");
    setActiveTag(null);
    setSource("all");
    setDate("");
    setPage(1);
  }, [lens, scope]);

  useEffect(() => {
    if (!scopesReady || lens !== "memory") return;
    api.tags(scope).then(setTags).catch(() => setTags([]));
  }, [scope, lens, scopesReady, refreshKey]);

  useEffect(() => {
    if (!scopesReady) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        if (lens === "memory" && source === "top") {
          const r = await api.topReferenced(scope, PAGE_SIZE);
          if (cancelled) return;
          setMemItems(r.items);
          setTotal(r.items.length);
        } else if (lens === "memory") {
          const r = await api.turns({
            scope,
            page,
            pageSize: PAGE_SIZE,
            q: submitted || undefined,
            tag: activeTag || undefined,
            mode,
            ...dayRange(date),
          });
          if (cancelled) return;
          setMemItems(r.items);
          setTotal(r.total);
        } else {
          const r = await api.retrievals({ scope, page, pageSize: PAGE_SIZE, q: submitted || undefined });
          if (cancelled) return;
          setRetItems(r.items);
          setTotal(r.total);
        }
      } catch (e) {
        if (cancelled) return;
        setError(String(e));
        setMemItems([]);
        setRetItems([]);
        setTotal(0);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [scope, lens, source, page, submitted, activeTag, mode, date, scopesReady, refreshKey]);

  const totalPages = useMemo(() => {
    if (source === "top") return 1;
    if (lens === "memory" && mode === "semantic" && submitted) return 1;
    return Math.max(1, Math.ceil(total / PAGE_SIZE));
  }, [total, lens, source, mode, submitted]);

  const onSearch = (e: React.FormEvent) => {
    e.preventDefault();
    setPage(1);
    setSource("all");
    setSubmitted(query.trim());
  };

  const onClear = () => {
    setQuery("");
    setSubmitted("");
    setActiveTag(null);
    setDate("");
    setPage(1);
  };

  const sortedTags = useMemo(() => [...tags].sort((a, b) => b.count - a.count), [tags]);
  const hasFilter = Boolean(submitted || activeTag || date);

  let statusText: string;
  if (lens === "memory") {
    if (source === "top") {
      statusText = t("status.topReferenced", { n: total });
    } else {
      statusText =
        t("status.memories", { n: total }) +
        (date ? t("status.suffix.date", { date }) : "") +
        (activeTag ? t("status.suffix.tag", { tag: activeTag }) : "") +
        (submitted ? t("status.suffix.query", { q: submitted }) : "");
    }
  } else {
    statusText = t("status.retrievals", { n: total }) + (submitted ? t("status.suffix.query", { q: submitted }) : "");
  }

  const isEmpty = !loading && (lens === "memory" ? memItems.length === 0 : retItems.length === 0);

  return (
    <main className="gallery">
      <div className="gallery-toolbar">
        <div className="gallery-toolbar-row">
          <form className="gallery-search" onSubmit={onSearch}>
            <input
              placeholder={lens === "memory" ? t("search.memory") : t("search.prompts")}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label={lens === "memory" ? t("search.memory.aria") : t("search.prompts.aria")}
            />
          </form>

          {lens === "memory" && (
            <>
              <div className="mode" role="radiogroup" aria-label={t("mode.aria")}>
                <label className={mode === "keyword" ? "active" : ""}>
                  <input type="radio" checked={mode === "keyword"} onChange={() => setMode("keyword")} />
                  {t("mode.keyword")}
                </label>
                <label className={mode === "semantic" ? "active" : ""}>
                  <input type="radio" checked={mode === "semantic"} onChange={() => setMode("semantic")} />
                  {t("mode.semantic")}
                </label>
              </div>

              <div className="source-toggle" role="tablist" aria-label={t("source.aria")}>
                <button
                  className={source === "all" ? "active" : ""}
                  onClick={() => {
                    setSource("all");
                    setPage(1);
                  }}
                >
                  {t("source.all")}
                </button>
                <button
                  className={source === "top" ? "active" : ""}
                  onClick={() => {
                    setSource("top");
                    setActiveTag(null);
                    setPage(1);
                  }}
                  title={t("source.top.title")}
                >
                  {t("source.top")}
                </button>
              </div>

              <input
                type="date"
                className="date-input"
                value={date}
                aria-label={t("date.aria")}
                onChange={(e) => {
                  setDate(e.target.value);
                  setSource("all");
                  setPage(1);
                }}
              />

              {hasFilter && (
                <button type="button" className="ghost-btn" onClick={onClear}>
                  {t("clear")}
                </button>
              )}
            </>
          )}
        </div>

        {lens === "memory" && source === "all" && sortedTags.length > 0 && (
          <div className="tag-strip">
            {sortedTags.map((tg) => (
              <button
                key={tg.name}
                className={activeTag === tg.name ? "tag-pill active" : "tag-pill"}
                onClick={() => {
                  setActiveTag((prev) => (prev === tg.name ? null : tg.name));
                  setPage(1);
                }}
              >
                {tg.name} <span className="count">{tg.count}</span>
              </button>
            ))}
          </div>
        )}

        <div className="gallery-status">
          <span>{statusText}</span>
          {loading && <span className="loading">{t("loading")}</span>}
        </div>
      </div>

      {error && <div className="error" style={{ margin: "16px 24px" }}>{error}</div>}

      {isEmpty ? (
        <div className="empty-panel">
          <div className="empty-title">
            {submitted || activeTag
              ? t("empty.noMatch")
              : lens === "memory"
                ? t("empty.noMemory")
                : t("empty.noRetrieval")}
          </div>
          <p>
            {submitted || activeTag
              ? t("empty.body.filter")
              : lens === "memory"
                ? t("empty.body.memory")
                : t("empty.body.retrieval")}
          </p>
        </div>
      ) : (
        <div className="card-grid">
          {lens === "memory"
            ? memItems.map((m) => (
                <button
                  key={m.id}
                  className={`card${selectedId === m.id ? " active" : ""}`}
                  onClick={() => onSelect({ kind: "memory", turn: m })}
                >
                  <span className="card-title">{preview(m.summary || m.user_msg, 84)}</span>
                  <span className="card-body">
                    {m.summary ? t("card.q", { text: preview(m.user_msg, 150) }) : preview(m.assistant_msg, 150)}
                  </span>
                  <span className="card-foot">
                    {m.tags.length > 0 && (
                      <span className="card-tags">
                        {m.tags.slice(0, 3).map((tag) => (
                          <span key={`${tag.kind}:${tag.name}`} className="entry-tag">
                            {tag.name}
                          </span>
                        ))}
                      </span>
                    )}
                    <span className="card-foot-right">
                      {typeof m.retrieval_count === "number" && m.retrieval_count > 0 && (
                        <span className="entry-ref">↘ {m.retrieval_count}</span>
                      )}
                      {typeof m.score === "number" && <span className="entry-score">{m.score.toFixed(2)}</span>}
                      <span>{fmtTs(m.ts)}</span>
                    </span>
                  </span>
                </button>
              ))
            : retItems.map((it) => (
                <button
                  key={it.id}
                  className={`card${selectedId === it.id ? " active" : ""}`}
                  onClick={() => onSelect({ kind: "retrieval", item: it })}
                >
                  <span className="card-title">{preview(it.prompt, 96)}</span>
                  <span className="card-body card-body-muted">{t("retrieval.hitsCard", { n: it.hit_count })}</span>
                  <span className="card-foot">
                    <span className="card-foot-right">
                      {it.scope_used && <span className="hit-scope">{it.scope_used}</span>}
                      <span>{fmtTs(it.ts)}</span>
                    </span>
                  </span>
                </button>
              ))}
        </div>
      )}

      {totalPages > 1 && (
        <div className="pagination">
          <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            {t("page.prev")}
          </button>
          <span>
            {page} / {totalPages}
          </span>
          <button disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
            {t("page.next")}
          </button>
        </div>
      )}
    </main>
  );
}
