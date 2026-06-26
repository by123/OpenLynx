export type Scope = "project" | "global";
export type SearchMode = "keyword" | "semantic";

/** Which lens the workspace is browsing through. */
export type Lens = "memory" | "retrieval";

export interface TagAttachment {
  name: string;
  kind: "user" | "project" | "module" | "custom" | string;
  source: "auto" | "manual" | string;
  confidence?: number | null;
}

export interface Turn {
  id: string;
  session_id: string;
  ts: number;
  cwd?: string | null;
  user_msg: string;
  assistant_msg: string;
  tags: TagAttachment[];
  score?: number | null;
  retrieval_count?: number;
  summary?: string | null;
  summary_source?: string | null;
  summary_model?: string | null;
  summary_ts?: number | null;
}

export interface RetrievalSummary {
  id: string;
  ts: number;
  session_id: string | null;
  cwd: string | null;
  prompt: string;
  scope_used: string | null;
  hit_count: number;
}

export interface RetrievalHit {
  turn_id: string;
  scope: string | null;
  kind: string | null;
  score: number;
  rank: number;
  turn: Turn | null;
}

export interface RetrievalDetail extends RetrievalSummary {
  hits: RetrievalHit[];
}

export interface RetrievalsResponse {
  items: RetrievalSummary[];
  total: number;
}

export interface TurnRetrievalsResponse {
  items: Array<RetrievalSummary & { score: number; rank: number }>;
  total: number;
}

export interface TurnsResponse {
  items: Turn[];
  total: number;
  mode: SearchMode;
}

export interface TopReferencedResponse {
  items: Turn[];
}

export interface ScopesResponse {
  project: boolean;
  global: boolean;
  project_dir: string | null;
  global_dir: string;
  cwd: string;
  /** SQLite row counts only; used to pick default scope without opening Chroma. */
  global_turn_count: number;
  project_turn_count: number;
}

export interface TagInfo {
  name: string;
  kind: string;
  count: number;
  created_at?: number;
}

/** What is currently open in the detail pane. */
export type Selection =
  | { kind: "memory"; turn: Turn }
  | { kind: "retrieval"; item: RetrievalSummary };

export interface AppSettings {
  summary_enabled: boolean;
  top_k: number;
  min_score: number;
  scope: string;
  summary_backend: string;
  openai_api_key_set: boolean;
  voyage_api_key_set: boolean;
  deepseek_api_key_set: boolean;
  qwen_api_key_set: boolean;
  openai_model: string;
  openai_base_url: string;
  deepseek_model: string;
  qwen_model: string;
  embedding_backend: string;
  openai_embedding_model: string;
  voyage_model: string;
  // cloud sync (Turso)
  sync_enabled: boolean;
  turso_org: string;
  turso_group: string;
  sync_url: string;
  turso_api_token_set: boolean;
  sync_token_set: boolean;
  // actual key values returned from server and sent on save
  openai_api_key_value?: string;
  voyage_api_key_value?: string;
  deepseek_api_key_value?: string;
  qwen_api_key_value?: string;
  turso_api_token_value?: string;
  sync_token_value?: string;
  openai_api_key?: string;
  voyage_api_key?: string;
  deepseek_api_key?: string;
  qwen_api_key?: string;
  turso_api_token?: string;
  sync_token?: string;
}
