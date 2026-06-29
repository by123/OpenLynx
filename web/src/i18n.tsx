import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";

export type Lang = "en" | "zh";

type Entry = { en: string; zh: string };

const D: Record<string, Entry> = {
  // ── top bar ──
  "brand.sub": { en: "Local memory", zh: "本地记忆库" },
  "view.memory": { en: "Memory", zh: "记忆" },
  "view.retrieval": { en: "Retrievals", zh: "检索记录" },
  "view.memory.title": { en: "Every turn the assistant remembers", zh: "助手记住的每一轮对话" },
  "view.retrieval.title": { en: "When the assistant recalled past memories", zh: "助手调用历史记忆的记录" },
  "scope.label": { en: "Scope", zh: "范围" },
  "scope.project": { en: "Project", zh: "本项目" },
  "scope.all": { en: "All projects", zh: "全部项目" },
  "action.help": { en: "Help", zh: "使用说明" },
  "action.help.aria": { en: "Open help", zh: "打开使用说明" },
  "action.settings": { en: "Settings", zh: "设置" },
  "action.settings.aria": { en: "Open settings", zh: "打开设置" },
  "theme.toLight": { en: "Switch to light", zh: "切换到浅色" },
  "theme.toDark": { en: "Switch to dark", zh: "切换到深色" },
  "theme.aria": { en: "Toggle theme", zh: "切换主题" },
  "lang.title": { en: "Language / 语言", zh: "语言 / Language" },
  "close": { en: "Close", zh: "关闭" },
  "loading": { en: "Loading…", zh: "加载中…" },

  // ── gallery toolbar ──
  "search.memory": { en: "Search memory…", zh: "搜索记忆…" },
  "search.prompts": { en: "Search prompts…", zh: "搜索提问…" },
  "search.memory.aria": { en: "Search memory", zh: "搜索记忆" },
  "search.prompts.aria": { en: "Search prompts", zh: "搜索提问" },
  "mode.keyword": { en: "Keyword", zh: "关键词" },
  "mode.semantic": { en: "Semantic", zh: "语义" },
  "mode.aria": { en: "Search mode", zh: "搜索方式" },
  "view.layout": { en: "Layout", zh: "布局" },
  "view.grid": { en: "Grid view", zh: "网格视图" },
  "view.list": { en: "List view", zh: "列表视图" },
  "select.enter": { en: "Select", zh: "选择" },
  "select.done": { en: "Done", zh: "完成" },
  "select.count": { en: "{n} selected", zh: "已选 {n} 项" },
  "select.all": { en: "Select page", zh: "全选本页" },
  "select.clearSel": { en: "Clear", zh: "清除" },
  "select.delete": { en: "Delete {n}", zh: "删除 {n} 项" },
  "select.deleting": { en: "Deleting…", zh: "删除中…" },
  "select.confirm": {
    en: "Delete {n} selected memories? This cannot be undone.",
    zh: "确定删除选中的 {n} 条记忆？此操作不可撤销。",
  },
  "select.deleteFail": { en: "Delete failed: {e}", zh: "删除失败：{e}" },
  "source.all": { en: "All", zh: "全部" },
  "source.top": { en: "Most retrieved", zh: "最常被检索" },
  "source.top.title": { en: "Memories other conversations retrieved most", zh: "被其它对话调用最多的记忆" },
  "source.aria": { en: "List source", zh: "列表来源" },
  "date.aria": { en: "Filter by date", zh: "按日期筛选" },
  "clear": { en: "Clear", zh: "清除" },

  // ── status / counts ──
  "status.topReferenced": { en: "Most retrieved · {n}", zh: "最常被检索 · {n} 条" },
  "status.memories": { en: "{n} memories", zh: "{n} 条记忆" },
  "status.retrievals": { en: "{n} retrievals", zh: "{n} 条检索记录" },
  "status.suffix.date": { en: " · {date}", zh: " · {date}" },
  "status.suffix.tag": { en: " · #{tag}", zh: " · #{tag}" },
  "status.suffix.query": { en: " · “{q}”", zh: " · “{q}”" },

  // ── empty states ──
  "empty.noMatch": { en: "No matching results", zh: "没有匹配的结果" },
  "empty.noMemory": { en: "No memories yet", zh: "这里还没有记忆" },
  "empty.noRetrieval": { en: "No retrievals yet", zh: "还没有检索记录" },
  "empty.body.filter": { en: "Try a different keyword, or clear the filters.", zh: "换个关键词，或清除筛选条件再试。" },
  "empty.body.memory": {
    en: "As you talk with your coding assistant, every turn is saved here.",
    zh: "当你和编程助手对话时，每一轮内容会自动存到这里。",
  },
  "empty.body.retrieval": {
    en: "When the assistant recalls past memories in a new conversation, each recall is logged here.",
    zh: "当助手在新对话里调用历史记忆时，每次调用都会记录在这里。",
  },

  // ── pagination / cards ──
  "page.prev": { en: "Prev", zh: "上一页" },
  "page.next": { en: "Next", zh: "下一页" },
  "card.q": { en: "Q: {text}", zh: "问：{text}" },
  "qa.q": { en: "Q", zh: "问" },
  "qa.a": { en: "A", zh: "答" },
  "retrieval.hits": { en: "{n} hits", zh: "命中 {n} 条" },
  "retrieval.hitsCard": { en: "{n} retrieved memories", zh: "命中 {n} 条历史记忆" },

  // ── detail: memory ──
  "detail.retrievedTimes": { en: "Retrieved {n}×", zh: "被检索 {n} 次" },
  "detail.delete": { en: "Delete", zh: "删除" },
  "detail.delete.confirm": { en: "Delete this memory? This cannot be undone.", zh: "确定删除这条记忆？此操作不可撤销。" },
  "detail.deleteFail": { en: "Delete failed: {e}", zh: "删除失败：{e}" },
  "summary.label": { en: "Summary", zh: "摘要" },
  "summary.source": { en: "via {s}", zh: "来源 {s}" },
  "summary.model": { en: "model {m}", zh: "模型 {m}" },
  "summary.regenerate": { en: "Regenerate", zh: "重新生成" },
  "summary.generate": { en: "Generate", zh: "生成摘要" },
  "summary.generating": { en: "Generating…", zh: "生成中…" },
  "summary.empty": { en: "No summary yet. Click “Generate”.", zh: "暂无摘要（可点击“生成摘要”生成）" },
  "role.user": { en: "User", zh: "用户" },
  "role.assistant": { en: "Assistant", zh: "助手" },
  "tag.add": { en: "+ tag", zh: "+ 标签" },
  "tag.remove": { en: "Remove tag", zh: "移除标签" },
  "tag.addFail": { en: "Add tag failed: {e}", zh: "添加标签失败：{e}" },
  "tag.removeFail": { en: "Remove tag failed: {e}", zh: "移除标签失败：{e}" },
  "refpanel.title": { en: "Prompts that retrieved this", zh: "调用过这条记忆的提问" },
  "refpanel.empty": { en: "No retrievals yet", zh: "暂无调用记录" },

  // ── detail: retrieval ──
  "retrieval.prompt": { en: "Prompt", zh: "提问" },
  "retrieval.hitsTitle": { en: "Retrieved memories ({n})", zh: "命中的历史记忆（{n}）" },
  "retrieval.noHits": { en: "No hits", zh: "无命中" },
  "retrieval.openMemory": { en: "Open memory", zh: "打开记忆" },
  "retrieval.q": { en: "Q: ", zh: "问：" },
  "retrieval.a": { en: "A: ", zh: "答：" },
  "retrieval.deleted": { en: "Memory deleted ({id}…)", zh: "记忆已删除（{id}…）" },

  // ── help ──
  "help.title": { en: "Help", zh: "使用说明" },
  "help.what.dt": { en: "What is this", zh: "这是什么" },
  "help.what.dd": {
    en: "Openlynx is your coding assistant's local memory. Every turn is remembered, and relevant new prompts recall it automatically.",
    zh: "Openlynx 是编程助手的本地记忆库。每一轮对话会被记住，之后相关的新提问会自动调用这些记忆。",
  },
  "help.lens.dt": { en: "Memory / Retrievals", zh: "记忆 / 检索记录" },
  "help.lens.dd": {
    en: "Switch lens at the top. Memory is what was stored; Retrievals is when it was used.",
    zh: "左上切换视角。记忆是存了什么；检索记录是用了什么。",
  },
  "help.layout.dt": { en: "Browse + detail", zh: "浏览 + 详情" },
  "help.layout.dd": {
    en: "Cards are a quick index. Click any card to open its full content, summary, tags and retrieval history in a drawer.",
    zh: "卡片是快速索引，点任意一张在抽屉里看完整内容、摘要、标签和调用情况。",
  },
  "help.scope.dt": { en: "Project tabs", zh: "项目标签" },
  "help.scope.dd": {
    en: "The top bar has one tab per memory directory found on this machine, plus All projects (the global store). Manage which tabs show — and rescan — under Settings → Memory directories.",
    zh: "顶部为本机发现的每个记忆目录各显示一个标签，外加「全部项目」（全局库）。在 设置 → 记忆目录 里可管理显示哪些标签并重新扫描。",
  },
  "help.search.dt": { en: "Keyword / Semantic", zh: "关键词 / 语义" },
  "help.search.dd": {
    en: "Keyword matches text exactly; Semantic finds by meaning (needs an API key in Settings).",
    zh: "关键词精确匹配文字；语义按意思相近查找（需在设置里配置 API key）。",
  },

  // ── settings ──
  "settings.title": { en: "Settings", zh: "设置" },
  "settings.sec.projects": { en: "Memory directories", zh: "记忆目录" },
  "settings.projects.desc": {
    en: "Every OpenLynx memory directory found on this machine shows up as a tab. Hide the ones you don't want to see — they stay on disk and can be shown again anytime.",
    zh: "本机发现的每个 OpenLynx 记忆目录都会显示为一个标签页。把不想看的隐藏起来即可——数据仍保留在磁盘上，随时可以再次显示。",
  },
  "settings.projects.found": { en: "{n} project directories", zh: "发现 {n} 个项目目录" },
  "settings.projects.rescan": { en: "Rescan", zh: "重新扫描" },
  "settings.projects.scanning": { en: "Scanning…", zh: "扫描中…" },
  "settings.projects.empty": {
    en: "No project memory directories found yet. Click Rescan, or open a coding session in a project.",
    zh: "还没有发现项目记忆目录。点击「重新扫描」，或在某个项目里开启一次编程会话。",
  },
  "settings.projects.current": { en: "current", zh: "当前" },
  "settings.projects.turns": { en: "{n} memories", zh: "{n} 条记忆" },
  "settings.projects.show": { en: "Show this tab", zh: "显示此标签" },
  "settings.projects.hide": { en: "Hide this tab", zh: "隐藏此标签" },
  "settings.sec.embeddings": { en: "Embeddings", zh: "向量检索 (Embeddings)" },
  "settings.sec.injection": { en: "Memory injection", zh: "记忆注入" },
  "settings.sec.summarization": { en: "Summarization", zh: "摘要生成" },
  "settings.backend": { en: "Provider", zh: "服务商" },
  "settings.backend.hint.embed": { en: "Embedding provider for semantic search", zh: "语义搜索使用的向量服务" },
  "settings.backend.hint.summary": { en: "API provider for summaries", zh: "生成摘要使用的 API 服务" },
  "settings.voyageModel": { en: "Voyage model", zh: "Voyage 模型" },
  "settings.openaiEmbedModel": { en: "OpenAI embedding model", zh: "OpenAI 嵌入模型" },
  "settings.warn.voyage": {
    en: "⚠ A Voyage API key is required for semantic search and memory injection.",
    zh: "⚠ 语义搜索与记忆注入需要配置 Voyage API key。",
  },
  "settings.warn.openaiEmbed": { en: "⚠ An OpenAI API key is required for embeddings.", zh: "⚠ 向量嵌入需要配置 OpenAI API key。" },
  "settings.topk": { en: "Top-K results", zh: "注入条数 (Top-K)" },
  "settings.topk.hint": { en: "Memories injected per prompt", zh: "每次提问注入的记忆条数" },
  "settings.minScore": { en: "Min score", zh: "最低相关度" },
  "settings.minScore.hint": { en: "Similarity threshold; below it nothing is injected (0–1)", zh: "相似度阈值，低于此值不注入（0–1）" },
  "settings.retrScope": { en: "Retrieval scope", zh: "检索范围" },
  "settings.retrScope.hint": { en: "Which memory store to search", zh: "从哪个记忆库检索" },
  "settings.scope.auto": { en: "auto (project → all projects)", zh: "自动（本项目 → 全部项目）" },
  "settings.scope.global": { en: "all projects only", zh: "仅全部项目" },
  "settings.scope.project": { en: "this project only", zh: "仅本项目" },
  "settings.enableSummary": { en: "Enable summarization", zh: "开启摘要" },
  "settings.enableSummary.hint": { en: "Generate a compact summary after each turn", zh: "每轮对话后自动生成一条精简摘要" },
  "settings.model": { en: "Model", zh: "模型" },
  "settings.model.hint.openai": { en: "OpenAI model used for summaries", zh: "用于生成摘要的 OpenAI 模型" },
  "settings.model.hint.deepseek": { en: "DeepSeek model used for summaries", zh: "用于生成摘要的 DeepSeek 模型" },
  "settings.model.hint.qwen": { en: "Qwen model used for summaries", zh: "用于生成摘要的 Qwen 模型" },
  "settings.baseUrl": { en: "Base URL", zh: "接口地址" },
  "settings.warn.keyMissing": {
    en: "⚠ No API key set for the selected provider; summaries will not run.",
    zh: "⚠ 所选服务商还没有配置 API key，摘要不会生成。",
  },
  "settings.cancel": { en: "Cancel", zh: "取消" },
  "settings.save": { en: "Save", zh: "保存" },
  "settings.saved": { en: "Saved", zh: "已保存" },
  "settings.saving": { en: "Saving…", zh: "保存中…" },
  "settings.key.willRemove": { en: "○ will be removed", zh: "○ 将移除" },
  "settings.key.configured": { en: "● configured", zh: "● 已配置" },
  "settings.key.notset": { en: "○ not set", zh: "○ 未设置" },
  "settings.key.remove": { en: "Remove key", zh: "移除密钥" },
  "settings.sec.sync": { en: "Cloud sync (Turso)", zh: "云端同步 (Turso)" },
  "settings.enableSync": { en: "Enable cloud sync", zh: "开启云端同步" },
  "settings.enableSync.hint": {
    en: "Sync the global store to its Turso replica. Project stores are configured per-project with `lynx-memory sync init`.",
    zh: "将全局记忆库同步到 Turso 副本。项目记忆库需在各自目录下用 `lynx-memory sync init` 单独配置。",
  },
  "settings.tursoOrg": { en: "Turso organization", zh: "Turso 组织" },
  "settings.tursoOrg.hint": { en: "Org slug used to provision per-project databases", zh: "用于为各项目创建数据库的组织标识" },
  "settings.tursoGroup": { en: "Turso group", zh: "Turso 分组" },
  "settings.tursoGroup.hint": { en: "Database group new project DBs are created in", zh: "新建项目数据库所属的分组" },
  "settings.syncUrl": { en: "Global sync URL", zh: "全局同步地址" },
  "settings.syncUrl.hint": { en: "libsql:// URL of the global store's Turso database", zh: "全局记忆库 Turso 数据库的 libsql:// 地址" },
  "settings.warn.sync": {
    en: "⚠ A Turso API token is needed to provision project databases via `sync init`.",
    zh: "⚠ 通过 `sync init` 创建项目数据库需要配置 Turso API token。",
  },
  "settings.sync.global.title": { en: "Global store sync", zh: "全局库同步" },
  "settings.sync.global.desc": {
    en: "Sync the global store (~/.openlynx) to a Turso database you already created. Needs that database's URL and token.",
    zh: "把全局记忆库（~/.openlynx）同步到一个你已建好的 Turso 数据库，需要填该库的地址和 token。",
  },
  "settings.sync.provision.title": { en: "Auto-provision project databases", zh: "自动创建项目库" },
  "settings.sync.provision.desc": {
    en: "Lets `lynx-memory sync init` create a Turso database for each project automatically. Only needed if you want project memory in the cloud too — the global sync above does not use these.",
    zh: "让 `lynx-memory sync init` 自动为每个项目在 Turso 建库。仅当你也想把项目记忆上云时才需要——上面的全局同步用不到这些。",
  },
  "settings.warn.syncGlobal": {
    en: "⚠ Enabling sync needs the sync URL and token below to be filled in.",
    zh: "⚠ 开启同步后，需要填写下方的同步地址和 token 才会生效。",
  },
};

export type TFunc = (key: string, vars?: Record<string, string | number>) => string;

interface I18nValue {
  lang: Lang;
  setLang: (l: Lang) => void;
  t: TFunc;
}

const Ctx = createContext<I18nValue | null>(null);

export function LangProvider({ children }: { children: ReactNode }) {
  const [lang, setLang] = useState<Lang>(() => {
    const s = typeof localStorage !== "undefined" ? localStorage.getItem("cm-lang") : null;
    return s === "zh" || s === "en" ? s : "en";
  });

  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    try {
      localStorage.setItem("cm-lang", lang);
    } catch {
      /* ignore */
    }
  }, [lang]);

  const t: TFunc = (key, vars) => {
    const entry = D[key];
    let s = entry ? entry[lang] : key;
    if (vars) {
      for (const k of Object.keys(vars)) {
        s = s.split(`{${k}}`).join(String(vars[k]));
      }
    }
    return s;
  };

  return <Ctx.Provider value={{ lang, setLang, t }}>{children}</Ctx.Provider>;
}

export function useI18n(): I18nValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useI18n must be used within LangProvider");
  return v;
}
