import type {
  AppSettings,
  RetrievalDetail,
  RetrievalsResponse,
  Scope,
  SearchMode,
  ScopesResponse,
  TagInfo,
  TopReferencedResponse,
  TurnRetrievalsResponse,
  TurnsResponse,
} from "./types";

async function jsonFetch<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const res = await fetch(input, init);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${text}`);
  }
  const ct = res.headers.get("content-type") ?? "";
  if (!ct.includes("application/json")) {
    throw new Error(`Server returned non-JSON response (${res.status}). Try restarting the server.`);
  }
  return (await res.json()) as T;
}

export const api = {
  scopes: () => jsonFetch<ScopesResponse>("/api/scopes"),

  turns: (params: {
    scope: Scope;
    page?: number;
    pageSize?: number;
    q?: string;
    tag?: string;
    mode?: SearchMode;
    since?: number;
    until?: number;
  }) => {
    const sp = new URLSearchParams();
    sp.set("scope", params.scope);
    if (params.page) sp.set("page", String(params.page));
    if (params.pageSize) sp.set("page_size", String(params.pageSize));
    if (params.q) sp.set("q", params.q);
    if (params.tag) sp.set("tag", params.tag);
    if (params.mode) sp.set("mode", params.mode);
    if (params.since !== undefined) sp.set("since", String(params.since));
    if (params.until !== undefined) sp.set("until", String(params.until));
    return jsonFetch<TurnsResponse>(`/api/turns?${sp}`);
  },

  deleteTurn: (scope: Scope, id: string) =>
    jsonFetch<{ ok: true }>(`/api/turns/${scope}/${id}`, { method: "DELETE" }),

  addTag: (scope: Scope, id: string, name: string, kind = "custom") =>
    jsonFetch<{ ok: true }>(`/api/turns/${scope}/${id}/tags`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, kind }),
    }),

  removeTag: (scope: Scope, id: string, name: string) =>
    jsonFetch<{ ok: true }>(`/api/turns/${scope}/${id}/tags/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  tags: (scope: Scope) => jsonFetch<TagInfo[]>(`/api/tags?scope=${scope}`),

  retrievals: (params: { scope: Scope; page?: number; pageSize?: number; q?: string }) => {
    const sp = new URLSearchParams();
    sp.set("scope", params.scope);
    if (params.page) sp.set("page", String(params.page));
    if (params.pageSize) sp.set("page_size", String(params.pageSize));
    if (params.q) sp.set("q", params.q);
    return jsonFetch<RetrievalsResponse>(`/api/retrievals?${sp}`);
  },

  retrievalDetail: (scope: Scope, id: string) =>
    jsonFetch<RetrievalDetail>(`/api/retrievals/${scope}/${id}`),

  turnRetrievals: (scope: Scope, turnId: string) =>
    jsonFetch<TurnRetrievalsResponse>(`/api/turns/${scope}/${turnId}/retrievals`),

  topReferenced: (scope: Scope, limit = 10) =>
    jsonFetch<TopReferencedResponse>(`/api/top-referenced?scope=${scope}&limit=${limit}`),

  regenerateSummary: (scope: Scope, id: string) =>
    jsonFetch<{ ok: true; summary: string; summary_model: string; summary_source: string }>(
      `/api/turns/${scope}/${id}/summary`,
      { method: "POST" },
    ),

  openFile: (path: string, line?: number) =>
    jsonFetch<{ ok: true; method: string; path: string; line: number | null }>("/api/open-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, line }),
    }),

  getSettings: () => jsonFetch<AppSettings>("/api/settings"),

  putSettings: (settings: AppSettings) =>
    jsonFetch<{ ok: true }>("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    }),
};
