import { useEffect } from "react";
import { useI18n } from "../i18n";

export function HelpModal({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const items = ["what", "lens", "layout", "scope", "search"] as const;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal help" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={t("help.title")}>
        <button className="modal-close" onClick={onClose} aria-label={t("close")}>
          ×
        </button>
        <h2 className="help-title">{t("help.title")}</h2>
        <dl className="help-list">
          {items.map((k) => (
            <div className="help-item" key={k}>
              <dt>{t(`help.${k}.dt`)}</dt>
              <dd>{t(`help.${k}.dd`)}</dd>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}
