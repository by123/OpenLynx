import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import { parseLocalFileTarget, sanitizeMarkdownHref } from "../utils/linkSanitizer";

function normalizeToolMarkdown(text: string): string {
  return text.replace(
    /(^|\n)\[tool: apply_patch\]\n([\s\S]*?\*\*\* End Patch)/g,
    (_match, prefix: string, patch: string) =>
      `${prefix}**Tool: apply_patch**\n\n\`\`\`diff\n${patch.trim()}\n\`\`\``,
  );
}

function DiffCode({ children }: { children: ReactNode }) {
  const text = String(children).replace(/\n$/, "");
  return (
    <code className="diff-code">
      {text.split("\n").map((line, i) => {
        const kind =
          line.startsWith("+") && !line.startsWith("+++")
            ? "added"
            : line.startsWith("-") && !line.startsWith("---")
              ? "removed"
              : line.startsWith("@@")
                ? "hunk"
                : line.startsWith("***")
                  ? "meta"
                  : "context";
        return (
          <span key={i} className={`diff-line ${kind}`}>
            {line || " "}
          </span>
        );
      })}
    </code>
  );
}

const components: Components = {
  a: ({ node, ...props }) => {
    const localTarget = parseLocalFileTarget(props.href);
    return (
      <a
        {...props}
        href={sanitizeMarkdownHref(props.href)}
        target="_blank"
        rel="noreferrer noopener"
        onClick={(e) => {
          if (!localTarget) return;
          e.preventDefault();
          api.openFile(localTarget.path, localTarget.line).catch((err) => {
            alert(`Open file failed: ${err}`);
          });
        }}
      />
    );
  },
  code: ({ node, className, children, ...props }) => {
    if (className?.includes("language-diff")) {
      return <DiffCode>{children}</DiffCode>;
    }
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  },
};

export function Markdown({ text, inline = false }: { text: string; inline?: boolean }) {
  return (
    <div className={inline ? "md inline" : "md"}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {inline ? text : normalizeToolMarkdown(text)}
      </ReactMarkdown>
    </div>
  );
}
