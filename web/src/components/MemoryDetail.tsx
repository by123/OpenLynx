import { useEffect, useState } from "react";
import { api } from "../api";
import type {
  RetrievalDetail,
  RetrievalSummary,
  Scope,
  Selection,
  TagAttachment,
  Turn,
  TurnRetrievalsResponse,
} from "../types";
import { Markdown } from "./Markdown";
import { clip, fmtTs } from "../utils/format";
import { useI18n } from "../i18n";

interface Props {
  selection: Selection;
  scope: Scope;
  onChanged: () => void;
  onDeleted: () => void;
  onOpenMemory: (turn: Turn) => void;
}

function inferSummarySource(source: string | null | undefined, model: string | null | undefined): string | null {
  if (source && source.trim()) return source.trim();
  const m = (model ?? "").toLowerCase();
  if (m.includes("codex")) return "codex";
  if (m.includes("claude") || m.includes("haiku")) return "haiku";
  return null;
}

export function MemoryDetail({ selection, scope, onChanged, onDeleted, onOpenMemory }: Props) {
  return (
    <div className="detail-inner">
      {selection.kind === "memory" ? (
        <MemoryView
          key={selection.turn.id}
          turn={selection.turn}
          scope={scope}
          onChanged={onChanged}
          onDeleted={onDeleted}
        />
      ) : (
        <RetrievalView key={selection.item.id} item={selection.item} scope={scope} onOpenMemory={onOpenMemory} />
      )}
    </div>
  );
}

function MemoryView({
  turn,
  scope,
  onChanged,
  onDeleted,
}: {
  turn: Turn;
  scope: Scope;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const { t } = useI18n();
  const [tags, setTags] = useState<TagAttachment[]>(turn.tags);
  const [newTag, setNewTag] = useState("");
  const [summary, setSummary] = useState<string | null>(turn.summary ?? null);
  const [summarySource, setSummarySource] = useState<string | null>(turn.summary_source ?? null);
  const [summaryModel, setSummaryModel] = useState<string | null>(turn.summary_model ?? null);
  const [summaryBusy, setSummaryBusy] = useState(false);
  const [summaryErr, setSummaryErr] = useState<string | null>(null);
  const [retr, setRetr] = useState<TurnRetrievalsResponse | null>(null);

  const displaySource = inferSummarySource(summarySource, summaryModel);

  useEffect(() => {
    let cancelled = false;
    setRetr(null);
    api
      .turnRetrievals(scope, turn.id)
      .then((d) => !cancelled && setRetr(d))
      .catch(() => !cancelled && setRetr({ items: [], total: 0 }));
    return () => {
      cancelled = true;
    };
  }, [scope, turn.id]);

  const regenerate = async () => {
    setSummaryBusy(true);
    setSummaryErr(null);
    try {
      const r = await api.regenerateSummary(scope, turn.id);
      setSummary(r.summary);
      setSummarySource(r.summary_source);
      setSummaryModel(r.summary_model);
      onChanged();
    } catch (e) {
      setSummaryErr(String(e));
    } finally {
      setSummaryBusy(false);
    }
  };

  const addTag = async (e: React.FormEvent) => {
    e.preventDefault();
    const clean = newTag.trim().replace(/^#/, "");
    if (!clean) return;
    try {
      await api.addTag(scope, turn.id, clean, "custom");
      setTags((prev) =>
        prev.some((x) => x.name === clean)
          ? prev
          : [...prev, { name: clean, kind: "custom", source: "manual" }].sort((a, b) =>
              `${a.kind}:${a.name}`.localeCompare(`${b.kind}:${b.name}`),
            ),
      );
      setNewTag("");
      onChanged();
    } catch (e) {
      alert(t("tag.addFail", { e: String(e) }));
    }
  };

  const removeTag = async (name: string) => {
    try {
      await api.removeTag(scope, turn.id, name);
      setTags((prev) => prev.filter((x) => x.name !== name));
      onChanged();
    } catch (e) {
      alert(t("tag.removeFail", { e: String(e) }));
    }
  };

  const del = async () => {
    if (!confirm(t("detail.delete.confirm"))) return;
    try {
      await api.deleteTurn(scope, turn.id);
      onDeleted();
    } catch (e) {
      alert(t("detail.deleteFail", { e: String(e) }));
    }
  };

  return (
    <>
      <div className="detail-head">
        <div className="detail-title-ts">
          <span>{fmtTs(turn.ts)}</span>
          {typeof turn.score === "number" && <span className="score">score {turn.score.toFixed(3)}</span>}
          {typeof turn.retrieval_count === "number" && turn.retrieval_count > 0 && (
            <span className="entry-ref">{t("detail.retrievedTimes", { n: turn.retrieval_count })}</span>
          )}
        </div>
        <button className="danger" onClick={del}>
          {t("detail.delete")}
        </button>
      </div>

      <div className={`summary-block${summary ? "" : " empty"}`}>
        <div className="summary-head">
          <span className="summary-tag">{t("summary.label")}</span>
          {displaySource && <span className="summary-source">{t("summary.source", { s: displaySource })}</span>}
          {summaryModel && <span className="summary-model">{t("summary.model", { m: summaryModel })}</span>}
          <span className="summary-spacer" />
          <button className="link" onClick={regenerate} disabled={summaryBusy}>
            {summaryBusy ? t("summary.generating") : summary ? t("summary.regenerate") : t("summary.generate")}
          </button>
        </div>
        {summaryErr && <div className="error">{summaryErr}</div>}
        {summary ? <Markdown text={summary} /> : <div className="empty">{t("summary.empty")}</div>}
      </div>

      <div className="exchange">
        <div className="msg msg-user">
          <div className="avatar" aria-hidden>
            U
          </div>
          <div className="bubble">
            <div className="bubble-head">{t("role.user")}</div>
            <Markdown text={turn.user_msg} />
          </div>
        </div>
        <div className="msg msg-assistant">
          <div className="avatar" aria-hidden>
            ✦
          </div>
          <div className="bubble">
            <div className="bubble-head">{t("role.assistant")}</div>
            <Markdown text={turn.assistant_msg} />
          </div>
        </div>
      </div>

      <div className="tags-row">
        {tags.map((tag) => (
          <span key={`${tag.kind}:${tag.name}`} className="tag-chip" title={`${tag.source} tag`}>
            {`[${tag.kind}] ${tag.name}`}
            <button title={t("tag.remove")} onClick={() => removeTag(tag.name)}>
              ×
            </button>
          </span>
        ))}
        <form onSubmit={addTag} className="add-tag">
          <input placeholder={t("tag.add")} value={newTag} onChange={(e) => setNewTag(e.target.value)} />
        </form>
      </div>

      <div className="ref-panel">
        <div className="block-title">{t("refpanel.title")}</div>
        {retr === null && <div className="empty">{t("loading")}</div>}
        {retr && retr.items.length === 0 && <div className="empty">{t("refpanel.empty")}</div>}
        {retr && retr.items.length > 0 && (
          <ul className="ref-list">
            {retr.items.map((r) => (
              <li key={r.id}>
                <span className="ts">{fmtTs(r.ts)}</span>
                <span className="score">score {r.score.toFixed(3)}</span>
                <span className="ref-prompt">{clip(r.prompt, 120)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}

function RetrievalView({
  item,
  scope,
  onOpenMemory,
}: {
  item: RetrievalSummary;
  scope: Scope;
  onOpenMemory: (turn: Turn) => void;
}) {
  const { t } = useI18n();
  const [data, setData] = useState<RetrievalDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    api
      .retrievalDetail(scope, item.id)
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, [scope, item.id]);

  return (
    <>
      <div className="detail-head">
        <div className="detail-title-ts">
          <span>{fmtTs(item.ts)}</span>
          <span className="entry-ref">{t("retrieval.hits", { n: item.hit_count })}</span>
          {item.scope_used && <span className="hit-scope">{item.scope_used}</span>}
        </div>
      </div>

      <div className="msg msg-user">
        <div className="avatar" aria-hidden>
          ?
        </div>
        <div className="bubble">
          <div className="bubble-head">{t("retrieval.prompt")}</div>
          <Markdown text={item.prompt} />
        </div>
      </div>

      <div className="retrieval-detail">
        <div className="block-title">{t("retrieval.hitsTitle", { n: data ? data.hits.length : "…" })}</div>
        {error && <div className="error">{error}</div>}
        {!error && !data && <div className="empty">{t("loading")}</div>}
        {data && data.hits.length === 0 && <div className="empty">{t("retrieval.noHits")}</div>}
        {data && data.hits.length > 0 && (
          <ul className="hit-list">
            {data.hits.map((h) => (
              <li key={h.turn_id} className="hit">
                <div className="hit-head">
                  <span className="rank">#{h.rank + 1}</span>
                  <span className="score">score {h.score.toFixed(3)}</span>
                  {h.scope && <span className="hit-scope">{h.scope}</span>}
                  {h.kind && <span className="hit-scope">{h.kind}</span>}
                  {h.turn && (
                    <button className="link hit-open" onClick={() => onOpenMemory(h.turn!)}>
                      {t("retrieval.openMemory")}
                    </button>
                  )}
                </div>
                {h.turn ? (
                  <div className="hit-body">
                    <div className="hit-snippet">
                      <strong>{t("retrieval.q")}</strong>
                      {clip(h.turn.user_msg, 240)}
                    </div>
                    <div className="hit-snippet">
                      <strong>{t("retrieval.a")}</strong>
                      <Markdown inline text={clip(h.turn.assistant_msg, 360)} />
                    </div>
                  </div>
                ) : (
                  <div className="empty">{t("retrieval.deleted", { id: h.turn_id.slice(0, 8) })}</div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}
